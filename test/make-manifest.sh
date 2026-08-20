#!/bin/sh
# Regenerate the Coral Reef manifest. Run from the repo root; diff the result
# against what is committed. Anything that moves is a change in how a shipped
# terrain is decoded.
set -e
cd "$(dirname "$0")/.."
rm -rf /tmp/cr_x /tmp/cr_wx
python3 spritetool.py extract "test/wa/Coral Reef/level.dir" /tmp/cr_x >/dev/null
python3 spritetool.py extract "test/wa/Coral Reef/water.dir" /tmp/cr_wx >/dev/null
python3 test/manifest.py > "test/wa/Coral Reef/manifest.txt"
echo "wrote test/wa/Coral Reef/manifest.txt"
