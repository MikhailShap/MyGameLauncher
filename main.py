import flet as ft
import asyncio
import sys
import json
import re
import logging
import threading
import webbrowser
import os
import atexit
import subprocess
import time
from pathlib import Path
from tkinter import Tk, filedialog
from typing import Optional

# Windows-specific imports for tray and single instance
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

# System tray support
try:
    import pystray
    from PIL import Image
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# Импорт backend
from game_manager import GameManager, GameModel, Platform, Category, logger as backend_logger, get_app_data_dir

# Версия приложения. Менять только здесь — используется и для заголовка окна
# (через который FindWindowW находит лаунчер для restore из BigPicture), и
# для текста "О приложении". Должна совпадать с installer.iss → MyAppVersion.
APP_VERSION = "1.4.4"
WINDOW_TITLE = f"CyberLauncher v{APP_VERSION}"

# Опциональные модули геймпада и BigPicture
try:
    from gamepad_manager import GamepadManager
    HAS_GAMEPAD = True
except Exception as _e:
    GamepadManager = None
    HAS_GAMEPAD = False

try:
    from bigpicture_view import BigPictureView
except Exception as _e:
    BigPictureView = None

# --- SINGLE INSTANCE LOCK ---
LOCK_FILE = None
LOCK_HANDLE = None

def acquire_single_instance_lock():
    """Проверяет, запущен ли уже экземпляр приложения (Windows).

    ERROR_ALREADY_EXISTS говорит лишь о том что mutex кто-то держит — это
    может быть и живой инстанс, и зомби (Flet-runner породил python.exe,
    который умер аварийно, но handle ещё не освобождён OS, или powershell
    shell держит). Поэтому верификация: если окна с нашим title нет —
    значит это зомби, идём дальше и сами захватываем mutex; иначе передаём
    фокус живому окну и выходим."""
    global LOCK_FILE, LOCK_HANDLE

    if sys.platform != "win32":
        return True

    mutex_name = "CyberLauncher_SingleInstance_Mutex"
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    LOCK_HANDLE = kernel32.CreateMutexW(None, True, mutex_name)

    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        hwnd = 0
        try:
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        except Exception:
            hwnd = 0

        if hwnd:
            # Реальный живой инстанс — выводим его окно и выходим
            try:
                SW_RESTORE = 9
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            return False

        # Окна нет = зомби-mutex. Закрываем handle и идём как обычный
        # запуск. Системный mutex объект исчезнет когда последний хэндл
        # на него закроется (наш и зомбиных) — либо Windows GC его
        # подберёт. Для нас критично только не выйти молча.
        try:
            kernel32.CloseHandle(LOCK_HANDLE)
        except Exception:
            pass
        LOCK_HANDLE = None

    return True

def release_single_instance_lock():
    """Освобождает блокировку при закрытии"""
    global LOCK_HANDLE
    if LOCK_HANDLE and sys.platform == "win32":
        ctypes.windll.kernel32.ReleaseMutex(LOCK_HANDLE)
        ctypes.windll.kernel32.CloseHandle(LOCK_HANDLE)

# --- SYSTEM TRAY ---
TRAY_ICON = None
TRAY_APP_INSTANCE = None

def create_tray_icon():
    """Создает иконку в системном трее"""
    global TRAY_ICON

    if not HAS_TRAY:
        return None

    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base_dir, "icon.ico")

    try:
        if os.path.exists(icon_path):
            image = Image.open(icon_path)
        else:
            # Fallback: создаем простую иконку
            image = Image.new('RGB', (64, 64), color='#D500F9')
    except Exception:
        image = Image.new('RGB', (64, 64), color='#D500F9')

    def on_show(icon, item):
        """Показать окно"""
        global TRAY_APP_INSTANCE
        if TRAY_APP_INSTANCE and TRAY_APP_INSTANCE.page:
            try:
                page = TRAY_APP_INSTANCE.page
                page.window.visible = True
                page.window.minimized = False
                page.update()

                # Выводим окно на передний план через WinAPI
                if sys.platform == "win32":
                    try:
                        user32 = ctypes.windll.user32
                        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
                        if hwnd:
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            user32.SetForegroundWindow(hwnd)
                    except:
                        pass
            except Exception as e:
                print(f"Error showing window: {e}")

    def on_exit(icon, item):
        """Выход из приложения через трей-меню.

        Порядок важен:
        1) Sync cleanup (gamepad/save) — безопасно из tray-thread
        2) page.window.destroy() — сообщаем Flutter Engine что окно закрывается
           ИНТЕНЦИОНАЛЬНО. Без этого engine видит обрыв TCP при os._exit() и
           показывает встроенный "Working..." (ждёт reconnect Python).
        3) icon.stop() + os._exit
        """
        global TRAY_APP_INSTANCE
        try:
            if TRAY_APP_INSTANCE is not None:
                # 1a. Геймпад
                if getattr(TRAY_APP_INSTANCE, "gamepad_manager", None) is not None:
                    try:
                        TRAY_APP_INSTANCE.gamepad_manager.shutdown()
                    except Exception:
                        pass
                # 1b. Финальный flush библиотеки + остановка IconExtractor
                gm = getattr(TRAY_APP_INSTANCE, "game_manager", None)
                if gm is not None:
                    try:
                        gm.emergency_sync_save()
                    except Exception:
                        pass
                    try:
                        if hasattr(gm, "cover_api_manager") and hasattr(gm.cover_api_manager, "icon_extractor"):
                            gm.cover_api_manager.icon_extractor.shutdown(wait=False)
                    except Exception:
                        pass

                # 2. Корректно закрываем Flutter-окно ДО убийства Python.
                # destroy() — async корутина в Flet event loop'е. Запускаем
                # threadsafe из tray-thread'а и ждём чуть-чуть пока команда
                # доедет до engine. Без этого "Working..." висит навсегда.
                page = getattr(TRAY_APP_INSTANCE, "page", None)
                if page is not None:
                    try:
                        future = page.run_task(page.window.destroy)
                        try:
                            future.result(timeout=1.0)
                        except Exception:
                            pass
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error during tray exit: {e}")

        # 3. Останавливаем pystray и завершаем процесс
        try:
            icon.stop()
        except Exception:
            pass
        release_single_instance_lock()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Показать", on_show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", on_exit)
    )

    TRAY_ICON = pystray.Icon("CyberLauncher", image, "CyberLauncher", menu)
    return TRAY_ICON

def run_tray_icon():
    """Запускает иконку трея в отдельном потоке"""
    global TRAY_ICON
    if TRAY_ICON:
        TRAY_ICON.run()

def stop_tray_icon():
    """Останавливает иконку трея"""
    global TRAY_ICON
    if TRAY_ICON:
        TRAY_ICON.stop()

logger = logging.getLogger("MainUI")

# --- КОНФИГУРАЦИЯ ЦВЕТОВ И СТИЛЯ ---
BG_COLOR = "#0F0F0F"
SIDEBAR_COLOR = "#1A1A1A"
ACCENT_PURPLE = "#D500F9"
ACCENT_BLUE = "#00E5FF"
CARD_BG = "#1E1E1E"
TEXT_WHITE = "#FFFFFF"
TEXT_GREY = "#AAAAAA"

# Градиентные темы для фона
GRADIENT_THEMES = {
    "dark": {
        "name": "Тёмная",
        "colors": ["#0F0F0F", "#151520"],
        "preview": ["#0F0F0F", "#151520"],
        "sidebar": "#801A1A1A",
    },
    "midnight": {
        "name": "Полночь",
        "colors": ["#0a0a12", "#1a1a2e", "#16213e"],
        "preview": ["#0a0a12", "#16213e"],
        "sidebar": "#801a1a2e",
    },
    "ocean": {
        "name": "Океан",
        "colors": ["#0c1015", "#0d1b2a", "#1b263b"],
        "preview": ["#0c1015", "#1b263b"],
        "sidebar": "#800d1b2a",
    },
    "forest": {
        "name": "Лес",
        "colors": ["#0a0f0d", "#1a2f1f", "#0d1f15"],
        "preview": ["#0a0f0d", "#1a2f1f"],
        "sidebar": "#801a2f1f",
    },
    "purple": {
        "name": "Фиолетовая",
        "colors": ["#0f0a15", "#1a0a2e", "#2d1b4e"],
        "preview": ["#0f0a15", "#2d1b4e"],
        "sidebar": "#801a0a2e",
    },
    "crimson": {
        "name": "Бордовая",
        "colors": ["#120a0a", "#2d1515", "#1f0f0f"],
        "preview": ["#120a0a", "#2d1515"],
        "sidebar": "#802d1515",
    },
    "space": {
        "name": "Космос",
        "colors": ["#05050a", "#0f0f1a", "#1a1025"],
        "preview": ["#05050a", "#1a1025"],
        "sidebar": "#800f0f1a",
    },
    "carbon": {
        "name": "Карбон",
        "colors": ["#0a0a0a", "#141414", "#1e1e1e"],
        "preview": ["#0a0a0a", "#1e1e1e"],
        "sidebar": "#80141414",
    },
}


class SidebarButton(ft.Container):
    """Кнопка боковой панели с эффектом подсветки"""
    
    def __init__(self, icon, text, is_active=False, on_click=None, data=None):
        super().__init__()
        self.is_active = is_active
        self.button_data = data
        self.default_bg = "transparent"
        self.hover_bg = "#1A00E5FF"
        self.active_bg = "#33D500F9"
        self._on_click_handler = on_click
        
        self.content = ft.Row(
            controls=[
                ft.Icon(icon, color=TEXT_WHITE if is_active else TEXT_GREY, size=20),
                ft.Text(
                    text, 
                    color=TEXT_WHITE if is_active else TEXT_GREY,
                    size=14,
                    weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL
                )
            ],
            spacing=15
        )
        self.padding = ft.Padding(left=15, right=15, top=12, bottom=12)
        self.border_radius = 10
        self.bgcolor = self.active_bg if is_active else self.default_bg
        self.animate = ft.Animation(200, ft.AnimationCurve.EASE_OUT)
        self.on_hover = self.hover_effect
        self.on_click = self.click_handler
        self.cursor = ft.MouseCursor.CLICK

    def hover_effect(self, e):
        if not self.is_active:
            self.bgcolor = self.hover_bg if e.data == "true" else self.default_bg
            self.content.controls[0].color = TEXT_WHITE if e.data == "true" else TEXT_GREY
            self.content.controls[1].color = TEXT_WHITE if e.data == "true" else TEXT_GREY
            self.update()
    
    def click_handler(self, e):
        if self._on_click_handler:
            self._on_click_handler(self.button_data)
    
    def set_active(self, active: bool, skip_update: bool = True):
        """Set active state - skip_update=True for batch updates"""
        if self.is_active == active:
            return  # No change needed
        self.is_active = active
        self.bgcolor = self.active_bg if active else self.default_bg
        self.content.controls[0].color = TEXT_WHITE if active else TEXT_GREY
        self.content.controls[1].color = TEXT_WHITE if active else TEXT_GREY
        text_control = self.content.controls[1]
        text_control.weight = ft.FontWeight.W_600 if active else ft.FontWeight.NORMAL
        # Don't call self.update() - let caller do page.update() once


