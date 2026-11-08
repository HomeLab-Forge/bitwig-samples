#!/usr/bin/env python3
"""Check the layouts, sfzkit.py and bitwigdoc.py against fake programs and
samples built in a temporary folder.

    ./selftest.py
"""


import ast
import math
import os
import struct
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "tools"))
import drumlayout  # noqa: E402

DRUMS = drumlayout.DRUMS
FAILED = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append(name)
        print(f"FAIL  {name}\n        {exc}")
    except Exception as exc:  # noqa: BLE001 — a crash is a failure too
        FAILED.append(name)
        print(f"ERROR {name}\n        {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {name}")


class Args:
    def __init__(self, **kw):
        self.layout, self.kit = "core16-bank-a", "bitwig-acoustic"
        self.output = self.preset = self.input = None
        self.write = False
        self.__dict__.update(kw)


def tone(path, amplitude, seconds=0.2, rate=44100):
    """A decaying blip at a known amplitude, so loudness is predictable."""
    frames = int(seconds * rate)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            decay = math.exp(-4.0 * i / frames)
            value = int(amplitude * 32767 * decay * math.sin(i * 0.3))
            data += struct.pack("<h", value)
        w.writeframes(bytes(data))


# --------------------------------------------------------------------------

def test_layout_loads():
    layout = drumlayout.load_layout("core16-bank-a")
    assert len(layout["pads"]) == 16, f"{len(layout['pads'])} pads, not 16"
    notes = [p["note"] for p in layout["pads"]]
    assert notes == list(range(36, 52)), notes
    assert layout["channel"] == 10, layout["channel"]
    for pad in layout["pads"]:
        role = layout["roles"][pad["role"]]
        for field in ("label", "gm", "colour"):
            assert field in role, f"role {pad['role']} has no {field}"


def test_bad_layouts_are_refused():
    cases = {
        "a pad with no role": "pads:\n  - {pad: 1, note: 36}\nroles:\n  kick: {}\n",
        "an undeclared role": "pads:\n  - {pad: 1, note: 36, role: cowbell}\n"
                              "roles:\n  kick: {}\n",
        "same_as across roles": "pads:\n  - {pad: 1, note: 36, role: kick}\n"
                                "  - {pad: 2, note: 37, role: snare, same_as: 1}\n"
                                "roles:\n  kick: {}\n  snare: {}\n",
        "a note off the keyboard": "pads:\n  - {pad: 1, note: 200, role: kick}\n"
                                   "roles:\n  kick: {}\n",
    }
    for what, text in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.yaml")
            with open(path, "w") as fh:
                fh.write(text)
            try:
                drumlayout.load_layout(path)
            except SystemExit:
                continue
            raise AssertionError(f"{what} was accepted")


def test_doc_is_current():
    layout = drumlayout.load_layout("core16-bank-a")
    committed = os.path.join(DRUMS, "layouts", "core16.md")
    with tempfile.TemporaryDirectory() as tmp:
        fresh = os.path.join(tmp, "core16.md")
        drumlayout.target_doc(Args(output=fresh), layout)
        with open(fresh) as fh:
            want = fh.read()
    with open(committed) as fh:
        have = fh.read()
    assert have == want, ("layouts/core16.md is stale; regenerate it with "
                          "`drumlayout.py doc -o layouts/core16.md`")


# --------------------------------------------------------------------------
# sfzkit.py

SFZ_PROGRAM = """\
#define $KICK 36
<control>
default_path=../samples/
set_cc20=127
set_cc22=63.5
<global>
loop_mode=one_shot
// close mic: on at its set_cc, panned left by a bipolar curve at 0
<master>
amplitude_cc20=100
locc20=1
pan_cc21=100
pan_curvecc21=1
tune_cc22=1200
tune_curvecc22=1
<group>
key=$KICK
hivel=63
<region> sample=soft1.wav seq_length=2
<region> sample=soft2.wav seq_length=2 seq_position=2
<group>
key=$KICK
lovel=64
<region> sample=hard.wav
<region> sample=*silence
<region> sample=hard.wav trigger=release
// the same kick, another articulation
<group>
key=35
<region> sample=alt.wav
#include "mappings/room.sfz"
"""

