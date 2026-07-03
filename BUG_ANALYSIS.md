# Анализ багов CyberLauncher — 2026-07-02

> Результат полного аудита кода (main.py, game_manager.py, wishlist_manager.py,
> bigpicture_view.py, Build.py, spec, логи). Все номера строк — по текущему
> рабочему дереву (включая незакоммиченные правки).
>
> **Главный вывод:** обе заявленные проблемы подтверждены, причины найдены и
> доказаны логами. Они связаны между собой: баг №2 (обложка «не меняется»)
> провоцирует пользователя жать «Авто-поиск в API», который безвозвратно
> удаляет кастомный файл (баг 1.4), а авто-чистка кэша (баг 1.1) массово
> удаляет загруженные обложки с диска.

---

## Статус исправления — 2026-07-03

Все правки внесены в рабочее дерево и проверены smoke-тестами (логика обложек
и wishlist прогнана изолированно, все три модуля компилируются). **Полный
прогон в GUI на реальной библиотеке — за пользователем** (см. раздел 4).

| # | Что | Статус |
|---|---|---|
| 1.1 | drive-offline guard + защита `custom_*` от cleanup + восстановление ссылки в merge | ✅ исправлено |
| 1.2 | `custom_<hash>_<ts>.jpg` — cache-bust имени в `CoverUploader` | ✅ исправлено |
| 1.3 | единый `cover_cache_path` + `find_cached_cover` в обоих сканерах | ✅ исправлено |
| 1.4 | `refresh_cover`: старый файл сносим только после успеха + ts-имя | ✅ исправлено |
| 1.5 | сброс `_card_cache` / `_icon_exists_cache` в `refresh_library` | ✅ исправлено |
| 1.6 | `Popen([exe])` без `shell=True` | ✅ исправлено |
| 1.7 | `.env` + `.gemini/` в `.gitignore` | ⚠️ частично — **ключи перевыпустить и untrack вручную** |
| 2.2 | общий `threading.Lock` + уникальное имя tmp для `library.json` | ✅ исправлено |
| W1 | HLS-fallback в `build_wishlist_item` + кнопка карточки → встроенный плеер | ✅ исправлено |
| W2 | «В браузере» для HLS → страница Steam (не качает .m3u8) | ✅ исправлено |
| W3 | stale-кэш деталей как fallback при офлайне | ✅ исправлено |
| W4 | snackbar/upload-диалог снимаются с `page.overlay` | ✅ исправлено |
| W7 | F11 не поднимает BigPicture под плеером; убран мёртвый `ThreadPoolExecutor` | ✅ исправлено |

**Осознанно отложено** (низкая ценность / риск без живого теста):
2.1 (tkinter в потоках), 2.3 (порог 2 КБ), 2.4 (lock event-loop — закрыт 2.2),
2.5 (DuckDuckGo-бандл), 2.6 (коллизия обложек по clean_name), 2.7 (переезд
папки), W5 (confirm-delete на AlertDialog — работает через `page.open`),
W6 (мёртвые `_trailer_reveal`/`_video_fullscreen` — **нужна живая проверка**
нативного fullscreen плеера, см. чек-лист №9), а также per-card refresh и
`cc=RU` в настройки. `1.7` — перевыпуск ключей физически за пользователем.

---

## Сводка (приоритет сверху вниз)

| # | Баг | Статус | Серьёзность | Где |
|---|---|---|---|---|
| 1.1 | Auto-sweep + cleanup удаляют файлы загруженных обложек | **Подтверждён логом** | 🔴 потеря данных | game_manager.py:1436, 730 |
| 1.2 | Повторная загрузка обложки не видна до перезапуска | **Подтверждён** | 🔴 UX | game_manager.py:926, main.py:441 |
| 1.3 | Обложки system-игр перекачиваются при КАЖДОМ скане | **Подтверждён логом** | 🟠 перфоманс/бан API | game_manager.py:1244 vs 836 |
| 1.4 | «Авто-поиск» удаляет старую обложку ДО скачивания новой | Подтверждён по коду | 🔴 потеря данных | main.py:4745 |
| 1.5 | Кэш карточек не сбрасывается после рескана | Подтверждён по коду | 🟠 UX | main.py:4029 |
| 1.6 | Игры с `&`/`%` в пути не запускаются (S&BOX!) | Подтверждён по коду | 🟠 | game_manager.py:1784 |
| 1.7 | API-ключи закоммичены в публичный GitHub | **Подтверждён** | 🔴 безопасность | .env, .gemini/settings.json |
| 2.x | Потенциальные баги (гонки, tkinter, 2КБ-порог и др.) | Риски | 🟡 | см. раздел 2 |
| W1 | Wishlist: у новых игр нет кнопки «Трейлер» на карточке; у старых она открывает mp4 в браузере вместо встроенного плеера | Подтверждён по коду | 🟠 | wishlist_manager.py:271, main.py:2034 |
| W2 | Wishlist: «В браузере» для HLS-трейлера скачивает .m3u8-файл | Подтверждён по коду | 🟠 UX | main.py:2395, 2461 |
| W3 | Wishlist: офлайн + кэш старше 7 дней = «Не удалось загрузить», хотя кэш на диске есть | Подтверждён по коду | 🟡 | wishlist_manager.py:500 |
| W4 | Утечка page.overlay: snackbar'ы и диалоги копятся бесконечно | Подтверждён по коду | 🟠 деградация | main.py:968, 4642 |
| W5-7 | Wishlist: мелкие риски (AlertDialog удаления, остатки старого плеера и др.) | Риски | 🟡 | см. раздел W |

