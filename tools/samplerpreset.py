#!/usr/bin/env python3
"""Turn .multisample files into Bitwig Sampler presets, one each, samples inside.

    ./samplerpreset.py -o out/ pads/*.multisample

Bitwig's preset format has no documentation, so nothing is written from
scratch: each preset is a template with its .multisample swapped in. Bitwig
keeps a copy of each zone in the document as well as in the embedded file,
so the zones are rebuilt from the new file's multisample.xml in the
template's shape.

The template, templates/sampler.bwpreset, is a Sampler preset Bitwig 6.1
saved, cut to two blank zones, with the account, machine and paths taken
out. --reference takes a Sampler preset of your own instead, to carry its
envelope and other settings; a preset Bitwig saved holds your Bitwig account
and home folder, and both are replaced in every preset written (see
bitwigdoc.scrub).
"""

import argparse
import hashlib
import io
import os
import re
import struct
import sys
import uuid
import wave
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bitwigdoc as bw  # noqa: E402

# Field ids, as read off a Sampler preset Bitwig 6.1 wrote. The drum ones
# are the same in drums/tools/drumkit.py.
PRESET_NAME = 0x12DE
ZONES = 0x076D
Z_SAMPLE, Z_KEY_LO, Z_KEY_HI, Z_VEL_LO, Z_VEL_HI = (
    0x076E, 0x076F, 0x0770, 0x0771, 0x0772)
S_PLAYER, S_FILE = 0x4112, 0x0748
S_TRACK, S_ROOT, S_TUNE, S_GAIN = 0x075B, 0x075C, 0x075D, 0x09E6
S_START, S_END, S_LOOP_START, S_LOOP_END, S_FADE, S_LOOP_MODE = (
    0x075E, 0x075F, 0x0760, 0x0761, 0x0762, 0x0763)
LOOP_MODES = {"off": 0, "sustain": 1}
F_NAME, F_NAME2, F_RATE, F_SECONDS, F_CHANNELS, F_ROOT, F_SOURCE = (
    0x129F, 0x0FB8, 0x0094, 0x0095, 0x0096, 0x08C0, 0x129E)
SRC_NAME, SRC_PATH, SRC_ORIGIN, SRC_HASH, SRC_SIZE, SRC_FILES = (
    0x0512, 0x0D3A, 0x0D3B, 0x0513, 0x0017, 0x129C)
ORIGIN_PATH, HASH_MD5 = 0x003B, 0x0016
MS_CATEGORY, MS_CREATOR, MS_DESCRIPTION = 0x0FB4, 0x0FB5, 0x0FB7
FILE_REFERENCES = 0x1422
# The same metadata again, on the document's preset object.
P_CATEGORY, P_CREATOR, P_DESCRIPTION, P_PRESET_CATEGORY = 0x009C, 0x009E, 0x009F, 0x00A1
# The preset's own metadata, in the block before the document.
META = {"comment": "description", "creator": "creator",
        "device_category": "category", "preset_category": "category"}


def die(msg):
    print(f"samplerpreset: {msg}", file=sys.stderr)
    sys.exit(1)


def double(x):
    return struct.pack(">d", x)


def int32(x):
    return struct.pack(">i", x)


def read_multisample(path):
    with open(path, "rb") as fh:
        data = fh.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ET.fromstring(zf.read("multisample.xml"))
        meta = {k: root.findtext(k) or "" for k in
                ("category", "creator", "description")}
        zones = []
        for s in root.iter("sample"):
            with wave.open(io.BytesIO(zf.read(s.get("file")))) as w:
                rate, channels, frames = (w.getframerate(), w.getnchannels(),
                                          w.getnframes())
            key, vel = s.find("key"), s.find("velocity")
            loop = s.find("loop")
            mode = loop.get("mode", "off") if loop is not None else "off"
            if mode not in LOOP_MODES:
                die(f"{path}: {s.get('file')}: loop mode {mode!r} is not mapped")
            track = key.get("track", "1")
            zones.append({
                "file": s.get("file"), "rate": rate, "channels": channels,
                "frames": frames, "gain": float(s.get("gain", 0)),
                "tune": float(s.get("tune", 0)),
                "start": float(s.get("sample-start", 0)),
                "stop": float(s.get("sample-stop", frames)),
                "key": (int(key.get("low")), int(key.get("high"))),
                "root": int(key.get("root", key.get("low"))),
                "track": 0.0 if track == "false" else
                         1.0 if track == "true" else float(track),
                # Bitwig refuses to load a zone outside 1-127.
                "vel": (max(1, int(vel.get("low", 1))),
                        min(127, max(1, int(vel.get("high", 127))))),
                "loop": (mode, float(loop.get("start", 0)) if mode != "off" else 0,
                         float(loop.get("stop", 0)) if mode != "off" else 0,
                         float(loop.get("fade", 0)) if mode != "off" else 0)})
    return data, root.get("name"), meta, zones


def set_meta(meta, values):
    """The meta block's string entries named in `values` rewritten: each is
    u32 1, u32 key length, key, type 0x08, u32 length, UTF-8 text."""
    out = bytearray(meta)
    for key, value in values.items():
        tag = struct.pack(">I", len(key)) + key.encode() + b"\x08"
        at = out.find(tag)
        if at < 0:
            continue
        at += len(tag)
        n = struct.unpack_from(">I", out, at)[0]
        raw = value.encode("utf-8")
        out[at:at + 4 + n] = struct.pack(">I", len(raw)) + raw
    return bytes(out)


