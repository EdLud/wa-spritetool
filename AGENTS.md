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
python3 spritetool.py unpack-terrain <Level.dir> [output_dir]
python3 spritetool.py pack <folder|name.dir.txt> [output_dir]
python3 spritetool.py pack-terrain <folder> [output_dir] [flags]
python3 spritetool.py land <land001.dat|mission.WSM> [output_dir]
python3 spritetool.py version | help
```

Note: the README's examples say `wa_spritetool.py`; the actual file is
`spritetool.py`. The tool's own help banner calls itself
`wa-py-spriteHelper`. These names refer to the same script.

`pack-terrain` confirms the folder on first use (a `setup.confirm` prompt
that reports its contents -- any folder may be a terrain now, the name no
longer decides), offers a default icon rather than refusing a folder that
has none (including on the `.dir.txt` listing path, where the icon is not
one of the entries the listing names), refuses to write into the source
folder, validates the terrain against the game's loading rules (112-colour
shared palette budget, required assets, icon dimensions), and can borrow
missing assets from `presets/` (`--defaults` / `--no-defaults`). See the
README for the full flag list.

`unpack-terrain` takes a `Level.dir` apart into a spritetool-owned build
folder: the art as BMP, and every object's placement and every terrain
sprite's geometry written to `settings.spritetool.toml`. The result packs
straight back with `pack-terrain`.

`decompress` unpacks the archive it is given and nothing that sits beside
it. It no longer writes a `<name>.dir.txt` listing (a synthesized pack
argument, not archive data -- `pack` rebuilds the order from a folder scan
when none is present), and it no longer writes the terrain icon, which is a
loose `TEXT.img` next to `Level.dir` rather than an entry. `index.txt`, a
real archive entry, is still copied through as data. Carrying the icon is
`unpack-terrain`'s job: that command builds a folder meant to be packed
again, so a terrain taken apart and put back together keeps it.

This matters when measuring a terrain's palette: the icon spends none of the
112-colour budget, and a `decompress` output that included it would count
about ten colours the terrain does not actually share.

## Repository layout

- `spritetool.py` — the entire tool: parsers, encoders, CLI
- `settings_toml.py` — the terrain settings file `settings.spritetool.toml`:
  a hand-rolled TOML reader/writer (no dependency, Python 3.8+ preserved) and
  the `TerrainSettings` model. This one file replaces the SpriteEditor-era
  formats the tool used to write for hand-editing (`object_settings.txt`,
  per-object `.inf`, the `.spr.spd` sidecar), which are now read only to
  migrate them. It imports nothing from the repo, so both `spritetool` and
  `gui` can import it without a cycle.
- `spritetool.command` / `spritetool.bat` — double-click launchers for the
  window, macOS and Windows. Each probes for a Python that can import both
  PySide6 and Pillow rather than the first interpreter it finds (a machine
  with several usually has PySide6 in only one), preferring a `.venv` beside
  the repo; when none can, it names the missing piece and pauses so the
  message is readable after a double-click. `.gitattributes` pins the `.bat`
  to CRLF (cmd.exe mis-parses bare LF) and the `.command` to LF, and the
  `.command` is committed executable.
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
  (`settings.spritetool.toml` via `settings_toml`, with legacy
  `object_settings.txt` / `.inf` read for migration), `DirectoryWriter`
- CLI: `main()` dispatches on `sys.argv[1]`

Parsers return `bool` from `parse()` and populate attributes; they never
raise across the boundary (format errors are caught and reported as parse
failure). `DecompressionError` carries partial progress for format
investigation.

## Testing

There is no unit-test framework. Verification is fixture-based, byte-for-byte.
One command runs all of it:

```bash
python3 test/run.py               # everything
python3 test/run.py --no-numpy    # again, on the pure-Python paths
python3 test/run.py pack          # one group, or several: `pack colours`
```

**Run what the change can reach.** Two groups are almost all of the wait --
`toml` is ~250s and `pack` ~50s, because each packs real terrains and every
pack spawns a process pool (~13s a run, mostly starting and stopping
workers). Everything else together is under 6s. So a decoder change is
`decode`, a palette or encoder change is `pack colours`, and anything about
settings.spritetool.toml, setup, or sprite records is `toml`. Run the whole
suite before a commit; `--no-numpy` only when the change touched colour
counting or palette fitting, which is all numpy does here. A change that
cannot affect a group -- a docstring, a GUI label -- does not need it run.

Non-zero if anything moved. What it covers:

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

- `test/pack/flat/` and `test/pack/wide/` are terrains built from source art,
  which nothing covered before: the fixtures above all read an archive, none
  built one. `flat` draws 12 colours, inside the 112 a terrain may hold, so its
  own colours are the palette, no quantiser runs, and the archive is compared
  by hash against `expected.txt` (regenerate with `test/make-pack-golden.sh`).
  `wide` draws 7122 and goes through Pillow's median cut, which is stable for
  one Pillow and not a promise across versions, so it asserts what must be true
  instead: every required entry present, at most 112 colours, and the archive
  re-parses.

  Packing writes into the folder it is given, so the runner copies a fixture to
  a temp folder and packs the copy. `test/pack/**` is a pristine input.

- The `colours` group checks numpy and pure Python agree. numpy is optional and
  decides which path counts and maps pixels; the two return colours in
  different orders, which feeds Pillow's median cut. It turns out not to change
  the result, which is worth continuing to check rather than assuming.

- The `toml` group covers the settings model: an unpack-terrain round-trip
  (the TOML holds every object's placement and every sprite's geometry, and
  the unpacked folder repacks byte-for-byte identically), migration of the
  legacy formats into the TOML, limited mode when conversion is declined, the
  `setup.confirm` prompt, and that `decompress` writes no `.dir.txt` while
  still passing `index.txt` through. It builds on the `flat` fixture rather
  than Coral Reef -- a round-trip through a shipped 6 MB terrain takes twenty
  seconds, which is the difference between a suite that gets run and one that
  does not.

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
  dependencies; numpy usage must stay optional (guarded import). This rule is
  scoped to the tool: `gui/` may depend on PySide6, because `gui` imports
  `spritetool` and never the other way round. Keep it that way — the tool has
  to stay usable and testable without Qt installed.
- `gui/job.py` runs in a spawned child and must not import Qt, directly or
  through `gui/app.py`. Packing there rather than in the window is what makes
  cancelling possible: `Team17Compressor.compress` is a tight Python loop that
  would ignore any request to stop, so a thread could only be waited for.
- Type hints are used on signatures; module-level constants in UPPER_CASE.
- Comments explain the *why*, especially reverse-engineered format facts —
  cite observed evidence (file counts, offsets) the way the existing code
  and `SPR_FORMAT.md` do. When a format behaviour is unresolved, say so in
  the comment rather than guessing.
- Docstrings and prose follow the existing plain, precise tone.
- CLI errors print `Error: ...` and return exit code 1; notes go to stderr.
- Every interactive prompt carries a `Question` with a stable `key`, and any
  key can be answered ahead of time with `--yes=KEY` / `--no=KEY`. That is
  structural rather than a habit: a prompt reachable only from a terminal
  cannot be reached from a window, and there was one of those.
  `--defaults` / `--no-defaults` are shorthand for the `defaults.` group.
  The keys are `setup.confirm` (set a folder up as a terrain),
  `settings.convert_toml` (migrate legacy object settings),
  `settings.convert_listing` (pack from scan + TOML instead of a
  `.dir.txt`/`index.txt` listing), `settings.clear_legacy` (delete the
  SpriteEditor-era files the TOML now answers for), `archive.clear_listing`
  (delete `<name>.dir.txt` and `index.txt`), `archive.clear_built` (delete
  built `.spr`/`.img` whose sheet is beside them), `defaults.<piece>`, and
  `palette.repalette`. `settings.consolidate` is superseded on the TOML path.
- Questions never block a run that has no terminal. When stdin is not a TTY
  and nothing answered the key, the question takes its own default and says
  `[n, not a terminal]`. A pipe nobody writes to never reaches EOF, so
  waiting on it would hang the build rather than ask anything; `--yes=` /
  `--no=` is how a script answers.
- Converting a folder offers to clear the legacy files away
  (`settings.clear_legacy`, default no). Only files the TOML wholly answers
  for: the per-object `.inf`/`.txt`, `object_settings.txt`, the old
  `settings.spritetool` marker, and a `.spr.spd` for a sprite the TOML holds
  geometry for -- the migration copies that geometry into `[sprite.*]` first,
  since a sheet says nothing about its own frame count. `index.txt` and
  `<name>.dir.txt` are never deleted: the first is a real archive entry and
  the second is the author's record of entry order. Art borrowed from
  `presets/` keeps its own sidecars, which are not the author's to lose.
- Setting a folder up also offers to clear what it no longer needs, as
  separate questions so each can be answered on its own: the settings files
  the TOML absorbed, the `<name>.dir.txt`/`index.txt` pair (packing reads
  neither -- entries come from the scan and `index.txt` is generated into the
  archive alphabetically), and the built `.spr`/`.img` files. A built picture
  is only offered when a `.bmp`/`.png` sits beside it to rebuild it from, and
  a `.spr` only when its frame count is in the TOML or a `.spd` -- a sheet
  says nothing about its own frame count. All default to no.
- The window re-reads its folder from one place, `Window.refresh`. Three
  things call it: `View -> Refresh` (F5 and Ctrl+R, the latter being Cmd+R on
  macOS), and `applicationStateChanged` when the app is brought forward --
  editing a file means being in another program, so returning is the gesture
  that follows an edit, and catching it needs no watcher on hundreds of
  files. It refuses while a job runs (packing writes into the folder it
  reads) and offers to save the object table when it is dirty, since a reload
  would drop those edits. `QKeySequence.Refresh` alone is not enough: it is
  F5 on every platform, macOS included.
- The window takes archives as well as folders. `DropZone` classifies what
  was dropped (`is_archive`, `.dir` only -- the one extension `extract` and
  `decompress` read) and the window asks output folder, then extract vs
  decompress, then GIFs. The work runs through `job.unpack` in a spawned
  child, the same arrangement packing uses: a `Water.dir` decoded to GIFs
  takes about twenty seconds. `job.unpack` reaches `extract`/`decompress` by
  calling `spritetool.main()` with a built argv, because there are no
  functions behind those commands -- they live inside `main()`'s dispatch,
  and a second copy here would drift from it.
- `sprite_records(folder)` pairs every sprite with its record and says what
  does not add up: `picture_size` reads a sheet's dimensions from its header
  alone (a parallax sheet is 1024x32000 and decoding one to measure it costs
  seconds), and `sprite_geometry_problem` holds the rule the packer enforces
  -- cells stack vertically, so `cell_h * frames` must equal the sheet's
  height and `cell_w` its width. Setup reports every sprite that fails it;
  the packer still refuses, but on the first one it reaches, which is a poor
  way to learn that three are wrong. The window's Sprites tab shows the same
  data. Only `flags` is editable there, as a named choice from the guide
  (0 stop, 1 loop, 2 forwards-back, 3 ping pong); geometry stays read-only
  because it has to agree with the sheet. `framerate` is not shown at all --
  the game ignores it -- but SpriteTable keeps the records it read so a save
  writes it back rather than dropping a field that was merely off screen.
  Saving refuses on a folder whose geometry still lives in `.spr.spd`
  sidecars, which would otherwise gain a TOML that disagrees with them.
- `_spd_geometry` reads the `.spr.spd` sidecars in the folder *and* in each
  `gfx0`/`gfx1` override folder, keyed as the packer looks them up (`gfx0\
  cloudm`). Coral Reef keeps 450 sprites in `gfx0`, so a migration that
  stopped at the top level would strand their frame counts.
- A terrain's settings live in `settings.spritetool.toml`, read and written
  through `settings_toml.py` -- a hand-rolled TOML parser/writer, no
  dependency, Python 3.8+ preserved (the stdlib's `tomllib` is 3.11+ and
  read-only). The legacy `.inf` / `.spr.spd` / `object_settings.txt` formats
  are read-only, for migration; the tool never writes them again. The `.inf`
  entries written INTO the archive are unchanged -- that is the game's
  format, not the author's. The `build/`-name gate is gone: any folder may be
  a terrain, confirmed by the `setup.confirm` prompt rather than refused by
  name.
- Process pools go through `_pool()`, which asks for the `spawn` start method
  everywhere so there is one behaviour to test rather than one per platform,
  and marks each worker via the initializer. How many processes to use is
  `Parallel`'s decision, never a fresh `os.cpu_count()` at the call site.
  Whatever the setting, the archive must come out byte for byte the same —
  `test/run.py jobs` checks that, because the pools fall back to serial work
  inside a bare `except` and a disagreement would otherwise be invisible.
- Anything that finds a file beside the tool must go through
  `defaults_roots()`. A frozen build has no `__file__` to be beside, and the
  failure is silent — it looks exactly like "no presets installed".
- Windows paths matter: archive entry names use `\` separators, and the tool
  is expected to run on Windows (no find(1) assumptions; CRLF in generated
  `.spd`/`.inf` files to match SpriteEditor).
- Output folders are named after the source archive so several archives can
  share one output directory without overwriting each other.

## Multiple agents work in this repo

More than one AI agent (and the author) edits this codebase, often in
parallel sessions that share no context. Work so the next agent — which may
not be you — can pick up cleanly:

- Re-read a file before editing it rather than trusting memory from earlier
  in the session; another agent may have changed it since.
- At session start, `git log` and `git status` show what changed recently.
  Uncommitted work from another session may be sitting in the tree — do not
  overwrite or revert it without asking.
- Keep `SPR_FORMAT.md`, the README and code comments current with what the
  code actually does. These are the shared memory between agents; a stale
  comment sends the next agent down a wrong path.
- Record reverse-engineered findings and the evidence for them in
  `SPR_FORMAT.md`, not just in the conversation that found them.

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
  `settings.spritetool.toml`). Deleting or overwriting user files there must
  stay behind explicit prompts or flags -- the legacy settings files are
  migrated only with a yes, and never deleted.
- License: GPL-3.0; see `LICENSE`.
