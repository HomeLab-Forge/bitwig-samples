#!/usr/bin/env python3
"""Turn an SFZ drum kit into Bitwig kits for a drum layout, at the kit's defaults.

    ./sfzkit.py inspect --kit big-rusty-drums       # what each pad resolves to
    ./sfzkit.py build --kit big-rusty-drums -o out/ # the Bitwig kits
    ./sfzkit.py build --kit big-rusty-drums --layout core16-bank-a \
        --layout core16-bank-b -o out/              # both MPD218 banks

An SFZ kit is a program, not a folder of samples: includes, defines, mic layers
gated and levelled by MIDI CC, round robins, tuning on CC. Bitwig's importer
reads none of that, so this evaluates the program the way an SFZ player would
with every CC at the value the program sets on load (`set_ccN`), keeps the
regions that would sound, and mixes the mic layers of each hit down to one
stereo sample. Bitwig's .multisample holds a gain and a tune per zone but no
pan, width or mic balance, so those are baked into the audio; the round robins
survive as Bitwig's own round-robin zone logic.

`build` writes, per kit:

    <Kit> <Layout>.multisample    every pad of every layout on one Sampler
    <Kit> <Layout> pads/NN Label.multisample
                                  one per pad, for a Drum Machine

It needs numpy and soundfile (python3-numpy, python3-soundfile), because most
SFZ kits are FLAC. Run it wherever those and the samples are.
"""

import argparse
import math
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
import drumlayout  # noqa: E402
# The SFZ engine, shared with non-drum instruments: everything from reading
# a program to writing a .multisample.
from sfz import *  # noqa: E402,F401,F403
from sfz import Evaluator, Mixer, Program, audio, die, hits, note_number, \
    warn, write_multisample  # noqa: E402,F401

DRUMS = drumlayout.DRUMS

# Headroom left on the loudest hit of a pad. What it takes off is put back as
# the zones' gain, so the level a pad plays at is the program's own.
PEAK = 10 ** (-1.0 / 20)

# --------------------------------------------------------------------------
# layouts

def merge_layouts(names):
    """Several layouts as one: their roles together, their pads by note.

    Each MPD218 bank is its own layout file, so a kit that answers both banks
    is built from both. A note claimed twice is an error, since one key can
    only play one drum.
    """
    layouts = [drumlayout.load_layout(name) for name in names]
    if len(layouts) == 1:
        return layouts[0]
    merged = dict(layouts[0])
    merged["roles"], merged["pads"] = {}, []
    taken = {}
    for layout in layouts:
        merged["roles"].update(layout["roles"])
        for pad in layout["pads"]:
            if pad["note"] in taken:
                die(f"note {pad['note']} is in both {taken[pad['note']]} "
                    f"and {layout['name']}")
            taken[pad["note"]] = layout["name"]
            merged["pads"].append(dict(pad))
    merged["pads"].sort(key=lambda p: p["note"])
    merged["name"] = "+".join(l["name"] for l in layouts)
    merged["title"] = layouts[0]["title"]
    return merged


# --------------------------------------------------------------------------
# kit files

def load_kit(name_or_path, layout, root=None):
    path = name_or_path
    if os.path.sep not in path and not path.endswith((".yaml", ".yml")):
        path = os.path.join(DRUMS, "kits", name_or_path + ".yaml")
    kit = drumlayout.load_yaml(path)
    if kit.get("source") != "sfz":
        die(f"{path}: not an SFZ kit (source: sfz)")
    for field in ("program", "roles"):
        if not kit.get(field):
            die(f"{path}: an SFZ kit needs {field}")
    missing = [r for r in drumlayout.used_roles(layout) if r not in kit["roles"]]
    if missing:
        die(f"{path}: no key for {', '.join(missing)}")
    kit.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    kit.setdefault("title", kit["name"])
    kit["content_root"] = os.path.expanduser(
        root or kit.get("content_root") or ".")
    kit["_path"] = path
    return kit


