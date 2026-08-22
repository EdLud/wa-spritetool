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

Some of the swarm are fireflies: they glow amber over a stretch of the strip
and are ordinary dark flies the rest of the time. Each one is a `Firefly` in
the FIREFLIES list at the bottom of this file, with its own size, halo and
flash schedule -- edit that list, including down to empty for no glow at all.
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
FRAMES = 128

# --- the swarm ------------------------------------------------------------
N_FLIES = 50
SIZE_RANGE = (2.0, 4.0)     # px, per fly

# Brown. Flies are dark, and at 2px a fly is one blob of colour, so the range
# is what keeps 50 of them from looking like one stamped sprite.
COLOR_DARK = (48, 32, 20)
COLOR_LIGHT = (110, 78, 46)

# --- the fireflies --------------------------------------------------------
# Some of the swarm light up. Each one is a `Firefly` in the FIREFLIES list at
# the bottom of this file -- that list is the thing to edit, and an empty list
# is a perfectly good answer if you want a plain swarm.
#
# Amber, not the obvious lemon yellow, and that is a palette decision rather
# than a taste one. W:A has no alpha: a halo can only exist as opaque colours
# already blended toward the backdrop, so what matters is whether the target
# palette holds real entries along the straight line from the glow colour down
# to the background. Measured against Entomology's build/palette.png over
# #101021 (see ramp_error in make_anim_test.py):
#
#   (246,170,86) amber   mean err  9.6 over 13 distinct entries  <- default
#   (248,215,103) yellow mean err 13.6 over 11, but detours through olive-grey
#                        in the midtones, so the halo goes muddy partway out
#   (242,205,45) gold    mean err 21.8 -- the palette has no saturated yellow
#                        below its brightest, so it bands hard into brown
#
# Per-firefly colour is allowed, but anything far off this amber has to be
# checked against the palette the same way or the halo will band.
FIREFLY_COLOR = (246, 170, 86)      # default lit body and halo


