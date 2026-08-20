#!/usr/bin/env python3
"""Generate the rising-bubble animation for an underwater layer.

Split out of build_coral_water.py, which patched a Water.dir in place. The
bubbles were the only part of that worth keeping: how a Water.dir gets rebuilt
is a separate problem, and this only makes the artwork.

The canvas is not hardcoded. Geometry comes from the .spr you are replacing --
parse_spr_header reads it -- because an earlier hardcoded guess is what
corrupted the sprite. Give it a size directly only when there is no source.

    ./make-bubbles.py --width 256 --height 48 --frames 160 -o bubbles.png
    ./make-bubbles.py --from layer.spr -o bubbles.png
"""

import argparse
import math
import os
import random
import struct
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("this needs Pillow: pip install pillow")

# ---- bubble wave layer -----------------------------------------------------
# Geometry comes from the SOURCE FILE's own header at build time (see
# parse_spr_header), never hardcoded -- the stock layer.spr is 256x48 with
# 160 frames, and an earlier hardcoded 250x50x8 guess is what corrupted the
# sprite. Only the artwork is ours; the canvas and frame count are the
# file's.

# The stock layer is a smooth teal wave band. Bubbles are drawn SOLID
# instead: opaque where a bubble is, index 0 (a real hole) everywhere else,
# so the straight waterline is genuinely covered in places and genuinely
# visible in others rather than uniformly veiled.
#
# Density was chosen by measuring how much of the map still shows through
# (index 0 = hole), since hiding the waterline is the whole point:
#     3 rows x 26  ->  54% holes   far too see-through
#     5 rows x 34  ->  25% holes
#     4 rows x 30  ->  30% holes   (solid, no hollow centres)
#     6 rows x 40  ->  18% holes   <- chosen
# Bubbles overlap heavily on purpose: this should read as a packed curtain,
# not as countable individual bubbles.
# RISE COSTS COVERAGE: bubbles near either band edge are shrunk by the fade
# (see BUBBLE_FADE_ROWS), so a rising layer needs noticeably more bubbles
# than a static one to read as equally dense. Measured on this band:
#     4 x 20 =  80 bubbles -> 42% covered
#     6 x 30 = 180         -> 69%
#     7 x 40 = 280         -> 82%   <- chosen
#     8 x 50 = 400         -> 88%
BUBBLE_ROWS = 10           # stacked bands of bubbles across the strip
BUBBLE_PER_ROW = 80        # bubbles per band
BUBBLE_R = (1.5, 6.0)      # radius range, px. SMALL ON PURPOSE: centres
                           # use the whole band and drawing is clipped at
                           # its edges, so a big bubble would be visibly
                           # sliced there. At <=4px a clipped bubble loses
                           # a row or two and reads as normal.
BUBBLE_HOLLOW = 0.25       # inner hole as a fraction of radius; 0 = solid.
                           # Small but nonzero, so a little waterline
                           # flickers through each bubble and it reads as
                           # gas rather than as paint.

# The hole's own boundary ring -- what actually reads as the dark "outline"
# hugging the hole in the preview. This is NOT a separate drawing step: the
# ring is the innermost band of the normal rim/mid/deep shading (k near 0
# right at the hole edge always lands in the `deep` bucket), so without a
# dedicated control it is always exactly one `deep`-coloured band wide,
# whatever the bubble's overall size.
#
# BUBBLE_HOLE_RIM_PX: width of that ring, in px, measured outward from the
# hole boundary. 0 = no dedicated ring at all -- the hole sits directly
# against the ordinary rim/mid/deep gradient with nothing forced dark next
# to it.
BUBBLE_HOLE_RIM_PX = 0.

# BUBBLE COLOUR. layer.spr's pixels are INDICES into the sprite's own
# palette (Water_solid.dir, 32 fixed RGB entries baked into the game asset)
# -- this code can only PICK among those 32 colours, it cannot invent a new
# RGB the way a plain image editor could. That palette is entirely greens
# (hue ~104-160 deg) and teals/blues (~173-218 deg), plus one lone red/pink
# at ~343 deg -- there is no orange, yellow or purple in it at all, so no
# hue target can make bubbles orange; the code will pick the closest thing
# the palette actually has and tell you how far off that was.
#
# BUBBLE_HUE_TARGET: the hue (0-360 deg) to aim for. None (default) = the
# old behaviour, auto-picking from whichever palette entries read as blue-
# green water tones (see pick_bubble_ramp). Set a number to steer toward a
# specific hue instead -- e.g. 140 for a greener bubble, 200 for deep blue.
BUBBLE_HUE_TARGET = 200
BUBBLE_HUE_TOLERANCE = 60.0   # deg; pick_bubble_ramp WARNS (does not fail)
                              # if the closest real palette entry to
                              # BUBBLE_HUE_TARGET is further than this, since
                              # that means the palette has nothing close to
                              # what was asked for and the result will not
                              # look like the requested hue at all.