def role_spec(kit, role):
    spec = kit["roles"][role]
    if not isinstance(spec, dict):
        spec = {"key": spec}
    return spec


def variants(kit, role):
    """The articulations a role plays, main one first, each with a name.

    A role's `alt:` lists other articulations of the same drum, as `{key, cc,
    name}`. Pads that share the role take them in turn, so two kick pads in a
    bank are the kick and its alternative rather than two takes of one sound.
    """
    spec = role_spec(kit, role)
    out = [dict(spec, name=role)]
    alts = spec.get("alt") or []
    if isinstance(alts, dict):
        alts = [alts]
    for i, alt in enumerate(alts, start=1):
        if not isinstance(alt, dict):
            alt = {"key": alt}
        out.append(dict(alt, name=f"{role} {alt.get('name', f'alt{i}')}"))
    return out


def borrowed(kit, name):
    """Another kit file, for a role taken from it (`from:`). It is found
    unpacked under --roots by its title, else at its own content_root."""
    path = os.path.join(DRUMS, "kits", name + ".yaml")
    other = drumlayout.load_yaml(path)
    roots = kit.get("_roots")
    root = os.path.join(roots, other.get("title", name)) if roots \
        else other.get("content_root") or "."
    other["content_root"] = os.path.expanduser(root)
    return other


def open_program(kit):
    path = os.path.join(kit["content_root"], kit["program"])
    if not os.path.exists(path):
        die(f"{path}: no such program; is content_root right?")
    return Program(path)


def resolve(kit, layout):
    """role -> [(spec, voices, hits, evaluator)], one per articulation.

    A role with `from: <kit>` plays that kit's program at its key, with that
    kit's CC values: a kit without cymbals borrows another kit's.
    """
    program = open_program(kit)
    others = {}
    out = {}
    for role in drumlayout.used_roles(layout):
        out[role] = []
        for spec in variants(kit, role):
            source, prog = kit, program
            if spec.get("from"):
                if spec["from"] not in others:
                    other = borrowed(kit, spec["from"])
                    others[spec["from"]] = (other, open_program(other))
                source, prog = others[spec["from"]]
            overrides = dict(source.get("cc") or {})
            overrides.update(spec.get("cc") or {})
            evaluator = Evaluator(prog, overrides)
            note = note_number(spec["key"])
            voices = evaluator.voices(note)
            # `tune:` retunes a role in cents, for a kit with no tuning CC.
            for v in voices:
                v.cents += float(spec.get("tune", 0))
            if not voices:
                die(f"{spec['name']}: key {note} plays nothing at these "
                    f"CC values")
            out[role].append((spec, voices, hits(voices), evaluator))
    return program, out


# --------------------------------------------------------------------------
# targets

def short(path, kit):
    return os.path.relpath(path, kit["content_root"])


def target_inspect(args, layout):
    kit = load_kit(args.kit, layout, args.root)
    kit["_roots"] = getattr(args, "roots", None)
    program, resolved = resolve(kit, layout)
    print(f"{kit['title']}: {short(program.path, kit)}, "
          f"{len(program.regions)} regions")
    for spec, voices, bands, evaluator in (
            v for role in drumlayout.used_roles(layout) for v in resolved[role]):
        folders = sorted({os.path.dirname(short(v.sample, kit)) for v in voices})
        print(f"\n{spec['name']} (key {spec['key']}): {len(voices)} regions, "
              f"{len(bands)} velocity bands x "
              f"{max(len(b[2]) for b in bands)} RR")
        for folder in folders:
            layer = [v for v in voices if os.path.dirname(short(v.sample, kit))
                     == folder]
            gains = sorted({round(20 * math.log10(v.gain), 1) for v in layer})
            pans = sorted({round(v.pan) for v in layer})
            widths = sorted({round(v.width) for v in layer})
            cents = sorted({round(v.cents) for v in layer})
            print(f"  {folder}: {len(layer)} regions, gain {gains} dB, "
                  f"pan {pans}, width {widths}, tune {cents} c")
        print("  bands: " + ", ".join(f"{lo}-{hi}" for lo, hi, _ in bands))
        env = evaluator.envelope(voices[0].region)
        print("  envelope: " + ", ".join(f"{k} {v:g}" for k, v in env.items()))
        if evaluator.unsupported:
            print(f"  not reproduced: {', '.join(sorted(evaluator.unsupported))}")


