"""
GamepadManager — фоновый менеджер геймпада для CyberLauncher.

Использует SDL2 GameController API через `pygame._sdl2.controller`. Это даёт
встроенный маппинг кнопок (A/B/X/Y/LB/RB/Guide/Start/Back/D-pad/triggers/sticks)
для сотен моделей контроллеров — XInput, DInput, Bluetooth, USB. То же,
что используют Steam, RetroArch и большинство современных игр.

Это значит: контроллер работает в любом режиме (X/D/S, по 2.4G/USB/BT) без
переключений. SDL знает раскладку 8BitDo Ultimate 2 в DInput и автоматически
выдаёт кнопки в стандартном виде.

Если SDL не распознаёт контроллер как GameController (редкий случай — древняя
модель), fallback на низкоуровневый pygame.joystick с XInput-маппингом.

Если pygame не установлен — GamepadManager работает как no-op, лаунчер
функционирует без геймпада.
"""
import threading
import time
import logging
from typing import Callable, Optional, Dict

logger = logging.getLogger("GamepadManager")

try:
    import pygame
    import pygame._sdl2.controller as sdl_controller
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False
    pygame = None
    sdl_controller = None


# Маппинг SDL CONTROLLER_BUTTON_* → логические имена в нашем UI
def _build_button_map():
    if not HAS_PYGAME:
        return {}
    return {
        pygame.CONTROLLER_BUTTON_A: "A",
        pygame.CONTROLLER_BUTTON_B: "B",
        pygame.CONTROLLER_BUTTON_X: "X",
        pygame.CONTROLLER_BUTTON_Y: "Y",
        pygame.CONTROLLER_BUTTON_LEFTSHOULDER: "LB",
        pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: "RB",
        pygame.CONTROLLER_BUTTON_BACK: "BACK",
        pygame.CONTROLLER_BUTTON_START: "START",
        pygame.CONTROLLER_BUTTON_LEFTSTICK: "LSTICK",
        pygame.CONTROLLER_BUTTON_RIGHTSTICK: "RSTICK",
    }


def _build_dpad_map():
    if not HAS_PYGAME:
        return {}
    return {
        pygame.CONTROLLER_BUTTON_DPAD_UP: "DPAD_UP",
        pygame.CONTROLLER_BUTTON_DPAD_DOWN: "DPAD_DOWN",
        pygame.CONTROLLER_BUTTON_DPAD_LEFT: "DPAD_LEFT",
        pygame.CONTROLLER_BUTTON_DPAD_RIGHT: "DPAD_RIGHT",
    }


