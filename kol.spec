# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for KoL Adaptive Lighting Control.

Build with:
    pyinstaller kol.spec --noconfirm

Output: dist/KoL/KoL.exe (one-directory bundle)
"""

import os
from pathlib import Path

block_cipher = None
ROOT = os.path.abspath(".")

a = Analysis(
    ["launcher.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # Bundle read-only static assets into the frozen package
        (os.path.join("dalicontrol", "static"), os.path.join("dalicontrol", "static")),
    ],
    hiddenimports=[
        # --- USB / Serial ---
        "hid",
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        # --- FastAPI / Uvicorn ---
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "fastapi",
        "pydantic",
        "starlette",
        "starlette.routing",
        "starlette.middleware",
        "anyio._backends._asyncio",
        # --- scikit-learn (lazy imports inside sklearn) ---
        "sklearn",
        "sklearn.ensemble",
        "sklearn.ensemble._forest",
        "sklearn.tree",
        "sklearn.tree._classes",
        "sklearn.utils._typedefs",
        "sklearn.neighbors._partition_nodes",
        # --- joblib ---
        "joblib",
        # --- dalicontrol package ---
        "dalicontrol",
        "dalicontrol.main",
        "dalicontrol.web_server",
        "dalicontrol.ai_operator",
        "dalicontrol.adaptive_engine",
        "dalicontrol.lamp_state",
        "dalicontrol.dali_controls",
        "dalicontrol.dali_transport",
        "dalicontrol.usb_occupancy",
        "dalicontrol.sensor_usb",
        "dalicontrol.cct_utils",
        "dalicontrol.energy_estimator",
        "dalicontrol.settings",
        "dalicontrol.preferences",
        "dalicontrol.paths",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # openai is optional and large — exclude it from the frozen build
    excludes=["openai", "tkinter", "matplotlib", "PIL", "numpy.testing"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KoL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Keep console window for logs and COM port diagnostics
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="KoL",
)