def source_of(kit, spec):
    """What an articulation plays: its key and the CC values it sets.

    Two with the same source are the same sound, whatever they are called,
    so they share its recordings out rather than each taking a copy.
    """
    cc = dict(kit.get("cc") or {})
    cc.update(spec.get("cc") or {})
    return (spec.get("from"), note_number(spec["key"]),
            float(spec.get("tune", 0)),
            tuple(sorted((int(k), float(v)) for k, v in cc.items())))


def share(bands, count):
    """Deal one sound's recordings out to `count` pads, none to two of them.

    Each band is (lo, hi, takes): a velocity range and its round-robin takes.
    Returns per pad a list of (lo, hi, band index, [takes]), every pad still
    covering velocity 1-127, and whether any recording had to repeat. Takes
    are dealt first, like alternating hands: four takes over two pads gives one
    pad 1 and 3, the other 2 and 4. With fewer takes than pads the velocity
    bands are dealt instead, each pad's bands widened to close the gaps, so
    every pad keeps the whole dynamic range on recordings of its own. Only a
    sound with fewer bands than pads repeats one.
    """
    if count <= 1:
        return [[(lo, hi, b, list(range(n)))
                 for b, (lo, hi, n) in enumerate(bands)]], False
    if min(n for _, _, n in bands) >= count:
        return [[(lo, hi, b, [s for s in range(n) if s % count == k])
                 for b, (lo, hi, n) in enumerate(bands)]
                for k in range(count)], False
    pads = []
    for k in range(count):
        kept = [b for b in range(len(bands)) if b % count == k] \
            or [k % len(bands)]
        dealt = []
        for i, b in enumerate(kept):
            lo = 1 if i == 0 else dealt[-1][1] + 1
            hi = 127 if i == len(kept) - 1 else bands[kept[i + 1]][0] - 1
            dealt.append((lo, hi, b, list(range(bands[b][2]))))
        pads.append(dealt)
    return pads, len(bands) < count


def pad_labels(layout, articulation, by_source):
    """A name per pad, short enough for a Drum Machine pad: the role's
    `short:` (else its label), then the articulation's `short:` when it is not
    the main one ("Kickop"). Pads in both banks that play one drum share the
    name; the note in front of each file name keeps the files apart."""
    labels = {}
    for pads in by_source.values():
        for pad in pads:
            role = layout["roles"][pad["role"]]
            label = role.get("short") or drumlayout.label_of(layout, pad)
            labels[pad["note"]] = label + articulation[pad["note"]]
    # A layout can name a pad outright (`label: Kick2`), the same in every kit.
    for pad in layout["pads"]:
        if pad.get("label"):
            labels[pad["note"]] = pad["label"]
    return labels


def own_only(kit, layout):
    """The kit and layout with nothing borrowed, for a public release: a
    role taken from another kit (`from:`) plays the kit's own `own:` sound
    instead, `own: none` or no `own:` empties its pads, and alternatives
    taken from another kit are dropped."""
    roles = {}
    for role, spec in kit["roles"].items():
        spec = dict(spec) if isinstance(spec, dict) else {"key": spec}
        if spec.get("from"):
            own = spec.get("own")
            if own in (None, "none"):
                continue
            spec = dict(own) if isinstance(own, dict) else {"key": own}
        alts = spec.get("alt") or []
        alts = [alts] if isinstance(alts, dict) else alts
        spec["alt"] = [a for a in alts
                       if not (isinstance(a, dict) and a.get("from"))]
        spec.pop("own", None)
        roles[role] = spec
    kit = dict(kit, roles=roles)
    layout = dict(layout, pads=[p for p in layout["pads"] if p["role"] in roles])
    return kit, layout


