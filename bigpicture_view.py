"""
BigPictureView — полноэкранный режим в стиле PlayStation для геймпада.

Layout:
  ┌─────────────────────────────────────────────────────────────────┐
  │  [Все] Избранное Steam <user collections>          ← top tabs   │
  ├─────────────────────────────────────────────────────────────────┤
  │  ┌────────┐                                                     │
  │  │        │  TITLE TITLE TITLE                                  │
  │  │  COVER │  Платформа · Время в игре · Последний запуск        │
  │  │ 320×480│                                                     │
  │  │        │  [A] Запустить    [Y] ★ Избранное                   │
  │  └────────┘                                                     │
  ├─────────────────────────────────────────────────────────────────┤
  │  ←  [card] [card] [▣ FOCUSED ▣] [card] [card]  →   carousel     │
  ├─────────────────────────────────────────────────────────────────┤
  │  [A]Запустить [B]Выход [Y]★ [X]Меню [Start]Настройки [Guide]Tog │
  └─────────────────────────────────────────────────────────────────┘

Навигация:
  D-pad LEFT/RIGHT — выбор игры в carousel
  D-pad UP/DOWN    — переключение между секциями (tabs / carousel)
  L1 / R1          — переключение коллекций (быстро, из любой секции)
  A                — запустить выбранное / активировать
  B                — выход или назад
  Y                — toggle favorite текущей игры
  X                — открыть список коллекций для текущей игры
  START            — открыть панель настроек/сканирования
"""
import flet as ft
import os
from typing import Callable, Optional, List
from enum import Enum

from game_manager import GameModel, Platform, Category


BIGPICTURE_BG = "#080814"
BIGPICTURE_GRADIENT = ["#080814", "#0d1428", "#1a1f3a"]
ACCENT = "#00E5FF"
ACCENT_GOLD = "#FFD700"
ACCENT_GREEN = "#00E676"
TEXT_WHITE = "#FFFFFF"
TEXT_DIM = "#AAAAAA"
CARD_BG = "#101828"
HERO_BG = "#0c1224CC"


class _Section(Enum):
    TABS = "tabs"
    CAROUSEL = "carousel"


def _format_play_time(minutes: int) -> str:
    if not minutes:
        return "ещё не играл"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours} ч {rest:02d} мин" if rest else f"{hours} ч"


