#!/usr/bin/env python3
"""Generate back2.spr for the Coral Reef terrain: the animated "reef swaying
in the current" background parallax layer.

(Renamed from make_back2_and_front.py: front.spr is its own pipeline in
make_front.py now -- own canvas, own sublayers -- and no longer derived from
back2's strip. What remains here that front still uses is the shared SWAY_*
machinery, which make_front.py imports; the dependency runs one way only.)

Split out of build_coral.py, which still owns everything about the palette,
the object pool (objects.txt) and how the finished .spr/.spd get written to
disk -- this module only turns that pool into the swaying silhouette
animation. `object_fill`, `silhouette_color` and `sync_config` are passed in
by the caller (build_coral.py) rather than imported back from it, so the two
modules do not import each other in a cycle.

WHY JELLYFISH-STYLE SWAY, NOT A STATIC IMAGE
---------------------------------------------
_back.spr turned out NOT to animate in practice, despite wkTerrainSync's own
docs claiming both parallax sprites "can be animated like regular sprites"
(confirmed by the user via actual play -- back2.spr genuinely cycles,
_back.spr does not). It is dropped from the build entirely rather than kept
as dead weight; the official Cosmic terrain's own packaging script treats it
the same way, only adding it to Level.dir.txt "IF EXIST".

Instead, back2.spr -- the one file that DOES animate -- is built from
BACK_LAYERS depth bands, PRE-COMPOSITED into one strip so they all animate
together (this is the "stack multiple animated layers on top of one another"
a two-file split was always only approximating). Band 0 is nearest (biggest,
brightest, fewest objects); the last band is farthest (smallest, darkest,
most objects, most sky haze).

front.spr is wkTerrainSync's third parallax layer, "displayed in front of
the map" (see docs/WkTerrainSync.txt) -- currently the SAME frames as
back2.spr, shifted down by FRONT_LOWER px (see make_front_strip): unlike
back2 (behind the terrain, so anything floating too high is masked by the
terrain silhouette), front sits IN FRONT of it, so the same coral reads as
floating with nothing to hide the gap unless it is pushed down first.
"""
import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageOps

# ---- canvas / geometry -----------------------------------------------------
# SIZE: keep frame counts modest. 650 frames at 1024px wide made SpriteEditor
# die with "Access violation ... Read of address 00000008" on a background
# strip this shape; the Mario reference uses 8 frames at 1024x410 (3.2 MB).
BACK2_W, BACK2_H = 1024, 410     # (Mario/Cosmic proportions)

# frames/framerate do NOT set speed (see SWAY_AMPLITUDE below) -- these only
# control how finely the 0.667s loop is subdivided, i.e. smoothness/file size.
BACK2_FRAMES = 30
BACK2_FRAMERATE = 1
# GLOBAL strip brightness, applied last by the caller (see build_strip in
# build_coral.py). 100 = no change. Per-band darkening/haze (BACK_LAYER_DARKEN
# / BACK_LAYER_HAZE) already happens earlier, inside make_back2, so this is a
# second, coarser knob on top of that.
BACK2_BRIGHTNESS = 100

# At higher in-game resolutions, players can see past BACK2_STEM_HIDE_Y down
# into the strip's bottom band, which is just bare stems (every crown sits
# higher up, the trunks below it are what's left). Rather than draw dedicated
# content for that band, we reuse the SAME finished frame, rolled sideways by
# half the canvas width and painted back on top of itself for rows
# BACK2_STEM_HIDE_Y and below -- since every sway object is already pasted at
# x-canvas_w/x/x+canvas_w so the strip tiles seamlessly (see _sway_bands), a
# horizontal roll is just another seamless phase of the same tiling pattern,
# not a new/different composition, so it hides the stems with coral that
# already matches the rest of the layer instead of looking like a patch.
BACK2_STEM_HIDE_Y = 250

# front.spr sits IN FRONT of the terrain, unlike back2.spr behind it -- copied
# from the same frames, its coral reads as floating too high once it is no
# longer masked by the terrain silhouette. FRONT_LOWER (px) pushes it down:
# each frame's bottom FRONT_LOWER rows are cut off and that much transparent
# space is added at the top instead, so the whole image shifts down without
# changing frame size.
FRONT_LOWER = 220

SWAY_ROTATION = {"floor": 0, "ceiling": 180, "side": 90}

