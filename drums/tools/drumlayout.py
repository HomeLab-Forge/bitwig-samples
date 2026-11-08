#!/usr/bin/env python3
"""Load drum layouts and kit files, and write a layout's pad tables.

    ./drumlayout.py doc --layout core16-bank-a -o ../layouts/core16.md

A layout names a role (kick, snare, hat-closed...) for each pad; a kit file
says what plays each role. sfzkit.py and drumkit.py build on these.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DRUMS = os.path.dirname(HERE)


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


TARGETS = {
    "doc": target_doc,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=sorted(TARGETS))
    parser.add_argument("--layout", default="core16-bank-a",
                        help="layout name under drums/layouts, or a path")
    parser.add_argument("-o", "--output", help="file to write")
    args = parser.parse_args(argv)
    TARGETS[args.target](args, load_layout(args.layout))


if __name__ == "__main__":
    main()
