#!/usr/bin/env python3
"""Run every fixture check in one command. Non-zero if anything moved.

    python3 test/run.py
    python3 test/run.py --no-numpy      # the pure-Python paths
    python3 test/run.py decode          # just one group

    python3 test/run.py --all           # including the slow ones

Groups:

  decode    the three Water.dir fixtures, decompressed and diffed byte for byte
  manifest  Coral Reef re-described and diffed against the committed manifest
  pack      a terrain built from source art and compared against a golden
  jobs      every --jobs setting building the same archive
  padding   compressed art widened to a multiple of 4, its height left alone
  colours   numpy and pure-Python agreeing on what they count
  toml      the settings.spritetool.toml model: round-trip, migration, the
            setup prompt, and decompress no longer writing a .dir.txt
  gui       the window builds and its job process imports no Qt (skipped
            without PySide6, which the tool does not depend on)
  nested    the pool-inside-a-pool agreeing with no pool at all. NOT run by
            default: it needs a megabyte of sprite to exercise and takes
            longer than everything else together. Name it, or pass --all.

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
# The tool now imports settings_toml from beside itself; the shim runs from a
# temp dir, so put the repo root back on the path for that import to find.
sys.path.insert(0, %r)
exec(open(%r).read(), {"__name__": "__main__", "__file__": %r})
''' % (ROOT, TOOL, TOOL)


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
    """Copy a pristine fixture, pack the copy, return (out_dir, tmp, rc, out).

    --yes=setup.confirm because the fixtures are pristine inputs with no
    settings.spritetool.toml yet: packing now asks to set the folder up first.
    The answer only writes the TOML marker into the copied source folder -- it
    is not an archive entry, so the packed bytes are untouched by it.
    """
    fixture = os.path.join(HERE, 'pack', name)
    tmp = tempfile.mkdtemp(prefix=f'pack-{name}-')
    work = os.path.join(tmp, 'src')
    shutil.copytree(fixture, work)
    out = os.path.join(tmp, 'out')
    rc, stdout, stderr = tool(
        ['pack-terrain', os.path.join(work, 'build'), out,
         '--yes=setup.confirm'] + flags, no_numpy)
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
    """A folder with a .dir.txt and no icon is offered one, not refused.

    A listing names the archive's entries, and the icon is not one of them --
    it goes beside Level.dir. That is exactly why offering a default is safe
    here: it does not touch the entry set the listing fixes. A SpriteEditor-
    era folder keeps its icon in the installed terrain rather than the build,
    so it reaches us without one, and refusing it outright turns a missing
    piece into a wall. Taking the offer packs; declining still refuses,
    because the game will not load a terrain with no icon.
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

        # Declining the icon still refuses, and writes no archive.
        out = os.path.join(tmp, 'out')
        rc, stdout, stderr = tool(
            ['pack-terrain', build, out, '--yes=setup.confirm',
             '--no=defaults.icon'], no_numpy)
        log = stdout + stderr
        if not rc:
            return say(False, 'pack listing without icon', 'packed anyway')
        if 'icon' not in log:
            return say(False, 'pack listing without icon', _tail(log))
        if os.path.exists(out):
            return say(False, 'pack listing without icon',
                       'refused, but wrote an output folder')

        # Taking the offer packs, and the icon lands beside Level.dir as
        # TEXT.img rather than becoming an entry the listing never named.
        out2 = os.path.join(tmp, 'out2')
        rc2, stdout2, stderr2 = tool(
            ['pack-terrain', build, out2, '--yes=setup.confirm',
             '--yes=defaults.icon'], no_numpy)
        if rc2:
            return say(False, 'pack listing without icon',
                       _tail(stdout2 + stderr2))
        if not os.path.exists(os.path.join(out2, 'Level.dir')):
            return say(False, 'pack listing without icon',
                       'accepted the icon but wrote no Level.dir')
        if not os.path.exists(os.path.join(out2, 'TEXT.img')):
            return say(False, 'pack listing without icon',
                       'no TEXT.img beside the archive')
        return say(True, 'pack listing without icon',
                   'offered: no refuses, yes packs')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_jobs(no_numpy=False):
    """Every parallelism setting must build the same archive.

    Nobody had ever checked this. The pools fall back to serial work inside a
    bare `except Exception`, so a parallel path that produced different bytes
    would look exactly like one that worked, and the difference would only
    show up as a terrain that behaved oddly on someone else's machine.

    This one covers the pool over entries, and is deliberately cheap: the flat
    fixture borrowing only the pieces a terrain cannot go without, which is 16
    entries -- past the threshold for spreading them over processes -- and no
    sprite big enough to be worth a second pool inside one. Under a second for
    all four settings.

    What it therefore does NOT cover is the nested pool, which needs a sprite
    of a megabyte or more and costs about a hundred seconds to exercise. That
    is `nested`, which is not in the default run. See its docstring.
    """
    return _jobs_agree('every --jobs agrees', REQUIRED_ONLY,
                       ('auto', []), ('--jobs=1', ['--jobs=1']),
                       ('--jobs=2', ['--jobs=2']),
                       ('--no-nested-jobs', ['--no-nested-jobs']),
                       no_numpy=no_numpy)


