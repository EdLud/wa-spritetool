#!/usr/bin/env python3
"""Replace the gfx0 explosion sprites with dissipating clouds of flies.

Nineteen animations, each with its own .spr.spd fixing frame count and frame
size (20px up to 300px -- a 15x span), so nothing here is one-size-fits-all.
Every sprite gets its own entry in BURSTS below.

The shared idea: an explosion throws its cloud outward and thins it out. So a
burst is flies scattering from the centre, decelerating, and dropping out as
they go. What differs per sprite is the CHARACTER of that scatter, and that is
what the per-sprite fields are for -- a puff of smoke is a slow round bloom, an
exhaust is a directional jet, a flame is a cloud that never really disperses.

Flies hold ONE colour for their whole life and then vanish outright. Nothing
dims: a half-alpha fly is exactly what the game's palette cannot represent, so
it quantises to the background and reads as a flicker instead of a departure.
The cloud thins by losing flies, not by going grey.

Readability at small sizes is the whole problem. At 20px a fly must be ONE
pixel or it is a blob, and a 20px frame cannot hold 40 of them -- so count and
size are per-sprite too, and `fly_px` of 1 draws hard single pixels with no
antialiasing at all (see _draw). Antialiasing a 1px fly spreads it over four
pixels at quarter alpha and it vanishes into the palette.

Output is RGBA PNGs plus matching .spd files. Frame counts and dimensions are
taken from the ORIGINAL .spd of each sprite so the replacements drop straight
in; only the artwork changes.
"""
import math
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# The sprites being replaced, as decompressed out of a Level.dir -- point
# this at your own gfx0 folder. Nothing is read from it but the frame
# geometry, so any gfx0 dump will do.
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.environ.get("GFX0_DIR", os.path.join(HERE, "gfx0"))

SS = 8                      # supersample factor, for flies big enough to need it

# Flies are dark; these sprites are drawn over open sky and terrain alike.
#
# A FIXED SHORT LIST, not a gradient. The whole terrain shares one 112-colour
# palette (see the guide), and 19 sprites drawing from a continuous ramp put
# 119 distinct browns in it by themselves -- the entire budget, for flies. Five
# shades read as variation at 1-3px and cost five entries.
FLY_COLORS = (
    (38, 26, 16),
    (56, 39, 24),
    (74, 52, 31),
    (91, 64, 38),
    (108, 76, 46),
)


@dataclass
class Burst:
    """One sprite's worth of scattering flies.

    n           how many flies. Small frames need few or they read as a blob.
    fly_px      fly size in px. 1 means a single hard pixel (see _draw).
    spread      how far flies travel by the last frame, in frame widths. 0.5
                puts them at the border; more sends them off-frame, which is
                what makes a cloud look like it is dispersing rather than
                stopping.
    drag        0 = flies coast at constant speed, 1 = they stop dead by the
                end. An explosion decelerates, so most of these sit high.
    rise        upward drift over the whole strip, in frame heights. Smoke
                and flame rise; exhaust and debris do not.
    swirl       degrees each fly's heading rotates across the strip. Turns a
                straight scatter into a curl.
    jitter      px of per-frame fly-like twitch on top of the trajectory.
    fade_from   fraction of the strip after which flies start disappearing.
                Below 1.0 the cloud thins out; 1.0 keeps every fly to the end.
                Flies CUT OUT at full colour -- they never dim on the way.
    cone        None = scatter in all directions. (deg, width) aims the burst,
                for jets and directional exhausts.
    hold        frames at the start where flies stay bunched before flying
                out, as a fraction of the strip. Gives a beat before the burst.
    """
    n: int
    fly_px: float
    spread: float
    drag: float = 0.85
    rise: float = 0.0
    swirl: float = 0.0
    jitter: float = 0.35
    fade_from: float = 0.55
    cone: Optional[Tuple[float, float]] = None
    hold: float = 0.0
    seed: int = 0


