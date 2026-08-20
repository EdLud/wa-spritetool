#!/usr/bin/env python3
"""Build a Worms Armageddon sprite-strip animation: rising circles.

W:A wants the frames of an animation stacked vertically in a single PNG, so
the sheet is FRAME_W x (FRAME_H * FRAMES). Each instance the game spawns runs
this strip independently, so nothing here needs to tile or loop seamlessly --
a circle is free to rise, shrink away and simply be gone.

Every circle is one `Circle`: its size, colour, speed, where it starts, how it
weaves, and how long it takes to shrink out. CIRCLES at the bottom is the list
the strip is built from -- add entries there to try different arrangements.

Three behaviours worth knowing:

  fade      A circle shrinks to nothing over `fade_frames`. The ramp starts
            when its edge first touches any frame border, or early enough to
            finish by the last frame, whichever comes first -- so a circle
            that would still be mid-screen at the end bows out rather than
            being cut off. `fade_in_frames` is the mirror image: the circle
            grows from nothing over that many frames once it appears.
  wave      `wave_amp` / `wave_period` push the circle sideways as it climbs.
            Sub-pixel X is fine; the supersampled draw turns it into a smooth
            weave instead of a stair-step.
  overlap   Circles are drawn onto one frame in list order, compositing
            normally, so they occlude rather than punch holes in each other.
"""
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image, ImageDraw

# --- sheet geometry -------------------------------------------------------
FRAMES = 160    
FRAME_W = 400
FRAME_H = 400

SS = 8                         # supersample factor for the circle edge

YELLOW = (245, 206, 30, 255)

# --- palette targets ------------------------------------------------------
# Two layers of the game are targeted and they do not share a palette, so a
# glow has to be tuned per layer. Both are copied in as constants rather than
# read at runtime, so this script does not depend on wa-spritetool sitting
# next to it.
#
# The common constraint: W:A has no alpha, only "this pixel is the transparent
# index or it is fully opaque". A glow can therefore never fade *out* -- it has
# to be painted as opaque colours already blended toward the background, which
# is why BG_RGB is not cosmetic. It is what the falloff resolves into, and it
# is why the two palettes want different glow colours: what matters is whether
# a hue has real entries along the straight line from itself down to BG_RGB.
BG_RGB = (0x10, 0x10, 0x21)    # 101021, the backdrop these sprites render over

# gfx0 layer: Palette_gfx0_90cols.ACT, 256 raw RGB triplets of which only
# 0..89 are real (the rest are padding zeros, not reproduced here). Index 0 is
# the transparent one.
PALETTE_GFX0 = [
    (  0,  0,  0), (242,185,  6), (188,144,  0), (242,184, 60), (224, 96,  0), (238,123,  6),  # 0
    (246,157,  8), (118, 36, 10), (120, 91, 30), (164, 99, 28), (176,141, 62), (140,  2,  2),  # 6
    (166, 47, 12), (186, 75, 36), (236, 81, 54), (244,123,114), (252,128,128), (246,  1,  0),  # 12
    (240, 48, 24), (  0,  0,  0), ( 70, 39, 24), ( 70, 50, 50), ( 86, 63, 62), ( 94, 73, 72),  # 18
    (106, 76, 74), (124, 89, 88), (150,141,132), (246,230,204), ( 52, 11,  2), (118, 64, 48),  # 24
    (144,103,102), (158,115,114), (176,128,126), (190,144,136), (218,153,140), (224,166,164),  # 30
    (222,183,168), (252,217,  0), (252,254,  6), (234,224,110), (242,254,116), (252,254,128),  # 36
    (242,220, 66), ( 80,152, 28), (106,185, 68), (138,221, 92), ( 60, 69, 14), ( 64, 96, 14),  # 42
    ( 76,124, 28), (132,132, 50), (174,186, 98), (228,224,180), ( 28, 40,  6), (182,184,178),  # 48
    (126,254,126), (156,157,156), (252,254,252), (110,188,180), (118,222,222), ( 92, 97,100),  # 54
    (116,124,124), (128,254,252), (  4,  5, 10), ( 80, 78,108), (190,198,222), ( 70, 93,186),  # 60
    ( 54, 55,106), ( 42, 53,158), ( 74, 83,154), ( 84, 99,154), (100,105,182), (120,126,174),  # 66
    (132,136,202), (164,157,224), (156,157,254), (176,177,228), (202,196,252), ( 22, 17,194),  # 72
    (  0,  1,220), (130,107,136), (238,234,240), (170,143,226), (130, 73,134), (252,128,252),  # 78
    (208, 93, 94), ( 24, 19, 22), ( 44, 39, 42), (180,147,156), (220,203,208), (190,100,172),  # 84
]

