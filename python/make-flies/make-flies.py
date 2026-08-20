#!/usr/bin/env python3
"""Build a W:A sprite strip of flies milling about.

400x400, 160 frames stacked vertically, ~50 brown flies of 2-4px. At that size
a fly is a couple of pixels, so nothing is drawn from art -- the whole look
lives in the MOTION, and the motion is the only thing worth getting right.

Flies do not drift. They fly in short straight dashes and change direction all
at once, which is what separates a fly from a bee or a piece of debris. So each
fly holds a heading for a few frames, then snaps to a new one:

  dash        a run of DASH_FRAMES at roughly constant velocity
  turn        at the end of a dash the heading jumps by a large random angle
              (TURN_MIN..TURN_MAX degrees), not a smooth curve
  hover       a fraction of dashes are near-stationary jitter instead, so the
              swarm is not uniformly busy

Everything is precomputed as a path per fly before any drawing, because the
turn schedule has to be known in advance for the loop to close: the strip
repeats, so a fly's position and heading on the last frame have to hand over
to the first. `_wrap_path` handles that by making every path cyclic.

Flies wrap at the frame edges rather than bouncing -- a fly leaving the right
edge reappears at the left. Bouncing reads as an insect in a box; wrapping
reads as a swarm that extends past the frame.

One fly in the swarm is a firefly: it glows amber over the first half of the
strip and is an ordinary dark fly for the second half. See FIREFLY_* below.
"""
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# --- sheet geometry -------------------------------------------------------
FRAME_W = 400
FRAME_H = 400
FRAMES = 160

# --- the swarm ------------------------------------------------------------
N_FLIES = 10
SIZE_RANGE = (2.0, 4.0)     # px, per fly

# Brown. Flies are dark, and at 2px a fly is one blob of colour, so the range
# is what keeps 50 of them from looking like one stamped sprite.
COLOR_DARK = (48, 32, 20)
COLOR_LIGHT = (110, 78, 46)

# --- the firefly ----------------------------------------------------------
# One fly of the swarm lights up. It is lit for the FIRST HALF of the strip and
# is an ordinary dark fly for the rest, so over a loop it reads as an insect
# that flashes rather than a lamp that is always on.
#
# Amber, not the obvious lemon yellow, and that is a palette decision rather
# than a taste one. W:A has no alpha: a halo can only exist as opaque colours
# already blended toward the backdrop, so what matters is whether the target
# palette holds real entries along the straight line from the glow colour down
# to the background. Measured against Entomology's build/palette.png over
# #101021 (see ramp_error in make_anim_test.py):
#
#   (246,170,86) amber   mean err  9.6 over 13 distinct entries  <- this
#   (248,215,103) yellow mean err 13.6 over 11, but detours through olive-grey
#                        in the midtones, so the halo goes muddy partway out
#   (242,205,45) gold    mean err 21.8 -- the palette has no saturated yellow
#                        below its brightest, so it bands hard into brown
FIREFLY_COLOR = (246, 170, 86)      # the lit body itself
FIREFLY_GLOW_COLOR = (246, 170, 86) # the halo next to it
FIREFLY_LIT_FRAMES = 80             # frames lit, counted from frame 0

FIREFLY_GLOW = 4.0          # halo radius as a multiple of the fly's own radius
FIREFLY_GLOW_GAMMA = 1.7    # falloff shape; >1 keeps the halo tight to the core
FIREFLY_SIZE = 2.0          # px. Fixed rather than random -- a firefly that
                            # rolled 2px would be too small to carry a halo.

# The glow does not simply switch off at FIREFLY_LIT_FRAMES. It ramps up over
# the first few frames and back down over the last few, so the light arrives
# and leaves rather than popping, and it breathes on a slow cycle underneath.
FIREFLY_RAMP = 12           # frames of fade-in at the start and fade-out at the
                            # end of the lit stretch
FIREFLY_PULSE_PERIOD = 26   # frames per breath
FIREFLY_PULSE_DEPTH = 0.35  # how far the breath dips, 0 = steady, 1 = to black

# --- motion ---------------------------------------------------------------
SPEED_RANGE = (2.2, 5.5)    # px per frame while dashing
DASH_FRAMES = (4, 14)       # frames held on one heading before a turn
TURN_MIN, TURN_MAX = 35, 165   # degrees a heading jumps at a turn
HOVER_CHANCE = 0.28         # fraction of dashes spent hovering instead
HOVER_SPEED = 0.45          # px per frame while hovering
JITTER = 0.55               # px of per-frame noise on top of everything