# MOTION MUST BE EXACTLY PERIODIC over the loop, or the wrap from the last
# frame back to the first jumps. Anything that accumulates -- a steady rise
# -- only closes if its total is a whole multiple of the distance it wraps
# in. An earlier 10px-per-loop rise did NOT close (10 % 48 != 0) and read as
# a visible jump: wrapping a position modulo the cell makes motion
# CONTINUOUS, not PERIODIC, and only the second one loops.
#
# Bubbles DO rise. The rise per loop is BUBBLE_RISE_BANDS whole traversals of
# the band, so a bubble ends exactly where the next one started and the loop
# closes by construction (asserted at build time).
#
# RISE AND UNCUT BUBBLES ARE IN TENSION, and this is the crux of the design:
# rows outside the shipped band do not exist, so a bubble that rises far
# enough to touch a band edge is sliced flat -- the horizontal seams seen in
# game. Insetting bubbles fixes the cut but forbids rising.
#
# The resolution is a PER-BUBBLE LIFECYCLE. Each bubble has its own birth
# height and its own lifespan: it swells from nothing, rises, and shrinks
# back to nothing, all at heights unique to it. Nothing about the fade
# depends on where the band edges are.
#
# An earlier version faded purely as a function of DISTANCE TO THE EDGE.
# That kept bubbles uncut, but because the fade was a function of position
# alone it was SYNCHRONISED: all 280 bubbles dissolved on the same two
# lines, so those lines read as their own seams. Measured coverage by row
# was a clean 96% -> 0% gradient at both edges, i.e. exactly the banding
# that was visible in game. Anything keyed to position rather than to the
# individual bubble will reproduce it.
BUBBLE_TRAVEL = (4.0, 14.0)  # px each bubble rises over ITS OWN lifetime,
                           # drawn per bubble. This is the rise: a bubble is
                           # born low, drifts up, and dies higher. Nothing
                           # wraps, so nothing has to cross a band edge --
                           # which is what lets the trajectory stay inside
                           # safe bounds while still reading as rising.
BUBBLE_LIFE = (0.35, 0.5)  # each bubble's lifespan as a fraction of the
                           # loop, drawn per bubble. <1 means it is absent
                           # for part of the loop, which staggers births.
BUBBLE_FADE_FRAC = 0.22    # fraction of a bubble's OWN life spent swelling
                           # in, and again shrinking out. Keyed to the
                           # bubble, never to the band, so no two fade
                           # together and no line is special.
BUBBLE_WOBBLE = 1.6        # px of horizontal sway (whole cycles per loop)
BUBBLE_BOB = 1.2           # px of vertical bob (whole cycles per loop)
BUBBLE_PULSE = 0.18        # radius modulation, fraction of r (0 = steady)
BUBBLE_SEED = 20250804

# Bubbles are drawn ONLY where the whole bubble fits inside the shipped
# band, with this much margin. The band is a hard crop -- rows outside it are
# never written -- so a bubble straddling its edge is sliced flat and reads
# as a horizontal seam, which is exactly what showed up in game.
BUBBLE_EDGE_MARGIN = 1


def parse_spr_header(blob):
    """layer.spr's real geometry and frame table. See the module docstring.

    Returns (ncol, w, h, n_frames, table_off, pixel_off, records) where
    `records` is a list of (data_offset, left, top, right, bottom).

    Raises if the frame table is not self-consistent -- the sum of the frame
    boxes must equal the pixel region exactly. That check is what catches a
    SpriteEditor re-export whose table has been dropped (every box zero-area),
    which would otherwise be patched happily and ship a sprite the game
    refuses to load.
    """
    if blob[:4] != SPR_MAGIC:
        raise ValueError(f"not an SPR blob: {blob[:4]!r}")
    ncol = struct.unpack_from("<H", blob, 10)[0]
    pal_end = 12 + ncol * 3
    w, h, nf = struct.unpack_from("<HHH", blob, pal_end + 6)
    table_off = pal_end + 12
    pixel_off = table_off + nf * 12
    recs = []
    for i in range(nf):
        recs.append(struct.unpack_from("<I4H", blob, table_off + i * 12))
    total = sum((r - l) * (b - t) for _, l, t, r, b in recs)
    region = len(blob) - pixel_off
    if total == 0 or total > region:
        raise ValueError(
            f"frame table is not usable: {nf} frames, boxes sum to {total}, "
            f"pixel region is {region}. This is a SpriteEditor re-export "
            f"with a dropped table -- build from a source whose table is "
            f"intact (see SRC).")
    return ncol, w, h, nf, table_off, pixel_off, recs


