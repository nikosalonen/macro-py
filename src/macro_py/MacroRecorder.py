import time
import json
import sys
import logging
import multiprocessing as mp
import threading
import queue
from pynput import mouse, keyboard

# Configure logging to help debug issues
logging.basicConfig(level=logging.INFO)


def _macro_listener_subprocess(event_queue: mp.Queue, stop_event: mp.Event) -> None:
    """Run pynput listeners in an isolated subprocess (macOS workaround).

    Sends event dicts to parent via event_queue. Exits when stop_event is set.
    """
    try:
        print("🔍 [SUB] Starting listener subprocess")
        start_time = time.time()

        # Local callbacks capture event_queue and start_time
        def on_move(x, y):
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
                print(f"⚠️ [SUB] on_move error: {e}")

        def on_click(x, y, button, pressed):
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
                print(f"⚠️ [SUB] on_click error: {e}")

        def on_scroll(x, y, dx, dy):
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
                print(f"⚠️ [SUB] on_scroll error: {e}")

        def on_key_press(key):
            try:
                # Intercept stop hotkey (F2) as a control event to parent
                try:
                    if key == keyboard.Key.f2:
                        try:
                            event_queue.put({"type": "__stop_request__"}, block=False)
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    key_name = key.char
                except AttributeError:
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
                print(f"⚠️ [SUB] on_key_press error: {e}")

        def on_key_release(key):
            try:
                try:
                    key_name = key.char
                except AttributeError:
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
                print(f"⚠️ [SUB] on_key_release error: {e}")

        # Create listeners
        m_listener = mouse.Listener(
            on_move=on_move,
            on_click=on_click,
            on_scroll=on_scroll,
            suppress=False,
        )
        k_listener = keyboard.Listener(
            on_press=on_key_press,
            on_release=on_key_release,
            suppress=False,
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
        print("🔍 [SUB] Listeners started")

        # Idle until asked to stop
        while not stop_event.is_set():
            time.sleep(0.05)

        print("🔍 [SUB] Stop event detected; stopping listeners")
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
        print(f"❌ [SUB] Listener subprocess error: {e}")
        sys.exit(1)
    finally:
        # Signal parent the child is exiting
        try:
            event_queue.put({"type": "__child_exit__"}, block=False)
        except Exception:
            pass
        print("🔍 [SUB] Listener subprocess exiting")


class MacroRecorder:
    def __init__(self):
        self.events = []
        self.recording = False
        self.start_time = None
        self.mouse_listener = None
        self.keyboard_listener = None
        self._is_darwin = sys.platform == "darwin"
        # macOS subprocess strategy
        self._mp_ctx = None
        self._event_queue = None
        self._proc = None
        self._stop_mp_event = None
        self._receiver_thread = None
        self._receiver_stop_event = None

    def start_recording(self):
        """Start recording with robust error handling"""
        print("🔍 [DEBUG] MacroRecorder.start_recording called")
        print("📝 Initializing macro recorder...")

        # Reset state
        print("🔍 [DEBUG] Resetting recorder state...")
        self.events = []
        self.recording = (
            False  # Will be set to True only if listeners start successfully
        )
        self.start_time = time.time()

        # macOS: run listeners in a subprocess to avoid CGEventTap + Qt crash
        if self._is_darwin:
            try:
                print("🔍 [DEBUG] Using macOS subprocess strategy for listeners")
                self._mp_ctx = mp.get_context("spawn")
                self._event_queue = self._mp_ctx.Queue(maxsize=10000)
                self._stop_mp_event = self._mp_ctx.Event()

                self._proc = self._mp_ctx.Process(
                    target=_macro_listener_subprocess,
                    args=(self._event_queue, self._stop_mp_event),
                    daemon=True,
                )
                self._proc.start()
                print("🔍 [DEBUG] Listener subprocess started")

                # Start queue consumer thread
                self._receiver_stop_event = threading.Event()
                self._receiver_thread = threading.Thread(
                    target=self._queue_consumer,
                    name="MacroQueueConsumer",
                    daemon=True,
                )
                self._receiver_thread.start()

                self.recording = True
                print("✅ Recording started successfully (subprocess mode)!")
                print(
                    "🔍 [DEBUG] MacroRecorder.start_recording completed successfully (subprocess)"
                )
            except Exception as e:
                error_msg = f"Failed to start recording (subprocess): {str(e)}"
                print(
                    f"🔍 [DEBUG] Exception in MacroRecorder.start_recording: {error_msg}"
                )
                print(f"🔍 [DEBUG] Exception type: {type(e).__name__}")
                import traceback

                print(f"🔍 [DEBUG] MacroRecorder traceback: {traceback.format_exc()}")
                self._cleanup_subprocess(force=True)
                raise RuntimeError(error_msg)
        else:
            try:
                # Create listeners first
                print("🔍 [DEBUG] About to create mouse listener...")
                print("🖱️ Creating mouse listener...")
                self.mouse_listener = mouse.Listener(
                    on_move=self.on_move,
                    on_click=self.on_click,
                    on_scroll=self.on_scroll,
                    suppress=False,
                )
                print("🔍 [DEBUG] Mouse listener created successfully")

                print("🔍 [DEBUG] About to create keyboard listener...")
                print("⌨️ Creating keyboard listener...")
                self.keyboard_listener = keyboard.Listener(
                    on_press=self.on_key_press,
                    on_release=self.on_key_release,
                    suppress=False,
                )
                print("🔍 [DEBUG] Keyboard listener created successfully")

                # Start listeners directly (no threading complications)
                print("🔍 [DEBUG] About to start mouse listener...")
                print("🚀 Starting mouse listener...")
                self.mouse_listener.start()
                # Ensure the listener is fully initialized before proceeding
                try:
                    self.mouse_listener.wait()
                    print("🔍 [DEBUG] Mouse listener wait() returned - listener ready")
                except Exception as wait_e:
                    print(f"🔍 [DEBUG] Mouse listener wait() raised: {wait_e}")
                print("🔍 [DEBUG] Mouse listener start() called")

                print("🔍 [DEBUG] About to start keyboard listener...")
                print("🚀 Starting keyboard listener...")
                self.keyboard_listener.start()
                # Ensure the listener is fully initialized before proceeding
                try:
                    self.keyboard_listener.wait()
                    print(
                        "🔍 [DEBUG] Keyboard listener wait() returned - listener ready"
                    )
                except Exception as wait_e:
                    print(f"🔍 [DEBUG] Keyboard listener wait() raised: {wait_e}")
                print("🔍 [DEBUG] Keyboard listener start() called")

                # Only mark as recording if both listeners started
                print("🔍 [DEBUG] Setting recording flag to True...")
                self.recording = True
                print("✅ Recording started successfully!")
                print("🔍 [DEBUG] MacroRecorder.start_recording completed successfully")

            except Exception as e:
                error_msg = f"Failed to start recording: {str(e)}"
                print(
                    f"🔍 [DEBUG] Exception in MacroRecorder.start_recording: {error_msg}"
                )
                print(f"🔍 [DEBUG] Exception type: {type(e).__name__}")
                import traceback

                print(f"🔍 [DEBUG] MacroRecorder traceback: {traceback.format_exc()}")
                print(f"❌ {error_msg}")

                # Clean up any partially started listeners
                print("🔍 [DEBUG] Cleaning up partially started listeners...")
                self.recording = False
                try:
                    if self.mouse_listener:
                        print("🔍 [DEBUG] Stopping mouse listener...")
                        self.mouse_listener.stop()
                except Exception as cleanup_e:
                    print(f"🔍 [DEBUG] Error stopping mouse listener: {cleanup_e}")
                try:
                    if self.keyboard_listener:
                        print("🔍 [DEBUG] Stopping keyboard listener...")
                        self.keyboard_listener.stop()
                except Exception as cleanup_e:
                    print(f"🔍 [DEBUG] Error stopping keyboard listener: {cleanup_e}")

                print("🔍 [DEBUG] About to raise RuntimeError")
                raise RuntimeError(error_msg)

    def stop_recording(self):
        """Stop recording with safe cleanup"""
        self.recording = False
        print("🛑 Stopping recording...")

        if self._is_darwin:
            # Signal child process to stop
            try:
                if self._stop_mp_event is not None:
                    self._stop_mp_event.set()
            except Exception as e:
                print(f"⚠️ Error signaling stop to subprocess: {e}")

            # Wait for receiver thread to exit after child sends exit signal
            if self._receiver_stop_event is not None:
                self._receiver_stop_event.set()

            # Join process
            try:
                if self._proc is not None:
                    self._proc.join(timeout=2.0)
                    if self._proc.is_alive():
                        print("⚠️ Subprocess did not exit in time; terminating")
                        self._proc.terminate()
                        self._proc.join(timeout=1.0)
            except Exception as e:
                print(f"⚠️ Error stopping subprocess: {e}")
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

            print("✅ Recording stopped successfully (subprocess mode)")
            return

        # Non-macOS: stop listeners in-process
        # Stop mouse listener safely
        if self.mouse_listener:
            try:
                print("🖱️ Stopping mouse listener...")
                self.mouse_listener.stop()
                print("✅ Mouse listener stopped")
            except Exception as e:
                print(f"⚠️ Error stopping mouse listener: {e}")
            finally:
                self.mouse_listener = None

        # Stop keyboard listener safely
        if self.keyboard_listener:
            try:
                print("⌨️ Stopping keyboard listener...")
                self.keyboard_listener.stop()
                print("✅ Keyboard listener stopped")
            except Exception as e:
                print(f"⚠️ Error stopping keyboard listener: {e}")
            finally:
                self.keyboard_listener = None

        print("✅ Recording stopped successfully")

    def _queue_consumer(self) -> None:
        """Consume events from subprocess and append to self.events."""
        print("🔍 [DEBUG] Queue consumer thread started")
        while True:
            if (
                self._receiver_stop_event is not None
                and self._receiver_stop_event.is_set()
            ):
                # Still drain quickly to avoid losing tail events
                try:
                    item = self._event_queue.get(timeout=0.2)
                except Exception:
                    break
            else:
                try:
                    item = self._event_queue.get(timeout=0.5)
                except Exception:
                    continue

            if not isinstance(item, dict):
                continue
            msg_type = item.get("type")
            if msg_type == "__child_exit__":
                print("🔍 [DEBUG] Received child exit sentinel")
                break
            if msg_type == "__error__":
                print(f"❌ Subprocess error: {item.get('message')}")
                continue
            if msg_type == "__stop_request__":
                # Append control event so GUI can react during its timer cycle
                try:
                    self.events.append(
                        {
                            "type": "__stop_request__",
                            "time": time.time() - (self.start_time or time.time()),
                        }
                    )
                except Exception:
                    pass
                continue

            # Regular event
            try:
                self.events.append(item)
            except Exception as e:
                print(f"⚠️ Error appending event: {e}")

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
                self._receiver_stop_event.set()
                self._receiver_thread.join(timeout=1.0)
            except Exception:
                pass
        self._receiver_thread = None
        self._receiver_stop_event = None

    def on_move(self, x, y):
        try:
            if self.recording:
                self.events.append(
                    {
                        "type": "mouse_move",
                        "x": x,
                        "y": y,
                        "time": time.time() - self.start_time,
                    }
                )
        except Exception as e:
            print(f"⚠️ on_move error: {e}")

    def on_click(self, x, y, button, pressed):
        try:
            if self.recording:
                self.events.append(
                    {
                        "type": "mouse_click",
                        "x": x,
                        "y": y,
                        "button": str(button),
                        "pressed": pressed,
                        "time": time.time() - self.start_time,
                    }
                )
        except Exception as e:
            print(f"⚠️ on_click error: {e}")

    def on_scroll(self, x, y, dx, dy):
        try:
            if self.recording:
                self.events.append(
                    {
                        "type": "mouse_scroll",
                        "x": x,
                        "y": y,
                        "dx": dx,
                        "dy": dy,
                        "time": time.time() - self.start_time,
                    }
                )
        except Exception as e:
            print(f"⚠️ on_scroll error: {e}")

    def on_key_press(self, key):
        try:
            if self.recording:
                # Intercept stop hotkey (F2) as a control event (non-macOS in-process)
                try:
                    if key == keyboard.Key.f2:
                        # Signal the GUI via a synthetic control event
                        self.events.append({"type": "__stop_request__"})
                        return
                except Exception:
                    pass

                try:
                    key_name = key.char
                except AttributeError:
                    key_name = str(key)

                self.events.append(
                    {
                        "type": "key_press",
                        "key": key_name,
                        "time": time.time() - self.start_time,
                    }
                )
        except Exception as e:
            print(f"⚠️ on_key_press error: {e}")

    def on_key_release(self, key):
        try:
            if self.recording:
                try:
                    key_name = key.char
                except AttributeError:
                    key_name = str(key)

                self.events.append(
                    {
                        "type": "key_release",
                        "key": key_name,
                        "time": time.time() - self.start_time,
                    }
                )
        except Exception as e:
            print(f"⚠️ on_key_release error: {e}")

    def save_macro(self, filename):
        with open(filename, "w") as f:
            json.dump(self.events, f, indent=2)

    def load_macro(self, filename):
        with open(filename, "r") as f:
            self.events = json.load(f)
