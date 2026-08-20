"""Core application logic orchestrating recording and playback.

Provides CLI-mode hotkeys and bridges between the recorder and player.
"""

from __future__ import annotations

import json
import logging
import time
from threading import Thread
from typing import Any

from pynput import keyboard
from .MacroRecorder import (
    MacroRecorder,
    compress_mouse_moves,
    secure_input_state,
    trim_leading_idle,
)
from .MacroPlayer import MacroPlayer

Event = dict[str, Any]

# Virtual key codes for F5 used for OS-level suppression of the stop hotkey
F5_KEYCODE_MACOS = 96
F5_VKCODE_WINDOWS = 0x74


class MacroApp:
    """High-level controller that manages recorder/player and hotkeys."""

    def __init__(self) -> None:
        self.recorder = MacroRecorder()
        self.player = MacroPlayer()
        self.macro_data: list[Event] = []
        self.hotkey_listener: keyboard.Listener | None = None
        self.file_hotkey_listener: keyboard.GlobalHotKeys | None = None
        self.running = True
        # Max seconds between events (None = no limit)
        self.max_idle_time: float | None = None
        self.playback_speed = 1.0  # Playback speed multiplier
        self.compress_moves = True  # Drop insignificant mouse moves after recording

    def setup_hotkeys(self) -> None:
        """Configure global hotkeys for CLI mode (not used by GUI)."""

        # Global hotkeys with pynput
        def on_key_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            try:
                if key == keyboard.Key.f1:
                    self.start_recording()
                elif key == keyboard.Key.f2:
                    self.stop_recording()
                elif key == keyboard.Key.f3:
                    self.play_once()
                elif key == keyboard.Key.f4:
                    self.play_infinite()
                elif key == keyboard.Key.f5:
                    self.stop_playback()
                elif key == keyboard.Key.esc:
                    print("ESC pressed - exiting...")
                    self.running = False
            except AttributeError:
                pass

        def darwin_intercept(event_type: int, event: Any) -> Any:
            # macOS: while playing, swallow F5 at the event-tap level so the
            # focused app doesn't also react (e.g. a browser refreshing the
            # page). on_key_press has already run by the time this returns.
            try:
                import Quartz

                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                if keycode == F5_KEYCODE_MACOS and self.player.playing:
                    return None
            except Exception:
                logging.exception("Error in darwin F5 intercept")
            return event

        def win32_event_filter(msg: int, data: Any) -> bool:
            # Windows only (ignored on Linux): swallow F5 while playing.
            # suppress_event() raises to signal suppression and skips
            # on_key_press, so stop playback here and don't catch it.
            if (
                getattr(data, "vkCode", None) == F5_VKCODE_WINDOWS
                and self.player.playing
            ):
                self.stop_playback()
                if self.hotkey_listener is not None:
                    # win32-only runtime method, absent from the stubs
                    self.hotkey_listener.suppress_event()  # type: ignore[attr-defined]
            return True

        self.hotkey_listener = keyboard.Listener(
            on_press=on_key_press,
            darwin_intercept=darwin_intercept,
            win32_event_filter=win32_event_filter,
        )
        self.hotkey_listener.start()

        # Save/load combos advertised in the CLI banner
        try:
            self.file_hotkey_listener = keyboard.GlobalHotKeys(
                {
                    "<ctrl>+<shift>+s": self.save_current_macro,
                    "<ctrl>+<shift>+l": self.load_macro_file,
                }
            )
            self.file_hotkey_listener.start()
        except Exception as e:
            logging.warning("Failed to register save/load hotkeys: %s", e)
            self.file_hotkey_listener = None

    def start_recording(self) -> None:
        """Start recording if not already recording or playing."""
        if not self.recorder.recording and not self.player.playing:
            print("Recording started...")
            state = secure_input_state()
            if state is not None:
                who = (
                    "an app that has since exited - log out and back in to " "clear it"
                    if state.stale
                    else f"held by {state.holder or 'another app'}"
                )
                print(
                    f"  WARNING: macOS Secure Input is on ({who}). Key presses "
                    "are being withheld, so keystrokes and the F2 stop hotkey "
                    "will not be recorded."
                )
            self.recorder.start_recording()
        else:
            logging.debug("Cannot start recording - already recording or playing")

    def stop_recording(self) -> None:
        """Stop recording and capture recorded events into macro_data."""
        if self.recorder.recording:
            print("Recording stopped")
            self.recorder.stop_recording()
            with self.recorder._events_lock:
                events = list(self.recorder.events)

            # Post-process: start playback immediately and drop move spam
            events = trim_leading_idle(events)
            if self.compress_moves:
                before = len(events)
                events = compress_mouse_moves(events)
                dropped = before - len(events)
                if dropped:
                    logging.debug("Compressed %d insignificant mouse moves", dropped)
            self.macro_data = events
            print(f"Recorded {len(self.macro_data)} events")

            # Diagnostic: show timing info to verify idle periods are captured
            if self.macro_data:
                # Filter out control events for timing analysis
                timed_events = [
                    e
                    for e in self.macro_data
                    if e.get("type") not in ("__stop_request__", "__system_message__")
                    and isinstance(e.get("time"), (int, float))
                ]
                if timed_events:
                    first_time = timed_events[0].get("time", 0)
                    last_time = timed_events[-1].get("time", 0)
                    duration = last_time - first_time
                    logging.debug(
                        "Timing: first=%.2fs, last=%.2fs, duration=%.2fs",
                        first_time,
                        last_time,
                        duration,
                    )

    def _play(self, loops: int) -> None:
        """Start playback in a background thread with current settings."""
        Thread(
            target=self.player.play_macro,
            args=(self.macro_data, loops),
            kwargs={
                "speed": self.playback_speed,
                "max_idle_time": self.max_idle_time,
            },
        ).start()

    def play_once(self) -> None:
        """Play current macro once."""
        if self.macro_data and not self.player.playing:
            print("Playing macro once...")
            self._play(1)

    def play_infinite(self) -> None:
        """Play current macro in an infinite loop (F5 to stop)."""
        if self.macro_data and not self.player.playing:
            print("Playing macro infinitely (F5 to stop)...")
            self._play(-1)

    def play_x_times(self, times: int) -> None:
        """Play current macro a fixed number of times."""
        if self.macro_data and not self.player.playing:
            print(f"Playing macro {times} times...")
            self._play(times)

    def stop_playback(self) -> None:
        """Stop playback if currently playing."""
        if self.player.playing:
            print("Playback stopped")
            self.player.stop_playback()

    def save_current_macro(self) -> None:
        """Save current macro to a timestamped JSON file (CLI mode)."""
        if self.macro_data:
            filename = f"macro_{int(time.time())}.json"
            with open(filename, "w") as f:
                json.dump(list(self.macro_data), f, indent=2)
            print(f"Saved to {filename}")
        else:
            print("No macro to save - record one first")

    def load_macro_file(self) -> None:
        """Load a macro from a JSON file path entered by the user (CLI mode)."""
        filename = input("Enter macro filename: ")
        try:
            self.recorder.load_macro(filename)
            with self.recorder._events_lock:
                self.macro_data = list(self.recorder.events)
            print(f"Loaded {len(self.macro_data)} events")
        except Exception as e:
            print(f"Error loading file: {e}")

    def run(self) -> None:
        """Run CLI loop with global hotkeys until exit."""
        self.setup_hotkeys()
        print("""
        Macro Recorder Ready!
        ========================
        F1 - Start Recording
        F2 - Stop Recording
        F3 - Play Once
        F4 - Play Infinitely
        F5 - Stop Playback
        Ctrl+Shift+S - Save Macro
        Ctrl+Shift+L - Load Macro
        ESC - Exit
        ========================
        """)

        try:
            while self.running:
                time.sleep(0.1)  # Keep program running
        except KeyboardInterrupt:
            pass
        finally:
            if self.hotkey_listener:
                self.hotkey_listener.stop()
            if self.file_hotkey_listener:
                self.file_hotkey_listener.stop()
            print("Exiting...")


if __name__ == "__main__":
    app = MacroApp()
    app.run()
