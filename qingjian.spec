# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe.

Build with:
    python -m PyInstaller --noconfirm qingjian.spec

The result is a folder, dist/MediaSorter, with MediaSorter.exe inside: ship the
whole folder. A one-file build unpacked about 250 MB into a temporary folder on
every launch, which cost two seconds before any of the program ran; UPX then
made Windows decompress the Qt libraries again on top of that.

The executable is named MediaSorter.exe on purpose. The interface is still
called 轻拣; a non-ASCII executable name reproduced a native-window crash on
the delivery machine, so the file name stays ASCII.
"""

from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("qingjian")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Never wanted, and each one is tens of megabytes.
        "tkinter", "matplotlib", "scipy", "cv2", "pandas", "hachoir",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtQuick3D",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtSerialPort", "PySide6.QtTest", "PySide6.QtDesigner",
    ],
    noarchive=False,
    # The self-check deliberately uses assertions for its own diagnostics;
    # production file-safety checks raise explicit TransactionError values.
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaSorter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    hide_console="hide-early",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MediaSorter",
)