class GamepadManager:
    """Фоновый polling геймпада. Доставляет события через колбэки.

    Колбэки вызываются из polling-thread'а — обработчик должен сам
    переправить работу в UI thread (например через page.run_task).
    """

    POLL_HZ = 30
    DEADZONE = 0.25
    REPEAT_INITIAL = 0.4   # сек до начала автоповтора hold
    REPEAT_INTERVAL = 0.1  # сек между повторами
    GUIDE_DEBOUNCE = 0.4   # минимальная пауза между двумя on_guide событиями

    def __init__(self):
        self.available = HAS_PYGAME
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False

        self.on_button: Optional[Callable[[str, bool], None]] = None
        self.on_dpad: Optional[Callable[[str, bool], None]] = None
        self.on_axis: Optional[Callable[[str, float], None]] = None
        self.on_guide: Optional[Callable[[], None]] = None
        self.on_connected: Optional[Callable[[str, int, bool], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        # Состояние
        self._controller = None
        self._joystick_fallback = None  # если SDL Controller не получился
        self._last_button_state: Dict[int, bool] = {}
        self._last_dpad_state: Dict[str, bool] = {}
        self._hold_state: Dict[str, float] = {}  # name -> next-fire timestamp
        self._last_axis_values: Dict[str, float] = {}
        self._last_guide_time: float = 0.0
        self._guide_was_pressed: bool = False
        self._last_reinit: float = 0.0

        self._button_map = _build_button_map()
        self._dpad_map = _build_dpad_map()

    def start(self):
        if not self.available:
            logger.info("GamepadManager: pygame не установлен, геймпад отключён")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="GamepadPoll", daemon=True)
        self._thread.start()
        logger.info("GamepadManager started (SDL2 GameController API)")

    def shutdown(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.available:
            try:
                if self._controller is not None:
                    self._controller.quit()
            except Exception:
                pass
            try:
                sdl_controller.quit()
                pygame.joystick.quit()
                pygame.quit()
            except Exception:
                pass
        logger.info("GamepadManager stopped")

    def is_connected(self) -> bool:
        return self._controller is not None or self._joystick_fallback is not None

    def rumble(self, low_frequency: float = 0.3, high_frequency: float = 0.3, duration_ms: int = 100):
        """Запускает вибрацию контроллера. Безопасно если контроллер не подключён.

        low_frequency / high_frequency — 0.0..1.0 (мощность двух моторов).
        duration_ms — длительность в миллисекундах."""
        try:
            ctrl = self._controller
            if ctrl is not None and hasattr(ctrl, "rumble"):
                ctrl.rumble(low_frequency, high_frequency, duration_ms)
        except Exception:
            pass  # Не падаем если rumble не поддерживается контроллером

    # --- internals ---

    def _run(self):
        try:
            pygame.init()
            pygame.joystick.init()
            sdl_controller.init()
            # SDL_GAMECONTROLLER_USE_BUTTON_LABELS = 1 — стандартно
            # Подгрузим встроенную SDL_GameControllerDB (есть в SDL2)
        except Exception as e:
            logger.error(f"pygame/SDL init failed: {e}")
            return

        period = 1.0 / self.POLL_HZ
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"Gamepad poll tick error: {e}")
            time.sleep(period)

    def _tick(self):
        # Обработка SDL events нужна чтобы SDL обновлял внутреннее состояние
        pygame.event.pump()

        # 1. Обнаружение подключения
        try:
            count = sdl_controller.get_count()
        except Exception:
            count = 0

        if self._controller is None and self._joystick_fallback is None:
            # Перепробуем найти и подключиться
            now = time.time()
            # Settling: после reinit SDL нужно ~200ms чтобы прочитать
            # дескрипторы устройства. Пытаться сразу — поймаем ghost
            # joystick (0 buttons / 0 axes), от которого толку ноль.
            if now - self._last_reinit < 0.25:
                return
            if count > 0:
                # Пробуем через SDL Controller сначала
                attached = False
                for idx in range(count):
                    try:
                        if sdl_controller.is_controller(idx):
                            self._controller = sdl_controller.Controller(idx)
                            self._controller.init()
                            name = self._controller.name if hasattr(self._controller, "name") else "Controller"
                            try:
                                # Уточнить имя через joystick API
                                joy = self._controller.as_joystick()
                                name = joy.get_name()
                            except Exception:
                                pass
                            logger.info(
                                f"SDL Controller connected: {name} "
                                f"(SDL GameController mapping active)"
                            )
                            self._safe_call(self.on_connected, name, 11, True)  # SDL даёт стандарт
                            attached = True
                            break
                    except Exception as e:
                        logger.warning(f"Controller init error idx={idx}: {e}")
                        self._controller = None

                # Fallback: SDL не считает это GameController — пробуем raw joystick
                if not attached:
                    try:
                        joy_count = pygame.joystick.get_count()
                        if joy_count > 0:
                            joy = pygame.joystick.Joystick(0)
                            joy.init()
                            name = joy.get_name()
                            nb = joy.get_numbuttons()
                            na = joy.get_numaxes()
                            nh = joy.get_numhats()

                            # Ghost-fallback: SDL зарегистрировал устройство,
                            # но не успел прочитать дескрипторы. Отклоняем
                            # такой joystick и форсим reinit — на следующем
                            # тике SDL должен выдать настоящие значения.
                            if nb == 0 and na == 0 and nh == 0:
                                logger.warning(
                                    f"Ghost joystick (0/0/0) detected: {name}. "
                                    "Forcing reinit, retry on next tick."
                                )
                                try:
                                    joy.quit()
                                except Exception:
                                    pass
                                try:
                                    sdl_controller.quit()
                                    pygame.joystick.quit()
                                    sdl_controller.init()
                                    pygame.joystick.init()
                                except Exception as e:
                                    logger.warning(f"Ghost-reinit failed: {e}")
                                self._last_reinit = time.time()
                                # _joystick_fallback остаётся None, пробуем заново
                                return

                            self._joystick_fallback = joy
                            logger.warning(
                                f"Fallback to raw joystick: {name} "
                                f"(buttons={nb}, axes={na}, hats={nh}). "
                                "Buttons may be mismapped."
                            )
                            self._safe_call(self.on_connected, name, nb, False)
                    except Exception:
                        self._joystick_fallback = None
            else:
                # Ничего не подключено — раз в 1 сек переинициализация
                # (раньше было 3с — слишком долго после reconnect геймпада)
                if now - self._last_reinit > 1.0:
                    self._last_reinit = now
                    try:
                        sdl_controller.quit()
                        pygame.joystick.quit()
                        sdl_controller.init()
                        pygame.joystick.init()
                    except Exception:
                        pass
            return

        # 2. Polling уже подключённого контроллера
        if self._controller is not None:
            self._poll_sdl_controller()
        elif self._joystick_fallback is not None:
            self._poll_raw_joystick()

    # --- SDL Controller polling ---

    def _poll_sdl_controller(self):
        try:
            attached = self._controller.attached()
        except Exception:
            attached = False
        if not attached:
            try:
                self._controller.quit()
            except Exception:
                pass
            self._controller = None
            self._reset_state()
            # Полный reinit SDL controller subsystem при disconnect.
            # Без этого SDL держит старый instance-id и не видит reconnect
            # того же устройства (ghost). Reinit сразу — не ждём 1 сек.
            try:
                logger.info("Gamepad: reinit SDL after disconnect")
                sdl_controller.quit()
                pygame.joystick.quit()
                sdl_controller.init()
                pygame.joystick.init()
            except Exception as e:
                logger.warning(f"Reinit on disconnect failed: {e}")
            self._last_reinit = time.time()
            self._safe_call(self.on_disconnected)
            return

        # Кнопки A/B/X/Y/LB/RB/BACK/START/LSTICK/RSTICK
        for sdl_btn, name in self._button_map.items():
            try:
                pressed = bool(self._controller.get_button(sdl_btn))
            except Exception:
                continue
            prev = self._last_button_state.get(sdl_btn, False)
            if pressed != prev:
                self._last_button_state[sdl_btn] = pressed
                self._safe_call(self.on_button, name, pressed)

        # D-pad — объединяем физический D-pad и левый стик в общий логический
        # D-pad с автоповтором при удержании. Это даёт непрерывную прокрутку
        # списка игр при удержании стика влево/вправо.
        now = time.time()
        STICK_DPAD_THRESHOLD = 0.5

        # Какие направления сейчас "нажаты" (физика OR стик)
        active = set()
        for sdl_btn, name in self._dpad_map.items():
            try:
                if self._controller.get_button(sdl_btn):
                    active.add(name)
            except Exception:
                pass

        # Левый стик → виртуальный D-pad
        try:
            lx = self._controller.get_axis(pygame.CONTROLLER_AXIS_LEFTX) / 32767.0
            ly = self._controller.get_axis(pygame.CONTROLLER_AXIS_LEFTY) / 32767.0
        except Exception:
            lx, ly = 0.0, 0.0
        if lx > STICK_DPAD_THRESHOLD:
            active.add("DPAD_RIGHT")
        elif lx < -STICK_DPAD_THRESHOLD:
            active.add("DPAD_LEFT")
        if ly > STICK_DPAD_THRESHOLD:
            active.add("DPAD_DOWN")  # SDL: positive Y = down
        elif ly < -STICK_DPAD_THRESHOLD:
            active.add("DPAD_UP")

        for name in ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"):
            is_pressed = name in active
            was_pressed = self._last_dpad_state.get(name, False)

            if is_pressed and not was_pressed:
                self._last_dpad_state[name] = True
                self._hold_state[name] = now + self.REPEAT_INITIAL
                self._safe_call(self.on_dpad, name, True)
            elif not is_pressed and was_pressed:
                self._last_dpad_state[name] = False
                self._hold_state.pop(name, None)
                self._safe_call(self.on_dpad, name, False)
            elif is_pressed and was_pressed:
                next_fire = self._hold_state.get(name, now + self.REPEAT_INITIAL)
                if now >= next_fire:
                    self._hold_state[name] = now + self.REPEAT_INTERVAL
                    self._safe_call(self.on_dpad, name, True)

        # Аналоговые axis events (для возможного аналогового использования)
        self._emit_axis_sdl("LX", pygame.CONTROLLER_AXIS_LEFTX, is_trigger=False)
        self._emit_axis_sdl("LY", pygame.CONTROLLER_AXIS_LEFTY, is_trigger=False)
        self._emit_axis_sdl("RX", pygame.CONTROLLER_AXIS_RIGHTX, is_trigger=False)
        self._emit_axis_sdl("RY", pygame.CONTROLLER_AXIS_RIGHTY, is_trigger=False)
        self._emit_axis_sdl("LT", pygame.CONTROLLER_AXIS_TRIGGERLEFT, is_trigger=True)
        self._emit_axis_sdl("RT", pygame.CONTROLLER_AXIS_TRIGGERRIGHT, is_trigger=True)

        # Guide / Home / PS button — SDL даёт её напрямую
        try:
            guide_pressed = bool(self._controller.get_button(pygame.CONTROLLER_BUTTON_GUIDE))
        except Exception:
            guide_pressed = False
        if guide_pressed and not self._guide_was_pressed:
            if now - self._last_guide_time >= self.GUIDE_DEBOUNCE:
                self._last_guide_time = now
                self._safe_call(self.on_guide)
        self._guide_was_pressed = guide_pressed

    def _emit_axis_sdl(self, name: str, sdl_axis: int, is_trigger: bool):
        try:
            raw = self._controller.get_axis(sdl_axis)
        except Exception:
            return
        # SDL отдаёт -32768..32767 (signed 16-bit)
        v = raw / 32767.0
        if is_trigger:
            # Триггеры идут 0..32767 → 0..1
            v = max(0.0, v)
            if v < 0.05:
                v = 0.0
        else:
            if abs(v) < self.DEADZONE:
                v = 0.0
        prev = self._last_axis_values.get(name, 0.0)
        if abs(v - prev) > 0.05 or (v == 0.0 and prev != 0.0):
            self._last_axis_values[name] = v
            self._safe_call(self.on_axis, name, v)

    # --- Raw joystick fallback (древние/нераспознанные контроллеры) ---

    _RAW_BUTTON_NAMES = {
        0: "A", 1: "B", 2: "X", 3: "Y",
        4: "LB", 5: "RB", 6: "BACK", 7: "START",
        8: "LSTICK", 9: "RSTICK",
    }

    def _poll_raw_joystick(self):
        joy = self._joystick_fallback
        try:
            num_buttons = joy.get_numbuttons()
        except Exception:
            self._joystick_fallback = None
            self._reset_state()
            # Reinit pygame.joystick subsystem чтобы reconnect был быстрым
            try:
                logger.info("Gamepad: reinit pygame.joystick after disconnect")
                pygame.joystick.quit()
                sdl_controller.quit()
                sdl_controller.init()
                pygame.joystick.init()
            except Exception as e:
                logger.warning(f"Reinit on raw-joystick disconnect failed: {e}")
            self._last_reinit = time.time()
            self._safe_call(self.on_disconnected)
            return

        for btn in range(min(num_buttons, len(self._RAW_BUTTON_NAMES))):
            try:
                pressed = bool(joy.get_button(btn))
            except Exception:
                continue
            prev = self._last_button_state.get(btn, False)
            if pressed != prev:
                self._last_button_state[btn] = pressed
                name = self._RAW_BUTTON_NAMES.get(btn)
                if name:
                    self._safe_call(self.on_button, name, pressed)

        # Hat → DPAD
        try:
            num_hats = joy.get_numhats()
        except Exception:
            num_hats = 0
        active = set()
        if num_hats > 0:
            try:
                hx, hy = joy.get_hat(0)
                if hx > 0: active.add("DPAD_RIGHT")
                elif hx < 0: active.add("DPAD_LEFT")
                if hy > 0: active.add("DPAD_UP")
                elif hy < 0: active.add("DPAD_DOWN")
            except Exception:
                pass

        now = time.time()
        for name in ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"):
            was_pressed = self._last_dpad_state.get(name, False)
            is_pressed = name in active
            if is_pressed and not was_pressed:
                self._last_dpad_state[name] = True
                self._hold_state[name] = now + self.REPEAT_INITIAL
                self._safe_call(self.on_dpad, name, True)
            elif not is_pressed and was_pressed:
                self._last_dpad_state[name] = False
                self._hold_state.pop(name, None)
                self._safe_call(self.on_dpad, name, False)
            elif is_pressed and was_pressed:
                next_fire = self._hold_state.get(name, now + self.REPEAT_INITIAL)
                if now >= next_fire:
                    self._hold_state[name] = now + self.REPEAT_INTERVAL
                    self._safe_call(self.on_dpad, name, True)

    # --- helpers ---

    def _reset_state(self):
        self._last_button_state.clear()
        self._last_dpad_state.clear()
        self._hold_state.clear()
        self._last_axis_values.clear()

    @staticmethod
    def _safe_call(cb, *args):
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as e:
            logger.error(f"Gamepad callback error: {e}")
