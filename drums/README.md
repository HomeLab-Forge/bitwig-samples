# Drum kit tools

How each drum release is built, from the original SFZ library. The tools are
developed in a private repository and copied here; each release is built from
the version here at its tag.

| Path | What it is |
|---|---|
| `tools/sfzkit.py` | The drum front end: plays a kit's program at its defaults and writes Bitwig `.multisample` files. |
| `tools/drumkit.py` | Builds a Drum Machine `.bwpreset` from them. |
| `tools/drumlayout.py` | Loads kit and layout files. |
| [`../tools/sfz.py`](../tools/sfz.py) | The SFZ engine underneath, not drum-specific. |
| [`../tools/bitwigdoc.py`](../tools/bitwigdoc.py) | Reads and writes Bitwig's preset format, for any device. |
| `kits/*.yaml` | One per library: its program, names for its notes, its roles on the pads. |
| `layouts/core16-bank-*.yaml` | The two 16-pad banks the presets' first two pages play. |

Needs Python 3 with `pyyaml`, `numpy`, `scipy` and `soundfile`.

## Rebuilding a kit

Unpack the original library, then:

```bash
# every sound the program plays, one .multisample each, plus the whole kit
./tools/sfzkit.py full --kit swirly-drums --root "<unpacked kit>" -o full/

# the two banks, with the kit's own sounds only
./tools/sfzkit.py build --own --kit swirly-drums --layout core16-bank-a \
    --layout core16-bank-b --root "<unpacked kit>" -o own/

# the preset: the banks on 36-67, every other sound from 68 up
./tools/drumkit.py --full --kit swirly-drums --layout core16-bank-a \
    --layout core16-bank-b --banks "own/Swirly Drums Core 16 pads" \
    --pads "full/Swirly Drums" --reference <your preset> \
    -o "Swirly Drums.bwpreset"
```

Bitwig's preset format is not documented, so `drumkit.py` does not write a
preset from nothing: it clones a Drum Machine preset you save yourself in
Bitwig, whose first pad is a Sampler loaded with a `.multisample`, set to
One-shot, and choking one other pad. With `--full` it replaces the account
email and home folder Bitwig writes into such a preset. Check the result holds
neither before sharing it.

A kit file's `from:` borrows a sound from another library for the owner's own
setup; `--own` leaves those out and plays the role's `own:` key instead.