# Paradise Ruins layer: read off build/palette.png, which stores its colours as
# a 16x7 grid of 16px swatches rather than as an .ACT. 112 swatches, the last
# of which is the transparent one and so is not a colour here.
PALETTE_PARADISE = [
    ( 92, 71, 22), (120, 81, 42), ( 83, 48,  9), (115,111, 38), (154,136, 74), (176,148, 80),  # 0
    (178,156, 96), (182,168, 88), (221,199,138), (221,199,129), (152,114, 38), (209,187,117),  # 6
    (243,222,160), (251,236,180), (229,207,137), (156,154, 83), (231,199,139), (238,215,148),  # 12
    (192,172,106), (227,207,147), ( 61, 83,  0), ( 67,104,  0), ( 76,113, 10), ( 81,124, 10),  # 18
    (106,139, 23), ( 83,149, 12), (212,189,131), (104,126, 40), (101,152, 11), (113,172, 28),  # 24
    (124,178, 28), ( 89,138, 16), (104,153, 33), (100,167, 25), (227,209,165), (226,215,149),  # 30
    (210,199,131), (121,153, 18), (121, 87,  0), (117,140,  7), (200,180,114), (193,140, 27),  # 36
    (180,124,  7), ( 65,128,  0), (104,151,  0), ( 93,136,  0), ( 94,120,  0), ( 87,137,  0),  # 42
    (116,167,  3), ( 83,149,  0), (100,168,  0), (128,167,  0), (118,173,  2), (103,152,  0),  # 48
    (115,184,  0), (130,155,  1), (129,202,  0), (140,185,  3), (167,210,  0), (168,232,  0),  # 54
    ( 41, 41, 47), (123,128, 95), (102, 95, 78), (162,162,166), (180,180,179), (204,206,180),  # 60
    (218,207,137), (144,201, 34), (164,200,  0), (234,217,135), (146,182,  0), (141,170,  0),  # 66
    (151,176,  5), (242,205, 45), (218,200,133), ( 16, 16, 32), ( 23, 35,  0), ( 34, 43,  6),  # 72
    ( 11, 12, 20), (  9, 10, 15), ( 63, 53, 33), (  3,  4,  4), (225,203,137), ( 99,120, 21),  # 78
    ( 81,122, 11), ( 69, 99,  4), (104,153, 31), ( 69,106,  2), (195,172,110), (210,187,124),  # 84
    (212,189,130), (192,172,107), (210,190,126), (217,195,132), (209,186,128), ( 94,120,  1),  # 90
    ( 70,104,  4), (128, 97, 51), (122, 84, 45), ( 95,150, 13), ( 67,104,  2), (114,172, 29),  # 96
    (104,153, 34), ( 99,158, 21), (227,207,144), (177,155,101), (229,207,138), (113,172, 29),  # 102
    ( 55, 66, 30), (121, 82, 43), (164,188, 13),  # 108
]

PALETTES = {
    "gfx0": PALETTE_GFX0,
    "paradise": PALETTE_PARADISE,
}

# Which glow colour each palette can actually render smoothly. Measured, not
# guessed: for a candidate hue, walk the straight line from it down to BG_RGB
# and check that every step lands near a real entry (see `ramp_error`).
#
#   gfx0      periwinkle, mean error ~16 over 9 distinct entries. Its blue
#             block (63..78) sits almost exactly on the line to the
#             background. Yellow there manages only 8 steps at ~25 and
#             detours through khaki, so it bands into mud.
#   paradise  yellow, mean error ~17 over 8 entries, and the palette even
#             contains 101020 -- the background itself -- as its last step.
#             Periwinkle here is hopeless at ~55: there is no blue in it.
GLOW_COLORS = {
    "gfx0": (156, 157, 254, 255),      # periwinkle, entry 74
    "paradise": (242, 205, 45, 255),   # gold, entry 73
}

