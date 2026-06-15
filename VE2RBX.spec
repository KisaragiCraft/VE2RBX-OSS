# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app\\launcher\\local_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('app\\static', 'app\\static'), ('converter', 'converter')],
    hiddenimports=['glob', 'hashlib', 'datetime', 're', 'struct', 'math', 'io', 'argparse', 'logging', 'ctypes', 'app.server.main', 'app.server.jobs', 'app.server.paths'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VE2RBX',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['converter\\VE2RBXicon.ico'],
)
