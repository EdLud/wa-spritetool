#!/usr/bin/env python3
"""Build a back2.spr layer: grass blades swaying in wind, with depth.

The sheet is FRAME_W x (FRAME_H * FRAMES), frames stacked vertically, matching
Coral Reef's back2.spr (1024x410, 30 frames).

Wind is not coral sway. The coral pool in make_back2.py oscillates around zero
-- sin(t) sends a frond as far left as right, which reads as water. Wind has a
prevailing direction: grass leans downwind and gusts FURTHER downwind, but it
never bends back into the wind. So the drive here is a RECTIFIED sine,

    lean + gust * (0.5 + 0.5*sin(theta))

which stays on one side of vertical for the whole cycle. Setting GUST to 0
gives a static lean; setting LEAN to 0 gives a blade that returns to upright at
the bottom of each gust but still never crosses it.

Three things make it read as a wind field rather than 500 blades each doing
their own thing:

  travel     A gust is a wave crossing the field, not a global pulse. Each
             blade's phase is offset by its x position (GUST_TRAVEL sets how
             many wave crests span the width), so bending sweeps across the
             grass the way a real gust does.
  tip bend   Displacement is t**BEND_POWER with t=0 at the root and 1 at the
             tip, so the base stays planted and the top does the travelling.
  one wind   All three depth layers share a direction by default. Real wind
             blows one way through the whole scene; per-layer directions read
             as three unrelated animations stacked up. Layer.wind_dir is there
             to override it anyway.

Depth is three layers, each smaller and darker than the one in front (see
LAYERS). DARKNESS at the top scales all of them together.

Amplitudes are fractions of a blade's own height, not pixels, so a distant
blade at half the size sways half as far without needing its own numbers.
"""
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# One blade, drawn once; every blade in the layer is this picture bent and
# scaled. Swap it for your own to change what sways.
BLADE_SRC = os.path.join(HERE, "assets", "grass_blade.png")

# --- sheet geometry -------------------------------------------------------
# back2.spr's shape: 1024 wide, 410 tall, 30 frames (Coral Reef ships exactly
# this). Swap -W/-H if the layer is meant to stand the other way up.
FRAME_W = 1024
FRAME_H = 410
FRAMES = 30

# --- wind -----------------------------------------------------------------
WIND_DIR = +1.0       # +1 blows right, -1 left
LEAN_FRAC = 0.10      # steady downwind lean at the tip, as a fraction of height
GUST_FRAC = 0.13      # EXTRA lean at the peak of a gust, same units
BEND_POWER = 2.3      # 1 = hinged at the root; higher keeps the bend in the tip
GUST_TRAVEL = 1.0     # gust crests spanning the frame width; 0 = everything
                      # gusts in unison
GUST_RATES = (1, 1, 2)  # gust cycles per loop, drawn per blade. WHOLE numbers
                        # only -- a fractional rate does not close the loop and
                        # the 30-frame strip jumps on repeat.
PHASE_JITTER = 0.35   # turns of random phase per blade, so a travelling gust
                      # still has texture instead of a hard wavefront

# --- depth ----------------------------------------------------------------
DARKNESS = 0.55        # total darkness knob: 1 = layers as written, lower
                      # darkens the whole field together

TILE_X = True         # back2.spr repeats left-to-right in game, so a blade
                      # overhanging one edge is redrawn on the other


@dataclass
class Layer:
    """One depth plane of grass.

    count       blades in this layer
    h_range     rendered blade height in px, (min, max)
    darkness    0..1 brightness multiplier; lower = further away
    base_y      where the roots sit, as a fraction of frame height. Slightly
                above 1.0 pushes roots below the bottom edge so no blade shows
                a cut-off stump.
    wind_dir    None = follow WIND_DIR. Set it per layer only to try the
                "each layer its own direction" idea; it costs the sense of a
                single wind.
    lean/gust   multipliers on LEAN_FRAC / GUST_FRAC, so a sheltered back
                layer can move less than the front without new numbers.
    """
    count: int
    h_range: Tuple[float, float]
    darkness: float
    base_y: float = 1.0
    wind_dir: Optional[float] = None
    lean: float = 1.0
    gust: float = 1.0