def _format_last_played(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "никогда"
    return iso_str.split("T")[0]


class BigPictureView:
    """PS-стилизованный экран. Создаёт UI один раз, обновляет фокус через update()."""

    CAROUSEL_VISIBLE = 9       # кол-во видимых карточек (4 слева + фокус + 4 справа)
    CARD_WIDTH = 140
    CARD_HEIGHT = 200
    CARD_FOCUS_SCALE = 1.18

    def __init__(
        self,
        page: ft.Page,
        game_manager,
        settings: dict,
        on_exit: Callable[[], None],
        on_launch: Callable[[GameModel], None],
        on_request_scan: Optional[Callable[[], None]] = None,
        on_change_theme: Optional[Callable[[str], None]] = None,
        on_save_settings: Optional[Callable[[], None]] = None,
        on_pick_custom_bg: Optional[Callable[[GameModel], None]] = None,
        gamepad_manager=None,
    ):
        self.page = page
        self.gm = game_manager
        self.settings = settings
        self._on_exit = on_exit
        self._on_launch = on_launch
        self._on_request_scan = on_request_scan
        self._on_change_theme = on_change_theme
        self._on_save_settings = on_save_settings
        self._on_pick_custom_bg = on_pick_custom_bg
        self._gp = gamepad_manager  # для rumble

        # State
        self._collections: List[dict] = []  # включая псевдо: All / Recent / Favorites / Steam
        self._current_collection_idx: int = 0
        self._games: List[GameModel] = []
        self._game_idx: int = 0
        self._section: _Section = _Section.CAROUSEL

        self._settings_open: bool = False
        self._collections_overlay_open: bool = False
        self._collections_overlay_idx: int = 0

        # UI references
        self._tabs_row: Optional[ft.Row] = None
        # Hero — Container с DecorationImage, не ft.Image: иначе при src=None
        # (пустая коллекция) Flet валится с 'Image must have "src" specified'.
        self._hero_card: Optional[ft.Container] = None
        self._hero_title: Optional[ft.Text] = None
        self._hero_meta: Optional[ft.Text] = None
        self._hero_counter: Optional[ft.Text] = None
        self._hero_favorite_icon: Optional[ft.Icon] = None
        self._carousel_row: Optional[ft.Row] = None
        self._hint_bar: Optional[ft.Container] = None
        self._settings_panel: Optional[ft.Container] = None
        self._collections_panel: Optional[ft.Container] = None
        self._root: Optional[ft.Container] = None

        # Background art (фон во весь экран — landscape арт текущей игры).
        # Используем Container с image=ft.DecorationImage: ft.Image даже с
        # expand=True не растягивается во весь экран (рендерится на natural-size).
        self._bg_art: Optional[ft.Container] = None

        self._refresh_collections()
        self._refresh_games()

    # ----- Public API -----

    def build(self) -> ft.Container:
        """Создаёт корневой контейнер. Должен вызываться один раз перед toggle."""
        self._tabs_row = ft.Row(controls=[], spacing=12, scroll=ft.ScrollMode.HIDDEN)
        self._build_tabs()

        initial_hero_cover = self._safe_icon_path(self._current_game())
        self._hero_card = ft.Container(
            width=320, height=480,
            border_radius=20,
            bgcolor=CARD_BG if not initial_hero_cover else None,
            image=ft.DecorationImage(src=initial_hero_cover, fit="cover") if initial_hero_cover else None,
        )
        self._hero_title = ft.Text("", size=44, weight=ft.FontWeight.BOLD, color=TEXT_WHITE)
        self._hero_meta = ft.Text("", size=18, color=TEXT_DIM)
        self._hero_counter = ft.Text("", size=14, color=ACCENT, weight=ft.FontWeight.BOLD)
        self._hero_favorite_icon = ft.Icon(ft.Icons.STAR_BORDER, size=28, color=TEXT_DIM)

        hero_buttons = ft.Row(
            spacing=20,
            controls=[
                self._hint_chip("Ⓐ", "Запустить", ACCENT_GREEN),
                self._hint_chip("Ⓨ", "Избранное", ACCENT_GOLD),
                self._hint_chip("Ⓧ", "Коллекции", ACCENT),
            ],
        )

        hero_right = ft.Column(
            spacing=18,
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                self._hero_counter,
                self._hero_title,
                self._hero_meta,
                ft.Row(spacing=8, controls=[self._hero_favorite_icon, ft.Text("в избранном", size=16, color=TEXT_DIM)]),
                ft.Container(height=24),
                hero_buttons,
            ],
        )

        hero_section = ft.Container(
            padding=ft.Padding(40, 30, 40, 20),
            content=ft.Row(
                spacing=40,
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[self._hero_card, hero_right],
            ),
        )

        self._carousel_row = ft.Row(
            controls=[],
            spacing=14,
            alignment=ft.MainAxisAlignment.CENTER,
        )
        self._build_carousel()

        carousel_section = ft.Container(
            padding=ft.Padding(40, 10, 40, 10),
            content=self._carousel_row,
        )

        tabs_section = ft.Container(
            padding=ft.Padding(40, 24, 40, 12),
            content=self._tabs_row,
        )

        self._hint_bar = ft.Container(
            bgcolor="#0008",
            padding=ft.Padding(40, 14, 40, 14),
            content=ft.Row(
                spacing=24,
                alignment=ft.MainAxisAlignment.CENTER,
                controls=self._build_hint_controls(),
            ),
        )

        # Overlay panels (initially hidden via visible=False)
        self._settings_panel = self._build_settings_panel()
        self._collections_panel = self._build_collections_panel()

        # Background art layer — фоном на весь экран лежит обложка/скриншот.
        # Container с image=DecorationImage растягивает картинку на весь
        # contained box. В Stack используем абсолютные left/top/right/bottom=0
        # вместо expand=True — Flet иначе на первом рендере даёт child'у
        # loose constraints, и фон занимает только часть экрана до relayout.
        initial_bg = self._initial_bg_url(self._current_game())
        self._bg_art = ft.Container(
            left=0, top=0, right=0, bottom=0,
            image=ft.DecorationImage(src=initial_bg, fit="cover") if initial_bg else None,
            opacity=0.55 if initial_bg else 0.0,
            animate_opacity=ft.Animation(400, ft.AnimationCurve.EASE_OUT),
        )
        # Тёмная вуаль для читаемости UI поверх ярких изображений
        bg_veil = ft.Container(
            left=0, top=0, right=0, bottom=0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(0, -1),
                end=ft.Alignment(0, 1),
                colors=["#000000A0", "#00000060", "#000000B0"],
            ),
        )

        main_column = ft.Column(
            expand=True,
            spacing=0,
            controls=[
                tabs_section,
                hero_section,
                ft.Container(expand=True, content=carousel_section),
                self._hint_bar,
            ],
        )

        self._root = ft.Container(
            expand=True,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=BIGPICTURE_GRADIENT,
            ),
            content=ft.Stack(
                expand=True,
                controls=[
                    self._bg_art,            # 1. background art (низший слой)
                    bg_veil,                  # 2. тёмная вуаль для читаемости
                    main_column,              # 3. основной UI
                    self._settings_panel,     # 4. settings overlay
                    self._collections_panel,  # 5. collections overlay
                ],
            ),
        )

        self._refresh_hero()
        # Инициируем загрузку фона для первой игры
        self._schedule_bg_update()
        return self._root

    # ----- Gamepad input dispatch (вызывается из CyberLauncher) -----

    def handle_dpad(self, name: str, pressed: bool):
        """name ∈ DPAD_UP/DOWN/LEFT/RIGHT. Реагируем только на pressed=True."""
        if not pressed:
            return
        if self._collections_overlay_open:
            if name == "DPAD_UP":
                self._collections_overlay_move(-1)
            elif name == "DPAD_DOWN":
                self._collections_overlay_move(1)
            return
        if self._settings_open:
            return  # настройки управляются через A/B/LB/RB

        if name == "DPAD_LEFT":
            if self._section == _Section.CAROUSEL:
                self._move_game(-1)
            else:
                self._move_collection(-1)
        elif name == "DPAD_RIGHT":
            if self._section == _Section.CAROUSEL:
                self._move_game(1)
            else:
                self._move_collection(1)
        elif name == "DPAD_UP":
            if self._section == _Section.CAROUSEL:
                self._section = _Section.TABS
                self._refresh_tabs()
                self._refresh_carousel_focus()
        elif name == "DPAD_DOWN":
            if self._section == _Section.TABS:
                self._section = _Section.CAROUSEL
                self._refresh_tabs()
                self._refresh_carousel_focus()

    def handle_button(self, name: str, pressed: bool):
        """name ∈ A/B/X/Y/LB/RB/BACK/START/LSTICK/RSTICK. Реагируем на pressed=True."""
        if not pressed:
            return

        if self._collections_overlay_open:
            if name == "A":
                self._collections_overlay_toggle_current()
            elif name == "B":
                self._close_collections_overlay()
            return

        if self._settings_open:
            if name == "B" or name == "START":
                self._close_settings()
            elif name == "A":
                self._on_settings_action_scan()
            elif name == "RB":
                self._cycle_theme(1)
            elif name == "LB":
                self._cycle_theme(-1)
            elif name == "Y":
                self._toggle_rumble_setting()
            elif name == "X":
                self._open_pick_custom_bg()
            return

        if name == "A":
            if self._section == _Section.CAROUSEL:
                game = self._current_game()
                if game:
                    self._rumble_launch()  # сильная вибрация при запуске
                    self._on_launch(game)
            elif self._section == _Section.TABS:
                self._section = _Section.CAROUSEL
                self._refresh_tabs()
                self._refresh_carousel_focus()
        elif name == "B":
            self._on_exit()
        elif name == "Y":
            self._toggle_favorite()
        elif name == "X":
            self._open_collections_overlay()
        elif name == "LB":
            self._move_collection(-1)
        elif name == "RB":
            self._move_collection(1)
        elif name == "START":
            self._open_settings()

    def handle_axis(self, name: str, value: float):
        # Стик переходит в discrete с порогом 0.7 (анти-дребезг)
        if not hasattr(self, "_axis_state"):
            self._axis_state = {}
        prev = self._axis_state.get(name, 0)
        threshold = 0.7
        if value > threshold and prev != 1:
            self._axis_state[name] = 1
            if name == "LX":
                self.handle_dpad("DPAD_RIGHT", True)
            elif name == "LY":
                self.handle_dpad("DPAD_DOWN", True)
        elif value < -threshold and prev != -1:
            self._axis_state[name] = -1
            if name == "LX":
                self.handle_dpad("DPAD_LEFT", True)
            elif name == "LY":
                self.handle_dpad("DPAD_UP", True)
        elif abs(value) < 0.3 and prev != 0:
            self._axis_state[name] = 0

    def refresh_after_external_change(self):
        """Вызывается извне когда библиотека изменилась (после scan и т.п.)."""
        self._refresh_collections()
        self._refresh_games()
        self._build_tabs()
        self._build_carousel()
        self._refresh_hero()
        self._safe_update()

    # ----- Internal: collections / games refresh -----

    def _refresh_collections(self):
        """Собирает виртуальные + пользовательские коллекции.

        При первой инициализации выбирает "Недавние" если там есть игры,
        иначе "Все" — чтобы юзер не открыл BigPicture с пустым экраном."""
        virtual = [
            {"id": "_recent", "name": "Недавние", "color": "#FF9800", "_virtual": True},
            {"id": "_all", "name": "Все", "color": ACCENT, "_virtual": True},
            {"id": "_favorites", "name": "Избранное", "color": ACCENT_GOLD, "_virtual": True},
            {"id": "_steam", "name": "Steam", "color": "#1B2838", "_virtual": True},
        ]
        user = self.gm.get_collections() if hasattr(self.gm, "get_collections") else []
        self._collections = virtual + list(user)

        # Smart default: "Недавние" если там есть игры, иначе "Все"
        if not getattr(self, "_initial_collection_set", False):
            self._initial_collection_set = True
            all_games = list(self.gm.get_all_games()) if hasattr(self.gm, "get_all_games") else []
            has_recent = any(g.last_played for g in all_games)
            if not has_recent:
                for i, c in enumerate(self._collections):
                    if c["id"] == "_all":
                        self._current_collection_idx = i
                        break

        if self._current_collection_idx >= len(self._collections):
            self._current_collection_idx = 0

    def _refresh_games(self):
        if not self._collections:
            self._games = []
            return
        col = self._collections[self._current_collection_idx]
        cid = col["id"]
        if cid == "_recent":
            # Недавние — игры с last_played, отсортированные по последнему запуску
            all_games = list(self.gm.get_all_games()) if hasattr(self.gm, "get_all_games") else []
            games = [g for g in all_games if g.last_played]
            games.sort(key=lambda g: g.last_played or "", reverse=True)
            # Ограничим топ-30 (как обычно делают консоли в "Recent")
            games = games[:30]
            self._games = games
            if self._game_idx >= len(self._games):
                self._game_idx = max(0, len(self._games) - 1)
            return
        if cid == "_all":
            games = list(self.gm.get_all_games()) if hasattr(self.gm, "get_all_games") else []
        elif cid == "_favorites":
            games = [g for g in self.gm.get_all_games() if g.is_favorite]
        elif cid == "_steam":
            games = [g for g in self.gm.get_all_games() if g.platform == Platform.STEAM.value]
        else:
            games = self.gm.get_games_by_collection(cid)
        # Сортировка: избранные сверху, потом по последнему запуску, потом по названию
        games.sort(key=lambda g: (
            not g.is_favorite,
            -(g.play_time or 0),
            g.title.lower(),
        ))
        self._games = games
        if self._game_idx >= len(self._games):
            self._game_idx = max(0, len(self._games) - 1)

    def _current_game(self) -> Optional[GameModel]:
        if 0 <= self._game_idx < len(self._games):
            return self._games[self._game_idx]
        return None

    # ----- Internal: navigation -----

    def _move_game(self, delta: int):
        if not self._games:
            return
        self._game_idx = (self._game_idx + delta) % len(self._games)
        self._rumble_tick()  # лёгкая вибрация при смене карточки
        self._build_carousel()
        self._refresh_hero()
        self._schedule_bg_update()
        self._safe_update()

    def _move_collection(self, delta: int):
        if not self._collections:
            return
        self._current_collection_idx = (self._current_collection_idx + delta) % len(self._collections)
        self._game_idx = 0
        self._rumble_collection()  # средняя вибрация при смене коллекции
        self._refresh_games()
        self._build_tabs()
        self._build_carousel()
        self._refresh_hero()
        self._schedule_bg_update()
        self._safe_update()

    # ----- Rumble helpers -----

    def _rumble_enabled(self) -> bool:
        """Проверяет настройку gamepad_rumble. По умолчанию вибрация ВКЛ."""
        return bool(self.settings.get("gamepad_rumble", True)) and self._gp is not None

    def _rumble_tick(self):
        """Очень короткая вибрация при перелистывании карточек."""
        if self._rumble_enabled():
            self._gp.rumble(low_frequency=0.0, high_frequency=0.25, duration_ms=40)

    def _rumble_collection(self):
        """Средняя — при смене коллекции (более значимое действие)."""
        if self._rumble_enabled():
            self._gp.rumble(low_frequency=0.15, high_frequency=0.35, duration_ms=80)

    def _rumble_launch(self):
        """Сильная — при запуске игры."""
        if self._rumble_enabled():
            self._gp.rumble(low_frequency=0.6, high_frequency=0.7, duration_ms=400)

    # ----- Internal: rendering -----

    def _build_tabs(self):
        if self._tabs_row is None:
            return
        chips = []
        for idx, col in enumerate(self._collections):
            is_active = idx == self._current_collection_idx
            is_focused = is_active and self._section == _Section.TABS
            border = ft.Border.all(3, ACCENT) if is_focused else None
            bgcolor = col.get("color", ACCENT) if is_active else "#1a233e"
            text_color = TEXT_WHITE if is_active else TEXT_DIM
            chips.append(ft.Container(
                padding=ft.Padding(20, 10, 20, 10),
                bgcolor=bgcolor,
                border=border,
                border_radius=24,
                content=ft.Text(col["name"], size=20, weight=ft.FontWeight.W_600, color=text_color),
            ))
        self._tabs_row.controls = chips

    def _build_carousel(self):
        if self._carousel_row is None:
            return
        if not self._games:
            self._carousel_row.controls = [
                ft.Container(
                    width=300, height=self.CARD_HEIGHT,
                    alignment=ft.Alignment(0, 0),
                    bgcolor=CARD_BG, border_radius=12,
                    content=ft.Text("Нет игр в этой коллекции", size=18, color=TEXT_DIM),
                )
            ]
            return

        n = len(self._games)
        focus_in_carousel = self._section == _Section.CAROUSEL
        cards = []

        # Если игр меньше чем видимых слотов — показываем каждую игру ровно один
        # раз без циклического дублирования. Иначе — оконный просмотр со scroll.
        if n <= self.CAROUSEL_VISIBLE:
            indices = list(range(n))
        else:
            radius = self.CAROUSEL_VISIBLE // 2
            start = self._game_idx - radius
            seen = set()
            indices = []
            for offset in range(self.CAROUSEL_VISIBLE):
                idx = (start + offset) % n
                if idx in seen:
                    continue
                seen.add(idx)
                indices.append(idx)

        for idx in indices:
            game = self._games[idx]
            is_focus = (idx == self._game_idx)
            scale = self.CARD_FOCUS_SCALE if (is_focus and focus_in_carousel) else 1.0
            border = ft.Border.all(3, ACCENT) if (is_focus and focus_in_carousel) else ft.Border.all(1, "#222")
            cards.append(self._make_card(game, scale, border, is_focus and focus_in_carousel))

        self._carousel_row.controls = cards

    def _refresh_carousel_focus(self):
        # Только обновляем подсветку без полного перестроения
        self._build_carousel()
        self._refresh_hero()
        self._safe_update()

    def _refresh_tabs(self):
        self._build_tabs()
        self._safe_update()

    def _make_card(self, game: GameModel, scale: float, border, is_focus: bool) -> ft.Container:
        icon_path = self._safe_icon_path(game)
        if icon_path:
            content = ft.Image(
                src=icon_path,
                width=self.CARD_WIDTH, height=self.CARD_HEIGHT,
                fit="cover", border_radius=10,
            )
        else:
            initial = game.title[:1].upper() if game.title else "?"
            content = ft.Container(
                width=self.CARD_WIDTH, height=self.CARD_HEIGHT,
                alignment=ft.Alignment(0, 0),
                bgcolor=CARD_BG, border_radius=10,
                content=ft.Text(initial, size=64, color=TEXT_DIM, weight=ft.FontWeight.BOLD),
            )

        # Glow shadow для фокуса
        shadow = ft.BoxShadow(
            spread_radius=2, blur_radius=24,
            color=ACCENT, offset=ft.Offset(0, 0),
        ) if is_focus else None

        return ft.Container(
            scale=scale,
            border=border,
            border_radius=12,
            shadow=shadow,
            content=content,
            animate_scale=ft.Animation(120, ft.AnimationCurve.EASE_OUT),
        )

    def _refresh_hero(self):
        game = self._current_game()
        col_name = ""
        if self._collections and 0 <= self._current_collection_idx < len(self._collections):
            col_name = self._collections[self._current_collection_idx].get("name", "")

        if self._hero_counter is not None:
            if self._games:
                self._hero_counter.value = f"{col_name.upper()}  ·  {self._game_idx + 1} / {len(self._games)}"
            else:
                self._hero_counter.value = col_name.upper()

        if game is None:
            if self._hero_title is not None:
                self._hero_title.value = "Нет игр"
                self._hero_meta.value = "Переключите коллекцию (L1/R1) или запустите сканирование (Start)"
                self._hero_favorite_icon.name = ft.Icons.STAR_BORDER
                self._hero_favorite_icon.color = TEXT_DIM
            if self._hero_card is not None:
                self._hero_card.image = None
                self._hero_card.bgcolor = CARD_BG
            return

        if self._hero_card is not None:
            cover = self._safe_icon_path(game)
            if cover:
                self._hero_card.image = ft.DecorationImage(src=cover, fit="cover")
                self._hero_card.bgcolor = None
            else:
                self._hero_card.image = None
                self._hero_card.bgcolor = CARD_BG
        if self._hero_title is not None:
            self._hero_title.value = game.title
        if self._hero_meta is not None:
            parts = [
                game.platform or "PC",
                f"⏱ {_format_play_time(game.play_time)}",
                f"Последний запуск: {_format_last_played(game.last_played)}",
            ]
            self._hero_meta.value = "  ·  ".join(parts)
        if self._hero_favorite_icon is not None:
            if game.is_favorite:
                self._hero_favorite_icon.name = ft.Icons.STAR
                self._hero_favorite_icon.color = ACCENT_GOLD
            else:
                self._hero_favorite_icon.name = ft.Icons.STAR_BORDER
                self._hero_favorite_icon.color = TEXT_DIM

    # ----- Hint bar -----

    def _build_hint_controls(self):
        return [
            self._hint_chip("Ⓐ / Enter", "Запустить", ACCENT_GREEN),
            self._hint_chip("Ⓑ / Esc", "Выход", "#FF5252"),
            self._hint_chip("Ⓨ / F", "Избранное", ACCENT_GOLD),
            self._hint_chip("Ⓧ / C", "Коллекции", ACCENT),
            self._hint_chip("L1/R1 / Q,E", "Колл.", "#80C0FF"),
            self._hint_chip("Start", "Настройки", "#C0C0C0"),
            self._hint_chip("Guide / F11", "Окно", TEXT_DIM),
        ]

    def _hint_chip(self, key: str, label: str, color: str) -> ft.Container:
        return ft.Container(
            padding=ft.Padding(10, 6, 10, 6),
            bgcolor="#0006",
            border_radius=12,
            content=ft.Row(
                spacing=8,
                controls=[
                    ft.Text(key, size=14, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(label, size=14, color=TEXT_WHITE),
                ],
            ),
        )

    # ----- Settings panel -----

    def _build_settings_panel(self) -> ft.Container:
        # Rumble chip — динамический, обновляется через _refresh_settings_panel
        self._rumble_chip = self._build_rumble_chip()

        return ft.Container(
            visible=False,
            alignment=ft.Alignment(0, 0),
            bgcolor="#000000B3",
            content=ft.Container(
                width=760,
                bgcolor="#0d1428",
                border=ft.Border.all(2, ACCENT),
                border_radius=20,
                padding=40,
                content=ft.Column(
                    spacing=18,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text("Настройки", size=32, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                        ft.Text("Быстрые действия в Big Picture-режиме", size=14, color=TEXT_DIM),
                        ft.Container(height=4),
                        ft.Row(
                            spacing=12,
                            alignment=ft.MainAxisAlignment.CENTER,
                            controls=[
                                self._hint_chip("Ⓐ", "Сканировать библиотеку", ACCENT_GREEN),
                                self._hint_chip("L1/R1", "Сменить тему", ACCENT),
                            ],
                        ),
                        # Динамический tobble — Y переключает вибрацию
                        self._rumble_chip,
                        ft.Container(height=4),
                        self._hint_chip("Ⓧ", "Загрузить свой фон для текущей игры", "#80C0FF"),
                        ft.Container(height=4),
                        self._hint_chip("Ⓑ/Start", "Закрыть", "#FF5252"),
                    ],
                ),
            ),
        )

    def _build_rumble_chip(self) -> ft.Container:
        """Чип с состоянием вибрации. Y переключает."""
        on = bool(self.settings.get("gamepad_rumble", True))
        color = ACCENT_GREEN if on else "#FF5252"
        label = f"Вибрация геймпада: {'ВКЛ' if on else 'ВЫКЛ'}"
        return ft.Container(
            padding=ft.Padding(14, 10, 14, 10),
            bgcolor="#0008",
            border_radius=14,
            border=ft.Border.all(2, color),
            content=ft.Row(
                spacing=10,
                controls=[
                    ft.Text("Ⓨ", size=16, weight=ft.FontWeight.BOLD, color=color),
                    ft.Text(label, size=14, color=TEXT_WHITE),
                ],
            ),
        )

    def _refresh_settings_panel(self):
        """Перерисовывает rumble-чип после toggle."""
        if self._settings_panel is None:
            return
        # Заменяем содержимое чипа на месте, без полного перестроения панели
        on = bool(self.settings.get("gamepad_rumble", True))
        color = ACCENT_GREEN if on else "#FF5252"
        label = f"Вибрация геймпада: {'ВКЛ' if on else 'ВЫКЛ'}"
        try:
            chip = self._rumble_chip
            chip.border = ft.Border.all(2, color)
            row = chip.content
            row.controls[0].color = color
            row.controls[1].value = label
        except Exception:
            pass
        self._safe_update()

    def _open_settings(self):
        self._settings_open = True
        self._settings_panel.visible = True
        self._safe_update()

    def _close_settings(self):
        self._settings_open = False
        self._settings_panel.visible = False
        self._safe_update()

    def _on_settings_action_scan(self):
        if self._on_request_scan:
            self._on_request_scan()
        self._close_settings()

    def _cycle_theme(self, delta: int):
        if not self._on_change_theme:
            return
        # Импортируем темы из main.py динамически чтобы избежать циклических импортов
        try:
            from main import GRADIENT_THEMES
        except Exception:
            return
        ids = list(GRADIENT_THEMES.keys())
        if not ids:
            return
        try:
            cur = ids.index(self.settings.get("theme", "dark"))
        except ValueError:
            cur = 0
        new_id = ids[(cur + delta) % len(ids)]
        self.settings["theme"] = new_id
        self._on_change_theme(new_id)

    def _toggle_rumble_setting(self):
        """Переключает gamepad_rumble в settings, сохраняет, обновляет UI.
        Также даёт короткий feedback-вибрацию (если включается) — чтобы юзер
        сразу почувствовал что новое состояние применилось."""
        new_value = not bool(self.settings.get("gamepad_rumble", True))
        self.settings["gamepad_rumble"] = new_value
        if self._on_save_settings:
            try:
                self._on_save_settings()
            except Exception:
                pass
        self._refresh_settings_panel()
        # Если включили — пульс для подтверждения
        if new_value:
            self._rumble_collection()

    def _open_pick_custom_bg(self):
        """Открывает диалог выбора файла для замены landscape-фона текущей
        игры. Реальная работа делегируется CyberLauncher (живёт tk-цикл)."""
        game = self._current_game()
        if game is None or self._on_pick_custom_bg is None:
            return
        # Закрываем settings panel чтобы юзер видел изменение фона
        self._close_settings()
        try:
            self._on_pick_custom_bg(game)
        except Exception:
            pass

    def get_current_games_snapshot(self) -> tuple[list, int]:
        """Возвращает (games_list, current_idx) для использования снаружи —
        screensaver хочет начать слайдшоу с текущей фокусной игры и идти
        дальше в том же порядке, что в BP-карусели."""
        return list(self._games), self._game_idx

    def apply_custom_bg(self, game_uid: str):
        """Вызывается из CyberLauncher после успешной загрузки своего фона.
        Если юзер всё ещё на этой игре — обновляем фон с диска.

        Hack: Flutter кэширует декодированные изображения по пути. Файл
        перезаписан, но путь тот же → Flutter отдаёт старый декод.
        Решение — двухфазный апдейт: сначала image=None + page.update()
        (Flutter дропает картинку), затем image=new + page.update() (новый
        декод с диска). Между ними обязательно нужен явный page.update()."""
        cur = self._current_game()
        if cur is None or cur.uid != game_uid:
            return
        try:
            if self._bg_art is not None:
                self._bg_art.image = None
                self._bg_art.opacity = 0.0
                self._safe_update()
        except Exception:
            pass
        # Теперь применяем с диска. _apply_bg_for_current_game сделает
        # второй page.update() — Flutter увидит новый image как смену
        # с None и заново прочитает файл.
        self._apply_bg_for_current_game()

    # ----- Collections overlay -----

    def _build_collections_panel(self) -> ft.Container:
        self._collections_panel_list = ft.Column(spacing=8, controls=[])
        return ft.Container(
            visible=False,
            alignment=ft.Alignment(0, 0),
            bgcolor="#000000B3",
            content=ft.Container(
                width=560,
                bgcolor="#0d1428",
                border=ft.Border.all(2, ACCENT),
                border_radius=20,
                padding=30,
                content=ft.Column(
                    spacing=16,
                    controls=[
                        ft.Text("Коллекции", size=28, weight=ft.FontWeight.BOLD, color=TEXT_WHITE),
                        ft.Text("Ⓐ — добавить/убрать, Ⓑ — закрыть",
                                size=14, color=TEXT_DIM),
                        ft.Container(height=8),
                        self._collections_panel_list,
                    ],
                ),
            ),
        )

    def _open_collections_overlay(self):
        game = self._current_game()
        if not game:
            return
        user_collections = self.gm.get_collections() if hasattr(self.gm, "get_collections") else []
        if not user_collections:
            # Нет пользовательских коллекций — показываем сообщение в hero
            return
        self._collections_overlay_open = True
        self._collections_overlay_idx = 0
        self._refresh_collections_overlay()
        self._collections_panel.visible = True
        self._safe_update()

    def _close_collections_overlay(self):
        self._collections_overlay_open = False
        self._collections_panel.visible = False
        # После изменений коллекций — полный refresh (вдруг текущий фильтр коллекция)
        self._refresh_games()
        self._build_carousel()
        self._refresh_hero()
        self._safe_update()

    def _refresh_collections_overlay(self):
        game = self._current_game()
        if not game:
            return
        user_cols = self.gm.get_collections() if hasattr(self.gm, "get_collections") else []
        rows = []
        for idx, col in enumerate(user_cols):
            in_col = col["id"] in game.collections
            is_focus = idx == self._collections_overlay_idx
            check = "☑" if in_col else "☐"
            border = ft.Border.all(2, ACCENT) if is_focus else ft.Border.all(1, "#333")
            rows.append(ft.Container(
                padding=ft.Padding(16, 12, 16, 12),
                bgcolor="#1a233e" if is_focus else "#101828",
                border=border,
                border_radius=12,
                content=ft.Row(
                    spacing=12,
                    controls=[
                        ft.Text(check, size=22, color=ACCENT if in_col else TEXT_DIM),
                        ft.Container(width=12, height=12, border_radius=6, bgcolor=col.get("color", ACCENT)),
                        ft.Text(col["name"], size=18, color=TEXT_WHITE),
                    ],
                ),
            ))
        self._collections_panel_list.controls = rows

    def _collections_overlay_move(self, delta: int):
        user_cols = self.gm.get_collections() if hasattr(self.gm, "get_collections") else []
        if not user_cols:
            return
        self._collections_overlay_idx = (self._collections_overlay_idx + delta) % len(user_cols)
        self._refresh_collections_overlay()
        self._safe_update()

    def _collections_overlay_toggle_current(self):
        game = self._current_game()
        if not game:
            return
        user_cols = self.gm.get_collections() if hasattr(self.gm, "get_collections") else []
        if not user_cols:
            return
        col = user_cols[self._collections_overlay_idx]
        # Идемпотентный toggle — единая точка истины в backend
        self.gm.toggle_collection_sync(game.uid, col["id"])
        self._refresh_collections_overlay()
        self._safe_update()

    # ----- Other actions -----

    def _toggle_favorite(self):
        game = self._current_game()
        if not game:
            return
        game.is_favorite = not game.is_favorite
        # Сохраняем через debounce в backend
        if hasattr(self.gm, "request_save"):
            self.gm.request_save()
        self._refresh_hero()
        # Если фильтр "Избранное" — список мог измениться
        if self._collections and self._collections[self._current_collection_idx]["id"] == "_favorites":
            self._refresh_games()
            self._build_carousel()
        self._safe_update()

    def _safe_icon_path(self, game: Optional[GameModel]) -> Optional[str]:
        if game is None or not game.icon_path:
            return None
        # Поддерживаем относительные пути относительно cwd
        if os.path.isabs(game.icon_path):
            return game.icon_path if os.path.exists(game.icon_path) else None
        abs_path = os.path.abspath(game.icon_path)
        return abs_path if os.path.exists(abs_path) else None

    def _initial_bg_url(self, game: Optional[GameModel]) -> Optional[str]:
        """Landscape-арт для фона — ТОЛЬКО из локального кэша.
        GameManager.prefetch_hero_art заранее скачал по одному качественному
        арту на игру (Steam library_hero / RAWG background_image). Если
        prefetch ещё не успел — None, BigPicture покажет тёмный градиент."""
        return self.gm.get_hero_path(game)

    # ----- Background art (single static landscape per game) -----

    def _schedule_bg_update(self):
        """Применяет фон текущей игры. Только из локального кэша
        (GameManager.prefetch_hero_art скачал заранее). Никаких сетевых
        запросов и cycling — один статичный качественный фон на игру."""
        self._apply_bg_for_current_game()

    def _apply_bg_for_current_game(self):
        """Ставит ОДИН качественный landscape-арт текущей игры из кэша.
        Если в кэше нет (prefetch не успел или арт не нашёлся) — фон
        прозрачный (только тёмный градиент), без плохих fallback-картинок."""
        try:
            if self._bg_art is None:
                return
            game = self._current_game()
            bg = self._initial_bg_url(game) if game else None
            if bg:
                self._bg_art.image = ft.DecorationImage(src=bg, fit="cover")
                self._bg_art.opacity = 0.55
            else:
                self._bg_art.image = None
                self._bg_art.opacity = 0.0
            self._safe_update()
        except Exception:
            pass

    def _safe_update(self):
        try:
            self.page.update()
        except Exception:
            pass
