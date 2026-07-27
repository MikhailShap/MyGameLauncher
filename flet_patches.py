"""Локальные патчи Flet 0.80 — обход багов фреймворка, которые бьют по UI.

Применять один раз при старте: `apply_flet_patches()` — ПОСЛЕ импорта `flet`
(и `flet_video`), чтобы патч накрыл все уже созданные классы контролов.
Только stdlib, Build.py трогать не нужно (модуль добавлен в add_data/hidden).

Разбор кейса (2026-07-24, 23:37): пользователь играл, Battlefield 2042 съел
всю память и вылетел — Windows в этот момент записал в System-лог событие 26
«Слишком мало виртуальной памяти». Через 15 с окно лаунчера снова стало
активным, Flutter-клиент прислал `app_lifecycle_state_change`, Flet после
события сам дёрнул `page.update()` → диффер полез строить debug-строку с
repr'ом всего дерева контролов → память не выделилась → `MemoryError` →
Flet отправил клиенту SESSION_CRASHED и лаунчер показал краш-экран.

Отсюда два патча ниже: убрать бессмысленно дорогой repr (причина большого
выделения памяти) и не давать разовому MemoryError убивать UI-сессию.
"""

import gc
import logging

logger = logging.getLogger("CyberLauncher.backend")

_applied = False


def apply_flet_patches() -> None:
    """Идемпотентно применяет все патчи. Ошибки не фатальны — лаунчер должен
    стартовать даже если внутренности Flet в новой версии переехали."""
    global _applied
    if _applied:
        return
    _applied = True
    try:
        _patch_control_repr()
    except Exception as e:
        logger.warning(f"flet_patches: repr-патч не применён: {e}")
    try:
        _patch_memory_guard()
    except Exception as e:
        logger.warning(f"flet_patches: memory-guard не применён: {e}")


def _cheap_repr(self):
    """Тот же формат, что у BaseControl.__str__, но без обхода поддерева."""
    return f"{type(self).__name__}({getattr(self, '_i', '?')})"


def _patch_control_repr() -> None:
    """Дешёвый `__repr__` у всех классов контролов.

    В Flet 0.80 `DiffBuilder._compare_lists` (object_patch.py) начинается со
    строки::

        logger.debug(f"\\n_compare_lists: {path} {src} {dst}")

    f-string вычисляется ВСЕГДА, даже когда debug-логи выключены, а
    dataclass-`__repr__` контрола рекурсивно печатает всё поддерево. Списки
    сравниваются «старый против нового», так что на каждый `page.update()`
    строится repr двух копий поддерева. У нас в «Желаемом» 470+ карточек —
    это мегабайты строк на КАЖДЫЙ апдейт: впустую и по CPU, и по памяти
    (замер: 472 простые карточки ≈ 3.4 МБ и 33 мс на один такой repr).

    repr контролов Flet использует только в диагностике: протокол/патчи
    собираются из полей dataclass'а, поэтому подмена безопасна.
    """
    from flet.controls import base_control as bc

    count = 0

    def patch_cls(cls) -> None:
        nonlocal count
        if cls.__dict__.get("__repr__") is not _cheap_repr:
            cls.__repr__ = _cheap_repr
            count += 1

    def walk(cls) -> None:
        for sub in cls.__subclasses__():
            patch_cls(sub)
            walk(sub)

    patch_cls(bc.BaseControl)
    walk(bc.BaseControl)

    # Классы, которые появятся позже (напр. flet_video при первом открытии
    # плеера), создаются декоратором @control → _apply_control. Оборачиваем,
    # чтобы и они получили дешёвый repr.
    orig_apply = getattr(bc, "_apply_control", None)
    if callable(orig_apply) and not getattr(orig_apply, "_cheap_repr_patched", False):
        def apply_control_patched(cls, *args, **kwargs):
            cls = orig_apply(cls, *args, **kwargs)
            try:
                cls.__repr__ = _cheap_repr
            except Exception:
                pass
            return cls

        apply_control_patched._cheap_repr_patched = True
        bc._apply_control = apply_control_patched

    logger.info(f"flet_patches: дешёвый __repr__ у {count} классов контролов")


def _patch_memory_guard() -> None:
    """Разовый MemoryError не должен убивать UI-сессию.

    После КАЖДОГО события Flet сам зовёт `Session.after_event` →
    `page.update()` (пересчёт патча по дереву). Если ровно в этот момент в
    системе нет памяти — а это штатная ситуация сразу после вылета тяжёлой
    игры — MemoryError вылетает наружу, Flet ловит его в `dispatch_event` и
    шлёт клиенту SESSION_CRASHED: окно показывает трейсбек вместо интерфейса,
    хотя сам лаунчер жив (бэкенд в тот раз спокойно доработал: поднял окно и
    записал время игры).

    Поэтому: (1) глотаем MemoryError в авто-апдейте после события — UI просто
    пропускает один кадр, следующее действие пользователя его перерисует;
    (2) не даём отправить SESSION_CRASHED, если краш — это MemoryError.
    """
    from flet.messaging.session import Session

    orig_after_event = Session.after_event
    if not getattr(orig_after_event, "_mem_guard", False):
        async def after_event_guarded(self, control):
            try:
                await orig_after_event(self, control)
            except MemoryError:
                logger.error(
                    "Flet auto-update: MemoryError (в системе кончилась память) — "
                    "апдейт пропущен, сессия жива"
                )
                try:
                    gc.collect()
                except Exception:
                    pass

        after_event_guarded._mem_guard = True
        Session.after_event = after_event_guarded

    orig_error = Session.error
    if not getattr(orig_error, "_mem_guard", False):
        def error_guarded(self, message: str):
            if isinstance(message, str) and "MemoryError" in message:
                logger.error(
                    "Flet session error подавлен (MemoryError) — краш-экран не "
                    f"показываем:\n{message}"
                )
                try:
                    gc.collect()
                except Exception:
                    pass
                return
            return orig_error(self, message)

        error_guarded._mem_guard = True
        Session.error = error_guarded

    logger.info("flet_patches: memory-guard на page.update() установлен")
