#!/usr/bin/env python3
"""Read and write W:A .spr sprite files directly, without SpriteEditor.

The format has no public specification; this was worked out from the game's
own files. A sprite is a header, a palette, then the frames.

Palettes for the gfx0/gfx1 slots come from colors.py rather than from an .ACT
on disk -- the game paints those slots from its own fixed table, so the art
has to point at the right entries before it ever reaches the archive.

Scope, worth knowing before reaching for read_spr: this handles the plain
uncompressed form, whole frames laid out end to end. It does NOT read a
sprite whose frames are cropped to their ink, which most of the game's own
are -- 39 of 40 in Gfx0.dir fail here, and did before this file was split out
of make_spr.py. spritetool.py reads those. This is for writing sprites we
generate, which is what it has always been used for.
"""

import os
import struct
import sys

try:
    from PIL import Image
except ImportError:                              # pragma: no cover
    sys.exit("this needs Pillow: pip install pillow")

import colors


def fixed_palette(gfx):
    """The 91-entry palette for 'gfx0' or 'gfx1'.

    Index 0 is transparency and is not one of the table's colours, so it is
    prepended here: entry 1 of this list is colors.GFX0[0].
    """
    table = colors.PALETTES[gfx]
    return [(0, 0, 0)] + [tuple(c) for c in table] + [(0, 0, 0)]


MAGIC = b"SPR\x1a"
FLAGS_RAW = 0x8008
MID_HEADER_LEN = 21



def remap_to_fixed(im, gfx, transparent_idx=0):
    """Repoint every pixel of a P-mode image at the nearest colour in the
    FIXED palette for `gfx`, returning a new P-mode image carrying that
    palette.

    This is the operation that actually matters for gfx0/gfx1 overrides: the
    game ignores the palette we ship and reads indices through its own table,
    so the pixels must already point at the right entries.

    Transparency is INDEX 0 -- and only index 0. Every other index is real
    artwork and is remapped into 1..90, so an opaque pixel can never become
    transparent.

    This used to test the palette VALUE instead ("is this entry pure black?"),
    which silently deleted artwork: the bazooka's dark shaft is a genuine
    (0,0,0) entry at index 9, so ~840 px per sprite collapsed onto index 0 and
    the weapon turned see-through. A sprite may legitimately contain black
    that is not the transparency slot.
    """
    if im.mode != "P":
        raise ValueError(f"expected P-mode image, got {im.mode}")
    pal = fixed_palette(gfx)
    src = im.getpalette()

    # nearest-entry lookup per SOURCE index (at most 256), not per pixel
    lut = []
    for i in range(256):
        c = tuple(src[3 * i:3 * i + 3]) if 3 * i + 3 <= len(src) else (0, 0, 0)
        if i == transparent_idx:
            lut.append(transparent_idx)
            continue
        best, best_d = 1, None
        for k in range(1, 91):
            p = pal[k]
            d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2
            if best_d is None or d < best_d:
                best, best_d = k, d
        lut.append(best)

    out = im.point(lut)
    flat = [v for rgb in pal for v in rgb]
    out.putpalette(flat + [0] * (768 - len(flat)))
    return out


def _read_spd(spd_path):
    """Parse a .spd parameter file -> dict of ints."""
    txt = open(spd_path).read()
    return {k: int(v) for k, v in re.findall(r"(\w+)\s*=\s*(\d+)", txt)}


def write_spr(bmp_path, spd_path, out_path, gfx=None):
    """Write an uncompressed .spr from a P-mode BMP + its .spd.

    The BMP holds every frame stacked vertically (height = frames * frame_h),
    exactly as SpriteEditor expects.

    If `gfx` is "gfx0" or "gfx1", the image is first REMAPPED onto that set's
    fixed palette (see remap_to_fixed). This is required for sprite overrides:
    the game ignores the palette in the file and reads indices through its own
    table, so the indices must already be correct. Without it the sprite
    renders as unrelated colours.
    """
    im = Image.open(bmp_path)
    im.load()
    if im.mode != "P":
        raise ValueError(f"{bmp_path}: expected P-mode (indexed) image, got {im.mode}")
    if gfx:
        im = remap_to_fixed(im, gfx)

    spd = _read_spd(spd_path)
    frames = spd["frames"]
    fw, fh = spd["width"], spd["height"]

    w, h = im.size
    if w != fw:
        raise ValueError(f"{bmp_path}: bmp width {w} != spd width {fw}")
    if h != frames * fh:
        raise ValueError(
            f"{bmp_path}: bmp height {h} != frames*height {frames}*{fh}={frames*fh}")

    # palette: only the entries actually referenced, in index order, so the
    # count matches what the header declares
    pal = im.getpalette()
    used = {idx for _, idx in im.getcolors(w * h)}
    ncolours = max(used) + 1
    pal_bytes = bytes(pal[: ncolours * 3])

    mid = struct.pack("<BHHHH", 0, 1, fw, fh, frames)
    mid += struct.pack("<HHHHHH", 0, 0, 0, 0, fw, fh)
    assert len(mid) == MID_HEADER_LEN, len(mid)

    pixels = im.tobytes()          # top-down, no row padding for 8bpp
    assert len(pixels) == w * h, (len(pixels), w * h)

    body = pal_bytes + mid + pixels
    total = 12 + len(body)
    out = MAGIC + struct.pack("<IHH", total, FLAGS_RAW, ncolours - 1) + body

    with open(out_path, "wb") as f:
        f.write(out)
    return len(out)


