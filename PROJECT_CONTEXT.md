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

### Кастомный видео-плеер трейлеров (в разделе «Желаемое»)
media_kit рисует видео **нативной поверхностью**, которая перехватывает ввод
и игнорирует z-порядок Flet → нативную панель отключили (`show_controls=False`)
и пишем **свою** на чистом Flet. Сделано и работает: play/pause (иконка через
замену content), перемотка (slider + опрос позиции 0.25с), громкость + mute,
in-app максимайз, спиннер загрузки, отключён HW-декод против фризов, кнопка
«В браузере» как fallback. Обработчики — **прямые `async def`** в on_click/
on_change (НЕ через `page.run_task` из sync-лямбды — та корутину не исполняет).

**Ручной выбор качества убран (v1.9.5):** Steam-трейлеры — HLS с ОТДЕЛЬНОЙ
аудио-дорожкой, media_kit не парсит синтетический локальный плейлист
(`Failed to recognize file format`). В режиме Авто media_kit и так играет
лучшее качество (ABR). Хелперы парсинга HLS остались в `wishlist_manager`
(не используются — на случай другого подхода).

**Что ещё шероховато:**
- стабильность HLS Steam с akamai (бывают `ffurl_read 0xffffff76`);
- более плавная шкала; настоящий полноэкранный режим (сейчас in-app максимайз).

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

---

## 9. Полезные команды

```powershell
# Закрыть наш лаунчер (наш flet.exe — по пути dist):
Get-Process -Name CyberLauncher -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process flet -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.ToLower().StartsWith('c:\mygamelauncher\dist') } | Stop-Process -Force

# Хвост лога:
Get-Content "$env:APPDATA\CyberLauncher\launcher.log" -Tail 30
```
