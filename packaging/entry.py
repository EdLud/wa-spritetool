#!/usr/bin/env python3
"""What a frozen build starts at.

PyInstaller needs a script rather than a package, and `gui/__main__.py` is
not usable as one: it reaches its siblings with `from .app import ...`, and
a file run as `__main__` has no package for that to resolve against. So the
bundle starts here, and this hands straight over.

`freeze_support()` comes first and before any import that could reach Qt --
the same rule `gui/__main__.py` follows, for the same reason. A spawned pack
worker re-executes the bundle to reach the function it was given; without
this it would start the window again, which packs, which spawns more. The
import below is deliberately inside the guard so that re-entrant copy does
not pay for it.
"""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    from gui.__main__ import main as gui_main
    return gui_main()


if __name__ == '__main__':
    sys.exit(main())
