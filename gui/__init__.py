"""A window around spritetool.

`gui` imports `spritetool`; `spritetool` never imports `gui`. The tool stays
usable, testable and installable with nothing but Pillow, and the window is
something added on top rather than a second way to maintain the same code.

    python -m gui              the window
    python -m gui --cli ...    the command-line tool, same binary
"""

import os as _os
import sys as _sys


def bootstrap():
    """Put the folder holding `spritetool.py` on the import path.

    Two layouts have to work. A source checkout keeps everything side by
    side, so the folder above `gui` holds the tool and its modules. An
    installed folder keeps only what a person double-clicks at the top --
    the launchers and `spritetool.py` -- with the working parts one level
    down, so `gui` sits in the subfolder and `spritetool.py` is in its
    parent. Both roots are added rather than chosen between: whichever one
    is real, the imports below find it, and adding a folder that holds
    nothing costs an entry on a list.

    Called on import, so anything reaching `gui` gets a usable path without
    having to remember this -- including a spawned pack worker, which comes
    up in a fresh interpreter with none of the parent's setup.
    """
    here = _os.path.dirname(_os.path.abspath(__file__))
    for root in (_os.path.dirname(here),
                 _os.path.dirname(_os.path.dirname(here))):
        if root and root not in _sys.path:
            _sys.path.insert(0, root)


bootstrap()
