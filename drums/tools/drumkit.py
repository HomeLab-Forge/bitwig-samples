#!/usr/bin/env python3
"""Make a Bitwig Drum Machine preset from a folder of per-pad .multisamples.

    ./drumkit.py --kit big-rusty-drums --pads "out/Big Rusty Drums Core 16 pads" \\
        --reference ~/Bitwig\\ Studio/Library/Presets/Drum\\ Machine/dm_sample.bwpreset \\
        -o "out/Big Rusty Drums.bwpreset"

One click loads the whole kit: every pad's Sampler in One-shot, a voice limit
so a re-hit cuts the drum's last hit, and the kit's chokes (a closed hat cuts
the open one). A .multisample can carry none of that, which is why this exists.

Bitwig's preset format is not documented, so this does not build one from
nothing. It takes a reference preset saved in Bitwig, a Drum Machine whose
first pad is a Sampler loaded with a .multisample and set to One-shot, and
clones that pad for every pad of the kit, with the zones, notes, names and
voice count changed. The reference holds the owner's Bitwig account details
in its sample references, so it stays on the box and out of this repository.
See ../README.md, "Drum Machine presets".
"""

import argparse
import hashlib
import io
import os
import re
import sys
import uuid
import wave
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "tools"))
import bitwigdoc as bw  # noqa: E402
import drumlayout  # noqa: E402
import sfzkit  # noqa: E402

# Field ids, as read off presets Bitwig 6.1 wrote.
PAD_UUID, PAD_NOTE = 0x2AB8, 0x08E5
PADS, CHOKES = 0x08E0, 0x1812
CHOKE_FROM, CHOKE_TO = 0x1814, 0x1815
PLAY_MODE, ONE_SHOT = 0x42E0, 1
VOICES = 0x28FF
ZONES = 0x076D
Z_SAMPLE, Z_KEY_LO, Z_KEY_HI, Z_VEL_LO, Z_VEL_HI, Z_LOGIC = (
    0x076E, 0x076F, 0x0770, 0x0771, 0x0772, 0x269A)
ROUND_ROBIN, ALWAYS_PLAY = 1, 0
S_PLAYER, S_FILE = 0x4112, 0x0748
S_ROOT, S_GAIN, S_END, S_LOOP_END = 0x075C, 0x09E6, 0x075F, 0x0761
F_NAME, F_NAME2, F_RATE, F_SECONDS, F_CHANNELS, F_SOURCE = (
    0x129F, 0x0FB8, 0x0094, 0x0095, 0x0096, 0x129E)
SRC_NAME, SRC_PATH, SRC_ORIGIN, SRC_HASH, SRC_SIZE, SRC_FILES = (
    0x0512, 0x0D3A, 0x0D3B, 0x0513, 0x0017, 0x129C)
ORIGIN_PATH, HASH_MD5 = 0x003B, 0x0016
MS_CATEGORY, MS_CREATOR, MS_KEYWORDS, MS_DESCRIPTION = 0x0FB4, 0x0FB5, 0x0FB6, 0x0FB7
FILE_REFERENCES = 0x1422

PRESETS = os.path.expanduser("~/Bitwig Studio/Library/Presets/Drum Machine")


def die(msg):
    print(f"drumkit: {msg}", file=sys.stderr)
    sys.exit(1)


def double(x):
    return bw.struct.pack(">d", x)


def int32(x):
    return bw.struct.pack(">i", x)


# --------------------------------------------------------------------------
# the reference