# Cutouts that look wrong/redundant as background silhouettes (too spindly,
# too similar to another shape at a distance, etc.) -- excluded from the
# background sway pool only; still used normally as foreground/floor objects.
SWAY_EXCLUDE_SRC = {
    "cutout_04.png", "cutout_16.png", "cutout_11.png", "cutout_09.png",
    "cutout_08.png", "cutout_15.png", "cutout_22.png", "cutout_23.png",
    "cutout_26.png", "cutout_30.png", "new_02.png", "new_05.png",
}

SWAY_AMPLITUDE = 3      # px, max horizontal offset at the very tip
                         # (for the full 410px-tall canvas)
SWAY_BEND_POWER = 2.4    # bend growth vs height fraction t (1=linear hinge,
                         # 2=very tip-only, <2=more of the body visibly moves)

# ---- per-object independence -----------------------------------------------
# Each coral gets its OWN random phase and its own slightly different sway
# rate, so the reef does not move as one body. Phases were previously spaced
# evenly by object index (idx/total*2pi), which -- because the two layers
# interleave the object list -- put neighbouring corals on near-identical
# phases and read as a single marching wave.
#
# SWAY_RATE_JITTER keeps every object on a WHOLE number of cycles across the
# strip (1 or 2), because the animation must loop seamlessly: a fractional
# rate would jump at the wrap. Two rates are enough to break up the unison
# while staying loopable.
SWAY_RATES = (1, 1, 1, 2)   # cycles per loop; drawn per object

# ---- thickness-driven wiggle ------------------------------------------------
# On top of the height-based bend, thin parts of a plant move further than
# thick ones -- twigs whip while the trunk barely stirs. Slenderness is
# measured per row from the mask itself (see _row_slenderness).
#
# The extra offset is SWAY_SLENDER_GAIN * slenderness^SWAY_SLENDER_POWER,
# multiplied by the same height ramp so the anchored base still never moves
# regardless of how thin it is.
SWAY_SLENDER_GAIN = 2.6   # px of EXTRA tip travel for the thinnest pixels
SWAY_SLENDER_POWER = 2.0  # >1 concentrates the extra motion in the thinnest
                          # parts only, rather than thickening the whole plant
SWAY_THICK_REF = 10.0     # local half-width (px) treated as fully rigid;
                          # ABSOLUTE, so a 2px twig reads as thin on every
                          # object regardless of how chunky its parent is

# ---- background LAYER STACK -------------------------------------------------
BACK_LAYERS = 4       # how many depth bands to stack; 2 matches the old
                      # _back(near)+back2(far) look, but this scales further

# Total sway-object INSTANCES drawn across every band combined, sampled WITH
# REPETITION from the 32-name pool in objects.txt (a "repeating and
# randomized" pool, not one draw per config row as before). Split across
# BACK_LAYERS by BACK_LAYER_SPLIT below, so raising this raises density
# without needing more source art.
BACK_OBJECTS = 1024

# How BACK_OBJECTS divides across the bands, nearest-to-farthest, as
# RELATIVE weights (not fractions -- normalised automatically). Distant bands
# get a BIGGER share: they are individually smaller, so more of them are
# needed to read as a reef rather than a few scattered dots. If BACK_LAYERS
# does not match len(BACK_LAYER_SPLIT), the split list is stretched/truncated
# by repeating its last value.
BACK_LAYER_SPLIT = (0.35, 0.65)

# Global multiplier on how strongly each band blends toward the sky colour
# (see BACK_LAYER_HAZE below). 1.0 = the authored per-band amounts; 0 = no
# haze at all (every band full colour); >1 exaggerates it. One knob to dial
# the whole depth effect up or down without re-tuning every band.
BACK_HAZYNESS = 0.8

# Per-band tuning, nearest-to-farthest. Same stretch/truncate rule as
# BACK_LAYER_SPLIT if BACK_LAYERS is changed.
#   height_frac / width_frac : each object's max size, as a FRACTION of the
#       canvas (was a hard px cap keyed by filename, "_back" vs "back2", back
#       when there were only ever two fixed layers).
#   darken     : silhouette_color() output is multiplied by this on top of
#       its own family-colour darkening -- distant bands are darker, not just
#       hazier, per "distant layers need smaller and darker ... objects".
#   haze       : 0..1, blended toward the sky colour BEFORE BACK_HAZYNESS is
#       applied (haze * BACK_HAZYNESS is the final blend fraction).
BACK_LAYER_HEIGHT_FRAC = (0.59, 0.44)   # of canvas height
BACK_LAYER_WIDTH_FRAC  = (0.39, 0.28)   # of canvas width
BACK_LAYER_DARKEN      = (1.0, 0.6)     # 1.0 = unchanged silhouette tone
BACK_LAYER_HAZE        = (0.0, 0.45)    # blend toward the sky colour

