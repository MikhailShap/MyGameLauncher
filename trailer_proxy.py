"""
trailer_proxy.py — локальный HLS-прокси для встроенного плеера трейлеров.

ПРОБЛЕМА. Steam отдаёт трейлеры через akamai CDN как HLS. mpv (внутри
flet_video/media_kit) при TCP-таймауте к CDN роняет воспроизведение
(`ffurl_read returned 0xffffff76` = ETIMEDOUT) и НЕ переподключается.
flet_video 0.80 не пробрасывает опции mpv (reconnect/timeout), поэтому
починить это на стороне плеера нельзя.

РЕШЕНИЕ. Поднимаем HTTP-сервер на 127.0.0.1 и отдаём mpv переписанный
плейлист, где все дорожки/сегменты ведут на нас. Сами качаем у akamai с
ретраями, браузерными заголовками и префетчем — таймаут CDN больше не убивает
просмотр, mpv просто получает сегмент чуть позже.

Плейлисты переписываются РЕКУРСИВНО: master → медиа-плейлисты (видео + аудио) →
сегменты. На каждом уровне URI следующего уровня заменяются на непрозрачный
`/s/<sid>/r/<id>` (mpv никогда не видит реальные CDN-адреса, а мы не принимаем
произвольные URL параметром — только заранее зарегистрированные id).

Только stdlib (http.server, urllib, threading) — PyInstaller подхватит без
доп. настройки в Build.py.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger("CyberLauncher.backend")

# Браузерные заголовки: akamai режет дефолтный UA ffmpeg/urllib, а Referer
# нужен, чтобы CDN отдал сегменты (см. PROJECT_CONTEXT §8.4).
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Referer": "https://store.steampowered.com/",
}

# Кап памяти на кэш сегментов одной сессии (мягкий, LRU-вытеснение).
_SESSION_CACHE_CAP = 96 * 1024 * 1024   # 96 МБ — с запасом на 1080p-трейлер
# Сколько следующих сегментов префетчить после запрошенного.
_PREFETCH_AHEAD = 2


def _looks_like_m3u8(raw: bytes, url: str) -> bool:
    """Плейлист это или бинарный сегмент. #EXTM3U — надёжный magic HLS."""
    return raw[:7] == b"#EXTM3U" or url.split("?", 1)[0].endswith(".m3u8")


