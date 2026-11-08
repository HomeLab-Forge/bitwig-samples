#!/usr/bin/env python3
"""Build each plugin's drum mapping from one layout file.

    ./drumlayout.py doc -o ../layouts/core16.md     # the tables, regenerated
    ./drumlayout.py mpd218                          # does BW_PADS still match?
    ./drumlayout.py mpk                             # the mpk-program.py command
    ./drumlayout.py kit --kit bitwig-acoustic       # which sample per layer
    ./drumlayout.py multisample --kit bitwig-acoustic -o out/
    ./drumlayout.py dropfolder --kit bitwig-acoustic -o out/
    ./drumlayout.py sfz --kit bitwig-acoustic -o out/

The targets that read samples need the samples: run those on the box, in the
desktop session's own filesystem. `doc`, `mpd218` and `mpk` read nothing but
the layout and run anywhere.
"""

import argparse
import fnmatch
import glob
import math
import os
import shutil
import sys
import wave
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DRUMS = os.path.dirname(HERE)
REPO = os.path.dirname(DRUMS)

# How much of a sample is measured. Long enough to catch a cymbal's attack,
# short enough that a 20-second ride tail does not drown the hit it starts with.
RMS_WINDOW_S = 0.2


def die(msg):
    print(f"drumlayout: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"drumlayout: warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# the layout

def load_yaml(path):
    try:
        import yaml
    except ImportError:
        die("needs PyYAML (python3-yaml)")
    try:
        with open(path) as fh:
            return yaml.safe_load(fh)
    except OSError as exc:
        die(f"{exc}")


def load_layout(name_or_path):
    path = name_or_path
    if not os.path.sep in path and not path.endswith((".yaml", ".yml")):
        path = os.path.join(DRUMS, "layouts", name_or_path + ".yaml")
    layout = load_yaml(path)
    roles, pads = layout.get("roles") or {}, layout.get("pads") or []
    if not roles or not pads:
        die(f"{path}: a layout needs both roles and pads")
    seen = {}
    for pad in pads:
        for field in ("pad", "note", "role"):
            if field not in pad:
                die(f"{path}: pad {pad} has no {field}")
        if pad["role"] not in roles:
            die(f"{path}: pad {pad['pad']} uses undeclared role {pad['role']!r}")
        if pad["pad"] in seen:
            die(f"{path}: pad {pad['pad']} appears twice")
        if not 0 <= pad["note"] <= 127:
            die(f"{path}: pad {pad['pad']} note {pad['note']} is not a MIDI note")
        seen[pad["pad"]] = pad
    for pad in pads:
        twin = pad.get("same_as")
        if twin is None:
            continue
        if twin not in seen:
            die(f"{path}: pad {pad['pad']} is same_as {twin}, which does not exist")
        if seen[twin]["role"] != pad["role"]:
            die(f"{path}: pad {pad['pad']} is same_as {twin} but their roles differ")
    layout["pads"] = sorted(pads, key=lambda p: p["pad"])
    layout.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    layout.setdefault("title", layout["name"])
    layout.setdefault("channel", 10)
    layout["_path"] = path
    return layout


def load_kit(name_or_path, layout):
    path = name_or_path
    if not os.path.sep in path and not path.endswith((".yaml", ".yml")):
        path = os.path.join(DRUMS, "kits", name_or_path + ".yaml")
    kit = load_yaml(path)
    if not kit.get("samples"):
        die(f"{path}: a kit needs samples")
    missing = [r for r in used_roles(layout) if r not in kit["samples"]]
    if missing:
        die(f"{path}: no samples for {', '.join(missing)}")
    kit.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    kit.setdefault("title", kit["name"])
    kit.setdefault("defaults", {})
    kit["content_root"] = os.path.expanduser(kit.get("content_root") or ".")
    kit["_path"] = path
    return kit


def used_roles(layout):
    """Roles in pad order, each one once."""
    out = []
    for pad in layout["pads"]:
        if pad["role"] not in out:
            out.append(pad["role"])
    return out


def label_of(layout, pad):
    return layout["roles"][pad["role"]].get("label", pad["role"])


# --------------------------------------------------------------------------
# samples

class Sample:
    def __init__(self, path, frames, rate, channels, width, peak, rms):
        self.path, self.frames, self.rate = path, frames, rate
        self.channels, self.width = channels, width
        self.peak, self.rms = peak, rms

    @property
    def seconds(self):
        return self.frames / float(self.rate) if self.rate else 0.0

    @property
    def basename(self):
        return os.path.basename(self.path)


def measure(path):
    """Read a wav and report how loud it is, by peak and by opening RMS."""
    try:
        with wave.open(path) as w:
            frames, width = w.getnframes(), w.getsampwidth()
            rate, channels = w.getframerate(), w.getnchannels()
            raw = w.readframes(frames)
    except (wave.Error, OSError) as exc:
        warn(f"skipping {path}: {exc}")
        return None
    if width not in (1, 2, 3, 4) or not frames:
        warn(f"skipping {path}: {width * 8}-bit, {frames} frames")
        return None
    full = 1 << (8 * width - 1)
    step = width
    window = min(len(raw), int(RMS_WINDOW_S * rate) * channels * width)
    peak, total, count = 0, 0.0, 0
    for i in range(0, len(raw), step):
        chunk = raw[i:i + step]
        if len(chunk) < step:
            break
        if width == 1:
            value = chunk[0] - 128
        else:
            value = int.from_bytes(chunk, "little", signed=True)
        if abs(value) > peak:
            peak = abs(value)
        if i < window:
            total += float(value) * value
            count += 1
    rms = math.sqrt(total / count) / full if count else 0.0
    return Sample(path, frames, rate, channels, width, peak / float(full), rms)


def candidates(kit, role):
    spec = kit["samples"][role]
    if isinstance(spec, str):
        spec = {"glob": spec}
    pattern = spec.get("glob")
    if not pattern:
        die(f"{kit['_path']}: role {role} has no glob")
    paths = sorted(glob.glob(os.path.join(kit["content_root"], pattern)))
    for drop in spec.get("exclude") or []:
        paths = [p for p in paths
                 if not fnmatch.fnmatch(os.path.basename(p), drop)]
    if not paths:
        die(f"{kit['_path']}: role {role} matched nothing under "
            f"{kit['content_root']}")
    return spec, paths


def layers_for(kit, role):
    """The samples for one role, softest first, spread over the velocities.

    The candidates are measured rather than read off their names: no two
    folders in Bitwig's content agree on whether the number, the letter or
    nothing at all is the dynamic, and a ladder built from the names alone puts
    a hard hit on a soft one. Duplicates — the same recording under two names —
    collapse into one.
    """
    spec, paths = candidates(kit, role)
    want = int(spec.get("layers", kit["defaults"].get("layers", 4)))
    longest = float(spec.get("max_seconds",
                             kit["defaults"].get("max_seconds", 8.0)))
    measured, seen = [], set()
    for path in paths:
        sample = measure(path)
        if sample is None:
            continue
        if sample.seconds > longest:
            continue
        key = (sample.frames, round(sample.peak, 6), round(sample.rms, 6))
        if key in seen:
            continue
        seen.add(key)
        measured.append(sample)
    if not measured:
        die(f"role {role}: every candidate was dropped; check max_seconds")
    measured.sort(key=lambda s: s.rms)
    if len(measured) <= want:
        if len(measured) < want:
            warn(f"role {role}: {len(measured)} usable samples for "
                 f"{want} layers")
        chosen = measured
    else:
        last = len(measured) - 1
        picks = sorted({round(i * last / (want - 1)) for i in range(want)}) \
            if want > 1 else [last]
        chosen = [measured[i] for i in picks]
    return spec, chosen


def velocity_zones(count):
    edges = [round(i * 127 / count) for i in range(count + 1)]
    return [(edges[i] + 1, edges[i + 1]) for i in range(count)]


def resolve(kit, layout):
    """role -> (spec, [Sample softest first]), measured once."""
    return {role: layers_for(kit, role) for role in used_roles(layout)}


# --------------------------------------------------------------------------
# targets

def target_doc(args, layout):
    rows, roles = layout["pads"], layout["roles"]
    by_pad = {p["pad"]: p for p in rows}
    out = []
    out.append(f"# {layout['title']} on the pads")
    out.append("")
    out.append(f"Generated from [`{os.path.relpath(layout['_path'], DRUMS)}`]"
               f"({os.path.relpath(layout['_path'], os.path.join(DRUMS, 'layouts'))})"
               " by `drums/tools/drumlayout.py doc`. Edit the layout, not this.")
    out.append("")
    out.append("## The grid")
    out.append("")
    out.append(f"Pad 1 is bottom-left. All sixteen send on MIDI channel "
               f"{layout['channel']}.")
    out.append("")
    out.append("| | | | |")
    out.append("|---|---|---|---|")
    for first in (13, 9, 5, 1):
        cells = []
        for pad in range(first, first + 4):
            if pad not in by_pad:
                cells.append("")
                continue
            entry = by_pad[pad]
            mark = " ·" if entry.get("same_as") else ""
            cells.append(f"**{pad}** · {entry['note']}<br>"
                         f"{label_of(layout, entry)}{mark}")
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    doubled = [p for p in rows if p.get("same_as")]
    if doubled:
        out.append("A pad marked · plays the same drum as another: " + ", ".join(
            f"pad {p['pad']} doubles pad {p['same_as']}" for p in doubled)
            + ". That is SSD5's own layout kept intact, so the pads feel the"
              " same whichever plugin is loaded.")
        out.append("")
    out.append("## Every pad")
    out.append("")
    out.append("| Pad | Note | Role | Plays | SSD5 articulation | GM note | Colour |")
    out.append("|---|---|---|---|---|---|---|")
    for pad in rows:
        role = roles[pad["role"]]
        out.append(f"| {pad['pad']} | {pad['note']} | `{pad['role']}` | "
                   f"{label_of(layout, pad)} | {role.get('ssd5', '—')} | "
                   f"{role.get('gm', '—')} | {role.get('colour', '—')} |")
    out.append("")
    out.append("## Reaching a General MIDI plugin")
    out.append("")
    moved = sum(1 for p in rows
                if roles[p["role"]].get("gm") not in (None, p["note"]))
    out.append("The pads send the left column. A plugin mapped to General MIDI"
               " — MT Power, Sitala, EZdrummer and most of what is still to be"
               " tried — hears the right one, so it needs the notes moved on"
               f" the way in. {moved} of the {len(rows)} land somewhere else.")
    out.append("")
    out.append("| Pad sends | Drum | GM wants |")
    out.append("|---|---|---|")
    for pad in rows:
        gm = roles[pad["role"]].get("gm")
        same = "" if gm is None or gm == pad["note"] else " **→**"
        out.append(f"| {pad['note']}{same} | {label_of(layout, pad)} | "
                   f"{gm if gm is not None else '—'} |")
    out.append("")
    text = "\n".join(out) + "\n"
    if args.output and args.output != "-":
        with open(args.output, "w") as fh:
            fh.write(text)
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)


