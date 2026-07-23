"""Wishlist Manager — отдельный раздел "Желаемое" с играми из Steam.

Хранит игры, в которые юзер ХОЧЕТ играть, но ещё не установил. Источник
метаданных — Steam Store API (storesearch + appdetails). Никаких локальных
exe/install_path: только Steam-страница, описание, трейлер.

Огонёк (priority) — 3-уровневая пометка:
  high   — зелёный — играть прям в ближайшее время
  medium — жёлтый — поиграть потом
  low    — серый — попробовать когда-нибудь (default)
Сортировка SORT_PRIORITY: high → medium → low, внутри уровня — новые сверху.

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
import re
import time
import html as _html
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("WishlistManager")


def strip_html(raw: str) -> str:
    """Грубая, но безопасная очистка Steam-HTML описания в читаемый текст.
    Блочные теги → перевод строки, <li> → '• ', остальные теги удаляются,
    HTML-сущности раскодируются. Без внешних зависимостей."""
    if not raw:
        return ""
    s = raw
    # <br>, </p>, </div>, заголовки, </li> → перенос строки
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*/\s*(p|div|h[1-6]|tr|ul|ol)\s*>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", s)
    # Остальные теги вырезаем
    s = re.sub(r"<[^>]+>", "", s)
    # HTML-сущности (&amp; &quot; &#39; …)
    s = _html.unescape(s)
    # Схлопываем лишние пустые строки и пробелы
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _http_get_text(url: str, timeout: float = 8.0):
    """GET → (text, final_url). final_url учитывает редиректы (нужен для
    разрешения относительных URI в HLS-плейлисте)."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (CyberLauncher) Wishlist/1.0",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None, url
            text = r.read().decode("utf-8", errors="replace")
            return text, r.geturl()
    except Exception as e:
        logger.debug(f"HLS GET failed {url}: {e}")
        return None, url


def parse_hls_variants(master_url: str) -> Optional[Dict[str, Any]]:
    """Парсит HLS master-плейлист трейлера Steam в список вариантов качества.

    Возвращает {'audio_media': str|None, 'variants': [{'height':int,
    'bandwidth':int, 'inf':str, 'video_url':abs_url}]} или None.

    В новой схеме Steam аудио вынесено отдельной дорожкой
    (#EXT-X-MEDIA:TYPE=AUDIO), а варианты видео — без звука. Поэтому для
    проигрывания конкретного качества нужен синтетический master с одним
    видео-вариантом + аудио-дорожкой (см. build_single_quality_m3u8)."""
    text, final_url = _http_get_text(master_url)
    if not text or "#EXTM3U" not in text:
        return None
    base = final_url.split("?", 1)[0].rsplit("/", 1)[0] + "/"

    def absolutize(uri: str) -> str:
        uri = uri.strip()
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri
        return base + uri

    def rewrite_uri_abs(line: str) -> str:
        # Заменяет URI="..." на абсолютный
        m = re.search(r'URI="([^"]+)"', line)
        if not m:
            return line
        return line[:m.start(1)] + absolutize(m.group(1)) + line[m.end(1):]

    audio_media = None
    variants = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        ls = line.strip()
        if ls.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in ls:
            audio_media = rewrite_uri_abs(ls)
        elif ls.startswith("#EXT-X-STREAM-INF:"):
            # Следующая непустая строка — URI варианта
            uri = ""
            for j in range(i + 1, len(lines)):
                cand = lines[j].strip()
                if cand and not cand.startswith("#"):
                    uri = cand
                    break
            if not uri:
                continue
            res = re.search(r"RESOLUTION=\d+x(\d+)", ls)
            bw = re.search(r"BANDWIDTH=(\d+)", ls)
            variants.append({
                "height": int(res.group(1)) if res else 0,
                "bandwidth": int(bw.group(1)) if bw else 0,
                "inf": ls,
                "video_url": absolutize(uri),
            })
    if not variants:
        return None
    variants.sort(key=lambda v: v["height"], reverse=True)
    return {"audio_media": audio_media, "variants": variants}