# --- rise -----------------------------------------------------------------
# The game sinks this sprite as it plays, so a fly that holds its height on
# screen has to climb through the strip to pay for it. In canvas heights over
# the whole 160 frames: 0.5 exactly cancels the sink (the fly appears to hold
# station), 1.0 leaves it climbing at the same rate it sinks, 0 lets it fall.
#
# The range is deliberately BELOW the 0.5 break-even, so the swarm drifts
# gently downward overall rather than hanging at a fixed height -- randomising
# it is also what stops 50 flies from moving as one sheet.
RISE_RANGE = (0.3, 0.5)

# False: a fly that leaves one edge reappears at the opposite one, so the
#        swarm reads as part of a larger cloud continuing past the frame.
# True:  a fly bounces off the edges and stays inside for the whole strip,
#        which reads as insects in a container. Note this contains the rise
#        too -- a fly climbing 0.3-0.5 canvas heights now bounces off the top
#        instead of wrapping under, so the upward drift turns into pacing.
TURN_AROUND_EDGE = True

EDGE_MARGIN = 2.0           # px inside the border a fly turns at, so it does
                            # not clip through while its dot is still drawn

SS = 8                      # supersample factor, as in the other strips


@dataclass
class Fly:
    size: float
    color: Tuple[int, int, int]
    xs: np.ndarray          # position per frame, already wrapped
    ys: np.ndarray
    firefly: bool = False
    lit_frames: int = FIREFLY_LIT_FRAMES
    dark_color: Optional[Tuple[int, int, int]] = None   # its unlit brown

    def glow_level(self, i):
        """How lit the firefly is on frame `i`, 0..1. 0 for an ordinary fly.

        Three things multiplied together: the lit/unlit gate, the ramp at each
        end of the lit stretch, and the slow breath underneath. Keeping them
        separate is what lets the light arrive smoothly and still flicker --
        one curve doing all three would have to trade one against the others.
        """
        if not self.firefly or i >= self.lit_frames:
            return 0.0
        # Ends of the lit stretch. min() rather than a branch so a lit stretch
        # shorter than two ramps still opens and closes instead of clipping.
        ramp = float(FIREFLY_RAMP) or 1.0
        env = min(1.0, (i + 1) / ramp, (self.lit_frames - i) / ramp)
        env = max(0.0, env)
        # The breath. Offset so it sits between 1 - depth and 1: the firefly
        # dims and recovers, it never goes fully out mid-stretch, because a
        # true zero would read as the glow having ended early.
        breath = 1.0 - FIREFLY_PULSE_DEPTH * 0.5 * (
            1.0 - math.cos(2 * math.pi * i / FIREFLY_PULSE_PERIOD))
        return env * breath


def _wrap_path(steps, frames):
    """Strip the net drift out of a per-frame velocity list.

    A fly's wandering has to average to nothing or it walks off in whatever
    direction its random turns happened to favour. Subtracting the mean from
    every step does that without touching the character of the motion: the
    dashes and turns are untouched, the path just no longer has a net
    direction.

    The rise is added AFTER this, on purpose -- it is the one piece of net
    movement the fly is meant to keep, so it must not be averaged away here.
    """
    steps = np.asarray(steps[:frames], dtype=np.float64)
    return steps - steps.mean(axis=0, keepdims=True)


def _reflect_path(steps, x, y, w, h, margin=EDGE_MARGIN):
    """Integrate `steps` from (x, y), turning the fly around at the borders.

    The reflection has to happen HERE rather than when drawing, because a
    bounce changes where every later step lands -- clamping positions after the
    fact would pile flies up along the edges instead of sending them back.

    A component that would cross a border is negated for the rest of the path,
    which turns the fly rather than merely stopping it. The step is then
    replayed from the border, so a fly that hits at a shallow angle keeps its
    speed instead of losing the remainder of that frame's travel.
    """
    steps = np.asarray(steps, dtype=np.float64).copy()
    lo_x, hi_x = margin, w - margin
    lo_y, hi_y = margin, h - margin
    # A fly seeded outside the playable box would bounce on frame 0 and read as
    # a fly stuck to the wall, so start it in bounds.
    x = min(max(x, lo_x), hi_x)
    y = min(max(y, lo_y), hi_y)

    out = np.empty_like(steps)
    for i in range(len(steps)):
        dx, dy = steps[i]
        nx, ny = x + dx, y + dy
        if nx < lo_x or nx > hi_x:
            steps[i:, 0] *= -1
            nx = lo_x + (lo_x - nx) if nx < lo_x else hi_x - (nx - hi_x)
            nx = min(max(nx, lo_x), hi_x)
        if ny < lo_y or ny > hi_y:
            steps[i:, 1] *= -1
            ny = lo_y + (lo_y - ny) if ny < lo_y else hi_y - (ny - hi_y)
            ny = min(max(ny, lo_y), hi_y)
        x, y = nx, ny
        out[i] = (x, y)
    return out


