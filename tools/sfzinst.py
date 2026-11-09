#!/usr/bin/env python3
"""Turn a melodic SFZ program into one Bitwig .multisample, sample for sample.

    ./sfzinst.py Presets/MNF-006-VerdantMeadows.sfz -o out/ [--title "Verdant Meadows"]

Unlike the drum front end, nothing is mixed or retuned: each region the
program plays at its defaults becomes one Sampler zone over the same keys,
with its root note, velocity range, gain, tune, start, end and sustain loop,
and Bitwig pitches it across the keys as the SFZ player would. The sample
files are copied as they are.

Not carried over, because a .multisample has no field for them: the amp
envelope (set the Sampler's release to the program's `ampeg_release`, which
is printed), velocity tracking, filters, and a ping-pong loop, which plays as
a forward loop with the program's crossfade.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sfz import Evaluator, Program, audio, die, note_number, warn  # noqa: E402


def zones(program):
    """(sample path, attributes) for every region that sounds at the defaults."""
    ev = Evaluator(program)
    out = []
    for region in program.regions:
        if "sample" not in region or region["sample"].startswith("*"):
            continue
        if not ev.gated(region):
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
    files, releases = {}, set()
    for path, r, lo, hi, root in found:
        info = sf.info(path)
        name = os.path.basename(path)
        if name in files and files[name] != path:
            die(f"two samples named {name}")
        files[name] = path
        start = float(r.get("offset", 0))
        stop = float(r.get("end", info.frames - 1)) + 1
        gain = float(r.get("volume", 0))
        amplitude = float(r.get("amplitude", 100)) / 100
        if amplitude <= 0:
            continue
        gain += 20 * np.log10(amplitude)
        tune = float(r.get("tune", 0)) / 100 + float(r.get("transpose", 0))
        track = float(r.get("pitch_keytrack", 100)) / 100
        node = ET.SubElement(layer, "sample", {
            "file": name, "gain": f"{gain:.3f}",
            "sample-start": f"{start:.3f}", "sample-stop": f"{stop:.3f}",
            "tune": f"{tune:.2f}"})
        ET.SubElement(node, "key", {
            "low": str(lo), "high": str(hi), "root": str(root),
            "track": f"{track:.4f}"})
        ET.SubElement(node, "velocity", {
            "low": str(int(float(r.get("lovel", 1)))),
            "high": str(int(float(r.get("hivel", 127))))})
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
    ET.indent(root_el, space="   ")
    os.makedirs(args.output, exist_ok=True)
    out = os.path.join(args.output, f"{title}.multisample")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for name, path in sorted(files.items()):
            zf.write(path, name)
        zf.writestr("multisample.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
                    + ET.tostring(root_el, encoding="unicode") + "\n")
    print(f"wrote {out}: {len(found)} zones, {len(files)} samples, "
          f"{os.path.getsize(out) // (1 << 20)} MiB, release "
          f"{', '.join(f'{x:g}' for x in sorted(releases))} s")


if __name__ == "__main__":
    main()