# The more corals we spawn, the smaller each one should be -- a crowded reef
# reads as crowded only if the extra objects are small filler, not if 320
# objects are all drawn at the same size as 32 would be (that just overlaps
# into mud). HEIGHT_FRAC/WIDTH_FRAC above are the size at BACK_DENSITY_REF
# objects; above/below that, every band's max size is scaled by
# (BACK_DENSITY_REF / BACK_OBJECTS) ** BACK_DENSITY_POWER -- but only if
# BACK_DENSITY_SCALE_ENABLED is True; False disables the response entirely
# (every band always sized at the plain *_FRAC values, regardless of
# BACK_OBJECTS), for comparison against the density-responsive look.
BACK_DENSITY_SCALE_ENABLED = False
BACK_DENSITY_REF   = 32     # object count the authored *_FRAC sizes assume
BACK_DENSITY_POWER = 0.5    # 0 = no size response to density; 1 = linear;
                            # 0.5 (default) is gentler -- 10x the objects
                            # gives ~3x smaller, not 10x smaller

# If True, each object's raise (how far its stem lifts it off the canvas
# floor) is allowed to range all the way up to canvas_h - its own height, so
# the tallest object a band can draw is always ABLE to reach exactly the
# canvas top with zero clipping -- this is what keeps a shrunk/dense reef
# from collapsing into a low strip at the bottom (see BACK_DENSITY_* above).
# If False, raise stays within the originally authored raise_range only
# (12-45% of max_h), the old behaviour, for comparison.
BACK_RAISE_TO_TOP = True


def _stretch(seq, n):
    """Repeat the last element (or truncate) so len(seq) == n, letting
    BACK_LAYERS change without every per-band tuple needing a matching edit."""
    seq = list(seq)
    if not seq:
        return [0] * n
    if len(seq) < n:
        seq = seq + [seq[-1]] * (n - len(seq))
    return seq[:n]


def _sway_names(sync_config):
    """All object names from objects.txt, paired with their `where`, in file
    order, minus SWAY_EXCLUDE_SRC. Returns [(name, src, where)]."""
    return [(r["name"], r["src"], r.get("where", "floor"))
            for r in sync_config() if r["src"] not in SWAY_EXCLUDE_SRC]


def _load_sway_silhouette(obj_dir, src_name, max_h, where="floor", stem_to=0,
                          max_w=None, flip=False):
    """Load Objects/<src_name> (a raw white-on-transparent cutout, keyed by
    the object's src filename in objects.txt), rotate it upright according to
    `where`, scale to max_h tall, and return an 'L' alpha mask (255 = solid).

    ROTATION: `where` names the cutout's contact/anchor edge, so it must be
    turned to stand on the seabed -- floor 0, ceiling 180, side +90 (see
    SWAY_ROTATION). Rotation happens BEFORE scaling so max_h applies to the
    upright height, not the original orientation's.

    If stem_to > 0, extend a solid STEM straight down from the cutout's own
    bottom edge (matching that edge's x-span, i.e. the trunk's natural
    width) for `stem_to` extra px, so a raised/floating object still reads
    as GROUNDED -- planted in the seabed -- rather than a detached island.
    The stem sits below t=0 (the sway anchor), so it never bends; only the
    original cutout above it sways.

    If flip, the cutout is horizontally mirrored before rotation -- so a
    repeated object reads as a distinct shape rather than an identical copy.
    """
    path = os.path.join(obj_dir, src_name)
    im = Image.open(path).convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    if flip:
        im = ImageOps.mirror(im)
    angle = SWAY_ROTATION.get(where, 0)
    if angle:
        im = im.rotate(angle, expand=True, resample=Image.NEAREST)
        b2 = im.getchannel("A").getbbox()
        if b2:
            im = im.crop(b2)
    # Cap BOTH dimensions: these cutouts have very different aspect ratios, so
    # scaling to a target height alone lets wide ones balloon (e.g. `thicket`
    # reached 1031px across on a 1024px canvas). Scale by whichever limit
    # binds first, preserving aspect.
    scale = max_h / im.height
    if max_w:
        scale = min(scale, max_w / im.width)
    im = im.resize((max(1, round(im.width * scale)),
                    max(1, round(im.height * scale))), Image.LANCZOS)
    alpha = im.getchannel("A").point(lambda v: 255 if v >= 96 else 0)

    if stem_to > 0:
        w, h = alpha.size
        bottom_row = alpha.crop((0, h - 1, w, h)).load()
        xs = [x for x in range(w) if bottom_row[x, 0]]
        if xs:
            x0, x1 = min(xs), max(xs)
            out = Image.new("L", (w, h + stem_to), 0)
            out.paste(alpha, (0, 0))
            dr = ImageDraw.Draw(out)
            dr.rectangle([x0, h, x1, h + stem_to - 1], fill=255)
            alpha = out
    return alpha