def _guess_content_type(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith((".mp4", ".m4s", ".cmfv", ".init")):
        return "video/mp4"
    if path.endswith(".aac"):
        return "audio/aac"
    if path.endswith(".m4a"):
        return "audio/mp4"
    return "application/octet-stream"


class _Session:
    """Одна открытая сессия воспроизведения (один трейлер).

    Хранит реестр id → реальный CDN-URL (заполняется по мере переписывания
    плейлистов) и LRU-кэш скачанных байтов сегментов.
    """

    def __init__(self, master_url: str, headers: Dict[str, str]):
        self.master_url = master_url
        self.headers = headers
        self.lock = threading.Lock()
        # Двусторонний маппинг: url ↔ короткий id (дедуп, чтобы byterange-
        # сегменты одного файла делили id и кэш).
        self._url_to_id: Dict[str, str] = {}
        self._id_to_url: Dict[str, str] = {}
        self._id_counter = 0
        # Кэш байтов по id: id → bytes. + порядок вставки для LRU и общий размер.
        self._blob_cache: Dict[str, bytes] = {}
        self._blob_order: List[str] = []
        self._blob_bytes = 0
        # Порядок сегментов внутри медиа-плейлиста (для префетча N+1..N+k):
        # список id в порядке появления; и позиция каждого id.
        self._seg_order: List[str] = []
        self._seg_pos: Dict[str, int] = {}

    def assign(self, abs_url: str) -> str:
        """url → локальный путь /r/<id>. Дедуплицирует одинаковые url."""
        with self.lock:
            rid = self._url_to_id.get(abs_url)
            if rid is None:
                rid = f"r{self._id_counter}"
                self._id_counter += 1
                self._url_to_id[abs_url] = rid
                self._id_to_url[rid] = abs_url
            return rid

    def url_for(self, rid: str) -> Optional[str]:
        with self.lock:
            return self._id_to_url.get(rid)

    def register_segment_order(self, ids: List[str]) -> None:
        """Запоминает порядок сегментов медиа-плейлиста для префетча."""
        with self.lock:
            for rid in ids:
                if rid not in self._seg_pos:
                    self._seg_pos[rid] = len(self._seg_order)
                    self._seg_order.append(rid)

    def next_segment_ids(self, rid: str, ahead: int) -> List[str]:
        with self.lock:
            pos = self._seg_pos.get(rid)
            if pos is None:
                return []
            return self._seg_order[pos + 1: pos + 1 + ahead]

    def cache_get(self, rid: str) -> Optional[bytes]:
        with self.lock:
            blob = self._blob_cache.get(rid)
            if blob is not None:
                # LRU-touch
                try:
                    self._blob_order.remove(rid)
                except ValueError:
                    pass
                self._blob_order.append(rid)
            return blob

    def cache_put(self, rid: str, blob: bytes) -> None:
        with self.lock:
            if rid in self._blob_cache:
                return
            self._blob_cache[rid] = blob
            self._blob_order.append(rid)
            self._blob_bytes += len(blob)
            # Вытеснение самых старых, пока не влезем в кап.
            while self._blob_bytes > _SESSION_CACHE_CAP and len(self._blob_order) > 1:
                old = self._blob_order.pop(0)
                ob = self._blob_cache.pop(old, b"")
                self._blob_bytes -= len(ob)


class TrailerProxy:
    """Локальный HTTP-прокси для HLS-трейлеров. Один инстанс на приложение;
    сессия на каждый открытый плеер. Потокобезопасен."""

    def __init__(self, retries: int = 3):
        self._retries = retries
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port = 0
        self._sessions: Dict[str, _Session] = {}
        self._sessions_lock = threading.Lock()
        self._prefetch_inflight: set = set()
        self._prefetch_lock = threading.Lock()

    # ---------- Жизненный цикл сервера ----------

    def start(self) -> str:
        """Идемпотентно поднимает сервер на 127.0.0.1:<эфемерный порт>.
        Возвращает базовый URL. Только loopback → файрвол не триггерится."""
        if self._server is not None:
            return self._base_url()
        proxy = self
        handler = _make_handler(proxy)
        # port 0 → ОС выдаст свободный эфемерный порт.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="TrailerProxy", daemon=True,
        )
        self._thread.start()
        logger.info(f"TrailerProxy: слушает 127.0.0.1:{self._port}")
        return self._base_url()

    def stop(self) -> None:
        """Гасит сервер. Дёрнуть в shutdown-пути приложения."""
        srv = self._server
        if srv is None:
            return
        self._server = None
        try:
            srv.shutdown()
            srv.server_close()
        except Exception as e:
            logger.debug(f"TrailerProxy stop: {e}")
        logger.info("TrailerProxy: остановлен")

    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    # ---------- Сессии ----------

    def start_session(self, master_url: str,
                      http_headers: Optional[Dict[str, str]] = None) -> str:
        """Регистрирует сессию для master-плейлиста трейлера и возвращает
        ЛОКАЛЬНЫЙ master-URL для mpv. Автоматически поднимает сервер."""
        self.start()
        sid = uuid.uuid4().hex[:12]
        headers = dict(DEFAULT_HEADERS)
        if http_headers:
            headers.update(http_headers)
        with self._sessions_lock:
            self._sessions[sid] = _Session(master_url, headers)
        logger.info(f"TrailerProxy: сессия {sid} для {master_url.split('?', 1)[0]}")
        return f"{self._base_url()}/s/{sid}/master.m3u8"

    def end_session(self, sid: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(sid, None)

    def _session(self, sid: str) -> Optional[_Session]:
        with self._sessions_lock:
            return self._sessions.get(sid)

    # ---------- Сеть с ретраями (это и есть фикс ETIMEDOUT) ----------

    def _fetch(self, url: str, headers: Dict[str, str]) -> Tuple[bytes, str]:
        """GET с ретраями. Возвращает (bytes, final_url для относительных URI).
        Кидает последнее исключение, если все попытки провалились."""
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=12.0) as r:
                    return r.read(), r.geturl()
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_exc = e
                tail = url.split("/")[-1].split("?", 1)[0]
                logger.info(
                    f"TrailerProxy retry {attempt + 1}/{self._retries}: {tail} ({e})")
                if attempt + 1 < self._retries:
                    time.sleep(0.5 * (2 ** attempt))   # 0.5 / 1.0 c
        raise last_exc if last_exc else RuntimeError("fetch failed")

    # ---------- Переписывание плейлистов ----------

    def _rewrite_playlist(self, sess: _Session, sid: str,
                          text: str, base_url: str) -> Tuple[str, List[str]]:
        """Заменяет все URI в HLS-плейлисте на локальные /s/<sid>/r/<id>.
        Обрабатывает: голые URI-строки (сегменты/варианты), URI="..." в тегах
        (#EXT-X-MEDIA аудио, #EXT-X-MAP init-сегмент fMP4, #EXT-X-KEY, i-frame).
        Возвращает (переписанный_текст, список id голых URI-строк — сегментов)."""
        prefix = f"/s/{sid}/r/"
        seg_ids: List[str] = []

        def assign(uri: str) -> str:
            rid = sess.assign(urljoin(base_url, uri.strip()))
            return prefix + rid

        def rewrite_uri_attr(line: str) -> str:
            return re.sub(r'URI="([^"]+)"',
                          lambda m: f'URI="{assign(m.group(1))}"', line)

        out: List[str] = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                out.append(line)
                continue
            if s.startswith("#"):
                out.append(rewrite_uri_attr(line) if 'URI="' in s else line)
            else:
                # Голая URI-строка = сегмент (в медиа-плейлисте) или вариант
                # (в master). Оба заворачиваем на себя.
                local = assign(s)
                out.append(local)
                seg_ids.append(local.rsplit("/", 1)[-1])
        return "\n".join(out) + "\n", seg_ids

    # ---------- Обработка запросов от mpv ----------

    def handle_master(self, sid: str) -> Optional[Tuple[bytes, str]]:
        sess = self._session(sid)
        if sess is None:
            return None
        raw, final_url = self._fetch(sess.master_url, sess.headers)
        text = raw.decode("utf-8", errors="replace")
        rewritten, _ = self._rewrite_playlist(sess, sid, text, final_url)
        return rewritten.encode("utf-8"), "application/vnd.apple.mpegurl"

    def handle_resource(self, sid: str, rid: str,
                        rng: Optional[str]) -> Optional[Tuple[int, bytes, str, Optional[str]]]:
        """Отдаёт ресурс по id: медиа-плейлист (рекурсивно переписан) или
        байты сегмента. Возвращает (status, body, content_type, content_range)
        или None если сессия/id неизвестны."""
        sess = self._session(sid)
        if sess is None:
            return None
        real_url = sess.url_for(rid)
        if real_url is None:
            return None

        # Байты из кэша или из сети (с ретраями).
        blob = sess.cache_get(rid)
        final_url = real_url
        if blob is None:
            blob, final_url = self._fetch(real_url, sess.headers)
            # Плейлисты не кэшируем как сегменты (они мелкие и переписываются);
            # сегменты — да (для Range-повторов и префетча).
            if not _looks_like_m3u8(blob, real_url):
                sess.cache_put(rid, blob)

        # Вложенный медиа-плейлист → переписать его сегменты и отдать как m3u8.
        if _looks_like_m3u8(blob, real_url):
            text = blob.decode("utf-8", errors="replace")
            rewritten, seg_ids = self._rewrite_playlist(sess, sid, text, final_url)
            sess.register_segment_order(seg_ids)
            return 200, rewritten.encode("utf-8"), "application/vnd.apple.mpegurl", None

        # Это сегмент → префетч следующих + отдать (с поддержкой Range).
        self._prefetch(sess, rid)
        ctype = _guess_content_type(real_url)
        if rng:
            sliced = _apply_range(blob, rng)
            if sliced is not None:
                start, end, chunk = sliced
                crange = f"bytes {start}-{end}/{len(blob)}"
                return 206, chunk, ctype, crange
        return 200, blob, ctype, None

    def _prefetch(self, sess: _Session, rid: str) -> None:
        """Фоново скачивает следующие _PREFETCH_AHEAD сегментов в кэш —
        сглаживает затупы CDN до нуля видимых пауз."""
        nxt = sess.next_segment_ids(rid, _PREFETCH_AHEAD)
        for nid in nxt:
            if sess.cache_get(nid) is not None:
                continue
            with self._prefetch_lock:
                if nid in self._prefetch_inflight:
                    continue
                self._prefetch_inflight.add(nid)
            threading.Thread(target=self._prefetch_one, args=(sess, nid),
                             daemon=True).start()

    def _prefetch_one(self, sess: _Session, nid: str) -> None:
        try:
            url = sess.url_for(nid)
            if url and sess.cache_get(nid) is None:
                blob, _ = self._fetch(url, sess.headers)
                if not _looks_like_m3u8(blob, url):
                    sess.cache_put(nid, blob)
        except Exception as e:
            logger.debug(f"TrailerProxy prefetch {nid}: {e}")
        finally:
            with self._prefetch_lock:
                self._prefetch_inflight.discard(nid)