def target_build(args, layout):
    np, sf = audio()
    own = getattr(args, "own", False)
    kit = load_kit(args.kit, layout if not own else dict(layout, pads=[]),
                   args.root)
    if own:
        kit, layout = own_only(kit, layout)
    kit["_roots"] = getattr(args, "roots", None)
    program, resolved = resolve(kit, layout)
    title = f"{kit['title']} {layout['title']}"
    out = args.output or os.path.join(DRUMS, "out")
    pads_dir = os.path.join(out, f"{title} pads")
    work = os.path.join(out, f".{kit['name']}-wav")
    # Fresh every time: a pad dropped from the layout must not linger.
    for folder in (pads_dir, work):
        if os.path.isdir(folder):
            shutil.rmtree(folder)
        os.makedirs(folder)
    mixer = Mixer()
    notes = [f"{title}, built by sfzkit.py from {kit['program']} at its "
             f"defaults.", "",
             "Per pad: velocity bands x round robins, the gain on its zones, "
             "and the amp envelope the SFZ program plays it with. An SFZ "
             "drum plays one-shot: lifting the pad does not stop it. A "
             "Sampler in Gated mode does, so a short pad press cuts a "
             "cymbal dead; take the pad's Sampler off Gated. Bitwig's "
             ".multisample cannot carry the envelope itself, so the values "
             "below are what to set if a pad rings on or stops short.", "",
             "Pads that play the same drum take its articulations in turn "
             "(the kit file's alt:), and pads on the same articulation never "
             "share a recording: its round-robin takes, or failing that its "
             "velocity layers, are dealt out between them.", ""]

    # Every recording of every sound, mixed once:
    # source -> (name, [(lo, hi, takes)], {(band, take): (name, path, frames)},
    # zone gain).
    sounds = {}
    for spec, voices, bands, evaluator in (
            v for role in drumlayout.used_roles(layout) for v in resolved[role]):
        source = source_of(kit, spec)
        if source in sounds:
            continue
        role = spec["name"]
        mixes = {}
        for b, (lo, hi, row) in enumerate(bands):
            for step, playing in enumerate(row):
                if playing:
                    mixes[(b, step)] = mixer.mix(playing)
        peak = max(float(np.abs(m).max()) for m in mixes.values()) or 1.0
        scale = PEAK / peak
        gain = -20 * math.log10(scale)
        files = {}
        for (b, step), data in mixes.items():
            lo, hi, _ = bands[b]
            name = f"{role} v{lo:03d}-{hi:03d} rr{step + 1}.wav"
            path = os.path.join(work, name)
            sf.write(path, data * scale, mixer.rate, subtype="PCM_24")
            files[(b, step)] = (name, path, len(data))
        shape = [(lo, hi, len(row)) for lo, hi, row in bands]
        sounds[source] = (role, shape, files, gain)
        amp = evaluator.envelope(voices[0].region)
        notes.append(f"{role}: {len(bands)} bands, "
                     f"{max(n for *_, n in shape)} RR, "
                     f"zone gain {gain:+.1f} dB; "
                     + ", ".join(f"{k} {v:g}" for k, v in amp.items()))
        print(f"{role}: {len(files)} samples, {gain:+.1f} dB")
        if evaluator.unsupported:
            warn(f"{role}: not reproduced: "
                 f"{', '.join(sorted(evaluator.unsupported))}")

    # A role's pads, in note order, take its articulations in turn, unless
    # the layout says `articulation: main` or `alt` for a pad (the same pads
    # in every kit), or the kit's `pad_articulation:` names one for a note
    # ("main" or an alt's name); then the pads each sound plays on share its
    # recordings out. A kit with no alt plays its main one there.
    by_source, turn, articulation = {}, {}, {}
    chosen = {int(k): v for k, v in (kit.get("pad_articulation") or {}).items()}
    for pad in layout["pads"]:
        options = resolved[pad["role"]]
        index = turn.get(pad["role"], 0)
        turn[pad["role"]] = index + 1
        spec = options[index % len(options)][0]
        if pad.get("articulation") == "main":
            spec = options[0][0]
        elif pad.get("articulation") == "alt":
            spec = options[1 % len(options)][0]
        if pad["note"] in chosen:
            want = chosen[pad["note"]]
            match = [o[0] for o in options
                     if (want == "main" and o[0]["name"] == pad["role"])
                     or o[0].get("name") == f"{pad['role']} {want}"]
            if not match:
                die(f"pad_articulation: note {pad['note']} ({pad['role']}) "
                    f"has no articulation {want!r}")
            spec = match[0]
        by_source.setdefault(source_of(kit, spec), []).append(pad)
        articulation[pad["note"]] = (
            "" if spec["name"] == pad["role"]
            else spec.get("short")
            or " " + spec["name"][len(pad["role"]):].strip().title())
    pad_zones = {}
    notes.append("")
    for source, pads in by_source.items():
        role, shape, files, gain = sounds[source]
        dealt, repeats = share(shape, len(pads))
        if repeats:
            warn(f"{role}: {len(pads)} pads and only {len(shape)} "
                 f"recordings; some pads repeat one")
        for pad, bands in zip(pads, dealt):
            zones = []
            for lo, hi, b, takes in bands:
                for take in takes:
                    if (b, take) not in files:
                        continue
                    name, path, frames = files[(b, take)]
                    zones.append((name, path, frames, lo, hi, gain,
                                  "round-robin" if len(takes) > 1
                                  else "always-play"))
            pad_zones[pad["note"]] = zones
        if len(pads) > 1:
            notes.append(
                f"{role}: notes {', '.join(str(p['note']) for p in pads)} "
                f"share its recordings out"
                + ("; some repeat, too few to go round" if repeats else ""))

    labels = pad_labels(layout, articulation, by_source)
    for pad in layout["pads"]:
        label = labels[pad["note"]]
        zones = pad_zones[pad["note"]]
        path = os.path.join(pads_dir, f"{pad['note']} {label}.multisample")
        write_multisample(
            path, f"{kit['title']} {label}", kit,
            f"{label} from {kit['title']} ({kit.get('licence', 'see kit')}), "
            f"mixed at the SFZ program's defaults. Note {pad['note']}.",
            [(name, frames, pad["note"], lo, hi, g, logic)
             for name, _, frames, lo, hi, g, logic in zones],
            sorted({(name, wav) for name, wav, *_ in zones}))
    zones, unique = [], {}
    for pad in layout["pads"]:
        for name, wav, frames, lo, hi, g, logic in pad_zones[pad["note"]]:
            zones.append((name, frames, pad["note"], lo, hi, g, logic))
            unique[name] = wav
    kit_file = os.path.join(out, f"{title}.multisample")
    write_multisample(
        kit_file, title, kit,
        f"{kit['title']} ({kit.get('licence', 'see kit')}), mixed at the SFZ "
        f"program's defaults. Notes "
        + ", ".join(f"{p['note']} {labels[p['note']]}"
                    for p in layout["pads"]) + ".",
        zones, sorted(unique.items()))
    print(f"wrote {kit_file}, "
          f"{os.path.getsize(kit_file) // (1 << 20)} MiB")
    with open(os.path.join(pads_dir, "README.txt"), "w") as fh:
        fh.write("\n".join(notes) + "\n")
    size = sum(os.path.getsize(os.path.join(pads_dir, f))
               for f in os.listdir(pads_dir))
    print(f"wrote {len(layout['pads'])} pads to {pads_dir}, "
          f"{size // (1 << 20)} MiB")