def _local_thickness(alpha):
    """Per-PIXEL local half-width of the limb each pixel belongs to, as a
    float array (numpy, shape (h, w)), 0 outside the silhouette.

    This is a chamfer distance transform: the distance from each opaque pixel
    to the nearest transparent one. Deep inside a trunk that distance is
    large; in a 2px twig it is ~1. It is the key to animating BRANCHES
    independently -- a per-ROW measure cannot, because a trunk and a twig at
    the same height share a row and would be averaged into one value, so the
    twig could only ever move exactly as much as the trunk beside it.

    Two passes (forward then backward) over the 3x3 chamfer neighbourhood give
    a close approximation of the true Euclidean distance, which is ample here:
    we only need a smooth "thin vs thick" gradient, not exact metrics.
    """
    a = np.asarray(alpha, dtype=np.uint8) > 0
    h, w = a.shape
    BIG = float(h + w)
    d = np.where(a, BIG, 0.0)

    # chamfer weights: 1 orthogonal, sqrt(2) diagonal
    D1, D2 = 1.0, 1.4142135

    # Row-sequential, column-vectorised: the chamfer recurrence depends on the
    # previous ROW (already final) and on the pixel to the left in the CURRENT
    # row, so rows must be walked in order, but each row's horizontal sweep is
    # a short scalar loop over w rather than a full 2D Python loop.
    for y in range(h):
        cur = d[y]
        if y > 0:
            prev = d[y - 1]
            cand = prev + D1
            cand[:-1] = np.minimum(cand[:-1], prev[1:] + D2)
            cand[1:] = np.minimum(cand[1:], prev[:-1] + D2)
            np.minimum(cur, cand, out=cur)
        for x in range(1, w):                     # left-to-right dependency
            if cur[x] > cur[x - 1] + D1:
                cur[x] = cur[x - 1] + D1

    for y in range(h - 1, -1, -1):
        cur = d[y]
        if y + 1 < h:
            nxt = d[y + 1]
            cand = nxt + D1
            cand[:-1] = np.minimum(cand[:-1], nxt[1:] + D2)
            cand[1:] = np.minimum(cand[1:], nxt[:-1] + D2)
            np.minimum(cur, cand, out=cur)
        for x in range(w - 2, -1, -1):            # right-to-left dependency
            if cur[x] > cur[x + 1] + D1:
                cur[x] = cur[x + 1] + D1

    d[~a] = 0.0
    return d


def _slenderness_map(alpha):
    """Per-pixel slenderness, 0 (chunky) .. 1 (hair-thin), on an ABSOLUTE
    px scale -- NOT normalised against the plant's own thickest point.

    Per-object normalisation was tried first and is wrong here: these cutouts
    differ enormously in build (birch's thickest point is 5px, hedge's is
    30px), so dividing by each plant's own peak stretched birch's 1-5px range
    across the full scale while scoring hedge's genuinely thin twigs the same
    as birch's trunk. A 2px twig should whip because it is 2px, whoever it
    belongs to.

    SWAY_THICK_REF sets where "thick enough to be rigid" begins; anything at
    or beyond it scores 0.
    """
    d = _local_thickness(alpha)
    s = 1.0 - np.clip(d / SWAY_THICK_REF, 0.0, 1.0)
    s[d <= 0] = 0.0
    return s