def target_kit(args, layout):
    kit = load_kit(args.kit, layout)
    chosen = resolve(kit, layout)
    print(f"{kit['title']} — {kit['content_root']}")
    for role in used_roles(layout):
        spec, samples = chosen[role]
        zones = velocity_zones(len(samples))
        note = f"  [{spec['substitute']}]" if spec.get("substitute") else ""
        print(f"\n{role}{note}")
        for (low, high), sample in zip(zones, samples):
            print(f"  vel {low:3d}-{high:3d}  rms {sample.rms:.4f}  "
                  f"{sample.seconds:5.2f}s  {sample.basename}")


def multisample_entries(layout, chosen):
    """Archive name per sample path, unique and stable."""
    names, taken = {}, {}
    for role in used_roles(layout):
        for sample in chosen[role][1]:
            if sample.path in names:
                continue
            name = sample.basename
            if taken.get(name, sample.path) != sample.path:
                stem, ext = os.path.splitext(name)
                name = f"{stem} ({role}){ext}"
            taken[name] = sample.path
            names[sample.path] = name
    return names


def target_multisample(args, layout):
    kit = load_kit(args.kit, layout)
    chosen = resolve(kit, layout)
    names = multisample_entries(layout, chosen)
    title = f"{layout['title']} {kit['title']}"
    root = ET.Element("multisample", {"name": title})
    ET.SubElement(root, "generator").text = "drumlayout.py"
    ET.SubElement(root, "category").text = "Drums"
    ET.SubElement(root, "creator").text = kit.get("creator", "audio-rig")
    ET.SubElement(root, "description").text = (
        f"{layout['title']} drum layout. "
        f"{kit.get('description', kit['title'])}. "
        f"One key per pad from note {layout['pads'][0]['note']} up, "
        f"velocity split over the layers of each kit piece.")
    keywords = ET.SubElement(root, "keywords")
    for word in ("drums", "kit", layout["name"]):
        ET.SubElement(keywords, "keyword").text = word
    layer = ET.SubElement(root, "layer", {"name": "Default"})
    for pad in layout["pads"]:
        samples = chosen[pad["role"]][1]
        for (low, high), sample in zip(velocity_zones(len(samples)), samples):
            node = ET.SubElement(layer, "sample", {
                "file": names[sample.path],
                "gain": "0.000",
                "sample-start": "0.000",
                "sample-stop": f"{sample.frames}.000",
                "tune": "0.0",
            })
            # One key per zone and no tracking: a drum is not transposed across
            # the pad it sits on.
            ET.SubElement(node, "key", {
                "high": str(pad["note"]), "low": str(pad["note"]),
                "root": str(pad["note"]), "track": "false"})
            ET.SubElement(node, "velocity", {"low": str(low), "high": str(high)})
            ET.SubElement(node, "loop", {
                "mode": "off", "start": "0.000",
                "stop": f"{sample.frames}.000"})
    ET.indent(root, space="   ")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           + ET.tostring(root, encoding="unicode") + "\n")

    out = args.output or os.path.join(DRUMS, "out")
    if os.path.isdir(out) or not out.endswith(".multisample"):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, f"{title}.multisample")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    # Stored, not deflated, and the xml last: that is how Bitwig writes its own.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for path, name in names.items():
            zf.write(path, name)
        zf.writestr("multisample.xml", xml)
    zones = sum(len(chosen[p["role"]][1]) for p in layout["pads"])
    print(f"wrote {out}")
    print(f"  {len(layout['pads'])} pads, {zones} velocity zones, "
          f"{len(names)} samples, {os.path.getsize(out) // 1024} KiB")


