# Drum kit tools

How each drum release is built, from the original SFZ library. Each release is
built from the version here at its tag.

| Path | What it is |
|---|---|
| `tools/sfzkit.py` | The drum front end: plays a kit's program at its defaults and writes Bitwig `.multisample` files. |
| `tools/drumkit.py` | Builds a Drum Machine `.bwpreset` from them. |
| `tools/drumlayout.py` | Loads kit and layout files, and writes a layout's pad tables (`layouts/core16.md`). |
| `tools/selftest.py` | Checks the layouts, `sfzkit.py` and `bitwigdoc.py` against fake programs and samples. |
| [`../tools/sfz.py`](../tools/sfz.py) | The SFZ engine underneath, not drum-specific. |
| [`../tools/bitwigdoc.py`](../tools/bitwigdoc.py) | Reads and writes Bitwig's preset format, for any device. |
| `kits/*.yaml` | One per library: its program, names for its notes, its roles on the pads. |
| `layouts/core16-bank-*.yaml` | The two 16-pad banks the presets' first two pages play; `core16.md` shows them. |

Needs Python 3 with `pyyaml`, `numpy`, `scipy` and `soundfile`.

## SFZ kits

The SFZ kits, and what their licences ask:

| Kit | By | Licence |
|---|---|---|
| Big Rusty Drums, Virtuosity Drums, Swirly Drums, Gogodze Phu Vol II, Unruly Drums; Frankensnare, The Hat With The Phat | Karoryfer (Virtuosity with Versilian) | CC0 |
| Naked Drums | Wilkinson Audio | CC-BY-4.0: credit |
| DRS Kit, Muldjord Kit | DrumGizmo (Lars Muldjord, Bent Bisballe Nyeng; DRSDrums), SFZ ports by kinwie | CC-BY-4.0: credit |
| Salamander Drumkit | Alexander Holm | CC-BY-SA-3.0: credit; share-alike for sample libraries made from it |
| SM Drums | Scott McLean, Tod Stillwell, Suleiman Ali | public domain (sfzinstruments catalogue; no licence file in the repository) |

Each release page gives the credit line a kit's licence asks for.
An SFZ kit is a program, not a folder of samples: `#include`s, `#define`s, mic layers switched and levelled
by MIDI CC, round robins, tuning on CC. Bitwig imports a `.sfz` as an empty
Sampler, and sfizz has no Linux binary, so `sfzkit.py` does what an SFZ player
would. It reads the program, sets every CC to the value the program sets on
load (`set_ccN`), keeps the regions that would sound for each role's key, and
mixes the mic layers of each hit to one stereo sample. Mic levels, pans, stereo
width and tuning are the kit's defaults, baked into the audio, because Bitwig's
`.multisample` has no per-zone pan or mic balance; velocity bands and round
robins carry over as zones, the round robins as Bitwig's `round-robin` zone
logic.

What the programs use and how it is read:

| SFZ | Kit | Read as |
|---|---|---|
| `lorand`/`hirand` | Swirly, Salamander | Each slice of the random range is a round-robin step, cut per velocity band: Bitwig's zones have no random pick. 16 steps at most |
| `sw_last`, `sw_down`, `sw_previous` | Naked | Nothing played yet: the `sw_default` mode, no choke key held |
| `set_hdccN` | Naked | The 0-1 form of `set_ccN` |
| `#define`, `#include` | Naked, DRS | One token per value, several directives on a line, a name redefined stays that name |
| `seq_length=1seq_position=1` | DRS | An opcode name starts with a letter: one opcode, value 1, as players read it |
| `fil_type`, `cutoff` | Gogodze | Static 1- and 2-pole low- and high-pass filters, baked in (needs scipy) |
| `delay` | Gogodze | Silence before the mic's sample, which lines the mics up |