def _sway_frame(alpha, offset_fn, slender=None):
    """Warp a silhouette mask by `offset_fn(t, s)`, displacing each PIXEL
    horizontally. `t` is 0 at the anchored bottom row and 1 at the top; `s` is
    that pixel's slenderness (see _slenderness_map), so a thin twig whips
    further than the trunk it grows from -- even where the two share a row.

    Sampling is a PULL (for each destination pixel, find its source), not a
    push. Pushing pixels that move by differing amounts tears a limb apart,
    leaving 1px gaps wherever the displacement gradient exceeds 1; pulling
    cannot, because every destination pixel is written exactly once.

    Returns an 'L' mask padded so nothing clips.
    """
    a = np.asarray(alpha, dtype=np.uint8)
    h, w = a.shape
    if slender is None:
        slender = np.zeros((h, w), dtype=float)

    # displacement of every pixel
    ys = np.arange(h, dtype=float).reshape(h, 1)
    t = 1.0 - ys / max(1, h - 1)                 # 1 at top row, 0 at bottom
    t = np.repeat(t, w, axis=1)
    disp = offset_fn(t, slender)
    disp = np.where(a > 0, disp, 0.0)

    pad = int(np.ceil(np.abs(disp).max())) + 2 if disp.size else 2
    out_w = w + 2 * pad

    # PULL: dest x = src x + disp  =>  src x = dest x - disp. The displacement
    # is defined on the SOURCE grid, so invert by scattering source->dest and
    # taking, for each destination, the nearest contributing source pixel.
    dst = np.zeros((h, out_w), dtype=np.uint8)
    xs = np.arange(w)
    for y in range(h):
        row = a[y]
        idx = np.nonzero(row)[0]
        if idx.size == 0:
            continue
        tx = xs[idx] + pad + np.rint(disp[y, idx]).astype(int)
        np.clip(tx, 0, out_w - 1, out=tx)
        dst[y, tx] = row[idx]
        # close 1px tears opened where neighbouring pixels moved apart
        if idx.size > 1:
            gaps = np.nonzero(np.diff(tx) == 2)[0]
            if gaps.size:
                dst[y, tx[gaps] + 1] = row[idx[gaps]]
    return Image.fromarray(dst, mode="L")