def build(reference, path, installed):
    with open(reference, "rb") as fh:
        doc = bw.Document(fh.read())
    multis = [o for o in doc.objects if o.name() == "MUTLISAMPLE"]
    if len(multis) != 1 or len(multis[0].get(ZONES)) < 2:
        die(f"{reference}: save a Sampler preset with a .multisample of two "
            "zones or more loaded")
    multi = multis[0]
    ref_zones = multi.get(ZONES)
    data, title, meta, zones = read_multisample(path)
    name = os.path.basename(path)

    first = bw.clone(ref_zones[0])
    first_file = first.get(Z_SAMPLE).get(S_PLAYER).get(S_FILE)
    source = first_file.get(F_SOURCE)
    template_file = source.get(SRC_FILES)[1]
    out, files = [], []
    for i, z in enumerate(zones):
        if i == 0:
            zone, sample_file = first, first_file
        else:
            zone = bw.clone(ref_zones[min(i, len(ref_zones) - 1)])
            sample_file = bw.clone(template_file)
            sample_file.set(F_SOURCE, bw.Ref(source))
            zone.get(Z_SAMPLE).get(S_PLAYER).set(S_FILE, bw.Ref(sample_file))
        files.append(sample_file)
        rate = z["rate"]
        sample_file.set(F_NAME, bw.string(z["file"]))
        sample_file.set(F_NAME2, bw.string(z["file"]))
        sample_file.set(F_RATE, int32(rate))
        sample_file.set(F_SECONDS, double(z["frames"] / rate))
        sample_file.set(F_CHANNELS, z["channels"])
        sample_file.set(F_ROOT, z["root"])
        zone.set(Z_KEY_LO, z["key"][0])
        zone.set(Z_KEY_HI, z["key"][1])
        zone.set(Z_VEL_LO, z["vel"][0])
        zone.set(Z_VEL_HI, z["vel"][1])
        s = zone.get(Z_SAMPLE)
        s.set(S_TRACK, double(z["track"]))
        s.set(S_ROOT, z["root"])
        s.set(S_TUNE, double(z["tune"]))
        s.set(S_GAIN, double(z["gain"]))
        s.set(S_START, double(z["start"] / rate))
        s.set(S_END, double(z["stop"] / rate))
        mode, ls, le, fade = z["loop"]
        s.set(S_LOOP_MODE, LOOP_MODES[mode])
        if mode != "off":
            s.set(S_LOOP_START, double(ls / rate))
            s.set(S_LOOP_END, double(le / rate))
            s.set(S_FADE, double(fade))
        out.append(zone)
    source.set(SRC_NAME, bw.string(name))
    source.set(SRC_PATH, bw.string(f"multi-samples/{name}"))
    source.set(SRC_SIZE, int32(len(data)))
    source.get(SRC_HASH).set(HASH_MD5, hashlib.md5(data).digest())
    source.get(SRC_ORIGIN).set(ORIGIN_PATH, bw.string(
        os.path.join(installed, name)))
    source.set(SRC_FILES, [bw.Ref(files[0])] + files[1:])
    multi.set(ZONES, out)
    multi.set(MS_CATEGORY, bw.string(meta["category"]))
    multi.set(MS_CREATOR, bw.string(meta["creator"]))
    multi.set(MS_DESCRIPTION, bw.string(meta["description"]))
    for o in doc.objects:
        if o.field(PRESET_NAME) is not None:
            o.set(PRESET_NAME, bw.string(title))
            for fid, key in ((P_CATEGORY, "category"), (P_CREATOR, "creator"),
                             (P_DESCRIPTION, "description"),
                             (P_PRESET_CATEGORY, "category")):
                if o.field(fid) is not None:
                    o.set(fid, bw.string(meta[key]))
    # The document lists every embedded file's reference object, and refuses
    # anything else in it.
    doc.root.set(FILE_REFERENCES, [bw.Ref(source)])
    doc.meta = bytearray(set_meta(doc.meta, {k: meta[v] for k, v in META.items()}))
    # A fresh revision id, so Bitwig's library does not take it for the
    # reference.
    doc.meta = bytearray(re.sub(
        rb"(\x0brevision_id\x08\x00\x00\x00\x28)[0-9a-f]{40}",
        lambda m: m.group(1) + uuid.uuid4().hex.encode() + uuid.uuid4().hex[:8].encode(),
        bytes(doc.meta)))

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"multi-samples/{name}", data)
    return bw.scrub(doc.to_bytes(archive.getvalue()), len(archive.getvalue())), title


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("multisample", nargs="+")
    parser.add_argument("--reference", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates", "sampler.bwpreset"),
        help="a Sampler preset saved in Bitwig, see above (default: the template)")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--installed", default=os.path.expanduser(
        "~/Bitwig Studio/Library/Multisamples/audio-rig"),
        help="where the .multisample files are installed, for Bitwig's record "
             "of the file's origin")
    args = parser.parse_args(argv)
    os.makedirs(args.output, exist_ok=True)
    for path in args.multisample:
        data, title = build(args.reference, path, args.installed)
        out = os.path.join(args.output, f"{title}.bwpreset")
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"wrote {out}: {len(data) // (1 << 20)} MiB")


if __name__ == "__main__":
    main()
