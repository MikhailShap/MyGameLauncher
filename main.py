import flet as ft
import asyncio
import sys
import json
import re
import logging
import threading
import webbrowser
from pathlib import Path
from tkinter import Tk, filedialog

# Импорт backend
from game_manager import GameManager, GameModel, Platform, Category, logger as backend_logger

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
    
    def __init__(self, game: GameModel, on_click=None, on_favorite=None, on_upload=None, show_size=False, enable_animations=False):
        super().__init__()
        self.game = game
        self._on_click = on_click
        self._on_favorite = on_favorite
        self._on_upload = on_upload
        self._enable_animations = enable_animations
        self._is_hovered = False  # Track hover state to avoid redundant updates
        
        self.border_radius = 15
        self.padding = 0
        self.bgcolor = CARD_BG
        self.expand = True
        self.clip_behavior = ft.ClipBehavior.HARD_EDGE
        
        # Only set up animations if enabled
        if enable_animations:
            self.animate_scale = ft.Animation(150, ft.AnimationCurve.EASE_OUT)
            self.animate = ft.Animation(150, ft.AnimationCurve.EASE_OUT)
            self.on_hover = self.on_card_hover
        
        self.shadow = None
        self.scale = 1.0
        self.border = ft.Border.all(1, "#333333")
        
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

            # 6. Кнопка загрузки обложки - ПРОСТОЙ Container с on_click
            ft.Container(
                content=ft.Icon(
                    ft.Icons.IMAGE_SEARCH,
                    color="#FFFFFF",
                    size=18,
                ),
                width=36,
                height=36,
                border_radius=18,
                bgcolor="#8000E5FF",
                alignment=ft.Alignment(0, 0),
                right=8,
                top=52,
                on_click=self.on_upload_click,
                ink=True,
            ),
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
        # Skip if animations disabled
        if not self._enable_animations:
            return
        
        is_hovering = e.data == True or e.data == "true"
        
        # Skip if state hasn't changed
        if is_hovering == self._is_hovered:
            return
        
        self._is_hovered = is_hovering
        
        if is_hovering:
            self.scale = 1.03
            self.border = ft.Border.all(2, ACCENT_BLUE)
        else:
            self.scale = 1.0
            self.border = ft.Border.all(1, "#333333")
        
        self.update()
    
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
    
    SETTINGS_FILE = "data/settings.json"
    
    def __init__(self, page: ft.Page):
        self.page = page

        # Load settings first to get API keys
        self.settings = self.load_settings()
        self.current_theme = self.settings.get("theme", "dark")

        # Extract API keys from settings
        api_keys = self.settings.get("api_keys", {})
        sgdb_key = api_keys.get("steamgriddb") or None
        rawg_key = api_keys.get("rawg") or None

        # Initialize GameManager with API keys
        # Default game paths
        default_paths = [r"C:\Games", r"D:\Games", r"D:\Install Games"]
        game_paths = self.settings.get("game_paths", default_paths)

        # Initialize GameManager with API keys and paths
        self.game_manager = GameManager(
            sgdb_key=sgdb_key,
            rawg_key=rawg_key,
            game_paths=game_paths
        )

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

    def setup_page(self):
        self.page.title = "CyberLauncher v1.0"
        self.page.bgcolor = BG_COLOR
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK
        
        self.page.window.width = 1200
        self.page.window.height = 750
        self.page.window.min_width = 900
        self.page.window.min_height = 600
        self.page.window.title_bar_hidden = True
        self.page.window.title_bar_buttons_hidden = True
        self.page.window.icon = "icon.ico"
        
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
                    ft.IconButton(ft.Icons.REMOVE, icon_size=16, on_click=lambda _: self.window_action("min"), icon_color=TEXT_GREY, tooltip="Свернуть"),
                    ft.IconButton(ft.Icons.CROP_SQUARE_OUTLINED, icon_size=16, on_click=lambda _: self.window_action("max"), icon_color=TEXT_GREY, tooltip="Развернуть"),
                    ft.IconButton(ft.Icons.CLOSE, icon_size=16, on_click=lambda _: self.window_action("close"), icon_color=TEXT_GREY, tooltip="Закрыть"),
                    ft.Container(width=5)
                ],
                spacing=0
            )
        )
        
        # Sidebar buttons
        self.sidebar_buttons["all"] = SidebarButton(ft.Icons.GRID_VIEW_ROUNDED, "Все игры", is_active=True, on_click=self.on_filter_click, data="all")
        self.sidebar_buttons["favorites"] = SidebarButton(ft.Icons.FAVORITE_BORDER, "Избранное", on_click=self.on_filter_click, data="favorites")
        self.sidebar_buttons["steam"] = SidebarButton(ft.Icons.VIDEOGAME_ASSET_OUTLINED, "Steam", on_click=self.on_filter_click, data="steam")
        self.sidebar_buttons["epic"] = SidebarButton(ft.Icons.TOKEN_OUTLINED, "Epic Games", on_click=self.on_filter_click, data="epic")
        self.sidebar_buttons["system"] = SidebarButton(ft.Icons.COMPUTER_OUTLINED, "Системные", on_click=self.on_filter_click, data="system")
        self.sidebar_buttons["settings"] = SidebarButton(ft.Icons.SETTINGS_OUTLINED, "Настройки", on_click=self.on_filter_click, data="settings")
        
        self.games_count_text = ft.Text("0 игр", size=11, color=TEXT_GREY)
        
        theme_data = GRADIENT_THEMES.get(self.current_theme, GRADIENT_THEMES["dark"])
        sidebar_color = theme_data.get("sidebar", "#801A1A1A")
        
        self.sidebar = ft.Container(
            width=240,
            bgcolor=sidebar_color,
            blur=ft.Blur(10, 10, ft.BlurTileMode.MIRROR),
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
                    ft.Container(height=5),
                    ft.Text("ПЛАТФОРМЫ", color="#80FFFFFF", size=11, weight=ft.FontWeight.BOLD),
                    ft.Container(height=5),
                    self.sidebar_buttons["steam"],
                    self.sidebar_buttons["epic"],
                    self.sidebar_buttons["system"],
                    ft.Container(height=10),
                    ft.Divider(color="#1AFFFFFF"),
                    ft.Container(height=5),
                    self.sidebar_buttons["settings"],
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
        
        self.sort_panel = ft.Container(
            height=50,
            content=ft.Row(
                controls=[
                    ft.Container(expand=True),
                    ft.Icon(ft.Icons.SORT, color=TEXT_GREY, size=16),
                    ft.Text("Сортировка:", size=12, color=TEXT_GREY),
                    self.sort_button,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=20, right=20, top=8, bottom=5),
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
        
        self.games_container = ft.Column(
            controls=[self.sort_panel, self.game_grid],
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
        
        layout = ft.Column(
            spacing=0,
            expand=True,
            controls=[
                title_bar,
                ft.Row(expand=True, spacing=0, controls=[self.sidebar, self.content_stack])
            ]
        )
        
        self.page.add(layout)
        self.page.run_task(self.load_library)
    
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
                    ft.Text("О приложении", size=18, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                    ft.Container(height=15),
                    ft.Row(controls=[
                        ft.Icon(ft.Icons.INFO_OUTLINE, color=ACCENT_BLUE, size=20),
                        ft.Text("CyberLauncher", size=16, color=TEXT_WHITE, weight=ft.FontWeight.BOLD),
                        ft.Text("v1.0", size=14, color=TEXT_GREY),
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
    
    def window_action(self, action: str):
        if action == "close":
            sys.exit(0)
        elif action == "min":
            self.page.window.minimized = True
            self.page.update()
        elif action == "max":
            self.is_maximized = not self.is_maximized
            self.page.window.maximized = self.is_maximized
            self.page.update()
    
    async def on_refresh_click(self, e):
        """Обработчик нажатия кнопки обновления"""
        try:
            # Low-level debug write
            with open("debug_click.txt", "a", encoding="utf-8") as f:
                f.write("Button clicked!\n")
                
            backend_logger.info("UI: Update Library button clicked")
            
            self.page.snack_bar = ft.SnackBar(content=ft.Text("Запуск сканирования..."))
            self.page.snack_bar.open = True
            self.page.update()
            
            # Since we are async, we can call refresh_library directly (await it)
            # or use run_task if we want it to be detached? 
            # calling directly is safer here to keep context
            await self.refresh_library()
        except Exception as ex:
            import traceback
            err = traceback.format_exc()
            backend_logger.error(f"Error in on_refresh_click: {err}")
            with open("debug_click.txt", "a", encoding="utf-8") as f:
                f.write(f"Error: {err}\n")

    async def load_library(self):
        backend_logger.info("UI: load_library started")
        self.loading_overlay.show("Загрузка библиотеки...")
        
        await self.game_manager.load_library()
        
        if self.game_manager.games_count == 0:
            backend_logger.info("UI: Library empty, starting initial scan")
            self.loading_overlay.show("Первый запуск. Сканирование игр...")
            self.game_manager.set_progress_callback(self.on_scan_progress)
            await self.game_manager.scan_all_games()
        
        self.loading_overlay.hide()
        self.update_game_grid()
    
    async def refresh_library(self):
        backend_logger.info("UI: refresh_library async task started")
        self.loading_overlay.show("Сканирование игр...")
        self.page.update()  # ВАЖНО: показать оверлей СРАЗУ
        await asyncio.sleep(0.1)  # Даём UI время отрендериться
        self.game_manager.set_progress_callback(self.on_scan_progress)
        await self.game_manager.scan_all_games()
        self.loading_overlay.hide()
        self.page.update()  # Скрыть оверлей
        self.update_game_grid()
    
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
            else:
                games = self.game_manager.get_all_games()
            
            self._all_games_list = list(games)
            
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
    
    def on_filter_click(self, filter_name: str):
        """Оптимизированная обработка переключения вкладок"""
        if filter_name == self.current_filter:
            return
        
        # Batch update all buttons without individual updates
        for name, button in self.sidebar_buttons.items():
            button.set_active(name == filter_name)
        
        self.current_filter = filter_name
        
        if filter_name == "settings":
            # Switch content and update once
            self.settings_view = self.build_settings_view()
            self.bg_container.content = self.settings_view
            self.page.update()  # Single update for everything
        else:
            self.bg_container.content = self.games_container
            self.update_game_grid()  # This already calls page.update()
    
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
        success = await self.game_manager.launch_game(game.uid)
        
        if success:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(f"Запуск: {game.title}"),
                bgcolor="#333333",
            )
            self.page.snack_bar.open = True
        else:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(f"Ошибка запуска: {game.title}"),
                bgcolor="#8B0000",
            )
            self.page.snack_bar.open = True
        
        self.page.update()
    
    def on_favorite_click(self, game: GameModel):
        self.page.run_task(self.toggle_favorite, game)
    
    async def toggle_favorite(self, game: GameModel):
        await self.game_manager.toggle_favorite(game.uid)
        # Инвалидируем кеш карточки чтобы она пересоздалась с новым состоянием
        if game.uid in self._card_cache:
            del self._card_cache[game.uid]
        # ИСПРАВЛЕНИЕ: Не сбрасываем страницу при изменении избранного
        self.update_game_grid(reset_page=False)


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

            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Обложка загружена успешно!"),
                bgcolor="#4CAF50",
            )
            self.page.snack_bar.open = True
            # Инвалидируем кеш карточки чтобы она пересоздалась с новой обложкой
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True  # Знаем что файл есть
            self.update_game_grid(reset_page=False)
        else:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Ошибка загрузки обложки"),
                bgcolor="#F44336",
            )
            self.page.snack_bar.open = True

        self.loading_overlay.hide()
        self.page.update()

    async def on_api_search_click(self, e):
        """Обработчик кнопки авто-поиска в диалоге"""
        try:
            # Low-level debug
            with open("debug_api_click.txt", "a", encoding="utf-8") as f:
                f.write(f"API Search clicked for {self.upload_target_game.title}\n")
            
            backend_logger.info(f"UI: API Search clicked for {self.upload_target_game.title}")
            
            # Close dialog first
            if self.upload_dialog:
                self.upload_dialog.open = False
            
            self.page.snack_bar = ft.SnackBar(content=ft.Text("Запуск авто-поиска..."))
            self.page.snack_bar.open = True
            self.page.update()
            
            await self.refresh_cover(self.upload_target_game)
            
        except Exception as ex:
            import traceback
            err = traceback.format_exc()
            backend_logger.error(f"Error in on_api_search_click: {err}")
            with open("debug_api_click.txt", "a", encoding="utf-8") as f:
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

            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Обложка загружена успешно!"),
                bgcolor="#4CAF50",
            )
            self.page.snack_bar.open = True
            # Инвалидируем кеш карточки
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True
            self.update_game_grid(reset_page=False)
        else:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Ошибка загрузки обложки"),
                bgcolor="#F44336",
            )
            self.page.snack_bar.open = True

        self.loading_overlay.hide()
        self.page.update()

    async def refresh_cover(self, game: GameModel):
        """Force re-download cover from APIs"""
        self.loading_overlay.show("Обновление обложки...")
        self.page.update()

        # Delete existing cache
        if game.icon_path:
            try:
                Path(game.icon_path).unlink()
            except:
                pass

        # Re-fetch using CoverAPIManager (now returns path, source)
        new_path, source = self.game_manager.cover_api_manager.get_cover(
            game.title,
            app_id=game.app_id,
            exe_path=game.exe_path
        )

        if new_path:
            game.icon_path = new_path
            self.game_manager._games[game.uid] = game
            await self.game_manager.save_library()

            # Show source in success message
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(f"Обложка обновлена! (Источник: {source})"),
                bgcolor="#4CAF50",
            )
            self.page.snack_bar.open = True
            # Инвалидируем кеш карточки
            if game.uid in self._card_cache:
                del self._card_cache[game.uid]
            # Инвалидируем кеш существования иконки
            if new_path in GameCard._icon_exists_cache:
                del GameCard._icon_exists_cache[new_path]
            GameCard._icon_exists_cache[new_path] = True
            self.update_game_grid(reset_page=False)
        else:
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text("Не удалось найти обложку"),
                bgcolor="#F44336",
            )
            self.page.snack_bar.open = True

        self.loading_overlay.hide()
        self.page.update()

    # ========== End Cover Upload Methods ==========

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
    app = CyberLauncher(page)


if __name__ == "__main__":
    ft.run(main)