@dataclass
class Firefly:
    """One glowing fly's look and flash schedule.

    Everything here is per firefly, so two of them can differ in size, halo
    reach, falloff shape and when they are lit. Only the flash timing has to
    be thought about as a group: fireflies all lit over the same frames read
    as one blinking cluster, which is why `lit_from` exists.

    size        px across the lit body. Below ~2px there is not enough core to
                anchor a halo and it reads as a smudge rather than an insect.
    glow        halo radius as a multiple of the body's own radius. 0 or 1 is
                no halo at all -- a hard lit dot.
    glow_gamma  falloff shape. 1 = linear, >1 keeps the halo tight around the
                core, <1 spreads it out flat and wide.
    lit_frames  how many frames it stays lit. None = half the strip.
    lit_from    first lit frame. Staggering this across fireflies is what stops
                them flashing in unison.
    color       RGB of the lit body; None = FIREFLY_COLOR.
    glow_color  RGB of the halo; None = whatever `color` resolved to.
    ramp        frames of fade-in at the start and fade-out at the end of the
                lit stretch, so the light arrives and leaves rather than
                popping.
    pulse_period / pulse_depth
                the slow breath underneath. Depth 0 = a steady glow, 1 = one
                that dips to nothing. Varying the period per firefly matters
                more than it looks: identical periods make two fireflies pulse
                in lockstep even when their lit stretches differ.
    """
    size: float = 2.0
    glow: float = 4.0
    glow_gamma: float = 1.7
    lit_frames: Optional[int] = None
    lit_from: int = 0
    color: Optional[Tuple[int, int, int]] = None
    glow_color: Optional[Tuple[int, int, int]] = None
    ramp: int = 12
    pulse_period: float = 26.0
    pulse_depth: float = 0.35

    def level(self, i, frames):
        """How lit this firefly is on frame `i`, 0..1.

        Three things multiplied together: the lit/unlit gate, the ramp at each
        end of the lit stretch, and the slow breath underneath. Keeping them
        separate is what lets the light arrive smoothly and still flicker --
        one curve doing all three would have to trade one against the others.
        """
        lit = frames // 2 if self.lit_frames is None else self.lit_frames
        t = i - self.lit_from
        if t < 0 or t >= lit:
            return 0.0
        # Ends of the lit stretch. min() rather than a branch so a lit stretch
        # shorter than two ramps still opens and closes instead of clipping.
        ramp = float(self.ramp) or 1.0
        env = max(0.0, min(1.0, (t + 1) / ramp, (lit - t) / ramp))
        if not self.pulse_period:
            return env
        # The breath. Offset so it sits between 1 - depth and 1: the firefly
        # dims and recovers, it never goes fully out mid-stretch, because a
        # true zero would read as the glow having ended early.
        breath = 1.0 - self.pulse_depth * 0.5 * (
            1.0 - math.cos(2 * math.pi * t / self.pulse_period))
        return env * breath

    def body(self):
        return self.color or FIREFLY_COLOR

    def halo(self):
        return self.glow_color or self.body()

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
    color: Tuple[int, int, int]     # its brown, worn whenever it is not lit
    xs: np.ndarray                  # position per frame, already wrapped
    ys: np.ndarray
    spec: Optional[Firefly] = None  # None = an ordinary fly, never lit

    def level(self, i, frames):
        """How lit this fly is on frame `i`, 0..1. Always 0 for a plain fly."""
        return 0.0 if self.spec is None else self.spec.level(i, frames)


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
             turn_around_edge=TURN_AROUND_EDGE, spec=None):
    """One fly: a full path plus its look.

    `spec` is a Firefly to make this one glow, or None for an ordinary fly.
    Either way the same random draws happen in the same order, so lighting a
    fly up does not reshuffle the swarm around it.
    """
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
    # Every fly keeps a brown, fireflies included: it is what they wear over
    # the stretch they spend unlit, so when the light goes out one becomes an
    # ordinary member of the swarm rather than a dimmed amber dot.
    color = tuple(int(round(a + (b - a) * t))
                  for a, b in zip(COLOR_DARK, COLOR_LIGHT))
    # Drawn unconditionally and only then overridden, so a firefly consumes the
    # same numbers an ordinary fly would and the rest of the swarm is unmoved.
    size = rnd.uniform(*SIZE_RANGE)
    return Fly(size=size if spec is None else spec.size, color=color,
               xs=pos[:, 0], ys=pos[:, 1], spec=spec)


def render_frame(flies, i, w=FRAME_W, h=FRAME_H,
                 turn_around_edge=TURN_AROUND_EDGE, frames=FRAMES):
    """One transparent frame with every fly drawn at its frame-`i` position."""
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    # "RGBA" mode makes ellipse() alpha-composite onto what is already there
    # instead of replacing the pixel, which a firefly's halo depends on: it is
    # a stack of semi-transparent discs, and a replacing draw would leave only
    # the last one. It is also what lets two overlapping halos add up.
    d = ImageDraw.Draw(big, "RGBA")
    # A bounced path is already inside the frame, so it needs neither the
    # modulo nor the straddling copies -- only a wrapping fly can be cut by an
    # edge and have to appear on the far side at the same time.
    offsets = ((0,),) * 2 if turn_around_edge else ((-w, 0, w), (-h, 0, h))
    # Dim flies first, brightest last, so a halo lays over the swarm rather
    # than having plain brown dots punched through it. Sorting by how lit each
    # fly is on THIS frame rather than by whether it is a firefly at all keeps
    # that true as fireflies come on and go out at different times.
    for f in sorted(flies, key=lambda f: f.level(i, frames)):
        r = f.size / 2.0
        lit = f.level(i, frames)
        spec = f.spec
        color = f.color if lit <= 0 else tuple(
            int(round(a + (b - a) * lit))
            for a, b in zip(f.color, spec.body()))
        outer = r * spec.glow if lit > 0 and spec.glow > 1.0 else r
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
                    halo = spec.halo()
                    steps = max(16, int((outer - r) * SS))
                    for s in range(steps):
                        t = 1.0 - s / float(steps)   # 1 outside -> 0 at core
                        rr = r + (outer - r) * t
                        a = (1.0 - t) ** spec.glow_gamma * lit
                        d.ellipse([(cx - rr) * SS, (cy - rr) * SS,
                                   (cx + rr) * SS, (cy + rr) * SS],
                                  fill=halo + (int(round(a * 255)),))
                d.ellipse([(cx - r) * SS, (cy - r) * SS,
                           (cx + r) * SS, (cy + r) * SS],
                          fill=color + (255,))
    # BOX, not LANCZOS: lanczos rings, and at 2px a ring is a bigger artefact
    # than the fly. See make_anim_test.py for what that costs in W:A.
    return big.resize((w, h), Image.BOX)


