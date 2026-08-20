"""Event recorder for mouse and keyboard.

Uses pynput to capture events. On macOS, runs listeners in a subprocess to
avoid CGEventTap conflicts with Qt, forwarding events to the parent process.
"""

from __future__ import annotations

import time
import json
import os
import re
import subprocess
import sys
import logging
import multiprocessing as mp
import threading
import queue
from typing import TYPE_CHECKING, Any, Iterable, NamedTuple

from pynput import mouse, keyboard

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event as MpEvent

Event = dict[str, Any]

#: macOS virtual keycode for F2, the stop-recording hotkey
F2_KEYCODE_MACOS = 120

#: Session key naming the process that turned on Secure Input
SECURE_INPUT_PID_RE = r'kCGSSessionSecureInputPID"\s*=\s*(\d+)'

# Configure logging to help debug issues
logging.basicConfig(level=logging.INFO)


def _macro_listener_subprocess(
    event_queue: mp.Queue[Event], stop_event: MpEvent
) -> None:
    """Run pynput listeners in an isolated subprocess (macOS workaround).

    Sends event dicts to parent via event_queue. Exits when stop_event is set.
    """
    log = logging.getLogger(__name__)
    try:
        log.debug("[SUB] Starting listener subprocess")
        start_time = time.time()

        # Local callbacks capture event_queue and start_time
        def on_move(x: float, y: float) -> None:
            try:
                event_queue.put(
                    {
                        "type": "mouse_move",
                        "x": x,
                        "y": y,
                        "time": time.time() - start_time,
                    },
                    block=False,
                )
            except Exception as e:
                log.warning("[SUB] on_move error: %s", e)

        def on_click(x: float, y: float, button: mouse.Button, pressed: bool) -> None:
            try:
                event_queue.put(
                    {
                        "type": "mouse_click",
                        "x": x,
                        "y": y,
                        "button": str(button),
                        "pressed": pressed,
                        "time": time.time() - start_time,
                    },
                    block=False,
                )
            except Exception as e:
                log.warning("[SUB] on_click error: %s", e)

        def on_scroll(x: float, y: float, dx: float, dy: float) -> None:
            try:
                event_queue.put(
                    {
                        "type": "mouse_scroll",
                        "x": x,
                        "y": y,
                        "dx": dx,
                        "dy": dy,
                        "time": time.time() - start_time,
                    },
                    block=False,
                )
            except Exception as e:
                log.warning("[SUB] on_scroll error: %s", e)

        def on_key_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            try:
                # Intercept stop hotkey (F2) as a control event to parent
                if key == keyboard.Key.f2:
                    try:
                        event_queue.put(
                            {
                                "type": "__stop_request__",
                                "time": time.time() - start_time,
                            },
                            block=False,
                        )
                    except Exception as e:
                        log.warning("[SUB] stop hotkey enqueue error: %s", e)
                    return

                if isinstance(key, keyboard.KeyCode) and key.char is not None:
                    key_name = key.char
                else:
                    key_name = str(key)
                event_queue.put(
                    {
                        "type": "key_press",
                        "key": key_name,
                        "time": time.time() - start_time,
                    },
                    block=False,
                )
            except Exception as e:
                log.warning("[SUB] on_key_press error: %s", e)

        def on_key_release(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            try:
                # F2 is the stop hotkey; its press became __stop_request__, so
                # drop the matching release instead of recording a stray F2.
                if key == keyboard.Key.f2:
                    return
                if isinstance(key, keyboard.KeyCode) and key.char is not None:
                    key_name = key.char
                else:
                    key_name = str(key)
                event_queue.put(
                    {
                        "type": "key_release",
                        "key": key_name,
                        "time": time.time() - start_time,
                    },
                    block=False,
                )
            except Exception as e:
                log.warning("[SUB] on_key_release error: %s", e)

        def darwin_intercept(event_type: int, event: Any) -> Any:
            """Swallow F2 so the stop hotkey never reaches the recorded app.

            Without this the keystroke also lands in whatever app is focused
            while recording. Returning None suppresses it system-wide; pynput
            has already run on_key_press by the time this is called.
            """
            try:
                import Quartz

                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                if keycode == F2_KEYCODE_MACOS:
                    return None
            except Exception as e:
                log.warning("[SUB] darwin_intercept error: %s", e)
            return event

        # Create listeners
        m_listener = mouse.Listener(
            on_move=on_move,
            on_click=on_click,
            on_scroll=on_scroll,
            suppress=False,
        )
        # This subprocess only runs on macOS, so darwin_intercept always applies
        k_listener = keyboard.Listener(
            on_press=on_key_press,
            on_release=on_key_release,
            suppress=False,
            darwin_intercept=darwin_intercept,
        )

        m_listener.start()
        k_listener.start()
        try:
            m_listener.wait()
        except Exception:
            pass
        try:
            k_listener.wait()
        except Exception:
            pass
        log.debug("[SUB] Listeners started")

        # Idle until asked to stop
        while not stop_event.is_set():
            time.sleep(0.05)

        log.debug("[SUB] Stop event detected; stopping listeners")
        try:
            m_listener.stop()
        except Exception:
            pass
        try:
            k_listener.stop()
        except Exception:
            pass
    except Exception as e:
        try:
            event_queue.put({"type": "__error__", "message": str(e)}, block=False)
        except Exception:
            pass
        log.error("[SUB] Listener subprocess error: %s", e)
        sys.exit(1)
    finally:
        # Signal parent the child is exiting
        try:
            event_queue.put({"type": "__child_exit__"}, block=False)
        except Exception:
            pass
        log.debug("[SUB] Listener subprocess exiting")


class SecureInputState(NamedTuple):
    """Why macOS is withholding key presses, and who asked for it."""

    #: App name holding Secure Input, or None when it could not be resolved
    holder: str | None
    #: True when the process that enabled it has exited without releasing it,
    #: which leaves the whole session stuck until the user logs out again
    stale: bool


def secure_input_state() -> SecureInputState | None:
    """Report macOS Secure Input, or None when it is off.

    While Secure Input is enabled - a focused password field, or an app such
    as a browser or a terminal with secure keyboard entry - event taps stop
    receiving key presses system-wide. Mouse events and modifier keys still
    arrive, so recording looks like it is working while every keystroke and
    the F2 stop hotkey are dropped without a word.
    """
    if sys.platform != "darwin":
        return None
    try:
        import objc
        from Foundation import NSBundle

        carbon = NSBundle.bundleWithPath_("/System/Library/Frameworks/Carbon.framework")
        namespace: dict[str, Any] = {}
        objc.loadBundleFunctions(
            carbon, namespace, [("IsSecureEventInputEnabled", b"B")]
        )
        is_enabled = namespace.get("IsSecureEventInputEnabled")
        if is_enabled is None or not is_enabled():
            return None
    except Exception:
        logging.debug("Could not query Secure Input state", exc_info=True)
        return None

    pid = _secure_input_pid()
    if pid is None:
        return SecureInputState(holder=None, stale=False)
    return SecureInputState(holder=_process_name(pid), stale=not _pid_is_alive(pid))


def _secure_input_pid() -> int | None:
    """PID recorded as holding Secure Input, if the session names one."""
    try:
        console = subprocess.run(
            ["ioreg", "-l", "-d", "1", "-k", "IOConsoleUsers"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        match = re.search(SECURE_INPUT_PID_RE, console)
        if match is None or match.group(1) == "0":
            return None
        return int(match.group(1))
    except Exception:
        logging.debug("Could not read the Secure Input PID", exc_info=True)
        return None


def _pid_is_alive(pid: int) -> bool:
    """Whether the process still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just owned by someone else
    except Exception:
        logging.debug("Could not probe pid %s", pid, exc_info=True)
        return True
    return True


def _process_name(pid: int) -> str | None:
    """Best-effort executable name for a pid."""
    try:
        command = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        return os.path.basename(command) or None
    except Exception:
        logging.debug("Could not name pid %s", pid, exc_info=True)
        return None


def compress_mouse_moves(
    events: Iterable[Event], min_distance: float = 3.0, min_interval: float = 0.05
) -> list[Event]:
    """Drop insignificant mouse_move events to shrink recordings.

    A move is kept if it is far enough (>= min_distance px) or late enough
    (>= min_interval s) relative to the last kept move. The move immediately
    preceding any non-move event is always kept so clicks/keys happen at the
    right cursor position. Non-move events are never touched.
    """
    result: list[Event] = []
    # last kept mouse_move (x, y, time)
    last_kept: tuple[float, float, float] | None = None
    # most recent dropped move, may be promoted before non-move
    pending: Event | None = None

    for event in events:
        if event.get("type") != "mouse_move":
            if pending is not None:
                result.append(pending)
                pending = None
            result.append(event)
            continue

        x, y, t = event.get("x"), event.get("y"), event.get("time")
        if (
            not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not isinstance(t, (int, float))
        ):
            result.append(event)
            continue

        if last_kept is None:
            result.append(event)
            last_kept = (x, y, t)
            continue

        dist = ((x - last_kept[0]) ** 2 + (y - last_kept[1]) ** 2) ** 0.5
        if dist >= min_distance or (t - last_kept[2]) >= min_interval:
            result.append(event)
            last_kept = (x, y, t)
            pending = None
        else:
            pending = event

    if pending is not None:
        result.append(pending)
    return result


def trim_leading_idle(events: Iterable[Event]) -> list[Event]:
    """Shift event times so playback starts immediately.

    Removes the idle gap between pressing the record hotkey and the first
    real event by subtracting the first event's timestamp from every event
    (control events like __stop_request__ included).
    """
    events = list(events)
    offset: float | None = None
    for event in events:
        if event.get("type") == "__stop_request__":
            continue
        t = event.get("time")
        if isinstance(t, (int, float)):
            offset = t
            break
    if not offset or offset <= 0:
        return list(events)

    trimmed: list[Event] = []
    for event in events:
        t = event.get("time")
        if isinstance(t, (int, float)):
            event = {**event, "time": max(0.0, t - offset)}
        trimmed.append(event)
    return trimmed


class MacroRecorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self._events_lock = threading.Lock()
        self.recording = False
        self.start_time: float | None = None
        self.mouse_listener: mouse.Listener | None = None
        self.keyboard_listener: keyboard.Listener | None = None
        self._is_darwin = sys.platform == "darwin"
        # macOS subprocess strategy
        self._mp_ctx: mp.context.SpawnContext | None = None
        self._event_queue: mp.Queue[Event] | None = None
        self._proc: mp.process.BaseProcess | None = None
        self._stop_mp_event: MpEvent | None = None
        self._receiver_thread: threading.Thread | None = None
        self._receiver_stop_event: threading.Event | None = None

    def start_recording(self) -> None:
        """Start recording with robust error handling"""
        logging.debug("MacroRecorder.start_recording called")

        # Reset state
        with self._events_lock:
            self.events = []
        self.recording = (
            False  # Will be set to True only if listeners start successfully
        )
        self.start_time = time.time()

        # macOS: run listeners in a subprocess to avoid CGEventTap + Qt crash
        if self._is_darwin:
            try:
                logging.debug("Using macOS subprocess strategy for listeners")
                self._mp_ctx = mp.get_context("spawn")
                self._event_queue = self._mp_ctx.Queue(maxsize=10000)
                self._stop_mp_event = self._mp_ctx.Event()

                self._proc = self._mp_ctx.Process(
                    target=_macro_listener_subprocess,
                    args=(self._event_queue, self._stop_mp_event),
                    daemon=True,
                )
                self._proc.start()
                logging.debug("Listener subprocess started")

                # Start queue consumer thread
                self._receiver_stop_event = threading.Event()
                self._receiver_thread = threading.Thread(
                    target=self._queue_consumer,
                    name="MacroQueueConsumer",
                    daemon=True,
                )
                self._receiver_thread.start()

                self.recording = True
                logging.info("Recording started (subprocess mode)")
            except Exception as e:
                error_msg = f"Failed to start recording (subprocess): {str(e)}"
                logging.exception(error_msg)
                self._cleanup_subprocess(force=True)
                raise RuntimeError(error_msg)
        else:
            try:
                # Create listeners first
                self.mouse_listener = mouse.Listener(
                    on_move=self.on_move,
                    on_click=self.on_click,
                    on_scroll=self.on_scroll,
                    suppress=False,
                )
                self.keyboard_listener = keyboard.Listener(
                    on_press=self.on_key_press,
                    on_release=self.on_key_release,
                    suppress=False,
                )

                # Start listeners and wait until each is fully initialized
                self.mouse_listener.start()
                try:
                    self.mouse_listener.wait()
                except Exception as wait_e:
                    logging.debug("Mouse listener wait() raised: %s", wait_e)

                self.keyboard_listener.start()
                try:
                    self.keyboard_listener.wait()
                except Exception as wait_e:
                    logging.debug("Keyboard listener wait() raised: %s", wait_e)

                # Only mark as recording if both listeners started
                self.recording = True
                logging.info("Recording started")

            except Exception as e:
                error_msg = f"Failed to start recording: {str(e)}"
                logging.exception(error_msg)

                # Clean up any partially started listeners
                self.recording = False
                try:
                    if self.mouse_listener:
                        self.mouse_listener.stop()
                except Exception as cleanup_e:
                    logging.debug("Error stopping mouse listener: %s", cleanup_e)
                try:
                    if self.keyboard_listener:
                        self.keyboard_listener.stop()
                except Exception as cleanup_e:
                    logging.debug("Error stopping keyboard listener: %s", cleanup_e)

                raise RuntimeError(error_msg)

    def stop_recording(self) -> None:
        """Stop recording with safe cleanup"""
        self.recording = False
        logging.debug("Stopping recording...")

        if self._is_darwin:
            # Signal child process to stop
            try:
                if self._stop_mp_event is not None:
                    self._stop_mp_event.set()
            except Exception as e:
                logging.warning("Error signaling stop to subprocess: %s", e)

            # Wait for receiver thread to exit after child sends exit signal
            if self._receiver_stop_event is not None:
                self._receiver_stop_event.set()

            # Join process
            try:
                if self._proc is not None:
                    self._proc.join(timeout=2.0)
                    if self._proc.is_alive():
                        logging.warning("Subprocess did not exit in time; terminating")
                        self._proc.terminate()
                        self._proc.join(timeout=1.0)
            except Exception as e:
                logging.warning("Error stopping subprocess: %s", e)
            finally:
                self._proc = None
                self._stop_mp_event = None

            # Drain and close queue
            try:
                if self._event_queue is not None:
                    while True:
                        try:
                            self._event_queue.get_nowait()
                        except queue.Empty:
                            break
            except Exception:
                pass
            finally:
                self._event_queue = None

            # Join receiver thread
            try:
                if self._receiver_thread is not None:
                    self._receiver_thread.join(timeout=1.0)
            except Exception:
                pass
            finally:
                self._receiver_thread = None
                self._receiver_stop_event = None

            logging.info("Recording stopped (subprocess mode)")
            return

        # Non-macOS: stop listeners in-process
        # Stop mouse listener safely
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
            except Exception as e:
                logging.warning("Error stopping mouse listener: %s", e)
            finally:
                self.mouse_listener = None

        # Stop keyboard listener safely
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
            except Exception as e:
                logging.warning("Error stopping keyboard listener: %s", e)
            finally:
                self.keyboard_listener = None

        logging.info("Recording stopped")

    def _queue_consumer(self) -> None:
        """Consume events from subprocess and append to self.events."""
        logging.debug("Queue consumer thread started")
        event_queue = self._event_queue
        stop_event = self._receiver_stop_event
        if event_queue is None or stop_event is None:
            return
        while True:
            if stop_event.is_set():
                # Still drain quickly to avoid losing tail events
                try:
                    item = event_queue.get(timeout=0.2)
                except Exception:
                    break
            else:
                try:
                    item = event_queue.get(timeout=0.5)
                except Exception:
                    continue

            if not isinstance(item, dict):
                continue
            msg_type = item.get("type")
            if msg_type == "__child_exit__":
                logging.debug("Received child exit sentinel")
                break
            if msg_type == "__error__":
                logging.error("Subprocess error: %s", item.get("message"))
                continue
            if msg_type == "__stop_request__":
                # Append control event so GUI can react during its timer cycle
                try:
                    with self._events_lock:
                        self.events.append(
                            {
                                "type": "__stop_request__",
                                "time": time.time() - (self.start_time or time.time()),
                            }
                        )
                except Exception:
                    logging.exception("Error appending control stop event")
                continue

            # Regular event
            try:
                with self._events_lock:
                    self.events.append(item)
            except Exception as e:
                logging.warning("Error appending event: %s", e)

    def _cleanup_subprocess(self, force: bool = False) -> None:
        """Best-effort cleanup of subprocess-related resources."""
        try:
            if self._stop_mp_event is not None:
                self._stop_mp_event.set()
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.join(timeout=1.0)
                if force and self._proc.is_alive():
                    self._proc.terminate()
                    self._proc.join(timeout=1.0)
        except Exception:
            pass
        self._proc = None
        self._stop_mp_event = None
        self._event_queue = None
        if self._receiver_thread is not None:
            try:
                if self._receiver_stop_event is not None:
                    self._receiver_stop_event.set()
                self._receiver_thread.join(timeout=1.0)
            except Exception:
                pass
        self._receiver_thread = None
        self._receiver_stop_event = None

    def on_move(self, x: float, y: float) -> None:
        try:
            if self.recording:
                now = time.time()
                with self._events_lock:
                    self.events.append(
                        {
                            "type": "mouse_move",
                            "x": x,
                            "y": y,
                            "time": now - (self.start_time or now),
                        }
                    )
        except Exception as e:
            logging.warning("on_move error: %s", e)

    def on_click(self, x: float, y: float, button: mouse.Button, pressed: bool) -> None:
        try:
            if self.recording:
                now = time.time()
                with self._events_lock:
                    self.events.append(
                        {
                            "type": "mouse_click",
                            "x": x,
                            "y": y,
                            "button": str(button),
                            "pressed": pressed,
                            "time": now - (self.start_time or now),
                        }
                    )
        except Exception as e:
            logging.warning("on_click error: %s", e)

    def on_scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        try:
            if self.recording:
                now = time.time()
                with self._events_lock:
                    self.events.append(
                        {
                            "type": "mouse_scroll",
                            "x": x,
                            "y": y,
                            "dx": dx,
                            "dy": dy,
                            "time": now - (self.start_time or now),
                        }
                    )
        except Exception as e:
            logging.warning("on_scroll error: %s", e)

    def on_key_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        try:
            if self.recording:
                # Intercept stop hotkey (F2) as a control event (non-macOS in-process)
                if key == keyboard.Key.f2:
                    # Compute timestamp deterministically (0.0 if start_time is None)
                    timestamp = (
                        0.0
                        if self.start_time is None
                        else time.time() - self.start_time
                    )
                    with self._events_lock:
                        self.events.append(
                            {"type": "__stop_request__", "time": timestamp}
                        )
                    return

                if isinstance(key, keyboard.KeyCode) and key.char is not None:
                    key_name = key.char
                else:
                    key_name = str(key)

                now = time.time()
                with self._events_lock:
                    self.events.append(
                        {
                            "type": "key_press",
                            "key": key_name,
                            "time": now - (self.start_time or now),
                        }
                    )
        except Exception:
            logging.exception("on_key_press error")

    def on_key_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        try:
            if self.recording:
                # F2 is the stop hotkey; its press became __stop_request__, so
                # drop the matching release instead of recording a stray F2.
                if key == keyboard.Key.f2:
                    return
                if isinstance(key, keyboard.KeyCode) and key.char is not None:
                    key_name = key.char
                else:
                    key_name = str(key)

                now = time.time()
                with self._events_lock:
                    self.events.append(
                        {
                            "type": "key_release",
                            "key": key_name,
                            "time": now - (self.start_time or now),
                        }
                    )
        except Exception as e:
            logging.warning("on_key_release error: %s", e)

    def save_macro(self, filename: str) -> None:
        with self._events_lock:
            snapshot = list(self.events)
        with open(filename, "w") as f:
            json.dump(snapshot, f, indent=2)

    def load_macro(self, filename: str) -> None:
        with open(filename, "r") as f:
            loaded = json.load(f)
        with self._events_lock:
            self.events = loaded

    def get_events_since(self, start_index: int) -> tuple[list[Event], int]:
        """Return a thread-safe slice of events since start_index and current count.

        Args:
            start_index: Starting index to slice from.

        Returns:
            tuple[list[dict], int]: (new_events, current_count)
        """
        with self._events_lock:
            current_count = len(self.events)
            if start_index < 0:
                start_index = 0
            if start_index > current_count:
                start_index = current_count
            new_events = self.events[start_index:current_count]
        # Return a shallow copy of the slice to avoid caller mutating our list
        return list(new_events), current_count
