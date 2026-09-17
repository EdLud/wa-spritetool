#!/usr/bin/env python3
"""Build a W:A sprite strip of a bonfire, one per team colour.

40x60, 200 frames stacked vertically: a stack of logs that never moves and a
flame that never repeats a shape. The logs are drawn once and pasted into
every frame; everything interesting is the fire.

A flame is not a shape that wobbles. It is a column of hot gas that is buoyant
at the bottom, breaks up as it rises, and stops existing partway up -- so the
model here is a field rather than a sprite:

  emit        the fire bed spawns hot cells across the log stack's width,
              weighted toward the middle where the coals are
  rise        each cell accelerates upward, because hotter gas is lighter,
              and cools as it goes
  wander      a slow horizontal drift, shared by neighbours, so a lick of
              flame leans as one piece rather than sparkling independently
  die         a cell is gone once it is cold, which is what gives the flame
              its height without ever drawing an outline

The heat field is blurred once and mapped through a ramp, which is what
makes it read as fire rather than as a cloud: a white core where the gas is
hottest, the team colour through the body, and a deep rim at the edge where
it is cooling out. A ramp rather than flat bands because a flame has no
bands -- it is a temperature gradient, and the eye reads the smoothness as
heat. The reference sprite this is modelled on spends about 20 of its 62
colours on exactly that.

The taper is the other half of the silhouette. A cell's heat falls off as a
power of its remaining life, and that exponent decides whether the fire ends
in a tip or spreads into a mushroom: old cells wander widest, so if they are
still carrying heat when they get there the top of the flame blooms.

Sparks are separate. They are single cells that detach from the top of the
flame and keep rising, and they matter more than they look: a fire without
them reads as a gas jet, because nothing crosses the boundary between the
flame and the air.

THE LOOP IS THE HARD PART. A strip repeats, so frame 199 has to hand over to
frame 0 with no visible seam. Rather than trying to fade one into the other,
the noise driving the wander is sampled around a circle -- `_loop_noise` --
so it is periodic by construction. The cells themselves are simulated for a
full extra lap before the first kept frame, so the field is already in its
steady state when recording starts and there is no thin-flame moment at the
loop point.

Six colours, `--team all`, one file each. They differ in more than hue: a
flame's colour comes from its temperature, so the hotter-looking teams get a
taller, thinner, faster fire and the cooler ones a squatter, lazier one. See
TEAMS at the bottom of this file -- that list is the thing to edit.
"""
import math
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import colors                                                    # noqa: E402
import gif                                                       # noqa: E402

# --- sheet geometry -------------------------------------------------------
FRAME_W = 40
FRAME_H = 60
FRAMES = 200

# --- the fire bed ---------------------------------------------------------
#: How wide the coals are, as a fraction of the frame. The flame is born
#: across this and narrows on its own as cells rise and die.
BED_W = 0.62
#: Where the top of the log stack sits, from the bottom of the frame.
BED_Y = 11

#: Cells emitted per frame. The single biggest knob on how solid the fire
#: looks: too few and it is a handful of licks, too many and it is a blob.
EMIT = 30
#: How long a cell lives, in frames, before it is cold. Scaled per team.
LIFE = (12.0, 26.0)
#: Upward speed at birth, and the buoyancy that keeps adding to it.
RISE = (0.55, 1.05)
BUOYANCY = 0.075
#: How far a cell wanders sideways per frame at full strength.
WANDER = 0.48
#: How much of the wander is shared with neighbours. High values make licks
#: that lean as one body; low values make every cell wander alone, which
#: reads as static rather than fire.
COHERENCE = 0.62
#: How much more freely a cell wanders once it is old and high. This is what
#: turns a rising column into licks that curl off the top -- at 0 the flame
#: is a plume, and the higher it goes the more the top frays.
TURBULENCE = 1.9

# --- sparks ---------------------------------------------------------------
SPARKS = 9
#: Short lives on purpose. A spark that survives to the top of the frame
#: reads as snow: what sells it is a brief bright fleck near the flame that
#: goes out, not a particle that makes the whole journey.
SPARK_LIFE = (6.0, 13.0)
SPARK_RISE = (0.26, 0.52)

