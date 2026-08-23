"""A window around spritetool.

`gui` imports `spritetool`; `spritetool` never imports `gui`. The tool stays
usable, testable and installable with nothing but Pillow, and the window is
something added on top rather than a second way to maintain the same code.

    python -m gui              the window
    python -m gui --cli ...    the command-line tool, same binary
"""
