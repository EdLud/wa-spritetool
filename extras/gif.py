#!/usr/bin/env python3
"""Write a sprite strip out as a GIF, to look at before the game sees it.

Two writers, because PIL's is not always usable. PIL MERGES byte-identical
consecutive frames, so a strip that holds still -- an intact frame before a
burn, a fly resting -- collapses into one multi-second frame that reads as a
static image. Where that matters, `save_ffmpeg` writes the frames out and
assembles them at a constant rate instead.
"""

import os
import subprocess
import shutil
import tempfile
from typing import Sequence, Tuple

try:
    from PIL import Image
except ImportError:                              # pragma: no cover
    Image = None

RGB = Tuple[int, int, int]


def frames_of(sheet, count: int, height: int):
    """Cut a vertical sprite strip into its frames."""
    return [sheet.crop((0, i * height, sheet.width, (i + 1) * height))
            for i in range(count)]


def save(sheet, path: str, frames: int, height: int, duration: int = 33,
         bed: RGB = (232, 232, 226)):
    """Write the strip as a GIF, flattened onto `bed`.

    GIF has no alpha blending, so the frames have to be composited onto some
    backdrop. Which one is worth choosing rather than defaulting: a glow or a
    halo can only be judged against the colour it will actually sit on.
    """
    imgs = []
    for f in frames_of(sheet, frames, height):
        under = Image.new("RGBA", f.size, tuple(bed) + (255,))
        imgs.append(Image.alpha_composite(under, f)
                    .convert("P", palette=Image.ADAPTIVE))
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=duration, loop=0, disposal=2)


def save_ffmpeg(images: Sequence, path: str, fps: int = 20,
                bed: RGB = (0, 0, 0), scale: int = 4):
    """Assemble RGBA frames with ffmpeg, keeping every frame.

    Use where frames repeat and PIL would collapse them. palettegen/paletteuse
    give the GIF clean colours; `scale` is nearest-neighbour, so single pixels
    stay legible.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("save_ffmpeg needs ffmpeg on PATH")
    tmp = tempfile.mkdtemp()
    try:
        w = h = None
        for i, fr in enumerate(images):
            under = Image.new("RGB", fr.size, bed)
            under.paste(fr.convert("RGB"), (0, 0), fr.convert("RGBA").getchannel("A"))
            under.save(os.path.join(tmp, f"f{i:04d}.png"))
            w, h = fr.size
        vf = (f"fps={fps},scale={w * scale}:{h * scale}:flags=neighbor,"
              "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-framerate", str(fps), "-i", os.path.join(tmp, "f%04d.png"),
             "-vf", vf, "-loop", "0", path], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def thumbnail(sheet, path: str, frames: int, height: int, every: int = 20,
              scale: int = 1, bed: RGB = (0, 0, 0)):
    """A contact sheet: every Nth frame side by side, to scan a long strip."""
    picked = frames_of(sheet, frames, height)[::every]
    w = sheet.width
    out = Image.new("RGB", (w * len(picked), height), bed)
    for i, f in enumerate(picked):
        out.paste(f.convert("RGB"), (i * w, 0))
    if scale != 1:
        out = out.resize((out.width * scale, out.height * scale), Image.NEAREST)
    out.save(path)
    return out
