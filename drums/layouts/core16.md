# Core 16 on the pads

Generated from [`layouts/core16-bank-a.yaml`](core16-bank-a.yaml) by `drums/tools/drumlayout.py doc`. Edit the layout, not this.

## The grid

Pad 1 is bottom-left. All sixteen send on MIDI channel 10.

| | | | |
|---|---|---|---|
| **13** · 48<br>Crash 1 | **14** · 49<br>Crash 2 | **15** · 50<br>Ride Bow | **16** · 51<br>Ride Bell |
| **9** · 44<br>Rack Tom 1 | **10** · 45<br>Rack Tom 2 | **11** · 46<br>Floor Tom 1 | **12** · 47<br>Floor Tom 2 |
| **5** · 40<br>Kick · | **6** · 41<br>Snare Rimshot | **7** · 42<br>Snare Sidestick | **8** · 43<br>Hat Open |
| **1** · 36<br>Kick | **2** · 37<br>Snare | **3** · 38<br>Hat Closed | **4** · 39<br>Hat Closed · |

A pad marked · plays the same drum as another: pad 4 doubles pad 3, pad 5 doubles pad 1. That is SSD5's own layout kept intact, so the pads feel the same whichever plugin is loaded.

## Every pad

| Pad | Note | Role | Plays | SSD5 articulation | GM note | Colour |
|---|---|---|---|---|---|---|
| 1 | 36 | `kick` | Kick | Kick Center | 36 | red |
| 2 | 37 | `snare` | Snare | Snare Side | 38 | amber |
| 3 | 38 | `hat-closed` | Hat Closed | Hi-Hat Tip Closed Tight | 42 | teal |
| 4 | 39 | `hat-closed` | Hat Closed | Hi-Hat Tip Closed Tight | 42 | teal |
| 5 | 40 | `kick` | Kick | Kick Center | 36 | red |
| 6 | 41 | `snare-rimshot` | Snare Rimshot | Snare Rimshot Edge | 40 | orange |
| 7 | 42 | `snare-sidestick` | Snare Sidestick | Snare Sidestick | 37 | yellow |
| 8 | 43 | `hat-open` | Hat Open | Hi-Hat Shank Open 1 | 46 | cyan |
| 9 | 44 | `tom-rack-1` | Rack Tom 1 | Rack Tom 1 Center | 48 | green |
| 10 | 45 | `tom-rack-2` | Rack Tom 2 | Rack Tom 2 Center | 47 | lime |
| 11 | 46 | `tom-floor-1` | Floor Tom 1 | Floor Tom 1 Center | 45 | sea-green |
| 12 | 47 | `tom-floor-2` | Floor Tom 2 | Floor Tom 2 Center | 43 | light-green |
| 13 | 48 | `crash-1` | Crash 1 | Crash Left Edge | 49 | dodger-blue |
| 14 | 49 | `crash-2` | Crash 2 | Crash Right Edge | 57 | blue |
| 15 | 50 | `ride-bow` | Ride Bow | Ride Bow Shank | 51 | violet |
| 16 | 51 | `ride-bell` | Ride Bell | Ride Bell | 53 | magenta |

## Reaching a General MIDI plugin

The pads send the left column. A plugin mapped to General MIDI — MT Power, Sitala, EZdrummer and most of what is still to be tried — hears the right one, so it needs the notes moved on the way in. 15 of the 16 land somewhere else.

| Pad sends | Drum | GM wants |
|---|---|---|
| 36 | Kick | 36 |
| 37 **→** | Snare | 38 |
| 38 **→** | Hat Closed | 42 |
| 39 **→** | Hat Closed | 42 |
| 40 **→** | Kick | 36 |
| 41 **→** | Snare Rimshot | 40 |
| 42 **→** | Snare Sidestick | 37 |
| 43 **→** | Hat Open | 46 |
| 44 **→** | Rack Tom 1 | 48 |
| 45 **→** | Rack Tom 2 | 47 |
| 46 **→** | Floor Tom 1 | 45 |
| 47 **→** | Floor Tom 2 | 43 |
| 48 **→** | Crash 1 | 49 |
| 49 **→** | Crash 2 | 57 |
| 50 **→** | Ride Bow | 51 |
| 51 **→** | Ride Bell | 53 |

