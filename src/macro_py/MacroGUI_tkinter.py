import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
from .MacroApp import MacroApp


class MacroGUI:
    def __init__(self):
        self.app = MacroApp()
        self.root = tk.Tk()
        self.root.title("Macro Recorder")
        self.root.geometry("600x500")
        self.root.resizable(True, True)

        # GUI state
        self.log_visible = False
        self.last_event_count = 0
        self.last_mouse_pos = None
        self.mouse_move_count = 0

        # Create GUI elements
        self.setup_ui()

        # Timer for updating log
        self.update_log_periodically()

    def setup_ui(self):
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Configure grid weights
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)  # Log area gets extra space

        # Recording section
        record_frame = ttk.LabelFrame(main_frame, text="Recording", padding="5")
        record_frame.grid(
            row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10)
        )

        ttk.Button(
            record_frame, text="Start Recording", command=self.start_recording_gui
        ).grid(row=0, column=0, padx=(0, 5))
        ttk.Button(
            record_frame, text="Stop Recording", command=self.stop_recording_gui
        ).grid(row=0, column=1, padx=5)

        # Playback section
        play_frame = ttk.LabelFrame(main_frame, text="Playback", padding="5")
        play_frame.grid(
            row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10)
        )

        ttk.Button(play_frame, text="Play Once", command=self.play_once_gui).grid(
            row=0, column=0, padx=(0, 5)
        )
        ttk.Button(
            play_frame, text="Play Infinite", command=self.play_infinite_gui
        ).grid(row=0, column=1, padx=5)
        ttk.Button(
            play_frame, text="Stop Playback", command=self.stop_playback_gui
        ).grid(row=0, column=2, padx=5)

        # Loop control
        loop_frame = ttk.Frame(play_frame)
        loop_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(5, 0))

        ttk.Label(loop_frame, text="Loops:").grid(row=0, column=0, padx=(0, 5))
        self.loop_entry = ttk.Entry(loop_frame, width=5)
        self.loop_entry.insert(0, "5")
        self.loop_entry.grid(row=0, column=1, padx=(0, 5))
        ttk.Button(loop_frame, text="Play X Times", command=self.play_x).grid(
            row=0, column=2, padx=5
        )

        # File operations
        file_frame = ttk.Frame(main_frame)
        file_frame.grid(
            row=2, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10)
        )

        ttk.Button(file_frame, text="Save Macro", command=self.save_macro).grid(
            row=0, column=0, padx=(0, 5)
        )
        ttk.Button(file_frame, text="Load Macro", command=self.load_macro).grid(
            row=0, column=1, padx=5
        )

        # Log controls
        self.toggle_log_btn = ttk.Button(
            file_frame, text="Show Log Console", command=self.toggle_log_console
        )
        self.toggle_log_btn.grid(row=0, column=2, padx=5)

        ttk.Button(file_frame, text="Clear Log", command=self.clear_log).grid(
            row=0, column=3, padx=5
        )

        # Log console (initially hidden)
        self.log_frame = ttk.LabelFrame(main_frame, text="Event Log", padding="5")
        self.log_console = scrolledtext.ScrolledText(
            self.log_frame,
            height=12,
            state=tk.DISABLED,
            font=("Monaco", 10) if tk.sys.platform == "darwin" else ("Consolas", 10),
            bg="#1e1e1e",
            fg="white",
            insertbackground="white",
        )
        self.log_console.pack(fill=tk.BOTH, expand=True)

        # Status bar
        self.status_var = tk.StringVar()
        self.status_var.set("Ready - Click Start Recording to begin")
        status_bar = ttk.Label(
            main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W
        )
        status_bar.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(5, 0))

    def start_recording_gui(self):
        if not self.app.recorder.recording and not self.app.player.playing:
            try:
                self.status_var.set("🔄 Starting recording...")

                # Show log console
                self.show_log_console()
                self.log_message("📝 Initializing Recording Session...")
                self.log_message("=" * 50)

                # Reset counters
                self.last_event_count = 0
                self.last_mouse_pos = None
                self.mouse_move_count = 0

                # Start recording in a separate thread to avoid blocking the UI
                def start_recording_thread():
                    try:
                        self.app.start_recording(gui_mode=True)
                        # Update UI from main thread
                        self.root.after(0, self.recording_started_callback)
                    except Exception:
                        # Update UI from main thread
                        self.root.after(
                            0, lambda: self.recording_failed_callback(str(e))
                        )

                threading.Thread(target=start_recording_thread, daemon=True).start()

            except Exception as e:
                self.recording_failed_callback(str(e))
        else:
            self.status_var.set("Cannot start recording - already recording or playing")

    def recording_started_callback(self):
        """Called when recording starts successfully"""
        self.status_var.set("🔴 Recording started successfully!")
        self.log_message("✅ Recording Session Started Successfully")
        self.log_message("Monitoring mouse and keyboard events...")

    def recording_failed_callback(self, error_msg):
        """Called when recording fails to start"""
        self.status_var.set("❌ Recording failed - Check permissions")
        self.log_message(f"❌ Recording Failed: {error_msg}")
        self.log_message("")
        self.log_message("💡 Troubleshooting Tips:")
        self.log_message("• Go to System Preferences → Security & Privacy → Privacy")
        self.log_message("• Click 'Accessibility' and add your Terminal or Python")
        self.log_message("• Restart the application after granting permissions")

    def stop_recording_gui(self):
        if self.app.recorder.recording:
            self.app.stop_recording()
            self.status_var.set(
                f"⏹️ Recording stopped - {len(self.app.macro_data)} events recorded"
            )
            self.log_message("=" * 50)
            self.log_message(
                f"✅ Recording Complete: {len(self.app.macro_data)} events captured"
            )
        else:
            self.status_var.set("Not currently recording")

    def play_once_gui(self):
        if self.app.macro_data and not self.app.player.playing:
            self.app.play_once()
            self.status_var.set("▶️ Playing macro once...")
        else:
            self.status_var.set("No macro to play or already playing")

    def play_infinite_gui(self):
        if self.app.macro_data and not self.app.player.playing:
            self.app.play_infinite()
            self.status_var.set("🔁 Playing macro infinitely...")
        else:
            self.status_var.set("No macro to play or already playing")

    def stop_playback_gui(self):
        if self.app.player.playing:
            self.app.stop_playback()
            self.status_var.set("⏹️ Playback stopped")
        else:
            self.status_var.set("Not currently playing")

    def play_x(self):
        try:
            loops = int(self.loop_entry.get())
            if self.app.macro_data and not self.app.player.playing:
                self.app.play_x_times(loops)
                self.status_var.set(f"🔄 Playing macro {loops} times...")
            else:
                self.status_var.set("No macro to play or already playing")
        except ValueError:
            self.status_var.set("Invalid loop count")

    def save_macro(self):
        if not self.app.macro_data:
            messagebox.showwarning("No Macro", "No macro data to save")
            return

        filename = filedialog.asksaveasfilename(
            title="Save Macro",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if filename:
            self.app.recorder.events = self.app.macro_data
            self.app.recorder.save_macro(filename)
            self.status_var.set(f"Saved to {filename}")

    def load_macro(self):
        filename = filedialog.askopenfilename(
            title="Load Macro",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if filename:
            try:
                self.app.recorder.load_macro(filename)
                self.app.macro_data = self.app.recorder.events.copy()
                self.status_var.set(f"Loaded {len(self.app.macro_data)} events")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load macro: {str(e)}")

    def show_log_console(self):
        """Show the log console"""
        if not self.log_visible:
            self.log_frame.grid(
                row=3,
                column=0,
                columnspan=2,
                sticky=(tk.W, tk.E, tk.N, tk.S),
                pady=(0, 5),
            )
            self.log_visible = True
            self.toggle_log_btn.config(text="Hide Log Console")

    def hide_log_console(self):
        """Hide the log console"""
        if self.log_visible:
            self.log_frame.grid_remove()
            self.log_visible = False
            self.toggle_log_btn.config(text="Show Log Console")

    def toggle_log_console(self):
        """Toggle the visibility of the log console"""
        if self.log_visible:
            self.hide_log_console()
        else:
            self.show_log_console()

    def log_message(self, message):
        """Add a message to the log console"""
        self.log_console.config(state=tk.NORMAL)
        self.log_console.insert(tk.END, message + "\n")
        self.log_console.see(tk.END)
        self.log_console.config(state=tk.DISABLED)

    def clear_log(self):
        """Clear the log console"""
        self.log_console.config(state=tk.NORMAL)
        self.log_console.delete(1.0, tk.END)
        self.log_console.config(state=tk.DISABLED)
        if not self.app.recorder.recording:
            self.log_message("📝 Log Cleared - Ready for recording")

    def update_log_periodically(self):
        """Periodically update the log console with new events"""
        if self.app.recorder.recording and self.log_visible:
            current_count = len(self.app.recorder.events)
            if current_count > self.last_event_count:
                # Add new events to log
                new_events = self.app.recorder.events[self.last_event_count :]
                for event in new_events:
                    formatted_event = self.format_event(event)
                    if (
                        formatted_event
                    ):  # Only append if not None (filtered mouse moves)
                        self.log_message(formatted_event)

                self.last_event_count = current_count

        # Schedule next update
        self.root.after(100, self.update_log_periodically)

    def format_event(self, event):
        """Format a single event for display in the log"""
        event_type = event.get("type", "unknown")
        timestamp = f"{event.get('time', 0):.3f}s"

        if event_type == "mouse_move":
            x, y = event.get("x", 0), event.get("y", 0)
            # Reduce spam by only showing significant mouse movements
            if self.last_mouse_pos is None or (
                abs(x - self.last_mouse_pos[0]) > 10
                or abs(y - self.last_mouse_pos[1]) > 10
            ):
                self.last_mouse_pos = (x, y)
                self.mouse_move_count += 1
                return (
                    f"🖱️  [{timestamp}] Mouse Move #{self.mouse_move_count} → ({x}, {y})"
                )
            return None  # Skip this event

        elif event_type == "mouse_click":
            button = event.get("button", "unknown")
            action = "Press" if event.get("pressed") else "Release"
            x, y = event.get("x", 0), event.get("y", 0)
            return f"🖱️  [{timestamp}] Mouse {action} → {button} at ({x}, {y})"

        elif event_type == "mouse_scroll":
            dx, dy = event.get("dx", 0), event.get("dy", 0)
            x, y = event.get("x", 0), event.get("y", 0)
            return f"🖱️  [{timestamp}] Mouse Scroll → ({dx}, {dy}) at ({x}, {y})"

        elif event_type == "key_press":
            key = event.get("key", "unknown")
            return f"⌨️  [{timestamp}] Key Press → {key}"

        elif event_type == "key_release":
            key = event.get("key", "unknown")
            return f"⌨️  [{timestamp}] Key Release → {key}"

        else:
            return f"❓ [{timestamp}] Unknown Event → {event_type}"

    def run(self):
        """Start the GUI application"""
        # Don't setup global hotkeys in GUI mode - they conflict with GUI
        self.root.mainloop()


if __name__ == "__main__":
    gui = MacroGUI()
    gui.run()
