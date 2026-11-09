# bitwig-samples

Free sample libraries ported to Bitwig Studio's `.multisample` format, starting
with acoustic drum kits made from SFZ libraries. Each library is its own
release; this repository holds this guide and the tools. The drum ports are made by
the tools in [`drums/`](drums/) and [`tools/`](tools/) (MIT, see [LICENSE](LICENSE)) from
each library's own SFZ program at its default settings.

This is not an official Bitwig project and is not affiliated with Bitwig GmbH.

**Not using Bitwig?** Use the original library instead; each release page
links to it. Bitwig cannot load SFZ, which is the only reason these ports
exist: an SFZ player gets more from the original, with every microphone and
control.

## Libraries

Each library's newest release is linked from its name; *all versions* lists
every release of it, newest first. Drum kits so far; pianos and synths are next.

### Drums

| Kit | By | Licence | Sounds | Preset | Single sounds |
|---|---|---|---|---|---|
| [Big Rusty Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-big-rusty-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-big-rusty-drums&expanded=true)) | Karoryfer Samples | CC0 | 71 | 806 MB | 3.1 GB |
| [DRS Kit](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-drs-kit-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-drs-kit&expanded=true)) | DrumGizmo, DRSDrums; SFZ by kinwie | CC BY 4.0 | 35 | 244 MB | 557 MB |
| [Gogodze Phu Vol II](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-gogodze-phu-vol-ii-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-gogodze-phu-vol-ii&expanded=true)) | Karoryfer Samples | CC0 | 13 | 130 MB | 242 MB |
| [Muldjord Kit](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-muldjord-kit-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-muldjord-kit&expanded=true)) | Lars Muldjord / DrumGizmo; SFZ by kinwie | CC BY 4.0 | 17 | 121 MB | 289 MB |
| [Naked Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-naked-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-naked-drums&expanded=true)) | Wilkinson Audio | CC BY 4.0 | 24 | 477 MB | 1.2 GB |
| [Salamander Drumkit](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-salamander-drumkit-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-salamander-drumkit&expanded=true)) | Alexander Holm | CC BY-SA 3.0 | 30 | 374 MB | 871 MB |
| [SM Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-sm-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-sm-drums&expanded=true)) | Scott McLean, Tod Stillwell, Suleiman Ali | public domain | 29 | build it¹ | 3.6 GB |
| [Swirly Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-swirly-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-swirly-drums&expanded=true)) | Karoryfer Samples | CC0 | 36 | 718 MB | 1.8 GB |
| [Unruly Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-unruly-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-unruly-drums&expanded=true)) | Karoryfer Samples | CC0 | 70 | 718 MB | 2.9 GB |
| [Virtuosity Drums](https://github.com/HomeLab-Forge/bitwig-samples/releases/tag/bitwig-samples-virtuosity-drums-v1.0) ([all versions](https://github.com/HomeLab-Forge/bitwig-samples/releases?q=bitwig-samples-virtuosity-drums&expanded=true)) | Versilian Studios, Karoryfer | CC0 | 74 | 511 MB | 1.4 GB |

¹ Over GitHub's 2 GB limit per file; the release says how to build it.

## Releases

One library per release, tagged `bitwig-samples-<slug>-v<version>` and
titled `Bitwig <Library>`, for example `bitwig-samples-swirly-drums-v1.0`,
`Bitwig Swirly Drums`. Each release page lists the original kit, its
authors and licence, how the port differs from the original, and every file
with its notes, velocity layers and round robins. A new version of a library
moves its row in [Libraries](#libraries) to the new release, in the same change.

A port carries the licence of the kit it was made from. Most kits are CC0 or
public domain; some are CC BY 4.0 or CC BY-SA 3.0, which ask for credit (the
release page gives the line to use) and, for BY-SA, that sample libraries made
from them are shared under the same licence. Music you make with any of them
is yours.

## What a release holds

- **`<Kit>.multisample`**: the whole kit on one Sampler, on the original
  library's keymap.
- **One `.multisample` per articulation**: each drum sound on its own, at its
  original note, for a Drum Machine pad or a Sampler of its own.
- Large kits are split into several `.tar` assets, each under GitHub's 2 GiB
  limit.

A `.multisample` is Bitwig's own format: a zip of 24-bit WAV files plus a
`multisample.xml` keymap. Velocity layers and round robins are kept; the
original's microphones are mixed down to stereo.

## Install

1. **Download** the assets from the kit's release page.
2. **Unpack** any `.tar` asset:
   - Linux and macOS: `tar xf <file>.tar`
   - Windows: open it with 7-Zip, or run `tar xf <file>.tar` in a terminal.

   Do not unzip the `.multisample` files themselves; Bitwig reads them as they
   are.
3. **Move** the `.multisample` files into a folder Bitwig indexes. The simplest
   is your library's multisample folder, in a subfolder per kit:
   - Linux: `~/Bitwig Studio/Library/Multisamples/`
   - macOS and Windows: `Documents/Bitwig Studio/Library/Multisamples/`

   Any other folder works once you add it under **Settings → Locations → My
   Library**.
4. **Rescan** if the kit does not show up: in the Browser, right-click **My
   Library** and choose **Rescan**.
5. **Play** it:
   - The whole kit: drag `<Kit>.multisample` onto an instrument track. It opens
     in a Sampler on the original keymap, listed on the release page.
   - A Drum Machine of your own: add a Drum Machine and drag each
     articulation's `.multisample` onto the pad you want. Set each pad's
     Sampler to **One-shot** so hits ring out after the note ends, and make
     the closed hi-hat pads choke the open one.

## Credit

The original authors made the recordings and are credited on each release
page. They are not responsible for these conversions; report a problem with a
port here, not to them.
