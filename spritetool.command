#!/bin/bash
# Double-click this to open the spritetool window on macOS.
#
# A .command file is what Finder will run on a double-click; a plain .sh
# opens in a text editor instead. It needs the executable bit, which git
# records -- if it was lost, `chmod +x spritetool.command` puts it back.
#
# Everything here is about the two ways a double-click fails silently: the
# window never appears and Terminal closes before anyone can read why. So
# the script finds a Python for itself, says what is missing in plain words,
# and holds the window open on any failure.

cd "$(dirname "$0")" || exit 1

say_and_wait() {
    echo
    echo "$1"
    echo
    echo "Press return to close this window."
    read -r _
    exit 1
}

# The stock macOS /usr/bin/python3 is not always the one with PySide6 in it,
# and Homebrew's is not always first on a double-click PATH -- Finder does
# not read your shell profile. Try each, and take the first that can import
# the window's dependencies rather than the first that merely exists.
#
# .venv first: PySide6 is ~400 MB and belongs to the machine rather than the
# repo, so the usual way to have it here is a virtualenv beside this file.
# Anything on PATH would otherwise win and report the dependency missing
# while it sits installed a directory away.
PYTHON=""
for candidate in ./.venv/bin/python3 ./.venv/bin/python \
                 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                 /usr/bin/python3; do
    # -x for the venv paths, command -v for the bare names: `command -v` on a
    # relative path is not dependable across shells, and a bare name is not a
    # file to test with -x.
    if { [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; } \
       && "$candidate" -c 'import PySide6, PIL' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    # Nothing complete was found. Work out which half is missing, using
    # whatever python3 exists, so the message names the actual problem.
    FALLBACK="$(command -v python3 || echo /usr/bin/python3)"
    if ! [ -x "$FALLBACK" ]; then
        say_and_wait "spritetool needs Python 3.

Install it from https://www.python.org/downloads/ and double-click
this file again."
    fi
    MISSING=""
    "$FALLBACK" -c 'import PySide6' >/dev/null 2>&1 || MISSING="PySide6"
    "$FALLBACK" -c 'import PIL' >/dev/null 2>&1 \
        || MISSING="${MISSING:+$MISSING and }Pillow"
    if [ -z "$MISSING" ]; then
        # Both import one at a time but not together, or they import here and
        # not where the window runs. Nothing to name, so show the real error
        # rather than a confident sentence about a package that is present.
        echo
        echo "spritetool could not start the window. Python reported:"
        echo
        "$FALLBACK" -c 'import PySide6, PIL' 2>&1 | tail -5
        say_and_wait "Run this to see the whole error:

    $FALLBACK -m gui"
    fi
    # Offer to do it. Telling a novice to run pip is telling them to hit
    # PEP 668: a Homebrew or system Python refuses to install into itself
    # and suggests --break-system-packages, which is the last thing anyone
    # should paste into a terminal they do not know. A virtual environment
    # sidesteps that entirely and is one folder they can delete.
    echo
    echo "spritetool needs two things it can install for you:"
    echo
    echo "  Pillow      15 MB   reads and writes the pictures"
    echo "  PySide6    364 MB   draws the window itself"
    echo
    echo "They go into a .venv folder beside this file and touch nothing"
    echo "else on your Mac. Delete that folder to undo it."
    echo
    printf "Install them now? [y/N] "
    read -r answer
    case "$answer" in
        [Yy]*) ;;
        *)
            say_and_wait "Nothing installed.

To do it yourself:

    $FALLBACK -m venv .venv
    .venv/bin/python -m pip install pillow PySide6-Essentials

then double-click this file again."
            ;;
    esac

    echo
    echo "Creating .venv..."
    if ! "$FALLBACK" -m venv .venv; then
        say_and_wait "Could not create the .venv folder.

Check that you can write to:
    $(pwd)"
    fi
    echo "Downloading (this takes a few minutes the first time)..."
    # PySide6-Essentials, not PySide6: the meta-package pulls in Addons too
    # -- WebEngine, 3D, Multimedia, Charts -- for 1.2 GB against 364 MB, and
    # the window uses QtCore, QtGui and QtWidgets, all of which are here.
    # Warnings from pip's own cache are not this author's business; the
    # exit status is what says whether it worked. Errors still show, since
    # only stderr's chatter is dropped and a real failure says so below.
    if ! ./.venv/bin/python -m pip install --quiet --no-cache-dir \
            pillow PySide6-Essentials 2>/dev/null; then
        say_and_wait "The download did not finish.

Try again, or do it yourself:

    ./.venv/bin/python -m pip install pillow PySide6-Essentials"
    fi
    if ! ./.venv/bin/python -c 'import PySide6, PIL' >/dev/null 2>&1; then
        say_and_wait "The install finished but the window still cannot start.

Run this to see why:

    ./.venv/bin/python -m gui"
    fi
    PYTHON=./.venv/bin/python
    echo "Done."
    echo
fi

"$PYTHON" -m gui "$@"
status=$?

# Only pause on a failure. A window that lingers after every ordinary quit
# is a nuisance; one that vanishes on a crash hides the reason.
if [ $status -ne 0 ]; then
    echo
    echo "spritetool stopped with an error (exit $status)."
    echo
    echo "Press return to close this window."
    read -r _
fi
exit $status