def target_dropfolder(args, layout):
    kit = load_kit(args.kit, layout)
    chosen = resolve(kit, layout)
    out = args.output or os.path.join(DRUMS, "out", f"{kit['name']}-drop")
    os.makedirs(out, exist_ok=True)
    for pad in layout["pads"]:
        samples = chosen[pad["role"]][1]
        # The hardest layer but one: a kit dropped on a Drum Machine gets one
        # sample per pad, and the loudest take of a cymbal is usually the
        # longest wash rather than the most useful hit.
        pick = samples[max(0, len(samples) - 2)]
        name = f"{pad['note']} {label_of(layout, pad)}.wav"
        shutil.copyfile(pick.path, os.path.join(out, name))
    print(f"wrote {len(layout['pads'])} samples to {out}")
    print("  drag the folder onto a Drum Machine: one pad per file, from the "
          "lowest note up")


def target_sfz(args, layout):
    kit = load_kit(args.kit, layout)
    chosen = resolve(kit, layout)
    lines = [f"// {layout['title']} — {kit['title']}",
             "// Generated by drums/tools/drumlayout.py sfz.",
             "",
             "<global> loop_mode=one_shot ampeg_attack=0.001 ampeg_release=0.5",
             ""]
    for pad in layout["pads"]:
        samples = chosen[pad["role"]][1]
        lines.append(f"// pad {pad['pad']} — {label_of(layout, pad)}")
        lines.append(f"<group> key={pad['note']} "
                     f"group_label={label_of(layout, pad).replace(' ', '_')}")
        for (low, high), sample in zip(velocity_zones(len(samples)), samples):
            lines.append(f"<region> lovel={low} hivel={high} "
                         f"sample={sample.path}")
        lines.append("")
    out = args.output or os.path.join(DRUMS, "out")
    if os.path.isdir(out) or not out.endswith(".sfz"):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, f"{kit['name']}-{layout['name']}.sfz")
    with open(out, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out}")


