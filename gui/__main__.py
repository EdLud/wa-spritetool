"""Entry point: `python -m gui`.

freeze_support() comes first, before Qt is imported and before an argument is
read. A spawned worker re-executes this program to reach the function it was
given; in a frozen build there is no script to re-execute, so it re-runs the
bundle -- which would start the window again, which packs, which spawns more.
freeze_support() recognises that re-entry, does the worker's job and exits.

Importing Qt above it would be worse than pointless: every one of those
re-entrant copies would load PySide6 before finding out it is not the app.
"""

import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()

    # --cli hands the whole thing to the command-line tool, so one frozen
    # binary can be tested against the same fixtures as the source. Without
    # it a packaged build could only be smoke-tested through the window.
    if '--cli' in sys.argv[1:2]:
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import spritetool
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        return spritetool.main()

    from .app import main as gui_main
    return gui_main()


if __name__ == '__main__':
    sys.exit(main())
