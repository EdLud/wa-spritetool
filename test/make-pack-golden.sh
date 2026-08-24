#!/bin/sh
# Regenerate the pack golden. Run from anywhere; diff the result against what
# is committed. Anything that moves is a change in how a terrain is built.
#
# Only the flat fixture has a golden. Its art is inside the 112 colours a
# terrain may hold, so its own colours are the palette and no quantiser runs --
# the archive comes out the same every time, with or without numpy. The wide
# fixture goes through Pillow's median cut, which is deterministic for one
# Pillow and not a promise across versions, so test/run.py asserts what must be
# true of it rather than an exact archive.
#
# The fixture is copied before packing: pack-terrain writes into the folder it
# is given, and test/pack/flat is a pristine input.
set -e
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r test/pack/flat "$work/flat"
# --yes=setup.confirm because the fixture is a pristine input with no
# settings.spritetool.toml: packing asks to set the folder up first, and
# with no terminal to answer it the question takes its safe answer and
# the pack stops. The answer writes only the TOML marker into the copied
# folder, which is not an archive entry, so the golden is unaffected.
python3 spritetool.py pack-terrain "$work/flat/build" "$work/out" \
    --yes=setup.confirm --defaults >/dev/null

golden=test/pack/flat/expected.txt
{
    echo "# md5 of every file pack-terrain writes beside Level.dir."
    echo "# Regenerate with test/make-pack-golden.sh; check with test/run.py."
    for f in $(ls "$work/out" | sort); do
        printf '%s  %s\n' "$(md5 -q "$work/out/$f" 2>/dev/null || md5sum "$work/out/$f" | cut -d' ' -f1)" "$f"
    done
} > "$golden"

echo "wrote $golden"
cat "$golden"