# Back to front: TALLER and darker with distance, which is upside down as
# perspective and better as a picture. Sizing the far layer up makes it a dark
# mass standing above the bright front rank instead of hiding behind it, so the
# depth arrives as a tonal step -- dark high, bright low -- that survives being
# mostly occluded. The real gradient put the tall blades in front, where they
# ate the other two layers whatever the density.
#
# Depth now rests entirely on the darkness tint, so keep those well separated;
# with the heights inverted it is the only cue left.
#
# The front layer is still the SPARSEST. Depth only reads if the far layers
# show between the near blades, and blade count rising with distance is also
# what a receding field does: the same ground spacing covers less screen the
# further off it is.
# Counts stay modest for a reason: with every layer inverted the three now
# overlap in the middle of the frame, so the band where they meet fills in far
# faster than the totals suggest. At ~1600 blades it went 100% solid there and
# the field read as three flat masses; ~500 keeps individual blades legible.
LAYERS = [
    Layer(count=300, h_range=(210, 340), darkness=0.84, base_y=0.88, gust=0.7),
    Layer(count=160, h_range=(115, 205), darkness=0.92, base_y=0.96, gust=0.88),
    Layer(count=45, h_range=(70, 150), darkness=1.00, base_y=1.06),
]


# ------------------------------------------------------------------ blade --
def load_blade(path=BLADE_SRC):
    """The blade as a PREMULTIPLIED float array, cropped to its opaque box.

    Premultiplied matters. The source has magenta/blue/yellow guide marks and
    a large black field sitting at alpha 0; a plain RGBA resize interpolates
    those dead colours into the edge pixels and the blade comes out fringed
    with black and magenta. Multiplying by alpha first means a transparent
    pixel contributes nothing whatever its colour is.
    """
    im = Image.open(path).convert("RGBA")
    a = im.getchannel("A")
    box = a.point(lambda v: 255 if v >= 8 else 0).getbbox()
    im = im.crop(box)
    arr = np.asarray(im, dtype=np.float32) / 255.0
    arr[..., :3] *= arr[..., 3:4]
    return arr


def scaled(blade, height):
    """Blade resampled to `height` px, keeping aspect. Stays premultiplied."""
    h, w = blade.shape[:2]
    new_h = max(2, int(round(height)))
    new_w = max(1, int(round(w * new_h / h)))
    im = Image.fromarray((np.clip(blade, 0, 1) * 255).astype(np.uint8), "RGBA")
    # BOX for the same reason the debris strip uses it: no ringing, so nothing
    # gets alpha the source never had.
    im = im.resize((new_w, new_h), Image.BOX)
    return np.asarray(im, dtype=np.float32) / 255.0


def bend(arr, shift):
    """Displace each ROW horizontally by shift[y] px, with sub-pixel accuracy.

    A blade is one continuous strip, so every pixel at a given height moves
    together -- unlike the coral warp in make_back2._sway_frame, which has to
    resolve a thin twig and a thick trunk sharing a row and scatters pixel by
    pixel. A per-row gather does the same job here and vectorises completely.

    Returns (array, pad) with the array widened by `pad` on each side so the
    bend cannot clip.
    """
    h, w = arr.shape[:2]
    pad = int(np.ceil(np.abs(shift).max())) + 2
    out_w = w + 2 * pad

    # dest = src + shift  =>  src = dest - shift, sampled with linear interp
    src_x = np.arange(out_w, dtype=np.float32)[None, :] - pad - shift[:, None]
    x0 = np.floor(src_x).astype(np.int32)
    frac = (src_x - x0)[..., None]
    x1 = x0 + 1

    ok0 = ((x0 >= 0) & (x0 < w))[..., None]
    ok1 = ((x1 >= 0) & (x1 < w))[..., None]
    rows = np.arange(h)[:, None]
    lo = arr[rows, np.clip(x0, 0, w - 1)] * ok0
    hi = arr[rows, np.clip(x1, 0, w - 1)] * ok1
    return lo * (1.0 - frac) + hi * frac, pad


def over(canvas, src, x, y):
    """Source-over composite of a premultiplied sprite, clipped to the canvas."""
    H, W = canvas.shape[:2]
    h, w = src.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    s = src[y0 - y:y1 - y, x0 - x:x1 - x]
    dst = canvas[y0:y1, x0:x1]
    canvas[y0:y1, x0:x1] = s + dst * (1.0 - s[..., 3:4])


