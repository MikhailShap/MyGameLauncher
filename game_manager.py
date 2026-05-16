"""
Game Manager - Backend ядро для Game Launcher
==============================================
VERSION 6.0 (ANTI-BAN & NEW API)
- Переход на новую библиотеку поиска (ddgs/duckduckgo_search актуальной версии).
- Добавлена задержка (sleep) между запросами, чтобы избежать ошибки 403 Ratelimit.
- Расширен список CDN ссылок Steam для повышения шансов найти обложку.
"""

import os
import sys
import re
import json
import asyncio
import ctypes
import hashlib
import logging
import subprocess
import urllib.request
import urllib.parse
import time  # <--- Добавлено для пауз
import random # <--- Для случайных пауз
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import tempfile

# Windows-specific imports
import winreg


def _get_processes_with_exe_in_dir(dir_path: str) -> List[int]:
    """Перечисляет процессы Windows и возвращает PID-ы тех, чьё .exe лежит
    внутри указанной директории. Используется в watcher-е Steam-игр чтобы
    определить когда игра завершилась.

    Реализация через CreateToolhelp32Snapshot + Process32FirstW + OpenProcess
    + QueryFullProcessImageNameW. Не требует прав админа, не зависит от psutil."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD),
            ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD),
            ('th32DefaultHeapID', ctypes.c_void_p),
            ('th32ModuleID', wintypes.DWORD),
            ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD),
            ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', wintypes.DWORD),
            ('szExeFile', wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]

    target = os.path.normpath(dir_path).lower().rstrip('\\') + '\\'
    matches: List[int] = []

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(pe)):
            return []
        while True:
            pid = pe.th32ProcessID
            if pid:
                h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        path = os.path.normpath(buf.value).lower()
                        if path.startswith(target):
                            matches.append(pid)
                    kernel32.CloseHandle(h)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(pe)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return matches


def get_app_data_dir() -> Path:
    """Папка для пользовательских данных (логи, кэш, библиотека, settings).

    В PyInstaller-сборке (frozen) приложение устанавливается в Program Files
    (read-only без UAC), поэтому писать туда нельзя — используем
    %APPDATA%\\CyberLauncher\\. В dev-режиме (запуск python main.py) —
    текущая рабочая папка, чтобы файлы лежали рядом с исходниками."""
    if getattr(sys, 'frozen', False):
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        d = Path(base) / 'CyberLauncher'
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path('.').resolve()


# Настройка логирования (must be before any code that uses logger)
log_file = str(get_app_data_dir() / "launcher.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("GameManager")
logger.info(f"App data dir: {get_app_data_dir()}")

try:
    import win32com.client
except ImportError as e:
    logger.error(f"Failed to import win32com: {e}")

# Попытка импорта новой библиотеки
try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
    logger.info("DuckDuckGo search library loaded successfully")
except ImportError as e:
    HAS_DDG = False
    logger.warning(f"DuckDuckGo search not available: {e}")

try:
    import icoextract
    HAS_ICOEXTRACT = True
except ImportError:
    HAS_ICOEXTRACT = False



class Platform(Enum):
    STEAM = "Steam"
    EPIC = "Epic Games"
    SYSTEM = "System"


class Category(Enum):
    ALL = "All"
    FAVORITES = "Favorites"


@dataclass
class GameModel:
    uid: str
    title: str
    exe_path: str
    icon_path: Optional[str] = None
    platform: str = Platform.SYSTEM.value
    category: str = Category.ALL.value
    app_id: Optional[str] = None
    install_path: Optional[str] = None
    last_played: Optional[str] = None
    play_time: int = 0
    is_favorite: bool = False
    added_date: str = field(default_factory=lambda: datetime.now().isoformat())
    collections: List[str] = field(default_factory=list)  # Список ID коллекций
    # Запускать с правами администратора (через UAC). Нужно некоторым играм
    # вроде Crash Bandicoot 4 — иначе DXGI_ERROR_INVALID_CALL при старте.
    run_as_admin: bool = False

    @staticmethod
    def generate_uid(path: str) -> str:
        return hashlib.md5(path.lower().encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GameModel':
        # Игнорируем неизвестные поля — на случай если в старой library.json
        # есть поля которые мы убрали; и наоборот, новые поля получают defaults
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class IconExtractor:
    """Умный загрузчик изображений с защитой от банов"""

    def __init__(self, cache_dir: str = "./cache/icons"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 2 потока даёт x2 ускорение при первом сканировании, при этом
        # rate-limit и time.sleep между внутренними запросами всё ещё работают
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._search_cache = {}

    def shutdown(self, wait: bool = False):
        """Корректно останавливает thread pool. Вызывается при выходе из приложения."""
        try:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        except Exception as e:
            logger.warning(f"IconExtractor shutdown error: {e}")

    def _get_cache_path(self, identifier: str) -> Path:
        uid = hashlib.md5(identifier.lower().encode()).hexdigest()[:12]
        return self.cache_dir / f"{uid}.jpg"

    def _clean_name(self, name: str) -> str:
        """Enhanced game name cleaning with year/tag removal"""
        n = name.replace('.', ' ').replace('_', ' ')

        # Expanded junk patterns
        junk = [
            # Release group tags
            r'TENOKE', r'RUNE', r'DODI', r'FitGirl', r'Repack', r'EMPRESS',
            r'CODEX', r'SKIDROW', r'CPY', r'PLAZA', r'HOODLUM', r'RAZOR1911',
            r'TiNYiSO', r'PROPHET', r'DARKSiDERS', r'ANOMALY', r'SiMPLEX',

            # Platform/Store tags
            r'GOG', r'Portable', r'Steam', r'Epic',

            # Version patterns
            r'v\d+(\.\d+)*[a-z]?', r'Build\s*\d+', r'Update\s*\d+',

            # Technical tags
            r'DX11', r'DX12', r'x64', r'x86', r'Multi\d+', r'DLC',
            r'VR', r'HDR', r'4K', r'UHD',

            # Edition tags
            r'GOTY', r'Game of the Year',
            r'Enhanced Edition', r'Definitive Edition', r'Complete Edition',
            r'Ultimate Edition', r'Deluxe Edition', r'Premium Edition',
            r'Gold Edition', r'Legendary Edition', r'Anniversary Edition',

            # Status tags
            r'Early Access', r'Demo', r'Alpha', r'Beta', r'Preview',
        ]

        for j in junk:
            n = re.sub(rf'\b{j}\b', '', n, flags=re.IGNORECASE)

        # Remove bracketed content [ANY TEXT]
        n = re.sub(r'\[.*?\]', '', n)

        # Remove parentheses with years (1990-2099)
        n = re.sub(r'\((?:19|20)\d{2}\)', '', n)

        # Remove other parentheses content
        n = re.sub(r'\(.*?\)', '', n)

        # Remove common separators at start/end
        n = re.sub(r'^[\s\-–—_.:]+|[\s\-–—_.:]+$', '', n)

        # Collapse multiple spaces
        return ' '.join(n.split())

    def _download_file(self, url: str, save_path: Path) -> bool:
        """Скачивание"""
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = response.read()
                    if len(data) > 2048:
                        with open(save_path, 'wb') as f:
                            f.write(data)
                        return True
        except Exception:
            pass
        return False

    def _search_steam_id_by_name(self, name: str) -> Optional[str]:
        """Поиск ID в Steam"""
        clean_name = self._clean_name(name)
        if len(clean_name) < 2: return None
        
        if clean_name in self._search_cache:
            return self._search_cache[clean_name]

        print(f"🔎 Ищем в Steam: '{clean_name}'")

        try:
            query = urllib.parse.quote(clean_name)
            url = f"https://store.steampowered.com/api/storesearch/?term={query}&l=english&cc=US"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as res:
                data = json.load(res)
                if data.get('total', 0) > 0:
                    appid = str(data['items'][0]['id'])
                    self._search_cache[clean_name] = appid
                    print(f"   ✅ Найдено ID: {appid}")
                    return appid
        except:
            pass
        
        # Небольшая пауза, чтобы не спамить API Steam (оптимизировано)
        time.sleep(0.2)
        
        self._search_cache[clean_name] = None
        return None

    def _download_steam_cover(self, app_id: str, save_path: Path) -> bool:
        """Качает обложку Steam (расширенный список ссылок)"""
        urls = [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900.jpg",
            f"https://steamcdn-a.akamaihd.net/steam/apps/{app_id}/library_600x900.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/capsule_616x353.jpg" # Fallback
        ]
        for url in urls:
            if self._download_file(url, save_path):
                return True
        return False

    def _search_duckduckgo(self, name: str, save_path: Path) -> bool:
        """Поиск в DDG с задержкой (Anti-Ban)"""
        if not HAS_DDG:
            logger.warning("   DuckDuckGo недоступен (библиотека не загружена)")
            return False
        clean_name = self._clean_name(name)
        
        # ВАЖНО: Пауза перед запросом, чтобы избежать 403 (оптимизировано)
        delay = random.uniform(0.3, 0.6)
        logger.info(f"   🦆 Ищем в DDG: '{clean_name}'")
        time.sleep(delay)
        
        try:
            query = f"{clean_name} game box art"
            with DDGS() as ddgs:
                # Ищем 3 результата для большей вероятности успеха
                results = list(ddgs.images(query, max_results=3))
                logger.info(f"   DDG нашёл {len(results)} изображений")
                for res in results:
                    if self._download_file(res['image'], save_path):
                        logger.info(f"   ✅ Скачано через DDG")
                        return True
                logger.warning(f"   DDG: не удалось скачать ни одно изображение")
        except Exception as e:
            logger.error(f"   ❌ Ошибка DDG: {e}")
        return False

    def _extract_exe_icon(self, exe_path: str, save_path: Path) -> bool:
        if not HAS_ICOEXTRACT or not exe_path: return False
        try:
            extractor = icoextract.IconExtractor(exe_path)
            if extractor.get_icon_count() > 0:
                data = extractor.get_icon(0)
                with open(save_path, 'wb') as f:
                    f.write(data)
                return True
        except: pass
        return False

    async def get_icon(self, game_title: str, app_id: str = None, exe_path: str = None) -> Optional[str]:
        key_id = app_id if app_id else hashlib.md5(self._clean_name(game_title).encode()).hexdigest()
        cache_path = self._get_cache_path(str(key_id))
        
        if cache_path.exists() and cache_path.stat().st_size > 1000:
            return str(cache_path)
        
        loop = asyncio.get_event_loop()
        
        def process():
            # 1. Steam ID (Точно)
            if app_id:
                if self._download_steam_cover(app_id, cache_path):
                    return str(cache_path)

            # 2. Поиск Steam ID (Для пираток)
            found_id = self._search_steam_id_by_name(game_title)
            if found_id:
                if self._download_steam_cover(found_id, cache_path):
                    return str(cache_path)

            # 3. DuckDuckGo (Для консолей/редких игр)
            if self._search_duckduckgo(game_title, cache_path):
                return str(cache_path)

            # 4. EXE
            if exe_path:
                if self._extract_exe_icon(exe_path, cache_path):
                    return str(cache_path)
            
            return None

        return await loop.run_in_executor(self._executor, process)


class SteamGridDBClient:
    """SteamGridDB API v2 Client для высококачественных обложек"""

    BASE_URL = "https://www.steamgriddb.com/api/v2"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.session_cache = {}
        self._last_request_time = 0
        self._min_delay = 0.25  # Rate limit: 4 req/sec (оптимизировано)

    def _wait_rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_delay:
            time.sleep(self._min_delay - elapsed)
        self._last_request_time = time.time()

    def _make_request(self, endpoint: str) -> Optional[dict]:
        """Make authenticated API request"""
        if not self.api_key:
            return None

        self._wait_rate_limit()

        url = f"{self.BASE_URL}/{endpoint}"

        try:
            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'CyberLauncher/1.0'
            })

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                logger.error("SteamGridDB API Unauthorized (Invalid Key). Disabling API.")
                self.api_key = None # Disable to prevent further errors
            else:
                logger.warning(f"SteamGridDB API error: {e}")
        except Exception as e:
            logger.warning(f"SteamGridDB API error: {e}")

        return None

    def validate_key(self) -> tuple[bool, str]:
        """Validate API key. Returns (success, message)"""
        if not self.api_key:
            return False, "Ключ не указан"
        
        try:
            url = f"{self.BASE_URL}/search/autocomplete/portal"
            req = urllib.request.Request(url, headers={
                'Authorization': f'Bearer {self.api_key}',
                'User-Agent': 'CyberLauncher/1.0'
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return True, "✅ Ключ валидный"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "❌ Неверный ключ (401)"
            return False, f"❌ Ошибка: {e.code}"
        except Exception as e:
            return False, f"❌ Ошибка: {e}"
        return False, "❌ Неизвестная ошибка"

    def get_grids_by_steam_id(self, steam_app_id: str) -> List[str]:
        """Get grid images by Steam App ID"""
        cache_key = f"steam_{steam_app_id}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]

        data = self._make_request(f"grids/steam/{steam_app_id}")

        urls = []
        if data and data.get('success') and data.get('data'):
            grids = data['data']
            # Prefer 600x900 vertical grids
            for grid in grids:
                if grid.get('height') == 900 and grid.get('width') == 600:
                    urls.append(grid['url'])
            # Fallback to any grid
            if not urls:
                urls = [g['url'] for g in grids[:3]]

        self.session_cache[cache_key] = urls
        return urls

    def search_game(self, game_name: str) -> Optional[str]:
        """Search game by name, return first game ID"""
        clean_name = urllib.parse.quote(game_name)
        data = self._make_request(f"search/autocomplete/{clean_name}")

        if data and data.get('success') and data.get('data'):
            games = data['data']
            if games:
                return str(games[0]['id'])

        return None

    def get_grids_by_game_id(self, game_id: str) -> List[str]:
        """Get grid images by SteamGridDB game ID"""
        cache_key = f"sgdb_{game_id}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]

        data = self._make_request(f"grids/game/{game_id}")

        urls = []
        if data and data.get('success') and data.get('data'):
            grids = data['data']
            for grid in grids:
                if grid.get('height') == 900 and grid.get('width') == 600:
                    urls.append(grid['url'])
            if not urls:
                urls = [g['url'] for g in grids[:3]]

        self.session_cache[cache_key] = urls
        return urls

    def get_heroes_by_steam_id(self, steam_app_id: str) -> List[str]:
        """Heroes — landscape-арт SteamGridDB специально для фонов лаунчеров.
        Возвращает URL-ы, отсортированные с приоритетом 1920x620."""
        cache_key = f"sgdb_hero_steam_{steam_app_id}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]
        data = self._make_request(f"heroes/steam/{steam_app_id}")
        urls = []
        if data and data.get('success') and data.get('data'):
            heroes = data['data']
            # Предпочитаем 1920x620 (стандарт hero), иначе любые
            preferred = [h['url'] for h in heroes
                         if h.get('width') == 1920 and h.get('height') == 620]
            others = [h['url'] for h in heroes if h.get('url') not in preferred]
            urls = preferred + others[:3]
        self.session_cache[cache_key] = urls
        return urls

    def get_heroes_by_game_id(self, game_id: str) -> List[str]:
        """Heroes по SteamGridDB-id (для не-Steam игр через search_game)."""
        cache_key = f"sgdb_hero_game_{game_id}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]
        data = self._make_request(f"heroes/game/{game_id}")
        urls = []
        if data and data.get('success') and data.get('data'):
            heroes = data['data']
            preferred = [h['url'] for h in heroes
                         if h.get('width') == 1920 and h.get('height') == 620]
            others = [h['url'] for h in heroes if h.get('url') not in preferred]
            urls = preferred + others[:3]
        self.session_cache[cache_key] = urls
        return urls


class RAWGClient:
    """RAWG.io API Client для метаданных игр"""

    BASE_URL = "https://api.rawg.io/api"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.session_cache = {}
        self._last_request_time = 0
        self._min_delay = 0.1  # 10 req/sec safe limit

    def _wait_rate_limit(self):
        """Enforce rate limiting"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_delay:
            time.sleep(self._min_delay - elapsed)
        self._last_request_time = time.time()

    def validate_key(self) -> tuple[bool, str]:
        """Validate API key. Returns (success, message)"""
        if not self.api_key:
            return False, "Ключ не указан"
        
        try:
            params = urllib.parse.urlencode({'key': self.api_key, 'page_size': '1'})
            url = f"{self.BASE_URL}/games?{params}"
            req = urllib.request.Request(url, headers={'User-Agent': 'CyberLauncher/1.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    return True, "✅ Ключ валидный"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "❌ Неверный ключ (401)"
            return False, f"❌ Ошибка: {e.code}"
        except Exception as e:
            return False, f"❌ Ошибка: {e}"
        return False, "❌ Неизвестная ошибка"

    def search_game(self, game_name: str) -> Optional[str]:
        """Search for game and return background image URL"""
        if not self.api_key:
            return None

        cache_key = f"rawg_{game_name.lower()}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key]

        self._wait_rate_limit()

        params = urllib.parse.urlencode({
            'key': self.api_key,
            'search': game_name,
            'search_precise': 'true',
            'page_size': '1'
        })

        url = f"{self.BASE_URL}/games?{params}"

        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'CyberLauncher/1.0'
            })

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read())
                    if data and data.get('results'):
                        game = data['results'][0]
                        image_url = game.get('background_image')
                        self.session_cache[cache_key] = image_url
                        return image_url
        except Exception as e:
            logger.warning(f"RAWG API error: {e}")

        self.session_cache[cache_key] = None
        return None

    def get_screenshots_by_name(self, game_name: str, limit: int = 5) -> List[str]:
        """Поиск игры по названию → загрузка её скриншотов. Возвращает URL'ы.
        Используется в BigPicture для cycling-фона нон-Steam игр."""
        if not self.api_key:
            return []

        cache_key = f"rawg_shots_{game_name.lower()}"
        if cache_key in self.session_cache:
            return self.session_cache[cache_key] or []

        # 1) Найти id игры
        self._wait_rate_limit()
        params = urllib.parse.urlencode({
            'key': self.api_key,
            'search': game_name,
            'search_precise': 'true',
            'page_size': '1',
        })
        url = f"{self.BASE_URL}/games?{params}"
        game_id = None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CyberLauncher/1.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read())
                    if data and data.get('results'):
                        game_id = data['results'][0].get('id')
        except Exception as e:
            logger.warning(f"RAWG screenshots search error: {e}")

        if not game_id:
            self.session_cache[cache_key] = []
            return []

        # 2) Скриншоты по id
        self._wait_rate_limit()
        params = urllib.parse.urlencode({'key': self.api_key, 'page_size': str(limit)})
        url = f"{self.BASE_URL}/games/{game_id}/screenshots?{params}"
        urls: List[str] = []
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CyberLauncher/1.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read())
                    for item in (data.get('results') or [])[:limit]:
                        img = item.get('image')
                        if img:
                            urls.append(img)
        except Exception as e:
            logger.warning(f"RAWG screenshots fetch error: {e}")

        self.session_cache[cache_key] = urls
        return urls


