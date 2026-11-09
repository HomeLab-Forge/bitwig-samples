#!/usr/bin/env python3
"""Turn a melodic SFZ program into one Bitwig .multisample, sample for sample.

    ./sfzinst.py Presets/MNF-006-VerdantMeadows.sfz -o out/ [--title "Verdant Meadows"]

Unlike the drum front end, nothing is mixed or retuned: each region the
program plays at its defaults becomes one Sampler zone over the same keys,
with its root note, velocity range, gain, tune, start, end and sustain loop,
and Bitwig pitches it across the keys as the SFZ player would.

The program's gain is written into the samples rather than left to the zone,
because SFZ programs lean on positive volume (+8 dB is common) and a Sampler
zone plays them quiet. Each file is raised by the most any of its zones asks,
the whole set then by one amount so the loudest peaks at --peak dBFS, and a
zone keeps only what is left, never above 0 dB. Levels between zones stay as
the program has them. The files are written as 24-bit wav.

Round robins keep their first take only (a zone cannot choose among takes,
so they would all sound together). Not carried over, because a .multisample
has no field for them: the amp
envelope (set the Sampler's release to the program's `ampeg_release`, which
is printed), velocity tracking, filters, and a ping-pong loop, which plays as
a forward loop with the program's crossfade.
"""

import argparse
import io
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sfz import Evaluator, Program, audio, die, leading_number, note_number, warn  # noqa: E402


def zones(program):
    """(sample path, attributes) for every region that sounds at the defaults."""
    ev = Evaluator(program)
    out = []
    for region in program.regions:
        if "sample" not in region or region["sample"].startswith("*"):
            continue
        if not ev.gated(region):
            continue
        # One take per note: a round robin's takes would all play at once,
        # since a zone cannot pick among them. The first step of a
        # sequence, and the take a random pick of 0 lands on.
        if int(leading_number(region.get("seq_length", 1))) > 1 and \
                int(leading_number(region.get("seq_position", 1))) != 1:
            continue
        if not (leading_number(region.get("lorand", 0)) <= 0
                < leading_number(region.get("hirand", 1))):
            continue
        lo, hi, root = ev.keys(region)
        path = os.path.normpath(os.path.join(
            program.root, region["_default_path"],
            region["sample"].replace("\\", "/")))
        out.append((path, region, lo, hi, root))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("program")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--title")
    parser.add_argument("--creator", default="")
    parser.add_argument("--category", default="Synth")
    parser.add_argument("--peak", type=float, default=-1.0,
                        help="dBFS the loudest sample peaks at (default -1)")
    args = parser.parse_args(argv)
    np, sf = audio()
    program = Program(args.program)
    title = args.title or os.path.splitext(os.path.basename(args.program))[0]
    found = zones(program)
    if not found:
        die(f"{args.program}: no region sounds at the defaults")
    root_el = ET.Element("multisample", {"name": title})
    ET.SubElement(root_el, "generator").text = "sfzinst.py"
    ET.SubElement(root_el, "category").text = args.category
    ET.SubElement(root_el, "creator").text = args.creator
    ET.SubElement(root_el, "description").text = (
        f"{title}, from {os.path.basename(args.program)} at its defaults.")
    ET.SubElement(root_el, "keywords")
    layer = ET.SubElement(root_el, "layer", {"name": "Default"})
    files, releases, nodes = {}, set(), []
    for path, r, lo, hi, root in found:
        info = sf.info(path)
        name = os.path.splitext(os.path.basename(path))[0] + ".wav"
        if name in files and files[name][0] != path:
            # The same name in another folder: the folder goes in front.
            name = os.path.basename(os.path.dirname(path)) + " " + name
            if name in files and files[name][0] != path:
                die(f"two samples named {name}")
        start = float(r.get("offset", 0))
        # Some programs set `end` to the length, one past the last frame.
        stop = min(float(r.get("end", info.frames - 1)) + 1, info.frames)
        gain = float(r.get("volume", 0))
        amplitude = float(r.get("amplitude", 100)) / 100
        if amplitude <= 0:
            continue
        gain += 20 * np.log10(amplitude)
        files[name] = (path, max(gain, files.get(name, (path, gain))[1]))
        tune = float(r.get("tune", 0)) / 100 + float(r.get("transpose", 0))
        track = float(r.get("pitch_keytrack", 100)) / 100
        node = ET.SubElement(layer, "sample", {
            "file": name, "sample-start": f"{start:.3f}",
            "sample-stop": f"{stop:.3f}", "tune": f"{tune:.2f}"})
        nodes.append((node, name, gain))
        ET.SubElement(node, "key", {
            "low": str(lo), "high": str(hi), "root": str(root),
            "track": f"{track:.4f}"})
        # SFZ allows a velocity of 0 (VCSL starts its layers there); Bitwig
        # refuses to load a zone outside 1-127.
        ET.SubElement(node, "velocity", {
            "low": str(max(1, int(float(r.get("lovel", 1))))),
            "high": str(min(127, max(1, int(float(r.get("hivel", 127))))))})
        mode = r.get("loop_mode", "no_loop")
        if mode in ("loop_sustain", "loop_continuous") and "loop_start" in r:
            ls, le = float(r["loop_start"]), float(r["loop_end"]) + 1
            xfade = float(r.get("loop_crossfade", 0)) * info.samplerate
            fade = min(1.0, xfade / (le - ls)) if le > ls else 0.0
            if r.get("loop_type", r.get("looptype", "forward")) != "forward":
                warn(f"{name}: {r.get('loop_type')} loop played forward")
            ET.SubElement(node, "loop", {
                "mode": "sustain" if mode == "loop_sustain" else "loop",
                "start": f"{ls:.3f}", "stop": f"{le:.3f}",
                "fade": f"{fade:.4f}"})
        else:
            ET.SubElement(node, "loop", {"mode": "off"})
        releases.add(float(r.get("ampeg_release", 0)))
    audio_by_name, loudest = {}, 0.0
    for name, (path, gain) in files.items():
        data, rate = sf.read(path, dtype="float64", always_2d=True)
        data *= 10 ** (gain / 20)
        audio_by_name[name] = (data, rate)
        loudest = max(loudest, float(np.abs(data).max()))
    if loudest <= 0:
        die(f"{args.program}: every sample is silent")
    lift = 10 ** (args.peak / 20) / loudest
    for node, name, gain in nodes:
        node.set("gain", f"{gain - files[name][1]:.3f}")
    ET.indent(root_el, space="   ")
    os.makedirs(args.output, exist_ok=True)
    out = os.path.join(args.output, f"{title}.multisample")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for name in sorted(files):
            data, rate = audio_by_name[name]
            buf = io.BytesIO()
            sf.write(buf, data * lift, rate, subtype="PCM_24", format="WAV")
            zf.writestr(name, buf.getvalue())
        zf.writestr("multisample.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
                    + ET.tostring(root_el, encoding="unicode") + "\n")
    print(f"wrote {out}: {len(found)} zones, {len(files)} samples, "
          f"{os.path.getsize(out) // (1 << 20)} MiB, raised "
          f"{20 * np.log10(lift):+.1f} dB, release "
          f"{', '.join(f'{x:g}' for x in sorted(releases))} s")


if __name__ == "__main__":
    main()