# Each sprite gets a distinct character. Sizes and counts follow the frame:
# tiny frames get single-pixel flies and few of them.
#
#   elipse*/elips100  the round blast rings -- clean radial blooms, scaled by
#                     frame size, bigger ones denser and further-flung
#   smkdrk*/smklt*    smoke puffs -- slower, rising, curling, thinning out
#   exhaust/hexhaust/mexhaust  directional jets, aimed with `cone`
#   flame1/flame2     looping (flags=1), so no fade and no net escape
#   firehit           a 9-frame snap: few flies, violent, gone
#   kamismk/petrol1   trailing smoke, light and drifting
BURSTS = {
    # --- blast rings ------------------------------------------------------
    "elipse25":  Burst(n=14, fly_px=1, spread=0.62, drag=0.88, swirl=25,
                       fade_from=0.5, seed=1),
    "elipse50":  Burst(n=30, fly_px=2, spread=0.66, drag=0.88, swirl=-30,
                       fade_from=0.52, seed=2),
    "elipse75":  Burst(n=44, fly_px=2, spread=0.70, drag=0.9, swirl=35,
                       fade_from=0.5, seed=3),
    "elips100":  Burst(n=64, fly_px=3, spread=0.74, drag=0.9, swirl=-22,
                       fade_from=0.48, seed=4),

    # --- dark smoke -------------------------------------------------------
    "smkdrk20":  Burst(n=7, fly_px=1, spread=0.5, drag=0.8, rise=0.28,
                       swirl=40, fade_from=0.45, jitter=0.25, seed=5),
    "smkdrk30":  Burst(n=11, fly_px=1, spread=0.54, drag=0.8, rise=0.26,
                       swirl=-45, fade_from=0.45, jitter=0.3, seed=6),
    "smkdrk40":  Burst(n=16, fly_px=1, spread=0.56, drag=0.82, rise=0.24,
                       swirl=50, fade_from=0.47, seed=7),

    # --- light smoke, bigger and lazier ----------------------------------
    "smklt25":   Burst(n=15, fly_px=1, spread=0.52, drag=0.78, rise=0.3,
                       swirl=-38, fade_from=0.44, seed=8),
    "smklt50":   Burst(n=22, fly_px=2, spread=0.56, drag=0.8, rise=0.28,
                       swirl=42, fade_from=0.46, seed=9),
    "smklt75":   Burst(n=34, fly_px=2, spread=0.6, drag=0.82, rise=0.25,
                       swirl=-34, fade_from=0.46, seed=10),
    "smklt100":  Burst(n=48, fly_px=3, spread=0.64, drag=0.84, rise=0.22,
                       swirl=30, fade_from=0.45, seed=11),

    # --- directional jets -------------------------------------------------
    # cone aims them: 90deg is straight up in screen terms (y grows downward,
    # and _fly_paths negates it), so these fire upward in a narrow spray.
    # Kept well under 0.5 spread: a cone concentrates every fly on one bearing,
    # so the same travel that merely thins a radial burst empties a jet clean
    # off the frame. Wider cones for the same reason -- a narrow one puts the
    # whole swarm on a single line.
    "exhaust":   Burst(n=20, fly_px=1, spread=0.42, drag=0.75, rise=0.12,
                       cone=(90, 60), fade_from=0.5, jitter=0.4, seed=12),
    "hexhaust":  Burst(n=26, fly_px=2, spread=0.46, drag=0.72, rise=0.1,
                       cone=(90, 48), fade_from=0.48, jitter=0.45, seed=13),
    "mexhaust":  Burst(n=22, fly_px=1, spread=0.44, drag=0.74, rise=0.11,
                       cone=(90, 72), fade_from=0.5, jitter=0.4, seed=14),

    # --- looping flames (flags=1): must not disperse or the loop shows ----
    # fade_from 1.0 keeps every fly alive, and the low spread with heavy drag
    # keeps the cloud in place. _fly_paths closes these loops exactly.
    "flame1":    Burst(n=18, fly_px=2, spread=0.3, drag=0.95, rise=0.0,
                       swirl=120, fade_from=1.0, jitter=0.5, seed=15),
    "flame2":    Burst(n=18, fly_px=2, spread=0.34, drag=0.95, rise=0.0,
                       swirl=-140, fade_from=1.0, jitter=0.5, seed=16),
    "petrol1":   Burst(n=12, fly_px=1, spread=0.36, drag=0.9, rise=0.0,
                       swirl=90, fade_from=1.0, jitter=0.45, seed=17),

    # --- one-shots --------------------------------------------------------
    "firehit":   Burst(n=12, fly_px=2, spread=0.78, drag=0.7, swirl=0,
                       fade_from=0.35, jitter=0.5, seed=18),
    "kamismk":   Burst(n=10, fly_px=1, spread=0.48, drag=0.8, rise=0.34,
                       swirl=-55, fade_from=0.4, seed=19),
}


def read_spd(path):
    """frames/width/height/framerate/flags from a .spd."""
    out = {}
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            if "=" in line:
                k, v = line.split("=")
                out[k.strip()] = int(v.strip())
    return out