def ramp_error(color, palette, bg=BG_RGB, steps=13):
    """How well `palette` can render a glow of `color` fading into `bg`.

    Walks the straight line from `color` to `bg` and, at each step, measures
    how far the nearest palette entry is from where the ramp actually wants to
    be. Returns (mean error, worst error, distinct entries used).

    Low mean with a high distinct count is a smooth glow. A high mean means
    the falloff is being dragged off-hue at some point -- the visible symptom
    is a halo that goes muddy partway out. Few distinct entries means banding
    no matter how finely the glow is drawn, because there is simply nothing in
    the palette to put between the steps.
    """
    picks = []
    errs = []
    for k in range(steps):
        t = k / float(steps - 1)
        want = tuple(color[j] * (1 - t) + bg[j] * t for j in range(3))
        best = min(palette, key=lambda c: sum((c[j] - want[j]) ** 2
                                              for j in range(3)))
        errs.append(math.sqrt(sum((best[j] - want[j]) ** 2 for j in range(3))))
        picks.append(best)
    return sum(errs) / len(errs), max(errs), len(set(picks))


@dataclass
class Circle:
    """One circle's whole life over the strip.

    diameter    px at full size, before any fade shrink
    speed       px risen per frame (positive = upward)
    start_y     centre Y on frame 0; None = just below the bottom edge
    x           centre X, the axis the wave swings around; None = centred
    color       RGBA; None = whatever the target layer's palette can render as
                a smooth glow (see GLOW_COLORS). Leave it unset unless you want
                a specific hue, since the right answer differs per layer.
    fade_frames how many frames the shrink-to-nothing takes. 0 = never fade,
                the circle just leaves the top edge.
    fade_in_frames
                how many frames the grow-from-nothing takes, counted from the
                circle's first live frame (`delay`). 0 = pop in at full size.
    wave_amp    px of horizontal swing either side of `x`. 0 = rise straight.
    wave_period frames for one full left-right cycle
    wave_phase  turns (0..1) to offset the wave by, so several circles on the
                same period don't move in lockstep
    delay       frames to wait before the circle appears at all
    glow        radius of the halo as a multiple of the circle's own radius.
                0 = no glow, a hard-edged disc. 3.0 means the halo reaches
                three radii past the core before it hits background.
    glow_gamma  shape of the falloff. 1 = linear, >1 keeps the halo tight
                around the core, <1 spreads it out flat and wide.
    glow_color  RGBA the halo starts at next to the core; None = `color`.
    """
    diameter: float = 5.0
    speed: float = 0.5 * FRAME_H / FRAMES
    start_y: Optional[float] = None
    x: Optional[float] = None
    color: Optional[Tuple[int, int, int, int]] = None
    fade_frames: int = 20
    fade_in_frames: int = 0
    wave_amp: float = 0.0
    wave_period: float = 60.0
    wave_phase: float = 0.0
    delay: int = 0
    glow: float = 0.0
    glow_gamma: float = 1.4
    glow_color: Optional[Tuple[int, int, int, int]] = None

    def pos(self, i, w=FRAME_W, h=FRAME_H):
        """Centre (x, y) on frame `i`, in frame space."""
        base_x = w / 2.0 if self.x is None else self.x
        start_y = (h + self.diameter / 2.0) if self.start_y is None else self.start_y
        t = i - self.delay
        y = start_y - t * self.speed
        x = base_x
        if self.wave_amp and self.wave_period:
            x += self.wave_amp * math.sin(
                2 * math.pi * (t / self.wave_period + self.wave_phase))
        return x, y

    def fade_start(self, frames=FRAMES, w=FRAME_W, h=FRAME_H):
        """First frame of the shrink ramp.

        Whichever comes first: the frame the circle's edge touches a border,
        or late enough that `fade_frames` still fits before the strip ends.
        A circle with no fade returns None.
        """
        if self.fade_frames <= 0:
            return None
        r = self.diameter / 2.0
        touch = None
        # A circle usually spawns below the bottom edge and rises in, so it
        # starts out "touching" a border. That must not count -- the ramp is
        # for an edge it arrives at, so only look once it is fully inside.
        entered = False
        for i in range(self.delay, frames):
            x, y = self.pos(i, w, h)
            inside = (x - r >= 0 and x + r <= w
                      and y - r >= 0 and y + r <= h)
            if not entered:
                entered = inside
                continue
            if not inside:
                touch = i
                break
        # ...and always leave room to finish before the sheet runs out.
        by_end = frames - self.fade_frames
        return by_end if touch is None else min(touch, by_end)

    def scale(self, i, frames=FRAMES, w=FRAME_W, h=FRAME_H):
        """Size multiplier on frame `i`: 1.0 normally, ramping at either end.

        Grows in over `fade_in_frames` from `delay`, shrinks out over
        `fade_frames` from `fade_start`. If the two ramps overlap -- a short
        life, or a fade that starts before the grow-in finished -- the smaller
        of the two wins, so neither end gets skipped.
        """
        s = 1.0
        if self.fade_in_frames > 0:
            s = min(s, max(0.0, (i - self.delay) / float(self.fade_in_frames)))
        start = self.fade_start(frames, w, h)
        if start is not None and i >= start:
            s = min(s, max(0.0, 1.0 - (i - start) / float(self.fade_frames)))
        return s