def build(blade, layers=LAYERS, frames=FRAMES, w=FRAME_W, h=FRAME_H,
          darkness=DARKNESS, wind_dir=WIND_DIR, seed=11, tile_x=TILE_X):
    """Render the whole strip. Returns an RGBA Image, frames stacked down."""
    rnd = np.random.default_rng(seed)

    # Everything about a blade that does not change between frames, worked out
    # once: the warp is the only per-frame cost.
    plants = []
    for layer in layers:
        wd = wind_dir if layer.wind_dir is None else layer.wind_dir
        dark = layer.darkness * darkness
        for _ in range(layer.count):
            bh = rnd.uniform(*layer.h_range)
            art = scaled(blade, bh)
            art = art.copy()
            art[..., :3] *= dark          # premultiplied, so RGB alone
            plants.append(dict(
                art=art,
                x=int(rnd.integers(0, w)),
                y=int(round(layer.base_y * h)) - art.shape[0],
                phase=float(rnd.uniform(0, 1)) * PHASE_JITTER,
                rate=int(rnd.choice(GUST_RATES)),
                lean=LEAN_FRAC * layer.lean * wd * art.shape[0],
                gust=GUST_FRAC * layer.gust * wd * art.shape[0],
                flip=bool(rnd.integers(0, 2)),
            ))
            if plants[-1]["flip"]:
                plants[-1]["art"] = plants[-1]["art"][:, ::-1]

    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for f in range(frames):
        canvas = np.zeros((h, w, 4), dtype=np.float32)
        for p in plants:
            art = p["art"]
            bh = art.shape[0]
            # A gust is a wave crossing the field: phase advances with x.
            theta = (2 * math.pi * (p["rate"] * f / frames
                                    + p["phase"]
                                    + GUST_TRAVEL * p["x"] / w))
            # Rectified: 0..1, never negative, so the blade never crosses
            # upright into the wind.
            drive = p["lean"] + p["gust"] * (0.5 + 0.5 * math.sin(theta))
            t = 1.0 - np.arange(bh, dtype=np.float32) / max(1, bh - 1)
            shift = drive * (t ** BEND_POWER)

            bent, pad = bend(art, shift)
            x = p["x"] - pad
            for ox in ((x - w, x, x + w) if tile_x else (x,)):
                over(canvas, bent, ox, p["y"])

        # back out of premultiplied for the PNG
        a = canvas[..., 3:4]
        rgb = np.divide(canvas[..., :3], a, out=np.zeros_like(canvas[..., :3]),
                        where=a > 1e-6)
        out = np.concatenate([np.clip(rgb, 0, 1), np.clip(a, 0, 1)], axis=2)
        frame = Image.fromarray((out * 255).round().astype(np.uint8), "RGBA")
        sheet.paste(frame, (0, f * h))
    return sheet


def save_gif(sheet, path, frames=FRAMES, h=FRAME_H, duration=66):
    """Preview the loop so the wind can be judged before it goes in the game."""
    imgs = []
    for i in range(frames):
        f = sheet.crop((0, i * h, sheet.width, (i + 1) * h))
        bed = Image.new("RGBA", f.size, (24, 28, 38, 255))
        imgs.append(Image.alpha_composite(bed, f)
                    .convert("P", palette=Image.ADAPTIVE))
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, disposal=2)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="back2.spr.png",
                    help="output PNG strip (default ./back2.spr.png)")
    ap.add_argument("-b", "--blade", default=BLADE_SRC)
    ap.add_argument("-n", "--frames", type=int, default=FRAMES)
    ap.add_argument("-W", "--width", type=int, default=FRAME_W)
    ap.add_argument("-H", "--height", type=int, default=FRAME_H)
    ap.add_argument("-D", "--darkness", type=float, default=DARKNESS,
                    help="total darkness, 1 = layers as written")
    ap.add_argument("--wind", type=float, default=WIND_DIR,
                    help="+1 blows right, -1 left")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--no-tile", action="store_true",
                    help="do not wrap blades across the left/right edges")
    ap.add_argument("--spd", action="store_true",
                    help="also write the .spd beside the PNG")
    ap.add_argument("--gif", metavar="PATH", nargs="?", const="",
                    help="also write an animated preview GIF")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)

    blade = load_blade(args.blade)
    sheet = build(blade, frames=args.frames, w=args.width, h=args.height,
                  darkness=args.darkness, wind_dir=args.wind, seed=args.seed,
                  tile_x=not args.no_tile)
    sheet.save(out)
    total = sum(l.count for l in LAYERS)
    print(f"wrote {out} ({sheet.width}x{sheet.height}, "
          f"{args.frames} frames of {args.width}x{args.height})")
    print(f"  {total} blades over {len(LAYERS)} layers, darkness {args.darkness}")

    if args.spd:
        spd = os.path.splitext(out)[0] + ".spd"
        if spd.endswith(".spr.spd") is False:
            spd = out + ".spd"
        with open(spd, "w", newline="\r\n") as fh:
            fh.write(f"frames = {args.frames}\nheight = {args.height}\n"
                     f"width = {args.width}\nframerate = 1\nflags = 1\n")
        print(f"wrote {spd}")

    if args.gif is not None:
        gif = args.gif or os.path.splitext(out)[0] + ".gif"
        save_gif(sheet, gif, frames=args.frames, h=args.height)
        print(f"wrote {gif}")
