#!/usr/bin/env python3
"""Run every animation tool in extras/ and collect one GIF from each.

A quick look at what they all currently produce -- useful after changing a
shared module, where a break might only show up as motion going wrong rather
than as an error. Each GIF lands in extras/previews/<tool>.gif, which is
gitignored.

    ./preview_extras_gifs.py              every tool
    ./preview_extras_gifs.py make-flies   just one
    ./preview_extras_gifs.py --keep       leave the PNG strips behind too

Only the animation tools are here. The rest of extras/ makes textures, sprite
files and single pictures, and there is nothing to watch.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEWS = os.path.join(HERE, "previews")

# Small and fast beats representative: this is a smoke test, not a render.
# Each entry is the arguments to run with, and how the GIF comes out --
# "native" for the tools that write one themselves given --gif, "strip" for
# the ones that only write a PNG sprite strip, which gif.py then animates.
TOOLS = {
    "make-anim-test": {
        # Its circles are sized against the canvas, so a small one draws
        # nothing at all -- 200x200 comes out empty. Keep the real 400x400.
        "args": ["-n", "40", "-W", "400", "-H", "400"],
        "gif": "native",
    },
    "make-flies": {
        "args": ["-n", "40", "-N", "25", "-W", "200", "-H", "120"],
        "gif": "native",
    },
    "make-back2": {
        # A reef swaying. Slow: every frame composites four depth bands of
        # cutouts, so a preview takes a handful rather than its real 30.
        "args": ["-n", "6"],
        "gif": "native",
    },
    "make-grass-wind": {
        "args": ["-n", "30", "-W", "256", "-H", "120"],
        "gif": "native",
    },
    "make-fly-bursts": {
        # Reads a gfx0 dump for its frame geometry, which this repo does not
        # ship -- so it is only run when one is pointed at.
        "args": ["--gif"],
        "gif": "folder",
        "needs_env": "GFX0_DIR",
    },
    "make-bubbles": {
        "args": ["--width", "192", "--height", "48", "--frames", "60"],
        "gif": "strip",
        "frame_size": (192, 48),
        "frames": 60,
        "bed": (18, 34, 60),
    },
}


def _describe(path):
    """(frame count, whether anything is drawn) for a GIF.

    A preview that came out blank is the failure worth catching: the tool
    exits cleanly, writes a file, and there is simply nothing in it.
    """
    from PIL import Image
    im = Image.open(path)
    frames = 0
    drawn = False
    try:
        while True:
            im.seek(frames)
            if not drawn:
                colours = im.convert("RGB").getcolors(maxcolors=1 << 16)
                drawn = colours is not None and len(colours) > 1
            frames += 1
    except EOFError:
        pass
    return frames, drawn


def run(tool, spec, keep):
    """Produce one GIF for `tool`. Returns a line describing what happened."""
    script = os.path.join(HERE, tool, f"{tool}.py")
    if not os.path.exists(script):
        return f"{tool}: no script"
    if spec.get("needs_env") and not os.environ.get(spec["needs_env"]):
        return f"{tool}: skipped, needs ${spec['needs_env']}"

    out_gif = os.path.join(PREVIEWS, f"{tool}.gif")
    work = tempfile.mkdtemp(prefix=f"{tool}-")
    try:
        args = list(spec["args"])
        if spec["gif"] == "native":
            strip = os.path.join(work, "out.png")
            args += ["-o", strip, "--gif", out_gif]
        elif spec["gif"] == "folder":
            args += ["-o", work]
        else:
            args += ["-o", os.path.join(work, "out.png")]

        r = subprocess.run([sys.executable, script] + args,
                           capture_output=True, text=True, cwd=os.path.join(HERE, tool))
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()
            return f"{tool}: failed -- {tail[-1][:70] if tail else 'no output'}"

        if spec["gif"] == "strip":
            from PIL import Image
            sys.path.insert(0, HERE)
            import gif as gifmod
            sheet = Image.open(os.path.join(work, "out.png"))
            w, h = spec["frame_size"]
            gifmod.save(sheet, out_gif, spec["frames"], h,
                        duration=50, bed=spec.get("bed", (0, 0, 0)))
        elif spec["gif"] == "folder":
            # This one writes a GIF per sprite; take the largest as the sample.
            found = [os.path.join(d, f) for d, _, fs in os.walk(work)
                     for f in fs if f.endswith(".gif")]
            if not found:
                return f"{tool}: ran, but wrote no GIF"
            shutil.copy(max(found, key=os.path.getsize), out_gif)

        if keep:
            for d, _, fs in os.walk(work):
                for f in fs:
                    if f.endswith(".png"):
                        shutil.copy(os.path.join(d, f),
                                    os.path.join(PREVIEWS, f"{tool}-{f}"))

        if not os.path.exists(out_gif):
            return f"{tool}: ran, but no GIF at {os.path.basename(out_gif)}"
        frames, drawn = _describe(out_gif)
        if not drawn:
            return (f"{tool}: EMPTY -- {frames} frames, nothing drawn "
                    f"(the arguments here may be too small for it)")
        return f"{tool}: {frames} frames, {os.path.getsize(out_gif) // 1024} KB"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("only", nargs="*", help="run just these tools")
    p.add_argument("--keep", action="store_true",
                   help="also copy the PNG strips into previews/")
    a = p.parse_args()

    wanted = a.only or list(TOOLS)
    unknown = [t for t in wanted if t not in TOOLS]
    if unknown:
        p.error(f"not an animation tool: {', '.join(unknown)}\n"
                f"choose from: {', '.join(TOOLS)}")

    os.makedirs(PREVIEWS, exist_ok=True)
    print(f"writing to {os.path.relpath(PREVIEWS)}/")
    bad = 0
    for tool in wanted:
        line = run(tool, TOOLS[tool], a.keep)
        print(f"  {line}")
        if "failed" in line or "no GIF" in line:
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