SFZ_ROOM = """\
// room mic: its CC is never set, so it stays off
<master>
amplitude_cc30=100
locc30=1
<group>
key=$KICK
<region> sample=room.wav
"""

SFZ_LAYOUT = """\
name: two
title: Two
roles:
  kick: {label: Kick, gm: 36}
pads:
  - {pad: 1, note: 36, role: kick}
  - {pad: 2, note: 40, role: kick, same_as: 1}
"""


def fixture_sfz(tmp):
    import sfzkit
    root = os.path.join(tmp, "kit")
    for folder in ("Programs/mappings", "samples"):
        os.makedirs(os.path.join(root, folder))
    with open(os.path.join(root, "Programs", "kit.sfz"), "w") as fh:
        fh.write(SFZ_PROGRAM)
    with open(os.path.join(root, "Programs", "mappings", "room.sfz"), "w") as fh:
        fh.write(SFZ_ROOM)
    for name, amp in (("soft1", 0.2), ("soft2", 0.25), ("hard", 0.8),
                      ("room", 0.5), ("alt", 0.6)):
        tone(os.path.join(root, "samples", f"{name}.wav"), amp)
    layout_path = os.path.join(tmp, "two.yaml")
    with open(layout_path, "w") as fh:
        fh.write(SFZ_LAYOUT)
    kit_path = os.path.join(tmp, "sfzfix.yaml")
    with open(kit_path, "w") as fh:
        fh.write(f"name: sfzfix\ntitle: Fixture\nsource: sfz\n"
                 f"content_root: {root}\nprogram: Programs/kit.sfz\n"
                 f"roles:\n  kick: {{key: 36}}\n")
    layout = drumlayout.load_layout(layout_path)
    return sfzkit, layout, sfzkit.load_kit(kit_path, layout)


def test_sfz_program_plays_its_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        sfzkit, layout, kit = fixture_sfz(tmp)
        program, resolved = sfzkit.resolve(kit, layout)
        _, voices, bands, _ = resolved["kick"][0]
        names = sorted(os.path.basename(v.sample) for v in voices)
        # The room mic is gated off, the release and the generator never sound.
        assert names == ["hard.wav", "soft1.wav", "soft2.wav"], names
        for v in voices:
            assert abs(v.gain - 1.0) < 1e-6, v.gain
            assert v.pan == -100, v.pan
            assert abs(v.cents) < 1e-6, v.cents
        spans = [(lo, hi, len(row)) for lo, hi, row in bands]
        assert spans == [(1, 63, 2), (64, 127, 1)], spans


def test_sfz_cc_override_retunes():
    with tempfile.TemporaryDirectory() as tmp:
        sfzkit, layout, kit = fixture_sfz(tmp)
        kit["roles"]["kick"] = {"key": 36, "cc": {22: 47.625, 30: 127}}
        _, resolved = sfzkit.resolve(kit, layout)
        voices = resolved["kick"][0][1]
        assert any(v.sample.endswith("room.wav") for v in voices)
        for v in voices:
            if not v.sample.endswith("room.wav"):
                assert abs(v.cents + 300) < 1e-6, v.cents


def test_sfz_redefine_keeps_its_name():
    import sfzkit
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "redef.sfz"), "w") as fh:
            fh.write("#define $KEY 36\n#define $KEY 38   #define $VEL 90\n"
                     '#include "a.txt" #include "b.txt"\n')
        with open(os.path.join(tmp, "a.txt"), "w") as fh:
            fh.write("<region> sample=a.wav key=$KEY hivel=$VEL\n")
        with open(os.path.join(tmp, "b.txt"), "w") as fh:
            fh.write("<region> sample=b.wav key=$KEY hivel=$VEL\n")
        program = sfzkit.Program(os.path.join(tmp, "redef.sfz"))
        keys = [(r.get("key"), r.get("hivel")) for r in program.regions]
        assert keys == [("38", "90")] * 2, keys