---

# 1. Подтверждённые баги

## 1.1 «Часть иконок, которые я загружал, слетело» — авто-удаление файлов обложек

### Механизм (цепочка из трёх шагов)

1. **Auto-sweep** при старте ([game_manager.py:1436](game_manager.py#L1436),
   `_sweep_missing_sync` внутри `load_library`): игра считается удалённой, если
   `Path(game.exe_path).exists()` вернул `False`. Но `exists()` возвращает
   `False` и когда **диск в этот момент недоступен**: HDD ещё не раскрутился
   после сна/включения, USB/сетевой диск не подключён, буква диска сменилась.
   Игра **удаляется из library.json** вместе с `icon_path` и `legacy_uids`.

2. Сразу после sweep вызывается `save_library()` → library.json на диске уже
   **без** этих игр.

3. Следом (та же функция, [game_manager.py:1469](game_manager.py#L1469))
   запускается `cleanup_orphaned_cache()` ([game_manager.py:730](game_manager.py#L730)):
   он удаляет из `cache/icons/` все файлы, не упомянутые в library.json.
   Файлы обложек только что удалённых игр — **включая загруженные вручную** —
   стираются с диска **безвозвратно**.

Когда диск «возвращается» и происходит рескан, игры добавляются заново, но
кастомных файлов уже нет — сканер тянет обложки из API. Визуально это и есть
«иконки слетели».

### Доказательство из лога (launcher.log в корне проекта, 2026-06-12)

```
02:11:14 - INFO - Auto-sweep: removing missing game 'Nine Sols (2024)' (uid=c64fea5a46d0)
02:11:14 - INFO - Auto-sweep: removing missing game 'REPLACED (2026)' (uid=b6ef8ea13e29)
...
02:11:14 - INFO - Auto-sweep: removed 28 games no longer on disk
02:11:14 - INFO - Removed orphaned cache: 00de17bd5026.jpg
...(28 файлов)...
02:11:14 - INFO - Cache cleanup: 28/31 orphaned files removed
```

28 игр «пропали» одним махом (типичная картина офлайнового диска) — и через
10 мс их обложки удалены с диска. В логе `%APPDATA%\CyberLauncher\launcher.log`
за 2026-07-02 те же игры (`REPLACED (2026)` и др.) сканируются заново и
обложки перекачиваются из API.

### Решение (три независимых защиты, лучше все сразу)

**а) Guard «диск офлайн» в sweep** — не удалять игру, если недоступен весь
корень диска (значит, отвалился диск, а не игра):

```python
@staticmethod
def _drive_available(path_str: str) -> bool:
    """False если корень диска (D:\\) недоступен — диск офлайн/спит."""
    try:
        anchor = Path(path_str).anchor          # 'D:\\'
        return bool(anchor) and Path(anchor).exists()
    except OSError:
        return False
```

В `_sweep_missing_sync` (и в аналогичном `_check_removed_games_sync` в
`scan_all_games`, game_manager.py:1612):

```python
else:
    # System — проверяем сам exe, НО только если диск вообще доступен
    if game.exe_path and not Path(game.exe_path).exists():
        if not self._drive_available(game.exe_path):
            logger.info(f"Sweep skip (drive offline): '{game.title}'")
            continue
        to_remove.append((uid, game.title))
```

То же для Steam-ветки: перед `steam_install_present` проверять
`_drive_available(game.install_path)`.

**б) `cleanup_orphaned_cache` не должен трогать пользовательские файлы.**
Сейчас кастомная обложка неотличима от авто-скачанной (оба — `<12hex>.jpg`).
Дать кастомным файлам узнаваемое имя `custom_<hash>_<ts>.jpg` (это же чинит
баг 1.2, см. ниже) и пропускать их при чистке:

```python
for cache_file in self.cache_dir.glob('*.jpg'):
    total += 1
    if cache_file.name.startswith('custom_'):
        continue   # загруженное пользователем не удаляем НИКОГДА
    if cache_file.name not in referenced_files:
        ...
```

Ровно такой же компромисс уже принят для hero-артов —
`_recover_orphan_heroes` удаляет только auto-fetched файлы, «custom оставляем
(вдруг юзер вернёт игру)» (game_manager.py:2277-2287).

**в) Восстановление ссылки при возврате игры.** В merge-блоке
`scan_all_games` (game_manager.py:1689-1723), если у игры нет валидной старой
иконки — поискать выживший кастомный файл:

```python
# Кастомная обложка переживает sweep/реинсталл (файл custom_* не чистится).
# Если игра вернулась с тем же uid — восстанавливаем ссылку.
if not game.icon_path or not self.cover_validator.validate_cache_file(game.icon_path):
    base = hashlib.md5(game.uid.encode()).hexdigest()[:12]
    customs = sorted(self.cover_api_manager.cache_dir.glob(f"custom_{base}_*.jpg"))
    if customs:
        game.icon_path = str(customs[-1])
```

> Опционально (усиление): вместо мгновенного sweep — «карантин»: помечать
> игру `missing_since` и удалять только если она отсутствует 2+ запуска
> подряд. Но guard из пункта (а) закрывает основной сценарий проще.

---

## 1.2 Повторная загрузка обложки не отображается до перезапуска

### Механизм

`CoverUploader.upload_from_file` ([game_manager.py:926](game_manager.py#L926))
всегда сохраняет в **один и тот же файл**:

```python
cache_name = hashlib.md5(game_uid.encode()).hexdigest()[:12] + ".jpg"
```

Карточка рисует обложку через `ft.DecorationImage(src=<путь>)`
([main.py:441-448](main.py#L441)). **Flutter ImageCache ключуется по пути**:
если путь не изменился — движок отдаёт старую картинку из памяти, что бы ни
лежало в файле. Поэтому:

- **первая загрузка работает** — путь меняется с API-обложки
  (`md5(app_id).jpg`) на кастомную (`md5(uid).jpg`) → Flutter грузит новый файл;
- **все последующие — нет** — путь тот же → движок берёт из кэша;
- **после перезапуска видно** — кэш движка очищается.

Эта грабля в проекте уже известна и решена… но только для hero-артов.
Комментарий в `set_custom_hero_art` ([game_manager.py:2498-2500](game_manager.py#L2498)):

```python
# Уникальное имя <uid>_<timestamp_ms>.jpg — Flutter ImageCache
# ключует по пути, поэтому без смены имени новый файл с тем же
# путём показывался бы из кэша как старый.
```

Для иконок карточек тот же приём не применили.

### Решение

Скопировать паттерн hero-артов в `CoverUploader.upload_from_file`
(заодно префикс `custom_` для бага 1.1):

```python
import time  # уже импортирован в модуле

# Generate cache filename: custom_<hash>_<ts>.jpg
#  - смена имени на каждую загрузку обходит Flutter ImageCache
#    (ключуется по пути — та же грабля, что у hero, см. set_custom_hero_art)
#  - префикс custom_ защищает файл от cleanup_orphaned_cache
base = hashlib.md5(game_uid.encode()).hexdigest()[:12]
cache_name = f"custom_{base}_{int(time.time() * 1000)}.jpg"
cache_path = self.cache_dir / cache_name

...
img.save(cache_path, 'JPEG', quality=90)

# Удаляем прежние версии, чтобы не копились
for old in self.cache_dir.glob(f"custom_{base}_*.jpg"):
    if old != cache_path:
        try:
            old.unlink()
        except OSError:
            pass
# И файл старого формата md5(uid).jpg, если был
legacy = self.cache_dir / f"{base}.jpg"
if legacy.exists():
    try:
        legacy.unlink()
    except OSError:
        pass
```

`upload_from_url` использует `upload_from_file` — починится автоматически.
В main.py (`upload_cover_from_file` / `upload_cover_from_url`) менять ничего
не нужно: карточка уже инвалидируется, а `icon_path` теперь будет каждый раз
новым → Flutter загрузит файл заново.

Сопутствующие правки:

1. В `cleanup_orphaned_cache` — пропуск `custom_*` (код в 1.1-б). Защиту
   через `md5(uid)`/`legacy_uids` (game_manager.py:748-756) можно оставить
   для старых файлов.
2. Нормализация путей в `load_library` (game_manager.py:1407) ищет
   `'icons' in p.parts` по имени файла — с новыми именами работает без правок.

---

## 1.3 Обложки system-игр перекачиваются из API при каждом сканировании

### Механизм

Сканер и загрузчик считают имя кэш-файла **по-разному**:

- `DiskScanner.scan_sync` ([game_manager.py:1244](game_manager.py#L1244)) и
  `restore_game` (game_manager.py:2169) проверяют кэш по имени
  **`md5(clean_name)[:12].jpg`**;
- `CoverAPIManager.get_cover` ([game_manager.py:834-836](game_manager.py#L834))
  сохраняет в **`md5( md5(clean_name).hexdigest().lower() )[:12].jpg`** —
  хэш от хэша!

```python
# get_cover:
key_id = app_id if app_id else hashlib.md5(clean_name.encode()).hexdigest()
cache_path = self.cache_dir / f"{hashlib.md5(str(key_id).lower().encode()).hexdigest()[:12]}.jpg"
```

Для Steam-игр (`key_id = app_id`) имена совпадают, для system-игр — **никогда**.
Проверка кэша всегда промахивается → каждый рескан гоняет полный каскад API
по каждой system-игре.

### Доказательство из лога (%APPDATA%, 2026-07-02 14:49)

Каждая (!) system-игра при рескане проходит Tier 3/5/7:

```
14:49:37 - INFO - [Tier 3] Steam Store Search: 'Slots and Diapers 0 1'
14:49:37 - INFO - [Tier 5] RAWG.io: 'Slots and Diapers 0 1'
14:49:38 - INFO -    ✅ Downloaded from RAWG.io
14:49:38 - INFO - Найдена игра: Slots.and.Diapers.v1.0.1 ...
```

Последствия: рескан медленный (1–6 с на игру), лишний трафик, риск бана
API (DuckDuckGo уже банит — в коде есть «анти-бан» меры), и перезапись
файлов обложек на ровном месте.

### Решение

Единая функция имени файла в `CoverAPIManager`, использовать её везде:

```python
def cover_cache_path(self, game_title: str, app_id: str = None) -> Path:
    """Каноническое имя кэш-файла обложки. ЕДИНСТВЕННОЕ место, где оно
    вычисляется — сканеры и get_cover обязаны использовать эту функцию."""
    clean = self.icon_extractor._clean_name(game_title)
    key_id = app_id if app_id else hashlib.md5(clean.encode()).hexdigest()
    name = hashlib.md5(str(key_id).lower().encode()).hexdigest()[:12]
    return self.cache_dir / f"{name}.jpg"
```

- В `get_cover` заменить строки 834-836 на `cache_path = self.cover_cache_path(game_title, app_id)`.
- В `SteamScanner.scan_sync` (1056-1058), `DiskScanner.scan_sync` (1244-1247)
  и `restore_game` (2169-2172) заменить ручное вычисление на
  `cover_manager.cover_cache_path(...)`.

> Существующие файлы со «старым» именем `md5(clean_name)` в кэше не мешают:
> их приберёт cleanup как orphan.

---

## 1.4 «Авто-поиск в API» / «Обновить» удаляет обложку ДО скачивания новой

### Механизм

`refresh_cover` ([main.py:4744-4749](main.py#L4744)):

```python
# Delete existing cache
if game.icon_path:
    try:
        Path(game.icon_path).unlink()   # ← удаляем СНАЧАЛА
    ...
new_path, source = await asyncio.to_thread(...get_cover...)  # ← качаем ПОТОМ
```

Если каскад не нашёл обложку (в логе такое есть: `❌ All tiers failed for:
TheDarkPicturesAnthology`):

- старый файл уже удалён — **кастомная обложка потеряна безвозвратно**;
- `game.icon_path` остаётся указывать на удалённый файл;
- `GameCard._icon_exists_cache[old_path]` залип в `True` → карточка рисует
  битую/пустую картинку до перезапуска, при следующем старте
  `repair_library_references` зануляет ссылку.

Это второй усилитель жалобы «иконки слетели»: из-за бага 1.2 пользователь
думает, что загрузка не сработала, жмёт «Авто-поиск» — и его файл стирается.

### Решение

```python
async def refresh_cover(self, game: GameModel):
    self.loading_overlay.show("Поиск обложки...")
    self.page.update()
    await asyncio.sleep(0.05)

    old_path = game.icon_path

    new_path, source = await asyncio.to_thread(
        self.game_manager.cover_api_manager.get_cover,
        game.title, game.app_id, game.exe_path,
    )

    self.loading_overlay.hide()

    if new_path:
        # Смена имени файла — иначе Flutter ImageCache покажет старую
        # картинку (get_cover пишет в фиксированное имя). Паттерн как у hero.
        p = Path(new_path)
        busted = p.with_name(f"{p.stem}_{int(time.time() * 1000)}{p.suffix}")
        try:
            os.replace(p, busted)
            new_path = str(busted)
        except OSError:
            pass
        # Прежние ts-версии этой же обложки не копим
        for old in p.parent.glob(f"{p.stem}_*{p.suffix}"):
            if str(old) != new_path:
                try:
                    old.unlink()
                except OSError:
                    pass

        # Старый файл удаляем ТОЛЬКО после успеха (и только если это не он же)
        if old_path and old_path != new_path:
            try:
                Path(old_path).unlink()
            except OSError:
                pass

        game.icon_path = new_path
        ...  # (дальше как сейчас: _card_cache, _icon_exists_cache, grid)
    else:
        # Ничего не нашли — старая обложка ОСТАЁТСЯ на месте
        self.page.update()
        self.show_snackbar("❌ Не удалось найти обложку", bgcolor="#F44336")
```

Дополнительно стоит спрашивать подтверждение, если у игры сейчас кастомная
обложка (`Path(game.icon_path).name.startswith("custom_")`): «Заменить
загруженную вами обложку результатом из API?»

> Примечание: ts-переименование означает, что фиксированного файла для
> сканера нет → при следующем рескане эта игра перекачает обложку один раз
> (merge сохранит валидный icon_path). Если хочется избежать и этого —
> научить `cover_cache_path`-проверку сканера искать и `<name>_*.jpg`
> (glob), как делает `get_hero_path`.

---

## 1.5 После «Обновить библиотеку» карточки показывают устаревшее состояние

### Механизм

`refresh_library` ([main.py:4029-4051](main.py#L4029)) после `scan_all_games`
вызывает `update_game_grid()`, но **не очищает** `self._card_cache` и
`GameCard._icon_exists_cache`:

- `_card_cache` хранит карточки со **старыми объектами GameModel** — рескан
  создаёт новые объекты (`self._games = new_games_dict`), а карточки
  продолжают показывать старые `icon_path`/название;
- `_icon_exists_cache` хранит негативные записи: если файла не было при
  первом построении карточки, а рескан его скачал **по тому же
  детерминированному имени** — карточка так и останется с заглушкой
  до перезапуска.

Третий вклад в симптом «иконки слетели/не обновляются».

### Решение

В `refresh_library` после завершения сканирования (перед `update_game_grid()`):

```python
# Рескан пересоздал GameModel'ы и мог изменить файлы обложек по тем же
# путям — старые карточки и exists-кэш больше не валидны.
self._card_cache.clear()
GameCard._icon_exists_cache.clear()
self.refresh_collections_sidebar()
self.update_game_grid()
```

---

## 1.6 Не запускаются игры с `&`, `%`, кавычками в пути (у вас есть S&BOX!)

### Механизм

[game_manager.py:1784](game_manager.py#L1784):

```python
popen = subprocess.Popen(game.exe_path, cwd=game.install_path, shell=True)
```

`shell=True` оборачивает команду в `cmd.exe /c "<путь>"`. cmd сохраняет
кавычки только если между ними **нет спецсимволов** (`& < > ( ) @ ^ |`).
Путь `D:\Game Install\S&BOX\sbox.exe` (есть в вашей библиотеке — см. лог за
2026-07-02 14:50) содержит `&` → cmd срежет кавычки и попытается выполнить
`D:\Game` и `BOX\sbox.exe` как две команды. Запуск падает. `%` в пути
раскрывается как переменная окружения — тоже ломает запуск.

### Решение

Убрать shell — передавать список (CreateProcess сам корректно квотит):

```python
popen = subprocess.Popen([game.exe_path], cwd=game.install_path)
```

Работает с пробелами, `&`, `%` и не тянет лишний процесс cmd.exe
(бонус: `popen.wait()` в watcher-е будет ждать саму игру, а не cmd).

---

## 1.7 API-ключи опубликованы в публичном GitHub-репозитории

### Факты

Репозиторий публичный (https://github.com/MikhailShap/MyGameLauncher), при
этом в git отслеживаются:

- **`.env`** (закоммичен как минимум с коммита 6776dae) — `.env` НЕ указан
  в `.gitignore`;
- **`.gemini/settings.json`** — содержит **API-ключ Context7**
  (`ctx7sk-bccb…`, виден в файле прямо сейчас).

Любой ключ, однажды запушенный в публичный репозиторий, считается
скомпрометированным — даже после удаления файла он остаётся в истории.

### Решение (по порядку)

1. **Отозвать/перевыпустить ключи**: Context7 (и всё, что лежит в `.env` —
   проверить содержимое). Это главный шаг, остальное — гигиена.
2. Убрать из индекса и запретить впредь:
   ```powershell
   git rm --cached .env .gemini/settings.json
   Add-Content .gitignore "`n.env`n.gemini/"
   git commit -m "chore: remove secrets from repo"
   ```
3. (Опционально) Вычистить историю `git filter-repo` + force-push. Если ключи
   перевыпущены — можно не переписывать историю.

---

# 2. Потенциальные баги (не воспроизведены, но риски реальные)

## 2.1 tkinter из фонового потока — риск случайных крэшей

[main.py:4480-4512](main.py#L4480): на каждый выбор файла создаётся новый
`Tk()` в новом `threading.Thread`. Tcl/Tk не потокобезопасен; повторное
создание/уничтожение интерпретаторов в разных потоках — известный источник
крэшей `Tcl_AsyncDelete: async handler deleted by the wrong thread`
(проявляется редко и «на ровном месте», чаще после нескольких открытий).

**Решение:** держать один скрытый `Tk`-корень в одном выделенном потоке на
всё время жизни приложения и слать ему задания через `queue.Queue`; либо
вызывать нативный диалог напрямую через
`ctypes.windll.comdlg32.GetOpenFileNameW` (без tkinter вообще). Минимум —
сериализовать вызовы `threading.Lock`-ом.

## 2.2 Гонка `save_library` (async) × `save_library_sync` — риск порчи library.json

Оба пути пишут в **один и тот же** `library.json.tmp`
(game_manager.py:1492 и 1576), но `save_library_sync` не берёт
`asyncio.Lock` (он и не может — вызывается из других потоков). Если
debounce-сохранение и sync-сохранение совпадут по времени — два писателя в
один tmp-файл, `os.replace` опубликует битый JSON. Следующий запуск упадёт на
`Load library error` → библиотека «пустая» → рескан с нуля → потеря
метаданных (и каскад из бага 1.1).

**Решение:** общий `threading.Lock` для обеих записей (async-путь берёт его
внутри `asyncio.to_thread(_write_sync)`) **и** уникальное имя tmp-файла:

```python
tmp_path = self.library_file.with_suffix(f'.json.{os.getpid()}.{threading.get_ident()}.tmp')
```

## 2.3 Порог «< 2 КБ = битый файл» зануляет маленькие валидные обложки

`CoverValidator.validate_cache_file` (game_manager.py:722) считает файл
меньше 2048 байт невалидным. Маленькая кастомная картинка (например,
пиксель-арт иконка, однотонная обложка) после JPEG-сжатия легко весит < 2 КБ →
при каждом старте `repair_library_references` зануляет её `icon_path`
(«Invalidated missing cache»). Тихая потеря пользовательских ссылок.

**Решение:** для файлов, которые существуют, но меньше порога — проверять
по-настоящему (быстрый `PIL.Image.open(...).verify()`), а не отбрасывать по
размеру; порог оставить только для очевидного мусора (< 100 байт).

## 2.4 `_get_save_lock` привязан к первому event loop

game_manager.py:1476-1480: `asyncio.Lock` создаётся в loop-е первого вызова.
Если при shutdown `save_library` выполняется через новый `asyncio.run(...)`
(комментарий в `request_save` это допускает), обращение к lock-у из другого
loop-а даст `RuntimeError`/зависание. Решается тем же общим
`threading.Lock` из 2.2.

## 2.5 DuckDuckGo (Tier 7) мёртв в установленной сборке

Лог 2026-07-02: `DuckDuckGo недоступен (библиотека не загружена)` — при том
что `duckduckgo_search` есть в hiddenimports. Либо пакет не установлен в
venv на момент сборки, либо не собрались его нативные зависимости
(`curl_cffi`/`primp`). Проверить: `venv\Scripts\python -c "import
duckduckgo_search"` и build_last.log. Либо починить бандлинг, либо честно
выпилить Tier 7 (сейчас он просто съедает время каскада).

## 2.6 Коллизия обложек у игр с одинаковым «чистым» именем

Имя кэш-файла для system-игр — функция только от `clean_name`. Две разные
папки, дающие одинаковый `clean_name` (переиздания, «Game» и «Game (2024)»
после чистки), делят один файл обложки: обновление обложки одной игры молча
меняет её у второй. **Решение:** подмешивать в ключ `uid` игры, либо принять
как осознанный дедуп (задокументировать).

## 2.7 Переезд папки игры = потеря метаданных

Fallback-матчинг в merge идёт по `install_path` (game_manager.py:1693).
Если игру перенесли в другую папку/на другой диск, меняются и uid, и
install_path → матчинг не срабатывает → favorite/коллекции/наигранное/
кастомный арт теряются. **Идея:** третий fallback — по `(title, размер exe)`
или по имени exe-файла.

## 2.8 Мелочи

- `GameCard._icon_exists_cache` не ограничен и хранит негативные записи —
  после фикса 1.5 приемлемо, но правильнее инвалидировать точечно.
- `update_game_grid()` из upload-колбэков дёргается, даже если открыт другой
  вид (настройки/wishlist) — проверить, что Flet не кидает исключение на
  update отсоединённого контрола.
- `upload_target_game` — общее мутабельное состояние диалога загрузки; при
  быстром открытии диалога для другой игры результат уйдёт не туда
  (микро-риск, `on_pick_file_click` использует замыкание `game` — ок, а вот
  `on_api_search_click` читает `self.upload_target_game`).
- Дев-мусор в корне репозитория: `_flettest.py`, `_splice.py`,
  `_new_trailer.txt`, `debug_*.txt`, 76-МБ `CyberLauncher.exe` (в
  .gitignore, но лежит в рабочей папке). Стоит прибрать.

---

# W. Раздел «Желаемое» (wishlist)

Аудит `wishlist_manager.py` + wishlist-части `main.py` (view/карточки:
1839-2151, детальный экран: 2153-2652, плеер: 2385-2512, диалог добавления:
2675-2961). Общая оценка: раздел сделан аккуратнее остального кода —
персистенс атомарный, оверлеи корректно снимаются с `page.overlay`,
async-обработчики прямые, race-guard'ы в загрузке деталей есть. Но есть
находки.

## W1. Кнопка «Трейлер» на карточке: не появляется у новых игр, а у старых ведёт в браузер

**Механизм.** `build_wishlist_item` ([wishlist_manager.py:271-277](wishlist_manager.py#L271))
заполняет `item.trailer_url` только из **старой** схемы Steam
(`movies[].mp4.max/480`). У игр с новой схемой (2024+, только
`hls_h264`/`dash_h264`) `trailer_url` остаётся пустым → кнопка «Трейлер» на
карточке просто не рисуется (main.py:2033-2041). При этом детальный экран
(`fetch_game_details`, wishlist_manager.py:356-376) обе схемы поддерживает —
рассинхрон между карточкой и деталкой.

Вторая половина: когда кнопка ЕСТЬ (старые игры), она делает
`webbrowser.open(mp4_url)` (main.py:2038) — открывает голый mp4 в браузере,
хотя в приложении есть встроенный плеер, которым пользуется деталка.

**Решение.**
1. В `build_wishlist_item` добавить fallback на новую схему (как в
   `fetch_game_details`):
   ```python
   mp4 = chosen.get("mp4") or {}
   item.trailer_url = (mp4.get("max") or mp4.get("480")
                       or chosen.get("hls_h264") or chosen.get("dash_h264") or "")
   ```
2. На карточке открывать встроенный плеер, а не браузер:
   ```python
   on_click=lambda e, url=item.trailer_url, nm=item.title: self._show_trailer_player(url, nm),
   ```
   (`_show_trailer_player` сам падает в браузер, если flet_video недоступен.)

⚠️ Пункт 1 без пункта 2 делает хуже: `webbrowser.open("…/hls_master.m3u8")`
не проигрывает, а **скачивает файл** (см. W2). Менять оба места вместе.

Существующие элементы wishlist.json созданы старым кодом — у них
`trailer_url` уже пустой. После фикса нужно либо перезаполнить его при
загрузке (одноразовая миграция через `build_wishlist_item`), либо смириться,
что кнопка появится только у заново добавленных игр.

## W2. «В браузере» для HLS-трейлера скачивает .m3u8-файл

`_show_trailer_player` использует `webbrowser.open(url)` в трёх местах как
fallback (main.py:2395, 2427) и как кнопку «В браузере» (main.py:2461).
Для старых mp4-трейлеров это работает, но для новой схемы `url` — это
HLS-манифест: браузеры не играют `.m3u8` нативно и просто скачивают его.

**Решение:** для не-mp4 URL открывать страницу игры в Steam — там трейлер
точно играет:

```python
def _open_trailer_in_browser(self, url: str, store_url: str = ""):
    if ".m3u8" in url or ".mpd" in url or "/hls_" in url or "/dash_" in url:
        webbrowser.open(store_url or url)
    else:
        webbrowser.open(url)
```

(store_url доступен и в карточке — `item.store_url`, и в деталке —
`d["store_url"]`; надо просто пробросить его в `_show_trailer_player`.)

## W3. Офлайн + кэш старше 7 дней = пустой экран деталей

`_read_details_disk` ([wishlist_manager.py:500](wishlist_manager.py#L500))
возвращает `None`, если кэшу больше 7 дней. `get_details` тогда идёт в сеть;
если сети нет / Steam недоступен — возвращается `None`, и деталка показывает
«Не удалось загрузить данные из Steam», **хотя на диске лежит полный кэш**,
просто устаревший.

**Решение:** держать stale-кэш как fallback при сетевой неудаче:

```python
def get_details(self, app_id, force=False):
    ...
    data = fetch_game_details(app_id)
    if data:
        self._details_mem[app_id] = data
        self._write_details_disk(app_id, data)
        return data
    # Сеть не дала данных — отдаём протухший кэш, это лучше чем ничего
    stale = self._read_details_disk(app_id, ignore_ttl=True)
    if stale:
        logger.info(f"Details: network failed, serving stale cache for {app_id}")
        self._details_mem[app_id] = stale
    return stale
```

(в `_read_details_disk` добавить параметр `ignore_ttl: bool = False`).

## W4. Утечка page.overlay — snackbar'ы и диалоги копятся бесконечно

Не только wishlist, но бьёт по всему приложению:

- `show_snackbar` ([main.py:960-970](main.py#L960)) на **каждый вызов**
  создаёт новый `ft.SnackBar` и делает `page.overlay.append()` — и никогда
  не удаляет. Каждая загрузка обложки, смена приоритета, удаление, скан —
  +1 мёртвый контрол в overlay навсегда.
- `show_upload_dialog` ([main.py:4640-4642](main.py#L4640)): диалог
  пересоздаётся на каждое открытие, проверка `if self.upload_dialog not in
  self.page.overlay` всегда истинна (объект-то новый) → на каждое открытие
  диалога overlay растёт на один закрытый AlertDialog.

Последствие: `page.update()` диффит всё более жирное дерево → UI
деградирует по ходу сессии, память растёт. Wishlist-оверлеи, к слову,
сделаны правильно (append при открытии, remove при закрытии) — этот паттерн
и нужно распространить.

**Решение:**
- snackbar: удалять по закрытию (`on_dismiss`):
  ```python
  def show_snackbar(self, message, bgcolor="#333333", duration=4000):
      snackbar = ft.SnackBar(content=ft.Text(message), bgcolor=bgcolor,
                             duration=duration)
      def _cleanup(e):
          try:
              self.page.overlay.remove(snackbar)
              self.page.update()
          except Exception:
              pass
      snackbar.on_dismiss = _cleanup
      self.page.overlay.append(snackbar)
      snackbar.open = True
      self.page.update()
  ```
- upload_dialog: перед `append` удалять предыдущий
  (`if self.upload_dialog in self.page.overlay: self.page.overlay.remove(...)`)
  или перевести на `_open_card_modal` (кастомный overlay, как везде).

## W5. Подтверждение удаления — на «ненадёжном» ft.AlertDialog

`_wishlist_confirm_delete` (main.py:2654-2673) использует `ft.AlertDialog`,
хотя грабля №2 из PROJECT_CONTEXT гласит, что в этой версии Flet AlertDialog
ненадёжно закрывается, и все остальные модалки wishlist уже переведены на
кастомный overlay. `_dismiss_dialog` пробует новый API `page.close()` — может
и работает, но это единственное место в разделе, где осталась старая схема.

**Решение:** перевести на `_open_card_modal(title_row, body, actions)` —
хелпер уже есть и используется рядом. Заодно диалог получит закрытие по ESC
через общий обработчик (сейчас ESC его не закрывает).

## W6. Остатки выпиленного кастомного плеера (мёртвый код с побочкой)

В рабочем дереве плеер переведён на нативную панель media_kit
(`show_controls=True`), но остались хвосты:

- `self._trailer_reveal` — проверяется в keyboard-handler (main.py:3571),
  но **нигде не устанавливается** → мёртвая ветка;
- `self._video_fullscreen` — сбрасывается в двух `_close()` (main.py:2320,
  2442), но **нигде не ставится в True**. Если нативная fullscreen-кнопка
  media_kit переводит само окно в полноэкран, то закрытие плеера крестиком/ESC
  окно из fullscreen НЕ вернёт (код возврата завязан на всегда-False флаг).
  Нужно проверить вживую: открыть трейлер → нативный fullscreen → ESC/крестик;
- PROJECT_CONTEXT.md §7 всё ещё описывает кастомную Flet-панель как
  «сделано и работает» — документация разошлась с кодом. Обновить при
  коммите.

## W7. Мелочи wishlist (низкий приоритет)

- **`ThreadPoolExecutor(max_workers=3)`** в `WishlistManager.__init__`
  (wishlist_manager.py:420) создаётся, но не используется ни разу и не
  shutdown-ится — удалить.
- **`save_sync` с fsync в UI-потоке**: каждый клик по огоньку приоритета =
  синхронная запись с fsync в обработчике события + полный rebuild всего
  вида (`_refresh_wishlist_view`). На большом списке и HDD клики будут
  подлагивать. Достаточно перестраивать одну карточку, когда сортировка не
  «по приоритету».
- **F11 при открытом трейлере** запускает BigPicture ПОД оверлеем плеера
  (keyboard-handler отдаёт F11 приоритет). Логичнее игнорировать F11, пока
  открыт `_media_overlay`.
- **Хардкод `cc=RU&l=russian`** в steam_search/appdetails — цены и выдача
  всегда российские. Для себя — ок, для публичного проекта — вынести в
  настройки.
- Кэш `cache/wishlist_details/*.json` не чистится по возрасту для игр,
  удалённых из списка вручную ещё до появления `_drop_details_cache` —
  разовая ручная чистка или мини-sweep при старте.
- Плеер задаёт `width/height` видео один раз при открытии — при ресайзе
  окна с открытым трейлером видео не подстраивается.

---

# 3. Рекомендуемый порядок работ

1. **1.7 — отозвать ключи** (5 минут, независимо от кода).
2. **1.2 + 1.1-б/в** — `custom_<hash>_<ts>.jpg` в `CoverUploader` + пропуск
   `custom_*` в cleanup + восстановление ссылки в merge. Одна связная правка,
   закрывает обе заявленные жалобы наполовину.
3. **1.1-а** — drive-offline guard в оба sweep-а. Закрывает массовую потерю.
4. **1.4** — refresh_cover: удалять старый файл только после успеха + ts-имя.
5. **1.5** — очистка `_card_cache`/`_icon_exists_cache` в `refresh_library`.
6. **1.3** — `cover_cache_path()` и единые имена (ускорит рескан в разы).
7. **1.6** — `Popen([exe])` без shell (проверить на S&BOX).
8. **W4** — утечка overlay (snackbar + upload-диалог): дешёвый фикс,
   останавливает деградацию UI в длинных сессиях.
9. **W1 + W2** — трейлеры wishlist: одна связная правка (кнопка карточки →
   встроенный плеер, «В браузере» → Steam-страница для HLS).
10. Раздел 2 и остальное из W — по мере сил; в первую очередь 2.2
    (порча library.json) и W3 (stale-кэш офлайн).

# 4. Как проверить после фиксов

1. **Повторная загрузка:** загрузить обложку → сразу загрузить другую →
   карточка должна смениться **без** перезапуска (файлы в
   `%APPDATA%\CyberLauncher\cache\icons\` — `custom_*_<ts>.jpg`, старый
   удалён).
2. **Слетание иконок:** загрузить кастомную обложку → закрыть лаунчер →
   отключить диск с игрой (или временно переименовать папку) → запустить и
   закрыть лаунчер → вернуть диск → запустить: игра на месте, обложка
   кастомная. В логе — `Sweep skip (drive offline)`.
3. **Рескан:** «Обновить библиотеку» → в логе НЕ должно быть `[Tier 3/5]`
   для игр, у которых обложка уже была; карточки обновляются сразу.
4. **Авто-поиск при недоступном API:** отключить сеть → «Авто-поиск» →
   старая обложка остаётся и на карточке, и на диске.
5. **S&BOX** запускается из лаунчера.
6. **Wishlist-трейлеры:** добавить в желаемое новую игру (2024+, напр.
   какой-нибудь свежий релиз) → на карточке есть кнопка «Трейлер», клик
   открывает встроенный плеер; «В браузере» ведёт на Steam-страницу, а не
   скачивает .m3u8.
7. **Wishlist офлайн:** открыть деталку игры (кэш создан) → выключить сеть →
   открыть деталку снова после истечения TTL (или временно поставить
   `DETAILS_TTL_SECONDS = 0`) → показываются кэшированные данные, а не
   «Не удалось загрузить».
8. **Overlay-утечка:** после ~20 snackbar'ов/диалогов `len(page.overlay)`
   не растёт монотонно (проверить debug-печатью).
9. **Трейлер + нативный fullscreen:** открыть трейлер → нативная кнопка
   fullscreen → ESC → окно лаунчера вернулось из полноэкрана (проверка W6).