class Reference:
    """The parts of the reference preset a kit is cloned from."""

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.doc = bw.Document(fh.read())
        objs = self.doc.objects
        drum_pads = [o for o in objs if o.name() == "DRUM_PADS"]
        if len(drum_pads) != 1:
            die(f"{path}: not a Drum Machine preset")
        self.drum_pads = drum_pads[0]
        pads = self.drum_pads.get(PADS)
        if not pads:
            die(f"{path}: the reference has no pads")
        self.pad = pads[0]
        players = [o for o in bw.walk(self.pad) if o.field(PLAY_MODE)]
        if not players or players[0].get(PLAY_MODE) != ONE_SHOT:
            die(f"{path}: set the first pad's Sampler to One-shot and save it "
                f"again")
        multis = [o for o in bw.walk(self.pad) if o.name() == "MUTLISAMPLE"]
        if not multis or len(multis[0].get(ZONES)) < 2:
            die(f"{path}: the first pad needs a .multisample of two zones or "
                f"more")
        zones = multis[0].get(ZONES)
        self.zone_first, self.zone_next = zones[0], zones[1]
        chokes = self.drum_pads.get(CHOKES) or []
        if not chokes:
            die(f"{path}: the reference needs one choke, to copy its shape")
        self.choke = chokes[0]


# --------------------------------------------------------------------------
# one pad

class Pad:
    """What a .multisample says one pad plays."""

    def __init__(self, path):
        self.path = path
        self.file = os.path.basename(path)
        with open(path, "rb") as fh:
            self.data = fh.read()
        with zipfile.ZipFile(io.BytesIO(self.data)) as zf:
            root = ET.fromstring(zf.read("multisample.xml"))
            self.meta = {k: (root.findtext(k) or "") for k in
                         ("category", "creator", "description")}
            self.keywords = " ".join(k.text or "" for k in
                                     root.iter("keyword"))
            self.zones = []
            for s in root.iter("sample"):
                with wave.open(io.BytesIO(zf.read(s.get("file")))) as w:
                    rate, channels = w.getframerate(), w.getnchannels()
                frames = float(s.get("sample-stop"))
                key, vel = s.find("key"), s.find("velocity")
                self.zones.append({
                    "file": s.get("file"), "rate": rate, "channels": channels,
                    "seconds": frames / rate, "gain": float(s.get("gain")),
                    "note": int(key.get("low")),
                    "vel": (int(vel.get("low")), int(vel.get("high"))),
                    "logic": s.get("zone-logic")})
        self.note = self.zones[0]["note"]


def build_pad(ref, pad, voices, installed):
    """The reference pad cloned and turned into `pad`."""
    new = bw.clone(ref.pad)
    new.set(0x2B9, bw.string(f"PAD{pad.note}"))
    new.set(PAD_UUID, uuid.uuid4().bytes)
    new.set(PAD_NOTE, pad.note)
    old_stem = os.path.splitext(bw.text(
        next(o for o in bw.walk(new) if o.field(SRC_NAME)).get(SRC_NAME)))[0]
    new_stem = os.path.splitext(pad.file)[0]
    # The pad's and the Sampler's display names are the file name.
    for o in bw.walk(new):
        for f in o.fields:
            if f[1] == bw.STRING and old_stem in bw.text(f[2]):
                f[2] = bw.string(bw.text(f[2]).replace(old_stem, new_stem))
    for o in bw.walk(new):
        if o.field(VOICES) is not None and o.name() == "POLY":
            o.set(VOICES, voices)
        if o.field(PLAY_MODE) is not None:
            o.set(PLAY_MODE, ONE_SHOT)
    multi = next(o for o in bw.walk(new) if o.name() == "MUTLISAMPLE")
    multi.set(MS_CATEGORY, bw.string(pad.meta["category"]))
    multi.set(MS_CREATOR, bw.string(pad.meta["creator"]))
    multi.set(MS_KEYWORDS, bw.string(pad.keywords))
    multi.set(MS_DESCRIPTION, bw.string(pad.meta["description"]))
    multi.set(ZONES, build_zones(ref, pad, installed))
    return new