def test_sfz_keyswitches_play_their_default():
    import sfzkit
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "sw.sfz"), "w") as fh:
            fh.write("<group> key=46 sw_lokey=12 sw_hikey=13 sw_default=12\n"
                     "<region> sample=half.wav sw_last=12\n"
                     "<region> sample=bow.wav sw_last=13\n"
                     "<group> key=46\n"
                     "<region> sample=choke.wav sw_down=60\n"
                     "<region> sample=ring.wav sw_up=60\n")
        evaluator = sfzkit.Evaluator(sfzkit.Program(os.path.join(tmp, "sw.sfz")))
        names = sorted(os.path.basename(v.sample) for v in evaluator.voices(46))
        assert names == ["half.wav", "ring.wav"], names
        assert not evaluator.unsupported, evaluator.unsupported


def test_sfz_filter_and_delay_are_reproduced():
    import sfzkit
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "f.sfz"), "w") as fh:
            fh.write("<control> set_cc110=127\n"
                     "<region> key=36 sample=a.wav fil_type=hpf_2p cutoff=150 "
                     "delay_oncc110=0.02\n")
        evaluator = sfzkit.Evaluator(sfzkit.Program(os.path.join(tmp, "f.sfz")))
        v, = evaluator.voices(36)
        assert v.filter == ("hpf_2p", 150.0, 0.0), v.filter
        assert abs(v.delay - 0.02) < 1e-9, v.delay
        assert not evaluator.unsupported, evaluator.unsupported
    # A high-pass at 150 Hz keeps 1 kHz and all but removes 30 Hz.
    rate = 48000
    b, a = sfzkit.filter_coefficients("hpf_2p", 150, 0, rate)
    def gain(hz):
        z = complex(math.cos(2 * math.pi * hz / rate),
                    -math.sin(2 * math.pi * hz / rate))
        return abs(sum(c * z ** i for i, c in enumerate(b))
                   / sum(c * z ** i for i, c in enumerate(a)))
    assert gain(1000) > 0.95 and gain(30) < 0.06, (gain(1000), gain(30))


def test_sfz_retune_is_clean():
    try:
        import numpy as np
        import scipy  # noqa: F401
    except ImportError:
        print("        (no numpy/scipy here; retune not exercised)")
        return
    import sfzkit
    rate = 48000
    t = np.arange(rate) / rate
    # A 10 kHz tone 3 semitones down lands on 10000 * 2**-0.25 Hz at full
    # level: a straight-line resample loses about 1.5 dB up there.
    tone = np.sin(2 * np.pi * 10000 * t).astype("float32")
    mixer = sfzkit.Mixer.__new__(sfzkit.Mixer)
    mixer.np, mixer.rate = np, rate
    mixer.read = lambda path: np.stack([tone, tone], axis=1)
    v = sfzkit.Voice("x", 1.0, 0, 100, -300, 0, None, 1, 127, 1, 1, {})
    out = mixer.voice(v)[2000:-2000, 0]
    spectrum = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peak = np.argmax(spectrum) * rate / (2 * (len(spectrum) - 1))
    want = 10000 * 2 ** -0.25
    assert abs(peak - want) < 5, (peak, want)
    level = np.sqrt(2) * np.sqrt(np.mean(out ** 2)) / (np.sqrt(2) * np.cos(np.pi / 4))
    assert level > 0.97, level


