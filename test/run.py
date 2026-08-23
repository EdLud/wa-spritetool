#!/usr/bin/env python3
"""Run every fixture check in one command. Non-zero if anything moved.

    python3 test/run.py
    python3 test/run.py --no-numpy      # the pure-Python paths
    python3 test/run.py decode          # just one group

Five groups:

  decode    the three Water.dir fixtures, decompressed and diffed byte for byte
  manifest  Coral Reef re-described and diffed against the committed manifest
  pack      a terrain built from source art and compared against a golden
  padding   compressed art widened to a multiple of 4, its height left alone
  colours   numpy and pure-Python agreeing on what they count

`pack` is the one that did not exist before. The decode fixtures cover reading
an archive; nothing covered building one, so every packing change was checked by
hand and nothing checked it afterwards.

Packing writes into the folder it is given -- borrowed presets, settings, art
refitted in place -- so a fixture is copied to a temp folder before each run and
the copy is what gets packed. The fixtures under test/pack are pristine inputs
and must stay that way.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, 'spritetool.py')

# Blocks the numpy import so a run exercises the pure-Python fallbacks. numpy is
# optional at runtime and decides which code path counts and maps pixels, so
# "it works here" is only half an answer unless both are run.
_NO_NUMPY = '''import builtins, sys
_real = builtins.__import__
def _blocked(name, *a, **k):
    if name == "numpy" or name.startswith("numpy."):
        raise ImportError("numpy disabled")
    return _real(name, *a, **k)
builtins.__import__ = _blocked
sys.argv = sys.argv[1:]
exec(open(%r).read(), {"__name__": "__main__", "__file__": %r})
''' % (TOOL, TOOL)


class Failed(Exception):
    pass


def tool(args, no_numpy=False):
    """Run spritetool, returning (returncode, stdout, stderr)."""
    if no_numpy:
        shim = os.path.join(tempfile.gettempdir(), 'spritetool_nonumpy.py')
        with open(shim, 'w') as fh:
            fh.write(_NO_NUMPY)
        cmd = [sys.executable, shim, TOOL] + args
    else:
        cmd = [sys.executable, TOOL] + args
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def md5(path):
    with open(path, 'rb') as fh:
        return hashlib.md5(fh.read()).hexdigest()


def _tail(text):
    """The line worth reporting from a failed run.

    The tool prints its errors to stdout and its notes to stderr, so the two
    cannot be told apart by stream and the last line of a failed run is usually
    an unrelated note. Prefer a line that announces a failure, and carry the one
    after it, which is where the reason usually sits.
    """
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not lines:
        return 'failed with no output'
    for opener in ('Error:', 'Not packing', 'Could not pack', 'Refusing'):
        for i, line in enumerate(lines):
            if line.lstrip().startswith(opener):
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
                return line.strip() + (f' -- {nxt}' if nxt else '')
    return lines[-1].strip()


def say(ok, what, detail=''):
    print(f'  {"ok  " if ok else "FAIL"}  {what}{"  " + detail if detail else ""}')
    return ok


# ----------------------------------------------------------------- decode --

def check_decode(no_numpy=False):
    """Each game's Water.dir, decompressed and diffed against the committed
    tree. The three are not interchangeable, so all three are checked."""
    good = True
    for game in ('wa', 'wwp online', 'wwp aqua'):
        src = os.path.join(HERE, game, 'Water.dir')
        want = os.path.join(HERE, game, 'decompressed')
        if not os.path.exists(src):
            good = say(False, game, 'fixture missing') and good
            continue
        out = tempfile.mkdtemp(prefix='decode-')
        try:
            rc, _, err = tool(['decompress', src, out], no_numpy)
            if rc:
                good = say(False, game, _tail(err))
                continue
            diff = subprocess.run(['diff', '-r', want, out],
                                  capture_output=True, text=True)
            good = say(diff.returncode == 0, game,
                       '' if diff.returncode == 0
                       else diff.stdout.strip().splitlines()[0]) and good
        finally:
            shutil.rmtree(out, ignore_errors=True)
    return good


# --------------------------------------------------------------- manifest --

def check_manifest(no_numpy=False):
    """Coral Reef described again and diffed against what is committed."""
    committed = os.path.join(HERE, 'wa', 'Coral Reef', 'manifest.txt')
    if not os.path.exists(committed):
        return say(False, 'Coral Reef manifest', 'not committed')
    before = open(committed, encoding='utf-8').read()
    r = subprocess.run([os.path.join(HERE, 'make-manifest.sh')],
                       capture_output=True, text=True, cwd=ROOT)
    after = open(committed, encoding='utf-8').read()
    if before != after:
        with open(committed, 'w', encoding='utf-8') as fh:
            fh.write(before)          # leave the tree as we found it
        return say(False, 'Coral Reef manifest', 'regenerated differently')
    return say(r.returncode == 0, 'Coral Reef manifest')


# ------------------------------------------------------------------- pack --

def _pack_fixture(name, flags, no_numpy=False):
    """Copy a pristine fixture, pack the copy, return (out_dir, tmp, rc, out)."""
    fixture = os.path.join(HERE, 'pack', name)
    tmp = tempfile.mkdtemp(prefix=f'pack-{name}-')
    work = os.path.join(tmp, 'src')
    shutil.copytree(fixture, work)
    out = os.path.join(tmp, 'out')
    rc, stdout, stderr = tool(
        ['pack-terrain', os.path.join(work, 'build'), out] + flags, no_numpy)
    return out, tmp, rc, stdout + stderr


def check_pack(no_numpy=False):
    good = True

    # flat: the art is inside the 112 a terrain may hold, so its own colours
    # are the palette and no quantiser runs. Reproducible, so a hash is fair.
    golden = os.path.join(HERE, 'pack', 'flat', 'expected.txt')
    out, tmp, rc, log = _pack_fixture('flat', ['--defaults'], no_numpy)
    try:
        if rc:
            good = say(False, 'pack flat', _tail(log))
        else:
            got = {n: md5(os.path.join(out, n))
                   for n in sorted(os.listdir(out))}
            want = {}
            if os.path.exists(golden):
                for line in open(golden):
                    if line.strip() and not line.startswith('#'):
                        h, n = line.split(None, 1)
                        want[n.strip()] = h
            if not want:
                good = say(False, 'pack flat', 'no golden; run make-pack-golden.sh')
            elif got != want:
                moved = [n for n in sorted(set(got) | set(want))
                         if got.get(n) != want.get(n)]
                good = say(False, 'pack flat', 'moved: ' + ', '.join(moved[:4]))
            else:
                good = say(True, 'pack flat', f'{len(got)} files') and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # wide: past the budget, so Pillow's median cut decides the palette. That
    # is deterministic for one Pillow but not a promise across versions, so
    # this asserts what must be true rather than an exact archive.
    out, tmp, rc, log = _pack_fixture('wide', ['--defaults', '--repalette'],
                                      no_numpy)
    try:
        if rc:
            good = say(False, 'pack wide', _tail(log))
        else:
            good = _check_wide(out) and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # shortpal: an indexed BMP whose palette holds fewer than 256 colours,
    # packed alongside a PNG so a shared palette is planned and the BMP is
    # fitted to it. That path once scanned range(1, 256) against the short
    # palette and died on an IndexError; this is the regression guard. The
    # assertion is simply that packing succeeds.
    out, tmp, rc, log = _pack_fixture('shortpal', ['--defaults'], no_numpy)
    try:
        if rc:
            good = say(False, 'pack shortpal', _tail(log))
        else:
            good = say(True, 'pack shortpal', 'packed') and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    good = _check_listing_needs_icon(no_numpy) and good
    return good


def _check_listing_needs_icon(no_numpy=False):
    """A folder with a .dir.txt and no icon must be refused, not packed.

    A listing names the archive's entries, and the icon is not one of them --
    it goes beside Level.dir. So the borrow-a-default step that covers a
    scanned folder never runs, and a SpriteEditor-era folder (which keeps its
    icon in the installed terrain, not the build) would otherwise pack all the
    way to the end and mention the missing icon in its closing notes, having
    written an archive the game will not load.
    """
    fixture = os.path.join(HERE, 'pack', 'flat')
    tmp = tempfile.mkdtemp(prefix='pack-listing-')
    try:
        work = os.path.join(tmp, 'src')
        shutil.copytree(fixture, work)
        build = os.path.join(work, 'build')
        names = sorted(f for f in os.listdir(build) if f.endswith('.png'))
        with open(os.path.join(build, 'Level.dir.txt'), 'w',
                  encoding='latin-1', newline='') as fh:
            fh.write(''.join(f'{n[:-4]}.img\r\n' for n in names))

        out = os.path.join(tmp, 'out')
        rc, stdout, stderr = tool(
            ['pack-terrain', build, out, '--defaults'], no_numpy)
        log = stdout + stderr
        if not rc:
            return say(False, 'pack listing without icon', 'packed anyway')
        if 'no icon' not in log:
            return say(False, 'pack listing without icon', _tail(log))
        if os.path.exists(out):
            return say(False, 'pack listing without icon',
                       'refused, but wrote an output folder')
        return say(True, 'pack listing without icon', 'refused')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_padding(no_numpy=False):
    """Compressed art is widened to a multiple of 4, and left alone otherwise.

    The guide asks for both dimensions and blames "a bug in Sprite Editor",
    which binds art going back through that tool rather than the format. Of
    3,120 compressed images across 143 installed terrains none is an odd width
    and 690 are an odd height, so the width half is kept and the height half
    dropped. Padding the height moved art inside its own box -- an object is
    placed by its bottom edge -- which changed where the game spawned it.
    """
    sys.path.insert(0, ROOT)
    import spritetool as st

    good = True
    # Our encoder has no such bug in either dimension, which is what makes the
    # padding deference to SpriteEditor rather than a correctness fix.
    palette = bytes([0, 0, 0] + [(i * 37) % 256 for i in range(45)])
    bad = []
    for w, h in ((104, 98), (105, 98), (117, 50), (65, 33), (338, 7), (8, 3)):
        pixels = bytes((x * 7 + y * 13) % 16 for y in range(h) for x in range(w))
        blob = st.encode_image(w, h, pixels, palette, compress=True)
        back = st.ImageFile(blob)
        if not (back.parse() and (back.width, back.height) == (w, h)
                and bytes(back.pixels) == pixels):
            bad.append(f'{w}x{h}')
    good = say(not bad, 'compresses at any size',
               'exact' if not bad else 'failed: ' + ', '.join(bad)) and good

    w, h, out, grew = st._pad_to_multiple_of_four(104, 98, bytes(104 * 98))
    good = say((w, h, grew) == (104, 98, False), 'odd height left alone',
               f'104x98 -> {w}x{h}') and good

    art = bytes([1] * (5 * 3))
    w, h, out, grew = st._pad_to_multiple_of_four(5, 3, art)
    rows = [list(out[y * w:(y + 1) * w]) for y in range(h)]
    want = [[1, 1, 1, 1, 1, 0, 0, 0]] * 3
    good = say((w, h, grew) == (8, 3, True) and rows == want,
               'odd width widened, art at left', f'5x3 -> {w}x{h}') and good

    w, h, _out, grew = st._pad_to_multiple_of_four(8, 8, bytes(64))
    good = say((w, h, grew) == (8, 8, False), 'already aligned untouched',
               f'8x8 -> {w}x{h}') and good
    return good


def _check_wide(out):
    """Invariants for a terrain the cut had to reduce."""
    sys.path.insert(0, ROOT)
    import spritetool as st

    archive = os.path.join(out, 'Level.dir')
    if not os.path.exists(archive):
        return say(False, 'pack wide', 'no Level.dir')

    dump = tempfile.mkdtemp(prefix='wide-x-')
    try:
        rc, _, err = tool(['extract', archive, dump])
        if rc:
            return say(False, 'pack wide', 'built an archive it cannot read')

        entries, colours = {}, set()
        for root, _dirs, files in os.walk(dump):
            for f in files:
                p = os.path.join(root, f)
                blob = open(p, 'rb').read()
                nm = os.path.basename(p).replace('\\', '/').split('/')[-1]
                entries[nm.lower()] = blob
                if '/' in nm or nm.lower() == 'icon.img':
                    continue          # overrides and the icon are outside it
                ob = (st.ImageFile(blob) if blob[:4] == st.ImageFile.SIGNATURE
                      else st.SpriteFile(blob)
                      if blob[:4] == st.SpriteFile.SIGNATURE else None)
                if ob is not None and ob.parse():
                    colours |= {bytes(ob.palette[i * 3:i * 3 + 3])
                                for i in range(ob.ncolours)}

        want = ('text.img', 'soil.img', 'grass.img', 'gradient.img',
                'bridge.img', 'bridge-l.img', 'bridge-r.img', 'index.txt')
        missing = [n for n in want if n not in entries]
        if missing:
            return say(False, 'pack wide', 'missing ' + ', '.join(missing))
        if len(colours) > st.MAX_SHARED_COLOURS:
            return say(False, 'pack wide',
                       f'{len(colours)} colours, past {st.MAX_SHARED_COLOURS}')
        if not os.path.exists(os.path.join(out, 'TEXT.img')):
            return say(False, 'pack wide', 'no icon beside the archive')
        return say(True, 'pack wide',
                   f'{len(entries)} entries, {len(colours)} colours')
    finally:
        shutil.rmtree(dump, ignore_errors=True)


# ---------------------------------------------------------------- colours --

def check_colours(no_numpy=False):
    """numpy and pure Python must count and cut a palette the same way.

    They do not read pixels in the same order -- numpy returns them sorted,
    the loop returns them first-seen -- and that order feeds Pillow's median
    cut. It turns out not to matter, which is worth continuing to check
    rather than assuming.
    """
    sys.path.insert(0, ROOT)
    import spritetool as st
    try:
        import numpy as _np
    except ImportError:
        return say(True, 'numpy vs pure Python', 'numpy not installed, skipped')

    folder = os.path.join(HERE, 'pack', 'wide', 'build')
    src = {f: os.path.join(folder, f) for f in sorted(os.listdir(folder))
           if f.lower().endswith('.png')}
    if not src:
        return say(False, 'numpy vs pure Python', 'no fixture')

    st.np = _np
    a, _ = st.plan_shared_palette(src)
    st.np = None
    b, _ = st.plan_shared_palette(src)
    st.np = _np
    return say(a == b, 'numpy vs pure Python',
               f'{len(a or [])} colours' if a == b else 'palettes differ')


GROUPS = {'decode': check_decode, 'manifest': check_manifest,
          'pack': check_pack, 'padding': check_padding,
          'colours': check_colours}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('groups', nargs='*', choices=list(GROUPS) + [],
                    help='which checks to run (default: all)')
    ap.add_argument('--no-numpy', action='store_true',
                    help='block the numpy import and use the slow paths')
    a = ap.parse_args()

    wanted = a.groups or list(GROUPS)
    print(f'spritetool fixtures{" (no numpy)" if a.no_numpy else ""}')
    good = True
    for name in wanted:
        print(f'{name}:')
        good = GROUPS[name](a.no_numpy) and good
    print('all good' if good else 'SOMETHING MOVED')
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
