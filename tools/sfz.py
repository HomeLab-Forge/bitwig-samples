"""The SFZ engine: read a program, evaluate it at fixed CC values the way an
SFZ player would, mix what one note plays to stereo, and write Bitwig
.multisample files. Instrument-neutral; drums/tools/sfzkit.py is the drum
front end built on it.

It needs numpy and soundfile, and scipy for filters and resampling.
"""

import math
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

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
