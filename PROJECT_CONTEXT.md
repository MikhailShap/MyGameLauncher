# CyberLauncher — контекст проекта

> Рабочий документ для быстрого ввода в курс дела (в т.ч. для новых сессий
> ассистента). Описывает что за проект, как устроен, как собирается, что уже
> сделано и над чем работаем. Версия на момент написания — **v1.9.2**.

---

## 1. Что это за проект

**CyberLauncher** — десктопный игровой лаунчер под Windows. Собирает в одну
библиотеку игры из **Steam**, **Epic Games** и просто игры с дисков
(«системные» — найденные по `.exe`). Красивый UI, автозагрузка обложек,
режим **BigPicture** с управлением геймпадом, коллекции, избранное, раздел
**«Желаемое»** (Steam wishlist) и удаление игр прямо из лаунчера.

- Репозиторий: https://github.com/MikhailShap/MyGameLauncher
- Платформа: Windows (x64)
- Язык интерфейса: русский

---

## 2. Технологический стек

| Компонент | Назначение |
|---|---|
| **Python 3.14** | основной язык |
| **Flet 0.80** (Flutter под капотом) | весь UI (`flet_desktop` — готовый Flutter-клиент) |
| **flet-video 0.80** (media_kit / libmpv) | встроенное воспроизведение трейлеров |
| **pygame-ce** (SDL2 GameController) | геймпад в BigPicture |
| **Pillow** | обработка обложек |
| **send2trash** (legacy ctypes-бэкенд) | удаление игр в Корзину |
| **SteamGridDB / RAWG.io API** | обложки и арт |
| **PyInstaller** (`--onedir`) | сборка `.exe` |
| **Inno Setup** | установщик |

---

## 3. Структура кода

| Файл | Роль |
|---|---|
| `main.py` | весь UI на Flet: библиотека, карточки, диалоги, BigPicture-хост, wishlist-экран и плеер, удаление игр. Класс `CyberLauncher`. Константа `APP_VERSION`. |
| `game_manager.py` | бэкенд: модель `GameModel`, сканеры (Steam + диск), library.json, кэш обложек/hero, запуск/удаление игр, классы `GameManager`, `SteamScanner`, `DiskScanner`, `CoverAPIManager`, `CoverValidator`, `CoverUploader`. |
| `wishlist_manager.py` | раздел «Желаемое»: `WishlistItem`, `WishlistManager`, Steam Store API (`steam_search`, `steam_appdetails`, `fetch_game_details`), парсинг HLS-трейлеров, кэш деталей. |
| `gamepad_manager.py` | `GamepadManager` — опрос геймпада через SDL2, маппинг кнопок. |
| `bigpicture_view.py` | `BigPictureView` — крупный TV-режим (карусель, hero-арт, скринсейвер). |
| `Build.py` | сборка через PyInstaller (перезапускает себя из venv, проверяет зависимости). |
| `installer.iss` | Inno Setup скрипт. `MyAppVersion` обязан совпадать с `APP_VERSION`. |

---

## 4. Где лежат данные (runtime)

Всё в `%APPDATA%\CyberLauncher\` (в dev-режиме — рядом с исходниками):

```
%APPDATA%\CyberLauncher\
├─ data\
│  ├─ library.json          # игры + коллекции
│  ├─ wishlist.json         # список желаемого
│  └─ settings.json         # настройки (темы, excluded_paths, ...)
├─ cache\
│  ├─ icons\<hash>.jpg       # обложки
│  ├─ heroes\<uid>[_<ts>].jpg# landscape-арт для BigPicture/деталей
│  └─ wishlist_details\<appid>.json  # кэш деталей игр (TTL 7 дней)
└─ launcher.log             # ВАЖНО: главный источник диагностики
```

> Путь определяется `get_app_data_dir()` в `game_manager.py`. Frozen-сборка
> пишет в APPDATA, dev (`python main.py`) — в cwd.

---

## 5. Как собирать (важный рабочий процесс)

```powershell
# 1. Закрыть запущенный лаунчер (иначе PyInstaller не очистит dist\):
#    он держит flet.exe и .dll
# 2. Сборка .exe (Build.py сам перезапустится через venv):
python Build.py
# 3. Установщик:
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

Артефакты: `dist\CyberLauncher\CyberLauncher.exe` и
`installer_output\CyberLauncher_Setup.exe`.

**Чек-лист при каждом релизе:**
1. Поднять версию **в ДВУХ местах**: `APP_VERSION` в `main.py` и
   `MyAppVersion` в `installer.iss` (должны совпадать — иначе непонятно,
   обновился ли пользователь; версия видна в «О приложении» и заголовке окна).
