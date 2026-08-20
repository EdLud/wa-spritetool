#!/usr/bin/env python3
"""Colours the games fix for us, and the small operations on them.

The two gfx tables are the reason this file exists. A sprite in a terrain's
gfx0 or gfx1 folder carries a palette, but the game does not paint with it:
it indexes into the fixed table the slot already holds, so a colour is
whatever that table says and not what the sprite meant. Art that strays
outside comes out recoloured. Every one of Coral Reef's 446 gfx0 overrides
stays inside these 89, without exception.

Index 0 is transparent and is not listed, so entry 0 here is palette index 1.
One of the 89 is a true black, so the run cannot be found by trimming blacks
off the end -- it is 89 long by definition.
"""

from typing import Iterable, List, Sequence, Tuple

RGB = Tuple[int, int, int]

GFX0 = (
    (242, 185,   6), (188, 144,   0), (242, 184,  60), (224,  96,   0),
    (238, 123,   6), (246, 157,   8), (118,  36,  10), (120,  91,  30),
    (164,  99,  28), (176, 141,  62), (140,   2,   2), (166,  47,  12),
    (186,  75,  36), (236,  81,  54), (244, 123, 114), (252, 128, 128),
    (246,   1,   0), (240,  48,  24), (  0,   0,   0), ( 70,  39,  24),
    ( 70,  50,  50), ( 86,  63,  62), ( 94,  73,  72), (106,  76,  74),
    (124,  89,  88), (150, 141, 132), (246, 230, 204), ( 52,  11,   2),
    (118,  64,  48), (144, 103, 102), (158, 115, 114), (176, 128, 126),
    (190, 144, 136), (218, 153, 140), (224, 166, 164), (222, 183, 168),
    (252, 217,   0), (252, 254,   6), (234, 224, 110), (242, 254, 116),
    (252, 254, 128), (242, 220,  66), ( 80, 152,  28), (106, 185,  68),
    (138, 221,  92), ( 60,  69,  14), ( 64,  96,  14), ( 76, 124,  28),
    (132, 132,  50), (174, 186,  98), (228, 224, 180), ( 28,  40,   6),
    (182, 184, 178), (126, 254, 126), (156, 157, 156), (252, 254, 252),
    (110, 188, 180), (118, 222, 222), ( 92,  97, 100), (116, 124, 124),
    (128, 254, 252), (  4,   5,  10), ( 80,  78, 108), (190, 198, 222),
    ( 70,  93, 186), ( 54,  55, 106), ( 42,  53, 158), ( 74,  83, 154),
    ( 84,  99, 154), (100, 105, 182), (120, 126, 174), (132, 136, 202),
    (164, 157, 224), (156, 157, 254), (176, 177, 228), (202, 196, 252),
    ( 22,  17, 194), (  0,   1, 220), (130, 107, 136), (238, 234, 240),
    (170, 143, 226), (130,  73, 134), (252, 128, 252), (208,  93,  94),
    ( 24,  19,  22), ( 44,  39,  42), (180, 147, 156), (220, 203, 208),
    (190, 100, 172),
)