def build_single_quality_m3u8(variant: Dict[str, Any], audio_media: Optional[str]) -> str:
    """Синтетический master-плейлист с ОДНИМ видео-вариантом + аудио-дорожкой.
    Все URI абсолютные, поэтому mpv проиграет нужное качество со звуком."""
    out = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    if audio_media:
        out.append(audio_media)
    out.append(variant["inf"])
    out.append(variant["video_url"])
    return "\n".join(out) + "\n"


# Уровни приоритета. Зелёный — "хочу прям сейчас", жёлтый — "потом",
# серый — "просто попробовать когда-нибудь". Сортировка идёт high → med → low.
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_LEVELS = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)
_PRIORITY_RANK = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}


@dataclass
class WishlistItem:
    """Один элемент списка желаемого. Все поля кроме app_id могут быть пустыми,
    если метаданные ещё не подтянуты или Steam не вернул их."""
    app_id: str                                  # Steam appid (строка для совместимости)
    title: str = ""
    header_image_url: str = ""                   # 460×215 — для карточки
    short_description: str = ""
    store_url: str = ""                          # https://store.steampowered.com/app/<id>/
    trailer_url: str = ""                        # предпочтительный трейлер (mp4 / dash_av1 / hls)
    trailer_url_hls: str = ""                    # HLS-фоллбек, если trailer_url = DASH AV1
    trailer_thumb_url: str = ""                  # тубнейл трейлера
    # True — запись создана импортом из Steam и ждёт догрузки деталей
    # (название/обложка). Импорт даёт только app_id одним запросом; детали
    # Steam отдаёт лишь по одной игре, поэтому тянем их фоном. См.
    # WishlistManager.fill_missing_details.
    needs_details: bool = False
    release_date: str = ""
    developers: str = ""                         # "Dev1, Dev2"
    genres: str = ""                             # "Action, RPG"
    # 3-уровневый приоритет: high (зелёный — играть прям сейчас),
    # medium (жёлтый — потом), low (серый — попробовать когда-нибудь).
    # default low чтобы новые игры не лезли в верх списка по умолчанию.
    priority: str = PRIORITY_LOW
    added_date: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WishlistItem":
        valid = {f for f in cls.__dataclass_fields__.keys()}
        filtered = {k: v for k, v in data.items() if k in valid}
        # Миграция со старой модели (is_priority: bool). Если в JSON ещё нет
        # priority, но есть is_priority — конвертируем True → high, False → low.
        if "priority" not in filtered:
            filtered["priority"] = PRIORITY_HIGH if data.get("is_priority") else PRIORITY_LOW
        # Валидация: неизвестные значения → low (защита от мусора в JSON)
        if filtered.get("priority") not in PRIORITY_LEVELS:
            filtered["priority"] = PRIORITY_LOW
        return cls(**filtered)


