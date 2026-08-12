# wa-spritetool

Extracts Worms Armageddon graphics archives (.dir) and decodes the sprites and images inside them to BMP and animated GIF.


The `.spr` sprite format has no public specification; it was reverse engineered
for this tool and is documented in [SPR_FORMAT.md](SPR_FORMAT.md).

## Requirements

Python 3.8 or newer. [Pillow](https://pypi.org/project/Pillow/) is optional and
only affects GIF writing — without it a built-in encoder is used instead.

```bash
pip install pillow      # optional
```

## Install

```bash
git clone https://github.com/EdLud/wa-spritetool
cd wa-spritetool
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

### Build an archive

```bash
python3 wa_spritetool.py pack Level.dir.txt
python3 wa_spritetool.py pack Level.dir.txt output/
```

Takes a `<name>.dir.txt` listing -- one entry per line, in the order they
should appear -- and writes `<name>.dir` in lower case beside it, or in
`output/` if given. The listed files are read from the folder containing the
listing; `gfx0\name.spr` style entries come from a `gfx0` subfolder.

Sprites and images are rebuilt from their `.bmp` (plus `.spd` for sprite
metadata) so edits to those take effect. Anything else -- `.inf`, `.txt` -- is
copied through unchanged.

| Flag | Effect |
|---|---|
| `--no-compress-img` | store images uncompressed |
| `--no-recreate` | reuse an existing `.spr`/`.img` instead of rebuilding from BMP |
| `--opaque-img` | treat images as having no transparent colour |

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

Each image becomes a single `<name>.img.bmp`. Both use the names `pack`
expects, so a decoded archive can be edited and built straight back into a
`.dir`.

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
| `.spr` sprite | yes | compressed only |
| `.img` image | yes | yes |
| `.bnk` sprite bank | yes | no |

Archives from Worms Armageddon, Worms World Party Aqua and Online Worms are
all read; the older Online Worms files name their sprites and images inside
the file, which is handled automatically.

Uncompressed sprites can be read but not written; every sprite the game ships
inside a `.dir` is compressed, so `pack` produces compressed output.

## Documentation

- [SPR_FORMAT.md](SPR_FORMAT.md) — the sprite format, as far as it is understood

## Credits

The Team17 compression algorithm was reverse engineered by acme_pjz and revised
by Pisto; the graphics directory format by Jon Skeet.

## License

GPL-3.0. See [LICENSE](LICENSE).

Worms Armageddon and its data are property of Team17. This repository contains
no game data — only the code to read it.
