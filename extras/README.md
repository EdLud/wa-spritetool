# extras

Tools to experiment with animations, and the modules they share.

None of this is needed to pack a terrain -- `spritetool.py` does that on its
own. These are for making the art that goes in, and for looking at how it
moves before the game sees it.

Three files sit at the top and are imported by everything else:

| module | what it holds |
|---|---|
| `colors.py` | the fixed gfx0/gfx1 palettes, and operations on colour |
| `spr.py` | reading and writing `.spr` sprites |
| `gif.py` | previewing a sprite strip as an animation |

`colors.py` is the one that matters. A sprite in a terrain's `gfx0` or `gfx1`
folder carries a palette, but the game does not paint with it -- it indexes
into a fixed table it already holds. Art outside those 89 colours comes out
recoloured. Every tool here and `spritetool.py` read the same two tables from
this file, so they cannot drift apart.

Each tool lives in its own folder with whatever it needs:

```
make-grass-wind/
  make-grass-wind.py
  assets/grass_blade.png
```

Run one with `--help` to see what it takes. Most write a PNG sprite strip and
a GIF beside it, so you can look at the animation before the game does.

## Seeing them all at once

```bash
./extras/preview_extras_gifs.py
```

Runs each animation tool at small settings and drops one GIF per tool into
`extras/previews/`, which is gitignored. Worth doing after touching a shared
module: a break there often shows up as motion going wrong rather than as an
error, and a folder of GIFs is quicker to scan than five separate runs.

A preview that comes out blank is reported as EMPTY rather than passing
quietly -- the tools size their subjects against the canvas, so a small one
can draw nothing at all while still exiting cleanly.

`make-fly-bursts` is skipped unless `GFX0_DIR` points at a decompressed gfx0
folder; it takes its frame geometry from the sprites it replaces, and this
repo ships no game data.

## The tools

**Animation** — `make-grass-wind` (blades swaying), `make-back2` (a reef
swaying), `make-bubbles` (bubbles rising), `make-flies` and `make-fly-bursts`
(insects milling and scattering), `make-anim-test` (rising circles, to check
a strip reads at all).

**Texture** — `make-seamless-texture` (tile a photograph without a seam),
`make-dendrites` and `make-coral` (grown patterns).

**Terrain pieces** — `build-grass`, `build-bridges`, `build-debris`.

**Odds and ends** — `make-spr` (write a `.spr` directly), `repalette`,
`convert-to-bmp`, `cutout-transform` (split a sheet into objects),
`make-object-gifs`, `make-grids`.