def build(n=N_FLIES, frames=FRAMES, w=FRAME_W, h=FRAME_H, seed=5,
          turn_around_edge=TURN_AROUND_EDGE, fireflies=None):
    """The swarm. `fireflies` is a list of Firefly specs, or None for FIREFLIES.

    The first len(fireflies) flies take those specs, one each, and the rest are
    ordinary. Assigning by index rather than at random is what makes the list
    predictable to edit: the second entry is always the second firefly, and
    changing it does not disturb the first.

    A list longer than `n` is an error rather than a silent truncation -- a
    firefly you wrote down and cannot see is worth being told about.
    """
    specs = FIREFLIES if fireflies is None else fireflies
    if len(specs) > n:
        raise ValueError(f"{len(specs)} fireflies but only {n} flies")
    rnd = np.random.default_rng(seed)
    flies = [make_fly(rnd, frames, w, h, turn_around_edge,
                      spec=specs[k] if k < len(specs) else None)
             for k in range(n)]
    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for i in range(frames):
        sheet.paste(render_frame(flies, i, w, h, turn_around_edge, frames),
                    (0, i * h))
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


# --- the arrangement ------------------------------------------------------
# Edit this list to change which flies glow and how. Every field is optional;
# Firefly() alone is a 2px fly with a 4x amber halo, lit for the first half of
# the strip. An empty list is a plain swarm with nothing lit.
#
# The first entries of the swarm take these specs, one each, so three entries
# means three glowing flies out of N_FLIES. The rest stay ordinary.
#
# Worth staggering `lit_from` and varying `pulse_period`: fireflies sharing
# both flash in unison, which reads as a string of lights rather than as
# insects that happen to be near each other.
FIREFLIES = [
    # Firefly(size=1.0, glow=4.0, glow_gamma=1.7, lit_frames=80, lit_from=0),
    # Firefly(size=0.6, glow=5.0, glow_gamma=1.4, lit_frames=60, lit_from=34,
    #         pulse_period=31.0, pulse_depth=0.45),
    # Firefly(size=1.8, glow=3.2, glow_gamma=2.0, lit_frames=50, lit_from=96,
    #         pulse_period=19.0, pulse_depth=0.25),
]


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
    ap.add_argument("--fireflies", type=int, metavar="N",
                    help="use only the first N entries of FIREFLIES, for "
                         "trying fewer without editing the list. 0 = none. "
                         f"Omit for all {len(FIREFLIES)} of them.")
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

    specs = (FIREFLIES if args.fireflies is None
             else FIREFLIES[:max(0, args.fireflies)])

    sheet = build(n=args.flies, frames=args.frames,
                  w=args.width, h=args.height, seed=args.seed,
                  turn_around_edge=args.turn_around_edge, fireflies=specs)
    sheet.save(out)
    print(f"wrote {out} ({sheet.width}x{sheet.height}, "
          f"{args.frames} frames of {args.width}x{args.height})")
    print(f"  {args.flies} flies, {SIZE_RANGE[0]}-{SIZE_RANGE[1]}px")
    if not specs:
        print("  no fireflies")
    for k, s in enumerate(specs):
        lit = args.frames // 2 if s.lit_frames is None else s.lit_frames
        last = min(s.lit_from + lit, args.frames) - 1
        note = "" if last >= s.lit_from else "  <- never lit, past the end"
        print(f"  firefly {k}: {s.size}px #{'%02x%02x%02x' % s.body()} "
              f"glow {s.glow}x gamma {s.glow_gamma}, "
              f"lit frames {s.lit_from}-{last} of {args.frames}, "
              f"pulse {s.pulse_period}f x{s.pulse_depth}{note}")

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