def build_zones(ref, pad, installed):
    """Zones in the reference's shape: the first holds the sample file of
    every zone, inside the .multisample's own file object; the rest point
    back at theirs."""
    zones, files = [], []
    first = bw.clone(ref.zone_first)
    first_file = first.get(Z_SAMPLE).get(S_PLAYER).get(S_FILE)
    source = first_file.get(F_SOURCE)
    template_file = source.get(SRC_FILES)[1] if len(source.get(SRC_FILES)) > 1 \
        else first_file
    for i, z in enumerate(pad.zones):
        if i == 0:
            zone, sample_file = first, first_file
        else:
            zone = bw.clone(ref.zone_next)
            sample_file = bw.clone(template_file)
            zone.get(Z_SAMPLE).get(S_PLAYER).set(S_FILE, bw.Ref(sample_file))
        files.append(sample_file)
        sample_file.set(F_NAME, bw.string(z["file"]))
        sample_file.set(F_NAME2, bw.string(z["file"]))
        sample_file.set(F_RATE, int32(z["rate"]))
        sample_file.set(F_SECONDS, double(z["seconds"]))
        sample_file.set(F_CHANNELS, z["channels"])
        if i:
            sample_file.set(F_SOURCE, bw.Ref(source))
        zone.set(Z_KEY_LO, z["note"])
        zone.set(Z_KEY_HI, z["note"])
        zone.set(Z_VEL_LO, z["vel"][0])
        zone.set(Z_VEL_HI, z["vel"][1])
        zone.set(Z_LOGIC, ROUND_ROBIN if z["logic"] == "round-robin"
                 else ALWAYS_PLAY)
        sample = zone.get(Z_SAMPLE)
        sample.set(S_ROOT, z["note"])
        sample.set(S_GAIN, double(z["gain"]))
        sample.set(S_END, double(z["seconds"]))
        sample.set(S_LOOP_END, double(z["seconds"]))
        zones.append(zone)
    source.set(SRC_NAME, bw.string(pad.file))
    source.set(SRC_PATH, bw.string(f"multi-samples/{pad.file}"))
    source.set(SRC_SIZE, int32(len(pad.data)))
    source.get(SRC_HASH).set(HASH_MD5, hashlib.md5(pad.data).digest())
    source.get(SRC_ORIGIN).set(ORIGIN_PATH, bw.string(
        os.path.join(installed, pad.file)))
    # The file object lists every sample file of the .multisample; the first
    # zone's own is the one being written around it, so it is a reference.
    source.set(SRC_FILES, [bw.Ref(files[0])] + files[1:])
    return zones


# --------------------------------------------------------------------------
# the kit

# The order a full kit's pads are laid out in, by what the sound is called.
# Pedal and release noises go last, ahead of anything named for a drum.
GROUPS = [
    ("noise", r"pedal|release|return|noise|mech"),
    # Brush stirs and swells are played as textures, not hits.
    ("texture", r"stir|flutter|swirl|swell|roll"),
    ("kick", r"kick|bass drum"),
    ("snare", r"snare|stir|flutter|rim|stick"),
    ("hat", r"hi-?hat|\bhat\b|\bhh\b"),
    ("tom", r"tom"),
    ("cymbal", r"crash|ride|china|splash|cymbal|bell|stack"),
]
ORDER = ["kick", "snare", "hat", "tom", "cymbal", "perc", "texture", "noise"]


def group_of(name):
    for group, pattern in GROUPS:
        if re.search(pattern, name, re.I):
            return group
    return "perc"


def covered(pads, kit):
    """The full export's pads that a bank pad already plays: each role's
    key (an `own:` one included), and for a role on a CC (the open hat) the
    state of that key nearest its value."""
    kit, _ = sfzkit.own_only(kit, {"pads": []})
    # A note's plain sound has the shortest name; the rest are its CC states
    # ("046 Hi-hat Openness 64"). "Tom Floor 2" is a name, not a state.
    base = {}
    for p in pads:
        if p.note not in base or len(p.file) < len(base[p.note].file):
            base[p.note] = p
    out = set()
    for role in kit["roles"]:
        for one in sfzkit.variants(kit, role):
            key = sfzkit.note_number(one["key"])
            here = [p for p in pads if p.note == key]
            if not one.get("cc"):
                if key in base:
                    out.add(id(base[key]))
                continue
            want = list(one["cc"].values())[0]
            states = [(abs(float(STATE.search(p.file).group(1)) - float(want)),
                       p) for p in here
                      if p is not base[key] and STATE.search(p.file)]
            if states:
                out.add(id(min(states, key=lambda s: s[0])[1]))
    return out