class CoverValidator:
    """Валидация и очистка кэша иконок"""

    def __init__(self, cache_dir: Path, library_file: Path):
        self.cache_dir = cache_dir
        self.library_file = library_file

    def validate_cache_file(self, file_path: str) -> bool:
        """Check if cache file exists and is valid"""
        if not file_path:
            return False

        path = Path(file_path)

        # Check existence
        if not path.exists():
            return False

        # Check minimum size (< 2KB = likely invalid)
        try:
            if path.stat().st_size < 2048:
                return False
        except:
            return False

        # Skip expensive PIL verification for speed - file size check is enough
        return True

    def cleanup_orphaned_cache(self) -> tuple:
        """Remove cache files not referenced in library.json"""
        if not self.library_file.exists():
            return (0, 0)

        try:
            with open(self.library_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                referenced_files = set()

                for game in data.get('games', []):
                    icon_path = game.get('icon_path')
                    if icon_path:
                        referenced_files.add(Path(icon_path).name)
        except:
            return (0, 0)

        removed = 0
        total = 0

        if self.cache_dir.exists():
            for cache_file in self.cache_dir.glob('*.jpg'):
                total += 1
                if cache_file.name not in referenced_files:
                    try:
                        cache_file.unlink()
                        removed += 1
                        logger.info(f"Removed orphaned cache: {cache_file.name}")
                    except:
                        pass

            for cache_file in self.cache_dir.glob('*.png'):
                total += 1
                if cache_file.name not in referenced_files:
                    try:
                        cache_file.unlink()
                        removed += 1
                        logger.info(f"Removed orphaned cache: {cache_file.name}")
                    except:
                        pass

        return (removed, total)

    def repair_library_references(self, games: List['GameModel']) -> int:
        """Fix library entries with missing cache files"""
        repaired = 0

        for game in games:
            if game.icon_path and not self.validate_cache_file(game.icon_path):
                logger.info(f"Invalidated missing cache for: {game.title}")
                game.icon_path = None
                repaired += 1

        return repaired


class CoverAPIManager:
    """Orchestrates all cover art APIs with 8-tier fallback"""

    def __init__(self, cache_dir: Path, sgdb_key: str = None, rawg_key: str = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sgdb = SteamGridDBClient(sgdb_key) if sgdb_key else None
        self.rawg = RAWGClient(rawg_key) if rawg_key else None
        self.icon_extractor = IconExtractor(str(self.cache_dir))

        if sgdb_key:
            logger.info("SteamGridDB API initialized")
        if rawg_key:
            logger.info("RAWG.io API initialized")

    def _download_image(self, url: str, save_path: Path) -> bool:
        """Download image from URL"""
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    data = response.read()
                    if len(data) > 2048:
                        with open(save_path, 'wb') as f:
                            f.write(data)
                        return True
        except Exception as e:
            logger.debug(f"Download failed from {url}: {e}")
        return False

    
    def get_cover(self, game_title: str, app_id: str = None, exe_path: str = None) -> Tuple[Optional[str], str]:
        """Main cover retrieval with 7-tier fallback. Returns (path, source)"""
        clean_name = self.icon_extractor._clean_name(game_title)
        key_id = app_id if app_id else hashlib.md5(clean_name.encode()).hexdigest()
        cache_path = self.cache_dir / f"{hashlib.md5(str(key_id).lower().encode()).hexdigest()[:12]}.jpg"

        # Tier 1: Cache (already validated by caller, but if we are here, we are fetching new)

        # Tier 2: Steam Direct CDN (fast, free, no API key needed)
        if app_id:
            logger.info(f"[Tier 2] Steam CDN: {app_id}")
            if self.icon_extractor._download_steam_cover(app_id, cache_path):
                logger.info(f"   ✅ Downloaded from Steam CDN")
                return (str(cache_path), "Steam CDN")

        # Tier 3: Steam Store Search → CDN (find ID by name, then download)
        logger.info(f"[Tier 3] Steam Store Search: '{clean_name}'")
        found_id = self.icon_extractor._search_steam_id_by_name(game_title)
        if found_id:
            if self.icon_extractor._download_steam_cover(found_id, cache_path):
                logger.info(f"   ✅ Downloaded from Steam Store")
                return (str(cache_path), "Steam Store")

        # Tier 4: SteamGridDB by Steam App ID (higher quality, needs API key)
        if app_id and self.sgdb:
            logger.info(f"[Tier 4] SteamGridDB by ID: {app_id}")
            urls = self.sgdb.get_grids_by_steam_id(app_id)
            for url in urls:
                if self._download_image(url, cache_path):
                    logger.info(f"   ✅ Downloaded from SteamGridDB")
                    return (str(cache_path), "SteamGridDB")

        # Tier 5: RAWG.io Search
        if self.rawg:
            logger.info(f"[Tier 5] RAWG.io: '{clean_name}'")
            image_url = self.rawg.search_game(clean_name)
            if image_url:
                if self._download_image(image_url, cache_path):
                    logger.info(f"   ✅ Downloaded from RAWG.io")
                    return (str(cache_path), "RAWG.io")

        # Tier 6: SteamGridDB by Name
        if self.sgdb:
            logger.info(f"[Tier 6] SteamGridDB by Name: '{clean_name}'")
            game_id = self.sgdb.search_game(clean_name)
            if game_id:
                urls = self.sgdb.get_grids_by_game_id(game_id)
                for url in urls:
                    if self._download_image(url, cache_path):
                        logger.info(f"   ✅ Downloaded from SteamGridDB")
                        return (str(cache_path), "SteamGridDB")

        # Tier 7: DuckDuckGo (last resort for images)
        logger.info(f"[Tier 7] DuckDuckGo: '{clean_name}'")
        if self.icon_extractor._search_duckduckgo(game_title, cache_path):
            logger.info(f"   ✅ Downloaded from DuckDuckGo")
            return (str(cache_path), "DuckDuckGo")

        # Tier 8: EXE Icon
        if exe_path:
            logger.info(f"[Tier 8] EXE Icon Extraction")
            if self.icon_extractor._extract_exe_icon(exe_path, cache_path):
                logger.info(f"   ✅ Extracted from EXE")
                return (str(cache_path), "EXE Icon")

        logger.warning(f"   ❌ All tiers failed for: {game_title}")
        return (None, "None")


class CoverUploader:
    """Handles manual cover art uploads"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def upload_from_file(self, game_uid: str, source_path: str) -> Optional[str]:
        """Upload cover from local file"""
        try:
            from PIL import Image

            source = Path(source_path)
            if not source.exists():
                return None

            # Validate it's an image
            try:
                img = Image.open(source)
                img.verify()
            except:
                logger.error(f"Invalid image file: {source_path}")
                return None

            # Generate cache filename
            cache_name = hashlib.md5(game_uid.encode()).hexdigest()[:12] + ".jpg"
            cache_path = self.cache_dir / cache_name

            # Re-open after verify and convert
            img = Image.open(source)

            # Convert RGBA to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (0, 0, 0))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1])
                else:
                    background.paste(img)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # Resize if too large (max 1200x1800)
            max_size = (1200, 1800)
            if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)

            # Save as JPEG
            img.save(cache_path, 'JPEG', quality=90)

            logger.info(f"Uploaded cover for game {game_uid}: {cache_path}")
            return str(cache_path)

        except Exception as e:
            logger.error(f"Cover upload failed: {e}")
            return None

    def upload_from_url(self, game_uid: str, url: str) -> Optional[str]:
        """Upload cover from URL"""
        try:
            # Validate URL
            if not url.startswith(('http://', 'https://')):
                return None

            # Download to temp location
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })

            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    data = response.read()

                    # Save to temp file
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
                        tmp.write(data)
                        tmp_path = tmp.name

                    # Use upload_from_file to process
                    result = self.upload_from_file(game_uid, tmp_path)

                    # Cleanup temp
                    try:
                        Path(tmp_path).unlink()
                    except:
                        pass

                    return result
        except Exception as e:
            logger.error(f"Cover upload from URL failed: {e}")
        return None


class SteamScanner:
    async def scan(self, cover_manager: 'CoverAPIManager', excluded_paths: List[str] = None) -> List[GameModel]:
        return await asyncio.to_thread(self.scan_sync, cover_manager, excluded_paths)

    def scan_sync(self, cover_manager: 'CoverAPIManager', excluded_paths: List[str] = None) -> List[GameModel]:
        logger.info("Starting Steam scan...")
        games = []
        excluded = set(str(Path(p).resolve()).lower() for p in (excluded_paths or []))
        
        steam_path = None
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")
            steam_path = winreg.QueryValueEx(key, "InstallPath")[0]
            winreg.CloseKey(key)
        except:
            if os.path.exists(r"C:\Program Files (x86)\Steam"):
                steam_path = r"C:\Program Files (x86)\Steam"

        if not steam_path:
            return []

        lib_paths = [os.path.join(steam_path, "steamapps")]
        vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            try:
                with open(vdf, 'r', encoding='utf-8') as f:
                    content = f.read()
                    matches = re.findall(r'"path"\s+"([^"]+)"', content)
                    for m in matches:
                        p = m.replace("\\\\", "\\")
                        lib_paths.append(os.path.join(p, "steamapps"))
            except:
                pass

        for lib in set(lib_paths):
            if not os.path.exists(lib):
                continue
            for f in os.listdir(lib):
                if f.startswith("appmanifest_") and f.endswith(".acf"):
                    try:
                        with open(os.path.join(lib, f), 'r', encoding='utf-8') as af:
                            data = af.read()
                            name = re.search(r'"name"\s+"([^"]+)"', data)
                            appid = re.search(r'"appid"\s+"(\d+)"', data)
                            install = re.search(r'"installdir"\s+"([^"]+)"', data)

                            if name and appid:
                                n = name.group(1)
                                aid = appid.group(1)
                                idir = install.group(1) if install else ""
                                if aid in ['228980', '1070560', '1391110']:
                                    continue
                                full_path = os.path.join(lib, "common", idir)
                                
                                # EXCLUSION CHECK
                                if str(Path(full_path).resolve()).lower() in excluded:
                                    logger.info(f"Skipping excluded Steam game: {n}")
                                    continue
                                
                                # OPTIMIZATION: Check cache first before API calls
                                cache_key = hashlib.md5(aid.lower().encode()).hexdigest()[:12]
                                cache_path = cover_manager.cache_dir / f"{cache_key}.jpg"
                                if cache_path.exists() and cache_path.stat().st_size > 2048:
                                    icon = str(cache_path)
                                else:
                                    icon, _ = cover_manager.get_cover(n, app_id=aid) # Unpack tuple
                                
                                games.append(GameModel(
                                    uid=GameModel.generate_uid(f"steam_{aid}"),
                                    title=n,
                                    exe_path=f"steam://rungameid/{aid}",
                                    icon_path=icon,
                                    platform=Platform.STEAM.value,
                                    app_id=aid,
                                    install_path=full_path
                                ))
                    except:
                        pass
        return games



class DiskScanner:
    """Сканирование папок на диске для поиска игр (System games)"""

    # Игнорируемые папки и файлы (lower case)
    IGNORE_DIRS = {'windows', 'window.old', 'program data', 'users', '$recycle.bin', 'system volume information', 'common files', 'microsoft', 'drivers', 'directx', 'vcredist', 'support', 'redist', 'prerequisites'}
    # NB: 'crash' убран — это substring и ловил легитимные игры вроде
    # CrashBandicoot4.exe. Crash-handlers ловятся через 'crashhandler'
    # и 'unitycrashhandler'. 'launcher' оставлен для пары
    # GameLauncher.exe + Game.exe в одной папке (берём вторую).
    IGNORE_FILES = {'unins', 'setup', 'update', 'config', 'crashhandler', 'unitycrashhandler', 'dxsetup', 'vcredist', 'redist', 'console', 'terminal', 'server', 'launcher'}
    # Минимальный размер exe чтобы считаться игрой. Раньше было 512 КБ —
    # отбрасывало легитимные UE-bootstrap'ы (Grip.exe ~201 КБ, который
    # лежит в корне и стартует Engine\Binaries\Win64\... shipping exe).
    # 100 КБ — отсекает мелкие утилиты, но впускает мини-лаунчеры.
    MIN_EXE_SIZE_BYTES = 100 * 1024

    def __init__(self):
        pass

    def _is_game_exe(self, path: Path) -> bool:
        """Эвристика: является ли файл игровым исполняемым файлом"""
        name_lower = path.name.lower()

        # 1. Проверка расширения (только exe)
        if path.suffix.lower() != ".exe":
            return False

        # 2. Игнорирование по имени файла
        if any(x in name_lower for x in self.IGNORE_FILES):
            return False

        # 3. Минимальный размер — отсеивает мелкие утилиты
        try:
            if path.stat().st_size < self.MIN_EXE_SIZE_BYTES:
                return False
        except Exception:
            return False

        return True

    def _find_best_exe(self, folder: Path) -> Optional[Path]:
        """Находит главный exe в папке игры"""
        exes = []
        try:
            # Ищем exe на нескольких уровнях глубины. UE-структура для
            # таких игр как Grip / Crash Bandicoot 4:
            #   GameFolder\GameInternalName\Binaries\Win64\Game-Win64-Shipping.exe
            # это 4 уровня от корневой папки игры — поэтому добавлен
            # пятый паттерн */*/*/*/*.exe.
            search_patterns = [
                "*.exe",
                "*/*.exe",
                "*/*/*.exe",
                "*/*/*/*.exe",
                "*/*/*/*/*.exe",
            ]

            for pattern in search_patterns:
                for item in folder.glob(pattern):
                    # Пропускаем папки с техническими именами (кроме Binaries для UE)
                    parent_parts = [p.lower() for p in item.relative_to(folder).parts[:-1]]
                    skip_dirs = {'bin', 'tools', 'redist', 'support', '_commonredist',
                                 'directx', 'vcredist', 'prerequisites', '__installer',
                                 'engine', 'plugins', 'update', 'patch'}
                    if any(d in skip_dirs for d in parent_parts):
                        continue

                    if self._is_game_exe(item):
                        exes.append(item)
        except:
            return None
            
        if not exes:
            return None

        # Если exe один - это он
        if len(exes) == 1:
            return exes[0]

        # Если несколько, пробуем выбрать лучший
        folder_name = folder.name.lower()

        # Сортируем по глубине вложенности (предпочитаем ближе к корню)
        exes.sort(key=lambda x: len(x.relative_to(folder).parts))

        # 1. Точное совпадение имени папки и exe (с учётом всех родительских папок)
        for exe in exes:
            if exe.stem.lower() == folder_name:
                return exe
            # Также проверяем совпадение с именем родительской папки exe
            if exe.parent.name.lower() == exe.stem.lower():
                return exe

        # 2. Совпадение с удалением пробелов/символов
        clean_folder = re.sub(r'[^a-z0-9]', '', folder_name)
        for exe in exes:
            clean_name = re.sub(r'[^a-z0-9]', '', exe.stem.lower())
            if clean_name == clean_folder:
                return exe
            # Также проверяем имя родительской папки
            clean_parent = re.sub(r'[^a-z0-9]', '', exe.parent.name.lower())
            if clean_parent and clean_name == clean_parent:
                return exe

        # 3. Предпочитаем exe в корне, затем по размеру
        root_exes = [e for e in exes if e.parent == folder]
        if root_exes:
            root_exes.sort(key=lambda x: x.stat().st_size, reverse=True)
            return root_exes[0]

        # 4. Самый большой файл из всех
        exes.sort(key=lambda x: x.stat().st_size, reverse=True)
        return exes[0]

    async def scan(self, cover_manager: 'CoverAPIManager', excluded_paths: List[str] = None, additional_paths: List[str] = None) -> List[GameModel]:
        return await asyncio.to_thread(self.scan_sync, cover_manager, excluded_paths, additional_paths)

    def scan_sync(self, cover_manager: 'CoverAPIManager', excluded_paths: List[str] = None, additional_paths: List[str] = None) -> List[GameModel]:
        games = []
        all_paths = additional_paths or []
        if not all_paths:
            logger.info("Нет папок для сканирования системных игр")
            return games
        logger.info(f"Сканирование системных игр в папках: {all_paths}")
        
        excluded = set(str(Path(p).resolve()).lower() for p in (excluded_paths or []))
        scanned_folders = set()
        
        for root_path_str in all_paths:
            root_path = Path(root_path_str)
            if not root_path.exists():
                continue
                
            try:
                # Сканируем папки первого уровня (глубина 1)
                # Например в C:\Games лежат папки C:\Games\Doom, C:\Games\Quake
                for item in root_path.iterdir():
                    if item.is_dir():
                        if item.name.lower() in self.IGNORE_DIRS:
                            continue
                            
                        # Проверяем, не сканировали ли мы эту папку уже (symlinks etc)
                        if item.resolve() in scanned_folders:
                            continue
                        
                        # EXCLUSION CHECK (Folder)
                        if str(item.resolve()).lower() in excluded:
                            logger.info(f"Skipping excluded folder: {item}")
                            continue
                        
                        scanned_folders.add(item.resolve())
                        
                        # Пытаемся найти игру внутри этой папки
                        game_exe = self._find_best_exe(item)
                        if game_exe:
                            # EXCLUSION CHECK (Exe)
                            if str(game_exe.resolve()).lower() in excluded:
                                logger.info(f"Skipping excluded exe: {game_exe}")
                                continue

                            name = item.name # Используем имя папки как название игры
                            
                            # Clean name heuristic
                            clean_name = cover_manager.icon_extractor._clean_name(name)
                            
                            # Cache check
                            cache_key = hashlib.md5(clean_name.encode()).hexdigest()[:12]
                            cache_path = cover_manager.cache_dir / f"{cache_key}.jpg"
                            
                            if cache_path.exists() and cache_path.stat().st_size > 2048:
                                icon = str(cache_path)
                            else:
                                icon, _ = cover_manager.get_cover(name, exe_path=str(game_exe)) # Unpack tuple
                            
                            logger.info(f"Найдена игра: {name} ({game_exe})")
                            
                            games.append(GameModel(
                                uid=GameModel.generate_uid(str(game_exe)),
                                title=name,
                                exe_path=str(game_exe),
                                icon_path=icon,
                                platform=Platform.SYSTEM.value,
                                install_path=str(item)
                            ))
            except Exception as e:
                logger.error(f"Ошибка при сканировании {root_path}: {e}")
                
        return games



class GameManager:
    def __init__(self, data_dir: str = None, cache_dir: str = None,
                 sgdb_key: str = None, rawg_key: str = None):
        # В frozen-сборке (Program Files read-only) пишем в %APPDATA%
        app_dir = get_app_data_dir()
        if data_dir is None:
            data_dir = str(app_dir / "data")
        if cache_dir is None:
            cache_dir = str(app_dir / "cache")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.library_file = self.data_dir / "library.json"

        # Initialize cache directory
        cache_icons = Path(cache_dir) / "icons"
        cache_icons.mkdir(parents=True, exist_ok=True)
        # Heroes cache — landscape арт для BigPicture-фона
        self.cache_heroes = Path(cache_dir) / "heroes"
        self.cache_heroes.mkdir(parents=True, exist_ok=True)

        # Initialize new cover art components
        self.cover_validator = CoverValidator(cache_icons, self.library_file)
        self.cover_api_manager = CoverAPIManager(cache_icons, sgdb_key, rawg_key)
        self.cover_uploader = CoverUploader(cache_icons)

        # Backward compatibility: expose icon_extractor
        self.icon_extractor = self.cover_api_manager.icon_extractor

        self.steam_scanner = SteamScanner()
        self.disk_scanner = DiskScanner()
        self._games: Dict[str, GameModel] = {}
        self._collections: List[Dict[str, Any]] = []  # Пользовательские коллекции
        self._on_progress = None

        # --- Persistence: atomic save + lock + debounce ---
        # Защищает от параллельной записи library.json и от частичной записи при kill
        self._save_lock: Optional[asyncio.Lock] = None  # ленивый, на первом use
        self._save_dirty: bool = False
        self._pending_save_task: Optional[asyncio.Task] = None
        self._debounce_seconds: float = 1.0
        self._shutdown_called: bool = False

    def reinitialize_api_clients(self, sgdb_key: str = None, rawg_key: str = None):
        """Reinitialize API clients with new keys."""
        cache_icons = self.cover_api_manager.cache_dir
        self.cover_api_manager = CoverAPIManager(cache_icons, sgdb_key, rawg_key)
        self.icon_extractor = self.cover_api_manager.icon_extractor
        logger.info("API clients reinitialized")

    def set_progress_callback(self, cb):
        self._on_progress = cb

    async def load_library(self):
        # Подчищаем хвост от прерванной записи
        tmp_path = self.library_file.with_suffix('.json.tmp')
        if tmp_path.exists():
            try:
                tmp_path.unlink()
                logger.info("Removed stale library.json.tmp")
            except OSError:
                pass

        if self.library_file.exists():
            try:
                # IO в отдельном потоке — не блокируем UI на больших библиотеках
                def _read_sync():
                    with open(self.library_file, 'r', encoding='utf-8') as f:
                        return json.load(f)

                data = await asyncio.to_thread(_read_sync)

                for g in data.get('games', []):
                    if 'collections' not in g:
                        g['collections'] = []
                    self._games[g['uid']] = GameModel.from_dict(g)

                self._collections = data.get('collections', [])
                logger.info(f"Loaded library: {len(self._games)} games, "
                            f"{len(self._collections)} collections")

                # Sweep: автоматически чистим игры, которых больше нет на диске
                # (удалены пока лаунчер был закрыт). Раньше это требовало
                # ручного "Обновить библиотеку".
                def _sweep_missing_sync():
                    to_remove = []
                    for uid, game in self._games.items():
                        if game.exe_path and game.exe_path.startswith("steam://"):
                            # Steam — проверяем install_path
                            if game.install_path and not Path(game.install_path).exists():
                                to_remove.append((uid, game.title))
                        else:
                            # System — проверяем сам exe
                            if game.exe_path and not Path(game.exe_path).exists():
                                to_remove.append((uid, game.title))
                    return to_remove

                missing = await asyncio.to_thread(_sweep_missing_sync)
                if missing:
                    for uid, title in missing:
                        logger.info(f"Auto-sweep: removing missing game '{title}' (uid={uid})")
                        del self._games[uid]
                    await self.save_library()
                    logger.info(f"Auto-sweep: removed {len(missing)} games no longer on disk")

                # Validate and repair cache references
                games_list = list(self._games.values())
                repaired = self.cover_validator.repair_library_references(games_list)

                if repaired > 0:
                    logger.info(f"Repaired {repaired} invalid cache references")
                    for game in games_list:
                        self._games[game.uid] = game
                    await self.save_library()

                # Cleanup orphaned cache files
                removed, total = self.cover_validator.cleanup_orphaned_cache()
                if removed > 0:
                    logger.info(f"Cache cleanup: {removed}/{total} orphaned files removed")

            except Exception as e:
                logger.error(f"Load library error: {e}")

    def _get_save_lock(self) -> asyncio.Lock:
        """Lazy-init lock в текущем event loop (создаётся при первом save)."""
        if self._save_lock is None:
            self._save_lock = asyncio.Lock()
        return self._save_lock

    async def save_library(self):
        """Атомарная запись library.json. tmp + os.replace гарантирует
        целостность файла при kill процесса в любой момент."""
        async with self._get_save_lock():
            data = {
                'games': [g.to_dict() for g in self._games.values()],
                'collections': self._collections
            }

            def _write_sync():
                tmp_path = self.library_file.with_suffix('.json.tmp')
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, self.library_file)

            await asyncio.to_thread(_write_sync)
            self._save_dirty = False

    def request_save(self):
        """Помечает библиотеку грязной и шедулит debounce-сохранение через
        _debounce_seconds. Множественные быстрые toggle сворачиваются в один write."""
        self._save_dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # нет event loop — save выполнится в shutdown через asyncio.run

        if self._pending_save_task is None or self._pending_save_task.done():
            self._pending_save_task = loop.create_task(self._debounced_save())

    async def _debounced_save(self):
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        if self._save_dirty and not self._shutdown_called:
            try:
                await self.save_library()
            except Exception as e:
                logger.error(f"Debounced save error: {e}")

    async def flush_save(self):
        """Гарантирует, что любые незаписанные изменения попадут на диск.
        Вызывается перед закрытием приложения и из критичных диалогов."""
        if self._pending_save_task and not self._pending_save_task.done():
            self._pending_save_task.cancel()
            try:
                await self._pending_save_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._save_dirty:
            try:
                await self.save_library()
            except Exception as e:
                logger.error(f"Flush save error: {e}")

    async def shutdown(self):
        """Корректное завершение: финальный flush + остановка thread pools."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        logger.info("GameManager shutdown started")
        try:
            await self.flush_save()
        except Exception as e:
            logger.error(f"Shutdown flush error: {e}")
        try:
            if hasattr(self.cover_api_manager, 'icon_extractor'):
                self.cover_api_manager.icon_extractor.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"IconExtractor shutdown error: {e}")
        logger.info("GameManager shutdown completed")

    def emergency_sync_save(self):
        """Синхронный save без asyncio loop. Для atexit, SIGTERM и подобных
        путей, где event loop уже закрыт. Использует ту же атомарную запись."""
        if not self._save_dirty:
            return False
        return self.save_library_sync()

    def save_library_sync(self) -> bool:
        """Синхронная атомарная запись библиотеки. Для критических операций
        (collections, favorite) — пишет НЕМЕДЛЕННО, не через debounce.
        Безопасно вызывается из любого потока, не зависит от event loop."""
        try:
            data = {
                'games': [g.to_dict() for g in self._games.values()],
                'collections': self._collections,
            }
            tmp_path = self.library_file.with_suffix('.json.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, self.library_file)
            self._save_dirty = False
            logger.info(f"Sync save: {len(self._games)} games, {len(self._collections)} collections")
            return True
        except Exception as e:
            logger.error(f"Sync save error: {e}")
            return False


    # Standard paths for launchers
    LAUNCHER_PATHS = {
        "Epic Games": [r"C:\Program Files\Epic Games", r"D:\Epic Games", r"E:\Epic Games"],
        "Ubisoft": [r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games"],
        "GOG": [r"C:\GOG Games", r"C:\Program Files (x86)\GOG Galaxy\Games", r"D:\GOG Games"],
        "Battle.net": [r"C:\Program Files (x86)\Battle.net\Games", r"C:\Program Files\Battle.net\Games"]
    }

    async def scan_all_games(self, excluded_paths: List[str] = None, additional_paths: List[str] = None, enabled_launchers: dict = None):
        logger.info("scan_all_games called")
        if self._on_progress:
            self._on_progress("Проверка удалённых игр...", 0, 100)
        
        # Default enabled launchers if not provided
        if enabled_launchers is None:
            enabled_launchers = {"Steam": True} # Only Steam by default for safety

        # ИСПРАВЛЕНИЕ: Удаляем игры, которых больше нет на диске
        # Выполняем синхронные проверки IO в отдельном потоке, чтобы не блокировать UI
        def _check_removed_games_sync():
            to_remove = []
            for uid, game in self._games.items():
                # Для Steam игр проверяем install_path (папку установки)
                if game.exe_path.startswith("steam://"):
                    # Проверяем существует ли папка установки
                    if game.install_path and not Path(game.install_path).exists():
                        to_remove.append(uid)
                        logger.info(f"Steam игра удалена с диска: {game.title} ({game.install_path})")
                else:
                    # Для системных игр проверяем exe файл
                    if not Path(game.exe_path).exists():
                        to_remove.append(uid)
                        logger.info(f"Игра удалена с диска: {game.title}")
            return to_remove

        games_to_remove = await asyncio.to_thread(_check_removed_games_sync)
        
        # Удаляем из библиотеки
        for uid in games_to_remove:
            del self._games[uid]
        
        if games_to_remove:
            logger.info(f"Удалено {len(games_to_remove)} игр из библиотеки")
        
        steam_games = []
        if enabled_launchers.get("Steam", True):
            if self._on_progress:
                self._on_progress("Сканирование Steam...", 20, 100)
            # Use new CoverAPIManager for 8-tier fallback
            logger.info("Invoking steam_scanner.scan")
            steam_games = await self.steam_scanner.scan(self.cover_api_manager, excluded_paths)
            logger.info(f"Steam scan finished. Found {len(steam_games)} games.")

        if self._on_progress:
            self._on_progress("Сканирование дисков...", 60, 100)

        # Собираем все пути для сканирования
        system_games = []
        all_scan_paths = []

        # 1. Стандартные пути лаунчеров (если включены в настройках)
        for launcher, paths in self.LAUNCHER_PATHS.items():
            if enabled_launchers.get(launcher, False):
                existing_paths = [p for p in paths if Path(p).exists()]
                if existing_paths:
                    logger.info(f"Scanning launcher {launcher}: {existing_paths}")
                    all_scan_paths.extend(existing_paths)

        # 2. Дополнительные пользовательские папки (extra_game_paths)
        if additional_paths:
            logger.info(f"Scanning custom paths: {additional_paths}")
            all_scan_paths.extend(additional_paths)

        # Сканируем все собранные пути
        if all_scan_paths:
            system_games = await self.disk_scanner.scan(self.cover_api_manager, excluded_paths, additional_paths=all_scan_paths)

        logger.info(f"Disk scan finished. Found {len(system_games)} system games.")

        # Создаём новый словарь игр, полностью заменяя старый (чтобы удалить игры из отключенных лаунчеров)
        # Но при этом сохраняем метаданные (избранное, категории, кастомные иконки) для игр, которые снова нашлись.
        new_games_dict = {}
        
        all_found_games = steam_games + system_games
        
        for game in all_found_games:
            # Если игра была в старой базе - восстанавливаем метаданные
            if game.uid in self._games:
                old_game = self._games[game.uid]
                game.is_favorite = old_game.is_favorite
                game.category = old_game.category
                # КРИТИЧНО: collections иначе все принадлежности игр коллекциям
                # обнуляются на каждый "Обновить библиотеку" — пользователь
                # теряет все свои коллекции после rescan
                game.collections = list(old_game.collections or [])
                # Также сохраняем историю активности — иначе rescan стирает
                # last_played/play_time и "Недавние" в BP сбрасываются
                game.last_played = old_game.last_played
                game.play_time = old_game.play_time
                game.added_date = old_game.added_date
                # Per-game settings — сохраняем через rescan
                game.run_as_admin = old_game.run_as_admin

                # Восстанавливаем кастомную иконку, если она валидна
                if old_game.icon_path and self.cover_validator.validate_cache_file(old_game.icon_path):
                    game.icon_path = old_game.icon_path

            new_games_dict[game.uid] = game

        # Заменяем библиотеку
        self._games = new_games_dict
        logger.info(f"Library updated. Total games: {len(self._games)}")

        # Сбрасываем .miss-маркеры hero-арта — даём шанс повторно подтянуть
        # фоны через расширенный каскад (SteamGridDB Heroes) для игр, у
        # которых раньше не нашлось фона.
        try:
            cleared = 0
            for miss in self.cache_heroes.glob("*.miss"):
                try:
                    miss.unlink()
                    cleared += 1
                except Exception:
                    pass
            if cleared:
                logger.info(f"Cleared {cleared} .miss markers — heroes will be retried")
        except Exception:
            pass

        await self.save_library()
        if self._on_progress:
            self._on_progress("Готово!", 100, 100)

    def get_all_games(self): return list(self._games.values())
    def get_games_by_category(self, cat): return [g for g in self._games.values() if g.is_favorite] if cat == Category.FAVORITES.value else self.get_all_games()
    def get_games_by_platform(self, plat): return [g for g in self._games.values() if g.platform == plat]
    
    async def launch_game(self, uid):
        if uid not in self._games:
            return False
        game = self._games[uid]
        popen = None
        try:
            if game.exe_path.startswith("steam://"):
                # Steam — запускаем через протокол, Popen не получим
                os.startfile(game.exe_path)
            elif game.run_as_admin and sys.platform == "win32":
                # Запуск с правами админа: ShellExecuteW + verb "runas" →
                # стандартный диалог UAC Windows. Нужно для некоторых игр
                # (Crash Bandicoot 4 и т.п.) — иначе DXGI_ERROR_INVALID_CALL.
                # subprocess.Popen с CreateProcess игнорирует UAC-флаги.
                SW_SHOWNORMAL = 1
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", game.exe_path, None,
                    game.install_path or None, SW_SHOWNORMAL,
                )
                # Returncode: > 32 = success (HINSTANCE), <= 32 = ошибка
                # 5 = ACCESS_DENIED (юзер нажал "Нет" в UAC)
                # 2 = FILE_NOT_FOUND
                if result <= 32:
                    if result == 5:
                        logger.info(f"User declined UAC for '{game.title}'")
                    else:
                        logger.error(f"ShellExecute runas failed for '{game.title}', code={result}")
                    return False
                logger.info(f"Launched '{game.title}' as admin via ShellExecuteW")
            else:
                # Обычный системный запуск — через subprocess.Popen
                popen = subprocess.Popen(game.exe_path, cwd=game.install_path, shell=True)
        except Exception as e:
            logger.error(f"Launch failed for {game.title}: {e}")
            return False

        # Обновляем last_played сразу — иначе hero-карта в BP показывает
        # "никогда" даже после многократных запусков
        game.last_played = datetime.now().isoformat()
        self.request_save()

        # Спавним watcher — он будет ждать окончания игры и:
        # 1) добавит elapsed-время к play_time
        # 2) дёрнет on_game_exited callback (main.py поднимает лаунчер обратно)
        if popen is not None:
            # System-игра без admin: ждём через Popen.wait()
            self._watch_game_via_popen(uid, popen)
        elif game.install_path:
            # Steam-игра ИЛИ запуск через runas — Popen нет, мониторим через
            # энумерацию процессов внутри install_path
            self._watch_game_via_process_scan(uid, game.install_path)

        return True

    def _notify_game_exited(self, uid: str):
        """Уведомляет main.py что игра завершилась — лаунчер должен подняться."""
        cb = getattr(self, 'on_game_exited', None)
        if cb is None:
            return
        try:
            cb(uid)
        except Exception as e:
            logger.warning(f"on_game_exited callback failed: {e}")

    def _watch_game_via_popen(self, uid: str, popen):
        """Watcher для system-игр: Popen.wait() блокирует до выхода процесса.
        play_time считается на стороне main.py через _pending_play_sessions —
        единая точка истины для всех игр."""
        import threading as _threading
        def _waiter():
            try:
                popen.wait()
            except Exception:
                pass
            self._notify_game_exited(uid)
        _threading.Thread(target=_waiter, daemon=True).start()

    def _watch_game_via_process_scan(self, uid: str, install_path: str):
        """Watcher для Steam/admin-игр: периодически смотрим список процессов
        и ищем процесс с .exe внутри install_path. Сначала ждём появления
        (Steam-launch не моментальный), потом ждём исчезновения.

        Важно: для игр с анти-читом (EasyAntiCheat, BattlEye, etc.)
        OpenProcess к игре блокируется и мы НЕ видим процесс — appeared
        останется False. В этом случае play_time считается на стороне
        main.py через _pending_play_sessions (время от запуска до alt-tab
        обратно в лаунчер). Callback exited тоже не дёргаем — лаунчер
        поднимется только когда юзер сам alt-tab'нет."""
        import threading as _threading
        def _waiter():
            target_dir = os.path.normpath(install_path).lower()
            # Этап 1: ждём появления. 5 минут — некоторые Steam-игры стартуют
            # долго (анти-чит, проверки, обновления). Раньше было 90 сек —
            # лаунчер преждевременно вылазил на экран если процесс не нашёлся.
            appeared = False
            for _ in range(150):  # 5 мин (150 × 2 сек)
                time.sleep(2)
                if _get_processes_with_exe_in_dir(target_dir):
                    appeared = True
                    break
            if not appeared:
                title = self._games[uid].title if uid in self._games else "?"
                logger.info(f"Game '{title}' process never visible in {target_dir} — "
                            f"likely anti-cheat protected. play_time будет посчитан "
                            f"на стороне main.py при возврате юзера в лаунчер.")
                # НЕ зовём _notify_game_exited — иначе лаунчер вылезет поверх
                # игры. Юзер сам вернётся через alt-tab когда закончит.
                return
            # Этап 2: ждём исчезновения. play_time считается на стороне
            # main.py — здесь только уведомление о выходе.
            while True:
                time.sleep(3)
                if not _get_processes_with_exe_in_dir(target_dir):
                    break
            self._notify_game_exited(uid)
        _threading.Thread(target=_waiter, daemon=True).start()

    async def toggle_favorite(self, uid):
        if uid in self._games:
            self._games[uid].is_favorite = not self._games[uid].is_favorite
            self.request_save()

    @property
    def games_count(self) -> int:
        return len(self._games)

    async def exclude_game(self, uid: str) -> Optional[str]:
        """Remove game from library and return its path for exclusion list.
        Коллекции игры остаются на месте у других игр — удаляется только этот uid."""
        if uid in self._games:
            game = self._games[uid]
            path = game.exe_path or game.install_path
            del self._games[uid]
            await self.save_library()  # критично: подтверждённое удаление
            logger.info(f"Excluded game: {game.title} (path: {path})")
            return path
        return None

    # ========== Collections Management ==========

    def get_collections(self) -> List[Dict[str, Any]]:
        """Получить список всех коллекций из settings"""
        return self._collections

    def add_collection(self, name: str, color: str = "#D500F9", icon: str = "folder") -> Dict[str, Any]:
        """Создать новую коллекцию. Сохраняется НЕМЕДЛЕННО синхронно —
        чтобы коллекция гарантированно сохранилась, даже если приложение
        упадёт в следующую секунду."""
        collection_id = hashlib.md5(f"{name}_{datetime.now().isoformat()}".encode()).hexdigest()[:8]
        collection = {
            "id": collection_id,
            "name": name,
            "color": color,
            "icon": icon,
            "created": datetime.now().isoformat()
        }
        self._collections.append(collection)
        logger.info(f"Created collection: '{name}' (id: {collection_id}), total: {len(self._collections)}")
        self.save_library_sync()
        return collection

    def update_collection(self, collection_id: str, name: str = None, color: str = None, icon: str = None) -> bool:
        """Обновить коллекцию. Sync save немедленно."""
        for col in self._collections:
            if col["id"] == collection_id:
                if name is not None:
                    col["name"] = name
                if color is not None:
                    col["color"] = color
                if icon is not None:
                    col["icon"] = icon
                logger.info(f"Updated collection: {collection_id} ('{col.get('name')}')")
                self.save_library_sync()
                return True
        return False

    def delete_collection(self, collection_id: str) -> bool:
        """Удалить коллекцию и убрать её из всех игр. Sync save немедленно."""
        # Найдём имя для лога перед удалением
        col_name = next((c.get("name", "?") for c in self._collections if c["id"] == collection_id), "?")
        for game in self._games.values():
            if collection_id in game.collections:
                game.collections.remove(collection_id)
        self._collections = [c for c in self._collections if c["id"] != collection_id]
        logger.info(f"Deleted collection: '{col_name}' (id: {collection_id}), remaining: {len(self._collections)}")
        self.save_library_sync()
        return True

    def set_collection_for_games(self, collection_id: str, game_uids: set) -> int:
        """Атомарно выставляет принадлежность коллекции для всех игр:
        - игры из game_uids получают этот collection_id (если ещё не было)
        - у всех остальных игр этот collection_id удаляется

        Возвращает количество затронутых игр. Sync save — изменения сразу
        на диске. Используется новым UI 'Управление играми коллекции' для
        мульти-селекта без N отдельных toggle-вызовов."""
        # Защита: пустой collection_id не трогаем
        if not collection_id:
            return 0
        changed = 0
        target_uids = set(game_uids or [])
        for uid, game in self._games.items():
            should_be_in = uid in target_uids
            currently_in = collection_id in game.collections
            if should_be_in and not currently_in:
                game.collections.append(collection_id)
                changed += 1
            elif not should_be_in and currently_in:
                game.collections.remove(collection_id)
                changed += 1
        if changed:
            self.save_library_sync()
            logger.info(f"Bulk collection '{collection_id}' update: "
                        f"{len(target_uids)} included, {changed} games changed")
        return changed

    def toggle_collection_sync(self, game_uid: str, collection_id: str) -> Optional[bool]:
        """Идемпотентный toggle игры в/из коллекции. Возвращает True (добавлена),
        False (убрана) или None (игра не найдена). Сохранение — через debounce.

        Идемпотентность критична: UI может звать это в разных местах, не зная
        текущего состояния. Здесь нет race с UI-копией collections, как было
        в старом async-варианте."""
        if game_uid not in self._games:
            return None
        game = self._games[game_uid]
        if collection_id in game.collections:
            game.collections.remove(collection_id)
            self.request_save()
            logger.info(f"Removed game {game.title} from collection {collection_id}")
            return False
        else:
            game.collections.append(collection_id)
            self.request_save()
            logger.info(f"Added game {game.title} to collection {collection_id}")
            return True

    async def add_game_to_collection(self, game_uid: str, collection_id: str) -> bool:
        """Async-обёртка над sync-логикой. Сохранение через debounce."""
        if game_uid in self._games:
            game = self._games[game_uid]
            if collection_id not in game.collections:
                game.collections.append(collection_id)
                self.request_save()
                logger.info(f"Added game {game.title} to collection {collection_id}")
                return True
        return False

    async def remove_game_from_collection(self, game_uid: str, collection_id: str) -> bool:
        """Async-обёртка над sync-логикой. Сохранение через debounce."""
        if game_uid in self._games:
            game = self._games[game_uid]
            if collection_id in game.collections:
                game.collections.remove(collection_id)
                self.request_save()
                logger.info(f"Removed game {game.title} from collection {collection_id}")
                return True
        return False

    def get_games_by_collection(self, collection_id: str) -> List[GameModel]:
        """Получить игры в коллекции"""
        return [g for g in self._games.values() if collection_id in g.collections]

    async def add_game_from_path(self, path: str) -> Optional[GameModel]:
        """Add a single game/app from path (used for restoring excluded items)"""
        path_obj = Path(path)
        if not path_obj.exists():
            logger.warning(f"Cannot restore game, path not found: {path}")
            return None

        # Check if already exists
        for game in self._games.values():
            if game.exe_path == path or game.install_path == path:
                 logger.info(f"Game already in library: {game.title}")
                 return game

        # Create basic GameModel (treat as System game for simplicity)
        name = path_obj.name
        # Try to use parent folder name if it's an exe
        if path_obj.is_file() and path_obj.suffix.lower() == '.exe':
             name = path_obj.parent.name
        
        # Add basic cleaned name
        name = self.icon_extractor._clean_name(name)

        # Generate UID
        uid = GameModel.generate_uid(str(path_obj))
        
        # Try to find icon in cache or extract new
        icon = None
        cache_key = hashlib.md5(name.encode()).hexdigest()[:12]
        cache_path = self.cover_api_manager.cache_dir / f"{cache_key}.jpg"
        
        if cache_path.exists():
             icon = str(cache_path)
        else:
             # Try to extract icon since we are restoring
             exe_path = str(path_obj) if path_obj.is_file() else None
             if exe_path:
                 self.icon_extractor._extract_exe_icon(exe_path, cache_path)
                 if cache_path.exists():
                     icon = str(cache_path)
        
        game = GameModel(
            uid=uid,
            title=name,
            exe_path=str(path_obj) if path_obj.is_file() else "",
            icon_path=icon,
            platform=Platform.SYSTEM.value,
            install_path=str(path_obj.parent) if path_obj.is_file() else str(path_obj)
        )
        
        self._games[uid] = game
        await self.save_library()
        logger.info(f"Restored game: {game.title}")
        return game

    # ========== Hero art (landscape) prefetch для BigPicture ==========

    def get_hero_path(self, game: 'GameModel') -> Optional[str]:
        """Возвращает абсолютный путь к закэшированному landscape-арту игры.
        Приоритет: кастомный <uid>_<mtime>.jpg (загружен юзером, новейший по
        mtime суффиксу), затем auto-fetched <uid>.jpg. None если ничего нет —
        BigPicture покажет пустой градиент вместо плохой картинки."""
        if game is None or not game.uid:
            return None

        # 1) Кастомный фон с timestamp-суффиксом (последний загруженный)
        custom_files = sorted(
            self.cache_heroes.glob(f"{game.uid}_*.jpg"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for cf in custom_files:
            if cf.stat().st_size > 2048:
                return str(cf.resolve())

        # 2) Auto-fetched (Steam library_hero / RAWG background_image)
        local = self.cache_heroes / f"{game.uid}.jpg"
        if local.exists() and local.stat().st_size > 2048:
            return str(local.resolve())
        return None

    def is_hero_known_missing(self, game: 'GameModel') -> bool:
        """True если landscape-арт для этой игры не нашёлся ни в одном источнике
        (помечен .miss-маркером — не дёргаем повторно)."""
        if game is None or not game.uid:
            return False
        return (self.cache_heroes / f"{game.uid}.miss").exists()

    def _try_download_hero_for_game(self, game: 'GameModel') -> bool:
        """Скачивает ОДИН качественный landscape-арт для игры. Каскад:
        1) Steam library_hero (1920×620)
        2) SteamGridDB Heroes по Steam app_id (требует API key)
        3) RAWG background_image
        4) SteamGridDB Heroes поиск по названию
        Сохраняет в cache/heroes/<uid>.jpg. Если ничего не нашлось — .miss."""
        local = self.cache_heroes / f"{game.uid}.jpg"
        miss = self.cache_heroes / f"{game.uid}.miss"
        if local.exists() or miss.exists():
            return local.exists()

        candidate_urls: List[str] = []

        # 1) Steam library_hero — landscape 1920×620, идеально для fullscreen
        if game.platform == Platform.STEAM.value and game.app_id:
            candidate_urls.append(
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game.app_id}/library_hero.jpg"
            )

        sgdb = self.cover_api_manager.sgdb if self.cover_api_manager else None
        rawg = self.cover_api_manager.rawg if self.cover_api_manager else None

        clean_name = ""
        try:
            clean_name = self.cover_api_manager.icon_extractor._clean_name(game.title)
        except Exception:
            clean_name = game.title

        # 2) SteamGridDB Heroes по Steam ID — куратор-арт под фоны лаунчеров
        if sgdb and game.platform == Platform.STEAM.value and game.app_id:
            try:
                candidate_urls.extend(sgdb.get_heroes_by_steam_id(game.app_id))
            except Exception:
                pass

        # 3) RAWG background_image — официальный landscape арт
        if rawg:
            try:
                rawg_url = rawg.search_game(clean_name)
                if rawg_url:
                    candidate_urls.append(rawg_url)
            except Exception:
                pass

        # 4) SteamGridDB Heroes поиск по названию (для не-Steam и для Steam
        #    у которых первый каскад провалился)
        if sgdb and clean_name:
            try:
                game_id = sgdb.search_game(clean_name)
                if game_id:
                    candidate_urls.extend(sgdb.get_heroes_by_game_id(game_id))
            except Exception:
                pass

        for url in candidate_urls:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    if r.status == 200:
                        data = r.read()
                        # 8KB минимум — отсекает 404-страницы Steam (~1KB)
                        if len(data) > 8192:
                            with open(local, 'wb') as f:
                                f.write(data)
                            return True
            except Exception:
                continue

        # Ни один источник не дал результат — маркируем
        try:
            miss.touch()
        except Exception:
            pass
        return False

    def set_custom_hero_art(self, game: 'GameModel', source_path: str) -> bool:
        """Заменяет landscape-фон игры на пользовательскую картинку.
        Конвертирует в JPEG, ресайзит до max 1920×1080, сохраняет в
        cache/heroes/<uid>.jpg. Удаляет .miss-маркер если был.
        Возвращает True при успехе."""
        if game is None or not game.uid:
            return False
        try:
            from PIL import Image
        except ImportError:
            logger.error("Pillow not available, cannot upload custom hero art")
            return False

        src = Path(source_path)
        if not src.exists():
            return False

        # Валидация — это вообще картинка?
        try:
            with Image.open(src) as probe:
                probe.verify()
        except Exception as e:
            logger.error(f"Invalid image for custom hero: {e}")
            return False

        try:
            img = Image.open(src)
            # RGBA / palette → RGB на чёрном фоне
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (0, 0, 0))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    bg.paste(img, mask=img.split()[-1])
                else:
                    bg.paste(img)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # Ресайз — фон 1920×1080 это уже больше чем нужно
            max_size = (1920, 1080)
            if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)

            # Уникальное имя <uid>_<timestamp_ms>.jpg — Flutter ImageCache
            # ключует по пути, поэтому без смены имени новый файл с тем же
            # путём показывался бы из кэша как старый.
            ts = int(time.time() * 1000)
            target = self.cache_heroes / f"{game.uid}_{ts}.jpg"
            img.save(target, 'JPEG', quality=88)

            # Удаляем предыдущие кастомные файлы (тоже <uid>_*.jpg) чтобы
            # не копились
            for old in self.cache_heroes.glob(f"{game.uid}_*.jpg"):
                if old != target:
                    try:
                        old.unlink()
                    except Exception:
                        pass

            # Сбрасываем .miss-маркер если был
            miss = self.cache_heroes / f"{game.uid}.miss"
            if miss.exists():
                try:
                    miss.unlink()
                except Exception:
                    pass

            logger.info(f"Custom hero art set for '{game.title}': {target}")
            return True
        except Exception as e:
            logger.error(f"Failed to save custom hero art: {e}")
            return False

    async def prefetch_hero_art(self):
        """Скачивает по одному landscape-арту для каждой игры в библиотеке
        (Steam library_hero ИЛИ RAWG background_image). Вызывается из main.py
        после load_library — в фоне, не блокирует UI. К моменту F11 кэш прогрет."""
        logger.info(f"prefetch_hero_art: библиотека содержит {len(self._games)} игр")
        all_games = list(self._games.values())
        if not all_games:
            return

        # Фильтруем уже закэшированные (включая кастомные) / known-missing
        todo = []
        for g in all_games:
            if not g.uid:
                continue
            if self.get_hero_path(g):  # уже есть фон (auto или custom)
                continue
            miss = self.cache_heroes / f"{g.uid}.miss"
            if miss.exists():
                continue
            todo.append(g)

        if not todo:
            logger.info("prefetch_hero_art: всё уже в кэше")
            return

        logger.info(f"prefetch_hero_art: качаем landscape-арт для {len(todo)} игр")

        def _run_parallel():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(self._try_download_hero_for_game, todo))
            ok = sum(1 for r in results if r)
            logger.info(f"prefetch_hero_art: успех {ok}/{len(todo)}")

        await asyncio.to_thread(_run_parallel)