# --- the logs -------------------------------------------------------------
#: Browns for the log stack, darkest first. Snapped to the gfx palette like
#: everything else, so they survive the game's fixed table.
LOG_DARK = (52, 24, 10)
LOG_MID = (118, 64, 30)
LOG_LIT = (176, 104, 44)
#: How strongly the coals under the flame glow with the team colour. The logs
#: are the one part that says the fire is sitting on something burning.
EMBER = 0.55


def _loop_noise(rng, frames: int, octaves: int = 3) -> np.ndarray:
    """A smooth 1-D signal over `frames` that joins back to its own start.

    Built from sines at integer frequencies rather than from filtered noise:
    an integer number of cycles across the strip is periodic by construction,
    so the loop closes exactly and does not have to be crossfaded. Three
    octaves is enough to stop it reading as a single sine wave.
    """
    t = np.arange(frames) * (2.0 * math.pi / frames)
    out = np.zeros(frames)
    for k in range(1, octaves + 1):
        phase = rng.uniform(0, 2 * math.pi)
        out += np.sin(k * t + phase) / k
    return out / np.abs(out).max()


def _blur(field: np.ndarray, passes: int = 1) -> np.ndarray:
    """Soften the heat field with a small separable box blur.

    Cheap on purpose -- this runs once per frame over a 40x60 grid, and a
    proper gaussian would buy nothing the thresholding does not throw away.
    """
    out = field
    for _ in range(passes):
        p = np.pad(out, 1, mode='edge')
        out = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]
               + 2.0 * out) / 6.0
    return out


@dataclass
class Team:
    """One team's fire.

    `body` is the colour the flame reads as, and the rest is the physical
    difference that goes with it: a blue flame is hotter, so it is thinner,
    faster and taller than a red one. Setting only the colour and leaving the
    shape alone gives six identical fires in six hues, which is exactly what
    this is meant to avoid.
    """
    name: str
    body: Tuple[int, int, int]
    #: The cooling edge, where the flame is turning to smoke.
    rim: Tuple[int, int, int]
    #: Multiplies cell lifetime, so >1 is a taller flame.
    height: float = 1.0
    #: Multiplies the width of the bed, so <1 is a thinner flame.
    width: float = 1.0
    #: Multiplies rise speed, so >1 is a busier, flickier fire.
    speed: float = 1.0
    #: How much of the flame is white-hot core, 0..1 of its heat range.
    core: float = 0.62


class Cell:
    """One parcel of hot gas. Not drawn -- it deposits heat into a field."""

    __slots__ = ('x', 'y', 'vx', 'vy', 'age', 'life', 'lane', 'heat')

    def __init__(self, x, y, vy, life, lane, heat):
        self.x, self.y = x, y
        self.vx, self.vy = 0.0, vy
        self.age, self.life = 0.0, life
        self.lane = lane
        self.heat = heat


def _spawn(rng, team, w, h, lanes):
    """A new cell at the fire bed, biased toward the middle of the coals."""
    half = w * BED_W * team.width * 0.5
    # Two draws averaged, so the middle of the bed is favoured without the
    # hard cut a clamped gaussian gives at the edges.
    off = (rng.uniform(-1, 1) + rng.uniform(-1, 1)) * 0.5
    x = w * 0.5 + off * half
    life = rng.uniform(*LIFE) * team.height
    vy = -rng.uniform(*RISE) * team.speed
    # Hotter in the middle: the edges of the bed never reach the white core,
    # which is what keeps the core a spine rather than a slab.
    heat = 1.0 - abs(off) * 0.45
    return Cell(x, float(h - BED_Y), vy, life, rng.randrange(lanes), heat)


def _step(cells, rng, team, w, h, lanes, drift, frame):
    """Advance every cell one frame, and drop the ones that are cold."""
    alive = []
    for c in cells:
        c.age += 1.0
        if c.age >= c.life:
            continue
        # Buoyancy: hot gas keeps accelerating upward rather than coasting,
        # and that acceleration is most of why a flame looks like a flame.
        c.vy -= BUOYANCY * team.speed
        # Shared drift per lane, plus a little of its own. The lane is what
        # makes neighbouring cells lean together into a lick.
        own = rng.uniform(-1, 1) * (1.0 - COHERENCE)
        # Sideways drift grows with height: near the coals the gas is
        # constrained by the fuel bed, higher up nothing holds it, which is
        # why a flame is a column at the bottom and licks at the top.
        loose = 1.0 + (c.age / c.life) * TURBULENCE
        c.vx = (drift[c.lane, frame] * COHERENCE + own) * WANDER * loose
        c.x += c.vx
        c.y += c.vy
        if -2 <= c.x < w + 2 and c.y > -4:
            alive.append(c)
    return alive