def make_fly(rnd, frames=FRAMES, w=FRAME_W, h=FRAME_H,
             turn_around_edge=TURN_AROUND_EDGE, firefly=False):
    """One fly: a full path plus its look."""
    steps = []
    heading = rnd.uniform(0, 2 * math.pi)
    while len(steps) < frames:
        hovering = rnd.random() < HOVER_CHANCE
        n = int(rnd.integers(*DASH_FRAMES))
        speed = (HOVER_SPEED if hovering
                 else rnd.uniform(*SPEED_RANGE))
        for _ in range(n):
            if hovering:
                # hover is not a slow dash -- it is directionless jitter, so
                # the heading is rerolled every frame rather than held
                a = rnd.uniform(0, 2 * math.pi)
            else:
                a = heading
            steps.append((math.cos(a) * speed, math.sin(a) * speed))
        # the turn: a jump, not a curve
        delta = math.radians(rnd.uniform(TURN_MIN, TURN_MAX))
        heading += delta * rnd.choice([-1, 1])

    steps = _wrap_path(steps, frames)
    steps += rnd.normal(0, JITTER, steps.shape)
    steps -= steps.mean(axis=0, keepdims=True)      # jitter must close too

    # Climb, in canvas heights across the strip. Negative y is up. Applied
    # after the drift has been cancelled so it is not cancelled with it.
    rise = rnd.uniform(*RISE_RANGE)
    steps[:, 1] -= rise * h / frames

    if turn_around_edge:
        pos = _reflect_path(steps, rnd.uniform(0, w), rnd.uniform(0, h), w, h)
    else:
        pos = np.cumsum(steps, axis=0)
        pos[:, 0] += rnd.uniform(0, w)
        pos[:, 1] += rnd.uniform(0, h)

    t = rnd.random()
    color = tuple(int(round(a + (b - a) * t))
                  for a, b in zip(COLOR_DARK, COLOR_LIGHT))
    # The firefly keeps a brown of its own for the half of the strip it spends
    # unlit, so when the light goes out it becomes one of the swarm rather than
    # a dimmed amber dot.
    return Fly(size=FIREFLY_SIZE if firefly else rnd.uniform(*SIZE_RANGE),
               color=color, xs=pos[:, 0], ys=pos[:, 1],
               firefly=firefly, dark_color=color)


def render_frame(flies, i, w=FRAME_W, h=FRAME_H,
                 turn_around_edge=TURN_AROUND_EDGE):
    """One transparent frame with every fly drawn at its frame-`i` position."""
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    # "RGBA" mode makes ellipse() alpha-composite onto what is already there
    # instead of replacing the pixel, which the firefly's halo depends on: it
    # is a stack of semi-transparent discs, and a replacing draw would leave
    # only the last one.
    d = ImageDraw.Draw(big, "RGBA")
    # A bounced path is already inside the frame, so it needs neither the
    # modulo nor the straddling copies -- only a wrapping fly can be cut by an
    # edge and have to appear on the far side at the same time.
    offsets = ((0,),) * 2 if turn_around_edge else ((-w, 0, w), (-h, 0, h))
    # Flies first, firefly last, so its halo lays over the swarm rather than
    # having plain brown dots punched through it.
    for f in sorted(flies, key=lambda f: f.firefly):
        r = f.size / 2.0
        lit = f.glow_level(i)
        color = f.color if lit <= 0 else tuple(
            int(round(a + (b - a) * lit))
            for a, b in zip(f.dark_color or f.color, FIREFLY_COLOR))
        outer = r * FIREFLY_GLOW if lit > 0 else r
        x = f.xs[i] % w if not turn_around_edge else f.xs[i]
        y = f.ys[i] % h if not turn_around_edge else f.ys[i]
        for ox in offsets[0]:
            for oy in offsets[1]:
                cx, cy = x + ox, y + oy
                if (cx + outer < 0 or cx - outer > w
                        or cy + outer < 0 or cy - outer > h):
                    continue
                if lit > 0 and outer > r:
                    # Concentric rings from the outside in, one per
                    # supersampled pixel of halo width. Drawing outermost-first
                    # and letting each disc paint over the last is what makes a
                    # gradient rather than a stack of visible bands.
                    steps = max(16, int((outer - r) * SS))
                    for s in range(steps):
                        t = 1.0 - s / float(steps)   # 1 outside -> 0 at core
                        rr = r + (outer - r) * t
                        a = (1.0 - t) ** FIREFLY_GLOW_GAMMA * lit
                        d.ellipse([(cx - rr) * SS, (cy - rr) * SS,
                                   (cx + rr) * SS, (cy + rr) * SS],
                                  fill=FIREFLY_GLOW_COLOR
                                  + (int(round(a * 255)),))
                d.ellipse([(cx - r) * SS, (cy - r) * SS,
                           (cx + r) * SS, (cy + r) * SS],
                          fill=color + (255,))
    # BOX, not LANCZOS: lanczos rings, and at 2px a ring is a bigger artefact
    # than the fly. See make_anim_test.py for what that costs in W:A.
    return big.resize((w, h), Image.BOX)