def _fly_paths(b, frames, w, h, looping):
    """Per-fly (xs, ys, alphas) over the strip.

    Position is an eased outward travel, not a straight ramp: `drag` bends the
    distance curve so the fly covers most of its ground early and coasts to a
    stop, which is what an explosion's debris does.

    `looping` closes the loop exactly -- a flame sprite plays continuously, so
    the last frame has to hand back to the first. That rules out net escape
    and net rise, so a looping burst orbits instead of dispersing.
    """
    rnd = np.random.default_rng(b.seed)
    t = np.arange(frames, dtype=np.float64) / max(1, frames - 1)

    # hold: nothing moves for the first slice, then the burst starts
    if b.hold > 0:
        tt = np.clip((t - b.hold) / max(1e-6, 1 - b.hold), 0, 1)
    else:
        tt = t

    paths = []
    reach = b.spread * min(w, h)
    for i in range(b.n):
        if b.cone is not None:
            centre, width = b.cone
            ang = math.radians(centre + rnd.uniform(-width / 2, width / 2))
        else:
            # stratified around the circle so a small count still looks even
            ang = 2 * math.pi * (i + rnd.uniform(0, 1)) / b.n

        if looping:
            # A churning cluster, not an orbit. One shared circle makes every
            # fly ride the same ring and the sprite reads as a rotating arc;
            # what a flame needs is flies milling around their own spots. Each
            # gets its own centre, its own small radius and its own whole
            # number of turns, so the cloud boils while still closing exactly
            # at the last frame.
            phase = rnd.uniform(0, 2 * math.pi)
            turns = int(rnd.integers(1, 4))
            rad = reach * rnd.uniform(0.10, 0.30)
            # own anchor inside the cloud, filled rather than ringed
            ca = 2 * math.pi * rnd.uniform(0, 1)
            cr = reach * 0.55 * math.sqrt(rnd.uniform(0, 1))
            cx, cy = w / 2 + cr * math.cos(ca), h / 2 + cr * math.sin(ca) * 0.8
            th = 2 * math.pi * turns * t + phase
            xs = cx + rad * np.cos(th)
            ys = cy + rad * np.sin(th) * 0.7 - b.rise * h * np.sin(math.pi * t)
            alphas = np.ones(frames)
        else:
            # eased outward: fast start, coasting finish. The cloud starts at
            # a small radius rather than a point -- every fly leaving from the
            # exact centre makes frame 0 a single pixel, which reads as a dot
            # appearing before the burst rather than as the burst itself.
            k = 1.0 + 3.0 * b.drag
            dist = (1 - np.exp(-k * tt)) / (1 - math.exp(-k))
            # Start radius varies per fly, and by sqrt so the seed cluster is
            # evenly FILLED rather than a hollow ring -- a fixed r0 puts every
            # fly on one circle and the burst opens as an outline.
            r0 = max(1.0, 0.10 * reach) * math.sqrt(rnd.uniform(0, 1))
            dist = r0 + dist * (reach * rnd.uniform(0.55, 1.15) - r0)
            a = ang + math.radians(b.swirl) * tt
            xs = w / 2 + dist * np.cos(a)
            ys = h / 2 - dist * np.sin(a) - b.rise * h * tt

            if b.fade_from >= 1.0:
                alphas = np.ones(frames)
            else:
                # A fly is either there or it is not -- it never dims. Each
                # picks its own frame to vanish on, so the cloud still thins
                # out, but it thins by LOSING flies rather than by turning the
                # whole swarm grey. A dimmed fly is also the one thing the
                # palette cannot carry: quantisation rounds it to the
                # background and the fly flickers instead of leaving.
                gone = b.fade_from * rnd.uniform(0.85, 1.6)
                alphas = (tt < gone).astype(float)

        jit = rnd.normal(0, b.jitter, (frames, 2))
        if looping:                       # jitter must close too
            jit -= jit.mean(axis=0, keepdims=True)
        xs = xs + np.cumsum(jit[:, 0]) * 0.35
        ys = ys + np.cumsum(jit[:, 1]) * 0.35
        paths.append((xs, ys, alphas))
    return paths


def _fly_colors(b, n):
    """One fixed colour per fly, decided once.

    Drawn per fly rather than per frame: rolling a colour while drawing walks
    the generator only for the flies still alive, so a fly's shade would shift
    every time some other fly vanished.
    """
    rnd = np.random.default_rng(b.seed + 977)
    return [FLY_COLORS[int(rnd.integers(len(FLY_COLORS)))] + (255,)
            for _ in range(n)]


