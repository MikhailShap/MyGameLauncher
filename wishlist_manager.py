"""Wishlist Manager — отдельный раздел "Желаемое" с играми из Steam.

Хранит игры, в которые юзер ХОЧЕТ играть, но ещё не установил. Источник
метаданных — Steam Store API (storesearch + appdetails). Никаких локальных
exe/install_path: только Steam-страница, описание, трейлер.

Огонёк (is_priority) — пометка "хочу взять в ближайшее время", по нему
можно сортировать вверх списка.

Файл `wishlist.json` в %APPDATA%\\CyberLauncher\\data\\. Атомарная запись
через .tmp + os.replace.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("WishlistManager")


@dataclass
class WishlistItem:
    """Один элемент списка желаемого. Все поля кроме app_id могут быть пустыми,
    если метаданные ещё не подтянуты или Steam не вернул их."""
    app_id: str                                  # Steam appid (строка для совместимости)
    title: str = ""
    header_image_url: str = ""                   # 460×215 — для карточки
    short_description: str = ""
    store_url: str = ""                          # https://store.steampowered.com/app/<id>/
    trailer_url: str = ""                        # mp4 max-quality из movies[]
    trailer_thumb_url: str = ""                  # тубнейл трейлера
    release_date: str = ""
    developers: str = ""                         # "Dev1, Dev2"
    genres: str = ""                             # "Action, RPG"
    is_priority: bool = False                    # огонёк
    added_date: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WishlistItem":
        valid = {f for f in cls.__dataclass_fields__.keys()}
        return cls(**{k: v for k, v in data.items() if k in valid})


def _http_get_json(url: str, timeout: float = 10.0) -> Optional[Any]:
    """Простой HTTP GET → JSON. Возвращает None при любой ошибке."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (CyberLauncher) Wishlist/1.0",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.debug(f"HTTP GET failed {url}: {e}")
    return None


def steam_search(query: str, limit: int = 8) -> List[Dict[str, str]]:
    """Steam Store autocomplete. Возвращает список {app_id, name, header_image}.

    Используется для подсказок в диалоге "Добавить в желаемое"."""
    q = query.strip()
    if len(q) < 2:
        return []
    url = (f"https://store.steampowered.com/api/storesearch/"
           f"?term={urllib.parse.quote(q)}&l=russian&cc=RU")
    data = _http_get_json(url, timeout=8.0)
    if not data or not isinstance(data, dict):
        return []
    items: List[Dict[str, str]] = []
    for it in (data.get("items") or [])[:limit]:
        try:
            items.append({
                "app_id": str(it.get("id", "")),
                "name": it.get("name", "") or "",
                "header_image": it.get("tiny_image", "") or "",
            })
        except Exception:
            continue
    return items


def steam_appdetails(app_id: str, lang: str = "russian") -> Optional[Dict[str, Any]]:
    """Steam appdetails API — полные метаданные одной игры."""
    if not app_id:
        return None
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={urllib.parse.quote(str(app_id))}&l={lang}")
    data = _http_get_json(url, timeout=12.0)
    if not data or not isinstance(data, dict):
        return None
    entry = data.get(str(app_id))
    if not entry or not entry.get("success"):
        return None
    return entry.get("data") or {}


def build_wishlist_item(app_id: str) -> Optional[WishlistItem]:
    """Берёт app_id, тянет appdetails, собирает WishlistItem с заполненными
    полями. Возвращает None если Steam не дал данных."""
    details = steam_appdetails(app_id)
    if not details:
        return None
    item = WishlistItem(app_id=str(app_id))
    item.title = details.get("name") or ""
    item.header_image_url = details.get("header_image") or ""
    item.short_description = details.get("short_description") or ""
    item.store_url = f"https://store.steampowered.com/app/{app_id}/"
    # Трейлер: movies → берём highlight=True или первый
    movies = details.get("movies") or []
    if movies:
        chosen = next((m for m in movies if m.get("highlight")), movies[0])
        item.trailer_thumb_url = chosen.get("thumbnail") or ""
        mp4 = chosen.get("mp4") or {}
        # max → 480 fallback
        item.trailer_url = mp4.get("max") or mp4.get("480") or ""
    # Дата релиза
    rd = details.get("release_date") or {}
    item.release_date = rd.get("date") or ""
    # Разработчики, жанры
    devs = details.get("developers") or []
    item.developers = ", ".join(devs) if devs else ""
    genres = [g.get("description", "") for g in (details.get("genres") or [])]
    item.genres = ", ".join(g for g in genres if g)
    return item


