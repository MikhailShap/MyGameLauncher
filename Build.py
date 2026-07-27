"""
Скрипт сборки CyberLauncher в .exe

Использование:
    python build.py

Требования:
    pip install pyinstaller

После сборки .exe будет в папке dist/
"""

import subprocess
import sys
import os

def build():
    # Если запущены НЕ из venv (например, `python Build.py` без активации) —
    # перезапускаемся через venv/Scripts/python.exe. Иначе PyInstaller соберёт
    # exe без pygame/SteamGridDB/прочих зависимостей, которые есть только
    # в venv, и геймпад/иконки молча отвалятся в дистрибутиве.
    here = os.path.dirname(os.path.abspath(__file__))
    venv_py = os.path.join(here, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py) and os.path.abspath(sys.executable).lower() != os.path.abspath(venv_py).lower():
        print(f"[INFO] Re-launching via venv python: {venv_py}")
        os.execv(venv_py, [venv_py, os.path.abspath(__file__)])

    # Проверка критичных рантайм-зависимостей. Если падаем здесь — значит
    # venv не настроен / pip install не дотянул.
    missing = []
    for mod in ("pygame", "pygame._sdl2.controller", "PIL", "flet"):
        try:
            __import__(mod)
        except ImportError as e:
            missing.append(f"{mod} ({e})")
    if missing:
        print("[ERROR] Missing dependencies in current Python:")
        for m in missing:
            print(f"   - {m}")
        print(f"   python: {sys.executable}")
        print("Activate venv or run: pip install -r requirements.txt")
        sys.exit(1)

    # Проверяем PyInstaller
    try:
        import PyInstaller
        print("[OK] PyInstaller found")
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    
    # Параметры сборки
    app_name = "CyberLauncher"
    main_file = "main.py"
    icon_file = "icon.ico"
    
    # Дополнительные файлы для включения
    add_data = [
        f"game_manager.py;.",      # Копируем game_manager.py
        f"gamepad_manager.py;.",   # Менеджер геймпада
        f"bigpicture_view.py;.",   # PS-стиль режим
        f"wishlist_manager.py;.",  # Раздел "Желаемое" (Steam wishlist)
        f"steam_owned.py;.",       # Купленные в Steam, но не установленные игры
        f"trailer_proxy.py;.",     # Локальный HLS-прокси для трейлеров
        f"flet_patches.py;.",      # Патчи Flet 0.80 (repr + memory-guard)
        f"{icon_file};.",          # Копируем иконку
    ]

    # Скрытые импорты (модули которые PyInstaller может не найти)
    hidden_imports = [
        "win32gui",
        "win32ui",
        "win32con",
        "win32api",
        "pefile",
        "PIL",
        "PIL.Image",
        "flet",
        "flet_desktop",
        "icoextract",
        "duckduckgo_search",
        "curl_cffi",
        "primp",
        "pystray",
        "pystray._win32",
        "pygame",
        "pygame.joystick",
        "pygame._sdl2",
        "pygame._sdl2.controller",
        "gamepad_manager",
        "bigpicture_view",
        "wishlist_manager",
        "steam_owned",
        "trailer_proxy",
        "flet_patches",
        "flet_video",
        "send2trash",
        "send2trash.win",
        "send2trash.win.legacy",
        "send2trash.util",
    ]
    
    # Формируем команду
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", app_name,
        "--onedir",            # Папка вместо одного exe — быстрый старт, без распаковки в TEMP
        "--windowed",          # Без консоли (GUI приложение)
        "--icon", icon_file,   # Иконка exe
        "--clean",             # Очистить кэш перед сборкой
        "--noconfirm",         # Не спрашивать подтверждение
        "--noupx",             # БЕЗ UPX — иначе медленный старт и анимации
        # PyInstaller-хук для pygame в свежих pygame-ce не копирует
        # pygame/_sdl2/__init__.py, из-за чего "import pygame._sdl2.controller"
        # падает в frozen-сборке и GamepadManager.available=False → геймпад
        # не определяется. --collect-all гарантирует, что весь пакет
        # (исходники, .pyd, данные) попадёт в _internal/pygame/.
        "--collect-all", "pygame",
        # flet_video — Python-сторона видео-контрола. Нативные media_kit/libmpv
        # DLL уже лежат в flet_desktop/app/flet, так что нужен только пакет.
        "--collect-all", "flet_video",
    ]
    
    # Добавляем данные
    for data in add_data:
        cmd.extend(["--add-data", data])
    
    # Добавляем скрытые импорты
    for imp in hidden_imports:
        cmd.extend(["--hidden-import", imp])
    
    # Добавляем главный файл
    cmd.append(main_file)
    
    print(f"\nЗапускаю сборку: {app_name}")
    print("=" * 50)
    
    # Запускаем сборку
    result = subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
    
    if result.returncode == 0:
        print("\n" + "=" * 50)
        print(f"[OK] Build completed successfully!")
        print(f"[OK] File: dist/{app_name}.exe")
        print("=" * 50)
    else:
        print("\n[ERROR] Build failed!")
        sys.exit(1)

if __name__ == "__main__":
    build()