def test_sfz_random_round_robin_takes_turns():
    import sfzkit
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "rand.sfz"), "w") as fh:
            fh.write("<group> key=36\n"
                     "<region> sample=a.wav hirand=0.5\n"
                     "<region> sample=b.wav lorand=0.5\n"
                     "<region> sample=b-room.wav lorand=0.5\n")
        program = sfzkit.Program(os.path.join(tmp, "rand.sfz"))
        evaluator = sfzkit.Evaluator(program)
        bands = sfzkit.hits(evaluator.voices(36))
        assert not evaluator.unsupported, evaluator.unsupported
        # Two slices of the random range, two steps; a step plays one take
        # with its mics, never both takes at once.
        (lo, hi, row), = bands
        steps = [sorted(os.path.basename(v.sample) for v in p) for p in row]
        assert steps == [["a.wav"], ["b-room.wav", "b.wav"]], steps


def test_sfz_build_writes_round_robins():
    try:
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        print("        (no numpy/soundfile here; build not exercised)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        sfzkit, layout, kit = fixture_sfz(tmp)

        class Args:
            pass
        args = Args()
        args.kit, args.root, args.output = kit["_path"], None, tmp
        sfzkit.target_build(args, layout)
        pads = os.path.join(tmp, "Fixture Two pads")
        found = sorted(f for f in os.listdir(pads) if f.endswith(".multisample"))
        assert found == ["36 Kick.multisample", "40 Kick.multisample"], found
        # Both pads are the kick, and they deal its recordings out between
        # them: one each of its two velocity bands, widened to 1-127.
        used = {}
        for name, note in zip(found, ("36", "40")):
            with zipfile.ZipFile(os.path.join(pads, name)) as zf:
                root = ET.fromstring(zf.read("multisample.xml"))
                zones = root.findall("./layer/sample")
                for z in zones:
                    assert z.get("file") in zf.namelist(), z.get("file")
                    assert z.find("key").get("low") == note
                    vel = z.find("velocity")
                    assert (vel.get("low"), vel.get("high")) == ("1", "127"), \
                        (name, vel.attrib)
                used[note] = {z.get("file") for z in zones}
        assert not used["36"] & used["40"], used
        assert len(used["36"]) == 2 and len(used["40"]) == 1, used
        # The whole kit: both pads on one Sampler, each sample stored once.
        with zipfile.ZipFile(os.path.join(tmp, "Fixture Two.multisample")) as zf:
            root = ET.fromstring(zf.read("multisample.xml"))
            keys = sorted({z.find("key").get("low")
                           for z in root.findall("./layer/sample")})
            assert keys == ["36", "40"], keys
            wavs = [n for n in zf.namelist() if n.endswith(".wav")]
            assert len(wavs) == 3, wavs


def test_sfz_alt_articulation_takes_turns():
    try:
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        print("        (no numpy/soundfile here; build not exercised)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        sfzkit, layout, kit = fixture_sfz(tmp)
        # build reads the kit file itself, so the alt goes into the file.
        with open(kit["_path"]) as fh:
            text = fh.read()
        with open(kit["_path"], "w") as fh:
            fh.write(text.replace("kick: {key: 36}",
                                  "kick: {key: 36, alt: {key: 35, name: alt}}"))

        class Args:
            pass
        args = Args()
        args.kit, args.root, args.output = kit["_path"], None, tmp
        sfzkit.target_build(args, layout)
        pads = os.path.join(tmp, "Fixture Two pads")
        files = {}
        for name in ("36 Kick.multisample", "40 Kick Alt.multisample"):
            with zipfile.ZipFile(os.path.join(pads, name)) as zf:
                files[name[:2]] = {n for n in zf.namelist() if n.endswith(".wav")}
        # Pad 36 plays the kick's three recordings, pad 40 the alternative.
        assert len(files["36"]) == 3, files
        assert files["40"] == {"kick alt v001-127 rr1.wav"}, files


def test_sfz_share_never_repeats_a_recording():
    import sfzkit

    def recordings(dealt):
        return [{(b, t) for _, _, b, takes in pad for t in takes}
                for pad in dealt]

    def covers(pad):
        edges = [(lo, hi) for lo, hi, _, _ in pad]
        assert edges[0][0] == 1 and edges[-1][1] == 127, edges
        for (_, hi), (lo, _) in zip(edges, edges[1:]):
            assert lo == hi + 1, edges

    # Four takes over two pads: takes dealt like alternating hands.
    bands = [(1, 63, 4), (64, 127, 4)]
    dealt, repeats = sfzkit.share(bands, 2)
    sets = recordings(dealt)
    assert not repeats and not sets[0] & sets[1], sets
    assert sets[0] == {(0, 0), (0, 2), (1, 0), (1, 2)}, sets[0]
    # One take per band, thirty-six bands, three pads: bands dealt, each pad
    # still reaching from 1 to 127.
    bands = [(i * 3 + 1, i * 3 + 3, 1) for i in range(36)]
    bands[-1] = (106, 127, 1)
    dealt, repeats = sfzkit.share(bands, 3)
    sets = recordings(dealt)
    assert not repeats
    assert not (sets[0] & sets[1] or sets[1] & sets[2] or sets[0] & sets[2])
    for pad in dealt:
        covers(pad)
    # More pads than recordings is the one case that repeats, and says so.
    _, repeats = sfzkit.share([(1, 127, 1)], 2)
    assert repeats


def test_bitwigdoc_round_trips_and_clones():
    import bitwigdoc as bw
    child = bw.Obj(0x210, [[0x2B9, bw.STRING, bw.string("SAMPLE")],
                           [0x075F, bw.DOUBLE, struct.pack(">d", 1.5)]])
    pad = bw.Obj(0x252, [[0x2B9, bw.STRING, bw.string("PAD36")],
                         [0x2AB8, bw.UUID, bytes(16)],
                         [0x076E, bw.OBJECT, child],
                         [0x0748, bw.REF, bw.Ref(child)],
                         [0x129C, bw.LIST, [bw.Ref(child), bw.Obj(0x4A9, [])]],
                         [0x08E5, bw.INT8, 36]])
    shared = bw.Obj(0x2A, [[0x003B, bw.STRING, bw.string("/x")]])
    root = bw.Obj(0x561, [[0x1421, bw.OBJECT, shared],
                          [0x1420, bw.OBJECT, pad],
                          [0x1422, bw.LIST, [bw.Ref(shared)]]])
    w = bw.Writer()
    w.obj(root)
    header = b"BtWg0003000200c6" + b"%08x" % 50 + b"0" * 8 + b"0" * 8 + b"00"
    data = header + b"metadata" + bytes(w.out)
    doc = bw.Document(data)
    assert doc.to_bytes() == data, "round trip changed the bytes"
    copy = bw.clone(doc.root.get(0x1420))
    inner = copy.get(0x076E)
    # References inside the copy follow the copy, not the original.
    assert copy.get(0x0748).target is inner
    assert copy.get(0x129C)[0].target is inner
    assert inner is not doc.root.get(0x1420).get(0x076E)


def test_bank_b_merges_with_bank_a():
    import sfzkit
    merged = sfzkit.merge_layouts(["core16-bank-a", "core16-bank-b"])
    notes = [p["note"] for p in merged["pads"]]
    assert notes == list(range(36, 68)), notes
    core = drumlayout.load_layout("core16-bank-a")
    extra = set(merged["roles"]) - set(core["roles"])
    assert extra == {"china"}, extra
    for name in ("big-rusty-drums", "virtuosity-drums"):
        kit = drumlayout.load_yaml(os.path.join(DRUMS, "kits", name + ".yaml"))
        missing = set(drumlayout.used_roles(merged)) - set(kit["roles"])
        assert not missing, f"{name}: no key for {missing}"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name[5:].replace("_", " "), fn)
    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