def _deposit(cells, w, h):
    """Splat the cells' heat into a field, so the flame has a body.

    A cell is a point; the fire is what a lot of points add up to. Each one
    is spread over the four pixels it sits between, so a slow-moving cell
    does not flicker between whole pixels as it crosses a boundary.
    """
    field = np.zeros((h, w))
    for c in cells:
        # Cooling: a cell contributes less the older it is, which is what
        # makes the top of the flame thin out instead of ending in a line.
        # The exponent is the taper: too low and old wandering cells still
        # carry enough heat to spread the top into a mushroom, too high and
        # the flame is a stub with no licks at all.
        left = 1.0 - (c.age / c.life)
        heat = c.heat * left ** 2.4
        x0, y0 = int(math.floor(c.x)), int(math.floor(c.y))
        fx, fy = c.x - x0, c.y - y0
        for dy, wy in ((0, 1.0 - fy), (1, fy)):
            yy = y0 + dy
            if not 0 <= yy < h:
                continue
            for dx, wx in ((0, 1.0 - fx), (1, fx)):
                xx = x0 + dx
                if 0 <= xx < w:
                    field[yy, xx] += heat * wy * wx
    return field


def _logs(w, h, team, palette):
    """The log stack, drawn once and pasted into every frame.

    Four logs seen end-on and splayed like a hand of cards, which is the
    shape the reference sprite uses and the one that reads as a campfire at
    40px: what makes it legible is the round END of each log, not its
    length. So each is drawn as a short thick bar with a lighter disc at the
    outer end, and the two at the back sit higher and darker so the stack
    has a front and a back rather than lying flat.

    Static, deliberately: real logs do not move, and animating them would
    pull the eye off the only thing here worth watching.
    """
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    px = im.load()
    mid = w * 0.5
    dark, lit = _snap(LOG_DARK, palette), _snap(LOG_LIT, palette)
    body = _snap(LOG_MID, palette)
    ember = _snap(tuple(int(LOG_MID[i] + (team.body[i] - LOG_MID[i]) * EMBER)
                        for i in range(3)), palette)

    def bar(cx, cy, dx, dy, half, thick, shade, cap):
        """One log: a bar from the middle outward, with a disc on its end."""
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        # Perpendicular, for the thickness.
        nx, ny = -uy, ux
        for t in range(int(half * 2)):
            s = t * 0.5
            bx, by = cx + ux * s, cy + uy * s
            for u in np.arange(-thick, thick + 0.01, 0.5):
                x, y = int(bx + nx * u), int(by + ny * u)
                if 0 <= x < w and 0 <= y < h:
                    # Lighter along the top edge of the bar, so a round log
                    # reads as round rather than as a painted stick.
                    px[x, y] = (shade if u > -thick * 0.35 else cap) + (255,)
        # The end grain: the disc that says "log" at this size.
        ex, ey = cx + ux * half, cy + uy * half
        r = thick + 0.6
        for y in range(int(ey - r - 1), int(ey + r + 2)):
            for x in range(int(ex - r - 1), int(ex + r + 2)):
                if not (0 <= x < w and 0 <= y < h):
                    continue
                d = math.hypot(x + 0.5 - ex, y + 0.5 - ey)
                if d <= r:
                    px[x, y] = (cap if d > r - 1.0 else shade) + (255,)

    base = h - 4
    # Two crossed pairs, leaning out and down like a laid campfire. The
    # back pair is drawn first and darker so the front pair overlaps it;
    # at 40px that overlap is the entire depth cue.
    for lean in (-1, 1):
        bar(mid + lean * 1.5, base - 3.0, lean * 6.0, -2.2, 7.0, 1.5,
            body, dark)
    for lean in (-1, 1):
        bar(mid + lean * 1.0, base - 0.5, lean * 7.0, 1.4, 7.5, 1.7,
            lit, body)
    return im


def _snap(rgb, palette):
    """The nearest colour the game will actually paint, or `rgb` unchanged."""
    if palette is None:
        return tuple(int(v) for v in rgb)
    return palette[colors.nearest(tuple(int(v) for v in rgb), palette)]