def render_frame(circles, i, frames=FRAMES, w=FRAME_W, h=FRAME_H, layer=None):
    """One transparent frame with every live circle drawn on it.

    `layer` names the target palette ("gfx0" / "paradise"). Circles that left
    `color` unset take that layer's glow colour, which is how the same CIRCLES
    list renders correctly on either layer without hardcoding a hue.
    """
    tint = GLOW_COLORS.get(layer)
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    # "RGBA" mode on the draw makes ellipse() alpha-composite onto what is
    # already there instead of replacing the pixel. The glow rings depend on
    # it: they are semi-transparent discs stacked over each other, and a
    # replacing draw would leave only the last one.
    d = ImageDraw.Draw(big, "RGBA")

    for c in circles:
        if i < c.delay:
            continue
        r = c.diameter / 2.0 * c.scale(i, frames, w, h)
        if r <= 0:
            continue
        x, y = c.pos(i, w, h)
        core = c.color or tint or YELLOW
        outer = r * c.glow if c.glow > 1.0 else r
        # Off the top and gone -- nothing wraps back around.
        if y + outer < 0 or y - outer > h or x + outer < 0 or x - outer > w:
            continue

        if c.glow > 1.0:
            # Concentric rings from the outside in, each one step along the
            # falloff. Drawing outermost-first and letting each disc paint
            # over the last is what makes it a gradient rather than a stack
            # of visible bands -- with SS=8 the rings land sub-pixel apart
            # once the frame is boxed back down.
            gc = c.glow_color or core
            # One ring per supersampled pixel of halo width. Coarser than that
            # and a wide halo shows concentric banding once it is quantized --
            # the palette snap turns a slightly-stepped ramp into visible
            # rings, because neighbouring steps land on the same entry until
            # one of them suddenly does not.
            steps = max(16, int((outer - r) * SS))
            for s in range(steps):
                # 1 at the outer edge -> 0 just outside the core
                t = 1.0 - s / float(steps)
                rr = r + (outer - r) * t
                a = (1.0 - t) ** c.glow_gamma
                d.ellipse([(x - rr) * SS, (y - rr) * SS,
                           (x + rr) * SS, (y + rr) * SS],
                          fill=(gc[0], gc[1], gc[2], int(round(a * gc[3]))))

        d.ellipse([(x - r) * SS, (y - r) * SS, (x + r) * SS, (y + r) * SS],
                  fill=core)

    # BOX, not LANCZOS: lanczos rings, scattering alpha-1..3 speckle several
    # px outside a small circle. Invisible on screen, but W:A quantizes to a
    # palette with one transparent index, so that speckle survives as stray
    # opaque pixels. BOX is a plain area average -- no overshoot.
    return big.resize((w, h), Image.BOX)


# 4x4 Bayer matrix, centred to roughly -0.5..+0.5.
BAYER4 = [[(v + 0.5) / 16.0 - 0.5 for v in row] for row in
          ([0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5])]


