# spritetool

Extracts and packs Worms Armageddon graphics archives (.dir) and decodes the sprites and images inside them to BMP and animated GIF.

Alongside it, [extras/](extras/) holds tools to experiment with animations:
grass that sways, bubbles that rise, insects that mill about.


The `.spr` sprite format has no public specification; it was reverse engineered
for this tool and is documented in [SPR_FORMAT.md](SPR_FORMAT.md).

## Requirements

Python 3.8 or newer, and [Pillow](https://pypi.org/project/Pillow/).

```bash
pip install pillow
```

## Install

```bash
git clone https://github.com/EdLud/spritetool
cd spritetool
```

## Usage

### List an archive

```bash
python3 wa_spritetool.py list Gfx.dir
```

### Extract raw files

```bash
python3 wa_spritetool.py extract Gfx.dir output/
```

Writes the archive's files unchanged, preserving any internal subdirectories.

### Decode a map

```bash
python3 wa_spritetool.py land land001.dat output/
python3 wa_spritetool.py land Ropetm01.WSM output/
```

Writes the terrain as BMPs — the visible land, the collision mask and the
background layer — plus a `.txt` with the map's dimensions, water height,
texture path and object placements.

### Build an archive

```bash
python3 wa_spritetool.py pack my-water/
python3 wa_spritetool.py pack my-water/ output/
```

Point it at a folder and it works out the rest. A picture with a `.spd` beside
it is a sprite, any other picture is an image, and everything else rides along
untouched — that is all a `.dir` is. Subfolders become the `hi\name.img` style
entries the game's own archives use.


### Build a terrain

```bash
python3 wa_spritetool.py pack-terrain my-terrain/
python3 wa_spritetool.py pack-terrain my-terrain/ output/
```

Everything `pack` does, plus everything that is needed to build a terrain that loads.

```
my-terrain packed/
├── Level.dir
├── TEXT.img        from icon.png, which must be 64x64
└── Water.dir       copied through if the folder has one
```

The output folder may not be the source folder — the archive would land among
the art it was built from, where the next run would try to pack it. Leave the
output off and a `<name> packed` folder is made beside the source.

A terrain must bring its own **land texture** and **sky**: `text.png` and
`gradient.png`, if these two files are not present, the command will refuse to work.

An **icon** is required too, but that one the tool will lend: `icon.png` at
64x64, becoming `TEXT.img` beside `Level.dir`. Without one the game shows
nothing for the terrain on its land generator screen.
 

- the **core assets** are found by their fixed names: `text.img`, `soil.img`,
  `grass.img`, `gradient.img`, `bridge.img`, `bridge-l.img`, `bridge-r.img`,
  `back.spr`, `_back.spr`, `back2.spr`, `front.spr`, `debris.spr`
- a picture with a `.spd` beside it is a **sprite**; any other picture that is
  not a core asset is an **object**, so objects can be named anything
- **sprite overrides** are read from a `gfx`, `gfx0` or `gfx1` subfolder
- `index.txt` is generated into the archive, alphabetically. 

`toaster.img.bmp` is SpriteEditor's spelling and works, but the `.img` is
optional here: a plain `toaster.png` means the same thing, since an object is
always an `.img` in the archive.

An object's settings live in `object_settings.txt`, described below. One with
no entry there takes the guide's defaults (`5 0 0 1 1 3`).

Both commands also take a `<name>.dir.txt` listing, and one inside a scanned
folder takes precedence, for archives whose entry order matters:

```bash
python3 wa_spritetool.py pack-terrain Level.dir.txt output/
```

Sprites and images are rebuilt from their `.bmp` or `.png` (plus `.spd` for
sprite metadata, which is required). Anything else is copied through unchanged.

### PNG sources

A `.png` may stand in for the indexed `.bmp` anywhere, so art need not be
indexed by hand first. The game has one transparent colour rather than an
alpha channel, so converting one is lossy in two ways and both are reported:

- alpha is **thresholded at 128** -- at or above it a pixel is drawn, below it
  the pixel becomes index 0, the transparent one
- colours are reduced by median cut, and the mean distance they moved is
  printed, out of the 441 that spans the RGB cube

The reduction is done **once for the whole terrain**, not per picture. The game
aggregates every picture's palette into one table and the guide caps it at 112
colours, warning that a terrain past it will not load -- and thirty-two
pictures of a hundred colours each come to far more than a hundred together.
So `pack-terrain` reads all the art first, cuts one palette across it, and
maps every PNG onto that. Already-indexed sources are packed exactly as
authored and their colours counted against the budget, leaving the rest for
the PNGs.

The `gfx0`/`gfx1` overrides do not count towards it: those replace what the
game would otherwise take from `Gfx.dir`, so they are not the terrain's own
art. Under that rule the median across a stock install is exactly 112.

A terrain already inside the budget converts losslessly. Where both a `.bmp`
and a `.png` exist the `.bmp` wins, being already indexed and so authored
exactly.

Rules the terrain guide states outright are refused rather than written by
`pack-terrain`: more than 32 objects crashes the game on the land generator
screen, and an object whose name contains a space crashes it on load -- the
game reads the name only as far as the space, looks for a `.inf` that is not
there, and dies on what it did not find. None of the 3217 objects in a stock
install has one. Dimension and palette advice is reported and built anyway,
since the shipped terrains do break it.

| Flag | Effect |
|---|---|
| `--no-compress-img` | store images uncompressed |
| `--no-recreate` | reuse an existing `.spr`/`.img` instead of rebuilding from BMP |
| `--opaque-img` | treat images as having no transparent colour |
| `--force` | write the archive even if it would not load |
| `--defaults` | take a missing bridge or debris from the tool without asking |
| `--no-defaults` | never take either |
| `--no-output-inf` | do not write object settings back into the folder |
| `--write-palette` | draw the terrain's colours to `palette.png` |
| `--read-palette` | fit every picture to the colours in `palette.png` |
| `--no-palette` | cut no shared palette; each picture keeps its own |

`--write-palette` puts a `palette.png` in the source folder, the terrain's
colours as a grid of swatches, and says how many of the 112 are spent. It is
read back out of the finished archive rather than from the plan that made it,
so a terrain of already-indexed art -- which needs no plan -- draws one too.
Squares past the last colour are left transparent, so what is counted and what
is drawn agree.

`--no-palette` skips the shared cut entirely. Each picture keeps the colours
it was authored with, and an indexed source is packed exactly as it stands --
for an author who has already fitted their art to a palette they chose and
does not want it nudged to make room for the rest of the terrain.

The 112 then becomes theirs to stay inside. Nothing enforces it here, but the
count after packing says what the total came to: Entomology's PNGs cut to 112
together, and 4256 apart. A PNG drawing more colours than an `.img` can hold
is still reduced, since that is the format's limit rather than a choice this
flag can waive; the difference is that the reduction looks at one picture
instead of all of them.

`--read-palette` takes that file back and fits every picture to it, instead of
cutting a palette from the art. Edit the swatches, or hand over a palette of
your own -- any picture will do, its colours read in the order they are met.
Unlike the cut, this applies to already-indexed sources as well: a palette
given outright is meant to be the whole of the terrain's colours, so a `.bmp`
is fitted to it like everything else, and the shift is reported per picture.

Writing then reading gives back the same colour set, though not quite the same
archive: the cut weighs each colour by how often a picture uses it, where the
sheet is a flat list, so a few pixels land on a different near-neighbour.

### Object settings

`pack-terrain` keeps every object's settings in one `object_settings.txt`,
written into the folder on the first run and read on every one after:

```
// probability  1 to 10. Affects the chance of an object being placed...
// where        2 = ceiling, 3 = floor, 0 or 1 = the side of the terrain

floor1.png
probability = 5
front = 0
soil = 0
collide = 1
nostack = 1
where = 3
```

The file opens with a comment describing all six settings, so what each does
is where it is needed. A filename opens a block and its `key = value` lines
follow; blank lines and `//` comments are ignored, and any key left out takes
the guide's default. `where` is the one to
reach for: `3` puts an object on the floor, `2` on the ceiling, `0` or `1` on
the left or right wall. Each key holds the number the format holds, so
`collide = 1` enables collision exactly as the guide describes it.

Entries are written alphabetically and their order carries no meaning -- the
terrain is packed alphabetically whatever the file says.

The format itself keeps these in a `.inf` beside every object, and the archive
still holds them that way. If a folder has those, `pack-terrain` offers to
move them into `object_settings.txt` and delete them; declining stops the
build rather than leave the settings in two places. A loose file setting what
`object_settings.txt` already sets is refused outright, since there is no
saying which was meant.

Where an object has both a `.inf` and a `.txt`, the `.inf` is packed and the
tool says so, since the two can disagree.

### Defaults

`presets/` beside the tool stands in for the pieces a folder has not
got, so a terrain can begin as a texture, a sky and one object.

A terrain **must** have an icon and all three bridge pieces. The game draws a
bridge whenever a map is generated with them, and shows the icon on its land
generator screen. Both are offered from `presets/` and `pack-terrain` stops
rather than write a terrain it knows the game will not take.

A terrain **need not** have debris, whatever the guide says: one packs and
plays without it, just with an emptier sky. So the default is offered and
declining it carries on.

Nor does it need a `back.spr`, and none is lent: 24 stock terrains have none,
Coral Reef among them, and the sky shows through where a background would be.
One that is supplied has to draw something, though -- the game crashes
compositing a background with no colours in it, so an entirely transparent one
is refused.

`soil` and `grass` are filled in silently — both are blank, so there is no
look being imposed and nothing to decide. A blank soil is what five shipped
terrains do, and it shows the background through destroyed land rather than
someone else's dirt.

The defaults spend **none** of the 112-colour budget. It goes to the author's
own art, and the defaults are then fitted to whatever palette that produces,
however badly they come out — a bridge in the wrong colours is a prompt to draw
one, where a texture reduced to make room for a bridge is a loss.

Accepting copies the art into the terrain's own folder rather than reading it
from the tool, so it is yours to edit and the next run picks up whatever you
have made of it:

```
This terrain has 0 of the 3 bridge pieces; the game needs all three and will
not load without them.
Use the default bridge and write it to my-terrain? [y/N] y
  copied bridge.img.png into my-terrain
```

Neither prompt appears when the folder already has the art, and an edited copy
is never written over. If `presets/` is missing, `pack-terrain` says
where to fetch it and keeps going where it can.

### Decode sprites and images

```bash
python3 wa_spritetool.py decompress Gfx.dir output/
python3 wa_spritetool.py decompress Gfx.dir output/ --gif
```

For each sprite this writes three files:

| File | Contents |
|---|---|
| `.spr` | decoded pixels, all frames stacked vertically |
| `.bmp` | the same sheet as an 8-bit indexed bitmap |
| `.spd` | frame count, dimensions, frame rate, playback flags |

A terrain's icon is not in the archive -- it sits beside it as `text.img` in
whatever casing its author used -- so it is written out as `icon.img.bmp`,
the name `pack-terrain` looks for. Without that a terrain taken apart and put
back together would lose its icon and be offered a default instead. The land
texture shares the name and is told apart by being 256x256 rather than 64x64.

Each image becomes a single `<name>.img.bmp`. Anything that is not a picture --
`.inf` object parameters, `index.txt`, fonts -- is copied through untouched,
and a `<name>.dir.txt` listing the archive's entries in order is written
alongside. The result is a folder `pack` builds straight back into a `.dir`,
with the objects, palettes and frame counts intact.

A sprite bank (`.bnk`) holds many unnamed animations sharing one palette, so
its sprites go in a folder named after the bank and are numbered in order:
`mainspr/0000.spr`, `mainspr/0001.spr`, and so on.

`--gif` additionally writes one animated GIF per sprite. It is off by default
because GIF encoding takes far longer than everything else combined: decoding
all 770 sprites in `Gfx.dir` takes about two seconds, and adding `--gif` turns
that into several minutes.

Output is grouped by source archive, so several archives can share one output
directory:

```
output/
├── Gfx/            decoded sprites
│   ├── airjetb.spr
│   ├── airjetb.spr.bmp
│   ├── airjetb.spr.spd
│   └── ...
└── Gfx gifs/       animations (only with --gif)
    ├── airjetb.gif
    └── ...
```

## Supported formats

| Format | Read | Write |
|---|---|---|
| `.dir` graphics directory | yes | yes |
| `.spr` sprite | yes | yes |
| `.img` image | yes | yes |
| `.bnk` sprite bank | yes | no |
| `land.dat` map | yes | no |

Archives from Worms Armageddon, Worms World Party Aqua and Online Worms are
all read. Their differences are handled automatically: Online Worms names its
sprites and images inside the file, and Aqua stores some of its graphics under
a second compression that has no published description, documented in
[AQUA_COMPRESSION.md](AQUA_COMPRESSION.md).

### Icons and land textures

Two different files share the name `text.img`: the 64x64 icon that sits beside
`Level.dir`, and the 256x256 land texture packed inside it. The game is not
consistent about which case it uses for either -- 36 of its themes spell the
icon `TEXT.IMG` and 94 spell it `text.img` -- so this tool picks one and holds
to it:

| Name | Role | Size |
|---|---|---|
| `TEXT.img` | icon | 64x64 |
| `text.img` | land texture | 256x256 |

`pack` refuses an icon that is not 64x64, and says so when a `text.img` is
64x64 or a `TEXT.img` turns up mis-cased, rather than leaving either to be
found in the game.

An icon also has to be aligned. Three bytes per palette entry put the pixel
data on a 4-byte boundary only when the number of colours divides by four; at
any other count a compressed icon is padded before its pixels, and the game
crashes on the land generator screen. The count itself is not the constraint --
icons of 4 and of 20 colours both load and convert, while 17 does not -- so
`pack` rounds an icon's palette up to a multiple of four, repeating a colour
already in it.

The count is of colours actually drawn. Colour 0 is the transparent background
and is never stored, so an icon of 16 drawn colours has a 16-entry palette and
is the "17 colours" the terrain guide asks for once transparency is counted.
Reserving a palette slot for transparency instead makes it a real colour at
pixel index 1, which both wastes the slot and, at 16 + 1 entries, is the count
that crashes.

`pack` compresses sprites by default, but `pack-terrain` never compresses
`back.spr` or `debris.spr`: the game reads those two by a route that runs its
decompression loop off the end of the buffer, and it crashes the moment the
background or debris is drawn. Each is uncompressed in 113 of the roughly 119
stock terrains that have one, where `_back.spr` and `back2.spr` -- which the
game reaches differently -- are mostly compressed. Nothing needs passing;
`--no-compress-spr` still turns compression off for every sprite.

## Extras

`spritetool.py` packs a terrain; it does not draw one. [extras/](extras/) is a
set of tools to experiment with animations -- each in its own folder, run with
`--help`:

```bash
python3 extras/make-grass-wind/make-grass-wind.py -o back2.spr.png
python3 extras/make-bubbles/make-bubbles.py --from layer.spr -o bubbles.png
```

Most write a PNG sprite strip and a GIF beside it, so the animation can be
looked at before the game sees it. See [extras/README.md](extras/README.md).

## Documentation

- [SPR_FORMAT.md](SPR_FORMAT.md) — the sprite format, as far as it is understood
- [AQUA_COMPRESSION.md](AQUA_COMPRESSION.md) — Aqua's second compression

## Credits

The Team17 compression algorithm was reverse engineered by acme_pjz and revised
by Pisto; the graphics directory format by Jon Skeet.

## License

GPL-3.0. See [LICENSE](LICENSE).

Worms Armageddon and its data are property of Team17. This repository contains
no game data — only the code to read it.