def _make_sway_layer(obj_dir, object_fill, silhouette_color, entries,
                     canvas_w, canvas_h, obj_h_range, raise_range,
                     n_frames, seed, max_h, max_w):
    """One animated layer: place the given objects along canvas_w at random
    x positions and heights, each bottom-anchored NOT to the canvas floor but
    to a random height above it (so small objects still reach up into the
    visible sky instead of hugging the very bottom edge, where they'd sit
    below/behind the terrain line and never be seen).

    raise_range is only the LOWER bound of how far objects lift off the
    floor; the upper bound is stretched per-object up to canvas_h - h (its
    own height), so the tallest object obj_h_range can produce is always
    ABLE to reach exactly the canvas top with zero clipping, no matter how
    small max_h/obj_h_range has been shrunk (see BACK_DENSITY_* above).

    `entries` is [(name, src, where, flip)] -- `where` drives the upright
    rotation (see SWAY_ROTATION), `flip` mirrors the cutout horizontally (see
    _sway_pool), and each object is filled in a much darker version of its
    own family colour (silhouette_color), so the reef reads as the same
    species in shadow rather than as anonymous black shapes.

    obj_h_range is clamped to max_h: cutouts vary hugely in native size, so
    deriving size from relative proportions alone makes some objects
    enormous -- the cap is in ABSOLUTE px.

    Each object sways INDEPENDENTLY: its own random phase and its own whole
    number of cycles per loop (SWAY_RATES). Phases used to be spaced evenly by
    object index (idx/total*2pi), which -- because the old _back/back2 split
    interleaved the object list -- gave neighbouring corals near-identical
    phases, so the reef read as one body moving together rather than as many
    plants in the same current. Rates stay INTEGER so every object still
    closes its loop exactly at the wrap.

    Within an object, thin parts move further than thick ones: the bend is
    driven by per-pixel local thickness (see _slenderness_map), so a twig
    whips while the trunk it grows from barely stirs. Returns n_frames RGBA
    images.
    """
    rnd = random.Random(seed)

    lo, hi = obj_h_range
    hi = min(hi, max_h)
    lo = min(lo, hi)

    placed = []   # (alpha_mask, slenderness, x, phase, rate, fill_rgb)
    for idx, (name, src, where, flip) in enumerate(entries):
        h = rnd.randint(lo, hi)
        r_lo, r_hi = raise_range
        if BACK_RAISE_TO_TOP:
            # stretched so the TALLEST possible object (h == hi) can reach
            # all the way to canvas_h - hi (crown exactly touching the
            # canvas top) without clipping; shorter objects in the same draw
            # still randomize within that stretched range, capped so THEIR
            # OWN crown never passes the top either.
            r_hi = max(r_lo, canvas_h - hi)
            r_hi = min(r_hi, canvas_h - h)   # this object's own no-clip ceiling
        else:
            r_hi = min(r_hi, canvas_h - h)   # still never clip past the top
        r_lo = min(r_lo, r_hi)
        raise_px = rnd.randint(r_lo, r_hi)
        # stem_to extends the cutout's own trunk straight down to the true
        # floor, so the raised object still reads as GROUNDED instead of a
        # detached floating island; the stem sits in the low-t (rigid) zone
        # so it never sways, only the original crown above it does.
        alpha = _load_sway_silhouette(obj_dir, src, h, where=where,
                                      stem_to=raise_px, flip=flip,
                                      max_w=max_w)
        # full canvas_w range (not capped to canvas_w - alpha.width): objects
        # must be able to straddle the right edge so their overhang wraps
        # onto the left, keeping the strip seamless when tiled
        x = rnd.randint(0, max(0, canvas_w - 1))
        # own phase + own (integer, so still loopable) rate
        phase = rnd.uniform(0, 2 * math.pi)
        rate = rnd.choice(SWAY_RATES)
        # computed ONCE per object, not per frame -- it depends only on shape
        slender = _slenderness_map(alpha)
        fill = silhouette_color(object_fill(name))
        placed.append((alpha, slender, x, phase, rate, fill))

    frames = []
    for f in range(n_frames):
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        for alpha, slender, x, phase, rate, fill in placed:
            t_anim = 2 * math.pi * rate * f / n_frames + phase

            def offset_fn(t, s, t_anim=t_anim):
                # Height ramp: rigid at the anchored base, flexible toward the
                # tip. Thin parts get EXTRA travel on top, so a twig outswings
                # the trunk beside it at the same height.
                bend = t ** SWAY_BEND_POWER
                extra = SWAY_SLENDER_GAIN * (s ** SWAY_SLENDER_POWER)
                return (SWAY_AMPLITUDE + extra) * bend * math.sin(t_anim)

            bent = _sway_frame(alpha, offset_fn, slender)
            sprite = Image.new("RGBA", bent.size, (0, 0, 0, 0))
            sprite.paste(fill + (255,), (0, 0), bent)

            # bottom of the (now stem-extended) mask sits flush on the
            # canvas floor -- the stem itself reaches the ground, so the
            # object reads as planted rather than floating
            y0 = canvas_h - bent.height
            px = x - (bent.width - alpha.width) // 2

            # WRAP horizontally: back2.spr tiles left-to-right in game, so an
            # object that overhangs one edge must reappear on the other, or
            # the seam between repeats shows a visible cut. Pasting at
            # px-canvas_w/px+canvas_w as well as px covers every case cheaply
            # (alpha_composite no-ops once fully off-canvas).
            for ox in (px - canvas_w, px, px + canvas_w):
                if ox + sprite.width > 0 and ox < canvas_w:
                    canvas.alpha_composite(sprite, (ox, y0))
        frames.append(canvas)
    return frames


def _sway_pool(sync_config, total, seed=7):
    """BACK_OBJECTS worth of (name, src, where, flip) instances, sampled WITH
    REPETITION from the full objects.txt pool and shuffled.

    Previously each of the 32 config rows was drawn exactly once, split 50/50
    between the two layers. Now the pool is sampled `total` times (with
    replacement, so `total` can exceed or undercut 32 freely) and THEN split
    across bands, per the user's "determine how many objects we spawn in
    total ... and then those get distributed to the layers.\"

    For diversity, half of any src's REPEAT instances (its 2nd, 4th, ... draw,
    not the 1st) are horizontally flipped -- so the same cutout drawn multiple
    times doesn't read as identical copies stamped across the reef.
    """
    names = _sway_names(sync_config)
    if not names:
        return []
    rnd = random.Random(seed)
    picks = [names[rnd.randrange(len(names))] for _ in range(total)]
    seen = {}
    pool = []
    for name, src, where in picks:
        n = seen.get(src, 0)
        seen[src] = n + 1
        flip = (n % 2) == 1     # every other repeat instance, starting at 2nd
        pool.append((name, src, where, flip))
    return pool