# The MPD218's pad records: 48 of them from offset 22, eight bytes each, the
# note at +0 and the channel at +7. midi/mpd218/mappings/ssd5.md says how that
# was established.
MPD_BASE, MPD_STRIDE, MPD_CHANNEL, MPD_SIZE = 22, 8, 7, 549


def target_mpd218(args, layout):
    path = args.preset or os.path.join(
        REPO, "midi", "mpd218", "presets", "BW_PADS.mpd218")
    try:
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
    except OSError as exc:
        die(f"{exc}")
    if len(data) != MPD_SIZE:
        die(f"{path}: {len(data)} bytes, not the {MPD_SIZE} of a preset dump")
    drift = []
    for index, pad in enumerate(layout["pads"]):
        if index >= 16:
            break
        at = MPD_BASE + MPD_STRIDE * index
        if data[at] != pad["note"]:
            drift.append((f"pad {pad['pad']} note", at, data[at], pad["note"]))
        want_channel = layout["channel"]
        if data[at + MPD_CHANNEL] != want_channel:
            drift.append((f"pad {pad['pad']} channel", at + MPD_CHANNEL,
                          data[at + MPD_CHANNEL], want_channel))
    print(f"{os.path.relpath(path, REPO)}, bank A against "
          f"{layout['name']}: {'drift' if drift else 'matches'}")
    for what, at, have, want in drift:
        print(f"  {what}: byte {at} is {have}, layout says {want}")
    if not drift:
        return
    if not args.write:
        print("  --write patches those bytes in place")
        sys.exit(1)
    for _, at, _, want in drift:
        data[at] = want
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"  patched {len(drift)} bytes")
    print("  BW_AKAI keeps this preset in all sixteen slots, so re-run "
          "daw/bitwig/scripts/tools/bundle-programs.py (or daw/bitwig/link.sh) "
          "and press Send the bundled preset in its settings.")