STATE = re.compile(r" (\d+(?:\.\d+)?)\.multisample$")


def full_layout(pads, kit, start=36):
    """Pads and a layout for an `sfzkit.py full` export, packed for playing:
    every sound on its own pad from note 36 up, with no gaps, in the order
    kick, snare, hi-hat, toms, cymbals, percussion, textures, noises. Within a group
    the kit file's main sounds come first, then the rest by original note,
    and a note's CC states (a hat's openness) follow it. The original keymap
    stays in the whole-kit .multisample. Roles come from the kit file's
    keys, so its `chokes:` still apply: a CC state of the open hat's key
    counts as the open hat."""
    roles, mains = {}, set()
    for role in kit.get("roles") or {}:
        for i, one in enumerate(sfzkit.variants(kit, role)):
            if one.get("from"):
                continue
            key = sfzkit.note_number(one["key"])
            roles.setdefault((key, bool(one.get("cc"))), role)
            if i == 0 and not one.get("cc"):
                mains.add(key)
    seen, keyed = {}, []
    # The plain sound first ("046 Hi-hat"), then its states.
    for pad in sorted(pads, key=lambda p: (p.note, len(p.file), p.file)):
        state = seen.get(pad.note, 0)
        seen[pad.note] = state + 1
        role = roles.get((pad.note, bool(state))) or roles.get((pad.note, False))
        name = os.path.splitext(pad.file)[0].split(" ", 1)[-1]
        keyed.append(((ORDER.index(group_of(name)), pad.note not in mains,
                       pad.note, state), pad, role))
    if len(keyed) > 128 - start:
        die("more sounds than Drum Machine pads")
    layout = {"pads": []}
    for note, (_, pad, role) in enumerate(sorted(keyed, key=lambda k: k[0]),
                                          start=start):
        pad.note = note
        for z in pad.zones:
            z["note"] = note
        layout["pads"].append({"note": note, "role": role or pad.file})
    return layout


def scrub(data, zip_len):
    """The owner's account and home folder out of the document part, each
    replaced by a placeholder of the same length so no length field moves.
    The embedded samples (the last zip_len bytes) are left alone."""
    head, tail = data[:len(data) - zip_len], data[len(data) - zip_len:]
    head = re.sub(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+",
                  lambda m: b"x" * (len(m.group(0)) - 12) + b"@example.com"
                  if len(m.group(0)) > 12 else m.group(0), head)
    head = re.sub(rb"/home/[^/\x00-\x1f]+",
                  lambda m: b"/home/" + b"x" * (len(m.group(0)) - 6), head)
    return head + tail