GFX1 = (
    (244, 184,   6), (158, 132,  18), (210, 152,   2), (252, 186,  62),
    (228,  83,   0), (230, 122,   4), (246, 231, 198), (102,  13,   0),
    (162,  70,  26), (146, 105,  14), (156, 124,  76), (210, 162, 100),
    (158,  39,  10), (208,  35,  26), (216,  76,  52), (232, 120, 100),
    (254, 127, 126), (186,   1,   0), (252,   0,   0), (252,  22,  14),
    (  0,   0,   0), ( 28,  10,   0), ( 54,  29,  28), ( 64,  44,  42),
    ( 80,  60,  58), ( 86,  72,  72), (106,  74,  74), (130, 126, 124),
    (214, 182, 164), (226, 197, 196), (250, 248, 242), (110,  71,  50),
    (136,  93,  88), (160, 115, 114), (178, 132, 128), (216, 160, 158),
    (224, 167, 166), (250, 251,  36), (252, 254,  64), (250, 218,   6),
    (108, 167,  22), (152, 175,  48), (204, 197,  80), (232, 225, 112),
    (242, 253, 114), ( 34,  44,   0), ( 48,  70,   6), ( 82, 108,   6),
    ( 98, 104,  78), (158, 164, 138), (  0, 190,   0), (  0, 208,   0),
    (  8, 110,   8), (156, 157, 156), (252, 254, 252), ( 88, 162, 136),
    ( 92, 116, 114), ( 68, 187, 190), ( 76, 241, 240), ( 78, 255, 252),
    (  0, 149, 252), ( 16,  16,  20), (172, 175, 208), (192, 195, 202),
    ( 60,  87, 174), ( 40,  49, 126), ( 58,  68, 116), ( 86,  88, 156),
    ( 72,  91, 192), ( 84, 107, 160), (104, 109, 166), (112, 116, 202),
    (158, 142, 218), (166, 157, 216), (160, 157, 250), (200, 195, 252),
    ( 28, 123, 202), ( 66, 150, 248), (  8,   7, 240), ( 70,   6, 102),
    (170, 107, 174), (210,   0, 208), (230,   0, 232), (118,  83,  84),
    (130, 101, 102), (162, 140, 154), (232, 226, 230), (212, 136, 140),
    (172,   4,  86),
)


PALETTES = {"gfx0": GFX0, "gfx1": GFX1}


def nearest(colour: RGB, palette: Sequence[RGB]) -> int:
    """Index of the closest palette entry, by squared distance in RGB."""
    r, g, b = colour
    best, best_d = 0, None
    for i, (pr, pg, pb) in enumerate(palette):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def fit(colours: Iterable[RGB], palette: Sequence[RGB]) -> List[RGB]:
    """Snap each colour to its nearest entry in `palette`."""
    return [palette[nearest(c, palette)] for c in colours]


def distance(a: RGB, b: RGB) -> float:
    """How far apart two colours are, out of the 441 that spans the cube."""
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def tint(im, colour: RGB):
    """Recolour an RGBA sprite's fill, keeping its per-pixel brightness.

    Alpha is untouched, so the silhouette does not change.
    """
    from PIL import Image
    r, g, b, a = im.convert("RGBA").split()
    lum = Image.merge("RGB", (r, g, b)).convert("L")
    tinted = Image.merge("RGB", tuple(
        lum.point(lambda v, c=c: int(v * c / 255)) for c in colour))
    return Image.merge("RGBA", (*tinted.split(), a))


def brightness(im, pct: int):
    """Scale an RGBA image's colour by pct/100, keeping alpha. 100 = as is."""
    from PIL import Image
    if pct == 100:
        return im
    f = pct / 100.0
    r, g, b, a = im.convert("RGBA").split()
    rgb = Image.merge("RGB", (r, g, b)).point(lambda v: min(255, int(v * f)))
    return Image.merge("RGBA", (*rgb.split(), a))


def ramp(top: RGB, bottom: RGB, steps: int) -> List[RGB]:
    """`steps` colours blended from `top` to `bottom`."""
    steps = max(1, steps)
    if steps == 1:
        return [top]
    return [tuple(int(top[i] + (bottom[i] - top[i]) * n / (steps - 1))
                  for i in range(3)) for n in range(steps)]


def read_sheet(path: str) -> List[RGB]:
    """The colours in a picture, in the order they are first met.

    Transparent pixels are skipped, so a swatch sheet with unused squares
    left clear reads back as exactly the colours it shows.
    """
    from PIL import Image
    raw = Image.open(path).convert("RGBA").tobytes()
    seen: List[RGB] = []
    known = set()
    for o in range(0, len(raw), 4):
        if raw[o + 3] < 128:
            continue
        c = (raw[o], raw[o + 1], raw[o + 2])
        if c not in known:
            known.add(c)
            seen.append(c)
    return seen
