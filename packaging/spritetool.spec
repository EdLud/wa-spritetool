# -*- mode: python ; coding: utf-8 -*-
"""How a frozen spritetool is put together.

One spec for both platforms. What differs between them is handled at the
bottom -- macOS wants a .app bundle around the executable, Windows wants the
executable itself -- rather than in two files that would drift.

The art in `presets/` is the reason this is a spec rather than a command
line. It is data, not code, so nothing imports it and PyInstaller cannot
find it by following imports; it has to be listed. `defaults_roots()` looks
in `sys._MEIPASS` first for exactly this, so a bundle that carries it there
is one the setup flow can lend from.
"""
import os
import sys

# The repo root, which is where the modules and the art actually live. The
# spec is executed with SPECPATH set to its own folder, so the root is one
# level up -- a spec has no __file__ of its own to ask.
ROOT = os.path.dirname(os.path.abspath(SPECPATH))            # noqa: F821

block_cipher = None

a = Analysis(                                                # noqa: F821
    [os.path.join(SPECPATH, 'entry.py')],                    # noqa: F821
    pathex=[ROOT],
    binaries=[],
    # (source, destination inside the bundle). The tool looks for a folder
    # called `presets` beside itself, and _MEIPASS is what "beside itself"
    # means in a bundle.
    datas=[(os.path.join(ROOT, 'presets'), 'presets')],
    # settings_toml is imported by name from spritetool, which Analysis
    # follows, so it does not need listing. gui.job does not: it is reached
    # through multiprocessing by qualified name in a child process, and
    # nothing imports it along a path PyInstaller can see.
    hiddenimports=['gui', 'gui.app', 'gui.job', 'gui.bridge', 'settings_toml',
                   'spritetool'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is needed and each one is tens of megabytes. QtWebEngine
    # in particular doubles the bundle on its own.
    excludes=['tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.Qt3DCore', 'PySide6.QtQuick', 'PySide6.QtQml',
              'PySide6.QtMultimedia', 'PySide6.QtCharts', 'PySide6.QtDataVisualization'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)         # noqa: F821

exe = EXE(                                                   # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='spritetool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=False on Windows, so a double-click opens the window without a
    # black terminal behind it. On macOS the .app decides that and this is
    # ignored, so one value covers both.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(                                              # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='spritetool',
)

if sys.platform == 'darwin':
    # A bare executable on macOS opens without a Dock icon or a menu bar and
    # cannot be double-clicked from Finder in the way anyone expects. The
    # .app is what makes it an application rather than a program.
    app = BUNDLE(                                            # noqa: F821
        coll,
        name='spritetool.app',
        icon=None,
        bundle_identifier='com.spritetool.spritetool',
        info_plist={
            # Retina: without this the whole window renders at 1x and
            # upscales, which on a 40x60 sprite preview is very visible.
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': os.environ.get('SPRITETOOL_VERSION',
                                                         '0.0.0'),
        },
    )