def _apply_range(blob: bytes, rng: str) -> Optional[Tuple[int, int, bytes]]:
    """Парсит 'bytes=start-end' и режет blob. Возвращает (start, end, chunk)."""
    m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
    if not m:
        return None
    n = len(blob)
    s_raw, e_raw = m.group(1), m.group(2)
    if s_raw == "" and e_raw == "":
        return None
    if s_raw == "":                       # суффиксный диапазон: последние N байт
        length = int(e_raw)
        start = max(0, n - length)
        end = n - 1
    else:
        start = int(s_raw)
        end = int(e_raw) if e_raw else n - 1
    start = max(0, min(start, n - 1))
    end = max(start, min(end, n - 1))
    return start, end, blob[start:end + 1]


# Регэксп разбора пути: /s/<sid>/master.m3u8  или  /s/<sid>/r/<id>
_PATH_RE = re.compile(r"^/s/(?P<sid>[0-9a-f]{12})/(?P<rest>master\.m3u8|r/[A-Za-z0-9]+)$")


def _make_handler(proxy: TrailerProxy):
    class _Handler(BaseHTTPRequestHandler):
        # Тишина в stderr — свой лог у proxy.
        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, body: bytes, ctype: str,
                  content_range: Optional[str] = None, full_len: Optional[int] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            if content_range:
                self.send_header("Content-Range", content_range)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _fail(self, status: int):
            try:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass

        def do_GET(self):
            m = _PATH_RE.match(self.path)
            if not m:
                self._fail(404)
                return
            sid = m.group("sid")
            rest = m.group("rest")
            try:
                if rest == "master.m3u8":
                    res = proxy.handle_master(sid)
                    if res is None:
                        self._fail(404)
                        return
                    body, ctype = res
                    self._send(200, body, ctype)
                    return
                rid = rest.split("/", 1)[1]
                rng = self.headers.get("Range")
                res = proxy.handle_resource(sid, rid, rng)
                if res is None:
                    self._fail(404)
                    return
                status, body, ctype, crange = res
                self._send(status, body, ctype, crange)
            except Exception as e:
                # Все ретраи провалились или CDN недоступен → 502.
                # mpv поднимет on_error, приложение покажет снекбар.
                logger.info(f"TrailerProxy 502 for {self.path}: {e}")
                self._fail(502)

        do_HEAD = do_GET

    return _Handler
