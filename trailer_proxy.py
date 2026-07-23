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
from urllib.parse import urljoin, urlsplit, parse_qs

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


def _parse_master_heights(text: str) -> List[int]:
    """Высоты видео-вариантов из master (#EXT-X-STREAM-INF RESOLUTION=WxH),
    по убыванию, без дублей. Для дропдауна выбора качества."""
    hs = set()
    for line in text.splitlines():
        if line.strip().startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if m:
                hs.add(int(m.group(1)))
    return sorted(hs, reverse=True)


def _parse_mpd_reps(xml: str) -> Dict[str, int]:
    """DASH: Representation id → height. Только реальные видео-варианты
    (height задан и bandwidth > 0 — отсекает trickplay/storyboard дорожки,
    у Steam бывает 864p с bandwidth=0)."""
    reps: Dict[str, int] = {}
    for tag in re.findall(r"<Representation\b[^>]*", xml):
        mid = re.search(r'id="([^"]+)"', tag)
        h = re.search(r'height="(\d+)"', tag)
        bw = re.search(r'bandwidth="(\d+)"', tag)
        if mid and h and bw and int(bw.group(1)) > 0:
            reps[mid.group(1)] = int(h.group(1))
    return reps


def _filter_mpd_by_height(xml: str, height: int) -> str:
    """DASH-аналог _filter_master_by_height: оставить только видео-Representation
    нужной высоты (аудио и блоки без height не трогаем). Работает и с одним
    Representation на AdaptationSet (схема Steam), и с несколькими. Если
    совпадений нет — исходный XML (фоллбек на Авто)."""
    kept_any = False

    def _h(tag: str):
        m = re.search(r'height="(\d+)"', tag)
        return int(m.group(1)) if m else None

    def _bw(tag: str) -> int:
        m = re.search(r'bandwidth="(\d+)"', tag)
        return int(m.group(1)) if m else 0

    def filter_set(mset):
        nonlocal kept_any
        block = mset.group(0)
        reps = re.findall(r"<Representation\b[^>]*(?:/>|>.*?</Representation>)",
                          block, re.S)
        video_reps = [r for r in reps if _h(r) is not None]
        if not video_reps:
            return block                       # аудио и пр. — не трогаем
        keep = [r for r in video_reps if _h(r) == height and _bw(r) > 0]
        if not keep:
            return ""                          # другая высота / trickplay
        kept_any = True
        out = block
        for r in video_reps:
            if r not in keep:
                out = out.replace(r, "")
        return out

    new_xml = re.sub(r"<AdaptationSet\b.*?</AdaptationSet>", filter_set, xml,
                     flags=re.S)
    return new_xml if kept_any else xml