def read_spr(path):
    """Decode an uncompressed .spr -> (PIL P-mode image, spd dict).

    Verification helper. Raises if the file is compressed (i.e. the pixel data
    is shorter than frames*w*h), which is how the game's own worm sprites are
    stored -- see the module docstring.
    """
    d = open(path, "rb").read()
    if d[:4] != MAGIC:
        raise ValueError(f"{path}: bad magic {d[:4]!r}")
    total, flags, ncol_m1 = struct.unpack("<IHH", d[4:12])
    ncolours = ncol_m1 + 1

    pal_end = 12 + ncolours * 3
    pal = list(d[12:pal_end])

    mid = d[pal_end:pal_end + MID_HEADER_LEN]
    _, _, fw, fh, frames = struct.unpack("<BHHHH", mid[:9])

    pixels = d[pal_end + MID_HEADER_LEN:]
    need = frames * fw * fh
    if len(pixels) < need:
        raise ValueError(
            f"{path}: only {len(pixels)} pixel bytes but {frames}x{fw}x{fh}"
            f"={need} expected -- this file is COMPRESSED, not raw")

    im = Image.frombytes("P", (fw, frames * fh), pixels[:need])
    im.putpalette(pal + [0] * (768 - len(pal)))
    return im, {"frames": frames, "width": fw, "height": fh}


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4:
        n = write_spr(sys.argv[1], sys.argv[2], sys.argv[3])
        print(f"wrote {sys.argv[3]} ({n} bytes)")
    elif len(sys.argv) == 2:
        im, spd = read_spr(sys.argv[1])
        print(f"{sys.argv[1]}: {spd}, image {im.size}")
    else:
        print(__doc__)
        print("usage:\n  make_spr.py <in.bmp> <in.spd> <out.spr>\n"
              "  make_spr.py <in.spr>          (decode/inspect)")


# ---------------------------------------------------------------------------
# In-place recolour of an existing game .spr
# ---------------------------------------------------------------------------
# Writing a .spr from scratch requires reproducing the per-frame table, whose
# layout is only partly understood: frame data decodes correctly (frame 0 of
# wwalk is a clean worm at stride 20 x 27 rows = its exact 540-byte span), but
# the record's first two fields (19,16) do not match those dims and no
# consistent formula was found across all 15 frames. Writing full 60x60 frames
# with no table made the game misread frame boundaries -- worms rendered only
# ~7% of the time (the "flicker").
#
# This sidesteps the table entirely: keep the ORIGINAL file byte-for-byte --
# header, palette, frame table, trimmed layout, everything -- and substitute
# only pixel VALUES through an index->index map. Valid because we change
# colours, never shapes, so every frame's geometry is already correct.

def recolor_spr_inplace(src_path, out_path, index_map):
    """Copy a .spr, rewriting only its pixel bytes via `index_map`
    (a dict {old_index: new_index}). Header, palette and frame table are
    preserved exactly."""
    d = bytearray(open(src_path, "rb").read())
    if bytes(d[:4]) != MAGIC:
        raise ValueError(f"{src_path}: not a .spr")
    ncolours = struct.unpack("<H", d[10:12])[0] + 1
    pix_start = 12 + ncolours * 3 + MID_HEADER_LEN

    # The frame table sits between the mid header and the pixels; its records
    # are 12 bytes each and the frame count lives at mid-header offset 7.
    mid = 12 + ncolours * 3
    frames = struct.unpack("<H", d[mid + 7:mid + 9])[0]
    table_end = mid + 14 + frames * 12

    lut = bytes(index_map.get(i, i) for i in range(256))
    for i in range(table_end, len(d)):
        d[i] = lut[d[i]]

    with open(out_path, "wb") as f:
        f.write(bytes(d))
    return len(d)


def skin_index_map(spr_path, is_skin_fn, recolor_fn):
    """Build {old_index: new_index} for a .spr: every palette entry that
    `is_skin_fn` accepts is repointed at whichever existing entry is closest
    to `recolor_fn` of it. Only indices already in the file are used, so the
    palette itself never changes."""
    d = open(spr_path, "rb").read()
    ncolours = struct.unpack("<H", d[10:12])[0] + 1
    pal = [tuple(d[12 + k * 3:12 + k * 3 + 3]) for k in range(ncolours)]
    out = {}
    for i, c in enumerate(pal):
        if not is_skin_fn(*c):
            continue
        tgt = recolor_fn(*c)
        best = min(range(ncolours),
                   key=lambda k: sum((a - b) ** 2 for a, b in zip(pal[k], tgt)))
        if best != i:
            out[i] = best
    return out
