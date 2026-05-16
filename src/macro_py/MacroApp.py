"""Core application logic orchestrating recording and playback.

Provides CLI-mode hotkeys and bridges between the recorder and player.
"""

import time
from typing import Any
from threading import Thread
from pynput import keyboard
from .MacroRecorder import MacroRecorder
from .MacroPlayer import MacroPlayer
from ._types import MacroEvent


class MacroApp:
    """High-level controller that manages recorder/player and hotkeys."""

    def __init__(self) -> None:
        self.recorder = MacroRecorder()
        self.player = MacroPlayer()
        self.macro_data: list[MacroEvent] = []
        self.hotkey_listener: Any | None = None
        self.running = True
        self.max_idle_time: float | None = None  # Max seconds between events

    def setup_hotkeys(self) -> None:
        """Configure global hotkeys for CLI mode (not used by GUI)."""

        # Global hotkeys with pynput
        def on_key_press(key: Any) -> None:
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

        self.hotkey_listener = keyboard.Listener(on_press=on_key_press)
        self.hotkey_listener.start()

    def start_recording(self) -> None:
        """Start recording if not already recording or playing."""
        print("🔍 [DEBUG] MacroApp.start_recording called")
        if not self.recorder.recording and not self.player.playing:
            print(
                "🔍 [DEBUG] Conditions met in MacroApp - calling recorder.start_recording"
            )
            print("🔴 Recording started...")
            self.recorder.start_recording()
            print("🔍 [DEBUG] MacroApp.start_recording completed")
        else:
            print(
                "🔍 [DEBUG] Cannot start recording in MacroApp - already recording or playing"
            )

    def stop_recording(self) -> None:
        """Stop recording and capture recorded events into macro_data."""
        if self.recorder.recording:
            print("⏹️ Recording stopped")
            self.recorder.stop_recording()
            self.macro_data = self.recorder.events.copy()
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
                    first_time = timed_events[0]["time"]
                    last_time = timed_events[-1]["time"]
                    if not isinstance(first_time, (int, float)) or not isinstance(
                        last_time, (int, float)
                    ):
                        return
                    duration = last_time - first_time
                    print(
                        f"⏱️ Timing: first={first_time:.2f}s, last={last_time:.2f}s, duration={duration:.2f}s"
                    )

    def play_once(self) -> None:
        """Play current macro once."""
        if self.macro_data and not self.player.playing:
            print("▶️ Playing macro once...")
            Thread(
                target=self.player.play_macro,
                args=(self.macro_data, 1),
                kwargs={"max_idle_time": self.max_idle_time},
            ).start()

    def play_infinite(self) -> None:
        """Play current macro in an infinite loop (F5 to stop)."""
        if self.macro_data and not self.player.playing:
            print("🔁 Playing macro infinitely (F5 to stop)...")
            Thread(
                target=self.player.play_macro,
                args=(self.macro_data, -1),
                kwargs={"max_idle_time": self.max_idle_time},
            ).start()

    def play_x_times(self, times: int) -> None:
        """Play current macro a fixed number of times."""
        if self.macro_data and not self.player.playing:
            print(f"🔄 Playing macro {times} times...")
            Thread(
                target=self.player.play_macro,
                args=(self.macro_data, times),
                kwargs={"max_idle_time": self.max_idle_time},
            ).start()

    def stop_playback(self) -> None:
        """Stop playback if currently playing."""
        if self.player.playing:
            print("⏹️ Playback stopped")
            self.player.stop_playback()

    def save_current_macro(self) -> None:
        """Save current macro to a timestamped JSON file (CLI mode)."""
        if self.macro_data:
            filename = f"macro_{int(time.time())}.json"
            self.recorder.events = self.macro_data
            self.recorder.save_macro(filename)
            print(f"💾 Saved to {filename}")

    def load_macro_file(self) -> None:
        """Load a macro from a JSON file path entered by the user (CLI mode)."""
        # In a real app, you'd have a file dialog here
        filename = input("Enter macro filename: ")
        try:
            self.recorder.load_macro(filename)
            self.macro_data = self.recorder.events.copy()
            print(f"📂 Loaded {len(self.macro_data)} events")
        except Exception as e:
            print(f"Error loading file: {e}")

    def run(self) -> None:
        """Run CLI loop with global hotkeys until exit."""
        self.setup_hotkeys()
        print("""
        🎮 Macro Recorder Ready!
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
            print("Exiting...")


if __name__ == "__main__":
    app = MacroApp()
    app.run()