def _draw(paths, i, b, w, h, colors):
    """One frame. Single-pixel flies are stamped, larger ones supersampled.

    Alpha is only ever 0 or 255 -- a fly is present at full colour or absent.
    """
    if b.fly_px <= 1.2:
        # A 1px fly must be a hard pixel. Drawing it antialiased spreads it
        # over four pixels at a quarter alpha each, and after the game's
        # palette quantisation that is either nothing or a smear.
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        px = im.load()
        for (xs, ys, al), c in zip(paths, colors):
            if al[i] <= 0:
                continue
            x, y = int(round(xs[i])), int(round(ys[i]))
            if not (0 <= x < w and 0 <= y < h):
                continue
            px[x, y] = c
        return im

    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    r = b.fly_px / 2.0
    for (xs, ys, al), c in zip(paths, colors):
        if al[i] <= 0:
            continue
        x, y = xs[i], ys[i]
        if x + r < 0 or x - r > w or y + r < 0 or y - r > h:
            continue
        d.ellipse([(x - r) * SS, (y - r) * SS, (x + r) * SS, (y + r) * SS],
                  fill=c)
    # BOX, not LANCZOS -- lanczos ringing puts stray alpha outside the fly,
    # which survives palette quantisation as loose specks.
    im = big.resize((w, h), Image.BOX)

    # Cut the antialiased rim back to hard edges. Downsampling leaves a fly
    # ringed with part-alpha pixels in blended shades, which is the same
    # problem as fading: the palette has one transparent index and no partial
    # coverage, so those pixels either snap to the background or land on some
    # colour the fly never had. Thresholding keeps every drawn pixel at the
    # fly's own colour and full alpha, so a fly is one flat blob throughout.
    a = np.asarray(im.getchannel("A"))
    keep = a >= 110
    rgb = np.asarray(im.convert("RGB")).copy()
    if keep.any():
        # Re-quantise the blended rim to the nearest fly colour it came from.
        pal = np.array([c[:3] for c in colors], dtype=np.int16)
        px = rgb[keep].astype(np.int16)
        idx = np.argmin(((px[:, None, :] - pal[None, :, :]) ** 2).sum(2), axis=1)
        rgb[keep] = pal[idx]
    out = np.dstack([rgb, np.where(keep, 255, 0).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def build(name, b, spd, out_dir):
    frames, w, h = spd["frames"], spd["width"], spd["height"]
    looping = spd.get("flags", 0) == 1
    paths = _fly_paths(b, frames, w, h, looping)
    colors = _fly_colors(b, len(paths))

    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for i in range(frames):
        sheet.paste(_draw(paths, i, b, w, h, colors), (0, i * h))

    png = os.path.join(out_dir, f"{name}.spr.png")
    sheet.save(png)
    with open(os.path.join(out_dir, f"{name}.spr.spd"), "w",
              newline="\r\n") as fh:
        fh.write(f"frames = {frames}\nheight = {h}\nwidth = {w}\n"
                 f"framerate = {spd.get('framerate', 0)}\n"
                 f"flags = {spd.get('flags', 0)}\n")
    return sheet, frames, w, h, looping


def save_gif(sheet, path, frames, h, duration=60):
    imgs = []
    for i in range(frames):
        f = sheet.crop((0, i * h, sheet.width, (i + 1) * h))
        bed = Image.new("RGBA", f.size, (236, 236, 230, 255))
        imgs.append(Image.alpha_composite(bed, f)
                    .convert("P", palette=Image.ADAPTIVE))
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, disposal=2)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="fly_bursts",
                    help="output folder (default ./fly_bursts)")
    ap.add_argument("-s", "--src", default=SRC_DIR,
                    help="folder holding the original .spr.spd files")
    ap.add_argument("--only", nargs="*", help="build only these sprites")
    ap.add_argument("--gif", action="store_true",
                    help="also write an animated preview GIF for each")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    names = args.only or sorted(BURSTS)
    for name in names:
        b = BURSTS[name]
        spd_path = os.path.join(args.src, f"{name}.spr.spd")
        if not os.path.exists(spd_path):
            print(f"  !! no {name}.spr.spd in {args.src}, skipped")
            continue
        spd = read_spd(spd_path)
        sheet, frames, w, h, looping = build(name, b, spd, out_dir)
        print(f"{name:10s} {w:3d}x{h:3d} x{frames:3d}f  "
              f"{b.n:3d} flies @{b.fly_px:g}px"
              f"{'  LOOPING' if looping else ''}")
        if args.gif:
            save_gif(sheet, os.path.join(out_dir, f"{name}.gif"), frames, h)
    print(f"\nwrote {len(names)} sprites to {out_dir}")
