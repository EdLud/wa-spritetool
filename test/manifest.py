#!/usr/bin/env python3
"""Describe Coral Reef as this tool decodes it, one line per entry.

A whole decompressed copy would be 228 MB of BMPs, nearly all of it pixels
that the .dir already holds. This keeps what a decoder regression would
actually disturb -- each entry's kind, geometry and size -- and for the gfx0
overrides, whether their colours stay inside the slot's fixed palette. That
last column is the one worth having: the game paints those from its own
table, and all 446 of Coral Reef's stay inside it.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'extras'))

import spritetool as st
import colors

BASE = 'test/wa/Coral Reef'


def rgb_used(sprite):
    """The distinct colours a sprite draws, as RGB triples."""
    sheet = sprite.render_sheet()
    if sheet is None:
        return None
    pal = sprite.rgb_palette()
    return {(pal[v * 3], pal[v * 3 + 1], pal[v * 3 + 2])
            for v in set(sheet) if v}


def main():
    print("Coral Reef, as this tool decodes it.")
    print("Regenerate with test/make-manifest.sh and diff; any change is a")
    print("change in how a shipped terrain is read.")
    print()
    for arc in ('level.dir', 'text.img', 'water.dir'):
        b = open(os.path.join(BASE, arc), 'rb').read()
        print(f'{arc}\t{len(b)}\tsha256:{hashlib.sha256(b).hexdigest()}')

    gfx0 = set(colors.GFX0)
    for tag, root in (('level.dir', '/tmp/cr_x'), ('water.dir', '/tmp/cr_wx')):
        rows = []
        for d, _, fs in os.walk(root):
            for f in sorted(fs):
                fp = os.path.join(d, f)
                b = open(fp, 'rb').read()
                rel = os.path.relpath(fp, root).replace(os.sep, '/')
                rel = rel.replace('\\', '/')
                kind, dims, note = '-', '-', ''
                if b[:4] == b'SPR\x1a':
                    s = st.SpriteFile(b)
                    if s.parse():
                        kind = 'spr'
                        dims = f'{s.width}x{s.height}x{s.frames}'
                        if '/gfx0/' in f'/{rel}':
                            used = rgb_used(s)
                            if used is not None:
                                stray = len(used - gfx0)
                                note = ('gfx0-ok' if not stray
                                        else f'gfx0-stray:{stray}')
                elif b[:4] == b'IMG\x1a':
                    i = st.ImageFile(b)
                    if i.parse():
                        kind, dims = 'img', f'{i.width}x{i.height}'
                rows.append(f'{rel}\t{kind}\t{dims}\t{len(b)}\t{note}'.rstrip())
        print(f'\n-- {tag}: {len(rows)} entries')
        for r in sorted(rows):
            print(r)


if __name__ == '__main__':
    main()