def target_mpk(args, layout):
    pads = layout["pads"][:16]
    notes = [str(p["note"]) for p in pads]
    colours = [layout["roles"][p["role"]].get("colour", "unlit") for p in pads]
    spec_notes = ",".join(notes[:8]) + "/" + ",".join(notes[8:])
    pairs = [f"{c},{c}" for c in colours]
    spec_colours = ",".join(pairs[:8]) + "/" + ",".join(pairs[8:])
    tool = os.path.join("midi", "mpk-mini-plus", "tools", "mpk-program.py")
    source = args.input or os.path.join(
        "midi", "mpk-mini-plus", "programs", "BW_KEYS.syx")
    out = args.output or f"Bitwig_{layout['name'].upper()}.syx"
    print(f"# {layout['title']} on the MPK's sixteen pads, as an "
          f"mpk-program.py patch")
    print(f"./{tool} patch {source} -o {out} \\")
    print(f"    --name {layout['name'].upper()[:16]} \\")
    print(f"    --notes {spec_notes} \\")
    print(f"    --colors {spec_colours}")
    print()
    print("# The colours are one family per kit piece. BW_AKAI owns the live "
          "program and")
    print("# mutes every pad, so send this to a stored slot and play it with "
          "BW_AKAI off.")


TARGETS = {
    "doc": target_doc,
    "kit": target_kit,
    "multisample": target_multisample,
    "dropfolder": target_dropfolder,
    "sfz": target_sfz,
    "mpd218": target_mpd218,
    "mpk": target_mpk,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--layout", default="core16-bank-a",
                        help="layout name under drums/layouts, or a path")
    parser.add_argument("--kit", default="bitwig-acoustic",
                        help="kit name under drums/kits, or a path")
    parser.add_argument("-o", "--output", help="file or folder to write")
    parser.add_argument("--preset", help="mpd218: the preset to check")
    parser.add_argument("--write", action="store_true",
                        help="mpd218: patch the preset instead of reporting")
    parser.add_argument("--input", help="mpk: the program to patch from")
    args = parser.parse_args(argv)
    TARGETS[args.target](args, load_layout(args.layout))


if __name__ == "__main__":
    main()