def build(n=N_FLIES, frames=FRAMES, w=FRAME_W, h=FRAME_H, seed=5,
          turn_around_edge=TURN_AROUND_EDGE, fireflies=1,
          lit_frames=FIREFLY_LIT_FRAMES):
    """The swarm. The first `fireflies` of them glow for `lit_frames` frames.

    Which fly is the firefly is decided by index rather than at random, so the
    same seed gives the same swarm whether or not anything is lit -- the flies
    are built in the same order and draw the same numbers either way.
    """
    rnd = np.random.default_rng(seed)
    flies = [make_fly(rnd, frames, w, h, turn_around_edge,
                      firefly=(k < fireflies)) for k in range(n)]
    for f in flies:
        f.lit_frames = min(lit_frames, frames)
    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for i in range(frames):
        sheet.paste(render_frame(flies, i, w, h, turn_around_edge), (0, i * h))
    return sheet


def save_gif(sheet, path, frames=FRAMES, h=FRAME_H, duration=33,
             bed_rgb=(232, 232, 226)):
    """Preview the strip as a GIF.

    `bed_rgb` is what the frames are flattened onto, and it is worth setting:
    the default pale bed is there so a dark fly shows, but a firefly's halo can
    only be judged against the backdrop it will actually sit on, which for
    these sprites is the dark #101021. Pass that to look at the glow.
    """
    imgs = []
    for i in range(frames):
        f = sheet.crop((0, i * h, sheet.width, (i + 1) * h))
        bed = Image.new("RGBA", f.size, tuple(bed_rgb) + (255,))
        imgs.append(Image.alpha_composite(bed, f)
                    .convert("P", palette=Image.ADAPTIVE))
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, disposal=2)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="flies.png",
                    help="output PNG strip (default ./flies.png)")
    ap.add_argument("-N", "--flies", type=int, default=N_FLIES)
    ap.add_argument("-n", "--frames", type=int, default=FRAMES)
    ap.add_argument("-W", "--width", type=int, default=FRAME_W)
    ap.add_argument("-H", "--height", type=int, default=FRAME_H)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--turn-around-edge", action="store_true",
                    default=TURN_AROUND_EDGE,
                    help="bounce off the frame edges instead of wrapping")
    ap.add_argument("--fireflies", type=int, default=1,
                    help="how many of the flies glow (default 1)")
    ap.add_argument("--lit-frames", type=int, default=FIREFLY_LIT_FRAMES,
                    help="frames a firefly stays lit, from frame 0 "
                         f"(default {FIREFLY_LIT_FRAMES})")
    ap.add_argument("--dark-bed", action="store_true",
                    help="render the preview GIF over the #101021 backdrop "
                         "the sprite really sits on, so the glow reads")
    ap.add_argument("--spd", action="store_true",
                    help="also write the .spd beside the PNG")
    ap.add_argument("--gif", metavar="PATH", nargs="?", const="",
                    help="also write an animated preview GIF")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)

    sheet = build(n=args.flies, frames=args.frames,
                  w=args.width, h=args.height, seed=args.seed,
                  turn_around_edge=args.turn_around_edge,
                  fireflies=args.fireflies, lit_frames=args.lit_frames)
    sheet.save(out)
    print(f"wrote {out} ({sheet.width}x{sheet.height}, "
          f"{args.frames} frames of {args.width}x{args.height})")
    print(f"  {args.flies} flies, {SIZE_RANGE[0]}-{SIZE_RANGE[1]}px")
    if args.fireflies:
        lit = min(args.lit_frames, args.frames)
        print(f"  {args.fireflies} firefly, "
              f"#{'%02x%02x%02x' % FIREFLY_COLOR} lit frames 0-{lit - 1} "
              f"of {args.frames}, glow {FIREFLY_GLOW}x radius")

    if args.spd:
        spd = os.path.splitext(out)[0] + ".spd"
        with open(spd, "w", newline="\r\n") as fh:
            fh.write(f"frames = {args.frames}\nheight = {args.height}\n"
                     f"width = {args.width}\nframerate = 0\nflags = 1\n")
        print(f"wrote {spd}")

    if args.gif is not None:
        gif = args.gif or os.path.splitext(out)[0] + ".gif"
        save_gif(sheet, gif, frames=args.frames, h=args.height,
                 bed_rgb=(0x10, 0x10, 0x21) if args.dark_bed
                 else (232, 232, 226))
        print(f"wrote {gif}")
