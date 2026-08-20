#!/usr/bin/env python3
"""Generate grass_src.png in the standard W:A grass format.

Layout (136 x H): [ floor(66) | 2px gap | ceiling(66) | marker(2) ]
 - floor   : the sea-urchin mould shapes (mould1/mould2, hand-cut from plate
             0155) rotated to STAND UP on a seamless base band of the
             urchin-shell mesh texture
 - ceiling : the base texture ONLY (no moulds), recoloured purple, hangs down
 - marker  : 2px solid colour column (water tint; left blue for now)

The floor is composed so it tiles horizontally: the base band is the seamless
mesh texture and the moulds are placed within one tile width. Floor colouring
is warm and ~50% brighter than the old dark pass so it reads against the black
background; the ceiling is recoloured toward purple.

Output: grass_src.png (RGBA). build_terrain.py resizes to GRASS_W x GRASS_H and
quantises into the shared palette (grass pixels are weighted there so its
distinct colours survive median-cut against the sepia objects).
"""
import os
from PIL import Image, ImageEnhance, ImageChops, ImageFilter

# ---- project location (portable; see build_terrain.py) ----
HERE    = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(HERE, "Distant Planet")
GRASS   = os.path.join(PROJECT, "Grass")

OUTLINE       = (150, 80, 35)   # same dark sepia outline the objects use
OUTLINE_ALPHA = 255
OUTLINE_W     = 3               # ring MaxFilter kernel (3 => ~1px at grass scale)

MOULD1  = os.path.join(GRASS, "mould1.png")     # domed tubercle
MOULD2  = os.path.join(GRASS, "mould2.png")     # ribbed spine
TEXTURE = os.path.join(GRASS, "mould_tex.png")  # urchin mesh
OUT          = os.path.join(GRASS, "grass_src.png")          # grassy (with moulds)
OUT_NOMOULD  = os.path.join(GRASS, "grass_src_nomould.png")  # non-grassy

# Standard W:A grass layout (matched to the Trippy reference, 136 wide):
#   floor   = x0..63   (64 px)
#   ceiling = x64..127 (64 px)
#   marker  = x128..135 (8-col solid water-colour block)  <- must stay solid,
#             the game reads it as the water tint; content must NOT spill in.
W_HALF   = 64
H        = 31
MARKER_X = 128             # marker block starts here (8 cols wide)
MARKER   = (50, 90, 210)   # water-tint colour (blue for now)

BASE_ROWS   = 8            # rows of mesh base band along the bottom
MOULD_H     = 24           # scaled height of a standing mould
MOULD_ROT   = 270          # degrees CCW (=90 CW) so the domed head points up
MOULDS      = [(MOULD1, 8), (MOULD2, 40)]   # (file, x position on the tile)

# floor colour: warm, ~50% brighter than the old dark pass
FLOOR_BRIGHT = 1.1
FLOOR_TINT   = (150, 100, 116)
# ceiling colour: recolour toward purple
CEIL_BRIGHT  = 1.0
CEIL_TINT_RGB = (1.05, 0.7, 1.25)   # per-channel gain -> purple


def tint_to(im_rgb, target):
    from PIL import ImageStat
    cur = ImageStat.Stat(im_rgb).mean
    return Image.merge("RGB", [c.point(lambda v, f=t/max(m, 1): min(255, int(v*f)))
                               for c, m, t in zip(im_rgb.split(), cur, target)])