2. Перед сборкой закрыть лаунчер (kill `CyberLauncher.exe` + наш `flet.exe`
   из `dist\`).
3. После — закоммитить и запушить.

---

## 6. Что уже реализовано

- **Сканирование библиотеки**: Steam (через `libraryfolders.vdf` +
  `appmanifest`), системные игры (обход дисков по эвристикам, выбор
  главного `.exe`).
- **Обложки**: 8-уровневый каскад (кэш → SteamGridDB → Steam CDN → RAWG →
  иконка из exe …), ручная загрузка с файла/URL.
- **BigPicture** (F11): крупный TV-интерфейс, навигация геймпадом (SDL2),
  hero-арт, скринсейвер.
- **Коллекции, избранное, поиск, сортировка, пагинация.**
- **Запуск от админа** (per-game, через UAC/ShellExecuteW).
- **Раздел «Желаемое» (Steam wishlist):**
  - поиск и добавление игр через Steam Store API;
  - 3-уровневый приоритет (зелёный/жёлтый/серый огонёк) + сортировка;
  - **детальный экран** игры по клику: hero-баннер (`library_hero` 1920×620),
    мета-чипы (дата/цена/Metacritic/платформы), описание, особенности,
    **скриншоты** (встроенный лайтбокс с навигацией), **трейлеры**
    (встроенный плеер на media_kit);
  - **кэш деталей** (память + диск, TTL 7 дней) + кнопка «Обновить».
- **Удаление игр с компьютера прямо из лаунчера** (диалог свойств → шестерёнка):
  - Steam-игры → `steam://uninstall/<appid>` + фоновое слежение за папкой
    (карточка исчезает, когда Steam реально удалит файлы; отмена в Steam —
    игра остаётся);
  - системные игры → папка в **Корзину** (send2trash legacy ctypes),
    с защитными проверками пути.

---

## 7. Над чем работаем / WIP / что хотим доделать

### Видео-плеер трейлеров (в разделе «Желаемое») — актуальное состояние
Плеер использует **нативную панель media_kit** (`show_controls=True`) —
перемотка/громкость/скорость/fullscreen с авто-скрытием. Своя Flet-панель
выпилена: контролы поверх нативной видео-поверхности всё равно не получают ввод.
Точка входа — `_show_trailer_player(url, name, store_url)` в `main.py`
(играет и mp4, и HLS). Историю кастомной панели см. в git.

**Стабильность HLS решена локальным прокси (2026-07):** сбои
`ffurl_read 0xffffff76` = TCP-таймауты akamai (ETIMEDOUT), mpv их сам не
переподключает, а flet_video 0.80 не пробрасывает опции реконнекта mpv. Модуль
`trailer_proxy.py` (`TrailerProxy`) поднимает локальный HTTP-сервер на
127.0.0.1: mpv ходит на него, прокси качает сегменты у CDN **с ретраями**,
браузерными заголовками и префетчем. HLS-ссылки заворачиваются в прокси
автоматически; mp4/DASH — напрямую; фоллбек на прямой URL, если прокси не
завёлся. Только stdlib — Build.py править не нужно.

**Fullscreen (W6, исправлено):** нативный fullscreen media_kit отслеживается
событиями `on_enter_fullscreen`/`on_exit_fullscreen`; закрытие плеера выходит из
него через `player.fullscreen=False`.

**Ручной выбор качества убран (v1.9.5), но теперь реализуем через прокси:**
раньше синтетический локальный плейлист media_kit не парсил как `file://`
(`Failed to recognize file format`); по HTTP через прокси (правильный
Content-Type) он должен заработать — хелперы `parse_hls_variants` /
`build_single_quality_m3u8` в `wishlist_manager` живы. Пока не подключено к UI.

**Что ещё можно доделать:**
- выбор качества в шапке плеера (через прокси, см. выше);
- дисковый кэш сегментов трейлеров (повторный просмотр офлайн/мгновенно);
- HW-декод: сейчас выключен против фризов — попробовать `hwdec=auto-copy`
  (копия кадров в RAM обходит texture-sharing с Flutter), нужен живой тест.

### Прочие идеи на будущее
- кэширование изображений (скриншоты/баннеры) на диск, а не только в памяти
  сессии Flet;
- доработки UI раздела «Желаемое».

---

## 8. Важные технические решения и «грабли» (collected gotchas)

Эти вещи уже стоили времени — учитывать при правках:

1. **PyInstaller-бандлинг.** `Build.py` обязательно запускать из **venv**
   (системный Python не имеет pygame/flet_video и т.п.). В сборку добавлены
   `--collect-all pygame`, `--collect-all flet_video`, hidden-imports для
   `send2trash.win.legacy`. `flet_desktop` уже содержит `libmpv-2.dll` /
   `media_kit_video_plugin.dll`, поэтому видео работает без `flet build`.

2. **`ft.AlertDialog` в этой версии Flet ненадёжно закрывается** (кнопка
   «Закрыть» не срабатывает). Решение — кастомный overlay-модал. Хелпер
   `_open_card_modal(title, body, actions)` в `main.py`; тот же паттерн у
   wishlist-диалога и плеера (backdrop + центрированная карточка).

3. **Контролы поверх media_kit-видео не получают события** (нативная
   поверхность) → управление выносим ПОД видео. Все методы `flet_video`
   (`play`, `seek`, `get_*`) — **async**.

3a. **Обработчики событий — прямой `async def`, а не
   `lambda e: page.run_task(coro)`.** Проверено: `run_task` из синхронного
   колбэка события корутину НЕ исполняет (в логах — тишина), а Flet 0.80 сам
   awaitит async-обработчик (`base_control.py:314`). Назначать
   `control.on_click = my_async_fn` напрямую.

3b. **Диффинг Flet ненадёжен для некоторых свойств.** Смена `ft.Icon.name`
   или `ft.Dropdown.options` + `update()` НЕ перерисовывается (а `.value` у
   Text/Slider — да). Лечится заменой объекта: `btn.content = ft.Icon(new)` /
   пересборкой контрола. Обновлять **конкретный контрол** (`control.update()`),
   `page.update()` для вложенных в overlay ненадёжен.

4. **Steam сменил схему трейлеров.** У новых игр нет `movies[].mp4/webm`, есть
   `hls_h264 / dash_h264 / dash_av1` (HLS/DASH-манифесты). Парсер в
   `fetch_game_details` поддерживает обе схемы; mpv играет HLS. Akamai режет
   дефолтный UA ffmpeg → шлём браузерный `User-Agent` + `Referer`.

5. **Убийство «зомби» flet.exe при старте** — только **наши** процессы (по
   пути), иначе клали чужие Flet-приложения (DayPlanner). См.
   `_kill_orphan_flet_processes` в `main.py`.

6. **Нормализация `icon_path`** при загрузке library.json (старые
   относительные пути после переезда кэша в APPDATA) и **`legacy_uids`** у
   `GameModel` — чтобы hero-арт не терялся, когда DiskScanner перевыбрал
   `.exe` и uid сменился (`_recover_orphan_heroes`).

7. **Удаление папок — только с защитой** (`_validate_deletable_dir`): не
   корень диска, не Windows/Program Files/AppData/home, и `.exe` игры должен
   лежать **внутри** install_path.

8. **Диагностика — `launcher.log`** в `%APPDATA%\CyberLauncher\`. Многие баги
   ловятся именно по нему (ошибки плеера, миграции кэша, удаление).

9. **Путь к проекту обязан быть ASCII без пробелов** (только латиница, цифры,
   `_`/`-`). Flet поднимает нативный Flutter-клиент `flet.exe`, который **не
   стартует, если путь к нему содержит не-ASCII символы** (напр. кириллицу).
   Симптом в dev-режиме (`python main.py`): окно не открывается, `ft.run()`
   мгновенно возвращается, в логе через ~1с — `App process exiting (finally
   block)` и больше ничего. Собранный `.exe` от этого НЕ страдает (PyInstaller
   распаковывается в `%TEMP%\_MEI…`, тоже ASCII), поэтому баг виден только при
   запуске из исходников. Дело именно в пути, **не** в переносе venv/смене диска
   (venv привязан к `home = C:\Python314`, расположение проекта ему безразлично).
   Лечение: держать проект в ASCII-пути (напр. `C:\MyGameLauncher`, `D:\Launcher`).
   История грабли: переезд `C:\MyGameLauncher` → `C:\Мои проекты\Launcher` сломал
   dev-запуск именно из-за «Мои проекты».

10. **Инварианты кэша обложек (введены при фиксах 2026-07-03, не ломать).**
    - Имя кэш-файла обложки считается ТОЛЬКО через
      `CoverAPIManager.cover_cache_path()`; чтение из кэша — через
      `find_cached_cover()` (ловит и ts-варианты). Раньше сканеры считали имя
      как `md5(clean_name)`, а `get_cover` писал в `md5(md5(clean_name))` →
      кэш system-игр не находился, каскад API гонялся на каждом скане.
    - Ручные обложки: `custom_<md5(uid)[:12]>_<ts_ms>.jpg`. Префикс `custom_`
      = «не удалять при cleanup»; смена `<ts>` на каждую загрузку обходит
      Flutter ImageCache (ключует по пути — иначе повторная загрузка не видна
      до перезапуска). Та же грабля решена и у hero-арта (`set_custom_hero_art`).
    - `refresh_cover` (API-поиск) переименовывает результат в `<name>_<ts>.jpg`
      (cache-bust) и сносит старый файл ТОЛЬКО после успешного скачивания.
    - Auto-sweep/rescan НЕ удаляют игру, если недоступен корень её диска
      (`GameManager._drive_available`) — иначе отвал диска стирал игры и обложки.
    - Запись `library.json` защищена общим `threading.Lock` + уникальным именем
      `.json.<pid>.<tid>.tmp` (async `save_library` и sync `save_library_sync`
      писали в общий tmp → могли опубликовать битый JSON).

11. **Плеер трейлера — НАТИВНАЯ панель media_kit** (`show_controls=True`), не
    кастомная Flet-панель. Кнопка/клик трейлера в wishlist зовёт
    `_show_trailer_player(url, name, store_url)` — он играет и mp4, и HLS.
    «В браузере» для HLS-ссылок открывает страницу Steam (браузер иначе качает
    .m3u8) — см. `_open_trailer_in_browser`. `build_wishlist_item` заполняет
    `trailer_url` из mp4/webm ИЛИ hls/dash (у новых игр только манифест).
    - **HLS идёт через `trailer_proxy.TrailerProxy`** (локальный HTTP на
      127.0.0.1) — ретраи против таймаутов akamai. Детекция HLS по токенам
      `.m3u8`/`/hls_`; mp4/DASH — напрямую; фоллбек на прямой URL. Сессия
      закрывается в `_close()` плеера, сервер — в `shutdown()`.
    - **Fullscreen (W6):** отслеживается `on_enter/on_exit_fullscreen`, выход —
      `player.fullscreen=False` в `_close()`. `page.window.full_screen` тут ни
      при чём (это fullscreen плеера, не окна).
    - **`page.on_resize`** (не `on_resized`!) в Flet 0.80 подстраивает размеры
      открытого плеера под окно.
    - ⚠️ **`filter_quality=HIGH` у `fv.Video` на Windows = ЧЁРНЫЙ экран**
      (cubic-фильтрация не работает для внешней D3D-текстуры media_kit; звук и
      таймлайн идут, кадра нет — проверено 2026-07-23). Значение берётся из
      `settings.json → trailer_filter_quality` (low/medium, дефолт medium).
    - **Пробел/f/m/стрелки в плеере обрабатывает сама панель media_kit**
      (встроенные шорткаты). Свой глобальный Space-тоггл ДУБЛИРОВАЛ их (пауза
      ставилась и тут же снималась) — не добавлять.
    - **Событие Dropdown в Flet 0.80 — `on_select`**, не `on_change`
      (присвоение on_change молча игнорируется — его нет в полях dataclass).
    - **Регион Steam Store — с фоллбеком** (`set_steam_region`, дефолт
      cc=RU + fallback=KZ, настраивается `settings.json → steam_cc /
      steam_cc_fallback`). Часть игр не продаётся в RU (007 First Light):
      `storesearch` их не показывает, `appdetails` даёт `success=false`.
      Поиск сливает регионы ЧЕРЕДОВАНИЕМ ПО РАНГУ (иначе DLC из основного
      региона забивают лимит и сама игра выпадает); `appdetails` при отказе
      повторяет запрос в фоллбеке (цена тогда в валюте фоллбека).
    - **Трейлеры новых игр играются в DASH AV1** (`dash_av1`), не HLS: при том
      же битрейте (Steam кодирует оба 5800k@1080p) AV1 заметно чище — браузерный
      плеер Steam играет именно его. dav1d в libmpv есть (проверено по DLL).
      HLS хранится фоллбеком (`trailer_url_hls` / `url_hls`): авто-откат при
      ошибке AV1 (`_on_trailer_error`) и принудительно через
      `settings.json → "trailer_codec": "hls"`. DASH через прокси БЕЗ
      переписывания манифеста: сегменты относительные — mpv сам приходит на
      `/s/<sid>/<путь>`. Старые записи wishlist.json мигрируют при сетевом
      обновлении деталей (`_maybe_refresh_trailer_fields`).

---

## 9. Полезные команды

```powershell
# Закрыть наш лаунчер (наш flet.exe — по пути dist):
Get-Process -Name CyberLauncher -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process flet -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.ToLower().StartsWith('c:\mygamelauncher\dist') } | Stop-Process -Force

# Хвост лога:
Get-Content "$env:APPDATA\CyberLauncher\launcher.log" -Tail 30
```