def _url_exists(url: str, timeout: float = 6.0) -> bool:
    """HEAD-проверка доступности URL (200). Для выбора качественного баннера."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "Mozilla/5.0 (CyberLauncher) Wishlist/1.0",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


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


# Регион Steam Store. ОСНОВНОЙ задаёт валюту/выдачу; ФОЛЛБЕК нужен потому, что
# часть игр в RU не продаётся (напр. 007 First Light): storesearch их вообще не
# показывает, а appdetails отвечает success=false — игру нельзя было ни найти,
# ни добавить. Спрашиваем фоллбек-регион, чтобы игра была доступна (цена тогда
# в валюте фоллбека — это лучше, чем «игры не существует»).
# Переопределяются из settings.json: steam_cc / steam_cc_fallback.
STEAM_CC = "RU"
STEAM_CC_FALLBACK = "KZ"


def set_steam_region(cc: str = "", cc_fallback: str = "") -> None:
    """Задать регионы из настроек приложения (пустое = оставить как есть)."""
    global STEAM_CC, STEAM_CC_FALLBACK
    if cc:
        STEAM_CC = cc.strip().upper()
    # Пустой фоллбек — осознанное «искать только в основном регионе».
    STEAM_CC_FALLBACK = cc_fallback.strip().upper() if cc_fallback is not None else ""
    logger.info(f"Steam region: cc={STEAM_CC}, fallback={STEAM_CC_FALLBACK or '(нет)'}")


def _search_one_region(q: str, cc: str, limit: int) -> List[Dict[str, str]]:
    url = (f"https://store.steampowered.com/api/storesearch/"
           f"?term={urllib.parse.quote(q)}&l=russian&cc={urllib.parse.quote(cc)}")
    data = _http_get_json(url, timeout=8.0)
    if not data or not isinstance(data, dict):
        return []
    out: List[Dict[str, str]] = []
    for it in (data.get("items") or [])[:limit]:
        try:
            out.append({
                "app_id": str(it.get("id", "")),
                "name": it.get("name", "") or "",
                "header_image": it.get("tiny_image", "") or "",
            })
        except Exception:
            continue
    return out


def steam_search(query: str, limit: int = 8) -> List[Dict[str, str]]:
    """Steam Store autocomplete. Возвращает список {app_id, name, header_image}.

    Ищет в основном регионе, затем ДОБИРАЕТ из фоллбек-региона игры, которых
    в основном нет (региональные ограничения). Порядок основного региона
    сохраняется, дубли по app_id отсекаются."""
    q = query.strip()
    if len(q) < 2:
        return []
    primary = _search_one_region(q, STEAM_CC, limit * 2)
    if not (STEAM_CC_FALLBACK and STEAM_CC_FALLBACK != STEAM_CC):
        return primary[:limit]
    fallback = _search_one_region(q, STEAM_CC_FALLBACK, limit * 2)

    # Слияние ЧЕРЕДОВАНИЕМ ПО РАНГУ, а не «primary целиком, потом остатки»:
    # Steam сортирует по релевантности, и в регионе с ограничением основная
    # игра выпадает совсем (в RU по «007 First Light» — только DLC, они
    # занимают весь лимит). Чередование поднимает топ-результат фоллбека
    # (саму игру) на 2-е место вместо выпадения за лимит.
    merged: List[Dict[str, str]] = []
    seen = set()
    for i in range(max(len(primary), len(fallback))):
        for src in (primary, fallback):
            if i < len(src) and src[i]["app_id"] not in seen:
                seen.add(src[i]["app_id"])
                merged.append(src[i])
        if len(merged) >= limit:
            break
    return merged[:limit]


def steam_appdetails(app_id: str, lang: str = "russian") -> Optional[Dict[str, Any]]:
    """Steam appdetails API — полные метаданные одной игры.
    При success=false в основном регионе (игра там не продаётся) повторяет
    запрос в фоллбек-регионе."""
    if not app_id:
        return None
    for cc in (STEAM_CC, STEAM_CC_FALLBACK):
        if not cc:
            continue
        url = (f"https://store.steampowered.com/api/appdetails"
               f"?appids={urllib.parse.quote(str(app_id))}&l={lang}"
               f"&cc={urllib.parse.quote(cc)}")
        data = _http_get_json(url, timeout=12.0)
        entry = (data or {}).get(str(app_id)) if isinstance(data, dict) else None
        if entry and entry.get("success"):
            return entry.get("data") or {}
        if cc == STEAM_CC and STEAM_CC_FALLBACK and STEAM_CC_FALLBACK != STEAM_CC:
            logger.info(f"appdetails {app_id}: нет в {cc}, пробуем {STEAM_CC_FALLBACK}")
        if STEAM_CC_FALLBACK == STEAM_CC:
            break
    return None


def steam_get_wishlist(steamid64: str) -> Optional[List[Dict[str, Any]]]:
    """Список желаемого аккаунта: [{appid, priority, date_added}].

    IWishlistService — публичный, Web API-ключ НЕ нужен. Возвращает:
      • список (может быть пустым — у закрытого профиля Steam отдаёт 200 и
        пустой items, это НЕ ошибка сети, показать пользователю про приватность);
      • None — сеть/сервис недоступны либо steamid невалиден.
    """
    sid = str(steamid64).strip()
    if not sid.isdigit() or len(sid) != 17:
        logger.warning(f"Wishlist import: невалидный SteamID64 '{steamid64}'")
        return None
    url = (f"https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
           f"?steamid={urllib.parse.quote(sid)}")
    data = _http_get_json(url, timeout=20.0)
    if not isinstance(data, dict):
        return None
    resp = data.get("response")
    if not isinstance(resp, dict):
        return None
    items = resp.get("items")
    if items is None:
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        try:
            appid = it.get("appid")
            if appid:
                out.append({
                    "appid": str(appid),
                    "priority": int(it.get("priority") or 0),
                    "date_added": int(it.get("date_added") or 0),
                })
        except Exception:
            continue
    return out


def find_local_steamid64() -> Optional[Dict[str, str]]:
    """SteamID64 последнего вошедшего аккаунта из локальной установки Steam.
    Возвращает {'steamid', 'account', 'persona'} или None.

    Путь к Steam берём из реестра (как SteamScanner), аккаунты — из
    config/loginusers.vdf; при нескольких берём с MostRecent=1, иначе первый."""
    steam_path = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path = winreg.QueryValueEx(key, "SteamPath")[0]
    except Exception:
        for cand in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
            if os.path.isdir(cand):
                steam_path = cand
                break
    if not steam_path:
        return None
    vdf = Path(str(steam_path).replace("/", os.sep)) / "config" / "loginusers.vdf"
    if not vdf.exists():
        return None
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"loginusers.vdf read failed: {e}")
        return None
    # Плоский разбор: блоки "<steamid64>" { ... }
    accounts: List[Dict[str, str]] = []
    cur: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^"(\d{17})"$', s)
        if m:
            cur = {"steamid": m.group(1), "account": "", "persona": "", "recent": "0"}
            accounts.append(cur)
            continue
        if cur is None:
            continue
        kv = re.match(r'^"([^"]+)"\s+"([^"]*)"$', s)
        if kv:
            k, v = kv.group(1).lower(), kv.group(2)
            if k == "accountname":
                cur["account"] = v
            elif k == "personaname":
                cur["persona"] = v
            elif k == "mostrecent":
                cur["recent"] = v
    if not accounts:
        return None
    best = next((a for a in accounts if a.get("recent") == "1"), accounts[0])
    return {"steamid": best["steamid"], "account": best.get("account", ""),
            "persona": best.get("persona", "")}


def steam_priority_to_level(priority: int) -> str:
    """Позиция в ранжированном списке Steam → наш уровень важности.
    Steam: 0 = без ручной сортировки, 1..N = позиция (1 — самая желанная)."""
    if 1 <= priority <= 3:
        return PRIORITY_HIGH
    if 4 <= priority <= 10:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


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
        mp4 = chosen.get("mp4") if isinstance(chosen.get("mp4"), dict) else {}
        webm = chosen.get("webm") if isinstance(chosen.get("webm"), dict) else {}
        # Старая схема: mp4/webm {480,max}. Новая (2024+): только hls/dash-
        # манифест (строка-URL). Предпочитаем dash_av1 (чище картинка при том
        # же битрейте, см. fetch_game_details); HLS — фоллбек в trailer_url_hls.
        item.trailer_url = (
            mp4.get("max") or mp4.get("480")
            or webm.get("max") or webm.get("480")
            or chosen.get("dash_av1") or chosen.get("hls_h264")
            or chosen.get("dash_h264") or ""
        )
        hls = chosen.get("hls_h264") or ""
        item.trailer_url_hls = hls if hls != item.trailer_url else ""
    # Дата релиза
    rd = details.get("release_date") or {}
    item.release_date = rd.get("date") or ""
    # Разработчики, жанры
    devs = details.get("developers") or []
    item.developers = ", ".join(devs) if devs else ""
    genres = [g.get("description", "") for g in (details.get("genres") or [])]
    item.genres = ", ".join(g for g in genres if g)
    return item


def fetch_game_details(app_id: str) -> Optional[Dict[str, Any]]:
    """Полные детали игры для детального экрана. Тянет appdetails и
    нормализует в плоский dict, удобный для UI. Возвращает None если
    Steam не дал данных.

    Поля результата:
      title, header_image, background, about (plain text),
      release_date, developers, publishers (str),
      genres (str), categories (list[str] — фичи: одиночная игра,
      контроллер, ачивки…), platforms (list[str]: Windows/Mac/Linux),
      metacritic_score (int|None), metacritic_url,
      price (str: 'Бесплатно' / '999 руб.' / ''),
      screenshots (list[url]), trailers (list[{name, thumb, url}]),
      store_url
    """
    details = steam_appdetails(app_id)
    if not details:
        return None

    # Описание: about_the_game приоритетнее (полнее), затем detailed_description
    about_raw = details.get("about_the_game") or details.get("detailed_description") or ""
    about = strip_html(about_raw)

    devs = details.get("developers") or []
    pubs = details.get("publishers") or []
    genres = [g.get("description", "") for g in (details.get("genres") or [])]
    cats = [c.get("description", "") for c in (details.get("categories") or [])]

    plat_raw = details.get("platforms") or {}
    platforms = []
    if plat_raw.get("windows"):
        platforms.append("Windows")
    if plat_raw.get("mac"):
        platforms.append("macOS")
    if plat_raw.get("linux"):
        platforms.append("Linux")

    mc = details.get("metacritic") or {}
    metacritic_score = mc.get("score")
    metacritic_url = mc.get("url") or ""

    # Цена
    if details.get("is_free"):
        price = "Бесплатно"
    else:
        po = details.get("price_overview") or {}
        price = po.get("final_formatted") or ""

    screenshots = []
    for sh in (details.get("screenshots") or []):
        url = sh.get("path_full") or sh.get("path_thumbnail") or ""
        if url:
            screenshots.append(url)

    # Качественный баннер для шапки. header_image всего 460×215 → мылит при
    # растяжении. library_hero — 1920×620, специально под баннер. Если его нет
    # (редкие игры) — первый скриншот (1920×1080), иначе header_image.
    hero_image = ""
    lib_hero = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_hero.jpg"
    if _url_exists(lib_hero):
        hero_image = lib_hero
    elif screenshots:
        hero_image = screenshots[0]
    else:
        hero_image = details.get("header_image") or ""

    trailers = []
    for m in (details.get("movies") or []):
        # Две схемы Steam:
        #  1) Старая: mp4/webm = {"480": url, "max": url} — прямой файл.
        #  2) Новая (с 2024+): hls_h264 / dash_h264 / dash_av1 = строка-URL
        #     HLS/DASH-манифеста. media_kit/libmpv проигрывает HLS.
        url = ""
        url_hls = ""
        mp4 = m.get("mp4")
        webm = m.get("webm")
        if isinstance(mp4, dict) and (mp4.get("max") or mp4.get("480")):
            url = mp4.get("max") or mp4.get("480")
        elif isinstance(webm, dict) and (webm.get("max") or webm.get("480")):
            url = webm.get("max") or webm.get("480")
        else:
            # Новая схема: предпочитаем dash_av1 — при ТОМ ЖЕ битрейте (Steam
            # кодирует оба в 5800k@1080p) AV1 заметно чище H.264; браузерный
            # плеер Steam играет именно его — отсюда была разница «у нас
            # мыльнее». libmpv умеет AV1 (dav1d в сборке flet_desktop, проверено).
            # HLS оставляем фоллбеком (url_hls) — на него откатывается плеер
            # при ошибке AV1 и настройка trailer_codec="hls".
            url = (m.get("dash_av1") or m.get("hls_h264")
                   or m.get("dash_h264") or "")
            url_hls = m.get("hls_h264") or ""
            if url == url_hls:
                url_hls = ""     # фоллбек совпадает с основным — не дублируем
        if url:
            trailers.append({
                "name": m.get("name", "") or "",
                "thumb": m.get("thumbnail", "") or "",
                "url": url,
                "url_hls": url_hls,
            })

    return {
        "app_id": str(app_id),
        "title": details.get("name") or "",
        "header_image": details.get("header_image") or "",
        "hero_image": hero_image,
        "background": details.get("background_raw") or details.get("background") or "",
        "about": about,
        "release_date": (details.get("release_date") or {}).get("date") or "",
        "developers": ", ".join(devs) if devs else "",
        "publishers": ", ".join(pubs) if pubs else "",
        "genres": ", ".join(g for g in genres if g),
        "categories": [c for c in cats if c],
        "platforms": platforms,
        "metacritic_score": metacritic_score,
        "metacritic_url": metacritic_url,
        "price": price,
        "screenshots": screenshots,
        "trailers": trailers,
        "store_url": f"https://store.steampowered.com/app/{app_id}/",
    }


class WishlistManager:
    """Управление списком желаемого. Хранит элементы в wishlist.json
    рядом с library.json. Все мутирующие операции пишут на диск
    синхронно — wishlist маленький, лагов нет."""

    SORT_PRIORITY = "priority"      # огонёк сверху → date_desc
    SORT_DATE_DESC = "date_desc"    # новые сверху
    SORT_DATE_ASC = "date_asc"
    SORT_NAME = "name"

    # Кэш детальных данных (детальный экран). Описание/скриншоты меняются
    # редко; цена/дата релиза — иногда. 7 дней — разумный TTL.
    DETAILS_TTL_SECONDS = 7 * 24 * 3600

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.data_dir / "wishlist.json"
        self._items: Dict[str, WishlistItem] = {}   # app_id → item
        # Кэш детальных данных: память (на сессию) + диск (между запусками).
        # Диск: <app_data>/cache/wishlist_details/<app_id>.json
        self._details_cache_dir = self.data_dir.parent / "cache" / "wishlist_details"
        self._details_mem: Dict[str, Dict[str, Any]] = {}
        # Кэш разобранных HLS-вариантов трейлеров (по master-URL, на сессию)
        self._hls_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    # ---------- Качество трейлера (HLS) ----------

    def get_trailer_quality_info(self, master_url: str) -> Optional[Dict[str, Any]]:
        """Разбирает HLS master трейлера в список вариантов (с памятью-кэшем)."""
        if master_url in self._hls_cache:
            return self._hls_cache[master_url]
        info = parse_hls_variants(master_url)
        self._hls_cache[master_url] = info
        return info

    def write_quality_playlist(self, variant: Dict[str, Any],
                               audio_media: Optional[str]) -> str:
        """Пишет синтетический master-плейлист (одно качество + аудио) во
        временный файл и возвращает file:// URI. mpv проиграет его со звуком.
        Возвращаем именно URI (а не голый путь) — media_kit на Windows
        надёжнее открывает file:// чем 'C:\\...'."""
        text = build_single_quality_m3u8(variant, audio_media)
        self._details_cache_dir.mkdir(parents=True, exist_ok=True)
        # Уникальное имя — иначе media_kit/Flutter может взять старый из кэша
        path = self._details_cache_dir / f"_q_{variant.get('height', 0)}_{int(time.time()*1000)}.m3u8"
        path.write_text(text, encoding="utf-8")
        # Чистим прошлые временные плейлисты
        for old in self._details_cache_dir.glob("_q_*.m3u8"):
            if old != path:
                try:
                    old.unlink()
                except Exception:
                    pass
        return path.as_uri()

    # ---------- Детальные данные с кэшем ----------

    def get_details(self, app_id: str, force: bool = False) -> Optional[Dict[str, Any]]:
        """Детали игры для детального экрана с кэшированием.
        Порядок: память → диск (если свежий) → сеть. force=True игнорирует
        кэш и тянет заново (кнопка «Обновить»). Безопасно вызывать из потока."""
        app_id = str(app_id)
        if not force:
            mem = self._details_mem.get(app_id)
            if mem is not None:
                return mem
            disk = self._read_details_disk(app_id)
            if disk is not None:
                self._details_mem[app_id] = disk
                return disk
        # Сеть
        data = fetch_game_details(app_id)
        if data:
            self._details_mem[app_id] = data
            self._write_details_disk(app_id, data)
            self._maybe_refresh_trailer_fields(app_id, data)
            return data
        # Сеть не дала данных (офлайн/Steam недоступен) — отдаём протухший
        # диск-кэш, если он есть: показать устаревшие данные лучше, чем экран
        # "Не удалось загрузить" при полном кэше на диске.
        stale = self._read_details_disk(app_id, ignore_ttl=True)
        if stale is not None:
            logger.info(f"Details: network failed, serving stale cache for {app_id}")
            self._details_mem[app_id] = stale
        return stale

    def _maybe_refresh_trailer_fields(self, app_id: str, data: Dict[str, Any]) -> None:
        """Тихая миграция: записи, добавленные до перехода на dash_av1, хранят
        в wishlist.json старый HLS-URL. При успешном сетевом обновлении деталей
        освежаем трейлер-поля карточки (тот же трейлер ищем по тумбнейлу)."""
        try:
            it = self._items.get(str(app_id))
            trailers = data.get("trailers") or []
            if not it or not trailers:
                return
            cand = next((t for t in trailers
                         if t.get("thumb") and t.get("thumb") == it.trailer_thumb_url),
                        trailers[0])
            new_url = cand.get("url") or ""
            new_hls = cand.get("url_hls") or ""
            if new_url and (new_url != it.trailer_url or new_hls != it.trailer_url_hls):
                it.trailer_url = new_url
                it.trailer_url_hls = new_hls
                if cand.get("thumb"):
                    it.trailer_thumb_url = cand["thumb"]
                self.save_sync()
                logger.info(f"Wishlist: трейлер обновлён на предпочтительный формат "
                            f"(app_id={app_id})")
        except Exception as e:
            logger.debug(f"Wishlist trailer refresh skipped: {e}")

    def get_details_cached(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Только из памяти/свежего диска, БЕЗ сети. Для мгновенного показа
        без спиннера, если данные уже есть. None если в кэше нет."""
        app_id = str(app_id)
        mem = self._details_mem.get(app_id)
        if mem is not None:
            return mem
        disk = self._read_details_disk(app_id)
        if disk is not None:
            self._details_mem[app_id] = disk
        return disk

    def _read_details_disk(self, app_id: str, ignore_ttl: bool = False) -> Optional[Dict[str, Any]]:
        f = self._details_cache_dir / f"{app_id}.json"
        if not f.exists():
            return None
        try:
            with open(f, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            ts = raw.get("_cached_at", 0)
            # ignore_ttl=True — отдать даже протухший кэш (fallback при офлайне).
            if not ignore_ttl and (time.time() - ts) > self.DETAILS_TTL_SECONDS:
                return None  # устарело → перезапросим из сети
            return raw.get("data")
        except Exception:
            return None

    def _write_details_disk(self, app_id: str, data: Dict[str, Any]) -> None:
        try:
            self._details_cache_dir.mkdir(parents=True, exist_ok=True)
            f = self._details_cache_dir / f"{app_id}.json"
            payload = {"_cached_at": time.time(), "data": data}
            tmp = f.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False)
            os.replace(tmp, f)
        except Exception as e:
            logger.warning(f"Details cache write failed for {app_id}: {e}")

    def _drop_details_cache(self, app_id: str) -> None:
        """Удалить кэш деталей (при удалении игры из wishlist)."""
        self._details_mem.pop(str(app_id), None)
        try:
            f = self._details_cache_dir / f"{app_id}.json"
            if f.exists():
                f.unlink()
        except Exception:
            pass

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
            self._drop_details_cache(app_id)
            logger.info(f"Wishlist: removed '{title}' (app_id={app_id})")
            return True
        return False

    # ---------- Импорт из Steam ----------

    def import_from_steam(self, items: List[Dict[str, Any]]) -> Tuple[int, int]:
        """Фаза 1 импорта: создаёт записи по списку из steam_get_wishlist.
        Детали (название/обложка) НЕ тянет — это делает fill_missing_details.

        ИДЕМПОТЕНТНО: уже существующие app_id пропускаются, приоритеты и
        правки пользователя не затираются. Возвращает (добавлено, пропущено)."""
        added = skipped = 0
        for it in items or []:
            app_id = str(it.get("appid", "")).strip()
            if not app_id:
                continue
            if app_id in self._items:
                skipped += 1
                continue
            ts = int(it.get("date_added") or 0)
            item = WishlistItem(app_id=app_id)
            item.priority = steam_priority_to_level(int(it.get("priority") or 0))
            if ts > 0:
                item.added_date = datetime.fromtimestamp(ts).isoformat()
            item.needs_details = True
            self._items[app_id] = item
            added += 1
        if added:
            self.save_sync()
        logger.info(f"Wishlist import: добавлено {added}, пропущено {skipped}")
        return added, skipped

    def pending_details_count(self) -> int:
        return sum(1 for it in self._items.values() if it.needs_details)

    def fill_missing_details(self, progress_cb=None, should_stop=None,
                             delay: float = 1.2) -> int:
        """Фаза 2: догружает детали импортированных записей ПО ОДНОЙ с паузой.

        Steam отдаёт детали только по одной игре за запрос (пакетный вызов даёт
        400) и лимитирует ~200 запросов/5 мин, поэтому пауза обязательна —
        иначе временный бан, который заденет и загрузку обложек библиотеки.
        Прерывается через should_stop(); недогруженные останутся с
        needs_details=True и подхватятся при следующем запуске.

        progress_cb(done, total, title) — для UI. Возвращает число обновлённых."""
        pending = [it.app_id for it in self._items.values() if it.needs_details]
        total = len(pending)
        done = 0
        for app_id in pending:
            if should_stop is not None and should_stop():
                logger.info(f"Wishlist details: остановлено на {done}/{total}")
                self.save_sync()      # зафиксировать то, что успели
                break
            item = self._items.get(app_id)
            if item is None:
                continue
            fresh = build_wishlist_item(app_id)
            if fresh is None:
                # Игра удалена из Steam / недоступна в обоих регионах —
                # снимаем флаг, чтобы не долбить её при каждом запуске.
                item.needs_details = False
                item.title = item.title or f"App {app_id}"
                logger.info(f"Wishlist details: нет данных для {app_id}, помечена")
            else:
                # Переносим полученные поля, СОХРАНЯЯ приоритет и дату из Steam.
                item.title = fresh.title
                item.header_image_url = fresh.header_image_url
                item.short_description = fresh.short_description
                item.store_url = fresh.store_url
                item.trailer_url = fresh.trailer_url
                item.trailer_url_hls = fresh.trailer_url_hls
                item.trailer_thumb_url = fresh.trailer_thumb_url
                item.release_date = fresh.release_date
                item.developers = fresh.developers
                item.genres = fresh.genres
                item.needs_details = False
            done += 1
            # Сохраняем пачками, а не после каждой игры: save_sync делает fsync,
            # и на списке в сотни игр это сотни принудительных сбросов растущего
            # JSON на диск. Раз в 10 записей (и обязательно в конце) достаточно —
            # потеря максимум 10 позиций при аварийном закрытии, они просто
            # догрузятся снова (needs_details остался True).
            if done % 10 == 0 or done == total:
                self.save_sync()
            if progress_cb is not None:
                try:
                    progress_cb(done, total, item.title)
                except Exception:
                    pass
            if delay > 0 and done < total:
                time.sleep(delay)
        return done

    def set_priority(self, app_id: str, level: str) -> Optional[str]:
        """Установить уровень приоритета (high/medium/low).
        Возвращает новое значение или None если app_id неизвестен / уровень невалиден."""
        if app_id not in self._items or level not in PRIORITY_LEVELS:
            return None
        item = self._items[app_id]
        item.priority = level
        self.save_sync()
        return item.priority

    def cycle_priority(self, app_id: str) -> Optional[str]:
        """Циклить low → medium → high → low. Используется как одиночный
        клик на огоньке карточки. Возвращает новое значение."""
        if app_id not in self._items:
            return None
        cycle_order = (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)
        item = self._items[app_id]
        cur = item.priority if item.priority in cycle_order else PRIORITY_LOW
        item.priority = cycle_order[(cycle_order.index(cur) + 1) % len(cycle_order)]
        self.save_sync()
        return item.priority

    # Старое API оставляем как shim — есть и в коде, и в гипотетических хуках.
    # Маппим бинарный toggle на high ↔ low (skip medium для бинарной семантики).
    def toggle_priority(self, app_id: str) -> Optional[bool]:
        if app_id not in self._items:
            return None
        item = self._items[app_id]
        item.priority = PRIORITY_LOW if item.priority == PRIORITY_HIGH else PRIORITY_HIGH
        self.save_sync()
        return item.priority == PRIORITY_HIGH

    def has(self, app_id: str) -> bool:
        return str(app_id) in self._items

    # ---------- Queries ----------

    def get_items(self) -> List[WishlistItem]:
        return list(self._items.values())

    def get_sorted(self, sort_key: str) -> List[WishlistItem]:
        items = list(self._items.values())
        if sort_key == self.SORT_PRIORITY:
            # priority rank (high=0, med=1, low=2) → новые сверху внутри группы.
            items.sort(key=lambda it: (
                _PRIORITY_RANK.get(it.priority, 2),
                -_iso_ts(it.added_date),
            ))
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