def _sway_bands(obj_dir, object_fill, silhouette_color, sync_config,
                canvas_w, canvas_h, n_frames, seed_base=31):
    """Build BACK_LAYERS independently-animated silhouette bands (nearest
    first) and return them as a list of frame-lists, one per band, each
    n_frames long -- ready to be composited back-to-front into one strip.

    Each band gets its own slice of the BACK_OBJECTS instance pool (sized by
    BACK_LAYER_SPLIT), its own size range (BACK_LAYER_HEIGHT/WIDTH_FRAC), and
    its own darken/haze applied to silhouette_color()'s output -- see the
    BACK_LAYER_* constants for what each controls.
    """
    n = max(1, BACK_LAYERS)
    splits = _stretch(BACK_LAYER_SPLIT, n)
    h_fracs = _stretch(BACK_LAYER_HEIGHT_FRAC, n)
    w_fracs = _stretch(BACK_LAYER_WIDTH_FRAC, n)
    darkens = _stretch(BACK_LAYER_DARKEN, n)
    hazes = _stretch(BACK_LAYER_HAZE, n)

    pool = _sway_pool(sync_config, max(n, BACK_OBJECTS), seed=seed_base)
    weight_total = sum(splits) or 1.0
    counts = [max(1, round(len(pool) * w / weight_total)) for w in splits]

    # density-based shrink: more objects -> smaller objects (see
    # BACK_DENSITY_SCALE_ENABLED / BACK_DENSITY_REF / BACK_DENSITY_POWER above)
    if BACK_DENSITY_SCALE_ENABLED:
        density_scale = (BACK_DENSITY_REF / max(1, len(pool))) ** BACK_DENSITY_POWER
    else:
        density_scale = 1.0

    bands = []
    idx = 0
    for i in range(n):
        entries = pool[idx:idx + counts[i]] or pool[:1]
        idx += counts[i]
        max_h = max(4, int(canvas_h * h_fracs[i] * density_scale))
        max_w = max(4, int(canvas_w * w_fracs[i] * density_scale))
        obj_h_range = (max(4, int(max_h * 0.55)), max_h)
        # raise_range (how far the "seabed" stem lifts each crown above the
        # canvas floor) is sized off the UNSCALED reference height, not the
        # density-shrunk max_h -- otherwise denser/smaller corals also sit
        # lower, and past a certain density they never clear the visible
        # window at all (reported as "background completely empty").
        ref_h = max(4, int(canvas_h * h_fracs[i]))
        raise_range = (int(ref_h * 0.12), int(ref_h * 0.45))
        frames = _make_sway_layer(
            obj_dir, object_fill, silhouette_color, entries, canvas_w,
            canvas_h, obj_h_range, raise_range, n_frames,
            seed=seed_base + i * 17, max_h=max_h, max_w=max_w)
        bands.append((frames, darkens[i], hazes[i]))
    return bands


