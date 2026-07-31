"""
steam_owned.py — «Купленные, но не установленные» игры Steam.

Зачем отдельный модуль: SteamScanner (game_manager) видит ТОЛЬКО установленные
игры — он читает appmanifest_*.acf в steamapps. Список всего, что есть на
аккаунте, локально не лежит нигде в разбираемом виде (appcache — бинарь), его
приходится спрашивать у Steam по сети.

Источники:
  1. Web API IPlayerService/GetOwnedGames — ОСНОВНОЙ. Нужен бесплатный Web
     API-ключ (https://steamcommunity.com/dev/apikey). Даёт время в игре,
     дату последнего запуска и иконки.
  2. XML профиля сообщества (`/games?tab=all&xml=1`) — раньше работал без
     ключа, но ПРОВЕРЕНО 2026-07-26: Valve закрыла его, анонимному запросу
     теперь отдаётся HTML страницы входа (и для публичных профилей тоже).
     Оставлен фоллбеком на случай, если доступ вернут; при HTML-ответе тихо
     сдаётся, а UI просит завести ключ.

Web API отдаёт только то, что разрешено приватностью аккаунта: при закрытых
«Игровых подробностях» список будет пустым — это не сбой сети, и UI должен
сказать про приватность, а не про «Steam недоступен».

Результат кэшируется в data/steam_owned.json, чтобы раздел открывался мгновенно
и работал офлайн; обновление — по кнопке.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (CyberLauncher) SteamLibrary/1.0"

# Служебные приложения Steam, которые числятся «в библиотеке», но играми не
# являются. Те же id уже отсекает SteamScanner (Steamworks Redist, Proton …).
_NON_GAME_APPIDS = {"228980", "1070560", "1391110", "1493710", "1628350"}


@dataclass
class OwnedGame:
    """Игра, купленная на аккаунте Steam."""
    app_id: str
    title: str = ""
    playtime_min: int = 0          # всего минут в игре (0 — ни разу не играли)
    last_played: int = 0           # unix ts последнего запуска (0 — неизвестно)
    icon_url: str = ""             # мелкая иконка из Web API (может быть пустой)
    # Настоящий адрес обложки, если предсказуемый (старый) не работает —
    # см. header_url и resolve_header_url.
    header_override: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OwnedGame":
        valid = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in data.items() if k in valid})

    @property
    def header_url(self) -> str:
        """Обложка 460×215.

        Старая (предсказуемая) схема `…/steam/apps/<appid>/header.jpg` работает
        только для приложений, заведённых до перехода Steam на адреса с хэшем
        (`store_item_assets/steam/apps/<appid>/<hash>/header.jpg`). Хэш угадать
        нельзя, поэтому для новых игр настоящий адрес спрашивается у appdetails
        и кладётся в header_override (см. resolve_header_url)."""
        if self.header_override:
            return self.header_override
        return (f"https://cdn.cloudflare.steamstatic.com/steam/apps/"
                f"{self.app_id}/header.jpg")


# ---------------------------------------------------------------- локальный Steam

def steam_install_path() -> Optional[str]:
    """Путь к установленному Steam (реестр → стандартные папки)."""
    try:
        import winreg
        for hive, key_path, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    p = winreg.QueryValueEx(key, value)[0]
                if p and os.path.isdir(p.replace("/", os.sep)):
                    return p.replace("/", os.sep)
            except OSError:
                continue
    except Exception as e:
        logger.debug(f"Steam registry lookup failed: {e}")
    for cand in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(cand):
            return cand
    return None


def steam_library_dirs() -> List[Path]:
    """Все папки steamapps (основная + библиотеки с других дисков)."""
    base = steam_install_path()
    if not base:
        return []
    dirs = [Path(base) / "steamapps"]
    vdf = Path(base) / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        try:
            content = vdf.read_text(encoding="utf-8", errors="replace")
            for m in re.findall(r'"path"\s+"([^"]+)"', content):
                dirs.append(Path(m.replace("\\\\", "\\")) / "steamapps")
        except Exception as e:
            logger.warning(f"libraryfolders.vdf read failed: {e}")
    # dict.fromkeys — уникальные с сохранением порядка
    return list(dict.fromkeys(d for d in dirs if d.exists()))


def installed_app_ids() -> set:
    """appid всех УСТАНОВЛЕННЫХ игр — по наличию appmanifest_<id>.acf.

    Именно манифест, а не папка в common: при удалении Steam сносит манифест,
    но иногда оставляет огрызок папки (та же логика, что в
    GameManager.steam_install_present)."""
    ids = set()
    for lib in steam_library_dirs():
        try:
            for f in os.listdir(lib):
                m = re.match(r"^appmanifest_(\d+)\.acf$", f)
                if m:
                    ids.add(m.group(1))
        except OSError as e:
            logger.debug(f"Steam library dir unreadable ({lib}): {e}")
    return ids


# ---------------------------------------------------------------- сеть

def _http_get(url: str, timeout: float = 20.0, quiet: bool = False) -> Optional[bytes]:
    """GET → bytes. quiet=True — отказ не считается проблемой и пишется только
    в debug (так качаются обложки: у части приложений аккаунта header.jpg на
    CDN нет вовсе, и десятки штатных 404 засоряли бы launcher.log)."""
    log = logger.debug if quiet else logger.warning
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return r.read()
            log(f"Steam owned: HTTP {r.status} для {url.split('?')[0]}")
    except Exception as e:
        log(f"Steam owned: запрос не удался ({url.split('?')[0]}): {e}")
    return None


# Ссылка на страницу выдачи ключа — показывается в UI, когда ключа нет.
API_KEY_URL = "https://steamcommunity.com/dev/apikey"

ERR_NO_KEY = ("Нужен бесплатный Steam Web API-ключ: без него Valve не отдаёт "
              "список купленных игр (страница профиля с 2026 года требует "
              "входа даже для публичных аккаунтов).")
ERR_PRIVATE = ("Steam вернул пустой список. В настройках приватности Steam "
               "откройте «Игровые подробности» (Открытый профиль) и повторите.")
ERR_NETWORK = "Steam не ответил. Проверьте интернет, SteamID и API-ключ."


def resolve_header_url(app_id: str) -> str:
    """Настоящий адрес обложки из appdetails (filters=basic — самый лёгкий
    вариант запроса). '' если Steam не ответил или обложки нет.

    Нужен для приложений, у которых обложка лежит по адресу с хэшем: старый
    предсказуемый URL для них отдаёт 404, и карточка оставалась с заглушкой,
    хотя в детальном экране (он и так ходит в appdetails) картинка была."""
    url = (f"https://store.steampowered.com/api/appdetails"
           f"?appids={urllib.parse.quote(str(app_id))}&filters=basic&l=russian")
    raw = _http_get(url, timeout=12.0, quiet=True)
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return ""
    entry = data.get(str(app_id)) if isinstance(data, dict) else None
    if not (entry and entry.get("success")):
        return ""
    payload = entry.get("data")
    if not isinstance(payload, dict):
        return ""
    return (payload.get("header_image") or "").strip()


def fetch_owned_via_api(steamid64: str, api_key: str) -> Optional[List[OwnedGame]]:
    """IPlayerService/GetOwnedGames. None — ошибка запроса/ключа,
    [] — Steam ответил, но игр не отдал (закрытые игровые подробности)."""
    url = ("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
           f"?key={urllib.parse.quote(api_key)}"
           f"&steamid={urllib.parse.quote(steamid64)}"
           "&include_appinfo=1&include_played_free_games=1&format=json")
    raw = _http_get(url)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning(f"Steam owned: битый JSON от Web API: {e}")
        return None
    resp = (data or {}).get("response")
    if not isinstance(resp, dict):
        return None
    out: List[OwnedGame] = []
    for g in (resp.get("games") or []):
        app_id = str(g.get("appid") or "")
        if not app_id:
            continue
        icon_hash = g.get("img_icon_url") or ""
        out.append(OwnedGame(
            app_id=app_id,
            title=(g.get("name") or "").strip(),
            playtime_min=int(g.get("playtime_forever") or 0),
            last_played=int(g.get("rtime_last_played") or 0),
            icon_url=(f"https://media.steampowered.com/steamcommunity/public/"
                      f"images/apps/{app_id}/{icon_hash}.jpg" if icon_hash else ""),
        ))
    return out


def fetch_owned_via_profile(steamid64: str) -> Optional[List[OwnedGame]]:
    """XML публичного профиля — без Web API-ключа.

    None — сеть/профиль недоступны либо Valve отдала HTML вместо XML (сейчас
    так и происходит: эндпоинт требует входа). [] — профиль есть, но закрыт."""
    url = (f"https://steamcommunity.com/profiles/{urllib.parse.quote(steamid64)}"
           f"/games?tab=all&xml=1")
    raw = _http_get(url)
    if raw is None:
        return None
    if raw.lstrip()[:5].lower() == b"<!doc":
        # Страница входа вместо XML — эндпоинт закрыт для анонимных запросов.
        logger.info("Steam owned: games?xml=1 отдал HTML (нужен вход) — нужен API-ключ")
        return None
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logger.warning(f"Steam owned: XML профиля не разобран: {e}")
        return None
    # У закрытого профиля Steam отдаёт <response><error>…</error></response>
    err = root.find("error")
    if err is not None:
        logger.info(f"Steam owned: профиль закрыт ({(err.text or '').strip()[:80]})")
        return []
    out: List[OwnedGame] = []
    for node in root.findall(".//game"):
        app_id = (node.findtext("appID") or "").strip()
        if not app_id:
            continue
        # hoursOnRecord — строка с разделителем тысяч («1,234.5»), появляется
        # только если в игру играли.
        hours_raw = (node.findtext("hoursOnRecord") or "").replace(",", "").strip()
        try:
            minutes = int(round(float(hours_raw) * 60)) if hours_raw else 0
        except ValueError:
            minutes = 0
        out.append(OwnedGame(
            app_id=app_id,
            title=(node.findtext("name") or "").strip(),
            playtime_min=minutes,
        ))
    return out


def fetch_owned_games(steamid64: str, api_key: str = ""
                      ) -> Tuple[Optional[List[OwnedGame]], str, str]:
    """Список купленных игр. Возвращает (игры, источник, текст ошибки).

    Порядок: Web API (ключ есть — там же время в игре и дата запуска), затем
    XML профиля. games=None — ни один источник не ответил; games=[] — Steam
    ответил, но список пуст (приватность)."""
    sid = str(steamid64 or "").strip()
    if not (sid.isdigit() and len(sid) == 17):
        return None, "", "Неверный SteamID64 — нужно 17 цифр."
    empty_seen = False
    if api_key:
        games = fetch_owned_via_api(sid, api_key)
        if games:
            return games, "api", ""
        empty_seen = games is not None
        logger.info("Steam owned: Web API ничего не дал — пробуем публичный профиль")
    games = fetch_owned_via_profile(sid)
    if games:
        return games, "profile", ""
    if games == [] or empty_seen:
        return [], "profile" if games == [] else "api", ERR_PRIVATE
    return None, "", ERR_NETWORK if api_key else ERR_NO_KEY


# ---------------------------------------------------------------- менеджер

class SteamOwnedManager:
    """Кэш списка купленных игр + вычисление «куплено, но не установлено».

    Файл: data/steam_owned.json. Обложки — cache/steam_headers/<appid>.jpg
    (тот же приём, что в WishlistManager: Flutter держит картинки только в
    памяти сессии, без дискового кэша сотни обложек тянулись бы с CDN при
    каждом запуске)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.data_dir / "steam_owned.json"
        self._games: Dict[str, OwnedGame] = {}
        self.steamid: str = ""
        self.updated_at: str = ""
        self.source: str = ""
        # Игры, спрятанные пользователем (демо/софт/то, что ставить не собирается)
        self.hidden: set = set()
        self._header_cache_dir = self.data_dir.parent / "cache" / "steam_headers"
        self._header_lock = threading.Lock()
        self._header_inflight: set = set()
        self._installed_cache: Optional[set] = None
        self._installed_cache_ts: float = 0.0

    # ---------- persistence ----------

    def load(self) -> None:
        if not self.file.exists():
            return
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.steamid = str(data.get("steamid") or "")
            self.updated_at = str(data.get("updated_at") or "")
            self.source = str(data.get("source") or "")
            self.hidden = {str(x) for x in (data.get("hidden") or [])}
            for raw in (data.get("games") or []):
                try:
                    g = OwnedGame.from_dict(raw)
                    if g.app_id:
                        self._games[g.app_id] = g
                except Exception as e:
                    logger.warning(f"Skipping malformed owned game: {e}")
            logger.info(f"Steam owned: загружено {len(self._games)} игр из кэша")
        except Exception as e:
            logger.error(f"Steam owned load failed: {e}")

    def save_sync(self) -> bool:
        """Атомарная запись (tmp + os.replace) — как у library/wishlist."""
        try:
            payload = {
                "steamid": self.steamid,
                "updated_at": self.updated_at,
                "source": self.source,
                "hidden": sorted(self.hidden),
                "games": [g.to_dict() for g in self._games.values()],
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
            logger.error(f"Steam owned save failed: {e}")
            return False

    # ---------- данные ----------

    def refresh(self, steamid64: str, api_key: str = "") -> Dict[str, Any]:
        """Тянет список с серверов Steam и перезаписывает кэш.

        Возвращает {'ok', 'count', 'source', 'error'}. Пустой список при
        успешном ответе — почти всегда приватность профиля, а не сбой."""
        games, source, error = fetch_owned_games(steamid64, api_key)
        if not games:
            return {"ok": False, "count": 0, "source": source,
                    "error": error or ERR_NETWORK}
        self._games = {g.app_id: g for g in games if g.app_id not in _NON_GAME_APPIDS}
        self.steamid = str(steamid64)
        self.source = source
        self.updated_at = datetime.now().isoformat()
        self.save_sync()
        logger.info(f"Steam owned: получено {len(self._games)} игр (источник: {source})")
        return {"ok": True, "count": len(self._games), "source": source, "error": ""}

    def installed_ids(self, max_age: float = 5.0) -> set:
        """appid установленных игр с коротким кэшем — метод зовётся на каждый
        рендер списка, а это обход папок steamapps."""
        now = time.time()
        if self._installed_cache is None or now - self._installed_cache_ts > max_age:
            self._installed_cache = installed_app_ids()
            self._installed_cache_ts = now
        return self._installed_cache

    def invalidate_installed(self) -> None:
        self._installed_cache = None

    def all_games(self) -> List[OwnedGame]:
        return list(self._games.values())

    def not_installed(self, include_hidden: bool = False) -> List[OwnedGame]:
        """Куплено, но не установлено — по алфавиту."""
        installed = self.installed_ids()
        out = [g for g in self._games.values()
               if g.app_id not in installed
               and (include_hidden or g.app_id not in self.hidden)]
        out.sort(key=lambda g: (g.title or "").lower())
        return out

    def counts(self) -> Dict[str, int]:
        installed = self.installed_ids()
        owned = len(self._games)
        have = sum(1 for a in self._games if a in installed)
        return {"owned": owned, "installed": have,
                "not_installed": owned - have,
                "hidden": len(self.hidden & set(self._games))}

    def sort_games(self, games: List[OwnedGame], key: str) -> List[OwnedGame]:
        if key == "playtime_desc":
            return sorted(games, key=lambda g: (-g.playtime_min, (g.title or "").lower()))
        if key == "last_played":
            # Никогда не запускавшиеся — в конец, а не в начало.
            return sorted(games, key=lambda g: (-(g.last_played or 0),
                                                (g.title or "").lower()))
        if key == "never_played":
            return sorted(games, key=lambda g: (g.playtime_min, (g.title or "").lower()))
        return sorted(games, key=lambda g: (g.title or "").lower())

    @staticmethod
    def matches_query(g: OwnedGame, q: str) -> bool:
        q = (q or "").strip().lower()
        if not q:
            return True
        return q in (g.title or "").lower() or q == g.app_id

    def toggle_hidden(self, app_id: str) -> bool:
        """Скрыть/вернуть игру в списке. True — теперь скрыта."""
        app_id = str(app_id)
        if app_id in self.hidden:
            self.hidden.discard(app_id)
            hidden_now = False
        else:
            self.hidden.add(app_id)
            hidden_now = True
        self.save_sync()
        return hidden_now

    def unhide_all(self) -> int:
        n = len(self.hidden)
        if n:
            self.hidden.clear()
            self.save_sync()
        return n

    # ---------- обложки ----------

    def header_src(self, game: OwnedGame) -> str:
        """Локальный файл, если обложка уже скачана, иначе URL CDN."""
        path = self._header_cache_dir / f"{game.app_id}.jpg"
        try:
            if path.exists() and path.stat().st_size > 512:
                return str(path)
        except OSError:
            pass
        return game.header_url

    def cache_headers_async(self, games: List[OwnedGame], limit: int = 40,
                            on_ready=None) -> None:
        """Фоново докачивает обложки. on_ready(list[app_id]) — вызывается по
        завершении со списком игр, у которых обложка ПОЯВИЛАСЬ (чтобы UI мог
        подменить именно эти карточки, а не пересобирать весь список)."""
        todo = []
        for g in games[:limit]:
            path = self._header_cache_dir / f"{g.app_id}.jpg"
            try:
                if path.exists() and path.stat().st_size > 512:
                    continue
            except OSError:
                pass
            with self._header_lock:
                if g.app_id in self._header_inflight:
                    continue
                self._header_inflight.add(g.app_id)
            todo.append((g.app_id, g.header_url))
        if not todo:
            return
        threading.Thread(target=self._download_headers, args=(todo, on_ready),
                         daemon=True).start()

    def _download_headers(self, todo: List[Tuple[str, str]], on_ready=None) -> None:
        self._header_cache_dir.mkdir(parents=True, exist_ok=True)
        fetched: List[str] = []
        missing = 0
        resolved = 0
        for app_id, url in todo:
            try:
                # quiet: отсутствие обложки — штатная ситуация, не ошибка.
                data = _http_get(url, timeout=15.0, quiet=True) if url else None
                if not data:
                    # Старый предсказуемый адрес не сработал → у приложения
                    # обложка лежит по пути с хэшем. Спрашиваем настоящий URL
                    # и запоминаем его, чтобы в следующий раз идти сразу туда.
                    new_url = resolve_header_url(app_id)
                    if new_url and new_url != url:
                        data = _http_get(new_url, timeout=15.0, quiet=True)
                        if data and len(data) > 512:
                            game = self._games.get(app_id)
                            if game is not None:
                                game.header_override = new_url
                            resolved += 1
                    # appdetails лимитирован (~200 запросов / 5 мин) — не частим.
                    time.sleep(1.0)
                if data and len(data) > 512:
                    tmp = self._header_cache_dir / f"{app_id}.jpg.tmp"
                    tmp.write_bytes(data)
                    os.replace(tmp, self._header_cache_dir / f"{app_id}.jpg")
                    fetched.append(app_id)
                else:
                    missing += 1
            except Exception as e:
                logger.debug(f"Steam header cache failed for {app_id}: {e}")
            finally:
                with self._header_lock:
                    self._header_inflight.discard(app_id)
        if resolved:
            self.save_sync()        # сохранить найденные адреса обложек
        if fetched or missing:
            logger.info(
                f"Steam owned: обложек закэшировано: {len(fetched)}"
                + (f" (адрес найден через appdetails: {resolved})" if resolved else "")
                + (f", без обложки: {missing}" if missing else ""))
        if fetched and on_ready is not None:
            try:
                on_ready(fetched)
            except Exception as e:
                logger.debug(f"Steam owned: on_ready failed: {e}")