def quantize_to_palette(img, palette=PALETTE_GFX0, bg=BG_RGB, dither=28.0):
    """Flatten `img` onto `bg` and snap every pixel to the nearest palette entry.

    This is the honest preview of what W:A will show: the glow only exists as
    opaque colours sitting on the background, so the alpha has to be resolved
    against `bg` *before* the snap. Quantizing the RGBA directly would match
    the halo against the palette as if it were floating on nothing and pick
    completely different entries.

    `dither` is the amplitude of an ordered 4x4 Bayer nudge applied before the
    snap. It exists because a wide halo cannot be smooth otherwise: there are
    only about nine palette entries between periwinkle and the background, so
    a plain nearest-match paints the falloff as nine flat rings no matter how
    finely it was drawn. Jittering each pixel by a fixed screen-space pattern
    breaks those rings into a stipple of the two neighbouring colours. Ordered,
    not Floyd-Steinberg: the pattern is locked to pixel position, so a moving
    circle keeps the same texture instead of crawling frame to frame. 0 = off.

    Transparent pixels stay transparent -- they become index 0 in the sprite,
    not a colour. Everything else is measured in plain squared RGB distance.
    """
    flat = Image.alpha_composite(Image.new("RGBA", img.size, bg + (255,)), img)
    px = flat.load()
    ax = img.load()
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    op = out.load()
    cache = {}
    for yy in range(img.size[1]):
        for xx in range(img.size[0]):
            if ax[xx, yy][3] == 0:
                continue
            key = px[xx, yy][:3]
            if dither:
                n = BAYER4[yy & 3][xx & 3] * dither
                key = tuple(min(255, max(0, int(round(v + n)))) for v in key)
            best = cache.get(key)
            if best is None:
                best = min(palette, key=lambda c: (c[0] - key[0]) ** 2
                           + (c[1] - key[1]) ** 2 + (c[2] - key[2]) ** 2)
                cache[key] = best
            op[xx, yy] = best + (255,)
    return out


def build_strip(circles, frames=FRAMES, w=FRAME_W, h=FRAME_H,
                layer="gfx0", snap=True):
    """Stack `frames` frames vertically into one RGBA sheet.

    `layer` picks the target palette, which decides both the default glow
    colour and what the frames are snapped to. `snap=False` keeps full-colour
    RGBA -- useful for seeing what the palette is costing you, but not what
    the game will show.
    """
    palette = PALETTES[layer] if snap else None
    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for i in range(frames):
        f = render_frame(circles, i, frames, w, h, layer)
        if palette:
            f = quantize_to_palette(f, palette)
        sheet.paste(f, (0, i * h))
    return sheet


def save_gif(sheet, path, frames=FRAMES, h=FRAME_H, duration=33):
    """Preview the strip as a GIF so the motion can be checked before W:A."""
    imgs = []
    for i in range(frames):
        f = sheet.crop((0, i * h, sheet.width, (i + 1) * h))
        # GIF has no alpha blending: flatten onto the real backdrop, so the
        # glow is judged against the colour it will actually sit on.
        bed = Image.new("RGBA", f.size, BG_RGB + (255,))
        imgs.append(Image.alpha_composite(bed, f).convert("P", palette=Image.ADAPTIVE))
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, disposal=2)


# --- the arrangement ------------------------------------------------------
# Edit this list to try different numbers of circles. Every field is optional;
# Circle() alone is one centred 5px circle rising at half a frame per strip.
# CIRCLES = [
#     Circle(diameter=5, x=50, start_y=0, speed=1.2,
#            wave_amp=0, wave_period=70, fade_frames=160),
#     Circle(diameter=7, x=0, start_y=30, speed=1.8,
#            wave_amp=0, wave_period=95, wave_phase=0.33, fade_frames=160),
#     Circle(diameter=2, x=80, start_y=120, speed=0.7,
#            wave_amp=1, wave_period=100, wave_phase=0.26,
#            fade_frames=60, fade_in_frames=60,
#            glow=6.0, glow_gamma=1.4),
# ]