def _filter_master_by_height(text: str, height: int) -> str:
    """Оставляет в master ТОЛЬКО вариант нужной высоты (+ аудио и прочие теги),
    выкидывая остальные #EXT-X-STREAM-INF с их URI. mpv тогда играет именно это
    качество, а не выбирает по ABR. Если совпадений нет — возвращает исходный
    текст (фоллбек на Авто)."""
    lines = text.splitlines()
    out: List[str] = []
    kept = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("#EXT-X-STREAM-INF:"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", s)
            h = int(m.group(1)) if m else None
            uri = lines[i + 1] if i + 1 < len(lines) else ""
            if h == height:
                out.append(lines[i])
                out.append(uri)
                kept = True
            i += 2
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n" if kept else text


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
        # rid варианта (медиа-плейлиста из master) → метка качества ("1080p
        # (5800k)"). Заполняется при разборе master. Нужно, чтобы залогировать,
        # КАКОЕ качество реально запросил mpv (ответ на «а это точно 1080p?»).
        self._variant_label: Dict[str, str] = {}
        self._logged_variants: set = set()
        self._seg_variant: Dict[str, str] = {}   # rid сегмента → rid варианта
        # Список высот вариантов (1080, 720, …) по убыванию — для дропдауна
        # выбора качества. Заполняется eager-парсом master в start_session.
        self.heights: List[int] = []
        # DASH-сессия (Steam dash_av1/dash_h264): master = .mpd, сегменты
        # приходят ОТНОСИТЕЛЬНЫМИ путями (mpv строит их из SegmentTemplate
        # относительно нашего локального master.mpd) — переписывание не нужно.
        self.is_dash = False
        self.dash_reps: Dict[str, int] = {}   # Representation id → height

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

    def set_variant_label(self, rid: str, label: str) -> None:
        with self.lock:
            self._variant_label[rid] = label

    def variant_label(self, rid: str) -> Optional[str]:
        with self.lock:
            return self._variant_label.get(rid)

    def map_segments_to_variant(self, variant_rid: str, seg_ids: List[str]) -> None:
        """Сегмент → вариант, которому он принадлежит. Нужно, чтобы по первому
        РЕАЛЬНО отданному сегменту залогировать играющее качество (mpv при
        старте запрашивает плейлисты ВСЕХ вариантов — их запросы ничего не
        говорят о том, что играет)."""
        with self.lock:
            for sid_ in seg_ids:
                self._seg_variant.setdefault(sid_, variant_rid)

    def playing_label_once(self, seg_rid: str) -> Optional[str]:
        """Метка качества по сегменту — один раз на вариант (для лога)."""
        with self.lock:
            vr = self._seg_variant.get(seg_rid)
            if vr is None or vr in self._logged_variants:
                return None
            label = self._variant_label.get(vr)
            if label is None:
                return None
            self._logged_variants.add(vr)
            return label

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
        sess = _Session(master_url, headers)
        sess.is_dash = master_url.split("?", 1)[0].lower().endswith(".mpd")
        with self._sessions_lock:
            self._sessions[sid] = sess
        # Eager-парс master → список качеств для дропдауна. Одна лёгкая загрузка
        # (~1-3 КБ). Провал (офлайн) — не критично: дропдаун покажет только «Авто».
        try:
            raw, _ = self._fetch(master_url, headers)
            text = raw.decode("utf-8", errors="replace")
            if sess.is_dash:
                sess.dash_reps = _parse_mpd_reps(text)
                sess.heights = sorted(set(sess.dash_reps.values()), reverse=True)
            else:
                sess.heights = _parse_master_heights(text)
        except Exception as e:
            logger.debug(f"TrailerProxy: не удалось разобрать качества master: {e}")
        kind = "DASH" if sess.is_dash else "HLS"
        logger.info(f"TrailerProxy: сессия {sid} [{kind}] для {master_url.split('?', 1)[0]} "
                    f"(качества: {sess.heights or 'н/д'})")
        local_name = "master.mpd" if sess.is_dash else "master.m3u8"
        return f"{self._base_url()}/s/{sid}/{local_name}"

    def end_session(self, sid: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(sid, None)

    def session_heights(self, sid: str) -> List[int]:
        """Список доступных высот (1080, 720, …) для дропдауна. [] если неизвестно."""
        sess = self._session(sid)
        return list(sess.heights) if sess else []

    def _session(self, sid: str) -> Optional[_Session]:
        with self._sessions_lock:
            return self._sessions.get(sid)

    # ---------- Сеть с ретраями (это и есть фикс ETIMEDOUT) ----------

    def _fetch(self, url: str, headers: Dict[str, str]) -> Tuple[bytes, str]:
        """GET с ретраями. Возвращает (bytes, final_url для относительных URI).
        Кидает последнее исключение, если все попытки провалились.
        HTTP 404 НЕ ретраится: сервер ответил, файла просто нет (штатный случай
        для DASH-префетча за последним чанком)."""
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=12.0) as r:
                    return r.read(), r.geturl()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                last_exc = e
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_exc = e
            tail = url.split("/")[-1].split("?", 1)[0]
            logger.info(
                f"TrailerProxy retry {attempt + 1}/{self._retries}: {tail} ({last_exc})")
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
        pending_inf = None   # последний #EXT-X-STREAM-INF (в master) — метка качества
        for line in text.splitlines():
            s = line.strip()
            if not s:
                out.append(line)
                continue
            if s.startswith("#"):
                if s.startswith("#EXT-X-STREAM-INF:"):
                    pending_inf = s
                out.append(rewrite_uri_attr(line) if 'URI="' in s else line)
            else:
                # Голая URI-строка = сегмент (в медиа-плейлисте) или вариант
                # (в master). Оба заворачиваем на себя.
                local = assign(s)
                rid = local.rsplit("/", 1)[-1]
                if pending_inf is not None:
                    # Это вариант из master → запомнить его качество для лога.
                    res = re.search(r"RESOLUTION=\d+x(\d+)", pending_inf)
                    bw = re.search(r"BANDWIDTH=(\d+)", pending_inf)
                    label = f"{res.group(1)}p" if res else "?"
                    if bw:
                        label += f" ({int(bw.group(1)) // 1000}k)"
                    sess.set_variant_label(rid, label)
                    pending_inf = None
                out.append(local)
                seg_ids.append(rid)
        return "\n".join(out) + "\n", seg_ids

    # ---------- Обработка запросов от mpv ----------

    def handle_master(self, sid: str,
                      height: Optional[int] = None) -> Optional[Tuple[bytes, str]]:
        sess = self._session(sid)
        if sess is None:
            return None
        raw, final_url = self._fetch(sess.master_url, sess.headers)
        text = raw.decode("utf-8", errors="replace")
        # height задан (выбор качества в UI) → оставить только этот вариант.
        if height:
            text = _filter_master_by_height(text, height)
            logger.info(f"TrailerProxy: master отфильтрован до {height}p (session {sid})")
        rewritten, _ = self._rewrite_playlist(sess, sid, text, final_url)
        return rewritten.encode("utf-8"), "application/vnd.apple.mpegurl"

    def handle_dash_master(self, sid: str,
                           height: Optional[int] = None) -> Optional[Tuple[bytes, str]]:
        """DASH: отдать MPD как есть (сегменты относительные — mpv сам придёт
        к нам, переписывание не нужно). height → оставить один видео-вариант."""
        sess = self._session(sid)
        if sess is None or not sess.is_dash:
            return None
        raw, _ = self._fetch(sess.master_url, sess.headers)
        text = raw.decode("utf-8", errors="replace")
        if height:
            text = _filter_mpd_by_height(text, height)
            logger.info(f"TrailerProxy: MPD отфильтрован до {height}p (session {sid})")
        return text.encode("utf-8"), "application/dash+xml"

    def handle_dash_segment(self, sid: str, relpath: str,
                            rng: Optional[str]) -> Optional[Tuple[int, bytes, str, Optional[str]]]:
        """DASH-сегмент по ОТНОСИТЕЛЬНОМУ пути (init-stream0.m4s,
        dash_av1/chunk-stream0-00001.m4s …). Путь резолвится строго против
        master_url сессии — наружу за пределы каталога трейлера не выйти."""
        sess = self._session(sid)
        if sess is None or not sess.is_dash:
            return None
        if (".." in relpath or relpath.startswith("/") or "\\" in relpath
                or "://" in relpath):
            return None
        real_url = urljoin(sess.master_url, relpath)
        blob = sess.cache_get(relpath)
        if blob is None:
            blob, _ = self._fetch(real_url, sess.headers)
            sess.cache_put(relpath, blob)

        # Лог реального качества: chunk-stream<repID>-… → высота из MPD.
        m = re.search(r"chunk-stream([^-]+)-", relpath)
        if m:
            rep_id = m.group(1)
            key = f"dash:{rep_id}"
            with sess.lock:
                first = key not in sess._logged_variants
                if first:
                    sess._logged_variants.add(key)
            if first:
                h = sess.dash_reps.get(rep_id)
                label = f"{h}p" if h else f"stream{rep_id}"
                logger.info(f"TrailerProxy: mpv играет качество {label} (DASH, session {sid})")

        self._prefetch_dash(sess, relpath)
        ctype = _guess_content_type(real_url)
        if rng:
            sliced = _apply_range(blob, rng)
            if sliced is not None:
                start, end, chunk = sliced
                return 206, chunk, ctype, f"bytes {start}-{end}/{len(blob)}"
        return 200, blob, ctype, None

    def _prefetch_dash(self, sess: _Session, relpath: str) -> None:
        """Префетч следующих чанков по номеру в имени (…-00007.m4s → 00008)."""
        m = re.search(r"(\d+)(\.[A-Za-z0-9]+)$", relpath)
        if not m:
            return
        num, ext = m.group(1), m.group(2)
        for i in range(1, _PREFETCH_AHEAD + 1):
            nxt = str(int(num) + i).zfill(len(num))
            nrel = relpath[: m.start(1)] + nxt + ext
            if sess.cache_get(nrel) is not None:
                continue
            with self._prefetch_lock:
                if nrel in self._prefetch_inflight:
                    continue
                self._prefetch_inflight.add(nrel)
            threading.Thread(target=self._prefetch_dash_one, args=(sess, nrel),
                             daemon=True).start()

    def _prefetch_dash_one(self, sess: _Session, nrel: str) -> None:
        try:
            if sess.cache_get(nrel) is None:
                blob, _ = self._fetch(urljoin(sess.master_url, nrel), sess.headers)
                sess.cache_put(nrel, blob)
        except Exception as e:
            # За последним чанком — ожидаемый 404, не шумим.
            logger.debug(f"TrailerProxy dash prefetch {nrel}: {e}")
        finally:
            with self._prefetch_lock:
                self._prefetch_inflight.discard(nrel)

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
            # NB: запрос ПЛЕЙЛИСТА ничего не говорит о играющем качестве — mpv
            # при старте пробует плейлисты ВСЕХ вариантов. Реальное качество
            # логируем ниже, по первому отданному СЕГМЕНТУ варианта.
            text = blob.decode("utf-8", errors="replace")
            rewritten, seg_ids = self._rewrite_playlist(sess, sid, text, final_url)
            sess.register_segment_order(seg_ids)
            if sess.variant_label(rid):
                sess.map_segments_to_variant(rid, seg_ids)
            return 200, rewritten.encode("utf-8"), "application/vnd.apple.mpegurl", None

        # Это сегмент → значит вариант РЕАЛЬНО играет (лог один раз на вариант).
        playing = sess.playing_label_once(rid)
        if playing:
            logger.info(f"TrailerProxy: mpv играет качество {playing} (session {sid})")
        # Префетч следующих + отдать (с поддержкой Range).
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


# Разбор пути: /s/<sid>/master.m3u8 | /s/<sid>/master.mpd | /s/<sid>/r/<id>
# (HLS-ресурс) | /s/<sid>/<относительный путь DASH-сегмента>.
_PATH_RE = re.compile(r"^/s/(?P<sid>[0-9a-f]{12})/(?P<rest>.+)$")


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
            parts = urlsplit(self.path)
            m = _PATH_RE.match(parts.path)
            if not m:
                self._fail(404)
                return
            sid = m.group("sid")
            rest = m.group("rest")

            # Фаза 1 — собрать тело (сеть). Провал = реальный сбой CDN → 502.
            try:
                if rest in ("master.m3u8", "master.mpd"):
                    # ?q=<height> — ручной выбор качества (иначе Авто/ABR).
                    q = parse_qs(parts.query).get("q", [None])[0]
                    height = int(q) if q and q.isdigit() else None
                    if rest == "master.mpd":
                        res = proxy.handle_dash_master(sid, height)
                    else:
                        res = proxy.handle_master(sid, height)
                    payload = None if res is None else (200, res[0], res[1], None)
                elif rest.startswith("r/"):
                    rid = rest.split("/", 1)[1]
                    payload = proxy.handle_resource(sid, rid, self.headers.get("Range"))
                else:
                    # Относительный путь DASH-сегмента (mpv построил его из
                    # SegmentTemplate относительно нашего master.mpd).
                    payload = proxy.handle_dash_segment(sid, rest, self.headers.get("Range"))
            except urllib.error.HTTPError as e:
                # 404 от CDN (запрос за последний чанк DASH) — честный 404.
                code = 404 if e.code == 404 else 502
                logger.debug(f"TrailerProxy {code} (CDN HTTP {e.code}) for {self.path}")
                self._fail(code)
                return
            except ConnectionError as e:
                # Оборвалось соединение К CDN — редко, но это сеть, не клиент.
                logger.info(f"TrailerProxy 502 (CDN) for {self.path}: {e}")
                self._fail(502)
                return
            except Exception as e:
                # Ретраи провалились / CDN недоступен → 502. mpv поднимет
                # on_error, приложение покажет снекбар.
                logger.info(f"TrailerProxy 502 for {self.path}: {e}")
                self._fail(502)
                return

            if payload is None:
                self._fail(404)
                return

            # Фаза 2 — отдать. Обрыв ЗДЕСЬ = mpv сам закрыл запрос (seek/закрытие
            # плеера, WinError 10053/10054) — это норма, не ошибка. Тихо в debug.
            try:
                status, body, ctype, crange = payload
                self._send(status, body, ctype, crange)
            except (ConnectionError, OSError) as e:
                logger.debug(f"TrailerProxy: клиент закрыл запрос {self.path}: {e}")

        do_HEAD = do_GET

    return _Handler
