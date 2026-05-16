"""Playback engine for executing recorded macro events.

Provides mouse/keyboard playback with loop control and defensive handling
for malformed events.
"""

import logging
import time
from collections.abc import Iterable
from typing import Any, cast

from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController

from ._types import MacroEvent


class MacroPlayer:
    """Plays back recorded macro events using pynput controllers."""

    def __init__(self) -> None:
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.playing = False
        self.stop_flag = False
        self.current_loop = 0
        self.total_loops = 0  # -1 for infinite

    def play_macro(
        self,
        events: Iterable[MacroEvent],
        loops: int = 1,
        speed: float = 1.0,
        max_idle_time: float | None = None,
    ) -> None:
        """Play a list of recorded events.

        - events: iterable of event dicts with 'type' and 'time' fields
        - loops: number of repetitions (-1 for infinite)
        - speed: playback speed multiplier (e.g., 2.0 = double speed)
        - max_idle_time: max seconds to wait between events (None = no limit)
        """
        # Normalize to list to support generators and allow multiple iterations
        events = list(events)

        self.playing = True
        self.stop_flag = False
        self.current_loop = 0
        self.total_loops = loops

        # Find the recording end time from __stop_request__ event (if present)
        recording_end_time: float | None = None
        for event in events:
            if event.get("type") == "__stop_request__":
                t = event.get("time")
                if isinstance(t, (int, float)):
                    recording_end_time = float(t)
                break

        loop_count = 0
        while (loops == -1 or loop_count < loops) and not self.stop_flag:
            # Update loop index at the beginning of each iteration so UI shows 1-based progress
            self.current_loop = loop_count + 1
            last_time = 0.0

            for event in events:
                if self.stop_flag:
                    break

                # Skip control/meta events or events missing timing
                event_type = event.get("type")
                if not event_type:
                    logging.debug("Skipping event without type")
                    continue
                if event_type == "__stop_request__":
                    continue
                event_time = event.get("time")
                if not isinstance(event_time, (int, float)):
                    continue
                event_time = float(event_time)

                # Wait for the appropriate time
                wait_time = (event_time - last_time) / speed
                # Cap wait time if max_idle_time is set
                if max_idle_time is not None and wait_time > max_idle_time:
                    wait_time = max_idle_time
                if wait_time > 0:
                    time.sleep(wait_time)
                last_time = event_time

                # Execute the event
                self.execute_event(event)

            # Wait for final idle period before next loop (time from last event to F2 press)
            if (
                not self.stop_flag
                and recording_end_time is not None
                and last_time < recording_end_time
            ):
                final_wait = (recording_end_time - last_time) / speed
                if max_idle_time is not None and final_wait > max_idle_time:
                    final_wait = max_idle_time
                if final_wait > 0:
                    time.sleep(final_wait)

            loop_count += 1

        self.playing = False

    def execute_event(self, event: MacroEvent) -> None:
        """Execute a single event dict if it contains required fields."""
        event_type = event.get("type", "")

        if event_type == "mouse_move":
            x = event.get("x")
            y = event.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                self.mouse.position = cast(tuple[int, int], (x, y))
            else:
                logging.debug("mouse_move missing/invalid coordinates; skipping")

        elif event_type == "mouse_click":
            button_str = event.get("button")
            pressed = event.get("pressed")
            if not isinstance(button_str, str) or not isinstance(pressed, bool):
                logging.debug("mouse_click missing button/pressed; skipping")
                return
            button = self.parse_button(button_str)
            if pressed:
                self.mouse.press(button)
            else:
                self.mouse.release(button)

        elif event_type == "mouse_scroll":
            dx = event.get("dx", 0)
            dy = event.get("dy", 0)
            if not isinstance(dx, (int, float)):
                dx = 0
            if not isinstance(dy, (int, float)):
                dy = 0
            self.mouse.scroll(cast(int, dx), cast(int, dy))

        elif event_type == "key_press":
            key_str = event.get("key")
            if not isinstance(key_str, str):
                logging.debug("key_press missing key; skipping")
                return
            key = self.parse_key(key_str)
            self.keyboard.press(key)

        elif event_type == "key_release":
            key_str = event.get("key")
            if not isinstance(key_str, str):
                logging.debug("key_release missing key; skipping")
                return
            key = self.parse_key(key_str)
            self.keyboard.release(key)

    def parse_button(self, button_str: str) -> Button:
        """Map a recorded button string to a pynput Button."""
        if "left" in button_str.lower():
            return Button.left
        elif "right" in button_str.lower():
            return Button.right
        elif "middle" in button_str.lower():
            return Button.middle
        return Button.left

    def parse_key(self, key_str: str) -> Any:
        """Map a recorded key string to a pynput Key or plain string."""
        # Handle special keys
        if key_str.startswith("Key."):
            key_name = key_str.replace("Key.", "")
            return getattr(Key, key_name, key_str)
        return key_str

    def stop_playback(self) -> None:
        """Signal the playback loop to stop after the current event."""
        self.stop_flag = True
