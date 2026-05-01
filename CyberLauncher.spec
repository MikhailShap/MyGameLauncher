# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('game_manager.py', '.'), ('gamepad_manager.py', '.'), ('bigpicture_view.py', '.'), ('icon.ico', '.')],
    hiddenimports=['win32gui', 'win32ui', 'win32con', 'win32api', 'pefile', 'PIL', 'PIL.Image', 'flet', 'flet_desktop', 'icoextract', 'duckduckgo_search', 'curl_cffi', 'primp', 'pystray', 'pystray._win32', 'pygame', 'pygame.joystick', 'pygame._sdl2', 'pygame._sdl2.controller', 'gamepad_manager', 'bigpicture_view'],
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
    [],
    exclude_binaries=True,
    name='CyberLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CyberLauncher',
)