CIRCLES = [
    Circle(diameter=1.8, x=100,  start_y=380, speed=1.5, wave_amp=0, wave_period=68, wave_phase=0.12, fade_frames=10, fade_in_frames=10, glow=3.0, glow_gamma=1.4),
    # Circle(diameter=1.2, x=200, start_y=350, speed=2, wave_amp=0, wave_period=91, wave_phase=0.41, fade_frames=10, fade_in_frames=10, glow=6.0, glow_gamma=1.4),
    Circle(diameter=2.3, x=300, start_y=200, speed=1, wave_amp=0, wave_period=57, wave_phase=0.73, fade_frames=10, fade_in_frames=10, glow=1.0, glow_gamma=1.4),
    # Circle(diameter=1, x=306, start_y=400, speed=2.4-1, wave_amp=8, wave_period=82, wave_phase=0.25, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=352, start_y=400, speed=2.0-1, wave_amp=10, wave_period=73, wave_phase=0.58, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=68,  start_y=400, speed=3.0-1, wave_amp=12, wave_period=103, wave_phase=0.86, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=105, start_y=400, speed=1.7-1, wave_amp=14, wave_period=49, wave_phase=0.17, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=170, start_y=400, speed=2.3-1, wave_amp=16, wave_period=76, wave_phase=0.52, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=250, start_y=400, speed=3.198-1, wave_amp=18, wave_period=88, wave_phase=0.31, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=325, start_y=400, speed=2.9-1, wave_amp=20, wave_period=61, wave_phase=0.69, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=55,  start_y=400, speed=2.8-1, wave_amp=22, wave_period=94, wave_phase=0.44, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=130, start_y=400, speed=3.2-1, wave_amp=24, wave_period=71, wave_phase=0.08, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=195, start_y=400, speed=2.8-1, wave_amp=26, wave_period=54, wave_phase=0.77, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=275, start_y=400, speed=2.5-1, wave_amp=28, wave_period=79, wave_phase=0.36, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=340, start_y=400, speed=3.1-1, wave_amp=30, wave_period=107, wave_phase=0.62, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=185,  start_y=400, speed=2.6-1, wave_amp=32, wave_period=47, wave_phase=0.29, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=155, start_y=400, speed=2.95-1, wave_amp=34, wave_period=98, wave_phase=0.91, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=235, start_y=400, speed=2.1-1, wave_amp=36, wave_period=69, wave_phase=0.46, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=310, start_y=400, speed=2.4-1, wave_amp=38, wave_period=84, wave_phase=0.15, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
    # Circle(diameter=1, x=205, start_y=400, speed=3.7-1, wave_amp=40, wave_period=59, wave_phase=0.71, fade_frames=80, fade_in_frames=80, glow=6.0, glow_gamma=1.4),
]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # Relative to wherever this is called from, not to the script.
    ap.add_argument("-o", "--out", default="debris.png",
                    help="output PNG strip (default ./debris.png)")
    ap.add_argument("-n", "--frames", type=int, default=FRAMES)
    ap.add_argument("-W", "--width", type=int, default=FRAME_W)
    ap.add_argument("-H", "--height", type=int, default=FRAME_H)
    ap.add_argument("--gif", metavar="PATH", nargs="?", const="",
                    help="also write an animated preview GIF")
    ap.add_argument("-p", "--layer", choices=sorted(PALETTES), default="gfx0",
                    help="which layer's palette to target; also picks the "
                         "glow colour that palette can render (default gfx0)")
    ap.add_argument("--raw", action="store_true",
                    help="skip the palette snap and keep full-colour RGBA")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)

    sheet = build_strip(CIRCLES, frames=args.frames, w=args.width,
                        h=args.height, layer=args.layer, snap=not args.raw)
    sheet.save(out)
    glow = GLOW_COLORS[args.layer]
    mean, worst, uniq = ramp_error(glow[:3], PALETTES[args.layer])
    print(f"layer {args.layer}: glow #{'%02x%02x%02x' % glow[:3]} over "
          f"#{'%02x%02x%02x' % BG_RGB}, ramp err {mean:.1f} avg / "
          f"{worst:.1f} worst over {uniq} entries")
    if args.raw:
        print("  (--raw: not snapped, this is not what the game will show)")
    else:
        print(f"  snapped to {len(PALETTES[args.layer])} palette colours")
    print(f"wrote {out} ({sheet.width}x{sheet.height}, "
          f"{args.frames} frames of {args.width}x{args.height})")
    for n, c in enumerate(CIRCLES):
        fs = c.fade_start(args.frames, args.width, args.height)
        print(f"  circle {n}: d={c.diameter} speed={c.speed} "
              f"wave={c.wave_amp}/{c.wave_period} "
              f"fade {c.fade_frames}f from frame {fs}")

    if args.gif is not None:
        gif = args.gif or os.path.splitext(out)[0] + ".gif"
        save_gif(sheet, gif, frames=args.frames, h=args.height)
        print(f"wrote {gif}")