#: Below this fraction of the frame's peak heat there is no flame at all.
#: The one number that decides how far the fire reaches: too low and the
#: flame is a haze with no edge, too high and it is a hard-edged blob.
EDGE = 0.065

#: How many steps the flame ramp holds. The reference sprite uses about 20
#: for its flame, which is what lets a 40px fire look rounded rather than
#: cut from paper; a free-palette sprite can afford them.
RAMP = 18


def _ramp(team, palette):
    """The flame's colours, hottest first.

    A real flame has no bands -- it is a temperature gradient, and the eye
    reads the smoothness as heat. So this is a ramp through four anchors
    rather than four flat colours: near-white core, the team's bright hue,
    its saturated body, and the deep rim where it cools out. Snapping only
    happens when a fixed table is asked for, and then the ramp is squashed
    onto whatever that table actually holds.
    """
    body = team.body
    hot = tuple(min(255, int(v + (255 - v) * 0.55)) for v in body)
    deep = tuple(int(v * 0.55) for v in body)
    anchors = [(244, 252, 253), hot, body, deep, team.rim]
    out = []
    per = max(1, RAMP // (len(anchors) - 1))
    for a, b in zip(anchors, anchors[1:]):
        out.extend(colors.ramp(a, b, per + 1)[:-1])
    out.append(anchors[-1])
    if palette is not None:
        out = [_snap(c, palette) for c in out]
    return out


def build(team: Team, frames: int = FRAMES, w: int = FRAME_W,
          h: int = FRAME_H, seed: int = 7, emit: int = EMIT,
          sparks: int = SPARKS, palette: Optional[Sequence] = None):
    """Render one team's fire as a vertical strip of `frames`."""
    import random
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)

    lanes = 5
    drift = np.stack([_loop_noise(nrng, frames) for _ in range(lanes)])

    band = _ramp(team, palette)
    logs = _logs(w, h, team, palette)

    cells: List[Cell] = []
    spark_cells: List[Cell] = []

    # A full lap before recording, so the field is in its steady state when
    # the first kept frame is drawn -- otherwise the loop point shows a
    # flame that is visibly thinner than the one before it.
    sheet = Image.new('RGBA', (w, h * frames), (0, 0, 0, 0))
    for i in range(-frames, frames):
        f = i % frames
        for _ in range(emit):
            cells.append(_spawn(rng, team, w, h, lanes))
        cells = _step(cells, rng, team, w, h, lanes, drift, f)

        # Sparks are thrown from the upper flame, not from the bed: they are
        # bits that have already burned and broken away.
        if rng.random() < sparks / 12.0 and cells:
            c = rng.choice(cells)
            if c.y < h - BED_Y - 8:
                s = Cell(c.x, c.y, -rng.uniform(*SPARK_RISE) * team.speed,
                         rng.uniform(*SPARK_LIFE), c.lane, 1.0)
                spark_cells.append(s)
        spark_cells = _step(spark_cells, rng, team, w, h, lanes, drift, f)

        if i < 0:
            continue

        heat = _blur(_deposit(cells, w, h))
        frame = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        px = frame.load()
        # Mapped through the ramp rather than cut into bands. The scale is
        # the hottest cell in this frame rather than a fixed number, so the
        # flame keeps a white spine even in its quiet moments instead of
        # dimming to a stub whenever the emitter has a slow frame.
        top = max(heat.max(), 1e-6)
        last = len(band) - 1
        for y in range(h):
            row = heat[y]
            for x in range(w):
                v = row[x] / top
                if v < EDGE:
                    continue
                # Gamma below one widens the hot end, which is where the
                # detail an eye reads as fire actually lives.
                t = ((v - EDGE) / (1.0 - EDGE)) ** 0.65
                px[x, y] = band[last - min(last, int(t * last + 0.5))] + (255,)

        for s in spark_cells:
            x, y = int(s.x), int(s.y)
            if 0 <= x < w and 0 <= y < h:
                # Sparks cool as they go, and are drawn from the same ramp
                # as the flame so they belong to the same fire. Never the
                # white core: a spark is a fleck that has LEFT the hot part,
                # and pure white makes it read as a star instead.
                t = min(0.95, 0.25 + s.age / s.life)
                px[x, y] = band[min(len(band) - 1,
                                    int(t * (len(band) - 1)))] + (255,)

        frame.alpha_composite(logs)
        sheet.paste(frame, (0, f * h))
    return sheet


