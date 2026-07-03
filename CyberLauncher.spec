# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('game_manager.py', '.'), ('gamepad_manager.py', '.'), ('bigpicture_view.py', '.'), ('wishlist_manager.py', '.'), ('icon.ico', '.')]
binaries = []
hiddenimports = ['win32gui', 'win32ui', 'win32con', 'win32api', 'pefile', 'PIL', 'PIL.Image', 'flet', 'flet_desktop', 'icoextract', 'duckduckgo_search', 'curl_cffi', 'primp', 'pystray', 'pystray._win32', 'pygame', 'pygame.joystick', 'pygame._sdl2', 'pygame._sdl2.controller', 'gamepad_manager', 'bigpicture_view', 'wishlist_manager', 'flet_video', 'send2trash', 'send2trash.win', 'send2trash.win.legacy', 'send2trash.util']
tmp_ret = collect_all('pygame')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('flet_video')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
