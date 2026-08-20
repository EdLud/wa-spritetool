#!/usr/bin/env python3
"""Build the falling-debris source strip from a video clip.

W:A debris (debris.spr) is a single image with every animation frame stacked
vertically; the game spawns many instances that fall through the sky, each at a
random FPS in [frames/5 .. frames*9/20]. Transparency is palette index 0 (black),
exactly like objects.

This script turns Debris/debris1.mov into an RGBA source strip
(Debris/debris_src.png): FRAMES frames of FRAME x FRAME, luminance-keyed so the
dark background/core becomes transparent and only the bright dust strands remain.
build_terrain.py then remaps it to the terrain palette and emits debris.spr +
debris.spr.spd (frames/height/width from the constants below).

Frame count is a SPEED knob, not a smoothness knob: it sets the FPS range. 90
frames -> FPS 18..40 (lively but readable). Big counts (100s) push FPS into a
strobing blur AND bloat the BMP (SpriteEditor may choke), so keep it modest.

Transparency (luminance key): pixels darker than KEY_LO are fully transparent,
brighter than KEY_HI fully opaque, linear alpha between. Tune to taste.

Usage:
  python3 build_debris.py            # full FRAMES-frame strip -> debris_src.png
  python3 build_debris.py --preview  # a 4-frame contact sheet -> /tmp for review
"""
import os, sys, subprocess, tempfile, shutil, math
from PIL import Image, ImageChops, ImageFilter

HERE    = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(HERE, "Distant Planet")
DEBRIS  = os.path.join(PROJECT, "Debris")
SRC_MOV = os.path.join(DEBRIS, "debris1.mov")
OUT_PNG = os.path.join(DEBRIS, "debris_src.png")   # RGBA vertical strip

FRAME  = 418        # per-frame square size (px); debris.spr.spd width == height
FRAMES = 90      # SPEED knob. NOTE: the OS/2 BMP height is 16-bit (max 65535px),
                   # so FRAMES*FRAME must stay <= 65535 (=> <=2184 frames at 30px)
                   # or the BMP writer overflows. FPS = FRAMES/5 .. FRAMES*9/20.
KEY_LO = 40        # luminance <= this -> fully transparent
KEY_HI = 85        # luminance >= this -> fully opaque (linear alpha between)
# The source is a FULL-FRAME swirl, so a bare luminance key leaves a ragged
# square. A radial vignette multiplies the keyed alpha by a soft circular
# falloff so each frame reads as a rounded, tumbling dust-ball fading to
# transparency at the edges (dense core .. wispy rim). inner/outer are fractions
# of the half-width: solid within inner, gone beyond outer. Set USE_VIGNETTE
# False to keep the full swirl (wispier, but square-ish/ragged at frame edges).
USE_VIGNETTE   = True
VIGNETTE_INNER = 0.55
VIGNETTE_OUTER = 0.98


def extract_frames(n, size):
    """Extract n frames evenly across the whole clip, each scaled to size x size.

    The first n CONSECUTIVE frames from the start of the clip, so the debris
    animates as smooth continuous motion (adjacent frames are one source frame
    apart, not spread across the whole clip which would look choppy). At 60fps,
    90 frames is the first 1.5s of the clip.
    """
    tmp = tempfile.mkdtemp(prefix="debris_")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", SRC_MOV,
         "-vf", f"scale={size}:{size}", "-frames:v", str(n),
         os.path.join(tmp, "f%04d.png")], check=True)
    files = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
    frames = [Image.open(os.path.join(tmp, f)).convert("RGB").copy() for f in files]
    shutil.rmtree(tmp)
    return frames[:n]


def radial_mask(size, inner, outer):
    """Soft circular falloff: 255 within `inner`*half-width, 0 beyond `outer`."""
    m = Image.new("L", (size, size), 0)
    px = m.load()
    c = (size - 1) / 2
    span = max(1e-6, outer - inner)
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / (size / 2)
            if d <= inner:
                px[x, y] = 255
            elif d >= outer:
                px[x, y] = 0
            else:
                px[x, y] = int((outer - d) / span * 255)
    return m


def luminance_key(rgb, lo, hi, vignette=None):
    """RGBA copy: alpha = luminance ramp lo..hi, optionally multiplied by a
    radial vignette mask and lightly de-speckled so the dust reads as a rounded
    tumbling blob rather than a ragged square."""
    gray = rgb.convert("L")
    span = max(1, hi - lo)
    alpha = gray.point(lambda v: 0 if v <= lo else (255 if v >= hi
                                                    else int((v - lo) * 255 / span)))
    if vignette is not None:
        alpha = ImageChops.multiply(alpha, vignette)
    alpha = alpha.filter(ImageFilter.MedianFilter(3))   # de-speckle
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def main():
    if not os.path.exists(SRC_MOV):
        sys.exit(f"no source video at {SRC_MOV}")
    preview = "--preview" in sys.argv

    vig = radial_mask(FRAME, VIGNETTE_INNER, VIGNETTE_OUTER) if USE_VIGNETTE else None

    if preview:
        frames = extract_frames(4, FRAME)
        keyed = [luminance_key(f, KEY_LO, KEY_HI, vig) for f in frames]
        # contact sheet on magenta so transparency is obvious
        sheet = Image.new("RGBA", (FRAME * 4, FRAME), (255, 0, 255, 255))
        for i, k in enumerate(keyed):
            sheet.alpha_composite(k, (i * FRAME, 0))
        sheet = sheet.resize((sheet.width * 3, sheet.height * 3), Image.NEAREST)
        sheet.save("/tmp/debris_preview.png")
        print(f"preview -> /tmp/debris_preview.png  (key lo={KEY_LO} hi={KEY_HI})")
        return

    print(f"extracting {FRAMES} frames @ {FRAME}x{FRAME} ...")
    frames = extract_frames(FRAMES, FRAME)
    print(f"got {len(frames)} frames; luminance-keying (lo={KEY_LO} hi={KEY_HI}) ...")
    strip = Image.new("RGBA", (FRAME, FRAME * len(frames)), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        strip.alpha_composite(luminance_key(f, KEY_LO, KEY_HI, vig), (0, i * FRAME))
    strip.save(OUT_PNG)
    print(f"saved {OUT_PNG}  {strip.size}  ({len(frames)} frames, "
          f"FPS range {len(frames)//5}..{len(frames)*9//20})")


if __name__ == "__main__":
    main()
