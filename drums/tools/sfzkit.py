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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drumlayout  # noqa: E402

DRUMS = drumlayout.DRUMS

# Headroom left on the loudest hit of a pad. What it takes off is put back as
# the zones' gain, so the level a pad plays at is the program's own.
PEAK = 10 ** (-1.0 / 20)

NOTE_NAMES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def die(msg):
    print(f"sfzkit: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"sfzkit: warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# reading a program

HEADER = re.compile(r"(<\w+>)")
# An opcode name starts with a letter, so DRSKit's `seq_length=1seq_position=1`
# is one opcode whose value starts with 1, as players read it.
OPCODE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=")


def substitute(text, defines):
    for name in sorted(defines, key=len, reverse=True):
        if name in text:
            text = text.replace(name, defines[name])
    return text


def expand(path, root, defines, depth=0):
    """The program's lines with #include followed and #define applied.

    Includes resolve against the folder of the file that was loaded, not the
    file doing the including, which is how sfizz and ARIA read them.
    """
    if depth > 32:
        die(f"{path}: includes nested 32 deep; a loop?")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        die(f"{exc}")
    for line in lines:
        line = line.split("//", 1)[0]
        # A line can hold several directives: DRSKit puts two #includes on
        # one, Naked Drums two #defines. A define's value is one token, and
        # only the value is expanded: a name defined again (Naked Drums
        # redefines the same names in every include) stays that name.
        for segment in re.split(r"(?=#(?:define|include)\b)", line):
            segment = segment.strip()
            rest = segment
            if segment.startswith("#define"):
                parts = segment.split(None, 3)
                if len(parts) >= 2:
                    defines[parts[1]] = substitute(
                        parts[2] if len(parts) > 2 else "", defines)
                rest = parts[3] if len(parts) > 3 else ""
            elif segment.startswith("#include"):
                segment = substitute(segment, defines)
                match = re.search(r'"([^"]+)"', segment)
                if not match:
                    warn(f"{path}: unreadable include: {segment}")
                    continue
                yield from expand(os.path.join(root, match.group(1)), root,
                                  defines, depth + 1)
                rest = segment[match.end():]
            rest = substitute(rest, defines).strip()
            if rest:
                yield rest


def note_number(value):
    value = str(value).strip().lower()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    match = re.fullmatch(r"([a-g])([#b]?)(-?\d+)", value)
    if not match:
        raise ValueError(f"not a note: {value!r}")
    letter, accidental, octave = match.groups()
    shift = {"#": 1, "b": -1}.get(accidental, 0)
    return NOTE_NAMES[letter] + shift + (int(octave) + 1) * 12


class Program:
    """Every region of an SFZ program with its inherited opcodes."""

    def __init__(self, path):
        self.path = path
        self.root = os.path.dirname(os.path.abspath(path))
        self.regions, self.curves, self.set_cc = [], {}, {}
        self.labels = {}
        control, scopes = {}, {"global": {}, "master": {}, "group": {}}
        kind, current = None, {}

        def close():
            if kind == "region":
                merged = {}
                for scope in ("global", "master", "group"):
                    merged.update(scopes[scope])
                merged.update(current)
                merged["_default_path"] = control.get("default_path", "")
                self.regions.append(merged)
            elif kind == "curve":
                index = int(current.get("curve_index", -1))
                points = {int(k[1:]): float(v) for k, v in current.items()
                          if re.fullmatch(r"v\d+", k)}
                self.curves[index] = points
            elif kind == "control":
                for key, value in current.items():
                    match = re.fullmatch(r"set_cc(\d+)", key)
                    if match:
                        self.set_cc[int(match.group(1))] = float(value)
                    # The high-definition form is 0-1 (Naked Drums).
                    match = re.fullmatch(r"set_hdcc(\d+)", key)
                    if match:
                        self.set_cc[int(match.group(1))] = 127 * float(value)
                    match = re.fullmatch(r"label_cc(\d+)", key)
                    if match:
                        self.labels[int(match.group(1))] = value
                control.update(current)
            elif kind in scopes:
                scopes[kind] = dict(current)

        for line in expand(path, self.root, {}):
            for part in HEADER.split(line):
                if not part.strip():
                    continue
                header = HEADER.fullmatch(part.strip())
                if header:
                    close()
                    kind, current = header.group(1)[1:-1], {}
                    # A header clears what sits beneath it.
                    if kind == "global":
                        scopes.update(global_={}, master={}, group={})
                        scopes["global"] = {}
                    elif kind == "master":
                        scopes.update(master={}, group={})
                    elif kind == "group":
                        scopes["group"] = {}
                    continue
                matches = list(OPCODE.finditer(part))
                for i, match in enumerate(matches):
                    end = matches[i + 1].start() if i + 1 < len(matches) \
                        else len(part)
                    current[match.group(1)] = part[match.end():end].strip()
                if kind in scopes:
                    scopes[kind] = dict(current)
        close()
        scopes.pop("global_", None)


# --------------------------------------------------------------------------
# evaluating it at fixed CC values

BUILTIN_CURVES = {
    0: lambda x: x,
    1: lambda x: 2 * x - 1,
    2: lambda x: 1 - x,
    3: lambda x: 1 - 2 * x,
    4: lambda x: x * x,
    5: lambda x: math.sqrt(x),
    6: lambda x: math.sqrt(1 - x),
}


def leading_number(value):
    """The number an opcode's value starts with. Salamander Drumkit has a
    `hirand=0.66s`, which an SFZ player reads as 0.66."""
    match = re.match(r"\s*[-+]?(\d+\.?\d*|\.\d+)", str(value))
    if not match:
        raise ValueError(f"not a number: {value!r}")
    return float(match.group(0))


class Voice:
    """One region as it would sound for one note: what plays and how."""

    def __init__(self, sample, gain, pan, width, cents, offset, end,
                 lovel, hivel, seq_length, seq_position, region,
                 lorand=0.0, hirand=1.0, filter=None, delay=0.0):
        self.sample, self.gain, self.pan, self.width = sample, gain, pan, width
        self.cents, self.offset, self.end = cents, offset, end
        self.lovel, self.hivel = lovel, hivel
        self.seq_length, self.seq_position = seq_length, seq_position
        self.lorand, self.hirand = lorand, hirand
        self.filter, self.delay = filter, delay
        self.region = region

    def mix_key(self):
        return (self.sample, round(self.gain, 6), round(self.pan, 3),
                round(self.width, 3), round(self.cents, 3), self.offset,
                self.end, self.filter, round(self.delay, 5))


class Evaluator:
    # Opcodes that change the sound and that this does not reproduce. Seeing
    # one active is worth a warning rather than silence.
    UNSUPPORTED = re.compile(
        r"(cutoff[2-9]|fil[2-9]_type|resonance[2-9]|eq\d+_gain|"
        r"sw_vel|lfo\d+_\w+|pitchlfo_\w+|amplfo_\w+|fillfo_\w+)")

    def __init__(self, program, overrides=None):
        self.program = program
        self.cc = dict(program.set_cc)
        self.cc.update({int(k): float(v) for k, v in (overrides or {}).items()})
        self.unsupported = set()
        self.switch = None

    def ccvalue(self, number):
        return self.cc.get(number, 0.0)

    def curve(self, index, x):
        x = min(1.0, max(0.0, x))
        points = self.program.curves.get(index)
        if points is None:
            fn = BUILTIN_CURVES.get(index)
            if fn is None:
                warn(f"curve {index} is not defined; read as linear")
                return x
            return fn(x)
        pts = dict(points)
        pts.setdefault(0, 0.0)
        pts.setdefault(127, 1.0)
        at = x * 127
        keys = sorted(pts)
        for lo, hi in zip(keys, keys[1:]):
            if lo <= at <= hi:
                t = (at - lo) / (hi - lo) if hi > lo else 0.0
                return pts[lo] + t * (pts[hi] - pts[lo])
        return pts[keys[-1]]

    def mods(self, region, target):
        """Sum of a target's CC modulations, each through its curve."""
        out = []
        pattern = re.compile(rf"{target}_?(?:on)?cc(\d+)")
        for key, value in region.items():
            match = pattern.fullmatch(key)
            if not match:
                continue
            number = int(match.group(1))
            curve = int(float(region.get(f"{target}_curvecc{number}", 0)))
            out.append(float(value)
                       * self.curve(curve, self.ccvalue(number) / 127))
        return out

    def keyswitch(self, region):
        """The last keyswitch pressed: none yet, so the program's default."""
        if self.switch is None:
            for r in self.program.regions:
                if "sw_default" in r:
                    self.switch = note_number(r["sw_default"])
                    break
            else:
                self.switch = -1
        return self.switch

    def gated(self, region):
        """False when a CC range, a trigger or a keyswitch keeps it silent.

        Keyswitches are read as nothing having been played yet: `sw_last` and
        its range match only the program's `sw_default`, no key is held down
        (`sw_down` never sounds, `sw_up` always does), and there is no
        previous note (`sw_previous` never sounds).
        """
        if region.get("trigger", "attack") not in ("attack", "first"):
            return False
        if "sw_down" in region or "sw_previous" in region:
            return False
        if "sw_last" in region \
                and note_number(region["sw_last"]) != self.keyswitch(region):
            return False
        if "sw_lolast" in region or "sw_hilast" in region:
            lo = note_number(region.get("sw_lolast", 0))
            hi = note_number(region.get("sw_hilast", 127))
            if not lo <= self.keyswitch(region) <= hi:
                return False
        for key in region:
            if key.startswith(("on_locc", "on_hicc")):
                return False
        for key, value in region.items():
            match = re.fullmatch(r"(lo|hi)cc(\d+)", key)
            if not match:
                continue
            current = self.ccvalue(int(match.group(2)))
            if match.group(1) == "lo" and current < float(value):
                return False
            if match.group(1) == "hi" and current > float(value):
                return False
        return True

    def crossfade(self, region):
        gain = 1.0
        for key in region:
            match = re.fullmatch(r"xf(in|out)_locc(\d+)", key)
            if not match:
                continue
            way, number = match.groups()
            lo = float(region[key])
            hi = float(region.get(f"xf{way}_hicc{number}", 127))
            value = self.ccvalue(int(number))
            t = 1.0 if hi <= lo else min(1.0, max(0.0, (value - lo) / (hi - lo)))
            if way == "out":
                t = 1.0 - t
            gain *= math.sqrt(t)
        return gain

    def keys(self, region):
        if "key" in region:
            note = note_number(region["key"])
            return note, note, note
        lo = note_number(region.get("lokey", 0))
        hi = note_number(region.get("hikey", 127))
        centre = note_number(region.get("pitch_keycenter", 60))
        return lo, hi, centre

    def voices(self, note):
        """What sounds when `note` is played, every velocity, every RR step."""
        out = []
        for region in self.program.regions:
            # *silence, *sine and the like are generators, not recordings.
            if "sample" not in region or region["sample"].startswith("*"):
                continue
            try:
                lo, hi, centre = self.keys(region)
            except ValueError as exc:
                warn(f"{exc}; region skipped")
                continue
            if not lo <= note <= hi or not self.gated(region):
                continue
            amplitude = float(region.get("amplitude", 100)) / 100
            for depth in self.mods(region, "amplitude"):
                amplitude *= depth / 100
            decibels = float(region.get("volume", 0))
            decibels += sum(self.mods(region, "volume"))
            decibels += sum(self.mods(region, "gain"))
            gain = amplitude * 10 ** (decibels / 20) * self.crossfade(region)
            if gain <= 1e-6:
                continue
            for key in region:
                base = re.sub(r"_?(on)?cc\d+$", "", key)
                if not self.UNSUPPORTED.fullmatch(base):
                    continue
                if base.startswith("eq") and not (
                        float(region.get(base, 0))
                        + sum(self.mods(region, base))):
                    continue
                self.unsupported.add(base)
            pan = float(region.get("pan", 0)) + sum(self.mods(region, "pan"))
            width = float(region.get("width", 100)) \
                + sum(self.mods(region, "width"))
            keytrack = float(region.get("pitch_keytrack", 100))
            cents = float(region.get("tune", 0)) \
                + 100 * float(region.get("transpose", 0)) \
                + sum(self.mods(region, "tune")) \
                + sum(self.mods(region, "pitch")) \
                + (note - centre) * keytrack
            sample = os.path.normpath(os.path.join(
                self.program.root, region["_default_path"],
                region["sample"].replace("\\", "/")))
            end = int(float(region["end"])) if "end" in region else None
            filt = None
            if "cutoff" in region:
                kind = region.get("fil_type", "lpf_2p")
                if kind in FILTERS:
                    hz = float(region["cutoff"]) \
                        * 2 ** (sum(self.mods(region, "cutoff")) / 1200)
                    res = float(region.get("resonance", 0)) \
                        + sum(self.mods(region, "resonance"))
                    filt = (kind, round(hz, 2), round(res, 2))
                else:
                    self.unsupported.add(f"fil_type={kind}")
            out.append(Voice(
                sample=sample, gain=gain,
                pan=max(-100.0, min(100.0, pan)),
                width=max(-100.0, min(100.0, width)),
                cents=cents, offset=int(float(region.get("offset", 0))),
                end=end,
                lovel=max(1, int(float(region.get("lovel", 1)))),
                hivel=min(127, int(float(region.get("hivel", 127)))),
                seq_length=max(1, int(leading_number(
                    region.get("seq_length", 1)))),
                seq_position=max(1, int(leading_number(
                    region.get("seq_position", 1)))),
                lorand=leading_number(region.get("lorand", 0)),
                hirand=leading_number(region.get("hirand", 1)),
                filter=filt, region=region,
                # A mic's distance, as Gogodze lines its mics up.
                delay=max(0.0, float(region.get("delay", 0))
                          + sum(self.mods(region, "delay")))))
        return out

    def envelope(self, region):
        """The amp envelope the region plays with, at these CC values."""
        out = {}
        defaults = {"attack": 0.0, "hold": 0.0, "decay": 0.0,
                    "sustain": 100.0, "release": 0.0}
        for stage, default in defaults.items():
            name = f"ampeg_{stage}"
            value = float(region.get(name, default)) + sum(self.mods(region, name))
            out[stage] = value
        return out


def hits(voices):
    """Group voices into what one note-on plays: velocity band x RR step.

    Mic layers do not have to agree on their velocity splits or their round
    robin count, so the bands are the intersections of every layer's ranges and
    the step count is the least common multiple of their lengths.

    A random round robin (`lorand`/`hirand`) becomes a sequential one: each
    slice of the 0-1 range is a step, so the takes alternate instead of all
    playing at once. Bitwig's zones have no random pick. The slices are cut
    per band, since a kit can split each band's range differently.
    """
    if not voices:
        return []
    edges = sorted({v.lovel for v in voices} | {v.hivel + 1 for v in voices})
    bands = []
    for lo, hi in zip(edges, edges[1:]):
        hi -= 1
        here = [v for v in voices if v.lovel <= lo and hi <= v.hivel]
        cuts = sorted({0.0, 1.0} | {min(1.0, max(0.0, x)) for v in here
                                    for x in (v.lorand, v.hirand)})
        slices = [(a + b) / 2 for a, b in zip(cuts, cuts[1:])]
        steps = len(slices)
        for v in here:
            steps = steps * v.seq_length // math.gcd(steps, v.seq_length)
        if steps > 16:
            warn(f"{steps} round-robin steps at velocity {lo}-{hi}; "
                 "capped at 16")
            steps = 16
        row = []
        for step in range(steps):
            playing = tuple(sorted(
                (v for v in here
                 if step % v.seq_length + 1 == v.seq_position
                 and v.lorand <= slices[step % len(slices)] < v.hirand),
                key=lambda v: v.mix_key()))
            row.append(playing)
        signature = [tuple(v.mix_key() for v in p) for p in row]
        if bands and bands[-1][2] == signature:
            bands[-1][1] = hi
        elif any(row):
            bands.append([lo, hi, signature, row])
    # A round robin whose steps all play the same thing is not one.
    out = []
    for lo, hi, signature, row in bands:
        if len(set(signature)) == 1:
            row = row[:1]
        out.append((lo, hi, row))
    return out


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


def audio():
    try:
        import numpy
        import soundfile
    except ImportError:
        die("build needs numpy and soundfile (python3-numpy, python3-soundfile)")
    return numpy, soundfile


# The SFZ filters reproduced, at a fixed cutoff: no envelope or LFO on them.
FILTERS = ("lpf_1p", "hpf_1p", "lpf_2p", "hpf_2p")


def filter_coefficients(kind, hz, resonance, rate):
    """RBJ biquads for the 2-pole filters, a one-pole pair for the rest."""
    w = 2 * math.pi * min(hz, rate * 0.49) / rate
    if kind.endswith("_1p"):
        k = math.exp(-w)
        if kind == "lpf_1p":
            return [1 - k], [1, -k]
        return [(1 + k) / 2, -(1 + k) / 2], [1, -k]
    q = math.sqrt(0.5) * 10 ** (resonance / 20)
    alpha = math.sin(w) / (2 * q)
    cos = math.cos(w)
    if kind == "lpf_2p":
        b = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
    else:
        b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
    a = [1 + alpha, -2 * cos, 1 - alpha]
    return [x / a[0] for x in b], [x / a[0] for x in a]


class Mixer:
    """Mixes the regions of one hit to a stereo buffer, as the player would."""

    def __init__(self):
        self.np, self.sf = audio()
        self.cache, self.rate = {}, None

    def read(self, path):
        if path not in self.cache:
            try:
                data, rate = self.sf.read(path, dtype="float32", always_2d=True)
            except (RuntimeError, OSError) as exc:
                die(f"{path}: {exc}")
            if self.rate is None:
                self.rate = rate
            elif rate != self.rate:
                # A pad borrowed from another kit (from:) can be at another
                # rate: Frankensnare at 48 kHz in Salamander at 44.1.
                from fractions import Fraction
                from scipy.signal import resample_poly
                ratio = Fraction(self.rate, rate).limit_denominator(1000)
                data = resample_poly(data, ratio.numerator, ratio.denominator,
                                     axis=0).astype("float32")
            self.cache[path] = data
        return self.cache[path]

    def voice(self, v):
        np = self.np
        data = self.read(v.sample)
        end = len(data) if v.end is None else min(len(data), v.end + 1)
        data = data[v.offset:end]
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > 2:
            data = data[:, :2]
        if abs(v.cents) > 0.5:
            # Tune by resampling with a polyphase filter, as the rate change
            # in read() does: no straight-line steps between samples, so no
            # dulled top or aliasing. The ratio is exact to well under a cent.
            from fractions import Fraction
            from scipy.signal import resample_poly
            ratio = Fraction(2 ** (-v.cents / 1200)).limit_denominator(4000)
            data = resample_poly(data, ratio.numerator, ratio.denominator,
                                 axis=0).astype("float32")
        if v.filter:
            from scipy.signal import lfilter  # only kits with filters need it
            b, a = filter_coefficients(*v.filter, self.rate)
            data = lfilter(b, a, data, axis=0).astype("float32")
        if v.delay:
            pad = np.zeros((int(round(v.delay * self.rate)), 2), "float32")
            data = np.concatenate([pad, data])
        mid = (data[:, 0] + data[:, 1]) / 2
        side = (data[:, 0] - data[:, 1]) / 2 * (v.width / 100)
        angle = (v.pan / 100 + 1) * math.pi / 4
        left = (mid + side) * math.sqrt(2) * math.cos(angle)
        right = (mid - side) * math.sqrt(2) * math.sin(angle)
        return np.stack([left, right], axis=1) * v.gain

    def mix(self, voices):
        parts = [self.voice(v) for v in voices]
        out = self.np.zeros((max(len(p) for p in parts), 2), dtype="float32")
        for part in parts:
            out[:len(part)] += part
        return out


def zone(layer, name, frames, note, low, high, gain, logic):
    """One Sampler zone, attribute for attribute as Bitwig writes its own."""
    node = ET.SubElement(layer, "sample", {
        "file": name, "gain": f"{gain:.2f}", "group": "0",
        "parameter-1": "0.0000", "parameter-2": "0.0000",
        "parameter-3": "0.0000", "reverse": "false",
        "sample-start": "0.000", "sample-stop": f"{frames}.000",
        "zone-logic": logic})
    # One key and no tracking: a drum is not transposed across its pad.
    ET.SubElement(node, "key", {"high": str(note), "low": str(note),
                                "root": str(note), "track": "0.0000",
                                "tune": "0.00"})
    ET.SubElement(node, "velocity", {"low": str(low), "high": str(high)})
    ET.SubElement(node, "select")
    ET.SubElement(node, "loop", {"fade": "0.0000", "mode": "off",
                                 "start": "0.000", "stop": f"{frames}.000"})


def write_multisample(path, title, kit, description, zones, files):
    root = ET.Element("multisample", {"name": title})
    ET.SubElement(root, "generator").text = "sfzkit.py"
    ET.SubElement(root, "category").text = "Drums"
    ET.SubElement(root, "creator").text = kit.get("creator", "audio-rig")
    ET.SubElement(root, "description").text = description
    keywords = ET.SubElement(root, "keywords")
    for word in ("drums", "kit", "acoustic"):
        ET.SubElement(keywords, "keyword").text = word
    layer = ET.SubElement(root, "layer", {"name": "Default"})
    for args in zones:
        zone(layer, *args)
    ET.indent(root, space="   ")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           + ET.tostring(root, encoding="unicode") + "\n")
    # Stored, not deflated, and the xml last: that is how Bitwig writes its own.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name, wav in files:
            zf.write(wav, name)
        zf.writestr("multisample.xml", xml)


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