# --- the six teams --------------------------------------------------------
# Colour and physics together: a blue fire is hotter, so it is taller and
# thinner and flickers faster than the red one. Edit this list.
TEAMS = [
    Team('red', (250, 66, 18), (128, 22, 8),
         height=0.94, width=1.08, speed=0.90, core=0.66),
    Team('blue', (26, 96, 252), (10, 22, 140),
         height=1.12, width=0.92, speed=1.14, core=0.58),
    Team('green', (74, 236, 62), (18, 104, 26),
         height=1.02, width=1.00, speed=1.02, core=0.62),
    Team('yellow', (252, 216, 40), (168, 92, 10),
         height=0.98, width=1.06, speed=0.96, core=0.70),
    Team('pink', (250, 84, 214), (128, 20, 108),
         height=1.06, width=0.96, speed=1.06, core=0.60),
    Team('cyan', (48, 232, 244), (12, 106, 132),
         height=1.16, width=0.90, speed=1.18, core=0.56),
]

BY_NAME = {t.name: t for t in TEAMS}


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-o', '--out', default='bonfire.png',
                    help='where to write; with --team all this is the stem, '
                         'and each file gets its team name')
    ap.add_argument('-t', '--team', default='all',
                    choices=['all'] + [t.name for t in TEAMS],
                    help='which team colour, or all six')
    ap.add_argument('-n', '--frames', type=int, default=FRAMES)
    ap.add_argument('-W', '--width', type=int, default=FRAME_W)
    ap.add_argument('-H', '--height', type=int, default=FRAME_H)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--emit', type=int, default=EMIT,
                    help='cells emitted per frame; how solid the fire looks')
    ap.add_argument('--sparks', type=int, default=SPARKS,
                    help='0 for none')
    ap.add_argument('--palette', default='gfx0',
                    choices=['gfx0', 'gfx1', 'none'],
                    help="snap to the game's fixed table for that slot. "
                         "'none' keeps the colours as written, which is only "
                         "right for art the game will not recolour")
    ap.add_argument('--spd', action='store_true',
                    help='also write the .spd beside each PNG')
    ap.add_argument('--gif', metavar='PATH', nargs='?', const='',
                    help='also write an animated preview GIF; with a path '
                         'when you want it somewhere particular, which is '
                         'what the preview runner does')
    args = ap.parse_args()

    palette = {'gfx0': colors.GFX0, 'gfx1': colors.GFX1,
               'none': None}[args.palette]

    out = os.path.abspath(args.out)
    if os.path.dirname(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)

    wanted = TEAMS if args.team == 'all' else [BY_NAME[args.team]]
    stem, ext = os.path.splitext(out)
    ext = ext or '.png'

    for team in wanted:
        path = f'{stem}_{team.name}{ext}' if len(wanted) > 1 else out
        sheet = build(team, frames=args.frames, w=args.width, h=args.height,
                      seed=args.seed, emit=args.emit, sparks=args.sparks,
                      palette=palette)
        sheet.save(path)
        used = len(colors.read_sheet(path))
        print(f'wrote {path} ({sheet.width}x{sheet.height}, '
              f'{args.frames} frames of {args.width}x{args.height}, '
              f'{used} colours)')

        if args.spd:
            spd = os.path.splitext(path)[0] + '.spd'
            with open(spd, 'w', newline='\r\n') as fh:
                fh.write(f'frames = {args.frames}\nheight = {args.height}\n'
                         f'width = {args.width}\nframerate = 0\nflags = 1\n')
            print(f'  wrote {spd}')

        if args.gif is not None:
            # A named path only makes sense for a single team; with all six
            # it would be one file overwritten five times, so they fall back
            # to a GIF beside each PNG.
            g = (args.gif if args.gif and len(wanted) == 1
                 else os.path.splitext(path)[0] + '.gif')
            # Over the dark bed the sprite really sits on: a fire is mostly
            # judged by whether its edge reads, and it will not over white.
            gif.save(sheet, g, frames=args.frames, height=args.height,
                     bed=(0x10, 0x10, 0x21))
            print(f'  wrote {g}')


if __name__ == '__main__':
    main()