class GameCard(ft.Container):
    """Карточка игры с иконкой на весь фон - ОПТИМИЗИРОВАНО"""

    # Class-level cache for icon existence checks
    _icon_exists_cache = {}

    # Кешируем Border'ы один раз на класс, чтобы не аллоцировать новый объект
    # на каждый hover (12+ карточек × hover-events = заметная GC-нагрузка)
    _BORDER_NORMAL = ft.Border.all(1, "#333333")
    _BORDER_HOVER = ft.Border.all(2, ACCENT_BLUE)

    def __init__(self, game: GameModel, on_click=None, on_favorite=None, on_upload=None, on_exclude=None, on_properties=None, show_size=False, enable_animations=False):
        super().__init__()
        self.game = game
        self._on_click = on_click
        self._on_favorite = on_favorite
        self._on_upload = on_upload
        self._on_exclude = on_exclude
        self._on_properties = on_properties
        self._enable_animations = enable_animations
        self._is_hovered = False  # Track hover state to avoid redundant updates

        self.border_radius = 15
        self.padding = 0
        self.bgcolor = CARD_BG
        self.expand = True
        self.clip_behavior = ft.ClipBehavior.HARD_EDGE

        # Анимация hover. animate_scale — точечная анимация только scale
        # (GPU compositor effect, самое дешёвое во Flutter).
        if enable_animations:
            self.animate_scale = ft.Animation(120, ft.AnimationCurve.EASE_OUT)
            self.on_hover = self.on_card_hover

        self.shadow = None
        self.scale = 1.0
        self.border = GameCard._BORDER_NORMAL
        
        # Cache icon existence check
        icon_src = None
        has_icon = False
        if game.icon_path:
            if game.icon_path in GameCard._icon_exists_cache:
                has_icon = GameCard._icon_exists_cache[game.icon_path]
            else:
                has_icon = Path(game.icon_path).exists()
                GameCard._icon_exists_cache[game.icon_path] = has_icon
            if has_icon:
                icon_src = game.icon_path

        # Бейдж платформы
        platform_colors = {
            Platform.STEAM.value: "#1B2838",
            Platform.EPIC.value: "#2A2A2A", 
            Platform.SYSTEM.value: "#0078D4",
        }
        platform_color = platform_colors.get(game.platform, "#333333")
        
        platform_badge = ft.Container(
            content=ft.Text(
                game.platform,
                size=10,
                color="#FFFFFF",
                weight=ft.FontWeight.W_600
            ),
            bgcolor=platform_color,
            padding=ft.Padding(left=10, right=10, top=5, bottom=5),
            border_radius=10,
        )
        
        # ============================================================
        # ИСПРАВЛЕНИЕ: Используем строку "cover" вместо ft.ImageFit.COVER
        # ============================================================
        if has_icon:
            background = ft.Container(
                expand=True,
                image=ft.DecorationImage(
                    src=icon_src,
                    fit="cover",  # <-- ИСПРАВЛЕНО ЗДЕСЬ
                ),
            )
        else:
            # Заглушка если нет иконки
            background = ft.Container(
                expand=True,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment(0, -1),
                    end=ft.Alignment(0, 1),
                    colors=["#1a1a2e", "#16213e"],
                ),
                content=ft.Icon(
                    ft.Icons.SPORTS_ESPORTS,
                    size=80,
                    color="#FFD54F",
                ),
                alignment=ft.Alignment(0, 0),
            )
        
        # Название игры
        display_title = self._clean_title(game.title)
        
        game_title = ft.Text(
            display_title,
            size=13,
            weight=ft.FontWeight.W_600,
            color="#FFFFFF",
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        
        # Размер игры
        size_badge = None
        if show_size and game.install_path:
            game_size = self.get_folder_size(game.install_path)
            if game_size:
                size_badge = ft.Container(
                    content=ft.Text(
                        game_size,
                        size=10,
                        color="#FFFFFF",
                        weight=ft.FontWeight.W_500
                    ),
                    bgcolor="#80000000",
                    padding=ft.Padding(left=8, right=8, top=4, bottom=4),
                    border_radius=8,
                )
        
        # Собираем Stack - УПРОЩЁННАЯ СТРУКТУРА
        # Кликабельные кнопки используют Container с on_click напрямую
        stack_controls = [
            # 1. Фон с изображением
            background,
            
            # 2. Градиент затемнения снизу
            ft.Container(
                expand=True,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment(0, -1),
                    end=ft.Alignment(0, 1),
                    colors=["#00000000", "#00000000", "#CC000000"],
                    stops=[0.0, 0.5, 1.0],
                ),
            ),
            
            # 3. Кликабельная область для запуска игры
            ft.Container(
                expand=True,
                on_click=self.on_card_click,
            ),
            
            # 4. Бейдж платформы
            ft.Container(
                content=platform_badge,
                left=10,
                top=10,
            ),

            # 5. Название
            ft.Container(
                content=game_title,
                left=12,
                right=12,
                bottom=12,
            ),
            
            # 5. Кнопка избранного - ПРОСТОЙ Container с on_click
            ft.Container(
                content=ft.Icon(
                    ft.Icons.FAVORITE if game.is_favorite else ft.Icons.FAVORITE_BORDER,
                    color="#FF4081" if game.is_favorite else "#FFFFFF",
                    size=20,
                ),
                width=36,
                height=36,
                border_radius=18,
                bgcolor="#80000000",
                alignment=ft.Alignment(0, 0),
                right=8,
                top=8,
                on_click=self.on_favorite_click,
                ink=True,
            ),

            # 6-8. Дополнительные кнопки (Upload / Collection / Exclude).
            # Скрыты по умолчанию, показываются на hover. Это режет ~3 widget'а
            # × количество карточек из payload каждого page.update() — заметный
            # выигрыш на сетке 12+ карточек.
            self._build_secondary_actions(game),
        ]
        
        if size_badge:
            stack_controls.append(
                ft.Container(
                    content=size_badge,
                    left=10,
                    bottom=40,
                )
            )
        
        self.content = ft.Stack(
            controls=stack_controls,
            expand=True,
        )
    
    def _build_secondary_actions(self, game: GameModel) -> ft.Container:
        """Контейнер со второстепенными кнопками (Upload / Properties / Exclude).
        Добавление в коллекции перенесено на страницу самой коллекции —
        кнопка "Добавить игры" над сеткой, мульти-селект диалог."""
        upload_btn = ft.Container(
            content=ft.Icon(ft.Icons.IMAGE_SEARCH, color="#FFFFFF", size=18),
            width=36, height=36, border_radius=18,
            bgcolor="#8000E5FF",
            alignment=ft.Alignment(0, 0),
            on_click=self.on_upload_click,
            ink=True,
        )
        # Шестерёнка → диалог свойств игры (включая "запускать от админа").
        # Если у игры включён run_as_admin — подсвечиваем жёлтым щитом.
        is_admin = bool(getattr(game, "run_as_admin", False))
        properties_btn = ft.Container(
            content=ft.Icon(
                ft.Icons.SHIELD if is_admin else ft.Icons.SETTINGS,
                color="#FFD54F" if is_admin else "#FFFFFF",
                size=18,
            ),
            width=36, height=36, border_radius=18,
            bgcolor="#80FFB300" if is_admin else "#809E9E9E",
            alignment=ft.Alignment(0, 0),
            on_click=self.on_properties_click,
            ink=True,
            tooltip="Запуск от админа: ВКЛ" if is_admin else "Свойства игры",
        )
        exclude_btn = ft.Container(
            content=ft.Icon(ft.Icons.BLOCK, color="#FFFFFF", size=18),
            width=36, height=36, border_radius=18,
            bgcolor="#80F44336",
            alignment=ft.Alignment(0, 0),
            on_click=self.on_exclude_click,
            ink=True,
        )

        self._secondary_actions = ft.Container(
            right=8,
            top=52,
            content=ft.Column(
                controls=[upload_btn, properties_btn, exclude_btn],
                spacing=8,
            ),
        )
        return self._secondary_actions

    def _clean_title(self, title: str) -> str:
        """Очистка названия от тегов репаков"""
        if not title:
            return "Unknown Game"
        title = re.sub(r'\s*\[[^\]]*\]\s*', ' ', title)
        title = re.sub(r'\.?Build\.?\d+', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s*v?\d+\.\d+(\.\d+)*\s*$', '', title, flags=re.IGNORECASE)
        title = ' '.join(title.split())
        return title.strip() or "Unknown Game"
    
    def get_folder_size(self, path: str) -> str:
        """Получить размер папки - ОПТИМИЗАЦИЯ: отключено для производительности"""
        # Расчёт размера папки очень медленный для больших игр
        # и блокирует UI. Возвращаем None для отключения.
        return None

    def on_card_hover(self, e):
        is_hovering = e.data == True or e.data == "true"

        # Skip if state hasn't changed (защита от дублирующихся событий)
        if is_hovering == self._is_hovered:
            return

        self._is_hovered = is_hovering

        # Меняем сразу scale + border. animate_scale настроен в __init__,
        # так что scale поедет плавно за 120мс. border меняется мгновенно.
        # Используем закешированные Border'ы (нет аллокации на каждый hover).
        if is_hovering:
            self.scale = 1.03
            self.border = GameCard._BORDER_HOVER
        else:
            self.scale = 1.0
            self.border = GameCard._BORDER_NORMAL

        try:
            self.update()
        except Exception:
            pass
    
    def on_card_click(self, e):
        if self._on_click:
            self._on_click(self.game)
    
    def on_favorite_click(self, e):
        print(f"[DEBUG] Favorite button clicked for: {self.game.title}")
        if self._on_favorite:
            self._on_favorite(self.game)

    def on_upload_click(self, e):
        print(f"[DEBUG] Upload button clicked for: {self.game.title}")
        if self._on_upload:
            print(f"[DEBUG] Calling _on_upload callback...")
            self._on_upload(self.game)
        else:
            print(f"[DEBUG] ERROR: _on_upload is None!")

    def on_exclude_click(self, e):
        print(f"[DEBUG] Exclude button clicked for: {self.game.title}")
        if self._on_exclude:
            self._on_exclude(self.game)

    def on_properties_click(self, e):
        if self._on_properties:
            self._on_properties(self.game)


class LoadingOverlay(ft.Container):
    """Оверлей загрузки"""
    
    def __init__(self):
        super().__init__()
        self.visible = False
        self.bgcolor = "#CC000000"
        self.expand = True
        
        self.progress_text = ft.Text(
            "Загрузка...",
            size=16,
            color=TEXT_WHITE,
            text_align=ft.TextAlign.CENTER
        )
        
        self.progress_bar = ft.ProgressBar(
            width=300,
            color=ACCENT_PURPLE,
            bgcolor="#333333",
            value=None
        )
        
        self.content = ft.Column(
            controls=[
                ft.ProgressRing(color=ACCENT_PURPLE),
                ft.Container(height=20),
                self.progress_text,
                ft.Container(height=10),
                self.progress_bar,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        )
        
        self.alignment = ft.Alignment(0, 0)
    
    def show(self, message: str = "Загрузка..."):
        self.progress_text.value = message
        self.progress_bar.value = None
        self.visible = True
        self.update()
    
    def update_progress(self, message: str, current: int, total: int):
        self.progress_text.value = message
        self.progress_bar.value = current / total if total > 0 else None
        self.update()
    
    def hide(self):
        self.visible = False
        self.update()


class CyberLauncher:
    """Главный класс приложения"""

    def __init__(self, page: ft.Page):
        self.page = page
        # Settings лежат в той же папке что log/data/cache — в frozen-режиме
        # это %APPDATA%\\CyberLauncher\\, в dev — рядом с исходниками
        self.SETTINGS_FILE = str(get_app_data_dir() / "data" / "settings.json")

        # Load settings first to get API keys
        self.settings = self.load_settings()
        self.current_theme = self.settings.get("theme", "dark")

        # Extract API keys from settings
        api_keys = self.settings.get("api_keys", {})
        sgdb_key = api_keys.get("steamgriddb") or None
        rawg_key = api_keys.get("rawg") or None

        # Initialize GameManager with API keys
        self.game_manager = GameManager(
            sgdb_key=sgdb_key,
            rawg_key=rawg_key
        )
        # Watcher: когда игра завершится, GameManager позовёт нас обратно
        # и мы поднимем окно лаунчера на передний план
        self.game_manager.on_game_exited = self._on_game_exited

        self.current_filter = "all"
        self.sidebar_buttons: dict[str, SidebarButton] = {}
        
        self.game_grid = None
        self.loading_overlay = None
        self.games_count_text = None
        self.content_area = None
        
        # Кеш карточек для оптимизации производительности
        self._card_cache: dict[str, GameCard] = {}
        
        # Пагинация для оптимизации с большими библиотеками
        self._page_size = 12  # Уменьшено для скорости
        self._current_page = 0  # Текущая страница (начиная с 0)
        self._all_games_list = []  # Полный список игр для пагинации
        
        # Для загрузки обложек (tkinter file dialog)
        self.upload_target_game = None

        # Shutdown / BigPicture / Gamepad — заглушки, реальные значения позже
        self._shutdown_done: bool = False
        self._bigpicture_view = None
        self._bigpicture_active: bool = False
        self._pre_bigpicture_state: dict = {}
        self.gamepad_manager = None  # инициализируется в build_ui

        # Window focus — нужен чтобы блокировать gamepad-события когда юзер
        # играет в запущенную игру. SDL читает гейпад даже когда окно не в
        # фокусе, поэтому без этой защиты A/B/D-pad из игры случайно
        # запускают другие игры в нашем UI. Используем WinAPI
        # GetForegroundWindow(), не Flet-события focus/blur — последние
        # ненадёжно срабатывают при WinAPI-сворачивании на Windows.
        self._launcher_hwnd: Optional[int] = None  # cached HWND
        # После launch блокируем gamepad на 2 сек — пока окна перетасуются
        # (у нас минимизация, у игры запуск). Защита от случайных нажатий
        # которые могли пройти когда foreground state ещё не устаканился.
        self._gamepad_silence_until: float = 0.0

        # Pending play-time sessions: uid → timestamp запуска. Нужно для
        # игр которые watcher game_manager не может отследить — Steam-игры
        # с EasyAntiCheat и подобной защитой блокируют OpenProcess, и
        # process-scan возвращает пусто. В этом случае мы оцениваем
        # play_time по времени между запуском и возвратом фокуса в лаунчер.
        self._pending_play_sessions: dict = {}

        # Screensaver: после N сек бездействия в BigPicture показывает фоны
        # игр поочерёдно. Cross-fade между двумя image-слоями.
        self._idle_timeout_s: float = 300.0  # 5 минут
        self._screensaver_active: bool = False
        self._last_activity_ts: float = 0.0  # выставится в build_ui
        self._screensaver_idx: int = 0
        self._screensaver_overlay: Optional[ft.Container] = None
        self._screensaver_layer_a: Optional[ft.Container] = None
        self._screensaver_layer_b: Optional[ft.Container] = None

        self.setup_page()
        self.build_ui()
    
    def load_settings(self) -> dict:
        try:
            settings_path = Path(self.SETTINGS_FILE)
            if settings_path.exists():
                with open(settings_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки настроек: {e}")
        return {"theme": "dark", "show_game_size": False}
    
    def save_settings(self):
        try:
            settings_path = Path(self.SETTINGS_FILE)
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")

    def save_api_key(self, service: str, key: str):
        """Save API key to settings and reinitialize API clients"""
        if "api_keys" not in self.settings:
            self.settings["api_keys"] = {}

        self.settings["api_keys"][service] = key
        self.save_settings()

        # Reinitialize API clients in GameManager
        api_keys = self.settings.get("api_keys", {})
        self.game_manager.reinitialize_api_clients(
            sgdb_key=api_keys.get("steamgriddb") or None,
            rawg_key=api_keys.get("rawg") or None
        )

    def validate_api_key(self, service: str):
        """Validate API key for given service"""
        async def do_validate():
            self.show_snackbar(f"Проверяем ключ {service.upper()}...")
            
            # Run validation in background thread
            if service == "steamgriddb":
                client = self.game_manager.cover_api_manager.sgdb
                if client:
                    success, message = await asyncio.to_thread(client.validate_key)
                else:
                    success, message = False, "Клиент не инициализирован"
            elif service == "rawg":
                client = self.game_manager.cover_api_manager.rawg
                if client:
                    success, message = await asyncio.to_thread(client.validate_key)
                else:
                    success, message = False, "Клиент не инициализирован"
            else:
                success, message = False, "Неизвестный сервис"
            
            bgcolor = "#4CAF50" if success else "#F44336"
            self.show_snackbar(f"{service.upper()}: {message}", bgcolor=bgcolor)
        
        self.page.run_task(do_validate)

    def save_launcher_setting(self, launcher: str, value: bool):
        if "enabled_launchers" not in self.settings:
            self.settings["enabled_launchers"] = {"Steam": True}
        
        self.settings["enabled_launchers"][launcher] = value
        self.save_settings()

    def exclude_game(self, game: GameModel):
        """Show confirmation dialog to exclude game"""
        def on_confirm(e):
            dialog.open = False
            self.page.update()
            self.page.run_task(self._do_exclude, game)
        
        def on_cancel(e):
            dialog.open = False
            self.page.update()
        
        dialog = ft.AlertDialog(
            title=ft.Text("Исключить программу?"),
            content=ft.Text(f"'{game.title}' будет скрыта из библиотеки.\nВы сможете вернуть её в настройках."),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.TextButton("Исключить", on_click=on_confirm, style=ft.ButtonStyle(color="#F44336")),
            ],
        )
        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()
    
    async def _do_exclude(self, game: GameModel):
        """Actually exclude the game"""
        path = await self.game_manager.exclude_game(game.uid)
        if path:
            # Add to exclusions list in settings
            if "excluded_paths" not in self.settings:
                self.settings["excluded_paths"] = []
            if path not in self.settings["excluded_paths"]:
                self.settings["excluded_paths"].append(path)
            self.save_settings()

            # Remove from card cache
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]

            # Remove from current games list
            self._all_games_list = [g for g in self._all_games_list if g.uid != game.uid]

            # Re-render cards without resetting page
            self._render_visible_cards()
            # Обновляем счётчики коллекций в сайдбаре — иначе они остаются
            # со старым числом игр (показывают "17" пока в коллекции уже 16)
            self.refresh_collections_sidebar()
            self.show_snackbar(f"'{game.title}' исключена из библиотеки", bgcolor="#FF9800")


    def show_snackbar(self, message: str, bgcolor: str = "#333333", duration: int = 4000):
        """Helper to show snackbar compatible with Flet 0.80+"""
        snackbar = ft.SnackBar(
            content=ft.Text(message),
            bgcolor=bgcolor,
            duration=duration,
        )
        # Add to overlay if not already there
        self.page.overlay.append(snackbar)
        snackbar.open = True
        self.page.update()

    def _build_exclusions_list(self) -> ft.Container:
        """Build the list of excluded programs for settings"""
        excluded = self.settings.get("excluded_paths", [])
        
        if not excluded:
            return ft.Container(
                content=ft.Text("Нет исключённых программ", size=13, color=TEXT_GREY, italic=True),
                padding=15,
                border_radius=10,
                bgcolor="#1E1E1E",
            )
        
        rows = []
        for path in excluded:
            # Get just the filename from the path for display
            display_name = Path(path).name if path else "Unknown"
            
            row = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.BLOCK, color="#F44336", size=18),
                        ft.Column(
                            controls=[
                                ft.Text(display_name, size=13, color=TEXT_WHITE, weight=ft.FontWeight.W_500),
                                ft.Text(path, size=10, color=TEXT_GREY, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.RESTORE,
                            icon_color=ACCENT_BLUE,
                            tooltip="Вернуть в библиотеку",
                            on_click=lambda e, p=path: self.page.run_task(self.restore_excluded, p),
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=10,
                border_radius=8,
                bgcolor="#252525",
                margin=ft.Margin(0, 0, 0, 8),
            )
            rows.append(row)
        
        return ft.Container(
            content=ft.Column(controls=rows, spacing=0),
            padding=10,
            border_radius=10,
            bgcolor="#1E1E1E",
        )

    async def restore_excluded(self, path: str):
        """Remove path from exclusions list and add back to library immediately"""
        if "excluded_paths" in self.settings:
            if path in self.settings["excluded_paths"]:
                self.settings["excluded_paths"].remove(path)
                self.save_settings()
                
                # Attempt to restore immediately
                game = await self.game_manager.add_game_from_path(path)
                if game:
                    # Add to current list and sort if needed (simplest: append)
                    self._all_games_list.append(game)
                    self.show_snackbar(f"'{game.title}' восстановлена в библиотеку", bgcolor="#4CAF50")
                else:
                    self.show_snackbar("Убрано из исключений. Появится при сканировании.", bgcolor="#4CAF50")
                
                # Refresh settings view to update the list
                self.show_settings_view()
                # Update main grid in background (if settings is closed)
                # self.update_game_grid(reset_page=False)

    def setup_page(self):
        self.page.title = WINDOW_TITLE
        self.page.bgcolor = BG_COLOR
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK

        self.page.window.width = 1200
        self.page.window.height = 750
        self.page.window.min_width = 900
        self.page.window.min_height = 600
        self.page.window.title_bar_hidden = True
        self.page.window.title_bar_buttons_hidden = True

        # NB: НЕ выставляем prevent_close=True — в Flet 0.80 это вызывает
        # автоматический оверлей "Working..." на каждый close-event и блокирует UI.
        # Кастомная кнопка X в title bar ловится напрямую через window_action,
        # а Alt+F4 / системное закрытие подхватывается через atexit (см. main).
        self.page.window.on_event = self._on_window_event

        # Устанавливаем иконку с абсолютным путём для панели задач Windows
        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_dir, "icon.ico")
        if os.path.exists(icon_path):
            self.page.window.icon = icon_path
        
        self.page.fonts = {
            "Cyber": "Segoe UI Variable Display, Roboto, sans-serif"
        }
        self.page.theme = ft.Theme(font_family="Cyber")
    
    def build_ui(self):
        self.is_maximized = False

        # Title bar
        title_bar = ft.Container(
            height=40,
            bgcolor="#F20F0F0F",
            content=ft.Row(
                controls=[
                    ft.Container(width=15),
                    ft.Icon(ft.Icons.GAMEPAD_OUTLINED, color=ACCENT_BLUE, size=18),
                    ft.Text("CYBER LAUNCHER", size=12, color="#B3FFFFFF", weight=ft.FontWeight.BOLD),
                    ft.WindowDragArea(ft.Container(bgcolor="transparent"), expand=True),
                    ft.IconButton(ft.Icons.TV, icon_size=16, on_click=lambda _: self.toggle_bigpicture(), icon_color=ACCENT_BLUE, tooltip="Big Picture (F11)"),
                    ft.IconButton(ft.Icons.REMOVE, icon_size=16, on_click=lambda _: self.window_action("min"), icon_color=TEXT_GREY, tooltip="Свернуть"),
                    ft.IconButton(ft.Icons.CROP_SQUARE_OUTLINED, icon_size=16, on_click=lambda _: self.window_action("max"), icon_color=TEXT_GREY, tooltip="Развернуть"),
                    ft.IconButton(ft.Icons.CLOSE, icon_size=16, on_click=lambda _: self.window_action("close"), icon_color=TEXT_GREY, tooltip="Закрыть"),
                    ft.Container(width=5)
                ],
                spacing=0
            )
        )
        self._title_bar = title_bar
        
        # Sidebar buttons
        self.sidebar_buttons["all"] = SidebarButton(ft.Icons.GRID_VIEW_ROUNDED, "Все игры", is_active=True, on_click=self.on_filter_click, data="all")
        self.sidebar_buttons["favorites"] = SidebarButton(ft.Icons.FAVORITE_BORDER, "Избранное", on_click=self.on_filter_click, data="favorites")
        self.sidebar_buttons["wishlist"] = SidebarButton(ft.Icons.LOCAL_FIRE_DEPARTMENT_OUTLINED, "Желаемое", on_click=self.on_filter_click, data="wishlist")
        self.sidebar_buttons["steam"] = SidebarButton(ft.Icons.VIDEOGAME_ASSET_OUTLINED, "Steam", on_click=self.on_filter_click, data="steam")
        self.sidebar_buttons["epic"] = SidebarButton(ft.Icons.TOKEN_OUTLINED, "Epic Games", on_click=self.on_filter_click, data="epic")
        self.sidebar_buttons["system"] = SidebarButton(ft.Icons.COMPUTER_OUTLINED, "Системные", on_click=self.on_filter_click, data="system")
        self.sidebar_buttons["settings"] = SidebarButton(ft.Icons.SETTINGS_OUTLINED, "Настройки", on_click=self.on_filter_click, data="settings")
        self.sidebar_buttons["disk_info"] = SidebarButton(ft.Icons.STORAGE_OUTLINED, "Диски", on_click=self.on_filter_click, data="disk_info")

        # Disk info button visibility depends on settings
        self.disk_info_sidebar_button = ft.Container(
            content=self.sidebar_buttons["disk_info"],
            visible=self.settings.get("show_disk_info", False),
        )

        # Коллекции - динамически создаются после загрузки
        self.collections_column = ft.Column(controls=[], spacing=2)
        
        self.games_count_text = ft.Text("0 игр", size=11, color=TEXT_GREY)
        
        theme_data = GRADIENT_THEMES.get(self.current_theme, GRADIENT_THEMES["dark"])
        sidebar_color = theme_data.get("sidebar", "#801A1A1A")
        
        # Кнопка добавления коллекции
        self.add_collection_btn = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.ADD, color=ACCENT_PURPLE, size=16),
                    ft.Text("Добавить", color=ACCENT_PURPLE, size=12),
                ],
                spacing=5,
            ),
            padding=ft.Padding(left=15, right=15, top=8, bottom=8),
            border_radius=8,
            on_click=self.show_add_collection_dialog,
            ink=True,
        )

        # Sidebar: непрозрачный bg вместо backdrop blur — blur пересчитывается
        # каждый кадр над фоном с карточками и сильно тормозит UI на средних
        # библиотеках. Берём цвет темы и форсим непрозрачность ставя FF в alpha.
        # Если в теме был полупрозрачный (#80xxxxxx) — превращаем в solid.
        sidebar_solid = sidebar_color
        if isinstance(sidebar_solid, str) and sidebar_solid.startswith("#") and len(sidebar_solid) == 9:
            sidebar_solid = "#FF" + sidebar_solid[3:]  # alpha → FF
        self.sidebar = ft.Container(
            width=240,
            bgcolor=sidebar_solid,
            content=ft.Column(
                controls=[
                    ft.Row(controls=[
                        ft.Text("LIBRARY", color="#80FFFFFF", size=11, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        self.games_count_text,
                    ]),
                    ft.Container(height=8),
                    self.sidebar_buttons["all"],
                    self.sidebar_buttons["favorites"],
                    self.sidebar_buttons["wishlist"],
                    ft.Container(height=5),
                    ft.Text("ПЛАТФОРМЫ", color="#80FFFFFF", size=11, weight=ft.FontWeight.BOLD),
                    ft.Container(height=5),
                    self.sidebar_buttons["steam"],
                    self.sidebar_buttons["epic"],
                    self.sidebar_buttons["system"],
                    ft.Container(height=10),
                    ft.Divider(color="#1AFFFFFF"),
                    ft.Container(height=10),
                    # Секция коллекций
                    ft.Row(controls=[
                        ft.Text("КОЛЛЕКЦИИ", color="#80FFFFFF", size=11, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        self.add_collection_btn,
                    ]),
                    ft.Container(height=5),
                    self.collections_column,
                    ft.Container(height=10),
                    ft.Divider(color="#1AFFFFFF"),
                    ft.Container(height=5),
                    self.sidebar_buttons["settings"],
                    self.disk_info_sidebar_button,
                    ft.Container(
                        content=ft.TextButton(
                            "Обновить библиотеку",
                            icon=ft.Icons.REFRESH,
                            icon_color=ACCENT_BLUE,
                            style=ft.ButtonStyle(color=ACCENT_BLUE),
                            on_click=self.on_refresh_click
                        ),
                        alignment=ft.Alignment(0, 0)
                    ),
                ],
                spacing=2,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.Padding(left=15, right=15, top=20, bottom=20),
            alignment=ft.Alignment(0, -1),
        )
        
        # Сортировка
        self.current_sort = "default"
        self.sort_labels = {
            "default": "Стандартная",
            "name_asc": "По названию (А-Я)",
            "name_desc": "По названию (Я-А)",
            "date_desc": "По дате (новые)",
            "date_asc": "По дате (старые)",
        }
        
        self.sort_text = ft.Text(self.sort_labels[self.current_sort], size=12, color=TEXT_WHITE)
        
        self.sort_button = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    controls=[
                        self.sort_text,
                        ft.Icon(ft.Icons.ARROW_DROP_DOWN, color=TEXT_WHITE, size=20),
                    ],
                    spacing=5,
                ),
                padding=ft.Padding(left=12, right=8, top=6, bottom=6),
                border_radius=8,
                bgcolor="#1E1E1E",
                border=ft.Border.all(1, "#333333"),
            ),
            items=[
                ft.PopupMenuItem("Стандартная", on_click=lambda _: self.set_sort("default")),
                ft.PopupMenuItem("По названию (А-Я)", on_click=lambda _: self.set_sort("name_asc")),
                ft.PopupMenuItem("По названию (Я-А)", on_click=lambda _: self.set_sort("name_desc")),
                ft.PopupMenuItem("По дате (новые)", on_click=lambda _: self.set_sort("date_desc")),
                ft.PopupMenuItem("По дате (старые)", on_click=lambda _: self.set_sort("date_asc")),
            ],
        )
        
        # Debounce поиска: на каждый keystroke не запускаем full grid rebuild,
        # ждём 250 мс паузы. Печатать "spongebob" — 9 ребилдов было.
        self._search_debounce_token: int = 0
        self.search_field = ft.TextField(
            hint_text="Поиск игр...",
            prefix_icon=ft.Icons.SEARCH,
            on_change=lambda e: self._on_search_changed(),
            height=35,
            content_padding=10,
            text_size=13,
            border_radius=8,
            bgcolor="#1E1E1E",
            border_color="#333333",
            focused_border_color=ACCENT_BLUE,
            expand=True,
        )

        self.sort_panel = ft.Container(
            height=60,
            content=ft.Row(
                controls=[
                    self.search_field,
                    ft.Container(width=20),
                    ft.Icon(ft.Icons.SORT, color=TEXT_GREY, size=16),
                    ft.Text("Сортировка:", size=12, color=TEXT_GREY),
                    self.sort_button,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=20, right=20, top=10, bottom=5),
        )
        
        # Game grid
        self.game_grid = ft.GridView(
            expand=True,
            runs_count=4,
            max_extent=220,
            child_aspect_ratio=0.65,
            spacing=15,
            run_spacing=15,
            padding=ft.Padding(left=20, right=20, top=10, bottom=20),
        )

        # Панель действий для коллекции — видна только когда фильтр = коллекция.
        # Содержит кнопку "Управление играми" → мульти-селект диалог.
        self.collection_actions_panel = ft.Container(
            visible=False,
            padding=ft.Padding(left=20, right=20, top=0, bottom=8),
            content=ft.Row(
                controls=[
                    ft.ElevatedButton(
                        "Управление играми коллекции",
                        icon=ft.Icons.PLAYLIST_ADD_CHECK,
                        on_click=self._on_manage_collection_click,
                        bgcolor=ACCENT_PURPLE,
                        color=TEXT_WHITE,
                    ),
                ],
            ),
        )

        self.games_container = ft.Column(
            controls=[self.sort_panel, self.collection_actions_panel, self.game_grid],
            spacing=0,
            expand=True,
        )
        
        self.settings_view = self.build_settings_view()
        self.loading_overlay = LoadingOverlay()
        
        theme_colors = GRADIENT_THEMES.get(self.current_theme, GRADIENT_THEMES["dark"])["colors"]
        
        self.bg_container = ft.Container(
            expand=True,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=theme_colors,
            ),
            content=self.games_container
        )
        
        self.content_stack = ft.Stack(
            controls=[self.bg_container, self.loading_overlay],
            expand=True,
        )
        
        self._main_row = ft.Row(expand=True, spacing=0, controls=[self.sidebar, self.content_stack])

        layout = ft.Column(
            spacing=0,
            expand=True,
            controls=[
                title_bar,
                self._main_row,
            ]
        )

        self.page.add(layout)
        self.page.run_task(self.load_library)

        # Hotkey F11 для toggle BigPicture (помимо кнопки геймпада)
        self.page.on_keyboard_event = self._on_keyboard_event

        # Запуск геймпада (опционально)
        self._init_gamepad()

        # Screensaver overlay (поверх всего, изначально скрыт)
        self._build_screensaver_overlay()
        self._last_activity_ts = time.time()
        self.page.run_task(self._idle_monitor_loop)
    
    def build_settings_view(self):
        def create_theme_card(theme_id: str, theme_data: dict):
            is_selected = self.current_theme == theme_id
            
            preview = ft.Container(
                width=100,
                height=70,
                border_radius=12,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment(-1, -1),
                    end=ft.Alignment(1, 1),
                    colors=theme_data["preview"],
                ),
                border=ft.Border.all(3, ACCENT_BLUE if is_selected else "#333333"),
            )
            
            name = ft.Text(
                theme_data["name"],
                size=13,
                color=ACCENT_BLUE if is_selected else TEXT_WHITE,
                weight=ft.FontWeight.BOLD if is_selected else ft.FontWeight.NORMAL,
                text_align=ft.TextAlign.CENTER,
            )
            
            card = ft.Container(
                content=ft.Column(
                    controls=[preview, name],
                    spacing=8,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=12,
                border_radius=15,
                bgcolor="#2A2A2A" if is_selected else "#1E1E1E",
                on_click=lambda e, tid=theme_id: self.change_theme(tid),
                on_hover=lambda e: self.on_theme_card_hover(e),
            )
            
            return card
        
        theme_cards = [create_theme_card(tid, td) for tid, td in GRADIENT_THEMES.items()]
        
        self.custom_paths_column = ft.Column(
            controls=self._get_custom_path_controls(),
            spacing=10
        )

        return ft.Container(
            expand=True,
            padding=40,
            content=ft.Column(
                controls=[
                    ft.Row(controls=[
                        ft.Icon(ft.Icons.SETTINGS, color=ACCENT_BLUE, size=32),
                        ft.Text("Настройки", size=28, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ], spacing=15),
                    ft.Container(height=30),
                    ft.Text("Тема оформления", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Выберите цветовую схему фона приложения", size=14, color=TEXT_GREY),
                    ft.Container(height=20),
                    ft.Row(controls=theme_cards[:4], spacing=15),
                    ft.Container(height=10),
                    ft.Row(controls=theme_cards[4:], spacing=15),
                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),
                    ft.Text("Отображение", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Настройки отображения карточек игр", size=14, color=TEXT_GREY),
                    ft.Container(height=20),
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Column(controls=[
                                    ft.Text("Показывать размер игры", size=14, color=TEXT_WHITE),
                                    ft.Text("Отображать сколько весит игра на диске", size=12, color=TEXT_GREY),
                                ], spacing=2),
                                ft.Container(expand=True),
                                ft.Switch(
                                    value=self.settings.get("show_game_size", False),
                                    active_color=ACCENT_BLUE,
                                    on_change=lambda e: self.toggle_show_game_size(e.control.value),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),
                    
                    ft.Container(height=15),

                    # Переключатель анимации
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Column(controls=[
                                    ft.Text("Анимация плиток", size=14, color=TEXT_WHITE),
                                    ft.Text("Эффект при наведении на карточки игр", size=12, color=TEXT_GREY),
                                ], spacing=2),
                                ft.Container(expand=True),
                                ft.Switch(
                                    value=self.settings.get("enable_animations", True),
                                    active_color=ACCENT_BLUE,
                                    on_change=lambda e: self.toggle_animations(e.control.value),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),

                    ft.Container(height=15),

                    # Переключатель вибрации геймпада в BigPicture
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Column(controls=[
                                    ft.Text("Вибрация геймпада", size=14, color=TEXT_WHITE),
                                    ft.Text("Тактильный отклик в Big Picture при навигации и запуске игр", size=12, color=TEXT_GREY),
                                ], spacing=2),
                                ft.Container(expand=True),
                                ft.Switch(
                                    value=self.settings.get("gamepad_rumble", True),
                                    active_color=ACCENT_BLUE,
                                    on_change=lambda e: self.toggle_gamepad_rumble(e.control.value),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),

                    ft.Container(height=15),

                    # Переключатель вкладки дисков
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Column(controls=[
                                    ft.Text("Показывать вкладку Диски", size=14, color=TEXT_WHITE),
                                    ft.Text("Информация о свободном месте на дисках", size=12, color=TEXT_GREY),
                                ], spacing=2),
                                ft.Container(expand=True),
                                ft.Switch(
                                    value=self.settings.get("show_disk_info", False),
                                    active_color=ACCENT_BLUE,
                                    on_change=lambda e: self.toggle_show_disk_info(e.control.value),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),

                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),

                    # API Keys Section
                    ft.Text("API Ключи", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Для улучшенной загрузки обложек игр", size=12, color=TEXT_GREY),
                    ft.Container(height=20),

                    # SteamGridDB API Key
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Row(
                                    controls=[
                                        ft.Text("SteamGridDB API Key", size=14, color=TEXT_WHITE, weight=ft.FontWeight.BOLD),
                                        ft.Container(expand=True),
                                        ft.TextButton(
                                            "Проверить ключ",
                                            icon=ft.Icons.VERIFIED,
                                            on_click=lambda _: self.validate_api_key("steamgriddb"),
                                        ),
                                        ft.TextButton(
                                            "Получить ключ",
                                            icon=ft.Icons.OPEN_IN_NEW,
                                            on_click=lambda _: webbrowser.open("https://www.steamgriddb.com/profile/preferences/api"),
                                        ),
                                    ],
                                ),
                                ft.Container(height=5),
                                ft.TextField(
                                    value=self.settings.get("api_keys", {}).get("steamgriddb", ""),
                                    hint_text="Введите API ключ SteamGridDB",
                                    password=True,
                                    can_reveal_password=True,
                                    on_change=lambda e: self.save_api_key("steamgriddb", e.control.value),
                                    width=500,
                                ),
                                ft.Text(
                                    "Бесплатная регистрация. Высококачественные обложки игр.",
                                    size=11,
                                    color=TEXT_GREY,
                                ),
                            ],
                            spacing=5,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),

                    ft.Container(height=15),

                    # RAWG API Key
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Row(
                                    controls=[
                                        ft.Text("RAWG.io API Key", size=14, color=TEXT_WHITE, weight=ft.FontWeight.BOLD),
                                        ft.Container(expand=True),
                                        ft.TextButton(
                                            "Проверить ключ",
                                            icon=ft.Icons.VERIFIED,
                                            on_click=lambda _: self.validate_api_key("rawg"),
                                        ),
                                        ft.TextButton(
                                            "Получить ключ",
                                            icon=ft.Icons.OPEN_IN_NEW,
                                            on_click=lambda _: webbrowser.open("https://rawg.io/apidocs"),
                                        ),
                                    ],
                                ),
                                ft.Container(height=5),
                                ft.TextField(
                                    value=self.settings.get("api_keys", {}).get("rawg", ""),
                                    hint_text="Введите API ключ RAWG",
                                    password=True,
                                    can_reveal_password=True,
                                    on_change=lambda e: self.save_api_key("rawg", e.control.value),
                                    width=500,
                                ),
                                ft.Text(
                                    "Бесплатная регистрация. 20,000 запросов в месяц.",
                                    size=11,
                                    color=TEXT_GREY,
                                ),
                            ],
                            spacing=5,
                        ),
                        padding=15,
                        border_radius=10,
                        bgcolor="#1E1E1E",
                    ),

                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),

                    # --- Launcher Libraries Section ---
                    ft.Text("Библиотеки Лаунчеров", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Выберите, игры из каких лаунчеров сканировать", size=14, color=TEXT_GREY),
                    ft.Container(height=20),
                    
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Switch(
                                    label="Steam",
                                    value=self.settings.get("enabled_launchers", {}).get("Steam", True),
                                    on_change=lambda e: self.save_launcher_setting("Steam", e.control.value),
                                    active_color=ACCENT_BLUE,
                                ),
                                ft.Switch(
                                    label="Epic Games",
                                    value=self.settings.get("enabled_launchers", {}).get("Epic Games", False),
                                    on_change=lambda e: self.save_launcher_setting("Epic Games", e.control.value),
                                    active_color=ACCENT_BLUE,
                                ),
                                ft.Switch(
                                    label="Ubisoft Connect",
                                    value=self.settings.get("enabled_launchers", {}).get("Ubisoft", False),
                                    on_change=lambda e: self.save_launcher_setting("Ubisoft", e.control.value),
                                    active_color=ACCENT_BLUE,
                                ),
                                ft.Switch(
                                    label="GOG Galaxy",
                                    value=self.settings.get("enabled_launchers", {}).get("GOG", False),
                                    on_change=lambda e: self.save_launcher_setting("GOG", e.control.value),
                                    active_color=ACCENT_BLUE,
                                ),
                                ft.Switch(
                                    label="Battle.net",
                                    value=self.settings.get("enabled_launchers", {}).get("Battle.net", False),
                                    on_change=lambda e: self.save_launcher_setting("Battle.net", e.control.value),
                                    active_color=ACCENT_BLUE,
                                ),
                            ],
                            spacing=10,
                        ),
                        bgcolor="#1E1E1E",
                        padding=15,
                        border_radius=10,
                    ),


                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),
                    
                    # --- Custom Paths Section ---
                    ft.Text("Папки для сканирования", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Добавьте папки, в которых нужно искать игры", size=14, color=TEXT_GREY),
                    ft.Container(height=20),
                    
                    self.custom_paths_column,

                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),
                    
                    # Exclusions Section
                    ft.Text("Исключённые программы", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Text("Программы, скрытые из библиотеки", size=12, color=TEXT_GREY),
                    ft.Container(height=15),
                    self._build_exclusions_list(),

                    ft.Container(height=40),
                    ft.Divider(color="#333333"),
                    ft.Container(height=30),
                    ft.Text("О приложении", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Container(height=15),
                    ft.Row(controls=[
                        ft.Icon(ft.Icons.INFO_OUTLINE, color=ACCENT_BLUE, size=20),
                        ft.Text("CyberLauncher", size=16, color=TEXT_WHITE, weight=ft.FontWeight.BOLD),
                        ft.Text(f"v{APP_VERSION}", size=14, color=TEXT_GREY),
                    ], spacing=10),
                    ft.Container(height=10),
                    ft.Text("Универсальный лаунчер для всех ваших игр", size=13, color=TEXT_GREY),
                ],
                scroll=ft.ScrollMode.AUTO,
            ),
        )
    
    def on_theme_card_hover(self, e):
        if e.data == "true":
            e.control.scale = 1.05
        else:
            e.control.scale = 1.0
        e.control.update()
    
    def toggle_show_game_size(self, value: bool):
        self.settings["show_game_size"] = value
        self.save_settings()
        if self.current_filter != "settings":
            self.update_game_grid()
    
    def toggle_animations(self, value: bool):
        """Включить/выключить анимацию плиток"""
        self.settings["enable_animations"] = value
        self.save_settings()
        # Очищаем кеш чтобы карточки пересоздались с новой настройкой
        self._card_cache.clear()
        if self.current_filter != "settings":
            self.update_game_grid()

    def toggle_show_disk_info(self, value: bool):
        """Включить/выключить вкладку информации о дисках"""
        self.settings["show_disk_info"] = value
        self.save_settings()
        self.disk_info_sidebar_button.visible = value
        self.disk_info_sidebar_button.update()

    def toggle_gamepad_rumble(self, value: bool):
        """Включить/выключить вибрацию геймпада в BigPicture-режиме."""
        self.settings["gamepad_rumble"] = value
        self.save_settings()
        # BigPictureView читает self.settings["gamepad_rumble"] перед каждым
        # rumble-вызовом, так что отдельно его уведомлять не нужно.
        self.show_snackbar(
            "Вибрация геймпада включена" if value else "Вибрация геймпада выключена",
            bgcolor="#1E1E1E",
        )

    # ============================== Wishlist (Желаемое) ==============================

    WISHLIST_SORT_LABELS = {
        "priority": "🔥 Приоритетные сверху",
        "date_desc": "По дате (новые)",
        "date_asc": "По дате (старые)",
        "name": "По названию",
    }

    def build_wishlist_view(self):
        """Раздел "Желаемое". Карточки с Steam-играми, которые юзер хочет
        взять но ещё не установил. Метаданные — Steam Store API."""
        if not hasattr(self, "_wishlist_sort"):
            self._wishlist_sort = "priority"

        # Заголовок + кнопки управления (Добавить, сортировка)
        title_row = ft.Row(
            controls=[
                ft.Icon(ft.Icons.LOCAL_FIRE_DEPARTMENT, color="#FF6B35", size=32),
                ft.Text("Желаемое", size=28, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                ft.Container(expand=True),
                ft.ElevatedButton(
                    "Добавить из Steam",
                    icon=ft.Icons.ADD,
                    on_click=lambda e: self.show_wishlist_add_dialog(),
                    bgcolor=ACCENT_PURPLE,
                    color=TEXT_WHITE,
                ),
            ],
            spacing=15,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Sort dropdown
        self._wishlist_sort_text = ft.Text(
            self.WISHLIST_SORT_LABELS[self._wishlist_sort],
            size=12, color=TEXT_WHITE,
        )
        sort_button = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    controls=[
                        self._wishlist_sort_text,
                        ft.Icon(ft.Icons.ARROW_DROP_DOWN, color=TEXT_WHITE, size=20),
                    ],
                    spacing=5,
                ),
                padding=ft.Padding(left=12, right=8, top=6, bottom=6),
                border_radius=8,
                bgcolor="#1E1E1E",
                border=ft.Border.all(1, "#333333"),
            ),
            items=[
                ft.PopupMenuItem(label, on_click=lambda _, k=key: self._set_wishlist_sort(k))
                for key, label in self.WISHLIST_SORT_LABELS.items()
            ],
        )

        # Items grid
        items = self.game_manager.wishlist.get_sorted(self._wishlist_sort)
        cards = [self._build_wishlist_card(it) for it in items]
        self._wishlist_grid = ft.GridView(
            expand=True,
            runs_count=3,
            max_extent=380,
            child_aspect_ratio=1.05,
            spacing=15,
            run_spacing=15,
            padding=ft.Padding(left=0, right=0, top=10, bottom=20),
            controls=cards,
        )

        empty_hint = ft.Container(
            visible=not items,
            padding=40,
            alignment=ft.Alignment(0, 0),
            content=ft.Column(
                controls=[
                    ft.Icon(ft.Icons.LOCAL_FIRE_DEPARTMENT_OUTLINED, color=TEXT_GREY, size=64),
                    ft.Text("В списке желаемого пока пусто",
                            size=16, color=TEXT_GREY, weight=ft.FontWeight.W_500),
                    ft.Text('Нажмите "Добавить из Steam" чтобы найти и сохранить игру.',
                            size=13, color=TEXT_GREY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            ),
        )

        controls_row = ft.Row(
            controls=[
                ft.Text(f"{len(items)} игр", size=13, color=TEXT_GREY),
                ft.Container(expand=True),
                ft.Icon(ft.Icons.SORT, color=TEXT_GREY, size=16),
                ft.Text("Сортировка:", size=12, color=TEXT_GREY),
                sort_button,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return ft.Container(
            expand=True,
            padding=ft.Padding(left=40, right=40, top=30, bottom=20),
            content=ft.Column(
                controls=[
                    title_row,
                    ft.Container(height=10),
                    controls_row,
                    ft.Container(height=10),
                    empty_hint if not items else self._wishlist_grid,
                ],
                expand=True,
                spacing=0,
            ),
        )

    def _set_wishlist_sort(self, key: str):
        self._wishlist_sort = key
        # Перестроить view (быстрее чем точечно обновлять каждую карточку)
        self.wishlist_view = self.build_wishlist_view()
        self.bg_container.content = self.wishlist_view
        self.page.update()

    def _refresh_wishlist_view(self):
        """Полный rebuild — используется после add/remove/toggle."""
        if self.current_filter != "wishlist":
            return
        self.wishlist_view = self.build_wishlist_view()
        self.bg_container.content = self.wishlist_view
        self.page.update()

    def _build_wishlist_card(self, item) -> ft.Container:
        """Карточка одной игры в списке желаемого.
        Layout: header_image сверху, ниже название + кнопки."""
        # Cover image: используем header_image_url, fallback на градиент
        if item.header_image_url:
            cover = ft.Container(
                height=160,
                image=ft.DecorationImage(src=item.header_image_url, fit="cover"),
                border_radius=ft.BorderRadius(8, 8, 0, 0),
            )
        else:
            cover = ft.Container(
                height=160,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1),
                    colors=["#1a1a2e", "#16213e"],
                ),
                content=ft.Icon(ft.Icons.SPORTS_ESPORTS, size=64, color="#FFD54F"),
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(8, 8, 0, 0),
            )

        # Огонёк toggle
        fire_icon = ft.Icon(
            ft.Icons.LOCAL_FIRE_DEPARTMENT if item.is_priority else ft.Icons.LOCAL_FIRE_DEPARTMENT_OUTLINED,
            color="#FF6B35" if item.is_priority else "#888",
            size=22,
        )
        fire_btn = ft.Container(
            content=fire_icon,
            width=36, height=36, border_radius=18,
            bgcolor="#332A00" if item.is_priority else "#1E1E1E",
            border=ft.Border.all(1, "#FF6B35" if item.is_priority else "#333"),
            alignment=ft.Alignment(0, 0),
            on_click=lambda e, aid=item.app_id: self._wishlist_toggle_priority(aid),
            ink=True,
            tooltip="Приоритет (взять в ближайшее время)",
        )

        # Steam page button
        steam_btn = ft.ElevatedButton(
            "Steam",
            icon=ft.Icons.OPEN_IN_NEW,
            on_click=lambda e, url=item.store_url: webbrowser.open(url) if url else None,
            bgcolor="#1B2838",
            color=TEXT_WHITE,
        )

        # Trailer button (если есть)
        trailer_btn = None
        if item.trailer_url:
            trailer_btn = ft.ElevatedButton(
                "Трейлер",
                icon=ft.Icons.PLAY_CIRCLE_OUTLINE,
                on_click=lambda e, url=item.trailer_url: webbrowser.open(url),
                bgcolor="#333",
                color=TEXT_WHITE,
            )

        # Delete button
        del_btn = ft.IconButton(
            ft.Icons.DELETE_OUTLINE,
            icon_color="#FF5252",
            tooltip="Удалить из желаемого",
            on_click=lambda e, aid=item.app_id, title=item.title: self._wishlist_confirm_delete(aid, title),
        )

        title_text = ft.Text(
            item.title or "?",
            size=15, color=TEXT_WHITE, weight=ft.FontWeight.W_600,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        meta_line = " · ".join(s for s in [item.release_date, item.genres] if s)
        meta_text = ft.Text(
            meta_line, size=11, color=TEXT_GREY,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
        )
        desc_text = ft.Text(
            item.short_description or "",
            size=12, color="#BBBBBB",
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )

        actions = [steam_btn]
        if trailer_btn is not None:
            actions.append(trailer_btn)
        actions.append(ft.Container(expand=True))
        actions.append(fire_btn)
        actions.append(del_btn)

        body = ft.Container(
            padding=ft.Padding(left=14, right=14, top=10, bottom=10),
            content=ft.Column(
                controls=[
                    title_text,
                    meta_text if meta_line else ft.Container(),
                    ft.Container(height=4),
                    desc_text,
                    ft.Container(expand=True),
                    ft.Row(controls=actions, spacing=8,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
                spacing=2,
                expand=True,
            ),
        )

        return ft.Container(
            bgcolor=CARD_BG,
            border_radius=8,
            border=ft.Border.all(1, "#FF6B35" if item.is_priority else "#333"),
            content=ft.Column(controls=[cover, body], spacing=0, expand=True),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    def _wishlist_toggle_priority(self, app_id: str):
        new_val = self.game_manager.wishlist.toggle_priority(app_id)
        if new_val is None:
            return
        # Полный rebuild чтобы пересортировать (если сейчас sort=priority)
        self._refresh_wishlist_view()

    def _wishlist_confirm_delete(self, app_id: str, title: str):
        def on_confirm(e):
            dialog.open = False
            self.page.update()
            self.game_manager.wishlist.remove(app_id)
            self._refresh_wishlist_view()
            self.show_snackbar(f"'{title}' удалена из желаемого", bgcolor="#FF9800")

        def on_cancel(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Удалить из желаемого?"),
            content=ft.Text(f"'{title}' будет удалена из списка."),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.TextButton("Удалить", on_click=on_confirm,
                              style=ft.ButtonStyle(color="#F44336")),
            ],
        )
        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def show_wishlist_add_dialog(self):
        """Диалог поиска и добавления игры через Steam Store API."""
        from wishlist_manager import steam_search

        search_state = {"results": [], "query": "", "debounce_token": 0}

        results_column = ft.Column(controls=[], spacing=4)
        status_text = ft.Text("Введите название игры", size=12, color=TEXT_GREY)

        def render_results():
            results_column.controls.clear()
            for r in search_state["results"]:
                app_id = r.get("app_id", "")
                name = r.get("name", "?")
                in_wishlist = self.game_manager.wishlist.has(app_id)
                already_lbl = ft.Text(
                    "уже в списке" if in_wishlist else "",
                    size=11, color="#FFD54F",
                )
                row = ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Container(
                                width=46, height=24,
                                bgcolor=CARD_BG,
                                image=ft.DecorationImage(src=r.get("header_image", ""), fit="cover") if r.get("header_image") else None,
                                border_radius=4,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(name, size=14, color=TEXT_WHITE,
                                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                    already_lbl,
                                ],
                                spacing=0,
                                expand=True,
                                tight=True,
                            ),
                            ft.ElevatedButton(
                                "Добавить" if not in_wishlist else "✓",
                                icon=ft.Icons.ADD if not in_wishlist else ft.Icons.CHECK,
                                on_click=(lambda e, aid=app_id, nm=name: self._wishlist_add_from_dialog(aid, nm, render_results, status_text)) if not in_wishlist else None,
                                disabled=in_wishlist,
                                bgcolor=ACCENT_PURPLE if not in_wishlist else "#333",
                                color=TEXT_WHITE,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding(left=8, right=8, top=4, bottom=4),
                    border_radius=6,
                    bgcolor="#15FFFFFF",
                )
                results_column.controls.append(row)
            try:
                results_column.update()
            except Exception:
                pass

        def on_query_change(e):
            q = (search_field.value or "").strip()
            search_state["query"] = q
            search_state["debounce_token"] += 1
            token = search_state["debounce_token"]

            async def do_search():
                await asyncio.sleep(0.35)
                if token != search_state["debounce_token"]:
                    return
                if len(q) < 2:
                    search_state["results"] = []
                    status_text.value = "Введите минимум 2 символа"
                    try:
                        status_text.update()
                    except Exception:
                        pass
                    render_results()
                    return
                status_text.value = "Ищу…"
                try:
                    status_text.update()
                except Exception:
                    pass
                # network call в отдельный поток
                items = await asyncio.to_thread(steam_search, q, 10)
                if token != search_state["debounce_token"]:
                    return
                search_state["results"] = items
                status_text.value = f"Найдено: {len(items)}" if items else "Ничего не найдено"
                try:
                    status_text.update()
                except Exception:
                    pass
                render_results()

            self.page.run_task(do_search)

        search_field = ft.TextField(
            hint_text="Название игры в Steam…",
            prefix_icon=ft.Icons.SEARCH,
            on_change=on_query_change,
            autofocus=True,
            border_radius=8,
            bgcolor="#1E1E1E",
            border_color="#333333",
            focused_border_color=ACCENT_BLUE,
            expand=True,
        )

        def on_close(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.LOCAL_FIRE_DEPARTMENT, color="#FF6B35"),
                    ft.Text("Добавить в желаемое", weight=ft.FontWeight.BOLD),
                ],
                spacing=10,
            ),
            content=ft.Container(
                width=600,
                height=480,
                alignment=ft.Alignment(-1, -1),
                content=ft.Column(
                    controls=[
                        ft.Text("Поиск по Steam Store. Подсказки появляются после "
                                "2+ символов.", size=12, color=TEXT_GREY),
                        ft.Container(height=10),
                        search_field,
                        ft.Container(height=8),
                        status_text,
                        ft.Container(height=4),
                        ft.Container(
                            height=340,
                            content=ft.Column(
                                controls=[results_column],
                                scroll=ft.ScrollMode.AUTO,
                            ),
                        ),
                    ],
                    spacing=0,
                ),
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=on_close),
            ],
        )
        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def _wishlist_add_from_dialog(self, app_id: str, name_hint: str, render_cb, status_text):
        """Добавить игру в wishlist (используется из диалога поиска).
        Делает fetch appdetails в фоне, затем обновляет UI."""
        status_text.value = f"Добавляю '{name_hint}'…"
        try:
            status_text.update()
        except Exception:
            pass

        async def do_add():
            item = await asyncio.to_thread(self.game_manager.wishlist.add_by_app_id, app_id)
            if item is None:
                status_text.value = "Не удалось получить данные из Steam"
                try:
                    status_text.update()
                except Exception:
                    pass
                return
            status_text.value = f"Добавлено: {item.title}"
            try:
                status_text.update()
            except Exception:
                pass
            # Обновим UI диалога и страницы wishlist
            try:
                render_cb()
            except Exception:
                pass
            self._refresh_wishlist_view()

        self.page.run_task(do_add)

    # ============================== End Wishlist ==============================

    def build_disk_info_view(self):
        """Создает представление с информацией о дисках"""
        import shutil
        import string

        disk_cards = []

        # Получаем список доступных дисков (Windows)
        if sys.platform == "win32":
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append(drive)
        else:
            drives = ["/"]

        for drive in drives:
            try:
                usage = shutil.disk_usage(drive)
                total_gb = usage.total / (1024 ** 3)
                used_gb = usage.used / (1024 ** 3)
                free_gb = usage.free / (1024 ** 3)
                percent_used = (usage.used / usage.total) * 100

                # Цвет индикатора в зависимости от заполненности
                if percent_used >= 90:
                    bar_color = "#FF5252"  # Красный
                elif percent_used >= 70:
                    bar_color = "#FFB74D"  # Оранжевый
                else:
                    bar_color = ACCENT_BLUE  # Синий

                # Карточка диска
                disk_card = ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Icon(ft.Icons.STORAGE, color=bar_color, size=28),
                                    ft.Text(
                                        drive.rstrip("\\"),
                                        size=18,
                                        weight=ft.FontWeight.BOLD,
                                        color=TEXT_WHITE,
                                    ),
                                    ft.Container(expand=True),
                                    ft.Text(
                                        f"{percent_used:.1f}%",
                                        size=16,
                                        color=bar_color,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                ],
                            ),
                            ft.Container(height=10),
                            # Прогресс-бар
                            ft.Container(
                                content=ft.Stack(
                                    controls=[
                                        ft.Container(
                                            bgcolor="#333333",
                                            border_radius=5,
                                            height=12,
                                            expand=True,
                                        ),
                                        ft.Container(
                                            bgcolor=bar_color,
                                            border_radius=5,
                                            height=12,
                                            width=int(percent_used * 3.5),  # max ~350px
                                        ),
                                    ],
                                ),
                                width=350,
                                height=12,
                            ),
                            ft.Container(height=10),
                            ft.Row(
                                controls=[
                                    ft.Column(
                                        controls=[
                                            ft.Text("Занято", size=12, color=TEXT_GREY),
                                            ft.Text(
                                                f"{used_gb:.1f} ГБ",
                                                size=14,
                                                color=TEXT_WHITE,
                                                weight=ft.FontWeight.W_500,
                                            ),
                                        ],
                                        spacing=2,
                                    ),
                                    ft.Container(expand=True),
                                    ft.Column(
                                        controls=[
                                            ft.Text("Свободно", size=12, color=TEXT_GREY),
                                            ft.Text(
                                                f"{free_gb:.1f} ГБ",
                                                size=14,
                                                color=ACCENT_BLUE,
                                                weight=ft.FontWeight.W_500,
                                            ),
                                        ],
                                        spacing=2,
                                    ),
                                    ft.Container(expand=True),
                                    ft.Column(
                                        controls=[
                                            ft.Text("Всего", size=12, color=TEXT_GREY),
                                            ft.Text(
                                                f"{total_gb:.1f} ГБ",
                                                size=14,
                                                color=TEXT_WHITE,
                                                weight=ft.FontWeight.W_500,
                                            ),
                                        ],
                                        spacing=2,
                                    ),
                                ],
                            ),
                        ],
                        spacing=5,
                    ),
                    bgcolor="#1E1E1E",
                    padding=20,
                    border_radius=15,
                    border=ft.Border.all(1, "#333333"),
                    width=400,
                )
                disk_cards.append(disk_card)
            except (OSError, PermissionError):
                # Пропускаем недоступные диски
                continue

        return ft.Container(
            expand=True,
            padding=40,
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.STORAGE, color=ACCENT_BLUE, size=32),
                            ft.Text(
                                "Информация о дисках",
                                size=28,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT_WHITE,
                            ),
                        ],
                        spacing=15,
                    ),
                    ft.Container(height=10),
                    ft.Text(
                        "Использование дискового пространства на вашем компьютере",
                        size=14,
                        color=TEXT_GREY,
                    ),
                    ft.Container(height=30),
                    ft.Row(
                        controls=disk_cards,
                        wrap=True,
                        spacing=20,
                        run_spacing=20,
                    ),
                ],
                scroll=ft.ScrollMode.AUTO,
            ),
        )

    def window_action(self, action: str):
        if action == "close":
            # NB: НЕ вызываем page.run_task из этого sync-хендлера — Flet 0.80
            # покажет встроенный модал "Working..." и заблокирует UI.
            # Сохранение покрыто debounced save (1 сек) + atexit emergency_sync_save.
            if HAS_TRAY and TRAY_ICON:
                self.page.window.visible = False
                self.page.update()
            else:
                # Трея нет — полный выход через async помощник
                self.page.run_task(self._async_full_exit)
        elif action == "min":
            self.page.window.minimized = True
            self.page.update()
        elif action == "max":
            self.is_maximized = not self.is_maximized
            self.page.window.maximized = self.is_maximized
            self.page.update()
        elif action == "exit":
            # Полный выход (из меню трея или Alt+F4 без трея)
            self.page.run_task(self._async_full_exit)

    # ========== Screensaver (idle slideshow of game heroes) ==========

    SCREENSAVER_FADE_MS = 1500   # длительность cross-fade между картинками
    SCREENSAVER_HOLD_S = 7.0     # сколько каждая картинка полностью видна

    def _build_screensaver_overlay(self):
        """Создаёт fullscreen overlay со слайдшоу. Cross-fade через два слоя:
        пока A гаснет, B уже проявляется — нет паузы с чёрным экраном."""
        self._screensaver_layer_a = ft.Container(
            left=0, top=0, right=0, bottom=0,
            opacity=0.0,
            animate_opacity=ft.Animation(self.SCREENSAVER_FADE_MS, ft.AnimationCurve.EASE_IN_OUT),
        )
        self._screensaver_layer_b = ft.Container(
            left=0, top=0, right=0, bottom=0,
            opacity=0.0,
            animate_opacity=ft.Animation(self.SCREENSAVER_FADE_MS, ft.AnimationCurve.EASE_IN_OUT),
        )
        self._screensaver_overlay = ft.Container(
            expand=True,
            visible=False,
            bgcolor="#000000",
            on_click=lambda e: self._exit_screensaver(),
            content=ft.Stack(
                expand=True,
                controls=[
                    self._screensaver_layer_a,
                    self._screensaver_layer_b,
                ],
            ),
        )
        self.page.overlay.append(self._screensaver_overlay)

    def mark_activity(self):
        """Сбрасывает таймер бездействия. Вызывается из любых input-хендлеров.
        Если screensaver активен — выходим из него."""
        self._last_activity_ts = time.time()
        if self._screensaver_active:
            self._exit_screensaver()

    async def _idle_monitor_loop(self):
        """Раз в секунду проверяет idle-time. Слайдшоу запускается ТОЛЬКО
        когда юзер в BigPicture-режиме — в обычной библиотеке скрин-сейвер
        не появляется. Бесконечный цикл — ошибки на отдельной итерации
        логируются, но цикл продолжает жить, иначе один сбой убьёт фичу
        до перезапуска лаунчера."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if self._screensaver_active:
                    continue
                if not self._bigpicture_active:
                    continue  # screensaver только в BigPicture
                if time.time() - self._last_activity_ts >= self._idle_timeout_s:
                    self._enter_screensaver()
            except asyncio.CancelledError:
                backend_logger.info("Idle monitor loop cancelled")
                return
            except Exception as ex:
                backend_logger.warning(f"Idle monitor tick error: {ex}")
                # Маленькая пауза чтобы не спамить логи при перманентной ошибке
                try:
                    await asyncio.sleep(2.0)
                except Exception:
                    pass

    def _enter_screensaver(self):
        """Показывает overlay, запускает слайдшоу-цикл с текущей игры в BP."""
        if not self._bigpicture_active:
            return  # двойная защита — только в BP

        # Берём список и индекс из BP (текущая коллекция, фокусная игра)
        bp_games: list = []
        bp_idx: int = 0
        if self._bigpicture_view is not None:
            try:
                bp_games, bp_idx = self._bigpicture_view.get_current_games_snapshot()
            except Exception:
                bp_games, bp_idx = [], 0

        # Фильтруем только игры с готовым landscape-фоном
        games_with_art = [g for g in bp_games if self.game_manager.get_hero_path(g)]
        # Если в текущей коллекции нет ни одной игры с фоном — fallback
        # на всю библиотеку (всё равно показать что-то лучше, чем ничего)
        if not games_with_art:
            games_with_art = [g for g in self.game_manager.get_all_games()
                              if self.game_manager.get_hero_path(g)]
            if not games_with_art:
                return
            start_idx = 0
        else:
            # Найти стартовую позицию = текущая игра BP. Если её нет среди
            # отфильтрованных (например, у неё нет арта) — берём ближайшую
            # следующую с артом.
            start_idx = 0
            if bp_games and 0 <= bp_idx < len(bp_games):
                cur_uid = bp_games[bp_idx].uid
                for i, g in enumerate(games_with_art):
                    if g.uid == cur_uid:
                        start_idx = i
                        break

        self._screensaver_games = games_with_art
        self._screensaver_idx = start_idx
        self._screensaver_active = True
        # Сбросим оба слоя — стартуем с чёрного, далее cycle сам сделает fade-in
        self._screensaver_layer_a.image = None
        self._screensaver_layer_a.opacity = 0.0
        self._screensaver_layer_b.image = None
        self._screensaver_layer_b.opacity = 0.0
        self._screensaver_overlay.visible = True
        self.page.update()
        self.page.run_task(self._screensaver_cycle)
        backend_logger.info(f"Screensaver: started ({len(games_with_art)} arts, idx={start_idx})")

    def _exit_screensaver(self):
        """Прячет overlay, сбрасывает activity-таймер."""
        if not self._screensaver_active:
            return
        self._screensaver_active = False
        self._screensaver_layer_a.opacity = 0.0
        self._screensaver_layer_b.opacity = 0.0
        self._screensaver_overlay.visible = False
        self._last_activity_ts = time.time()
        try:
            self.page.update()
        except Exception:
            pass
        backend_logger.info("Screensaver: exited")

    async def _screensaver_cycle(self):
        """Циклит фоны игр через cross-fade двух слоёв.
        Каждая итерация ровно одинакова: fade-транзишн (1.5с) → hold (7с).
        Первая картинка тоже идёт через fade-in (просто без fade-out
        предыдущего слоя), поэтому ощущение длительности консистентное."""
        try:
            # on_back — слой куда будет загружаться следующая картинка,
            # on_top — слой который сейчас показывает текущую. Стартуют оба
            # с opacity=0 (выставлено в _enter_screensaver).
            on_top = self._screensaver_layer_a
            on_back = self._screensaver_layer_b
            # Стартуем с _screensaver_idx — позиции выставленной в _enter_screensaver
            # под текущую фокусную игру в BP
            idx = self._screensaver_idx

            while self._screensaver_active:
                game = self._screensaver_games[idx % len(self._screensaver_games)]
                hero = self.game_manager.get_hero_path(game)
                idx += 1
                if not hero:
                    continue

                # Cross-fade: новая картинка ставится в нижний слой (opacity 0)
                # и одновременно on_back→1, on_top→1→0. Если on_top уже был 0
                # (первая итерация), animate_opacity не сработает на нём — но
                # время сна одинаковое, так что timing консистентен.
                on_back.image = ft.DecorationImage(src=hero, fit="cover")
                on_back.opacity = 1.0
                on_top.opacity = 0.0
                self.page.update()

                # Ждём окончания fade-транзишна
                await asyncio.sleep(self.SCREENSAVER_FADE_MS / 1000.0)
                if not self._screensaver_active:
                    break

                # Меняем роли: то что фейдилось внутрь стало текущим топом
                on_top, on_back = on_back, on_top

                # Держим текущую картинку
                await asyncio.sleep(self.SCREENSAVER_HOLD_S)
                if not self._screensaver_active:
                    break
        except Exception as ex:
            backend_logger.warning(f"Screensaver cycle ended: {ex}")

    # ========== End Screensaver ==========

    async def _async_full_exit(self):
        """Запускает корректную последовательность завершения и выходит."""
        try:
            await self.shutdown()
        finally:
            stop_tray_icon()
            release_single_instance_lock()
            backend_logger.info("App shutdown completed (full exit)")
            os._exit(0)  # после shutdown корректно — все данные на диске, threads закрыты

    def _on_window_event(self, e):
        """Перехват системных событий окна. Главное — `close` (Alt+F4 / системная X).
        Также: restore для re-fullscreen в BP при возврате из игры."""
        try:
            data = getattr(e, 'data', None) or getattr(e, 'event', None)
        except Exception:
            data = None
        if data == "close":
            self.window_action("close")
            return
        # При возврате окна (alt-tab из игры) применяем play_time для всех
        # pending-сессий — это резервный путь когда watcher не смог
        # обнаружить процесс игры (EAC и подобные).
        if data in ("restore", "unminimize", "focus") and self._pending_play_sessions:
            try:
                for pending_uid in list(self._pending_play_sessions.keys()):
                    self._finalize_pending_play_session(pending_uid)
            except Exception:
                pass
        # При возврате окна (alt-tab из игры) и активном BP — снова в fullscreen.
        # NB: gamepad-гейтинг идёт через _is_launcher_foreground() (WinAPI),
        # не через этот обработчик — Flet ненадёжно фаерит focus/blur при
        # WinAPI-сворачивании.
        if data in ("restore", "unminimize", "focus") and self._bigpicture_active:
            try:
                if not self.page.window.full_screen:
                    self.page.window.full_screen = True
                    self.page.update()
                    self._force_window_to_front()
            except Exception:
                pass

    async def shutdown(self):
        """Корректная остановка приложения: gamepad → bigpicture → game_manager."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        backend_logger.info("CyberLauncher shutdown started")
        try:
            if self.gamepad_manager is not None:
                self.gamepad_manager.shutdown()
        except Exception as ex:
            backend_logger.warning(f"Gamepad shutdown error: {ex}")
        try:
            await self.game_manager.shutdown()
        except Exception as ex:
            backend_logger.error(f"GameManager shutdown error: {ex}")
        backend_logger.info("CyberLauncher shutdown finished")

    # ========== Gamepad + BigPicture ==========

    def _init_gamepad(self):
        """Лениво инициализирует GamepadManager. Безопасно если pygame не установлен."""
        if not HAS_GAMEPAD or GamepadManager is None:
            backend_logger.info("Gamepad support unavailable (pygame missing)")
            return
        try:
            self.gamepad_manager = GamepadManager()
            if not self.gamepad_manager.available:
                self.gamepad_manager = None
                return
            self.gamepad_manager.on_button = self._gp_dispatch_button
            self.gamepad_manager.on_dpad = self._gp_dispatch_dpad
            self.gamepad_manager.on_axis = self._gp_dispatch_axis
            self.gamepad_manager.on_guide = self._gp_on_guide
            self.gamepad_manager.on_connected = self._gp_on_connected
            self.gamepad_manager.on_disconnected = lambda: backend_logger.info("Gamepad: disconnected")
            self.gamepad_manager.start()
        except Exception as e:
            backend_logger.error(f"Gamepad init error: {e}")
            self.gamepad_manager = None

    def _gp_on_connected(self, name: str = "", num_buttons: int = 0, is_xinput: bool = False):
        """Callback подключения геймпада. Логирует и показывает snackbar
        с диагностикой если контроллер не в XInput."""
        backend_logger.info(f"Gamepad: connected ({name}, buttons={num_buttons}, xinput={is_xinput})")
        try:
            if not is_xinput or num_buttons == 0:
                msg = (
                    f"Геймпад '{name}' определён без XInput. "
                    "Переключите донгл/контроллер в режим X (XInput), "
                    "иначе кнопки не работают."
                )
                self.page.run_task(self._gp_show_warning, msg)
            else:
                self.page.run_task(self._gp_show_info, f"Геймпад готов: {name}")
        except Exception:
            pass

    async def _gp_show_warning(self, msg: str):
        try:
            self.show_snackbar(msg, bgcolor="#FF9800", duration=8000)
        except Exception:
            pass

    async def _gp_show_info(self, msg: str):
        try:
            self.show_snackbar(msg, bgcolor="#00C853", duration=3000)
        except Exception:
            pass

    def _gp_should_skip(self) -> bool:
        """Единая точка для всех gamepad-гейтов: silence-window после
        launch + foreground-check. Возвращает True если событие надо
        отбросить."""
        if time.time() < self._gamepad_silence_until:
            return True
        if not self._is_launcher_foreground():
            return True
        return False

    def _gp_dispatch_button(self, name: str, pressed: bool):
        # Колбэк из gamepad-thread'а — пересылаем в UI loop, если разрешено.
        if self._gp_should_skip():
            return
        if pressed:
            self.page.run_task(self._async_mark_activity)
        if self._bigpicture_view is not None and self._bigpicture_active:
            self.page.run_task(self._gp_async_button, name, pressed)

    def _gp_dispatch_dpad(self, name: str, pressed: bool):
        if self._gp_should_skip():
            return
        if pressed:
            self.page.run_task(self._async_mark_activity)
        if self._bigpicture_view is not None and self._bigpicture_active:
            self.page.run_task(self._gp_async_dpad, name, pressed)

    def _gp_dispatch_axis(self, name: str, value: float):
        if self._gp_should_skip():
            return
        # Только значимые движения стиков/триггеров
        if abs(value) > 0.3:
            self.page.run_task(self._async_mark_activity)
        if self._bigpicture_view is not None and self._bigpicture_active:
            self.page.run_task(self._gp_async_axis, name, value)

    async def _async_mark_activity(self):
        """Async-обёртка для mark_activity (gamepad коллбэки в другом thread'е)."""
        self.mark_activity()

    def _gp_on_guide(self):
        # Guide — глобальная кнопка, всегда переключает BigPicture.
        # Гейтим тем же набором проверок — иначе Guide из игры может
        # переключить наш режим на заднем плане.
        if self._gp_should_skip():
            return
        self.page.run_task(self._async_mark_activity)  # снять screensaver
        self.page.run_task(self._gp_async_toggle_bp)

    async def _gp_async_button(self, name: str, pressed: bool):
        try:
            self._bigpicture_view.handle_button(name, pressed)
        except Exception as e:
            backend_logger.error(f"BigPicture button error: {e}")

    async def _gp_async_dpad(self, name: str, pressed: bool):
        try:
            self._bigpicture_view.handle_dpad(name, pressed)
        except Exception as e:
            backend_logger.error(f"BigPicture dpad error: {e}")

    async def _gp_async_axis(self, name: str, value: float):
        try:
            self._bigpicture_view.handle_axis(name, value)
        except Exception as e:
            backend_logger.error(f"BigPicture axis error: {e}")

    async def _gp_async_toggle_bp(self):
        self.toggle_bigpicture()

    def _on_keyboard_event(self, e):
        """Глобальные хоткеи + клавиатурная навигация в BigPicture-режиме
        (на случай если геймпад не определился)."""
        # Любая клавиша = активность пользователя
        self.mark_activity()
        try:
            key = getattr(e, "key", None)
            if key is None:
                return
            # F11 — toggle BigPicture
            if key == "F11":
                self.toggle_bigpicture()
                return
            # ESC — выход из BigPicture (в обычном режиме игнорируется)
            if key == "Escape" and self._bigpicture_active:
                self._exit_bigpicture()
                return
            # Если в BigPicture — клавиатура работает как геймпад
            if self._bigpicture_active and self._bigpicture_view is not None:
                bp = self._bigpicture_view
                if key == "Arrow Left":
                    bp.handle_dpad("DPAD_LEFT", True)
                elif key == "Arrow Right":
                    bp.handle_dpad("DPAD_RIGHT", True)
                elif key == "Arrow Up":
                    bp.handle_dpad("DPAD_UP", True)
                elif key == "Arrow Down":
                    bp.handle_dpad("DPAD_DOWN", True)
                elif key == "Enter":
                    bp.handle_button("A", True)
                elif key == "Backspace":
                    bp.handle_button("B", True)
                elif key == "F":  # F = favorite (Y)
                    bp.handle_button("Y", True)
                elif key == "C":  # C = collections (X)
                    bp.handle_button("X", True)
                elif key in ("Q", "Page Up"):
                    bp.handle_button("LB", True)
                elif key in ("E", "Page Down"):
                    bp.handle_button("RB", True)
        except Exception as ex:
            backend_logger.warning(f"Keyboard event error: {ex}")

    def toggle_bigpicture(self):
        """Переключает BigPicture-режим. Сохраняет/восстанавливает window state."""
        if BigPictureView is None:
            self.show_snackbar("BigPicture модуль не доступен", bgcolor="#FF5252")
            return

        if not self._bigpicture_active:
            self._enter_bigpicture()
        else:
            self._exit_bigpicture()

    def _enter_bigpicture(self):
        # Сохраняем текущее состояние окна
        self._pre_bigpicture_state = {
            "width": self.page.window.width,
            "height": self.page.window.height,
            "maximized": self.page.window.maximized,
            "title_bar_hidden": self.page.window.title_bar_hidden,
        }

        # Создаём view только при первом входе, дальше переиспользуем
        if self._bigpicture_view is None:
            self._bigpicture_view = BigPictureView(
                page=self.page,
                game_manager=self.game_manager,
                settings=self.settings,
                on_exit=self._exit_bigpicture_from_view,
                on_launch=self._bp_launch_game,
                on_request_scan=self._bp_request_scan,
                on_change_theme=self.change_theme,
                on_save_settings=self.save_settings,  # для toggle rumble из BP
                on_pick_custom_bg=self._bp_pick_custom_bg,  # своя картинка-фон
                gamepad_manager=self.gamepad_manager,  # для rumble
            )
            self._bp_root = self._bigpicture_view.build()
        else:
            # При повторном входе — обновим коллекции и игры на случай изменений
            self._bigpicture_view.refresh_after_external_change()

        # Скрываем sidebar и titlebar, показываем bp_root
        self._title_bar.visible = False
        self.sidebar.visible = False
        self.bg_container.content = self._bp_root

        # Переход в fullscreen на Windows иногда сбрасывает focus / окно прячется.
        # 1) Сбрасываем maximized и minimized — чтобы Windows не путалась
        # 2) Включаем fullscreen
        # 3) Принудительно поднимаем окно на передний план
        try:
            self.page.window.minimized = False
            self.page.window.maximized = False
            self.page.window.visible = True
        except Exception:
            pass

        try:
            self.page.window.full_screen = True
        except Exception:
            self.page.window.maximized = True

        self._bigpicture_active = True
        self.page.update()
        # Принудительно поднимаем окно через WinAPI — иначе fullscreen может
        # уйти на задний план если в этот момент был активен другой процесс.
        self._force_window_to_front()
        backend_logger.info("Entered BigPicture mode")

    def _force_window_to_front(self):
        """Поднимает окно на передний план через WinAPI. Используется после
        перехода в fullscreen, потому что Flet/Flutter иногда теряет focus."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = self._get_launcher_hwnd() or user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                SW_SHOW = 5
                user32.ShowWindow(hwnd, SW_SHOW)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
        except Exception as e:
            backend_logger.warning(f"Force-to-front failed: {e}")

    def _get_launcher_hwnd(self):
        """Lazy-cached HWND нашего окна.
        FindWindowW по точному заголовку иногда промахивается (Flet/Flutter
        может добавить symbols или title менялся). Поэтому сначала строгий
        FindWindow, затем fallback EnumWindows с поиском по подстроке."""
        if sys.platform != "win32":
            return None
        if self._launcher_hwnd:
            # Verify cache still valid — handle could be invalidated
            try:
                if ctypes.windll.user32.IsWindow(self._launcher_hwnd):
                    return self._launcher_hwnd
            except Exception:
                pass
            self._launcher_hwnd = None

        user32 = ctypes.windll.user32
        try:
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                self._launcher_hwnd = hwnd
                return hwnd
        except Exception:
            pass

        # Fallback: enumerate visible windows, find one containing "CyberLauncher"
        try:
            from ctypes import wintypes
            EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            found = [None]

            def _cb(h, _lp):
                try:
                    if not user32.IsWindowVisible(h):
                        return True
                    length = user32.GetWindowTextLengthW(h)
                    if length == 0:
                        return True
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(h, buf, length + 1)
                    if "CyberLauncher" in buf.value:
                        found[0] = h
                        return False  # stop enumeration
                except Exception:
                    pass
                return True

            user32.EnumWindows(EnumWindowsProc(_cb), 0)
            if found[0]:
                self._launcher_hwnd = found[0]
                return found[0]
        except Exception:
            pass
        return None

    def _is_launcher_foreground(self) -> bool:
        """True если наше окно сейчас активное (foreground и НЕ свёрнуто).
        Безопасно вызывается из gamepad-thread'а — WinAPI thread-safe.
        Fail-CLOSED: если HWND не найден — возвращаем False (лучше упустить
        одно нажатие гейпада в лаунчере чем стрелять кнопками в фоне когда
        юзер играет)."""
        if sys.platform != "win32":
            return True  # Linux/Mac без гейтинга
        try:
            user32 = ctypes.windll.user32
            hwnd = self._get_launcher_hwnd()
            if not hwnd:
                return False  # fail-closed
            # Свёрнуто? — точно не foreground
            if user32.IsIconic(hwnd):
                return False
            fg = user32.GetForegroundWindow()
            return fg == hwnd
        except Exception:
            return False

    def _exit_bigpicture_from_view(self):
        # Колбэк от BigPictureView (кнопка B на главном)
        self._exit_bigpicture()

    def _exit_bigpicture(self):
        if not self._bigpicture_active:
            return
        # Если screensaver был активен — обязательно гасим перед выходом из BP,
        # иначе overlay останется висеть поверх обычной библиотеки
        if self._screensaver_active:
            self._exit_screensaver()
        try:
            self.page.window.full_screen = False
        except Exception:
            pass
        # Восстанавливаем layout
        self._title_bar.visible = True
        self.sidebar.visible = True
        self.bg_container.content = self.games_container

        st = self._pre_bigpicture_state or {}
        if "maximized" in st:
            self.page.window.maximized = st["maximized"]
        if "width" in st and not st.get("maximized"):
            self.page.window.width = st["width"]
        if "height" in st and not st.get("maximized"):
            self.page.window.height = st["height"]

        self._bigpicture_active = False
        self.page.update()
        # Перерисуем сетку — могли изменить favorite/коллекции в BigPicture
        try:
            self.update_game_grid(reset_page=False)
        except Exception:
            pass
        backend_logger.info("Exited BigPicture mode")

    def _bp_launch_game(self, game: GameModel):
        """Запуск игры из BigPicture. Перед стартом сворачиваем лаунчер,
        иначе fullscreen-окно перекрывает игру и она появляется свёрнутой
        в трее (Windows не отдаёт foreground новому окну поверх fullscreen)."""
        self.page.run_task(self._bp_async_launch, game)

    async def _bp_async_launch(self, game: GameModel):
        # Включаем gamepad silence ДО любых window-операций — гарантирует что
        # ни одна кнопка не пройдёт пока окна перетасовываются
        self._gamepad_silence_until = time.time() + 2.5
        # 1. Выйти из fullscreen — это отпускает Z-order topmost-флаги
        try:
            self.page.window.full_screen = False
            self.page.update()
        except Exception:
            pass
        await asyncio.sleep(0.05)
        # 2. Свернуть окно через WinAPI (надёжнее чем page.window.minimized)
        try:
            if sys.platform == "win32":
                user32 = ctypes.windll.user32
                hwnd = self._get_launcher_hwnd() or user32.FindWindowW(None, WINDOW_TITLE)
                if hwnd:
                    SW_MINIMIZE = 6
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
        except Exception:
            pass
        # 3. Запускаем игру — теперь её окно встанет поверх свободно
        await self.launch_game(game)
        # 4. Обновим hero-карту BP — чтобы при возврате юзер сразу увидел
        # свежий last_played (а не "никогда")
        try:
            if self._bigpicture_view is not None:
                self._bigpicture_view.refresh_after_external_change()
        except Exception:
            pass

    def _on_game_exited(self, uid: str):
        """Callback из GameManager-watcher'а: игра завершилась, поднимаем
        окно лаунчера. Вызывается из watcher-thread'а, не UI-thread'а —
        WinAPI безопасен из любого потока, page.run_task для Flet-операций."""
        backend_logger.info(f"Game exited (uid={uid}) — restoring launcher")
        # Сбрасываем silence-window заранее — игра не запущена больше
        self._gamepad_silence_until = 0.0
        # Финализируем pending-сессию play_time для игр которые watcher
        # game_manager не смог точно отследить (Steam с EAC, runas)
        self._finalize_pending_play_session(uid)
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = self._get_launcher_hwnd()
            if hwnd:
                SW_RESTORE = 9
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
        except Exception as e:
            backend_logger.warning(f"Restore-after-game WinAPI failed: {e}")
        # Если был BP — снова в fullscreen. Делаем через UI-loop потому что
        # это меняет page state, не только окно.
        if self._bigpicture_active:
            try:
                self.page.run_task(self._async_refullscreen_after_game)
            except Exception:
                pass

    def _finalize_pending_play_session(self, uid: str):
        """Применяет play_time для pending-сессии. Вызывается когда:
        - watcher определил окончание игры → _on_game_exited
        - пользователь вернулся в лаунчер (alt-tab → window restore)
        Удаляет запись из pending после применения, чтобы не учитывать
        одно и то же дважды."""
        start_ts = self._pending_play_sessions.pop(uid, None)
        if start_ts is None:
            return
        elapsed_min = int((time.time() - start_ts) / 60)
        # 1 мин минимум (фильтр мгновенных отмен), 24 ч максимум
        # (страховка от долгих простоев когда забыли свернуть игру)
        if elapsed_min < 1 or elapsed_min > 24 * 60:
            backend_logger.info(f"Skipping play_time tracking for uid={uid}: {elapsed_min} min out of range")
            return
        game = self.game_manager._games.get(uid)
        if not game:
            return
        game.play_time = (game.play_time or 0) + elapsed_min
        self.game_manager.request_save()
        backend_logger.info(f"Tracked play time for '{game.title}': +{elapsed_min} min (total: {game.play_time})")

    async def _async_refullscreen_after_game(self):
        try:
            if not self.page.window.full_screen:
                self.page.window.full_screen = True
                self.page.update()
                self._force_window_to_front()
            # Обновим hero — last_played / play_time изменились
            if self._bigpicture_view is not None:
                self._bigpicture_view.refresh_after_external_change()
        except Exception as e:
            backend_logger.warning(f"Re-fullscreen after game failed: {e}")

    def _bp_request_scan(self):
        """Сканирование библиотеки из BigPicture."""
        self.page.run_task(self.refresh_library)
        # После скана обновим BigPicture view
        async def _refresh_bp_after():
            try:
                while True:
                    await asyncio.sleep(2)
                    if self._bigpicture_view is not None:
                        self._bigpicture_view.refresh_after_external_change()
                    break
            except Exception:
                pass
        self.page.run_task(_refresh_bp_after)

    def _bp_pick_custom_bg(self, game: GameModel):
        """Открывает tkinter file picker для выбора картинки-фона.
        Запускается в отдельном thread'е, чтобы не блокировать Flet/Flutter
        event loop. После успеха — обновляем фон в BP."""
        def pick_thread():
            try:
                root = Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                file_path = filedialog.askopenfilename(
                    title=f"Свой фон для: {game.title}",
                    filetypes=[
                        ("Изображения", "*.jpg *.jpeg *.png *.webp *.bmp"),
                        ("JPEG", "*.jpg *.jpeg"),
                        ("PNG", "*.png"),
                        ("WebP", "*.webp"),
                        ("Все файлы", "*.*"),
                    ],
                )
                root.destroy()
                if not file_path:
                    return  # Отмена
                # Сохранение через Pillow — sync, поэтому в этом же потоке
                ok = self.game_manager.set_custom_hero_art(game, file_path)
                # Обновляем BP в UI-loop
                if ok and self._bigpicture_view is not None:
                    async def _refresh_bp():
                        try:
                            self._bigpicture_view.apply_custom_bg(game.uid)
                        except Exception:
                            pass
                    self.page.run_task(_refresh_bp)
            except Exception as ex:
                backend_logger.error(f"Custom bg picker error: {ex}")

        threading.Thread(target=pick_thread, daemon=True).start()
    
    async def on_refresh_click(self, e):
        """Обработчик нажатия кнопки обновления"""
        try:
            # Low-level debug write
            with open(get_app_data_dir() / "debug_click.txt", "a", encoding="utf-8") as f:
                f.write("Button clicked!\n")
                
            backend_logger.info("UI: Update Library button clicked")
            
            self.show_snackbar("Запуск сканирования...")
            
            # Since we are async, we can call refresh_library directly (await it)
            # or use run_task if we want it to be detached? 
            # calling directly is safer here to keep context
            await self.refresh_library()
        except Exception as ex:
            import traceback
            err = traceback.format_exc()
            backend_logger.error(f"Error in on_refresh_click: {err}")
            with open(get_app_data_dir() / "debug_click.txt", "a", encoding="utf-8") as f:
                f.write(f"Error: {err}\n")

    async def load_library(self):
        backend_logger.info("UI: load_library started")
        self.loading_overlay.show("Загрузка библиотеки...")

        await self.game_manager.load_library()
        self._all_games_list = list(self.game_manager.get_all_games())

        if not self._all_games_list:
             backend_logger.info("UI: Library empty, starting initial scan")
             self.loading_overlay.show("Первый запуск. Сканирование игр...")
             excluded = self.settings.get("excluded_paths", [])
             extra_paths = self.settings.get("extra_game_paths", [])
             launchers = self.settings.get("enabled_launchers", {"Steam": True})
             await self.game_manager.scan_all_games(excluded_paths=excluded, additional_paths=extra_paths, enabled_launchers=launchers)
             self._all_games_list = list(self.game_manager.get_all_games())

        # Загружаем коллекции в сайдбар
        self.refresh_collections_sidebar()
        self.update_game_grid()

        self.loading_overlay.hide()
        self.update_game_grid()

        # Прогреваем кэш landscape-арта для BigPicture-фона в фоне.
        # Не блокирует UI — page.run_task шедулит coroutine и идёт дальше.
        # К моменту F11 весь арт уже на диске.
        self.page.run_task(self.game_manager.prefetch_hero_art)

    async def refresh_library(self):
        backend_logger.info("UI: refresh_library async task started")
        self.loading_overlay.show("Сканирование игр...")
        self.page.update()  # ВАЖНО: показать оверлей СРАЗУ
        await asyncio.sleep(0.1) # Give UI time to render
        async def process():
            self.game_manager.set_progress_callback(self.on_scan_progress)
            excluded = self.settings.get("excluded_paths", [])
            extra_paths = self.settings.get("extra_game_paths", [])
            launchers = self.settings.get("enabled_launchers", {"Steam": True})
            await self.game_manager.scan_all_games(excluded_paths=excluded, additional_paths=extra_paths, enabled_launchers=launchers)
            self._all_games_list = list(self.game_manager.get_all_games())
        await process() # Call the async process function
        self.loading_overlay.hide()
        self.page.update()  # Скрыть оверлей
        # Сайдбар с коллекциями нужно освежить — после rescan-а
        # счётчики игр в коллекциях могут измениться
        self.refresh_collections_sidebar()
        self.update_game_grid()
        # Догружаем hero-арт для новых Steam-игр (если что-то добавилось)
        self.page.run_task(self.game_manager.prefetch_hero_art)

    def on_scan_progress(self, message: str, current: int, total: int):
        self.loading_overlay.update_progress(message, current, total)
        self.page.update()  # Обновляем UI для отображения прогресса
    
    def update_game_grid(self, reset_page: bool = True):
        """Оптимизированное обновление сетки игр с пагинацией"""
        
        if reset_page:
            self._current_page = 0
            
            # Получаем и сортируем игры один раз
            if self.current_filter == "all":
                games = self.game_manager.get_all_games()
            elif self.current_filter == "favorites":
                games = self.game_manager.get_games_by_category(Category.FAVORITES.value)
            elif self.current_filter == "steam":
                games = self.game_manager.get_games_by_platform(Platform.STEAM.value)
            elif self.current_filter == "epic":
                games = self.game_manager.get_games_by_platform(Platform.EPIC.value)
            elif self.current_filter == "system":
                games = self.game_manager.get_games_by_platform(Platform.SYSTEM.value)
            elif self.current_filter.startswith("collection_"):
                # Фильтр по коллекции
                collection_id = self.current_filter.replace("collection_", "")
                games = self.game_manager.get_games_by_collection(collection_id)
            else:
                games = self.game_manager.get_all_games()
            
            self._all_games_list = list(games)

            # Search filtering
            if hasattr(self, 'search_field') and self.search_field.value:
                query = self.search_field.value.lower()
                self._all_games_list = [g for g in self._all_games_list if query in g.title.lower()]
            
            # Сортировка
            if self.current_sort == "name_asc":
                self._all_games_list.sort(key=lambda g: g.title.lower())
            elif self.current_sort == "name_desc":
                self._all_games_list.sort(key=lambda g: g.title.lower(), reverse=True)
            elif self.current_sort == "date_desc":
                self._all_games_list.sort(key=lambda g: g.added_date or "", reverse=True)
            elif self.current_sort == "date_asc":
                self._all_games_list.sort(key=lambda g: g.added_date or "")
        
        self._render_visible_cards()
    
    def _render_visible_cards(self):
        """Рендерит только видимые карточки с пагинацией - ОПТИМИЗИРОВАНО"""
        show_size = self.settings.get("show_game_size", False)
        enable_animations = self.settings.get("enable_animations", False)  # Default OFF for speed

        # Количество карточек для отображения
        cards_to_show = (self._current_page + 1) * self._page_size
        visible_games = self._all_games_list[:cards_to_show]

        # Build new controls list (faster than modifying in-place)
        new_controls = []

        for game in visible_games:
            if game.uid in self._card_cache:
                card = self._card_cache[game.uid]
            else:
                card = GameCard(
                    game=game,
                    on_click=self.on_game_click,
                    on_favorite=self.on_favorite_click,
                    on_upload=self.show_upload_dialog,
                    on_exclude=self.exclude_game,
                    on_properties=self.show_game_properties_dialog,
                    show_size=show_size,
                    enable_animations=enable_animations
                )
                self._card_cache[game.uid] = card

            new_controls.append(card)

        # Кнопка "Показать ещё" если есть ещё игры
        if cards_to_show < len(self._all_games_list):
            remaining = len(self._all_games_list) - cards_to_show
            load_more_btn = ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.EXPAND_MORE, color=ACCENT_BLUE, size=24),
                        ft.Text(f"Показать ещё ({remaining})", color=TEXT_WHITE, size=12),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=5,
                ),
                alignment=ft.Alignment(0, 0),
                bgcolor="#1E1E1E",
                border_radius=15,
                border=ft.Border.all(1, "#333333"),
                on_click=self._load_more_games,
                ink=True,
            )
            new_controls.append(load_more_btn)

        # Single assignment instead of clear + append loop
        self.game_grid.controls = new_controls

        total = self.game_manager.games_count
        shown = len(visible_games)
        if self.current_filter == "all":
            self.games_count_text.value = f"{shown}/{total} игр"
        else:
            self.games_count_text.value = f"{shown} из {len(self._all_games_list)}"

        self.page.update()


    def _load_more_games(self, e):
        """Загружает следующую страницу игр"""
        self._current_page += 1
        self._render_visible_cards()

    def _on_search_changed(self):
        """Debounced search: ждём 250 мс паузы перед rebuild сетки."""
        self._search_debounce_token += 1
        token = self._search_debounce_token

        async def _delayed():
            await asyncio.sleep(0.25)
            # Если за это время был ещё один keystroke — этот вызов устарел
            if token != self._search_debounce_token:
                return
            self.update_game_grid(reset_page=True)

        self.page.run_task(_delayed)
    
    def on_filter_click(self, filter_name: str):
        """Оптимизированная обработка переключения вкладок"""
        if filter_name == self.current_filter:
            return

        # Batch update all buttons without individual updates
        for name, button in self.sidebar_buttons.items():
            if isinstance(button, SidebarButton):
                button.set_active(name == filter_name)
            elif isinstance(button, ft.Container) and name.startswith("collection_"):
                # Для контейнеров коллекций меняем фон
                button.bgcolor = "#33D500F9" if name == filter_name else "transparent"

        self.current_filter = filter_name

        # Панель "Управление коллекцией" видна только когда выбрана коллекция
        self.collection_actions_panel.visible = filter_name.startswith("collection_")

        if filter_name == "settings":
            # Switch content and update once
            self.settings_view = self.build_settings_view()
            self.bg_container.content = self.settings_view
            self.page.update()  # Single update for everything
        elif filter_name == "disk_info":
            # Show disk info view
            self.disk_info_view = self.build_disk_info_view()
            self.bg_container.content = self.disk_info_view
            self.page.update()
        elif filter_name == "wishlist":
            # Раздел "Желаемое" — отдельная view с карточками Steam-игр
            self.wishlist_view = self.build_wishlist_view()
            self.bg_container.content = self.wishlist_view
            self.page.update()
        else:
            self.bg_container.content = self.games_container
            self.update_game_grid()  # This already calls page.update()

    def _get_custom_path_controls(self):
        return [
            ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.FOLDER_OPEN, color=TEXT_GREY),
                        ft.Text(path, color=TEXT_WHITE, size=13, expand=True),
                        ft.IconButton(
                            ft.Icons.DELETE_OUTLINE, 
                            icon_color="#FF5252", 
                            tooltip="Удалить папку",
                            on_click=lambda _, p=path: self.remove_custom_path(p)
                        )
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                ),
                bgcolor="#252525",
                padding=10,
                border_radius=8,
            ) for path in self.settings.get("extra_game_paths", []) if path
        ] + [
            ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.ADD, color=ACCENT_BLUE),
                        ft.Text("Добавить папку", color=ACCENT_BLUE, weight=ft.FontWeight.BOLD)
                    ],
                    alignment=ft.MainAxisAlignment.CENTER
                ),
                bgcolor="#1E1E1E",
                padding=12,
                border_radius=8,
                border=ft.Border.all(1, ACCENT_BLUE),
                on_click=self.add_custom_path_click,
                ink=True,
            )
        ]

    def show_settings_view(self):
        self.settings_view = self.build_settings_view()
        self.bg_container.content = self.settings_view
        self.page.update()  # Use page.update for full refresh
    
    def show_games_view(self):
        self.bg_container.content = self.games_container
        self.page.update()  # Use page.update for full refresh

    
    def set_sort(self, sort_key: str):
        self.current_sort = sort_key
        self.sort_text.value = self.sort_labels.get(sort_key, "Стандартная")
        self.sort_text.update()
        self.update_game_grid()
    
    def on_game_click(self, game: GameModel):
        self.page.run_task(self.launch_game, game)
    
    async def launch_game(self, game: GameModel):
        # Записываем старт сессии для ВСЕХ игр — единая точка истины
        # для play_time. game_manager-watcher только уведомляет о выходе
        # (если может — для системных игр через Popen, для Steam-EAC он
        # может не сработать); main.py всегда считает время сам по
        # таймстампам запуска и финализации.
        self._pending_play_sessions[game.uid] = time.time()

        success = await self.game_manager.launch_game(game.uid)

        if success:
            self.show_snackbar(f"Запуск: {game.title}", bgcolor="#333333")
        else:
            # Запуск не состоялся (UAC отказ / файл не найден) — снимаем сессию
            self._pending_play_sessions.pop(game.uid, None)
            self.show_snackbar(f"Ошибка запуска: {game.title}", bgcolor="#8B0000")
    
    def on_favorite_click(self, game: GameModel):
        self.page.run_task(self.toggle_favorite, game)
    
    async def toggle_favorite(self, game: GameModel):
        await self.game_manager.toggle_favorite(game.uid)
        # Инвалидируем кеш карточки чтобы она пересоздалась с новым состоянием
        if game.uid in self._card_cache:
            del self._card_cache[game.uid]
        # ИСПРАВЛЕНИЕ: Не сбрасываем страницу при изменении избранного
        self.update_game_grid(reset_page=False)


    # ========== Custom Path Methods ==========

    def add_custom_path_click(self, e):
        """Open folder picker to add custom scan path"""
        def pick_folder_thread():
            try:
                root = Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                
                path = filedialog.askdirectory(
                    title="Выберите папку с играми"
                )
                
                root.destroy()
                
                if path:
                    self.page.run_task(self._add_custom_path, path)
            except Exception as ex:
                print(f"[ERROR] Folder picker error: {ex}")
        
        threading.Thread(target=pick_folder_thread, daemon=True).start()

    async def _add_custom_path(self, path: str):
        path = str(Path(path).resolve())
        if "extra_game_paths" not in self.settings:
            self.settings["extra_game_paths"] = []
        
        if path not in self.settings["extra_game_paths"]:
            self.settings["extra_game_paths"].append(path)
            self.save_settings()
            self.show_snackbar(f"Папка добавлена: {path}", bgcolor="#4CAF50")
            
            # Update only the custom paths UI
            if hasattr(self, 'custom_paths_column'):
                self.custom_paths_column.controls = self._get_custom_path_controls()
                self.custom_paths_column.update()
            else:
                self.show_settings_view() # Fallback
        else:
            self.show_snackbar("Эта папка уже добавлена", bgcolor="#FF9800")

    def remove_custom_path(self, path: str):
        if "extra_game_paths" in self.settings:
            if path in self.settings["extra_game_paths"]:
                self.settings["extra_game_paths"].remove(path)
                self.save_settings()
                
                # Update only the custom paths UI
                if hasattr(self, 'custom_paths_column'):
                    self.custom_paths_column.controls = self._get_custom_path_controls()
                    self.custom_paths_column.update()
                else:
                    self.show_settings_view() # Fallback
                
                self.show_snackbar("Папка удалена из списка сканирования", bgcolor="#FF9800")

    # ========== Cover Upload Methods ==========

    def show_upload_dialog(self, game: GameModel):
        """Show dialog for manual cover upload"""
        
        # Сохраняем игру для которой загружаем обложку
        self.upload_target_game = game
        self.upload_dialog = None  # Ссылка на диалог для закрытия

        def on_url_submit(e):
            url = url_input.value
            if url and url.strip():
                self.page.run_task(self.upload_cover_from_url, game, url.strip())
                self.upload_dialog.open = False
                self.page.update()

        def on_refresh(e):
            """Force re-download from APIs"""
            self.page.run_task(self.refresh_cover, game)
            self.upload_dialog.open = False
            self.page.update()

        def close_dialog(e):
            self.upload_dialog.open = False
            self.page.update()
        
        def on_pick_file_click(e):
            """Открываем нативный диалог выбора файла через tkinter"""
            print(f"[DEBUG] Opening file picker for: {game.title}")
            self.upload_dialog.open = False  # Закрываем диалог
            self.page.update()
            
            # Запускаем tkinter диалог в отдельном потоке чтобы не блокировать UI
            def pick_file_thread():
                try:
                    # Создаём скрытое окно tkinter
                    root = Tk()
                    root.withdraw()  # Скрываем главное окно
                    root.attributes('-topmost', True)  # Поверх всех окон
                    
                    file_path = filedialog.askopenfilename(
                        title="Выберите изображение обложки",
                        filetypes=[
                            ("Изображения", "*.jpg *.jpeg *.png *.gif *.webp"),
                            ("JPEG", "*.jpg *.jpeg"),
                            ("PNG", "*.png"),
                            ("GIF", "*.gif"),
                            ("WebP", "*.webp"),
                            ("Все файлы", "*.*"),
                        ]
                    )
                    
                    root.destroy()  # Закрываем tkinter
                    
                    if file_path:
                        print(f"[DEBUG] Local file selected: {file_path}")
                        # Вызываем загрузку обложки через async task
                        self.page.run_task(self.upload_cover_from_file, game, file_path)
                    else:
                        print("[DEBUG] File picker cancelled")
                        
                except Exception as ex:
                    print(f"[ERROR] File picker error: {ex}")
            
            # Запускаем в отдельном потоке
            threading.Thread(target=pick_file_thread, daemon=True).start()

    
        url_input = ft.TextField(
            label="URL изображения",
            label_style=ft.TextStyle(color=TEXT_GREY),
            hint_text="https://example.com/cover.jpg",
            hint_style=ft.TextStyle(color="#555555"),
            text_style=ft.TextStyle(color=TEXT_WHITE),
            border_color="#333333",
            bgcolor="#252525",
            border_radius=10,
            width=400,
            on_submit=on_url_submit,
        )

        # Получаем цвета текущей темы для градиента
        theme_data = GRADIENT_THEMES.get(self.current_theme, GRADIENT_THEMES["dark"])
        gradient_colors = theme_data["colors"]

        self.upload_dialog = ft.AlertDialog(
            modal=True,
            bgcolor="transparent",
            content_padding=0,
            title=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.IMAGE, color=ACCENT_BLUE),
                    ft.Text(f"Обложка: {game.title[:30]}...", color=TEXT_WHITE, weight=ft.FontWeight.BOLD),
                ],
                alignment=ft.MainAxisAlignment.START,
            ),
            content=ft.Container(
                width=480,
                padding=25,
                border_radius=15,
                gradient=ft.LinearGradient(
                    begin=ft.Alignment(-1, -1),
                    end=ft.Alignment(1, 1),
                    colors=gradient_colors,
                ),
                content=ft.Column(
                    controls=[
                        ft.Text("Выберите способ загрузки:", size=14, color=TEXT_GREY),
                        ft.Container(height=10),
                        
                        # Кнопка выбора файла с компьютера
                        ft.Container(
                            content=ft.Row(
                                controls=[
                                    ft.Icon(ft.Icons.FOLDER_OPEN, color="#FFFFFF"),
                                    ft.Text("Выбрать файл с компьютера", color="#FFFFFF", weight=ft.FontWeight.W_600),
                                ],
                                alignment=ft.MainAxisAlignment.CENTER,
                            ),
                            bgcolor=ACCENT_BLUE,
                            padding=15,
                            border_radius=10,
                            on_click=on_pick_file_click,
                            ink=True,
                        ),
                        ft.Text(
                            "Поддерживаемые форматы: JPG, PNG, GIF, WebP",
                            size=11,
                            color="#555555",
                            text_align=ft.TextAlign.CENTER,
                        ),
                        
                        ft.Container(height=15),
                        ft.Row(
                            controls=[
                                ft.Divider(color="#333333", expand=True),
                                ft.Text(" ИЛИ ", size=12, color="#555555"),
                                ft.Divider(color="#333333", expand=True),
                            ],
                            alignment=ft.MainAxisAlignment.CENTER,
                        ),
                        ft.Container(height=15),
                        
                        ft.Text("Загрузить по URL:", size=13, color=TEXT_GREY),
                        url_input,
                        ft.Container(
                            content=ft.Text("Загрузить", color="#FFFFFF"),
                            bgcolor="#333333",
                            padding=10,
                            border_radius=8,
                            alignment=ft.Alignment(0, 0),
                            on_click=on_url_submit,
                            ink=True,
                        ),
                        
                        ft.Container(height=20),
                        ft.Divider(color="#333333"),
                        ft.Container(height=10),
                        
                        # Кнопка API
                        ft.Container(
                            content=ft.Row(
                                controls=[
                                    ft.Icon(ft.Icons.AUTO_AWESOME, color=ACCENT_PURPLE, size=18),
                                    ft.Text("Авто-поиск в API", color=TEXT_WHITE, size=13),
                                ],
                                alignment=ft.MainAxisAlignment.CENTER,
                            ),
                            border=ft.Border.all(1, "#333333"),
                            padding=10,
                            border_radius=10,
                            on_click=self.on_api_search_click,
                            ink=True,
                            tooltip="Поиск в SteamGridDB и RAWG",
                        ),
                        
                        ft.Container(height=20),
                        
                        # Кнопка Отмена внутри контейнера
                        ft.Container(
                            content=ft.Text("Отмена", color=TEXT_GREY, size=14),
                            alignment=ft.Alignment(0, 0),
                            on_click=close_dialog,
                            ink=True,
                        ),
                    ],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            shape=ft.RoundedRectangleBorder(radius=15),
        )

        # ИСПРАВЛЕНИЕ: В новых версиях Flet диалоги добавляются через overlay
        if self.upload_dialog not in self.page.overlay:
            self.page.overlay.append(self.upload_dialog)
        self.upload_dialog.open = True
        print(f"[DEBUG] Opening upload dialog for: {game.title}")
        self.page.update()
        print(f"[DEBUG] Dialog should be visible now")
    
    def on_file_picked(self, e: ft.FilePickerResultEvent):
        """Handle local file selection from FilePicker"""
        if e.files and len(e.files) > 0:
            file_path = e.files[0].path
            print(f"[DEBUG] Local file selected: {file_path}")
            if self.upload_target_game:
                self.page.run_task(self.upload_cover_from_file, self.upload_target_game, file_path)
        else:
            print("[DEBUG] File picker cancelled")

    async def upload_cover_from_file(self, game: GameModel, file_path: str):
        """Upload cover from local file"""
        self.loading_overlay.show("Загрузка обложки...")
        self.page.update()

        new_path = self.game_manager.cover_uploader.upload_from_file(game.uid, file_path)

        if new_path:
            game.icon_path = new_path
            self.game_manager._games[game.uid] = game
            await self.game_manager.save_library()

            # Инвалидируем кеш карточки чтобы она пересоздалась с новой обложкой
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True  # Знаем что файл есть
            self.update_game_grid(reset_page=False)
            
            self.show_snackbar("✅ Обложка загружена успешно!", bgcolor="#4CAF50")
        else:
            self.show_snackbar("❌ Ошибка загрузки обложки", bgcolor="#F44336")

        self.loading_overlay.hide()
        self.page.update()

    async def on_api_search_click(self, e):
        """Обработчик кнопки авто-поиска в диалоге"""
        try:
            # Low-level debug
            with open(get_app_data_dir() / "debug_api_click.txt", "a", encoding="utf-8") as f:
                f.write(f"API Search clicked for {self.upload_target_game.title}\n")
            
            backend_logger.info(f"UI: API Search clicked for {self.upload_target_game.title}")
            
            # Close dialog first
            if self.upload_dialog:
                self.upload_dialog.open = False
            
            self.show_snackbar("Запуск авто-поиска...")
            
            await self.refresh_cover(self.upload_target_game)
            
        except Exception as ex:
            import traceback
            err = traceback.format_exc()
            backend_logger.error(f"Error in on_api_search_click: {err}")
            with open(get_app_data_dir() / "debug_api_click.txt", "a", encoding="utf-8") as f:
                f.write(f"Error: {err}\n")

    async def upload_cover_from_url(self, game: GameModel, url: str):
        """Upload cover from URL"""
        self.loading_overlay.show("Скачивание изображения...")
        self.page.update()

        new_path = self.game_manager.cover_uploader.upload_from_url(game.uid, url)

        if new_path:
            game.icon_path = new_path
            self.game_manager._games[game.uid] = game
            await self.game_manager.save_library()

            # Инвалидируем кеш карточки
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True
            self.update_game_grid(reset_page=False)
            
            self.show_snackbar("✅ Обложка загружена успешно!", bgcolor="#4CAF50")
        else:
            self.show_snackbar("❌ Ошибка загрузки обложки", bgcolor="#F44336")

        self.loading_overlay.hide()
        self.page.update()

    async def refresh_cover(self, game: GameModel):
        """Force re-download cover from APIs"""
        self.loading_overlay.show("Поиск обложки...")
        self.page.update()
        await asyncio.sleep(0.05)  # Give UI time to render

        # Delete existing cache
        if game.icon_path:
            try:
                Path(game.icon_path).unlink()
            except:
                pass

        # Re-fetch using CoverAPIManager in background thread (non-blocking)
        new_path, source = await asyncio.to_thread(
            self.game_manager.cover_api_manager.get_cover,
            game.title,
            game.app_id,
            game.exe_path
        )

        self.loading_overlay.hide()

        if new_path:
            game.icon_path = new_path
            self.game_manager._games[game.uid] = game
            await self.game_manager.save_library()

            # Инвалидируем кеш карточки
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True
            self.update_game_grid(reset_page=False)

            # Show source in success message
            self.show_snackbar(f"✅ Обложка найдена! Источник: {source}", bgcolor="#4CAF50")
        else:
            self.page.update()
            self.show_snackbar("❌ Не удалось найти обложку", bgcolor="#F44336")

    # ========== End Cover Upload Methods ==========

    # ========== Collections Methods ==========

    def refresh_collections_sidebar(self):
        """Обновить список коллекций в сайдбаре"""
        collections = self.game_manager.get_collections()
        controls = []

        for col in collections:
            col_id = col["id"]
            col_name = col["name"]
            col_color = col.get("color", ACCENT_PURPLE)

            # Создаём кнопку коллекции с кнопками редактирования и удаления
            btn = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Container(
                            width=8,
                            height=8,
                            border_radius=4,
                            bgcolor=col_color,
                        ),
                        ft.Text(col_name, color=TEXT_GREY, size=13, expand=True),
                        ft.Text(
                            str(len(self.game_manager.get_games_by_collection(col_id))),
                            color=TEXT_GREY,
                            size=11,
                        ),
                        # Кнопка редактирования
                        ft.IconButton(
                            icon=ft.Icons.EDIT,
                            icon_size=14,
                            icon_color=TEXT_GREY,
                            tooltip="Редактировать",
                            on_click=lambda e, cid=col_id, cname=col_name, ccolor=col_color: self.show_edit_collection_dialog(cid, cname, ccolor),
                            style=ft.ButtonStyle(padding=0),
                        ),
                        # Кнопка удаления
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            icon_color="#F44336",
                            tooltip="Удалить",
                            on_click=lambda e, cid=col_id, cname=col_name: self.confirm_delete_collection(cid, cname),
                            style=ft.ButtonStyle(padding=0),
                        ),
                    ],
                    spacing=5,
                ),
                padding=ft.Padding(left=15, right=5, top=8, bottom=8),
                border_radius=10,
                on_click=lambda e, cid=col_id: self.on_collection_click(cid),
                ink=True,
                data=col_id,
            )
            # Добавляем в sidebar_buttons для управления активным состоянием
            self.sidebar_buttons[f"collection_{col_id}"] = btn
            controls.append(btn)

        self.collections_column.controls = controls
        if self.page:
            self.collections_column.update()

    def confirm_delete_collection(self, collection_id: str, collection_name: str):
        """Подтверждение удаления коллекции"""
        def on_confirm(e):
            self.game_manager.delete_collection(collection_id)
            self.page.run_task(self._save_and_refresh_collections)
            dialog.open = False
            # Если мы были в этой коллекции - переключаемся на "Все игры"
            if self.current_filter == f"collection_{collection_id}":
                self.on_filter_click("all")
            self.page.update()
            self.show_snackbar(f"Коллекция '{collection_name}' удалена", bgcolor="#FF9800")

        def on_cancel(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Удалить коллекцию?"),
            content=ft.Text(f"Коллекция '{collection_name}' будет удалена.\nИгры останутся в библиотеке."),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.TextButton("Удалить", on_click=on_confirm, style=ft.ButtonStyle(color="#F44336")),
            ],
        )
        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def on_collection_click(self, collection_id: str):
        """Обработчик клика по коллекции"""
        self.current_filter = f"collection_{collection_id}"

        # Обновляем активное состояние кнопок
        for name, button in self.sidebar_buttons.items():
            if isinstance(button, SidebarButton):
                button.set_active(False)
            elif isinstance(button, ft.Container) and name.startswith("collection_"):
                # Для контейнеров коллекций меняем фон
                if name == f"collection_{collection_id}":
                    button.bgcolor = "#33D500F9"
                else:
                    button.bgcolor = "transparent"

        self.collection_actions_panel.visible = True
        self.bg_container.content = self.games_container
        self.update_game_grid()

    def show_add_collection_dialog(self, e=None):
        """Показать диалог создания коллекции"""
        name_field = ft.TextField(
            label="Название коллекции",
            hint_text="Например: RPG, Инди, На потом...",
            autofocus=True,
            text_size=14,
        )

        color_options = ["#D500F9", "#00E5FF", "#FF4081", "#4CAF50", "#FF9800", "#2196F3", "#9C27B0", "#F44336"]
        selected_color = {"value": color_options[0]}

        def make_color_btn(color):
            return ft.Container(
                width=32,
                height=32,
                border_radius=16,
                bgcolor=color,
                border=ft.Border.all(3, "#FFFFFF" if color == selected_color["value"] else "transparent"),
                on_click=lambda e, c=color: select_color(c),
                animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
            )

        def select_color(color):
            selected_color["value"] = color
            # Обновляем визуал кнопок
            for i, c in enumerate(color_options):
                color_row.controls[i].border = ft.Border.all(3, "#FFFFFF" if c == color else "transparent")
            color_row.update()

        color_row = ft.Row(
            controls=[make_color_btn(c) for c in color_options],
            spacing=8,
        )

        def on_create(e):
            name = name_field.value.strip()
            if name:
                col = self.game_manager.add_collection(name, selected_color["value"])
                self.page.run_task(self._save_and_refresh_collections)
                dialog.open = False
                self.page.update()
                self.show_snackbar(f"Коллекция '{name}' создана!", bgcolor="#4CAF50")

        def on_cancel(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Новая коллекция", weight=ft.FontWeight.BOLD),
            content=ft.Container(
                width=350,
                content=ft.Column(
                    controls=[
                        name_field,
                        ft.Container(height=15),
                        ft.Text("Цвет:", size=13, color=TEXT_GREY),
                        ft.Container(height=5),
                        color_row,
                    ],
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.ElevatedButton("Создать", on_click=on_create, bgcolor=ACCENT_PURPLE, color="#FFFFFF"),
            ],
        )

        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def show_edit_collection_dialog(self, collection_id: str, current_name: str, current_color: str):
        """Показать диалог редактирования/удаления коллекции"""
        name_field = ft.TextField(
            label="Название коллекции",
            value=current_name,
            autofocus=True,
            text_size=14,
        )

        color_options = ["#D500F9", "#00E5FF", "#FF4081", "#4CAF50", "#FF9800", "#2196F3", "#9C27B0", "#F44336"]
        selected_color = {"value": current_color if current_color in color_options else color_options[0]}

        def make_color_btn(color):
            return ft.Container(
                width=32,
                height=32,
                border_radius=16,
                bgcolor=color,
                border=ft.Border.all(3, "#FFFFFF" if color == selected_color["value"] else "transparent"),
                on_click=lambda e, c=color: select_color(c),
            )

        def select_color(color):
            selected_color["value"] = color
            for i, c in enumerate(color_options):
                color_row.controls[i].border = ft.Border.all(3, "#FFFFFF" if c == color else "transparent")
            color_row.update()

        color_row = ft.Row(
            controls=[make_color_btn(c) for c in color_options],
            spacing=8,
        )

        def on_save(e):
            name = name_field.value.strip()
            if name:
                self.game_manager.update_collection(collection_id, name=name, color=selected_color["value"])
                self.page.run_task(self._save_and_refresh_collections)
                dialog.open = False
                self.page.update()
                self.show_snackbar(f"Коллекция обновлена!", bgcolor="#4CAF50")

        def on_cancel(e):
            dialog.open = False
            self.page.update()

        dialog = ft.AlertDialog(
            title=ft.Text("Редактировать коллекцию", weight=ft.FontWeight.BOLD),
            content=ft.Container(
                width=350,
                content=ft.Column(
                    controls=[
                        name_field,
                        ft.Container(height=15),
                        ft.Text("Цвет:", size=13, color=TEXT_GREY),
                        ft.Container(height=5),
                        color_row,
                    ],
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.ElevatedButton("Сохранить", on_click=on_save, bgcolor=ACCENT_PURPLE, color="#FFFFFF"),
            ],
        )

        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    async def _save_and_refresh_collections(self):
        """Сохранить библиотеку и обновить UI коллекций"""
        await self.game_manager.save_library()
        self.refresh_collections_sidebar()

    def _on_manage_collection_click(self, e):
        """Открывает мульти-селект диалог управления играми текущей коллекции."""
        if not self.current_filter.startswith("collection_"):
            return
        collection_id = self.current_filter.replace("collection_", "")
        self.show_manage_collection_games_dialog(collection_id)

    def show_game_properties_dialog(self, game: GameModel):
        """Диалог свойств игры. Сейчас один тоггл — "Запускать от админа".
        Steam-игры через протокол управляются Steam-клиентом, для них тоггл
        не имеет эффекта, поэтому отключаем его и поясняем."""
        is_steam = game.exe_path and game.exe_path.startswith("steam://")

        def on_admin_toggle(e):
            new_val = bool(e.control.value)
            game.run_as_admin = new_val
            self.game_manager._games[game.uid] = game
            # Sync save — изменение критично, не теряем при крэше
            self.game_manager.save_library_sync()
            # Инвалидируем карточку чтобы шестерёнка → щит обновился
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            self._render_visible_cards()
            self.show_snackbar(
                f"'{game.title}': запуск от админа {'ВКЛ' if new_val else 'ВЫКЛ'}",
                bgcolor="#4CAF50" if new_val else "#FF9800",
            )

        def on_close(e):
            dialog.open = False
            self.page.update()

        admin_switch = ft.Switch(
            value=bool(getattr(game, "run_as_admin", False)),
            active_color=ACCENT_PURPLE,
            on_change=on_admin_toggle,
            disabled=is_steam,
        )

        admin_row = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SHIELD, color="#FFD54F", size=22),
                    ft.Column(
                        controls=[
                            ft.Text("Запускать от имени администратора",
                                    size=14, color=TEXT_WHITE,
                                    weight=ft.FontWeight.W_500),
                            ft.Text(
                                "Подходит для игр, требующих UAC (Crash Bandicoot 4 и др.)."
                                if not is_steam
                                else "Для Steam-игр не применимо — Steam управляет процессом.",
                                size=11, color=TEXT_GREY,
                                max_lines=2,
                            ),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    admin_switch,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=15,
            border_radius=10,
            bgcolor="#1E1E1E",
        )

        # Усечь длинный заголовок
        short = game.title[:40] + "..." if len(game.title) > 40 else game.title

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SETTINGS, color=ACCENT_BLUE),
                    ft.Text(f"Свойства: {short}", weight=ft.FontWeight.BOLD),
                ],
                spacing=10,
            ),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    controls=[
                        ft.Text("Платформа: " + (game.platform or "—"),
                                size=12, color=TEXT_GREY),
                        ft.Container(height=12),
                        admin_row,
                    ],
                    spacing=0,
                    tight=True,
                ),
            ),
            actions=[
                ft.ElevatedButton("Закрыть", on_click=on_close,
                                  bgcolor=ACCENT_BLUE, color=TEXT_WHITE),
            ],
        )

        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def show_manage_collection_games_dialog(self, collection_id: str):
        """Мульти-селект диалог: показать ВСЕ игры с чекбоксами, отмеченные —
        в коллекции, не отмеченные — нет. На «Сохранить» применяется новое
        множество атомарно через GameManager.set_collection_for_games."""
        col = next((c for c in self.game_manager.get_collections()
                    if c["id"] == collection_id), None)
        if not col:
            self.show_snackbar("Коллекция не найдена", bgcolor="#F44336")
            return
        col_name = col.get("name", "?")
        col_color = col.get("color", ACCENT_PURPLE)

        # Снапшот всех игр + начальное состояние чекбоксов
        all_games = sorted(self.game_manager.get_all_games(),
                           key=lambda g: g.title.lower())
        if not all_games:
            self.show_snackbar("В библиотеке пока нет игр", bgcolor="#FF9800")
            return

        checked = {g.uid: (collection_id in (g.collections or [])) for g in all_games}
        search_state = {"query": ""}

        def make_row(game):
            thumb_src = game.icon_path if (game.icon_path and Path(game.icon_path).exists()) else None
            thumb = ft.Container(
                width=66, height=92,
                border_radius=6,
                bgcolor=None if thumb_src else CARD_BG,
                image=ft.DecorationImage(src=thumb_src, fit="cover") if thumb_src else None,
            )
            def on_check(e, uid=game.uid):
                checked[uid] = bool(e.control.value)
                counter_text.value = f"Выбрано: {sum(1 for v in checked.values() if v)} / {len(checked)}"
                counter_text.update()
            cb = ft.Checkbox(value=checked[game.uid], on_change=on_check,
                             active_color=col_color)
            return ft.Container(
                content=ft.Row(
                    controls=[
                        cb,
                        thumb,
                        ft.Text(game.title, size=16, color=TEXT_WHITE, expand=True,
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                                weight=ft.FontWeight.W_500),
                    ],
                    spacing=14,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.Padding(left=12, right=12, top=6, bottom=6),
                border_radius=8,
                bgcolor="#15FFFFFF",  # лёгкая подложка для разделения строк
                data=game,  # для фильтрации по поиску
            )

        all_rows = [make_row(g) for g in all_games]
        # ft.Column + scroll=AUTO внутри Container с явной height даёт
        # детерминированный layout. ListView в AlertDialog иногда
        # рендерится с пустотой сверху из-за intrinsic sizing —
        # независимо от tight/expand флагов выше.
        list_column = ft.Column(
            controls=list(all_rows),
            spacing=4,
            scroll=ft.ScrollMode.AUTO,
        )
        list_view = ft.Container(
            height=480,
            content=list_column,
            padding=ft.Padding(left=4, right=4, top=4, bottom=4),
        )

        def apply_search(e):
            q = (search_field.value or "").lower().strip()
            search_state["query"] = q
            if not q:
                list_column.controls = list(all_rows)
            else:
                list_column.controls = [r for r in all_rows
                                         if q in r.data.title.lower()]
            list_column.update()

        search_field = ft.TextField(
            hint_text="Поиск...",
            prefix_icon=ft.Icons.SEARCH,
            on_change=apply_search,
            height=38,
            content_padding=10,
            text_size=13,
            border_radius=8,
            bgcolor="#1E1E1E",
            border_color="#333333",
            focused_border_color=ACCENT_BLUE,
            expand=True,
        )

        counter_text = ft.Text(
            f"Выбрано: {sum(1 for v in checked.values() if v)} / {len(checked)}",
            size=12, color=TEXT_GREY,
        )

        def on_save(e):
            selected = {uid for uid, v in checked.items() if v}
            changed = self.game_manager.set_collection_for_games(collection_id, selected)
            dialog.open = False
            self.page.update()
            self.refresh_collections_sidebar()
            # Инвалидируем кеш карточек для игр чьи коллекции изменились
            # (затронуло "changed" игр, но дёшево очистить полностью)
            self._card_cache.clear()
            self.update_game_grid(reset_page=True)
            if changed:
                self.show_snackbar(
                    f"Коллекция '{col_name}': обновлено {changed} игр",
                    bgcolor="#4CAF50",
                )

        def on_cancel(e):
            dialog.open = False
            self.page.update()

        # Цвет-чип коллекции у заголовка
        color_chip = ft.Container(width=14, height=14, border_radius=7, bgcolor=col_color)

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                controls=[
                    color_chip,
                    ft.Text(f"«{col_name}» — выбор игр", weight=ft.FontWeight.BOLD),
                ],
                spacing=10,
            ),
            content=ft.Container(
                # Явная ширина+высота + alignment.top_left на Container и
                # MainAxisAlignment.START на Column гарантирует что content
                # прибит к верху. Без этого Flet AlertDialog где-то применяет
                # центрирование и список выглядит "съехавшим" вниз.
                width=760,
                height=580,
                alignment=ft.Alignment(-1, -1),  # top-left
                content=ft.Column(
                    controls=[
                        ft.Text("Отметьте игры, которые должны входить в коллекцию.",
                                size=13, color=TEXT_GREY),
                        ft.Container(height=10),
                        search_field,
                        ft.Container(height=10),
                        list_view,
                        ft.Container(height=8),
                        counter_text,
                    ],
                    spacing=0,
                    alignment=ft.MainAxisAlignment.START,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ),
            actions=[
                ft.TextButton("Отмена", on_click=on_cancel),
                ft.ElevatedButton("Сохранить", on_click=on_save,
                                  bgcolor=ACCENT_PURPLE, color="#FFFFFF"),
            ],
        )

        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    # ========== End Collections Methods ==========

    def change_theme(self, theme_id: str):
        if theme_id not in GRADIENT_THEMES:
            return
        
        self.current_theme = theme_id
        self.settings["theme"] = theme_id
        self.save_settings()
        
        theme_data = GRADIENT_THEMES[theme_id]
        
        self.bg_container.gradient = ft.LinearGradient(
            begin=ft.Alignment(-1, -1),
            end=ft.Alignment(1, 1),
            colors=theme_data["colors"],
        )
        
        self.sidebar.bgcolor = theme_data.get("sidebar", "#801A1A1A")
        self.sidebar.update()
        
        self.settings_view = self.build_settings_view()
        self.bg_container.content = self.settings_view
        self.bg_container.update()


def main(page: ft.Page):
    global TRAY_APP_INSTANCE
    app = CyberLauncher(page)
    TRAY_APP_INSTANCE = app


def _kill_orphan_flet_processes():
    """Убивает зомби flet.exe из предыдущих запусков — но ТОЛЬКО наши.

    Сценарий: python.exe умер аварийно (Ctrl+C, краш, taskkill), но Flutter
    Desktop окно (flet.exe) осталось висеть как зомби с открытым TCP. На
    следующем старте новый flet.exe может конфликтовать с зомби и тихо
    умирать → лог обрывается на 'Flet View found' без 'App session started'.

    КРИТИЧНО: фильтруем по пути flet.exe — без этого убивались чужие Flet-
    приложения юзера (DayPlanner и т.п.), которые тоже используют flet.exe."""
    if sys.platform != "win32":
        return

    # Где живёт НАШ flet.exe? Frozen-сборка → рядом с CyberLauncher.exe;
    # dev-режим (python main.py) → внутри venv.
    if getattr(sys, "frozen", False):
        our_prefix = os.path.dirname(os.path.abspath(sys.executable))
    else:
        try:
            import flet_desktop
            our_prefix = os.path.dirname(os.path.abspath(flet_desktop.__file__))
        except Exception:
            our_prefix = os.path.dirname(os.path.abspath(__file__))
    our_prefix = our_prefix.lower()

    try:
        kernel32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        if not snap or snap == INVALID_HANDLE_VALUE:
            return
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            PROCESS_TERMINATE = 0x0001
            while True:
                if entry.szExeFile.lower() == "flet.exe":
                    pid = entry.th32ProcessID
                    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if h:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = wintypes.DWORD(1024)
                            ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                            if ok and buf.value.lower().startswith(our_prefix):
                                hk = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                                if hk:
                                    kernel32.TerminateProcess(hk, 1)
                                    kernel32.CloseHandle(hk)
                        finally:
                            kernel32.CloseHandle(h)
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        pass


if __name__ == "__main__":
    # Очистка зомби flet.exe от предыдущих аварийных запусков
    _kill_orphan_flet_processes()

    # Проверяем, не запущено ли уже приложение
    if not acquire_single_instance_lock():
        # Приложение уже запущено - выходим
        sys.exit(0)

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Создаем и запускаем иконку в трее
    if HAS_TRAY:
        tray_icon = create_tray_icon()
        if tray_icon:
            tray_thread = threading.Thread(target=run_tray_icon, daemon=True)
            tray_thread.start()

    # atexit-страховка: если процесс завершается через Alt+F4 / kill / любой
    # путь без _async_full_exit, всё равно сохранить любые dirty изменения.
    def _atexit_emergency_save():
        try:
            app = TRAY_APP_INSTANCE
            if app is not None and getattr(app, "game_manager", None) is not None:
                app.game_manager.emergency_sync_save()
        except Exception as e:
            try:
                backend_logger.error(f"atexit save error: {e}")
            except Exception:
                pass
    atexit.register(_atexit_emergency_save)

    try:
        ft.run(main, assets_dir=base_dir)
    finally:
        # Best-effort cleanup на случай если ft.run завершился аварийно
        # (нормальный выход идёт через _async_full_exit и не доходит сюда).
        try:
            stop_tray_icon()
        except Exception:
            pass
        try:
            release_single_instance_lock()
        except Exception:
            pass
        backend_logger.info("App process exiting (finally block)")