def make_back2(obj_dir, object_fill, silhouette_color, sync_config,
              frame=0, sky_colour=None, _cache={}):
    """The one TRULY animated background layer (back2.spr), ONE frame:
    BACK_LAYERS depth bands (see _sway_bands) composited back-to-front, each
    darkened/hazed by its own band settings so the stack reads front-to-back
    as depth rather than as flat layered copies.

    `obj_dir`, `object_fill`, `silhouette_color` and `sync_config` are
    build_coral.py's own OBJ_DIR / object_fill() / silhouette_color() /
    sync_config() -- passed in rather than imported, so this module and
    build_coral.py do not import each other in a cycle.
    """
    key = "stack"
    if key not in _cache:
        bands = _sway_bands(obj_dir, object_fill, silhouette_color,
                            sync_config, BACK2_W, BACK2_H, BACK2_FRAMES,
                            seed_base=31)
        haze_amt = max(0.0, BACK_HAZYNESS)
        composed = []
        for f in range(BACK2_FRAMES):
            canvas = Image.new("RGBA", (BACK2_W, BACK2_H), (0, 0, 0, 0))
            # bands are ordered nearest-first; paint farthest-first so nearer
            # (brighter, more colourful) bands end up on top, not buried under
            # the darker/hazier far ones.
            for frames, darken, haze in reversed(bands):
                fr = frames[f % len(frames)]
                if darken != 1.0:
                    r, g, b, a = fr.split()
                    rgb = Image.merge("RGB", (r, g, b)).point(
                        lambda v, d=darken: max(0, min(255, int(v * d))))
                    fr = Image.merge("RGBA", (*rgb.split(), a))
                blend = min(1.0, max(0.0, haze * haze_amt))
                if sky_colour and blend > 0:
                    r, g, b, a = fr.split()
                    rgb = Image.merge("RGB", (r, g, b))
                    sky_im = Image.new("RGB", rgb.size, sky_colour)
                    rgb = Image.blend(rgb, sky_im, blend)
                    fr = Image.merge("RGBA", (*rgb.split(), a))
                canvas.alpha_composite(fr)
            if BACK2_STEM_HIDE_Y < BACK2_H:
                # The frame reads as two bands: CROWNS on top (rows
                # 0..BACK2_STEM_HIDE_Y) and, below them, the bare STEMS that
                # carry each crown down to the floor. At high resolutions the
                # stem band is on screen, and bare trunks are not what should
                # be down there.
                #
                # So: copy the frame, roll it half a canvas-width sideways,
                # and paste that copy's CROWN band over the stem band --
                # crowns covering stems, not stems covering stems. The roll is
                # free of seams because every sway object is already pasted at
                # x-canvas_w/x/x+canvas_w (see _make_sway_layer), so the strip
                # tiles horizontally and any x-phase of it is as valid as the
                # one we started with; the shift is what stops each crown
                # landing directly on the stem it grew from.
                #
                # The crown band is taller than the stem band it fills, so its
                # bottom rows fall off the canvas and are cropped. Composited
                # (not pasted flat) so the original stems still show through
                # the gaps between crowns and keep reading as connected.
                shift = BACK2_W // 2
                rolled = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                rolled.paste(canvas, (shift, 0))
                rolled.paste(canvas, (shift - BACK2_W, 0))
                crowns = rolled.crop((0, 0, BACK2_W, BACK2_STEM_HIDE_Y))
                canvas.alpha_composite(crowns, (0, BACK2_STEM_HIDE_Y))
            composed.append(canvas)
        _cache[key] = composed
    return _cache[key][frame % len(_cache[key])]


def shift_frames_down(strip, w, h, n_frames, lower_px):
    """Crop `lower_px` rows off the BOTTOM of every frame in a P-mode strip
    (frames stacked vertically, each h tall) and pad that much transparent
    (index 0) space onto the TOP instead -- shifts the whole image down
    without changing frame size. Used to build front.spr from back2's own
    strip; see FRONT_LOWER above."""
    lower_px = max(0, min(h, lower_px))
    if lower_px == 0:
        return strip
    out = Image.new("P", strip.size, 0)
    out.putpalette(strip.getpalette())
    keep_h = h - lower_px
    for i in range(n_frames):
        if keep_h <= 0:
            continue
        region = strip.crop((0, i * h, w, i * h + keep_h))
        out.paste(region, (0, i * h + lower_px))
    return out

# The front.spr measuring ruler used to live here (make_front_ruler) but has
# moved to make_front.py -- it is now a front SUBLAYER (composited on top of
# the real content instead of replacing it), so the whole front pipeline,
# real content and its own debug tooling, lives in one file. See
# make_front._build_ruler / make_front.FRONT_RULER.


# ---- back2's own measuring ruler -------------------------------------------
# back2.spr is 1024x410, a different canvas from front.spr's 1024x250 and
# back.spr's 640x160, so it needs its own correctly-sized overlay rather than
# either of theirs.
#
# Like the others this is an OVERLAY, not a replacement: it returns a
# transparent RGBA frame with only ticks/labels/crosshairs opaque, so the
# real swaying reef stays visible underneath while measuring. build_coral.py
# composites it AFTER quantisation into reserved high palette indices (see
# _composite_ruler_onto_strip there), which keeps the tick colours exact and
# costs the master palette nothing.
#
# Deliberately reuses make_front.make_front_ruler rather than duplicating the
# drawing code -- that function already takes (w, h) and is generic. Imported
# lazily INSIDE the function because make_front.py imports THIS module at
# import time; a module-level import here would be a cycle.
BACK2_RULER = False


def make_back2_ruler_layer():
    """back2's ruler as one RGBA frame, or None if BACK2_RULER is off.

    Static (same frame every tick), so callers composite it onto every frame
    of the strip rather than regenerating it per frame.
    """
    if not BACK2_RULER:
        return None
    from make_front import make_front_ruler   # lazy: see BACK2_RULER above
    return make_front_ruler(BACK2_W, BACK2_H, name="back2.spr")