A role can also take a `tune:` in cents, for a kit with no tuning control
(Salamander's and DRS's missing toms), and `from: <kit>` to play another
kit's program at that kit's defaults: Gogodze has no cymbals and borrows Big
Rusty's, the kits with no rimshot take Frankensnare's 14x8" aluminium snare,
Swirly its 14x5" maple sidestick too, and Big Rusty Drums Phat Hat is Big
Rusty with The Hat With The Phat on every hi-hat pad. A source file such as
`frankensnare-14x8al-rims.yaml` names a program and has no roles of its own. `--roots` names the folder where the borrowed kit is unpacked, by
its title.

```bash
./tools/sfzkit.py inspect --kit big-rusty-drums   # what each pad resolves to
./tools/sfzkit.py build --kit big-rusty-drums \
    --layout core16-bank-a --layout core16-bank-b -o out/  # both MPD218 banks
```

`--layout` repeats: each MPD218 bank is a layout file, and a kit built from
both answers both, bank A on 36-51 and bank B on 52-67, laid out like a drum
kit.
`build` writes:

- `out/<Kit> Core 16.multisample`: every pad of both banks on one Sampler, each
  sample stored once. One drag, and one envelope switch (see below) for the
  whole kit; saved as a preset after that, it loads in one click.
- `out/<Kit> Core 16 pads/`: one `.multisample` per pad, named
  `36 Kick.multisample` upwards, for a Drum Machine with a chain per pad, plus a
  `README.txt` with each pad's bands, round robins and the amp envelope the
  program gives it.


**No two pads share a recording.** Both banks repeat drums on purpose (four
kicks, three snares, a tom or cymbal in each bank), and `same_as` in a layout
means the same drum, not the same file.

First, a role with an `alt:` in the kit file gives its pads, in note order, the
main articulation and the alternative in turn, so two kicks in one bank are the
kick and its alternative. Big Rusty: kick and undamped kick, snare centre and
edge, closed hat tip and shank. Virtuosity: kick with snares on and off, snare
centre and off-centre, closed hat and pedal (its only other closed hat). Two
takes of one articulation sound nearly alike (Big Rusty's kick takes differ by
about -20 dB); an alternative is plainly another sound.

Then a sound on several pads has its recordings dealt out between them.
Round-robin takes go first, like alternating hands: with four takes and two
pads, one pad gets takes 1 and 3, the other 2 and 4. With fewer takes than pads
the velocity bands are dealt instead, each pad's bands widened so it still
reaches from 1 to 127 on recordings of its own. The cost is fewer takes, or
fewer layers, per pad: each of Big Rusty's kick pads has two takes, and the
snare pads of Virtuosity, one take per layer, get half its 36 layers. A sound with
fewer bands than pads would have to repeat one, and `build` warns if it does;
neither kit does. Two roles with the same key and CC values count as one sound:
Virtuosity's flat ride is both crash 2 and the china stand-in, so its four
bands are dealt over three pads.

It needs numpy and soundfile (`python3-numpy`, `python3-soundfile`), because the
samples are FLAC; scipy too, for filters and resampling. `--root` points at
where the library is unpacked, over the kit file's `content_root`.

What does not survive:

- **The amp envelope.** An SFZ drum is one-shot, so lifting the pad does not stop
  it. A Sampler in Gated mode does, and a `.multisample` dropped on a track or
  a pad opens in Gated mode: the format has no field for it. Take the Sampler
  off Gated, or put a Note Length device in front of it. A saved preset keeps
  the setting.
- **Live controls.** Mic balance, sticking, hat pedal, tuning: each is fixed at
  the program's default. To change one, set it in the kit file, for the kit
  (`cc:`) or for one role (`roles.<role>.cc:`), and rebuild.
  `virtuosity-drums.yaml` does that for its second rack and floor tom: the kit
  has two toms, and the other two are the same drums tuned three semitones down
  with the kit's own tom tuning.
- **Choke groups, velocity curves, EQ and filters other than the four above.**
  `inspect` names any that would have changed the sound; at the defaults, no
  kit here uses one.