def note_names(kit, program):
    """A name per note: the kit file's role or articulation on that key,
    else the program's `label_key`, else the note number."""
    names = {n: v for n, v in
             ((note_number(k[9:]), v) for k, v in program_labels(program))}
    for role in kit["roles"]:
        for spec in variants(kit, role):
            if spec.get("from") or spec.get("cc"):
                continue
            names.setdefault(note_number(spec["key"]), spec["name"])
    return names


def program_labels(program):
    """`label_keyN=` from the program's <control> headers."""
    seen = {}
    for region in program.regions:
        for key, value in region.items():
            if re.fullmatch(r"label_key\d+", key):
                seen[key] = value
    return seen.items()


MIC_WORDS = {"top", "btm", "bottom", "oh", "cl", "close", "room", "amb",
             "ambience", "kick", "snare", "front", "back", "in", "out",
             "mono", "stereo", "left", "right", "ambl", "ambr", "ambm",
             # dynamics in a file name: Salamander's "Ride2 FF"
             "pp", "p", "mp", "mf", "f", "ff", "fff"}


def guessed_names(program, notes):
    """Names for notes the kit file and the program leave unnamed, from the
    recordings: the drum's folder (the first one under the samples' root)
    and what the file name adds to it, leaving out microphones, round robins
    and layers. "Samples/kick_24/phit/cl/k_phit_rr1.flac" is "kick 24 phit"."""
    evaluator = Evaluator(program)
    seen = {}
    for note in notes:
        paths = sorted({v.sample for v in evaluator.voices(note)})
        if paths:
            seen[note] = paths[0]
    if not seen:
        return {}
    root = os.path.commonpath([os.path.dirname(p) for p in seen.values()])
    noise = re.compile(r"(vl|rr|dl|v|l)?\d+|.*mic.*")
    out = {}
    for note, path in seen.items():
        parts = os.path.relpath(path, root).split(os.sep)
        folders = [f for f in parts[:-1]
                   if not noise.fullmatch(f.lower())
                   and f.lower() not in MIC_WORDS]
        group = folders[0].replace("_", " ") if folders else ""
        stem = [w for w in re.split(r"[_\s-]+",
                                    os.path.splitext(parts[-1])[0])
                if w and not noise.fullmatch(w.lower())
                and w.lower() not in MIC_WORDS]
        # "k_phit" in kick_24: the first word abbreviates the folder.
        if group and len(stem) > 1 and len(stem[0]) <= 3:
            stem = stem[1:]
        extra = [w for w in stem if w.lower() not in group.lower().split()]
        out[note] = " ".join([group] + extra).strip() or f"Note {note}"
    return out