def check_nested(no_numpy=False):
    """The pool inside a pool builds the same archive as no pool at all.

    Left out of the default run because it is the slowest thing here by a wide
    margin -- around 100 seconds against the rest of the suite's 60 -- and it
    cannot be made cheap: the inner pool only starts for a sprite of a
    megabyte or more, so exercising it means compressing one.

    Run it with `python3 test/run.py nested`, or `--all`, and in CI. The risk
    of it not running on every change is real but small: the pools produce the
    same bytes or they do not, and nothing in the packer varies by input in a
    way that would break only the nested path.
    """
    return _jobs_agree('nested and serial agree', ['--defaults'],
                       ('auto', []), ('--jobs=1', ['--jobs=1']),
                       ('--no-nested-jobs', ['--no-nested-jobs']),
                       golden=True, no_numpy=no_numpy)


#: Borrow what a terrain cannot load without, and none of the parallax layers.
#: The layers are the megabyte-scale sprites, and leaving them out is the
#: difference between a second and a minute and a half.
REQUIRED_ONLY = ['--no-defaults'] + [
    f'--yes=defaults.{piece}'
    for piece in ('text', 'gradient', 'soil', 'grass', 'bridge', 'icon')]


def _jobs_agree(label, flags, *settings, golden=False, no_numpy=False):
    """Pack the flat fixture under each setting; every archive must match."""
    want = None
    if golden:
        path = os.path.join(HERE, 'pack', 'flat', 'expected.txt')
        if os.path.exists(path):
            for line in open(path):
                if line.strip() and not line.startswith('#'):
                    h, n = line.split(None, 1)
                    if n.strip() == 'Level.dir':
                        want = h

    hashes = {}
    for name, extra in settings:
        out, tmp, rc, log = _pack_fixture('flat', flags + extra, no_numpy)
        try:
            if rc:
                return say(False, f'pack {name}', _tail(log))
            hashes[name] = md5(os.path.join(out, 'Level.dir'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if len(set(hashes.values())) != 1:
        odd = ', '.join(f'{k}={v[:8]}' for k, v in sorted(hashes.items()))
        return say(False, label, odd)
    got = next(iter(hashes.values()))
    if want and got != want:
        return say(False, label,
                   f'all agree on {got[:8]}, but the golden is {want[:8]}')
    return say(True, label, f'{len(hashes)} settings, {got[:8]}')


def check_gui(no_numpy=False):
    """The window builds, and the job process packs without Qt.

    Skipped when PySide6 is not installed, which is the ordinary case: the
    tool does not depend on it and must keep working without it.
    """
    have = subprocess.run(
        [sys.executable, '-c', 'import PySide6'],
        capture_output=True, cwd=ROOT)
    if have.returncode:
        return say(True, 'gui', 'PySide6 not installed, skipped')

    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    try:
        # Timed, because the failure this guards against is a hang rather than
        # a crash: a modal dialog opened where nobody can answer it waits for
        # ever, and without a deadline the whole run waits with it.
        r = subprocess.run([sys.executable, '-m', 'gui', '--selftest'],
                           capture_output=True, text=True, cwd=ROOT, env=env,
                           timeout=120)
    except subprocess.TimeoutExpired:
        return say(False, 'gui builds',
                   'did not finish in 120s -- a dialog with nobody to answer '
                   'it, or an event loop that was entered')
    if r.returncode or 'selftest ok' not in r.stdout:
        return say(False, 'gui builds', _tail(r.stdout + r.stderr))
    good = say(True, 'gui builds')

    # The child that does the packing must not drag Qt in with it: it runs
    # under spawn, and every import it makes is paid for again in each worker.
    probe = ('import sys, gui.job; '
             'print("qt" if any(m.startswith("PySide6") for m in sys.modules) '
             'else "clean")')
    r = subprocess.run([sys.executable, '-c', probe],
                       capture_output=True, text=True, cwd=ROOT)
    return say(r.stdout.strip() == 'clean', 'job imports no Qt',
               r.stdout.strip() or _tail(r.stderr)) and good


def check_padding(no_numpy=False):
    """Compressed art is widened to a multiple of 4, and left alone otherwise.

    Padding is never free: an object is anchored to a surface by one edge of
    its box, so anything added moves the art relative to that anchor and
    changes where the game places it. Of 3,120 compressed images across 143
    installed terrains none is an odd width and 690 are an odd height, so the
    width is padded and the height is left alone.
    """
    sys.path.insert(0, ROOT)
    import spritetool as st

    good = True
    # The file format is fine at any size -- whatever breaks on an odd width
    # is downstream of it, so this asserts our own end is not the problem.
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


# ------------------------------------------------------------------- toml --

def _pack_flat(out_name, extra, no_numpy=False):
    """Pack the flat fixture into a temp dir; return (level_dir, src, tmp)."""
    fixture = os.path.join(HERE, 'pack', 'flat')
    tmp = tempfile.mkdtemp(prefix='toml-')
    work = os.path.join(tmp, 'src')
    shutil.copytree(fixture, work)
    src = os.path.join(work, 'build')
    out = os.path.join(tmp, out_name)
    rc, stdout, stderr = tool(
        ['pack-terrain', src, out, '--yes=setup.confirm'] + extra, no_numpy)
    return os.path.join(out, 'Level.dir'), src, tmp, rc, stdout + stderr


def check_toml(no_numpy=False):
    """The settings.spritetool.toml model: round-trip, migration, limited
    mode, the setup prompt, and decompress no longer writing a listing.

    Built on the flat fixture rather than Coral Reef: the round-trip through
    a shipped 6 MB terrain takes twenty seconds, which is the difference
    between a suite that gets run and one that does not. flat packs in about
    a second and still holds objects and a borrowed sprite, so every assertion
    the model needs is available from it.
    """
    sys.path.insert(0, ROOT)
    import settings_toml
    import spritetool
    good = True

    # Round-trip: pack flat, unpack the result, and the TOML must hold every
    # object's placement and the sprite's geometry. Packing the unpacked
    # folder again must give the same archive byte for byte -- the pixels are
    # what a BMP round-trip preserves, and idempotence is what proves the
    # settings, not drift, decide them.
    level, _src, tmp, rc, log = _pack_flat('a', ['--defaults'], no_numpy)
    try:
        if rc:
            good = say(False, 'toml round-trip (setup pack)', _tail(log))
        else:
            un = os.path.join(tmp, 'unpacked')
            rc, _, err = tool(['unpack-terrain', level, un], no_numpy)
            if rc:
                good = say(False, 'toml round-trip (unpack)', _tail(err))
            else:
                settled = settings_toml.load(un)
                objects = [f[:-4] for f in os.listdir(
                    os.path.join(HERE, 'pack', 'flat', 'build'))
                    if f.endswith('.png')]
                missing = [o for o in objects
                           if o.lower() not in settled.objects]
                sprites = [n for n in settled.sprites
                           if not n.lower().startswith('gfx')]
                if missing:
                    good = say(False, 'toml round-trip (objects)',
                               'missing ' + ', '.join(missing[:3]))
                elif not sprites:
                    good = say(False, 'toml round-trip (sprites)',
                               'no sprite geometry recorded')
                else:
                    out2 = os.path.join(tmp, 'b')
                    rc, _, err = tool(
                        ['pack-terrain', un, out2, '--no-defaults'], no_numpy)
                    same = (not rc and os.path.exists(out2 + '/Level.dir')
                            and md5(out2 + '/Level.dir') == md5(level))
                    good = say(same, 'toml round-trip',
                               f'{len(settled.objects)} objects, '
                               f'{len(sprites)} sprite(s), repacks '
                               + ('identically' if same else _tail(err)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Migration: a folder with object_settings.txt, a loose .inf and a
    # .spr.spd, packed with --yes=settings.convert_toml, must hold the
    # migrated values in the TOML and pack the same archive as before.
    fixture = os.path.join(HERE, 'pack', 'flat')
    tmp = tempfile.mkdtemp(prefix='toml-mig-')
    try:
        work = os.path.join(tmp, 'src')
        shutil.copytree(fixture, work)
        build = os.path.join(work, 'build')
        # A combined file covering one object, a loose .inf covering another,
        # and a sprite sidecar -- the three legacy sources at once.
        with open(os.path.join(build, 'object_settings.txt'), 'w') as fh:
            fh.write('obj-floor-rock.png\nprobability = 9\nwhere = 3\n')
        with open(os.path.join(build, 'obj-side-vent.inf'), 'w') as fh:
            fh.write('7\n0\n0\n1\n1\n0\n')
        out_a = os.path.join(tmp, 'a')
        flags = ['--yes=setup.confirm', '--yes=settings.convert_toml',
                 '--defaults']
        rc, stdout, stderr = tool(['pack-terrain', build, out_a] + flags,
                                  no_numpy)
        log = stdout + stderr
        settled = settings_toml.load(build)
        rock = settled.objects.get('obj-floor-rock') if settled else None
        vent = settled.objects.get('obj-side-vent') if settled else None
        if rc:
            good = say(False, 'toml migration', _tail(log))
        elif rock is None or rock[0] != 9:
            good = say(False, 'toml migration',
                       f'object_settings.txt value lost: {rock}')
        elif vent is None or vent[0] != 7:
            good = say(False, 'toml migration',
                       f'loose .inf value lost: {vent}')
        elif not os.path.exists(os.path.join(build, 'object_settings.txt')):
            good = say(False, 'toml migration', 'legacy file was deleted')
        else:
            good = say(True, 'toml migration',
                       'object_settings.txt and .inf folded into the TOML, '
                       'legacy files left alone')

        # Clearing the legacy files: --yes=settings.clear_legacy deletes the
        # files the TOML now answers for, and the archive must not move for
        # it. The .spr.spd sidecars go too, which is only safe because the
        # migration copies their geometry into [sprite.*] first -- a sheet
        # says nothing about its own frame count.
        shutil.rmtree(work)
        shutil.copytree(fixture, work)
        with open(os.path.join(build, 'obj-side-vent.inf'), 'w') as fh:
            fh.write('7\n0\n0\n1\n1\n0\n')
        # The fixture ships no sidecars of its own; --defaults copies some in
        # with the borrowed art, and those belong to that art rather than to
        # the author, so they are not the clear-out's business. Give the
        # migration one sidecar that IS the author's, and check that one.
        with open(os.path.join(build, 'terrain.spr.spd'), 'w') as fh:
            fh.write('frames = 2\nwidth = 4\nheight = 8\n'
                     'framerate = 0\nflags = 0\n')
        out_c = os.path.join(tmp, 'c')
        rc, stdout, stderr = tool(
            ['pack-terrain', build, out_c, '--yes=setup.confirm',
             '--yes=settings.convert_toml', '--yes=settings.clear_legacy',
             '--defaults'], no_numpy)
        log = stdout + stderr
        settled = settings_toml.load(build)
        # Only what the author had: the presets' own sidecars arrive later
        # and stay, so they are not counted as left behind.
        left = [f for f in os.listdir(build)
                if f in ('obj-side-vent.inf', 'terrain.spr.spd',
                         'object_settings.txt')]
        if rc:
            good = say(False, 'toml clear legacy', _tail(log))
        elif left:
            good = say(False, 'toml clear legacy',
                       f'still there: {", ".join(sorted(left)[:3])}')
        elif settled is None or settled.objects.get('obj-side-vent',
                                                    [0])[0] != 7:
            good = say(False, 'toml clear legacy', 'settings lost with the files')
        elif settled.sprites.get('terrain', {}).get('frames') != 2:
            good = say(False, 'toml clear legacy',
                       f'sidecar deleted without recording its geometry: '
                       f'{settled.sprites.get("terrain")}')
        else:
            # The proof it was safe: the folder still packs with none of the
            # files it just lost, and lands the same archive.
            out_d = os.path.join(tmp, 'd')
            rc2, _, err2 = tool(['pack-terrain', build, out_d], no_numpy)
            same = (not rc2
                    and os.path.exists(os.path.join(out_d, 'Level.dir'))
                    and md5(os.path.join(out_d, 'Level.dir'))
                    == md5(os.path.join(out_c, 'Level.dir')))
            good = say(same, 'toml clear legacy',
                       'sidecar and .inf cleared, geometry kept, repacks '
                       'identically' if same else _tail(err2)) and good

        # The listing/index pair and the built pictures go under their own
        # questions, separately from the settings. A folder stripped of all
        # three must still pack: the entries come from the .bmp sheets, the
        # geometry from the TOML, and index.txt is generated. gfx0 matters
        # here -- its sprites keep their geometry in sidecars the top-level
        # scan never sees, so a migration that missed them would delete the
        # frame counts and quietly drop 450 entries.
        shutil.rmtree(work)
        shutil.copytree(fixture, work)
        os.makedirs(os.path.join(build, 'gfx0'), exist_ok=True)
        sheet = next(f for f in os.listdir(build) if f.endswith('.png'))
        shutil.copyfile(os.path.join(build, sheet),
                        os.path.join(build, 'gfx0', 'cloudm.spr.png'))
        from PIL import Image
        with Image.open(os.path.join(build, 'gfx0', 'cloudm.spr.png')) as im:
            _w, _h = im.size
        with open(os.path.join(build, 'gfx0', 'cloudm.spr.spd'), 'w') as fh:
            fh.write(f'frames = 1\nwidth = {_w}\nheight = {_h}\n'
                     f'framerate = 0\nflags = 0\n')
        _names = sorted(f for f in os.listdir(build) if f.endswith('.png'))
        with open(os.path.join(build, 'Level.dir.txt'), 'w',
                  encoding='latin-1', newline='') as fh:
            fh.write(''.join(f'{n[:-4]}.img\r\n' for n in _names))
        out_e = os.path.join(tmp, 'e')
        rc, stdout, stderr = tool(
            ['pack-terrain', build, out_e, '--yes=setup.confirm',
             '--yes=settings.convert_toml', '--yes=settings.clear_legacy',
             '--yes=archive.clear_listing', '--yes=archive.clear_built',
             '--yes=settings.convert_listing', '--defaults'], no_numpy)
        log = stdout + stderr
        settled = settings_toml.load(build)
        borrowed = {'_back.spr.spd', 'back2.spr.spd', 'debris.spr.spd',
                    'front.spr.spd'}
        left = [f for f in os.listdir(build)
                if (f.endswith(('.spr', '.img', '.spd', '.inf'))
                    or f in ('index.txt', 'Level.dir.txt'))
                and f not in borrowed]
        sub_left = [f for f in os.listdir(os.path.join(build, 'gfx0'))
                    if f.endswith(('.spr', '.spd'))]
        if rc:
            good = say(False, 'toml clear build files', _tail(log))
        elif left or sub_left:
            good = say(False, 'toml clear build files',
                       f'still there: {(left + sub_left)[:3]}')
        elif settled is None or 'gfx0\\cloudm' not in settled.sprites:
            good = say(False, 'toml clear build files',
                       f'gfx0 geometry not migrated: '
                       f'{sorted(settled.sprites) if settled else None}')
        else:
            # It has to still build with none of what was removed.
            out_f = os.path.join(tmp, 'f')
            rc2, _, err2 = tool(['pack-terrain', build, out_f], no_numpy)
            ok = not rc2 and os.path.exists(os.path.join(out_f, 'Level.dir'))
            good = say(ok, 'toml clear build files',
                       'listing, index and built pictures cleared, gfx0 '
                       'geometry kept, still packs' if ok
                       else _tail(err2)) and good

        # Limited mode: --no=settings.convert_toml still packs, writes no TOML.
        shutil.rmtree(work)
        shutil.copytree(fixture, work)
        with open(os.path.join(build, 'object_settings.txt'), 'w') as fh:
            fh.write('obj-floor-rock.png\nprobability = 9\n')
        out_b = os.path.join(tmp, 'b')
        rc, stdout, stderr = tool(
            ['pack-terrain', build, out_b, '--yes=setup.confirm',
             '--no=settings.convert_toml', '--defaults'], no_numpy)
        toml_path = os.path.join(build, settings_toml.SETTINGS_TOML_NAME)
        # The setup marker is a TOML, but it must hold no object tables.
        settled = settings_toml.load(build)
        if rc:
            good = say(False, 'toml limited mode', _tail(stdout + stderr))
        elif settled and settled.objects:
            good = say(False, 'toml limited mode',
                       'wrote object settings despite the decline')
        else:
            good = say(True, 'toml limited mode',
                       'packs, no object settings written')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The setup prompt: --no=setup.confirm stops, --yes packs regardless of
    # the folder's name (it no longer has to be called build).
    tmp = tempfile.mkdtemp(prefix='toml-setup-')
    try:
        work = os.path.join(tmp, 'mypics')        # not "build", on purpose
        shutil.copytree(fixture, work)
        pics = os.path.join(work, 'mypics')
        os.rename(os.path.join(work, 'build'), pics)
        rc, _, _ = tool(['pack-terrain', pics, os.path.join(tmp, 'o1'),
                         '--no=setup.confirm', '--defaults'], no_numpy)
        stopped = rc != 0
        rc2, _, _ = tool(['pack-terrain', pics, os.path.join(tmp, 'o2'),
                          '--yes=setup.confirm', '--defaults'], no_numpy)
        packed = rc2 == 0
        good = say(stopped and packed, 'setup prompt',
                   '--no stops, --yes packs a folder not called build'
                   if stopped and packed else
                   f'no->rc{rc}, yes->rc{rc2}') and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # decompress writes no <name>.dir.txt, but a terrain's index.txt (a real
    # archive entry) still comes through, and the folder still packs back.
    tmp = tempfile.mkdtemp(prefix='toml-dec-')
    try:
        level, _s, packtmp, rc, log = _pack_flat('d', ['--defaults'], no_numpy)
        try:
            out = os.path.join(tmp, 'dec')
            rc, _, err = tool(['decompress', level, out], no_numpy)
            folder = os.path.join(out, 'Level')
            listing = os.path.join(folder, 'Level.dir.txt')
            index = os.path.join(folder, 'index.txt')
            if rc:
                good = say(False, 'decompress listing gone', _tail(err))
            elif os.path.exists(listing):
                good = say(False, 'decompress listing gone',
                           'Level.dir.txt still written')
            elif not os.path.exists(index):
                good = say(False, 'decompress listing gone',
                           'index.txt did not come through')
            else:
                rc2, _, err2 = tool(
                    ['pack', folder, os.path.join(tmp, 're')], no_numpy)
                good = say(rc2 == 0, 'decompress listing gone',
                           'no .dir.txt, index.txt kept, folder repacks'
                           if rc2 == 0 else _tail(err2))

            # decompress unpacks the archive it was given and nothing that
            # sits beside it. A terrain's icon is a loose TEXT.img next to
            # Level.dir, so writing it here would put a picture in the output
            # that no entry accounts for -- and it would be counted against
            # the 112-colour budget by anyone measuring the folder. Carrying
            # the icon is unpack-terrain's job, and it still does.
            icon = os.path.join(folder, 'icon.img.bmp')
            sibling = os.path.join(os.path.dirname(level), 'TEXT.img')
            if not os.path.exists(sibling):
                good = say(False, 'decompress leaves the icon alone',
                           'fixture wrote no TEXT.img to leave alone')
            elif os.path.exists(icon):
                good = say(False, 'decompress leaves the icon alone',
                           'wrote icon.img.bmp from beside the archive')
            else:
                un = os.path.join(tmp, 'un')
                rc3, _, err3 = tool(['unpack-terrain', level, un], no_numpy)
                kept = os.path.exists(os.path.join(un, 'icon.img.bmp'))
                good = say(not rc3 and kept,
                           'decompress leaves the icon alone',
                           'no icon from decompress, unpack-terrain keeps it'
                           if not rc3 and kept
                           else (_tail(err3) if rc3
                                 else 'unpack-terrain lost the icon')) and good
        finally:
            shutil.rmtree(packtmp, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # A brand-new folder must still be able to reach the shipped defaults.
    # The window marked a folder settled as soon as its setup box was
    # answered, and the defaults offer skips a settled folder -- so every new
    # folder was marked before being asked and the presets became
    # unreachable. setup_terrain is what marks it, after the offer, and it
    # leaves the folder unmarked when the offer is refused so it stands next
    # time. Checked through the functions the window calls, so this runs
    # whether or not PySide6 is installed.
    tmp = tempfile.mkdtemp(prefix='toml-offer-')
    try:
        bare = os.path.join(tmp, 'bare')
        os.makedirs(bare)
        shutil.copyfile(
            os.path.join(HERE, 'pack', 'flat', 'build',
                         sorted(f for f in os.listdir(
                             os.path.join(HERE, 'pack', 'flat', 'build'))
                             if f.endswith('.png'))[0]),
            os.path.join(bare, 'obj-only.png'))
        # setup_terrain narrates each piece it offers; that is right on a
        # command line and noise in a fixture, so it is kept out of the way.
        import contextlib, io
        quiet = contextlib.redirect_stdout(io.StringIO())
        if spritetool.folder_settled(bare):
            good = say(False, 'new folder reaches the defaults',
                       'a fresh folder reported itself settled')
        else:
            # Refusing leaves it unmarked, so the offer stands next time.
            with quiet:
                _b, refused = spritetool.setup_terrain(
                    bare, spritetool.answer_with({'defaults.': False}))
            if not refused:
                good = say(False, 'new folder reaches the defaults',
                           'declining the defaults did not stop the setup')
            elif spritetool.folder_settled(bare):
                good = say(False, 'new folder reaches the defaults',
                           'marked settled after a refusal, so the offer '
                           'would never come back')
            else:
                # Accepting copies the art in and marks it, once.
                answers = {f'defaults.{p.split(".")[0]}': True
                           for p in spritetool.REQUIRED_ASSETS}
                with contextlib.redirect_stdout(io.StringIO()):
                    got, refused2 = spritetool.setup_terrain(
                        bare, spritetool.answer_with(answers))
                settled = spritetool.folder_settled(bare)
                good = say(bool(got) and not refused2 and settled,
                           'new folder reaches the defaults',
                           f'{len(got)} piece(s) borrowed, then settled'
                           if got and not refused2 and settled
                           else f'borrowed={len(got)} refused={refused2!r} '
                                f'settled={settled}') and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The sprite record: a sheet says nothing about how it is cut up, so a
    # wrong frame count is not a crash but art sliced in the wrong places.
    # sprite_records pairs each record with its sheet and says what does not
    # add up, for the window to show and for setup to warn about -- all of
    # them, where the packer refuses on the first one it reaches.
    tmp = tempfile.mkdtemp(prefix='toml-spr-')
    try:
        work = os.path.join(tmp, 'src')
        shutil.copytree(fixture, work)
        build = os.path.join(work, 'build')
        os.makedirs(os.path.join(build, 'gfx0'), exist_ok=True)
        sheet = sorted(f for f in os.listdir(build) if f.endswith('.png'))[0]
        from PIL import Image
        with Image.open(os.path.join(build, sheet)) as im:
            _w, _h = im.size
        # One sprite whose record fits its sheet, and one that does not.
        for name, frames in (('good.spr.png', 1), ('bad.spr.png', 7)):
            shutil.copyfile(os.path.join(build, sheet),
                            os.path.join(build, name))
        settled = settings_toml.TerrainSettings()
        settled.sprites['good'] = {'frames': 1, 'width': _w, 'height': _h,
                                   'framerate': 0, 'flags': 0}
        settled.sprites['bad'] = {'frames': 7, 'width': _w, 'height': _h,
                                  'framerate': 0, 'flags': 0}
        # A gfx0 override carrying its record in a sidecar, as a legacy
        # folder does -- it must be found in the subfolder, not just at top.
        shutil.copyfile(os.path.join(build, sheet),
                        os.path.join(build, 'gfx0', 'cloudm.spr.png'))
        with open(os.path.join(build, 'gfx0', 'cloudm.spr.spd'), 'w') as fh:
            fh.write(f'frames = 1\nwidth = {_w}\nheight = {_h}\n'
                     f'framerate = 0\nflags = 0\n')
        settings_toml.save(build, settled)

        rows = {r['name']: r for r in spritetool.sprite_records(build)}
        fits = rows.get('good')
        bad = rows.get('bad')
        over = rows.get('gfx0\\cloudm')
        if fits is None or bad is None or over is None:
            say(False, 'sprite records',
                f'found {sorted(rows)}')
            good_ok = False
        elif fits['problem']:
            say(False, 'sprite records',
                f"a matching record was called wrong: {fits['problem']}")
            good_ok = False
        elif not bad['problem']:
            say(False, 'sprite records', 'a wrong frame count went unnoticed')
            good_ok = False
        elif over['problem'] or over['source'] != 'cloudm.spr.spd':
            say(False, 'sprite records',
                f"gfx0 sidecar not read: {over['source']!r} "
                f"{over['problem']!r}")
            good_ok = False
        elif fits['source'] != settings_toml.SETTINGS_TOML_NAME:
            say(False, 'sprite records',
                f"record should come from the TOML, got {fits['source']!r}")
            good_ok = False
        else:
            good_ok = say(True, 'sprite records',
                          f'{len(rows)} sprite(s), the wrong one named, '
                          f'gfx0 sidecar read')
        good = good_ok and good
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # An unanswered question must never block. stdin here is an open pipe that
    # nobody writes to -- a build server, or any run whose input is a pipe --
    # which never reaches EOF, so a question that waits on it waits for ever.
    # The listing prompt sits on the ordinary pack-terrain path, so this is a
    # hung build rather than an edge case. Closed stdin is the easy half; the
    # silent pipe is the one that caught us.
    tmp = tempfile.mkdtemp(prefix='toml-tty-')
    try:
        work = os.path.join(tmp, 'src')
        shutil.copytree(fixture, work)
        build = os.path.join(work, 'build')
        names = sorted(f for f in os.listdir(build) if f.endswith('.png'))
        with open(os.path.join(build, 'Level.dir.txt'), 'w',
                  encoding='latin-1', newline='') as fh:
            fh.write(''.join(f'{n[:-4]}.img\r\n' for n in names))
        read_end, write_end = os.pipe()
        try:
            cmd = [sys.executable, TOOL, 'pack-terrain', build,
                   os.path.join(tmp, 'out'), '--defaults',
                   '--yes=setup.confirm']
            try:
                done = subprocess.run(cmd, stdin=read_end, capture_output=True,
                                      text=True, cwd=ROOT, timeout=60)
            except subprocess.TimeoutExpired:
                good = say(False, 'no question blocks on a silent stdin',
                           'hung waiting for an answer that cannot arrive')
            else:
                log = done.stdout + done.stderr
                good = say('not a terminal' in log,
                           'no question blocks on a silent stdin',
                           'took the default and said so'
                           if 'not a terminal' in log else _tail(log)) and good
        finally:
            os.close(read_end)
            os.close(write_end)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return good


GROUPS = {'decode': check_decode, 'manifest': check_manifest,
          'pack': check_pack, 'jobs': check_jobs, 'nested': check_nested,
          'padding': check_padding, 'colours': check_colours,
          'toml': check_toml, 'gui': check_gui}

#: Left out unless named or --all is given. Slow enough to change how often
#: the suite gets run, which is its own kind of risk.
SLOW = ('nested',)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('groups', nargs='*', choices=list(GROUPS) + [],
                    help=f'which checks to run (default: all but '
                         f'{", ".join(SLOW)})')
    ap.add_argument('--no-numpy', action='store_true',
                    help='block the numpy import and use the slow paths')
    ap.add_argument('--all', action='store_true',
                    help=f'include the slow ones ({", ".join(SLOW)})')
    a = ap.parse_args()

    if a.groups:
        wanted = a.groups          # named outright, however slow
    elif a.all:
        wanted = list(GROUPS)
    else:
        wanted = [g for g in GROUPS if g not in SLOW]
    print(f'spritetool fixtures{" (no numpy)" if a.no_numpy else ""}')
    good = True
    for name in wanted:
        print(f'{name}:')
        good = GROUPS[name](a.no_numpy) and good
    skipped = [g for g in SLOW if g not in wanted]
    if skipped:
        print(f'({", ".join(skipped)} not run; --all includes them)')
    print('all good' if good else 'SOMETHING MOVED')
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
