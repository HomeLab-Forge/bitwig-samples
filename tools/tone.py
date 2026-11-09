#!/usr/bin/env python3
"""Brightness controls for a Sampler or Drum Machine preset.

    ./tone.py <in.bwpreset> -o <out.bwpreset>

Every Sampler gets a 4-pole low-pass filter at about 10 kHz with no
resonance, and an Expressions modulator so a hard hit opens it fully. The
preset gets a "Perform" remote page whose first control, "Mod Tone", is the
cutoff. On a Drum Machine the page is the Drum Machine's own, with Mod Tone
on the first pad's cutoff.

BW_AKAI (audio-rig) drives a control named "Mod Tone" from the mod wheel, in
its "Device mod macro" mode, reversed: the wheel at rest is 0.85 of the
cutoff's range, which is OPEN below, so the first touch does not jump. On a
Drum Machine the script also copies Mod Tone to every pad's cutoff, because a
Bitwig remote control reaches one parameter and "Copy Value to All Layers"
copies once rather than linking.

The page and the modulator come from templates/tone.bwpreset: the blank
Sampler template with a Perform page and an Expressions modulator saved in
Bitwig 6.1 added, then scrubbed. Velocity is Expressions' first output, and
its link to FILT_FREQ is the one whose amount VELOCITY sets.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bitwigdoc as bw  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "tone.bwpreset")

# Field ids, as read off presets Bitwig 6.1 wrote.
PARAM_ID, PARAM_VALUE, ENUM_VALUE = 0x02B9, 0x0136, 0x0273
PAGES, PAGE_LIST, PAGE, PAGE_NAME = 0x077D, 0x1A7E, 0x077B, 0x1B69
CONTROLS, C_LABEL, C_TARGET, C_SLOT = 0x1A7A, 0x1A7B, 0x1A7C, 0x1A88
MODULATORS, MOD_LIST, MODULATOR = 0x075F, 0x1A46, 0x06C9
LINK, LINK_TARGET, LINK_AMOUNT = 0x02FD, 0x0E3D, 0x0E32

LOW_PASS_4 = 8      # FILTER_TYPE: Low-pass (4-pole)
TOP = 142.1         # FILT_FREQ's top, 30 kHz; its range is 15.4868-142.1
OPEN = 123.1        # 0.85 of the range, about 10 kHz: the wheel's rest
VELOCITY = TOP - OPEN   # a hit at 127 opens the filter fully


def double(x):
    return struct.pack(">d", x)


def params(objs, name):
    return [o for o in objs if o.field(PARAM_ID) is not None and bw.text(o.get(PARAM_ID)) == name]


class Template:
    def __init__(self, path=TEMPLATE):
        doc = bw.Document(open(path, "rb").read())
        self.page = next(o for o in doc.objects if o.cls == PAGE)
        self.modulator = next(o for o in doc.objects if o.cls == MODULATOR)

    def perform_page(self, target=None):
        """The Perform page; with target, only Mod Tone, pointed there."""
        page = bw.clone(self.page)
        if target is not None:
            tone = next(c for c in page.get(CONTROLS) if bw.text(c.get(C_LABEL)) == "Mod Tone")
            tone.set(C_TARGET, bw.string(target))
            page.set(CONTROLS, [tone])
        return page

    def expressions(self):
        mod = bw.clone(self.modulator)
        for o in bw.walk(mod):
            if o.cls == LINK and bw.text(o.get(LINK_TARGET)) == "CONTENTS/FILT_FREQ":
                o.set(LINK_AMOUNT, double(VELOCITY))
        return mod


def append(obj, field, item):
    obj.set(field, list(obj.get(field) or []) + [item])


def sampler(tpl, objs, page=True):
    """One Sampler, given its objects: the filter, Expressions and (unless
    page is False) the Perform page. objs must hold exactly one Sampler."""
    for o in params(objs, "FILTER_TYPE"):
        o.set(ENUM_VALUE, LOW_PASS_4)
    for o in params(objs, "FILT_FREQ"):
        o.set(PARAM_VALUE, double(OPEN))
    for o in params(objs, "FILT_RESO"):
        o.set(PARAM_VALUE, double(0.0))
    append(next(o for o in objs if o.cls == MODULATORS), MOD_LIST, tpl.expressions())
    if page:
        append(next(o for o in objs if o.cls == PAGES), PAGE_LIST, tpl.perform_page())


def drum_machine(tpl, root, pads, first_note):
    """A Drum Machine: every pad's Sampler, and Mod Tone on the Drum Machine's
    own page, on pad first_note's cutoff. pads are the pad objects."""
    inside = set()
    for pad in pads:
        objs = list(bw.walk(pad))
        inside.update(id(o) for o in objs)
        sampler(tpl, objs, page=False)
    own = next(o for o in bw.walk(root) if o.cls == PAGES and id(o) not in inside)
    target = f"CONTENTS/DRUM_PADS/PAD{first_note}/DEVICE_CHAIN/0:CONTENTS/FILT_FREQ"
    append(own, PAGE_LIST, tpl.perform_page(target))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("preset", help="a Sampler preset")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    data = open(args.preset, "rb").read()
    doc = bw.Document(data)
    if params(doc.objects, "DRUM_PADS"):
        sys.exit("tone.py: a Drum Machine; drumkit.py adds tone as it builds the kit")
    sampler(Template(), doc.objects)
    out = doc.to_bytes()
    assert bw.Document(out).to_bytes() == out
    with open(args.output, "wb") as fh:
        fh.write(out)
    print(f"wrote {args.output}: {len(out) // (1 << 20)} MiB")


if __name__ == "__main__":
    main()