def seamless_band(rows):
    """A W_HALF x rows RGB band of the mesh texture that tiles horizontally.

    The mesh is resized to the full half-width and made seamless with an
    offset-blend (roll by half, feather the central seam) so the wrap has no
    hard edge.
    """
    tex = Image.open(TEXTURE).convert("RGB").resize((W_HALF, rows))
    rolled = ImageChops.offset(tex, W_HALF // 2, 0)
    mask = Image.new("L", (W_HALF, rows), 0)
    mp = mask.load()
    band_w = max(4, W_HALF // 6)
    for x in range(W_HALF):
        d = abs(x - W_HALF // 2)
        v = int(255 * max(0, (band_w - d)) / band_w) if d < band_w else 0
        for y in range(rows):
            mp[x, y] = v
    return Image.composite(Image.blend(rolled, tex, 0.5), tex, mask)


SS = 4   # supersample factor for anti-aliasing the moulds + outline


def prep_mould(path, target_h):
    """Load, rotate, and scale a mould to target_h with a hugging outline and
    anti-aliased edges. Processed at SS x then downscaled once for AA."""
    m = Image.open(path).convert("RGBA").rotate(MOULD_ROT, expand=True,
                                                resample=Image.BICUBIC)
    b = m.getchannel("A").getbbox()
    if b:
        m = m.crop(b)
    # work large: scale to SS x the final height
    hi_h = target_h * SS
    s = hi_h / m.height
    m = m.resize((max(1, int(m.width*s)), hi_h), Image.LANCZOS)

    ow = OUTLINE_W * SS         # outline thickness at working scale
    pad = ow + SS
    p = Image.new("RGBA", (m.width + 2*pad, m.height + 2*pad), (0, 0, 0, 0))
    p.paste(m, (pad, pad))
    # Fill from the mould's own low-threshold silhouette so the ring hugs the
    # true (soft) edge. The ring = (silhouette grown by ow) minus silhouette;
    # then composite the mould on top. Both derive from the SAME silhouette so
    # there is no transparent gap between mould and ring.
    a = p.getchannel("A")
    sil = a.point(lambda v: 255 if v >= 40 else 0)
    grown = sil.filter(ImageFilter.MaxFilter(ow + 1))
    ring = Image.new("RGBA", p.size, OUTLINE + (OUTLINE_ALPHA,))
    outlined = Image.new("RGBA", p.size, (0, 0, 0, 0))
    outlined.paste(ring, (0, 0), grown)     # solid ring under everything
    outlined.alpha_composite(p)             # mould covers the inner ring exactly
    # downscale once -> anti-aliased mould + outline
    fh = target_h + 2*OUTLINE_W
    fw = max(1, round(outlined.width * fh / outlined.height))
    outlined = outlined.resize((fw, fh), Image.LANCZOS)
    bb = outlined.getchannel("A").getbbox()
    return outlined.crop(bb) if bb else outlined


def make_floor(with_moulds=True):
    """Floor half: seamless mesh base band + standing (rotated) outlined moulds.

    with_moulds=False -> just the base band (the Non-Grassy variant's floor)."""
    band = seamless_band(BASE_ROWS)
    floor = Image.new("RGBA", (W_HALF, H), (0, 0, 0, 0))
    floor.paste(band, (0, H - BASE_ROWS))
    if with_moulds:
        for path, mx in MOULDS:
            m = prep_mould(path, MOULD_H)
            my = H - BASE_ROWS - m.height + 3      # base sinks into the band
            floor.alpha_composite(m, (mx, max(0, my)))
    rgb = tint_to(floor.convert("RGB"), FLOOR_TINT)
    rgb = ImageEnhance.Brightness(rgb).enhance(FLOOR_BRIGHT)
    out = rgb.convert("RGBA")
    out.putalpha(floor.getchannel("A"))
    return out


def make_ceiling():
    """Ceiling half: the base texture ONLY (no moulds), purple, hanging down."""
    band = seamless_band(BASE_ROWS)
    ceil = Image.new("RGBA", (W_HALF, H), (0, 0, 0, 0))
    ceil.paste(band, (0, H - BASE_ROWS))           # build at bottom, flip later
    r, g, b = ceil.convert("RGB").split()
    fr, fg, fb = CEIL_TINT_RGB
    rgb = Image.merge("RGB", (r.point(lambda v: min(255, int(v*fr))),
                              g.point(lambda v: int(v*fg)),
                              b.point(lambda v: min(255, int(v*fb)))))
    rgb = ImageEnhance.Brightness(rgb).enhance(CEIL_BRIGHT)
    out = rgb.convert("RGBA")
    out.putalpha(ceil.getchannel("A"))
    return out.transpose(Image.FLIP_TOP_BOTTOM)


def assemble(floor, ceil):
    grass = Image.new("RGBA", (136, H), (0, 0, 0, 0))
    grass.paste(floor, (0, 0))              # floor  x0..63
    grass.paste(ceil, (W_HALF, 0))         # ceiling x64..127
    for x in range(MARKER_X, 136):         # marker  x128..135 (solid block)
        for y in range(H):
            grass.putpixel((x, y), MARKER + (255,))
    return grass


def main():
    ceil = make_ceiling()
    # Grassy variant: floor band + moulds
    g = assemble(make_floor(with_moulds=True), ceil)
    g.save(OUT); print(f"wrote {OUT} ({g.width}x{g.height})")
    # Non-Grassy variant: same, minus the moulds
    ng = assemble(make_floor(with_moulds=False), ceil)
    ng.save(OUT_NOMOULD); print(f"wrote {OUT_NOMOULD} ({ng.width}x{ng.height})")


if __name__ == "__main__":
    main()
