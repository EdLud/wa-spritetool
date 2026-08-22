# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project overview

**spritetool** is a single-file Python command-line tool for the 1999 game
Worms Armageddon (and its relatives Worms World Party Aqua and Online Worms).
It reads and writes the game's proprietary graphics formats:

- extracts `.dir` graphics archives and decodes the `.spr` sprites and `.img`
  images inside them to BMP and animated GIF
- packs a folder of art back into a `.dir`, including full terrain packages
  (`Level.dir` + `TEXT.img` + optional `Water.dir`) with validation against
  the game's loading rules
- decodes `land*.dat` / mission maps to BMP layers

The `.spr` sprite format has no public specification; it was reverse
engineered for this tool and is documented in `SPR_FORMAT.md`. The `.dir`
container and the Team17 LZ77 compression routine follow the Worms Knowledge
Base (algorithm reverse engineered by acme_pjz, revised by Pisto; directory
format by Jon Skeet).

There is no build system, no package manifest, and no dependency manager
file. The whole tool is `spritetool.py` (~4100 lines, stdlib + Pillow;
numpy is an optional speed-up for palette fitting). Version is the
`__version__` constant at the top of `spritetool.py` (currently 0.3.0).

## Requirements and setup

- Python 3.8+ (developed on 3.14)
- Pillow is required: `pip install pillow`
- numpy is optional (faster PNG colour fitting only)
- Some extras tools optionally use `ffmpeg` for GIF assembly

No installation step: run the script in place.

## Commands (the tool itself)

```bash
python3 spritetool.py list <archive.dir>
python3 spritetool.py extract <archive.dir> [output_dir]
python3 spritetool.py decompress <archive.dir|folder> [output_dir] [--gif]
python3 spritetool.py pack <folder|name.dir.txt> [output_dir]
python3 spritetool.py pack-terrain build/ [output_dir] [flags]
python3 spritetool.py land <land001.dat|mission.WSM> [output_dir]
python3 spritetool.py version | help
```

Note: the README's examples say `wa_spritetool.py`; the actual file is
`spritetool.py`. The tool's own help banner calls itself
`wa-py-spriteHelper`. These names refer to the same script.

`pack-terrain` requires the input folder to be named `build`, refuses to
write into the source folder, validates the terrain against the game's
loading rules (112-colour shared palette budget, required assets, icon
dimensions), and can borrow missing assets from `presets/` (`--defaults` /
`--no-defaults`). See the README for the full flag list.

## Repository layout

- `spritetool.py` — the entire tool: parsers, encoders, CLI
- `SPR_FORMAT.md` — reverse-engineering notes for the sprite format (working
  notes, with unresolved points marked)
- `README.md` — user documentation; the terrain-authoring workflow is
  documented here and nowhere else
- `presets/` — default terrain assets offered when a build is missing one;
  they spend none of the 112-colour budget
- `extras/` — standalone art-generation tools (grass wind, bubbles, flies,
  textures, terrain pieces), each in its own folder, plus two shared modules:
  - `extras/colors.py` — the fixed gfx0/gfx1 palettes (89 colours each); the
    single source of truth, imported by both the extras and by test tooling.
    The game paints gfx0/gfx1 sprite overrides from these fixed tables and
    ignores the palette inside the `.spr`, so art outside them comes out
    recoloured.
  - `extras/gif.py` — sprite-strip-to-GIF preview helper
  - `extras/preview_extras_gifs.py` — runs every animation tool at small
    settings and collects one GIF per tool into `extras/previews/`
    (gitignored); a smoke test for the shared modules
- `test/` — decoder fixtures; see "Testing" below
- `Terrains/` — local terrain working folders, gitignored, not shipped

## Code organisation inside spritetool.py

One file, organised top to bottom roughly as:

- compression: `Team17Decompressor`, `AquaDecompressor` (Aqua's undocumented
  second compression), `Team17Compressor`
- format parsers: `SpriteFile` (.spr), `ImageFile` (.img), `BankFile` (.bnk,
  read-only), `LandFile` (maps), `DirectoryReader` (.dir)
- colour handling: BMP/PNG reading, median-cut palette planning
  (`plan_shared_palette`, `cut_palette`), the 112-colour budget
