import time
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController


class MacroPlayer:
    def __init__(self):
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.playing = False
        self.stop_flag = False
        self.current_loop = 0
        self.total_loops = 0  # -1 for infinite

    def play_macro(self, events, loops=1, speed=1.0):
        """
        Play macro events
        loops: number of times to repeat (-1 for infinite)
        speed: playback speed multiplier (2.0 = 2x speed, 0.5 = half speed)
        """
        self.playing = True
        self.stop_flag = False
        self.current_loop = 0
        self.total_loops = loops

        loop_count = 0
        while (loops == -1 or loop_count < loops) and not self.stop_flag:
            # Update loop index at the beginning of each iteration so UI shows 1-based progress
            self.current_loop = loop_count + 1
            last_time = 0

            for event in events:
                if self.stop_flag:
                    break

                # Skip control/meta events or events missing timing
                event_type = event.get("type")
                if event_type is None:
                    print(f"❌ [DEBUG] Event type is None: {event}")
                    continue
                if event_type == "__stop_request__":
                    continue
                event_time = event.get("time")
                if not isinstance(event_time, (int, float)):
                    continue

                # Wait for the appropriate time
                wait_time = (event_time - last_time) / speed
                if wait_time > 0:
                    time.sleep(wait_time)
                last_time = event_time

                # Execute the event
                self.execute_event(event)

            loop_count += 1

        self.playing = False

    def execute_event(self, event):
        event_type = event.get("type", "")

        if event_type == "mouse_move":
            self.mouse.position = (event["x"], event["y"])

        elif event_type == "mouse_click":
            button = self.parse_button(event["button"])
            if event["pressed"]:
                self.mouse.press(button)
            else:
                self.mouse.release(button)

        elif event_type == "mouse_scroll":
            self.mouse.scroll(event["dx"], event["dy"])

        elif event_type == "key_press":
            key = self.parse_key(event["key"])
            self.keyboard.press(key)

        elif event_type == "key_release":
            key = self.parse_key(event["key"])
            self.keyboard.release(key)

    def parse_button(self, button_str):
        if "left" in button_str.lower():
            return Button.left
        elif "right" in button_str.lower():
            return Button.right
        elif "middle" in button_str.lower():
            return Button.middle
        return Button.left

    def parse_key(self, key_str):
        # Handle special keys
        if key_str.startswith("Key."):
            key_name = key_str.replace("Key.", "")
            return getattr(Key, key_name, key_str)
        return key_str

    def stop_playback(self):
        self.stop_flag = True