# A CC that picks what is played, not how it is mixed: a hi-hat's openness,
# a stir's select, snares off. Mic, colour and level controls ("Snare top",
# "Kick punch") stay at the program's defaults, and so does sticking (which
# hand), whose takes are near-duplicates.
ARTICULATION = re.compile(
    r"open|position|select|snares? off|choke|brush|mallet|rods?\b", re.I)


def pretty(name):
    """"tom-floor-2" and "kick_24 phit" as "Tom Floor 2" and "Kick 24 Phit"."""
    words = re.split(r"[-_\s]+", name.strip())
    return " ".join(w[:1].upper() + w[1:] for w in words if w)


MIX_CONTROL = re.compile(
    r"ambien|mic|room|overhead|close|bleed|volume|level|pan|tun|curve|"
    r"release|decay|width", re.I)


def switching_ccs(program, note, base):
    """CC number -> one value inside each range that turns a set of the
    note's regions on, other than the range the defaults sit in."""
    edges = {}
    for region in program.regions:
        if "sample" not in region:
            continue
        try:
            lo, hi, _ = Evaluator(program).keys(region)
        except ValueError:
            continue
        if not lo <= note <= hi:
            continue
        for key, value in region.items():
            match = re.fullmatch(r"(lo|hi)cc(\d+)", key)
            if match:
                cut = float(value) + (1 if match.group(1) == "hi" else 0)
                edges.setdefault(int(match.group(2)), {0.0, 128.0}).add(cut)
    out = {}
    evaluator = Evaluator(program, base)
    for number, cuts in edges.items():
        # A mix control (Swirly's "Snare ambience" turns room mics on) is
        # the program's balance, not a sound: the defaults are the port's.
        label = program.labels.get(number, "")
        if MIX_CONTROL.search(label) or not ARTICULATION.search(label):
            continue
        cuts = sorted(c for c in cuts if 0 <= c <= 128)
        current = evaluator.ccvalue(number)
        states = [float(int((a + b - 1) / 2)) for a, b in zip(cuts, cuts[1:])
                  if not a <= current < b]
        if states:
            out[number] = states
    return out