class WishlistManager:
    """Управление списком желаемого. Хранит элементы в wishlist.json
    рядом с library.json. Все мутирующие операции пишут на диск
    синхронно — wishlist маленький, лагов нет."""

    SORT_PRIORITY = "priority"      # огонёк сверху → date_desc
    SORT_DATE_DESC = "date_desc"    # новые сверху
    SORT_DATE_ASC = "date_asc"
    SORT_NAME = "name"

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.data_dir / "wishlist.json"
        self._items: Dict[str, WishlistItem] = {}   # app_id → item
        # Параллельный пул для fetch'а метаданных (несколько add сразу)
        self._executor = ThreadPoolExecutor(max_workers=3)

    # ---------- Persistence ----------

    def load(self):
        if not self.file.exists():
            return
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for raw in data.get("items", []):
                try:
                    item = WishlistItem.from_dict(raw)
                    if item.app_id:
                        self._items[item.app_id] = item
                except Exception as e:
                    logger.warning(f"Skipping malformed wishlist item: {e}")
            logger.info(f"Wishlist loaded: {len(self._items)} items")
        except Exception as e:
            logger.error(f"Wishlist load failed: {e}")

    def save_sync(self) -> bool:
        """Атомарная запись wishlist.json (tmp + os.replace)."""
        try:
            payload = {
                "items": [it.to_dict() for it in self._items.values()],
            }
            tmp = self.file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.file)
            return True
        except Exception as e:
            logger.error(f"Wishlist save failed: {e}")
            return False

    # ---------- Mutations ----------

    def add_by_app_id(self, app_id: str) -> Optional[WishlistItem]:
        """Добавляет игру по Steam app_id. Тянет метаданные. Если уже есть —
        возвращает существующий элемент без перезаписи."""
        app_id = str(app_id).strip()
        if not app_id:
            return None
        if app_id in self._items:
            return self._items[app_id]
        item = build_wishlist_item(app_id)
        if item is None:
            logger.warning(f"Wishlist add failed: no Steam data for app_id={app_id}")
            return None
        self._items[app_id] = item
        self.save_sync()
        logger.info(f"Wishlist: added '{item.title}' (app_id={app_id})")
        return item

    def remove(self, app_id: str) -> bool:
        if app_id in self._items:
            title = self._items[app_id].title
            del self._items[app_id]
            self.save_sync()
            logger.info(f"Wishlist: removed '{title}' (app_id={app_id})")
            return True
        return False

    def toggle_priority(self, app_id: str) -> Optional[bool]:
        """Огонёк on/off. Возвращает новое значение или None если нет такого app_id."""
        if app_id not in self._items:
            return None
        item = self._items[app_id]
        item.is_priority = not item.is_priority
        self.save_sync()
        return item.is_priority

    def has(self, app_id: str) -> bool:
        return str(app_id) in self._items

    # ---------- Queries ----------

    def get_items(self) -> List[WishlistItem]:
        return list(self._items.values())

    def get_sorted(self, sort_key: str) -> List[WishlistItem]:
        items = list(self._items.values())
        if sort_key == self.SORT_PRIORITY:
            # огонёк сверху, потом по дате убывания
            items.sort(key=lambda it: (not it.is_priority, it.added_date or ""), reverse=False)
            # added_date пустые в конец — но reverse=False с (False, date) сортит ascending по дате
            # хотим новые сверху среди огоньковых
            items.sort(key=lambda it: (not it.is_priority, -_iso_ts(it.added_date)))
        elif sort_key == self.SORT_DATE_DESC:
            items.sort(key=lambda it: it.added_date or "", reverse=True)
        elif sort_key == self.SORT_DATE_ASC:
            items.sort(key=lambda it: it.added_date or "")
        elif sort_key == self.SORT_NAME:
            items.sort(key=lambda it: (it.title or "").lower())
        return items


def _iso_ts(iso: str) -> float:
    """ISO-строку → POSIX-timestamp (для сортировки)."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0
