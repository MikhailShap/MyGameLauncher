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
    # Проверяем PyInstaller
    try:
        import PyInstaller
        print("✓ PyInstaller найден")
    except ImportError:
        print("Устанавливаю PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    
    # Параметры сборки
    app_name = "CyberLauncher"
    main_file = "main.py"
    icon_file = "icon.ico"
    
    # Дополнительные файлы для включения
    add_data = [
        f"game_manager.py;.",  # Копируем game_manager.py
        f"{icon_file};.",      # Копируем иконку
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
    ]
    
    # Формируем команду
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", app_name,
        "--onefile",           # Один exe файл
        "--windowed",          # Без консоли (GUI приложение)
        "--icon", icon_file,   # Иконка exe
        "--clean",             # Очистить кэш перед сборкой
        "--noconfirm",         # Не спрашивать подтверждение
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
        print(f"✓ Сборка завершена успешно!")
        print(f"✓ Файл: dist/{app_name}.exe")
        print("=" * 50)
    else:
        print("\n✗ Ошибка сборки!")
        sys.exit(1)

if __name__ == "__main__":
    build()