def safe(name):
    return re.sub(r"[^\w .+-]", "", name).strip()


def target_full(args, layout):
    """Every sound the kit's own program plays, for a public release.

    Each note that sounds at the program's defaults is one .multisample at its
    original note, plus one for each of the kit file's CC-selected
    articulations (a half-open hat), and the whole kit on one Sampler on the
    original keymap. Nothing borrowed (`from:`), no `tune:`, no layout: those
    are the box's pads, not the library.
    """
    np, sf = audio()
    kit = load_kit(args.kit, {"roles": {}, "pads": []}, args.root)
    program = open_program(kit)
    out = os.path.join(args.output or os.path.join(DRUMS, "out"), kit["title"])
    work = os.path.join(out, "samples")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(work)
    names = note_names(kit, program)
    for note, name in guessed_names(
            program, [n for n in range(128) if n not in names]).items():
        names[note] = name
    names = {n: pretty(v) for n, v in names.items()}
    base = dict(kit.get("cc") or {})

    # (note, name, cc overrides) for every file: each note at the defaults,
    # then each other state of a CC that switches its regions on and off
    # (a hat's openness), one file per state.
    names.update({int(k): v for k, v in (kit.get("names") or {}).items()})
    wanted = [(n, None, base) for n in range(128)]
    for note in range(128):
        for number, states in switching_ccs(program, note, base).items():
            for value in states:
                cc = dict(base)
                cc[number] = value
                name = names.get(note, f"Note {note}")
                label = program.labels.get(number, f"CC{number}")
                # "Hi-hat" and "Hi-hat openness" make "Hi-hat Openness 64".
                what = " ".join(w.title() for w in label.split()
                                if w.lower() not in name.lower().split())
                if len(states) == 1:
                    # An on/off control: "Snare, Snares Off".
                    state = f"{name}, {label.title()}" if what else \
                        f"{name}, CC{number} {value:g}"
                else:
                    state = " ".join(x for x in (name, what, f"{value:g}") if x)
                wanted.append((note, state, cc))

    mixer, rows, kit_zones, wavs = Mixer(), [], [], {}
    mixed = {}  # what a hit plays -> its wav, so an alias note reuses it
    played = set()
    for note, name, cc in wanted:
        evaluator = Evaluator(program, cc)
        voices = evaluator.voices(note)
        if not voices:
            continue
        bands = hits(voices)
        if not bands:
            continue
        # A CC state that plays what another state of the note plays is not
        # a sound of its own.
        heard = (note, tuple((lo, hi, tuple(tuple(v.mix_key() for v in p)
                                            for p in row))
                             for lo, hi, row in bands))
        if heard in played:
            continue
        played.add(heard)
        name = name or names.get(note) or f"Note {note}"
        label = f"{note:03d} {safe(name)}"
        takes = {}
        for b, (lo, hi, row) in enumerate(bands):
            for step, playing in enumerate(row):
                if playing:
                    key = tuple(v.mix_key() for v in playing)
                    if key not in mixed:
                        mixed[key] = (f"{label} v{lo:03d}-{hi:03d} "
                                      f"rr{step + 1}.wav", mixer.mix(playing))
                    takes[(b, step)] = key
        peak = max(float(np.abs(mixed[k][1]).max()) for k in takes.values()) \
            or 1.0
        zones = []
        for (b, step), key in takes.items():
            wav, data = mixed[key]
            lo, hi, row = bands[b]
            if wav not in wavs:
                # Every take keeps its level against the others: one
                # gain per sound, as the program plays it.
                path = os.path.join(work, wav)
                sf.write(path, data * (PEAK / peak), mixer.rate,
                         subtype="PCM_24")
                wavs[wav] = (path, len(data), peak)
            path, frames, scaled = wavs[wav]
            gain = -20 * math.log10(PEAK / scaled)
            zones.append((wav, frames, note, lo, hi, gain,
                          "round-robin" if len(row) > 1 else "always-play"))
        write_multisample(
            os.path.join(out, f"{label}.multisample"),
            f"{kit['title']} {name}", kit,
            f"{name} from {kit['title']} ({kit.get('licence', 'see kit')}), "
            f"note {note}, mixed at the SFZ program's defaults"
            + (f" with CC {cc}" if cc != base else "") + ".",
            zones, sorted({(z[0], wavs[z[0]][0]) for z in zones}))
        if cc == base:
            kit_zones += zones
        amp = evaluator.envelope(voices[0].region)
        rows.append((note, name, len(bands), max(len(r) for *_, r in bands),
                     len(zones), sorted({v.lovel for v in voices}) and
                     f"{bands[0][0]}-{bands[-1][1]}",
                     cc if cc != base else {}, amp,
                     sorted(evaluator.unsupported)))
        print(f"{label}: {len(zones)} zones")
    write_multisample(
        os.path.join(out, f"{kit['title']}.multisample"), kit["title"], kit,
        f"{kit['title']} ({kit.get('licence', 'see kit')}) on its original "
        f"keymap, mixed at the SFZ program's defaults.",
        kit_zones, sorted({(z[0], wavs[z[0]][0]) for z in kit_zones}))

    with open(os.path.join(out, "CONTENT.md"), "w") as fh:
        fh.write(f"| Note | Sound | Velocity layers | Round robins | Samples "
                 f"| CC | Amp envelope (s, %) |\n|---|---|---|---|---|---|---|\n")
        for note, name, nb, rr, nz, vel, cc, amp, unsup in rows:
            env = ", ".join(f"{k[0]} {v:g}" for k, v in amp.items())
            ccs = ", ".join(f"CC{k}={v:g}" for k, v in cc.items()) or "default"
            fh.write(f"| {note} | {name} | {nb} | {rr} | {nz} | {ccs} "
                     f"| {env} |\n")
        missing = sorted({u for *_, unsup in rows for u in unsup})
        if missing:
            fh.write(f"\nNot reproduced: {', '.join(missing)}.\n")
    shutil.rmtree(work)
    size = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out))
    print(f"wrote {len(rows)} sounds and the whole kit to {out}, "
          f"{size // (1 << 20)} MiB, {mixer.rate} Hz")


TARGETS = {
    "inspect": target_inspect,
    "build": target_build,
    "full": target_full,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--layout", action="append",
                        help="layout name under drums/layouts, or a path; "
                             "repeat it to build several into one kit "
                             "(default core16-bank-a)")
    parser.add_argument("--kit", required=True,
                        help="kit name under drums/kits, or a path")
    parser.add_argument("--root",
                        help="where the kit is unpacked, over content_root")
    parser.add_argument("--roots",
                        help="folder holding the kits unpacked by title, "
                             "for roles borrowed from another kit (from:)")
    parser.add_argument("--own", action="store_true",
                        help="build: the kit's own sounds only, for a public "
                             "release (a role's `own:` stands in for `from:`)")
    parser.add_argument("-o", "--output", help="folder to write")
    args = parser.parse_args(argv)
    TARGETS[args.target](args, merge_layouts(args.layout or ["core16-bank-a"]))


if __name__ == "__main__":
    main()