def guessed_names(program, notes):
    """Names for notes the kit file and the program leave unnamed, from the
    recordings: their folder, or the file name when one folder holds several
    sounds (Swirly's mech_noises)."""
    evaluator = Evaluator(program)
    seen = {}
    for note in notes:
        paths = sorted({v.sample for v in evaluator.voices(note)})
        if paths:
            seen[note] = paths[0]
    folders = {}
    for path in seen.values():
        folders.setdefault(os.path.dirname(path), set()).add(path)
    out = {}
    for note, path in seen.items():
        if len({p for n, p in seen.items()
                if os.path.dirname(p) == os.path.dirname(path)}) == 1 \
                or len({os.path.basename(p)[:4] for n, p in seen.items()
                        if os.path.dirname(p) == os.path.dirname(path)}) == 1:
            out[note] = os.path.basename(os.path.dirname(path))
        else:
            words = [w for w in os.path.splitext(os.path.basename(path))[0]
                     .split("_") if not re.fullmatch(r"(vl|rr|dl|v)?\d+", w)
                     and w not in ("top", "btm", "oh", "room", "close")]
            out[note] = "_".join(words)
    return out


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
        if MIX_CONTROL.search(program.labels.get(number, "")):
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
        names[note] = name.replace("_", " ")
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
                # "Hi-hat" and "Hi-hat openness" make "Hi-hat Openness 64".
                what = " ".join(w.title() for w in program.labels.get(
                    number, f"CC{number}").split()
                    if w.lower() not in name.lower().split())
                wanted.append((note, f"{name} {what} {value:g}", cc))

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