def build(reference, pads_dir, kit, layout, installed, full=False,
          banks=None, bank_layout=None):
    ref = Reference(reference)
    paths = sorted(p for p in os.listdir(pads_dir) if p.endswith(".multisample"))
    if full:
        # The whole-kit file is not a pad.
        paths = [p for p in paths if p != f"{kit.get('title')}.multisample"]
    if not paths:
        die(f"{pads_dir}: no .multisample files; run sfzkit.py build first")
    pads = [Pad(os.path.join(pads_dir, p)) for p in paths]
    if full and banks:
        # The owner's two banks first (36-67), as built by `sfzkit.py build
        # --own`, then every sound they leave out, packed from 68.
        skip = covered(pads, kit)
        extra = [p for p in pads if id(p) not in skip]
        bank = [Pad(os.path.join(banks, p)) for p in sorted(os.listdir(banks))
                if p.endswith(".multisample")]
        layout = full_layout(extra, kit, start=max(p.note for p in bank) + 1)
        layout["pads"] = bank_layout["pads"] + layout["pads"]
        pads = bank + extra
    elif full:
        layout = full_layout(pads, kit)
    by_note = {p["note"]: p for p in layout["pads"]}
    voices = kit.get("voices", 12)
    built = []
    for pad in pads:
        role = by_note.get(pad.note, {}).get("role")
        n = (kit.get("roles", {}).get(role) or {}).get("voices", voices) \
            if isinstance(kit.get("roles", {}).get(role), dict) else voices
        built.append(build_pad(ref, pad, int(n), installed))
    ref.drum_pads.set(PADS, built)
    # Chokes as Bitwig's own acoustic kits have them (Acoustic Drums Kit
    # Yamaha): only the hi-hat, and one way. Each `chokes:` entry is
    # {from: [roles], to: [roles]}: a hit on any pad of a `from` role cuts
    # every pad of a `to` role. Everything else rings on under the next hit.
    chokes = []
    for rule in kit.get("chokes") or []:
        for src in layout["pads"]:
            if src["role"] not in rule["from"]:
                continue
            for dst in layout["pads"]:
                if dst["role"] in rule["to"] and dst["note"] != src["note"]:
                    c = bw.clone(ref.choke)
                    c.set(CHOKE_FROM, src["note"])
                    c.set(CHOKE_TO, dst["note"])
                    chokes.append(c)
    ref.drum_pads.set(CHOKES, chokes)
    # The document lists every embedded file's reference object (Bitwig
    # calls the list file_references, and refuses anything else in it).
    sources = [o for p in built for o in bw.walk(p) if o.field(SRC_NAME)]
    ref.doc.root.set(FILE_REFERENCES, [bw.Ref(o) for o in sources])
    # A fresh revision id, so Bitwig's library does not take it for the
    # reference.
    ref.doc.meta = bytearray(re.sub(
        rb"(\x0brevision_id\x08\x00\x00\x00\x28)[0-9a-f]{40}",
        lambda m: m.group(1) + uuid.uuid4().hex.encode() + uuid.uuid4().hex[:8].encode(),
        bytes(ref.doc.meta)))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for pad in pads:
            zf.writestr(f"multi-samples/{pad.file}", pad.data)
    data = ref.doc.to_bytes(archive.getvalue())
    if full:
        data = scrub(data, len(archive.getvalue()))
    return data, len(built), len(chokes)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kit", required=True,
                        help="kit name under drums/kits, or a path")
    parser.add_argument("--layout", action="append",
                        help="layout name(s), as for sfzkit.py (default core16-bank-a)")
    parser.add_argument("--pads", required=True,
                        help="the folder of per-pad .multisamples")
    parser.add_argument("--reference", required=True,
                        help="a Drum Machine preset saved in Bitwig, see above")
    parser.add_argument("--installed", default=os.path.expanduser(
        "~/Bitwig Studio/Library/Multisamples/audio-rig"),
        help="where the pads' .multisamples live on the box")
    parser.add_argument("--full", action="store_true",
                        help="--pads is an `sfzkit.py full` export: every "
                             "sound on its own note, no layout, and the "
                             "owner's account and paths scrubbed for a "
                             "public release")
    parser.add_argument("--banks",
                        help="with --full: a pads folder from `sfzkit.py "
                             "build --own` with --layout, played first")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    layout = None if args.full and not args.banks else \
        sfzkit.merge_layouts(args.layout or ["core16-bank-a"])
    kit = drumlayout.load_yaml(args.kit if os.sep in args.kit else os.path.join(
        drumlayout.DRUMS, "kits", args.kit + ".yaml"))
    installed = os.path.join(args.installed, os.path.basename(
        os.path.normpath(args.pads)))
    if args.full:
        kit.setdefault("title", os.path.basename(os.path.normpath(args.pads)))
        installed = "."
    data, pads, chokes = build(args.reference, args.pads, kit, layout,
                               installed, full=args.full, banks=args.banks,
                               bank_layout=layout)
    with open(args.output, "wb") as fh:
        fh.write(data)
    print(f"wrote {args.output}: {pads} pads, {chokes} chokes, "
          f"{len(data) // (1 << 20)} MiB")


if __name__ == "__main__":
    main()
