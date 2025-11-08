import sys
import subprocess
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QPushButton,
    QLabel,
    QLineEdit,
    QGroupBox,
    QFileDialog,
    QStatusBar,
    QTextEdit,
    QSplitter,
    QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from .MacroApp import MacroApp


class MacroGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.app = MacroApp()
        self.setWindowTitle("Macro Recorder")
        self.setGeometry(100, 100, 600, 500)
        # Default to always-on-top
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Create splitter for controls and log
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Controls widget
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        self.setup_ui(controls_layout)
        splitter.addWidget(controls_widget)

        # Log console
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumHeight(200)
        self.log_console.hide()  # Initially hidden
        self.log_console.setStyleSheet(
            """
            QTextEdit {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: 'Monaco', 'Consolas', monospace;
                font-size: 11px;
                border: 1px solid #444;
            }
        """
        )
        splitter.addWidget(self.log_console)

        # Set splitter proportions
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        main_layout.addWidget(splitter)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready - Click Start Recording to begin")

        # Timer for updating log
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.update_log)
        self.last_event_count = 0
        self.last_mouse_pos = None
        self.mouse_move_count = 0
        self.was_hidden_for_recording = False
        self._restore_on_top_after_record = False
        self._restore_on_top_after_play = False
        self.prev_front_app_name = None

        # Timer for updating playback progress in the status bar
        self.play_progress_timer = QTimer()
        self.play_progress_timer.timeout.connect(self.update_play_progress)

        # In-window shortcuts (not global) for convenience
        QShortcut(QKeySequence("F1"), self, self.start_recording_gui)
        QShortcut(QKeySequence("F2"), self, self.stop_recording_gui)
        QShortcut(QKeySequence("F3"), self, self.play_once_gui)
        QShortcut(QKeySequence("F5"), self, self.stop_playback_gui)

    def setup_ui(self, layout):
        # Record buttons
        record_group = QGroupBox("Recording")
        record_layout = QVBoxLayout(record_group)

        record_btn = QPushButton("Start Recording")
        record_btn.clicked.connect(self.start_recording_gui)
        record_layout.addWidget(record_btn)

        stop_record_btn = QPushButton("Stop Recording")
        stop_record_btn.clicked.connect(self.stop_recording_gui)
        record_layout.addWidget(stop_record_btn)

        layout.addWidget(record_group)

        # Playback controls
        play_group = QGroupBox("Playback")
        play_layout = QVBoxLayout(play_group)

        play_once_btn = QPushButton("Play Once")
        play_once_btn.clicked.connect(self.play_once_gui)
        play_layout.addWidget(play_once_btn)

        play_infinite_btn = QPushButton("Play Infinite")
        play_infinite_btn.clicked.connect(self.play_infinite_gui)
        play_layout.addWidget(play_infinite_btn)

        # Loop count input
        loop_layout = QHBoxLayout()
        loop_layout.addWidget(QLabel("Loops:"))
        self.loop_entry = QLineEdit("5")
        self.loop_entry.setFixedWidth(50)
        loop_layout.addWidget(self.loop_entry)

        play_x_btn = QPushButton("Play")
        play_x_btn.clicked.connect(self.play_x)
        loop_layout.addWidget(play_x_btn)
        loop_layout.addStretch()

        play_layout.addLayout(loop_layout)

        stop_btn = QPushButton("Stop Playback")
        stop_btn.clicked.connect(self.stop_playback_gui)
        play_layout.addWidget(stop_btn)

        layout.addWidget(play_group)

        # File operations
        file_layout = QHBoxLayout()

        save_btn = QPushButton("Save Macro")
        save_btn.clicked.connect(self.save_macro)
        file_layout.addWidget(save_btn)

        load_btn = QPushButton("Load Macro")
        load_btn.clicked.connect(self.load_macro)
        file_layout.addWidget(load_btn)

        # Log controls
        log_controls_layout = QHBoxLayout()

        self.toggle_log_btn = QPushButton("Show Log Console")
        self.toggle_log_btn.setCheckable(True)
        self.toggle_log_btn.clicked.connect(self.toggle_log_console)
        log_controls_layout.addWidget(self.toggle_log_btn)

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.clicked.connect(self.clear_log)
        log_controls_layout.addWidget(clear_log_btn)

        # Always-on-top toggle
        self.always_on_top_checkbox = QCheckBox("Always on Top")
        self.always_on_top_checkbox.setChecked(True)
        self.always_on_top_checkbox.stateChanged.connect(self.on_always_on_top_changed)
        log_controls_layout.addWidget(self.always_on_top_checkbox)

        # Activate underlying app toggles
        self.activate_on_record_checkbox = QCheckBox("Activate app on Record")
        self.activate_on_record_checkbox.setChecked(True)
        log_controls_layout.addWidget(self.activate_on_record_checkbox)

        self.activate_on_play_checkbox = QCheckBox("Activate app on Play")
        self.activate_on_play_checkbox.setChecked(True)
        log_controls_layout.addWidget(self.activate_on_play_checkbox)

        log_controls_layout.addStretch()
        file_layout.addLayout(log_controls_layout)

        file_layout.addStretch()
        layout.addLayout(file_layout)

        # Shortcuts/help
        shortcuts_group = QGroupBox("Shortcuts")
        shortcuts_layout = QVBoxLayout(shortcuts_group)
        shortcuts_label = QLabel(
            "F1 – Start Recording (backgrounds window)\n"
            "F2 – Stop Recording (restores window)\n"
            "F3 – Play Once\n"
            "F5 – Stop Playback"
        )
        shortcuts_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        shortcuts_layout.addWidget(shortcuts_label)
        layout.addWidget(shortcuts_group)

    def start_recording_gui(self):
        if not self.app.recorder.recording and not self.app.player.playing:
            try:
                self.status_bar.showMessage("🔄 Starting recording...")

                # Instead of hiding, drop always-on-top and send window to background
                if self.isVisible():
                    # Remember if we need to restore always-on-top after recording
                    if self.always_on_top_checkbox.isChecked():
                        self._restore_on_top_after_record = True
                        # Uncheck triggers flag update via on_always_on_top_changed
                        self.always_on_top_checkbox.setChecked(False)
                    # Send behind other windows but keep taskbar entry and shortcuts active
                    self.lower()

                # On macOS, bring back the previously active app so the user can start right away
                if self.activate_on_record_checkbox.isChecked():
                    self.activate_previous_app()

                # Show log console first
                self.log_console.show()
                self.log_console.clear()
                self.log_console.append("📝 Initializing Recording Session...")
                self.log_console.append("=" * 50)

                # Update button text immediately
                self.toggle_log_btn.setText("Hide Log Console")
                self.toggle_log_btn.setChecked(True)

                # Initialize counters first
                self.last_event_count = 0
                self.last_mouse_pos = None
                self.mouse_move_count = 0

                # Defer recording startup to avoid PyQt6 event loop conflicts
                QTimer.singleShot(100, self._start_recording_delayed)

                self.status_bar.showMessage("🔄 Initializing listeners...")
                self.log_console.append("⏳ Starting listeners in background...")

            except Exception as e:
                # Recording failed - show error and clean up
                error_msg = f"❌ Failed to start recording: {str(e)}"
                print(error_msg)
                self.status_bar.showMessage("❌ Recording failed - Check permissions")

                # If we hid the window, restore it on failure
                if self.was_hidden_for_recording:
                    self.was_hidden_for_recording = False
                    self.show()

                self.log_console.append(f"❌ Recording Failed: {str(e)}")
                self.log_console.append("")
                self.log_console.append("💡 Troubleshooting Tips:")
                self.log_console.append(
                    "• Go to System Preferences → Security & Privacy → Privacy"
                )
                self.log_console.append(
                    "• Click 'Accessibility' and add your Terminal or Python"
                )
                self.log_console.append(
                    "• Restart the application after granting permissions"
                )

                # Reset button state
                self.toggle_log_btn.setText("Show Log Console")
                self.toggle_log_btn.setChecked(False)

        else:
            self.status_bar.showMessage(
                "Cannot start recording - already recording or playing"
            )

    def stop_recording_gui(self):
        if self.app.recorder.recording:
            self.app.stop_recording()
            self.status_bar.showMessage(
                f"⏹️ Recording stopped - {len(self.app.macro_data)} events recorded"
            )

            # Stop log timer and add summary
            if self.log_timer.isActive():
                self.log_timer.stop()
            self.log_console.append("=" * 50)
            self.log_console.append(
                f"✅ Recording Complete: {len(self.app.macro_data)} events captured"
            )

            # Restore GUI if we changed z-order/flags for recording
            if self._restore_on_top_after_record:
                self._restore_on_top_after_record = False
                # Restore always-on-top if it was previously enabled
                if not self.always_on_top_checkbox.isChecked():
                    self.always_on_top_checkbox.setChecked(True)
                # Bring window to front
                self.show()
                self.raise_()
                self.activateWindow()

        else:
            self.status_bar.showMessage("Not currently recording")

    def play_once_gui(self):
        if self.app.macro_data and not self.app.player.playing:
            # Send window to background for playback
            if self.isVisible():
                if self.always_on_top_checkbox.isChecked():
                    self._restore_on_top_after_play = True
                    self.always_on_top_checkbox.setChecked(False)
                self.lower()

            self.app.play_once()
            self.status_bar.showMessage("▶️ Running 1/1 loops")
            if not self.play_progress_timer.isActive():
                self.play_progress_timer.start(200)
            # Bring previous app to front if enabled
            if self.activate_on_play_checkbox.isChecked():
                self.activate_previous_app()
        else:
            self.status_bar.showMessage("No macro to play or already playing")

    def play_infinite_gui(self):
        if self.app.macro_data and not self.app.player.playing:
            # Send window to background for playback
            if self.isVisible():
                if self.always_on_top_checkbox.isChecked():
                    self._restore_on_top_after_play = True
                    self.always_on_top_checkbox.setChecked(False)
                self.lower()

            self.app.play_infinite()
            self.status_bar.showMessage("🔁 Running 1/∞ loops")
            if not self.play_progress_timer.isActive():
                self.play_progress_timer.start(200)
            # Bring previous app to front if enabled
            if self.activate_on_play_checkbox.isChecked():
                self.activate_previous_app()
        else:
            self.status_bar.showMessage("No macro to play or already playing")

    def stop_playback_gui(self):
        if self.app.player.playing:
            self.app.stop_playback()
            self.status_bar.showMessage("⏹️ Playback stopped")
            if self.play_progress_timer.isActive():
                self.play_progress_timer.stop()
            # Play completion sound on manual stop
            QApplication.beep()
            # Restore window if we backgrounded it for playback
            if self._restore_on_top_after_play:
                self._restore_on_top_after_play = False
                if not self.always_on_top_checkbox.isChecked():
                    self.always_on_top_checkbox.setChecked(True)
                self.show()
                self.raise_()
                self.activateWindow()
        else:
            self.status_bar.showMessage("Not currently playing")

    def play_x(self):
        try:
            loops = int(self.loop_entry.text())
            if self.app.macro_data and not self.app.player.playing:
                # Send window to background for playback
                if self.isVisible():
                    if self.always_on_top_checkbox.isChecked():
                        self._restore_on_top_after_play = True
                        self.always_on_top_checkbox.setChecked(False)
                    self.lower()

                self.app.play_x_times(loops)
                self.status_bar.showMessage(f"🔄 Running 1/{loops} loops")
                if not self.play_progress_timer.isActive():
                    self.play_progress_timer.start(200)
                # Bring previous app to front if enabled
                if self.activate_on_play_checkbox.isChecked():
                    self.activate_previous_app()
            else:
                self.status_bar.showMessage("No macro to play or already playing")
        except ValueError:
            self.status_bar.showMessage("Invalid loop count")

    def update_play_progress(self):
        player = self.app.player
        if not player.playing:
            if self.play_progress_timer.isActive():
                self.play_progress_timer.stop()
            # Play completion sound on natural finish
            QApplication.beep()
            # Restore window when playback completes naturally
            if self._restore_on_top_after_play:
                self._restore_on_top_after_play = False
                if not self.always_on_top_checkbox.isChecked():
                    self.always_on_top_checkbox.setChecked(True)
                self.show()
                self.raise_()
                self.activateWindow()
            return
        # Ensure at least 1 is shown when first loop starts
        current = player.current_loop or 1
        total = player.total_loops
        if total == -1:
            self.status_bar.showMessage(f"🔁 Running {current}/∞ loops")
        else:
            self.status_bar.showMessage(f"🔄 Running {current}/{total} loops")

    def save_macro(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Macro", "", "JSON files (*.json)"
        )
        if filename:
            if not filename.endswith(".json"):
                filename += ".json"
            self.app.recorder.events = self.app.macro_data
            self.app.recorder.save_macro(filename)
            self.status_bar.showMessage(f"Saved to {filename}")

    def load_macro(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Macro", "", "JSON files (*.json)"
        )
        if filename:
            self.app.recorder.load_macro(filename)
            self.app.macro_data = self.app.recorder.events.copy()
            self.status_bar.showMessage(f"Loaded {len(self.app.macro_data)} events")

    def update_log(self):
        """Update the log console with new events in real-time"""
        if not self.app.recorder.recording:
            return

        current_count = len(self.app.recorder.events)
        if current_count > self.last_event_count:
            # Add new events to log
            new_events = self.app.recorder.events[self.last_event_count :]
            for event in new_events:
                # Handle control stop request coming from subprocess (F2)
                if event.get("type") == "__stop_request__":
                    # Stop and restore window
                    self.stop_recording_gui()
                    # Skip logging this control event
                    continue
                formatted_event = self.format_event(event)
                if formatted_event:  # Only append if not None (filtered mouse moves)
                    self.log_console.append(formatted_event)

            self.last_event_count = current_count

            # Auto-scroll to bottom
            scrollbar = self.log_console.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

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

    def toggle_log_console(self):
        """Toggle the visibility of the log console"""
        if self.log_console.isVisible():
            self.log_console.hide()
            self.toggle_log_btn.setText("Show Log Console")
            self.toggle_log_btn.setChecked(False)
        else:
            self.log_console.show()
            self.toggle_log_btn.setText("Hide Log Console")
            self.toggle_log_btn.setChecked(True)

    def clear_log(self):
        """Clear the log console"""
        self.log_console.clear()
        if not self.app.recorder.recording:
            self.log_console.append("📝 Log Cleared - Ready for recording")

    def capture_prev_front_app(self):
        """Capture the currently frontmost app (macOS) to reactivate later when recording starts."""
        if sys.platform != "darwin":
            return
        try:
            name = (
                subprocess.check_output(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of (first process whose frontmost is true)',
                    ]
                )
                .decode("utf-8")
                .strip()
            )
            if name:
                self.prev_front_app_name = name
        except Exception:
            pass

    def activate_previous_app(self):
        """Reactivate the previously frontmost application on macOS.

        Falls back to a single Cmd+Tab if the previous name is unknown.
        """
        if sys.platform != "darwin":
            return
        try:
            if self.prev_front_app_name:
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "{self.prev_front_app_name}" to activate',
                    ],
                    check=False,
                )
            else:
                # Fallback: single Cmd+Tab to previous app in MRU list
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to key code 48 using {command down}',
                    ],
                    check=False,
                )
        except Exception:
            pass

    def on_always_on_top_changed(self, state):
        enabled = state == Qt.CheckState.Checked
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        # Re-show to apply flag change on macOS/Qt
        if self.isVisible():
            self.show()

    def _start_recording_delayed(self):
        """Start recording after PyQt6 event loop is fully initialized"""
        try:
            self.app.start_recording()

            # Recording started successfully
            self.status_bar.showMessage("🔴 Recording started successfully!")
            self.log_console.append("✅ Recording Session Started Successfully")
            self.log_console.append("Monitoring mouse and keyboard events...")

            # Start the log update timer
            if not self.log_timer.isActive():
                self.log_timer.start(100)

        except Exception as e:
            # Recording failed - show error and clean up
            error_msg = f"❌ Failed to start recording: {str(e)}"
            print(error_msg)
            self.status_bar.showMessage("❌ Recording failed - Check permissions")

            self.log_console.append(f"❌ Recording Failed: {str(e)}")
            self.log_console.append("")
            self.log_console.append("💡 Troubleshooting Tips:")
            self.log_console.append(
                "• Go to System Preferences → Security & Privacy → Privacy"
            )
            self.log_console.append(
                "• Click 'Accessibility' and add your Terminal or Python"
            )
            self.log_console.append(
                "• Restart the application after granting permissions"
            )

            # Reset button state
            self.toggle_log_btn.setText("Show Log Console")
            self.toggle_log_btn.setChecked(False)

            # If we hid the window earlier, restore it on failure
            if self.was_hidden_for_recording:
                self.was_hidden_for_recording = False
                self.show()

    def run(self):
        # Don't setup global hotkeys in GUI mode - they conflict with PyQt6
        # Capture the app currently in front, so we can reactivate it when we hide ourselves
        self.capture_prev_front_app()
        self.show()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = MacroGUI()
    gui.run()
    sys.exit(app.exec())