- encoders: `encode_sprite`, `encode_image`, `encode_icon`
- terrain packing: `scan_terrain`, `archive_problems`, object settings
  (`object_settings.txt` / `.inf` handling), `DirectoryWriter`
- CLI: `main()` dispatches on `sys.argv[1]`

Parsers return `bool` from `parse()` and populate attributes; they never
raise across the boundary (format errors are caught and reported as parse
failure). `DecompressionError` carries partial progress for format
investigation.

## Testing

There is no unit-test framework. Verification is fixture-based, byte-for-byte:

- `test/wa/`, `test/wwp online/`, `test/wwp aqua/` each hold a real
  `Water.dir` as the game ships it, plus `decompressed/` as this tool writes
  it. After any decoder change:

  ```bash
  python3 spritetool.py decompress "test/wa/Water.dir" /tmp/out
  diff -r "test/wa/decompressed" /tmp/out
  ```

  Silence means the decoder still reads that game the same way. All three
  games must be checked — they are not interchangeable (Aqua writes `LND\x1B`
  where the others write `LND\x1A`, its sprite bank differs, Online Worms
  sits between the two).

- `test/wa/Coral Reef/` is a whole shipped terrain. It is committed as
  `manifest.txt` (kind, geometry, size per entry, plus a gfx0 palette-stray
  check) rather than 228 MB of BMPs. Regenerate and diff:

  ```bash
  ./test/make-manifest.sh
  git diff test
  ```

  Any movement in the manifest is a change in how a shipped terrain is read.
  The script extracts to `/tmp/cr_x` and `/tmp/cr_wx`.

- After touching `extras/colors.py` or `extras/gif.py`, run
  `./extras/preview_extras_gifs.py` and look at the GIFs — a break there
  often shows as wrong motion rather than an error. Blank output is reported
  as EMPTY. `make-fly-bursts` is skipped unless `GFX0_DIR` points at a
  decompressed gfx0 folder (no game data ships in this repo).

The fixtures are compared byte for byte, so `.gitattributes` sets
`test/** -text -diff`: nothing in them may be line-ending-converted or
otherwise rewritten (the `.spd` files carry CRLF because that is what
SpriteEditor writes).

## Conventions

- Language: all code, comments, and docs are in English.
- Standard library only in `spritetool.py`, plus Pillow. Do not add
  dependencies; numpy usage must stay optional (guarded import).
- Type hints are used on signatures; module-level constants in UPPER_CASE.
- Comments explain the *why*, especially reverse-engineered format facts —
  cite observed evidence (file counts, offsets) the way the existing code
  and `SPR_FORMAT.md` do. When a format behaviour is unresolved, say so in
  the comment rather than guessing.
- Docstrings and prose follow the existing plain, precise tone.
- CLI errors print `Error: ...` and return exit code 1; notes go to stderr.
- Interactive prompts (e.g. borrowing defaults) must always have a flag that
  answers them ahead of time for scripting (`--defaults` / `--no-defaults`).
- Windows paths matter: archive entry names use `\` separators, and the tool
  is expected to run on Windows (no find(1) assumptions; CRLF in generated
  `.spd`/`.inf` files to match SpriteEditor).
- Output folders are named after the source archive so several archives can
  share one output directory without overwriting each other.

## Security and legal considerations

- **No game data in the repo.** `*.dir` files are gitignored globally except
  the small fixtures under `test/` (explicitly un-ignored). Never commit game
  assets elsewhere; the repo ships code only. Worms Armageddon and its data
  are property of Team17.
- The tool parses untrusted binary data: keep the sanity bounds (`MAX_DIM`,
  stream-count caps, table-vs-file-length checks) intact when touching
  parsers, and never let a malformed file produce a silently zero-padded
  image — that hides format bugs (see `DecompressionError`'s docstring).
- `pack-terrain` writes into the user's build folder (borrowed defaults,
  `object_settings.txt`, migrated `.inf` files). Deleting or overwriting
  user files there must stay behind explicit prompts or flags.
- License: GPL-3.0 per the README (note: no LICENSE file is currently
  present in the repo).