def _hue_dist(a, b):
    """Smallest angular distance between two hues in degrees, 0..180 --
    hue is circular (0 and 360 are the same colour), a plain subtraction
    would say red (0) and red (360) are as far apart as red and cyan."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def pick_bubble_ramp(palette, hue_target=None, tol=BUBBLE_HUE_TOLERANCE,
                     quiet=False):
    """(bright, mid, deep) indices for the bubble shading, CHOSEN BY COLOUR
    from the sprite's own fixed palette -- see BUBBLE_HUE_TARGET's comment
    for why this PICKS rather than invents an RGB.

    hue_target=None (BUBBLE_HUE_TARGET's default): the ORIGINAL auto-pick --
    keep only entries that read as blue-green water (blue >= green > red),
    sort by luminance, take three points along that ramp. Kept as the
    default because it is what every existing build has shipped with; the
    two jellyfish/legacy palettes this project has used both happen to
    satisfy blue>=green>red across most of their teal range.

    hue_target=<degrees>: rank EVERY reasonably-saturated palette entry
    (S > 0.15 -- near-grey entries have no meaningful hue, matching one
    "closest" would be an accident, not an intent) by hue distance to
    `hue_target`, and take the 3 closest, ordered dark -> bright. If the
    single closest entry is still more than `tol` degrees away, PRINT A
    WARNING (not an error -- there is no wrong palette to fall back to; the
    sprite only has the colours it has) naming the achieved hue and the gap,
    so a request for a hue this palette cannot produce is visible immediately
    instead of quietly picking whatever is nearest and looking unchanged.
    """
    import colorsys

    def hue_of(rgb):
        h, l, s = colorsys.rgb_to_hls(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        return h * 360.0, s, l

    if hue_target is None:
        teal = [(i, c) for i, c in enumerate(palette)
                if i and c[2] >= c[1] > c[0]]
        if len(teal) < 3:                  # no recognisable ramp -- spread out
            n = len(palette) - 1
            return 1, max(1, n // 2), max(1, n - 1)
        teal.sort(key=lambda t: 0.299 * t[1][0] + 0.587 * t[1][1]
                                + 0.114 * t[1][2])
        return teal[-1][0], teal[len(teal) // 2][0], teal[0][0]

    candidates = []
    for i, c in enumerate(palette):
        if i == 0:
            continue
        h, s, l = hue_of(c)
        if s <= 0.15:
            continue
        candidates.append((i, c, h, s, l))
    if not candidates:
        if not quiet:
            print(f"      BUBBLE_HUE_TARGET={hue_target}: no saturated "
                  f"palette entries at all -- falling back to the "
                  f"blue-green auto-pick")
        return pick_bubble_ramp(palette, hue_target=None, quiet=quiet)

    candidates.sort(key=lambda t: _hue_dist(t[2], hue_target))
    closest = candidates[:3]
    gap = _hue_dist(closest[0][2], hue_target)
    if not quiet:
        tag = "OK" if gap <= tol else "FAR -- palette has nothing close"
        print(f"      BUBBLE_HUE_TARGET={hue_target:.0f} deg -> closest "
              f"real colour is idx={closest[0][0]} {closest[0][1]} at "
              f"H={closest[0][2]:.0f} deg (gap {gap:.0f} deg) [{tag}]")
    closest.sort(key=lambda t: t[4])       # dark -> bright by lightness
    return closest[0][0], closest[1][0], closest[2][0]


_UNSET = object()   # sentinel distinguishing "caller passed nothing" from
                    # "caller explicitly passed None" (None is a valid,
                    # meaningful hue_target value: it means the teal auto-
                    # pick, not "no override given")


def make_bubble_layer(ncol, w, h, frames, seed=BUBBLE_SEED, palette=None,
                      band_top=0, band_h=None, _extra_frame=False,
                      hue_target=_UNSET, flipv=False):
    """A dense line of bubbles: one full-cell frame per animation frame.

    Returns a list of `frames` bytearrays, each w*h indices. Index 0 = hole
    (the map shows through), 1..ncol = the palette's light->dark teal ramp.

    Each bubble is a RING rather than a disc, so the band reads as bubbles
    instead of a lumpy bar.

    SEAMLESS on all three axes:
      - HORIZONTAL: every bubble is drawn three times (x-w, x, x+w), so one
        straddling a side edge reappears on the other side and the strip
        tiles across the map;
      - TIME: nothing accumulates. Sway, bob and pulse are all sinusoids on a
        WHOLE number of cycles per loop, so frame `frames` is identical to
        frame 0 by construction (asserted in the caller);
      - BAND EDGES: bubbles are placed so the whole bubble, at its largest
        pulse and furthest bob, stays inside `band_top..band_top+band_h`.
        Rows outside the band are never shipped, so anything crossing that
        boundary would be sliced flat -- the horizontal seams seen in game.

    `flipv` mirrors the STRUCTURE top-to-bottom while bubbles still RISE.

    Flipping the finished frames instead would invert the motion and the
    bubbles would fall, which is not what "flip it vertically" means here --
    the rise is the one thing that must survive the flip.

    The band's top/bottom asymmetry comes entirely from the TRAJECTORIES, in
    two places, not from the drawing (a bubble is a radially symmetric ring):
      - `travel` is clamped by the headroom ABOVE the birth height, so
        bubbles born low travel far and bubbles born high barely move at
        all -- the long streaks live at the bottom;
      - the lifecycle envelope makes every bubble small at birth (low) and
        small again at death (high), so the fully-swollen part of each life
        sits below its midpoint.
    Reflecting each trajectory about the band's horizontal centre line moves
    both of those to the other end, so the dense/streaky structure lands at
    the top -- while `y1` is still computed as "born low, dies higher up"
    and the animation still rises.
    """
    import math
    import random

    rnd = random.Random(seed)
    if hue_target is _UNSET:
        # Resolved HERE, on every call, not as a default-argument value --
        # `def f(x=BUBBLE_HUE_TARGET)` captures the value ONCE, at function-
        # definition time (module import), so editing BUBBLE_HUE_TARGET
        # afterwards (interactively, or in a driver script) would silently
        # keep using whatever it was at import. Measured: a caller that set
        # build_water.BUBBLE_HUE_TARGET = 140 after import and then called
        # this function with no explicit hue_target still shaded in the old
        # teal, because the frozen default from import time won -- the
        # printed diagnostic (which DOES re-read the module global live) said
        # 140 while the actual shipped pixels said otherwise.
        hue_target = BUBBLE_HUE_TARGET
    # Shading indices are picked from the palette's actual colours, never by
    # position -- see pick_bubble_ramp.
    if palette is None:
        rim, mid, deep = 1, max(1, ncol // 3), max(1, (2 * ncol) // 3)
    else:
        rim, mid, deep = pick_bubble_ramp(palette, hue_target=hue_target)

    # The hole's boundary ring reuses `deep` -- an unlabelled side effect of
    # the ordinary shading gradient (k near 0 at the hole edge always lands
    # in the deep bucket).
    hole_rim = deep

    if band_h is None:
        band_top, band_h = 0, h

    # Every bubble lives and dies ENTIRELY INSIDE this safe range, so it is
    # never clipped by the band edges, and it rises between two heights that
    # are its own, so no two bubbles share a fade line.
    #
    # Three earlier attempts each failed on one of those two requirements:
    #   - fade by DISTANCE TO THE EDGE: uncut, but synchronised -- every
    #     bubble dissolved on the same two lines (measured 96% -> 0%
    #     coverage gradient, visible as banding in game);
    #   - centres confined to an inset interior, rising the full height:
    #     uncut, but coverage tapered to nothing at the edges (94pp spread);
    #   - centres over the whole band with hard clipping: flat profile, but
    #     ~80-112 px per frame sliced flat on the edge rows.
    # Bounding each bubble's whole TRAJECTORY is what satisfies both.
    reach = BUBBLE_R[1] * (1.0 + BUBBLE_PULSE) + BUBBLE_BOB - 2.5
    safe_lo = band_top + reach
    safe_hi = band_top + band_h - 1 - reach
    if safe_hi - safe_lo < 2.0:
        raise SystemExit(
            f"band of {band_h} rows is too thin for bubbles up to "
            f"{BUBBLE_R[1]}px (safe range {safe_hi - safe_lo:.1f} rows). "
            f"Lower BUBBLE_R[1].")

    bubbles = []
    for _ in range(BUBBLE_ROWS * BUBBLE_PER_ROW):
        # Per-bubble trajectory: rises `travel` px, centred on `mid`.
        #
        # The MIDPOINT is what is drawn uniformly, not the birth height.
        # Drawing y0 uniformly and subtracting travel biases every
        # trajectory upward, so the lower rows empty out and the middle
        # piles up (measured: content bunched in rows 5-19, bottom 4 rows
        # empty). Centring instead keeps occupancy even.
        #
        # The range is shrunk by half the travel at each end so the whole
        # trajectory -- not just its midpoint -- stays inside the safe range,
        # which is what keeps bubbles from ever reaching a band edge.
        # Birth height is drawn UNIFORMLY over the whole safe span, and the
        # bubble rises from there by however much room is left above it (up
        # to its own travel). Uniform BIRTH gives flat occupancy; drawing a
        # uniform MIDPOINT instead concentrates every trajectory toward the
        # centre and produced a bell curve -- measured, rows 12-20 at ~85%
        # while the outer rows sat empty.
        #
        # NB: not named `mid` -- that is the mid-tone palette index from
        # pick_bubble_ramp, and shadowing it writes a float into the pixel
        # buffer (TypeError at draw time).
        y0 = rnd.uniform(safe_lo, safe_hi)
        travel = min(rnd.uniform(*BUBBLE_TRAVEL), y0 - safe_lo)
        y1 = y0 - travel                   # born low, dies higher up
        if flipv:
            # Reflect the WHOLE trajectory about the band's centre line. Both
            # ends move together, so the span y1..y0 keeps its length and
            # stays inside safe_lo..safe_hi (the range is symmetric about
            # `centre` by construction) -- no bubble can be pushed into a
            # band edge by this.
            #
            # y0 and y1 SWAP as well as move: reflection reverses their
            # order, and y0 must stay the LOWER end (the birth height) so
            # that `cy` still interpolates y0 -> y1 upward over the life.
            # Mirroring each independently and keeping the names would make
            # y1 the lower one and the bubbles would sink.
            centre = safe_lo + (safe_hi - safe_lo) / 2.0
            y0, y1 = 2 * centre - y1, 2 * centre - y0
        bubbles.append((
            rnd.uniform(0, w),
            y0, y1,
            rnd.uniform(*BUBBLE_R),
            rnd.uniform(0, 2 * math.pi),   # sway phase
            rnd.choice((1, 1, 2)),         # sway cycles per loop (integer)
            rnd.uniform(0, 2 * math.pi),   # bob phase
            rnd.choice((1, 2)),            # bob cycles per loop (integer)
            rnd.uniform(0, 2 * math.pi),   # pulse phase
            rnd.choice((1, 2, 3)),         # pulse cycles per loop (integer)
            rnd.random(),                  # birth phase, 0..1 of the loop
            rnd.uniform(*BUBBLE_LIFE),     # lifespan, fraction of the loop
        ))

    out = []
    tau = 2 * math.pi
    for f in range(frames + (1 if _extra_frame else 0)):
        t = f / frames
        cell = bytearray(w * h)
        for (bx, y0, y1, br, sph, srate, bph, brate, pph, prate,
             birth, life) in bubbles:
            # LIFECYCLE, per bubble: age through its own lifespan, wrapping
            # at the loop. `age` in 0..1 is how far through its life it is.
            # Because `birth` and `life` differ per bubble, every bubble
            # swells and dissolves at its own time and its own height --
            # which is the whole point, since a fade keyed to POSITION made
            # all of them dissolve on the same two lines.
            age = ((t - birth) % 1.0) / life
            if age > 1.0:
                continue                 # not alive in this frame
            # Envelope: swell in over the first BUBBLE_FADE_FRAC of life,
            # hold, shrink out over the last BUBBLE_FADE_FRAC. Peaks at 1.
            if age < BUBBLE_FADE_FRAC:
                env = age / BUBBLE_FADE_FRAC
            elif age > 1.0 - BUBBLE_FADE_FRAC:
                env = (1.0 - age) / BUBBLE_FADE_FRAC
            else:
                env = 1.0
            cx = bx + math.sin(sph + srate * tau * t) * BUBBLE_WOBBLE
            # RISE: interpolate from this bubble's own birth height to its own
            # death height across its own life. Nothing wraps and nothing
            # accumulates, so the trajectory stays inside the safe range for
            # the whole lifetime -- no clipping -- while each bubble still
            # visibly travels upward.
            cy = y0 + (y1 - y0) * age \
                + math.sin(bph + brate * tau * t) * BUBBLE_BOB
            r = br * (1.0 + BUBBLE_PULSE * math.sin(pph + prate * tau * t))
            r *= env                     # the per-bubble lifecycle envelope
            if r < 0.8:                  # too small to draw meaningfully
                continue
            r_in = r * BUBBLE_HOLLOW
            # Clip to the BAND, not just to the cell. The fade above should
            # already prevent any bubble reaching an edge, but that relies on
            # the arithmetic being exactly right and it twice was not; this
            # makes "nothing is ever drawn outside the shipped rows" a
            # structural property instead of a consequence.
            y_lo, y_hi = band_top, band_top + band_h
            for yy in range(int(cy - r - 1), int(cy + r + 2)):
                dy = yy - cy
                if abs(dy) > r or not (y_lo <= yy < y_hi):
                    continue
                span = math.sqrt(max(0.0, r * r - dy * dy))
                inner = math.sqrt(max(0.0, r_in * r_in - dy * dy)) \
                    if abs(dy) < r_in else 0.0
                for xrep in (-w, 0, w):
                    for xx in range(int(cx + xrep - span),
                                    int(cx + xrep + span) + 1):
                        if not (0 <= xx < w):
                            continue
                        d = math.hypot(xx - (cx + xrep), dy)
                        if d > r or (inner and d < inner):
                            continue
                        # The hole's own boundary ring, BUBBLE_HOLE_RIM_PX
                        # wide, checked before the general shading gradient
                        # so it wins right at the hole edge regardless of
                        # where that edge falls in the rim/mid/deep bands.
                        if inner and (d - inner) < BUBBLE_HOLE_RIM_PX:
                            cell[yy * w + xx] = hole_rim
                            continue
                        k = (d - inner) / max(0.001, r - inner)
                        cell[yy * w + xx] = rim if k > 0.72 else (
                            mid if k > 0.35 else deep)
        out.append(cell)
    return out

def _palette_from(path):
    """Read a palette out of a picture, its colours in the order first met."""
    raw = Image.open(path).convert("RGBA").tobytes()
    seen, known = [], set()
    for o in range(0, len(raw), 4):
        if raw[o + 3] < 128:
            continue
        c = (raw[o], raw[o + 1], raw[o + 2])
        if c not in known:
            known.add(c)
            seen.append(c)
    return seen


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="source", metavar="SPR",
                   help="take width/height/frames from this .spr")
    p.add_argument("--width", type=int, default=256)
    p.add_argument("--height", type=int, default=48)
    p.add_argument("--frames", type=int, default=160)
    p.add_argument("--ncol", type=int, default=64,
                   help="colours available to the ramp")
    p.add_argument("--palette", metavar="PNG",
                   help="take the bubble ramp from this picture's colours")
    p.add_argument("--seed", type=int, default=BUBBLE_SEED)
    p.add_argument("-o", "--out", default="bubbles.png")
    a = p.parse_args()

    w, h, frames = a.width, a.height, a.frames
    if a.source:
        with open(a.source, "rb") as fh:
            w, h, frames = parse_spr_header(fh.read())[:3]
        print(f"{os.path.basename(a.source)}: {w}x{h}, {frames} frames")

    palette = _palette_from(a.palette) if a.palette else None
    cells = make_bubble_layer(a.ncol, w, h, frames, seed=a.seed,
                              palette=palette)

    sheet = Image.new("RGBA", (w, h * frames), (0, 0, 0, 0))
    for i, cell in enumerate(cells):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        img.putdata([(255, 255, 255, 0) if v == 0 else (200, 230, 255, 255)
                     for v in cell])
        sheet.paste(img, (0, i * h))
    sheet.save(a.out)
    print(f"wrote {a.out}: {w}x{h * frames}, {frames} frames")


if __name__ == "__main__":
    main()