## Drum Machine presets

A `.multisample` cannot say One-shot, a voice limit or a choke, so each SFZ kit
also becomes a Drum Machine preset that loads in one click. It is set up
the way Bitwig's own acoustic kits are, read off Acoustic Drums Kit Yamaha:
every pad's Sampler is One-shot with 12 voices, so a re-hit rings over the last
one, and only the hi-hat chokes, one way. The kit file's `chokes:` says which
roles cut which (`{from: [hat-closed], to: [hat-open]}`), and `voices:` the
voice count.

```bash
./tools/sfzkit.py build --kit big-rusty-drums --layout core16-bank-a --layout core16-bank-b -o out/
./tools/drumkit.py --kit big-rusty-drums --layout core16-bank-a --layout core16-bank-b \
    --pads "out/Big Rusty Drums Core 16 pads" \
    --reference ~/"Bitwig Studio/Library/Presets/Drum Machine/dm_sample.bwpreset" \
    -o "out/Big Rusty Drums.bwpreset"
```

Bitwig's format is undocumented. [`../tools/bitwigdoc.py`](../tools/bitwigdoc.py) reads
and writes it, worked out from presets Bitwig 6.1 saved and checked by writing
them back byte for byte. [`tools/drumkit.py`](tools/drumkit.py) does not build a
preset from nothing: it clones the first pad of a **reference preset** saved in
Bitwig (a Drum Machine with a `.multisample` on one pad, that pad's Sampler set
to One-shot, and one choke) for every pad of the kit. A pad rebuilt that way from
the reference's own `.multisample` matches the saved one byte for byte apart
from the voice count. Keep your reference preset out of any repository:
Bitwig writes your account email into every sample reference.

Where a drum is on two pads of a bank, the layout says which takes the kit's
alternative sound (`articulation: alt`, as on Kick2, HHatCF and SStk2) and
which the main one, so the same pad means the same thing in every kit. A kit
with no alternative deals its recordings out between the two instead. A pad's
name is the role's `short:`, plus the alternative's `short:` where it plays
one, unless the layout gives it a `label:`; one drum has one name in both
banks, at most six characters. `pad_articulation:` in a kit file still picks
an articulation for one note, for swaps made by ear.

## Building a release

Each SFZ kit goes out as its own release of this repository, tagged
`bitwig-samples-<kit>-v<version>`, with two assets: a Drum Machine preset and
a tar of every sound.

```bash
# every sound the program plays, one .multisample each, plus the whole kit
./tools/sfzkit.py full --kit swirly-drums --root <unpacked kit> -o full/
# the two banks with the kit's own sounds only (a role's own: stands in for from:)
./tools/sfzkit.py build --own --kit swirly-drums --layout core16-bank-a \
    --layout core16-bank-b --root <unpacked kit> -o own/
# pages 1-2 are the banks, then every other sound from 68 up
./tools/drumkit.py --full --kit swirly-drums --layout core16-bank-a \
    --layout core16-bank-b --banks "own/Swirly Drums Core 16 pads" \
    --pads "full/Swirly Drums" --reference dm_sample.bwpreset \
    -o "Swirly Drums.bwpreset"
```

- **`full`** writes each note that sounds at the program's defaults at its
  original note, and each other state of a CC that switches the note's
  regions (Swirly's hi-hat openness) as a file of its own. Mix controls
  (ambience, mics, tuning) stay at the defaults. The kit file's `names:` name
  the notes; else the program's `label_key`, else the sample folder.
- **`--own`** leaves out every sound taken from another kit, so a release
  carries its own library only. A role with `from:` plays its `own:` key
  instead, or its pads stay empty.
- **`--full`** in `drumkit.py` packs the rest by group (kick, snare, hats,
  toms, cymbals, percussion, textures, noises) and **scrubs the owner's
  account email and home folder** from the preset, each replaced by a
  placeholder of the same length. Check a preset holds neither before
  publishing it.
