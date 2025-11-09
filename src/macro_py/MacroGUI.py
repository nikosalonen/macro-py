"""PyQt6 GUI for recording and playing macros.

Compact window with toolbar, options, and a log section.
"""
import sys
import subprocess
import logging
import multiprocessing as mp
import threading
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
    QToolBar,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QKeySequence, QAction
from .MacroApp import MacroApp
from pynput import keyboard


def _f5_hotkey_subprocess(stop_signal_queue, stop_event):
    """
    Subprocess function to listen for F5 key press on macOS.

    This runs in a separate process to avoid CGEventTap conflicts with PyQt6.
    When F5 is pressed, sends a signal to the main process via the queue.

    Args:
        stop_signal_queue: multiprocessing.Queue to send stop signals
        stop_event: multiprocessing.Event to signal subprocess termination
    """
    import queue
    from pynput import keyboard as kb

    # Set up logging for subprocess
    logger = logging.getLogger(__name__)

    def on_key_press(key):
        try:
            if key == kb.Key.f5:
                # Send stop signal to main process with timeout
                try:
                    stop_signal_queue.put("STOP", timeout=0.1)
                except queue.Full:
                    logger.warning("F5 hotkey subprocess: Queue full, STOP signal dropped")
                except (OSError, ValueError) as e:
                    # Queue closed or invalid state
                    logger.error("F5 hotkey subprocess: Queue error when sending STOP: %s", e)
        except AttributeError:
            # Key doesn't have the expected attributes
            pass
        except Exception as e:
            logger.exception("F5 hotkey subprocess: Unexpected error in on_key_press: %s", e)

    listener = None
    try:
        listener = kb.Listener(on_press=on_key_press)
        listener.start()
        logger.debug("F5 hotkey subprocess: Listener started")

        # Wait for stop event
        while not stop_event.is_set():
            stop_event.wait(timeout=0.1)

        logger.debug("F5 hotkey subprocess: Stop event received, shutting down")
    except Exception as e:
        logger.exception("F5 hotkey subprocess: Error in main loop: %s", e)
    finally:
        # Always stop the listener on exit
        if listener is not None:
            try:
                listener.stop()
                # Wait for listener thread to finish
                if hasattr(listener, 'join'):
                    listener.join(timeout=1.0)
                logger.debug("F5 hotkey subprocess: Listener stopped")
            except Exception as e:
                logger.error("F5 hotkey subprocess: Error stopping listener: %s", e)


class MacroGUI(QMainWindow):
    """Main window for recording and playback controls with logging."""
    def __init__(self):
        super().__init__()
        self.app = MacroApp()
        self.setWindowTitle("Macro Recorder")
        self.setGeometry(100, 100, 480, 320)
        # Default to always-on-top
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # Toolbar
        self._build_toolbar()

        # Create splitter for controls and log
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Controls widget
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        self.setup_ui(controls_layout)
        splitter.addWidget(controls_widget)

        # Log console inside a visually separated section
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumHeight(200)
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
        self.log_section = QGroupBox("Log")
        self.log_section.setStyleSheet(
            """
            QGroupBox {
                border: 1px solid #c8c8c8;
                border-radius: 4px;
                margin-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #666;
                font-weight: 600;
            }
        """
        )
        log_section_layout = QVBoxLayout(self.log_section)
        log_section_layout.setContentsMargins(8, 8, 8, 8)
        log_section_layout.setSpacing(6)
        log_section_layout.addWidget(self.log_console)
        self.log_section.hide()  # Initially hidden
        splitter.addWidget(self.log_section)

        # Set splitter proportions
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        main_layout.addWidget(splitter)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        # Visual separator for footer
        self.status_bar.setStyleSheet("QStatusBar { border-top: 1px solid #c8c8c8; }")
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
        self._play_hotkey_listener = None
        self.prev_front_app_name = None

        # Subprocess components for F5 hotkey on macOS
        self._f5_subprocess = None
        self._f5_stop_event = None
        self._f5_signal_queue = None
        self._f5_consumer_thread = None
        self._f5_consumer_stop_event = None

        # Timer for updating playback progress in the status bar
        self.play_progress_timer = QTimer()
        self.play_progress_timer.timeout.connect(self.update_play_progress)

    def _build_toolbar(self):
        """Create the main toolbar and wire up actions and shortcuts."""
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(toolbar)
        # Visual separator for topbar
        toolbar.setStyleSheet("QToolBar { border-bottom: 1px solid #c8c8c8; }")

        # Actions
        self.action_start_rec = QAction("Start", self)
        self.action_start_rec.setShortcut(QKeySequence("F1"))
        self.action_start_rec.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.action_start_rec.triggered.connect(self.start_recording_gui)

        self.action_stop_rec = QAction("Stop Rec", self)
        self.action_stop_rec.setShortcut(QKeySequence("F2"))
        self.action_stop_rec.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.action_stop_rec.triggered.connect(self.stop_recording_gui)

        self.action_play_once = QAction("Play 1x", self)
        self.action_play_once.setShortcut(QKeySequence("F3"))
        self.action_play_once.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.action_play_once.triggered.connect(self.play_once_gui)

        self.action_play_infinite = QAction("Play ∞", self)
        self.action_play_infinite.triggered.connect(self.play_infinite_gui)

        self.action_stop_play = QAction("Stop", self)
        self.action_stop_play.setShortcut(QKeySequence("F5"))
        self.action_stop_play.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.action_stop_play.triggered.connect(self.stop_playback_gui)

        toolbar.addAction(self.action_start_rec)
        toolbar.addAction(self.action_stop_rec)
        toolbar.addSeparator()
        toolbar.addAction(self.action_play_once)
        toolbar.addAction(self.action_play_infinite)
        toolbar.addAction(self.action_stop_play)
        toolbar.addSeparator()

        self.action_save = QAction("Save", self)
        self.action_save.triggered.connect(self.save_macro)
        self.action_load = QAction("Load", self)
        self.action_load.triggered.connect(self.load_macro)
        toolbar.addAction(self.action_save)
        toolbar.addAction(self.action_load)
        toolbar.addSeparator()

        self.toggle_log_action = QAction("Show Log", self)
        self.toggle_log_action.setCheckable(True)
        self.toggle_log_action.toggled.connect(self._toggle_log_from_action)
        toolbar.addAction(self.toggle_log_action)

        self.always_on_top_action = QAction("Always on Top", self)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.setChecked(True)
        self.always_on_top_action.toggled.connect(self.on_always_on_top_toggled)
        toolbar.addAction(self.always_on_top_action)

        toolbar.addSeparator()
        self.action_toggle_options = QAction("Options", self)
        self.action_toggle_options.setCheckable(True)
        self.action_toggle_options.setChecked(False)
        self.action_toggle_options.toggled.connect(self._toggle_options_panel)
        toolbar.addAction(self.action_toggle_options)

        toolbar.addSeparator()
        self.action_help = QAction("Help", self)
        self.action_help.triggered.connect(self._show_help)
        toolbar.addAction(self.action_help)

    def setup_ui(self, layout):
        """Build compact central controls, options panel, and shortcuts strip."""
        # Compact playback row
        playback_row = QHBoxLayout()
        playback_row.addWidget(QLabel("Loops:"))
        self.loop_entry = QLineEdit("5")
        self.loop_entry.setFixedWidth(60)
        playback_row.addWidget(self.loop_entry)

        play_x_btn = QPushButton("Play")
        play_x_btn.clicked.connect(self.play_x)
        playback_row.addWidget(play_x_btn)

        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(self.stop_playback_gui)
        playback_row.addWidget(stop_btn)

        playback_row.addStretch()
        layout.addLayout(playback_row)

        # Options panel (advanced)
        self.options_group = QGroupBox("Options")
        options_layout = QHBoxLayout(self.options_group)
        options_layout.setContentsMargins(8, 8, 8, 8)
        options_layout.setSpacing(12)

        # Activate underlying app toggles
        self.activate_on_record_checkbox = QCheckBox("Activate app on Record")
        self.activate_on_record_checkbox.setChecked(True)
        options_layout.addWidget(self.activate_on_record_checkbox)

        self.activate_on_play_checkbox = QCheckBox("Activate app on Play")
        self.activate_on_play_checkbox.setChecked(True)
        options_layout.addWidget(self.activate_on_play_checkbox)

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.clicked.connect(self.clear_log)
        options_layout.addWidget(clear_log_btn)

        options_layout.addStretch()
        self.options_group.setVisible(False)
        layout.addWidget(self.options_group)

        # Shortcuts (compact, always visible)
        self.shortcuts_group = QGroupBox("Shortcuts")
        self.shortcuts_group.setStyleSheet(
            """
            QGroupBox {
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                margin-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
                color: #666;
                font-weight: 600;
            }
        """
        )
        shortcuts_layout = QVBoxLayout(self.shortcuts_group)
        shortcuts_layout.setContentsMargins(8, 8, 8, 8)
        shortcuts_layout.setSpacing(4)
        shortcuts_label = QLabel("F1 - Start • F2 - Stop Rec • F3 - Play Once • F5 - Stop")
        shortcuts_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        shortcuts_label.setStyleSheet("color: #666;")
        shortcuts_layout.addWidget(shortcuts_label)
        layout.addWidget(self.shortcuts_group)

    def start_recording_gui(self):
        """Start recording and update UI/log state accordingly."""
        if not self.app.recorder.recording and not self.app.player.playing:
            try:
                self.status_bar.showMessage("🔄 Starting recording...")

                # Instead of hiding, drop always-on-top and send window to background
                if self.isVisible():
                    # Remember if we need to restore always-on-top after recording
                    if self.always_on_top_action.isChecked():
                        self._restore_on_top_after_record = True
                        # Uncheck triggers flag update
                        self.always_on_top_action.setChecked(False)
                    # Send behind other windows but keep taskbar entry and shortcuts active
                    self.lower()

                # On macOS, bring back the previously active app so the user can start right away
                if self.activate_on_record_checkbox.isChecked():
                    self.activate_previous_app()

                # Show log console first
                self.log_section.show()
                self.log_console.clear()
                self.log_console.append("📝 Initializing Recording Session...")
                self.log_console.append("=" * 50)

                # Update button text immediately
                self.toggle_log_action.blockSignals(True)
                self.toggle_log_action.setChecked(True)
                self.toggle_log_action.setText("Hide Log")
                self.toggle_log_action.blockSignals(False)

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
                self.toggle_log_action.blockSignals(True)
                self.toggle_log_action.setText("Show Log")
                self.toggle_log_action.setChecked(False)
                self.toggle_log_action.blockSignals(False)

        else:
            self.status_bar.showMessage(
                "Cannot start recording - already recording or playing"
            )

    def stop_recording_gui(self):
        """Stop recording and restore window/topmost state if needed."""
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
                if not self.always_on_top_action.isChecked():
                    self.always_on_top_action.setChecked(True)
                # Bring window to front
                self.show()
                self.raise_()
                self.activateWindow()

        else:
            self.status_bar.showMessage("Not currently recording")

    def play_once_gui(self):
        """Prepare and play current macro once."""
        if self.app.macro_data and not self.app.player.playing:
            # Prepare UI and hotkeys
            self._prepare_for_playback()

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
        """Prepare and play current macro in infinite loop until stopped."""
        if self.app.macro_data and not self.app.player.playing:
            # Prepare UI and hotkeys
            self._prepare_for_playback()

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
        """Stop playback, clean up hotkeys, and restore window state."""
        if self.app.player.playing:
            self.app.stop_playback()
            self.status_bar.showMessage("⏹️ Playback stopped")
            if self.play_progress_timer.isActive():
                self.play_progress_timer.stop()
            self._cleanup_after_playback()
        else:
            self.status_bar.showMessage("Not currently playing")

    def play_x(self):
        """Play current macro a user-specified number of loops."""
        try:
            loops = int(self.loop_entry.text())
            if self.app.macro_data and not self.app.player.playing:
                # Prepare UI and hotkeys
                self._prepare_for_playback()

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
        """Update loop progress in the status bar; restore window when finished."""
        player = self.app.player
        if not player.playing:
            if self.play_progress_timer.isActive():
                self.play_progress_timer.stop()
            self._cleanup_after_playback()
            return
        # Ensure at least 1 is shown when first loop starts
        current = player.current_loop or 1
        total = player.total_loops
        if total == -1:
            self.status_bar.showMessage(f"🔁 Running {current}/∞ loops")
        else:
            self.status_bar.showMessage(f"🔄 Running {current}/{total} loops")

    def save_macro(self):
        """Save the current macro to a JSON file chosen by the user."""
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
        """Load a macro from a JSON file chosen by the user."""
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
        if self.log_section.isVisible():
            self.log_section.hide()
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Show Log")
            self.toggle_log_action.setChecked(False)
            self.toggle_log_action.blockSignals(False)
        else:
            self.log_section.show()
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Hide Log")
            self.toggle_log_action.setChecked(True)
            self.toggle_log_action.blockSignals(False)

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

    def on_always_on_top_toggled(self, checked):
        """Apply the always-on-top flag and re-show the window to take effect."""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        # Re-show to apply flag change on macOS/Qt
        if self.isVisible():
            self.show()

    def _toggle_log_from_action(self, checked):
        # Reflect action state to the console visibility
        if checked:
            if not self.log_section.isVisible():
                self.log_section.show()
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Hide Log")
            self.toggle_log_action.setChecked(True)
            self.toggle_log_action.blockSignals(False)
        else:
            if self.log_section.isVisible():
                self.log_section.hide()
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Show Log")
            self.toggle_log_action.setChecked(False)
            self.toggle_log_action.blockSignals(False)

    def _toggle_options_panel(self, checked):
        """Show or hide the advanced options pane."""
        if hasattr(self, "options_group"):
            self.options_group.setVisible(bool(checked))

    def _show_help(self):
        """Display a small dialog with keyboard shortcuts."""
        QMessageBox.information(
            self,
            "Shortcuts",
            "F1 - Start Recording (backgrounds window)\n"
            "F2 - Stop Recording (restores window)\n"
            "F3 - Play Once\n"
            "F5 - Stop Playback",
        )

    def _f5_signal_consumer(self):
        """Thread that monitors the F5 signal queue from subprocess."""
        while not self._f5_consumer_stop_event.is_set():
            try:
                signal = self._f5_signal_queue.get(timeout=0.1)
                if signal == "STOP":
                    # Schedule stop on the Qt main thread
                    QTimer.singleShot(0, self.stop_playback_gui)
            except Exception:
                # Queue.get timeout or queue closed
                pass

    def _start_playback_hotkeys(self):
        """Start a global listener that maps F5 to stop playback."""
        # Check if already running
        if self._play_hotkey_listener is not None or self._f5_subprocess is not None:
            return

        # macOS: use subprocess to avoid CGEventTap conflict with PyQt6
        if sys.platform == "darwin":
            try:
                mp_ctx = mp.get_context("spawn")
                self._f5_signal_queue = mp_ctx.Queue(maxsize=10)
                self._f5_stop_event = mp_ctx.Event()

                # Start subprocess
                self._f5_subprocess = mp_ctx.Process(
                    target=_f5_hotkey_subprocess,
                    args=(self._f5_signal_queue, self._f5_stop_event),
                )
                self._f5_subprocess.start()

                # Start consumer thread to monitor queue
                self._f5_consumer_stop_event = threading.Event()
                self._f5_consumer_thread = threading.Thread(
                    target=self._f5_signal_consumer,
                    name="F5HotkeyConsumer",
                    daemon=True,
                )
                self._f5_consumer_thread.start()

                logging.debug("Started F5 hotkey subprocess on macOS")
            except Exception as e:
                logging.warning("Failed to start F5 hotkey subprocess: %s", e)
                self._cleanup_f5_subprocess()
        else:
            # Windows/Linux: use in-process listener (no CGEventTap conflict)
            def on_key_press(key):
                try:
                    if key == keyboard.Key.f5:
                        # Schedule stop on the Qt main thread
                        QTimer.singleShot(0, self.stop_playback_gui)
                except Exception:
                    logging.exception("Error in global hotkey on_key_press handler")

            try:
                self._play_hotkey_listener = keyboard.Listener(on_press=on_key_press)
                self._play_hotkey_listener.start()
            except Exception as e:
                # If listener fails, continue without global hotkey but log for diagnostics
                logging.warning("Failed to start global hotkey listener: %s", e)
                self._play_hotkey_listener = None

    def _cleanup_f5_subprocess(self):
        """Clean up the F5 hotkey subprocess and associated resources."""
        # Stop consumer thread
        if self._f5_consumer_stop_event is not None:
            self._f5_consumer_stop_event.set()
        if self._f5_consumer_thread is not None and self._f5_consumer_thread.is_alive():
            self._f5_consumer_thread.join(timeout=1.0)

        # Stop subprocess
        if self._f5_stop_event is not None:
            self._f5_stop_event.set()
        if self._f5_subprocess is not None and self._f5_subprocess.is_alive():
            self._f5_subprocess.join(timeout=1.0)
            if self._f5_subprocess.is_alive():
                self._f5_subprocess.terminate()

        # Clear references
        self._f5_subprocess = None
        self._f5_stop_event = None
        self._f5_signal_queue = None
        self._f5_consumer_thread = None
        self._f5_consumer_stop_event = None

    def _stop_playback_hotkeys(self):
        """Stop and clear the global F5 playback stop listener if present."""
        # macOS subprocess
        if self._f5_subprocess is not None:
            try:
                self._cleanup_f5_subprocess()
            except Exception:
                logging.exception("Error stopping F5 hotkey subprocess")

        # Windows/Linux in-process listener
        if self._play_hotkey_listener is not None:
            try:
                self._play_hotkey_listener.stop()
            except Exception:
                logging.exception("Error stopping global hotkey listener")
            finally:
                self._play_hotkey_listener = None

    def _cleanup_after_playback(self):
        # Beep to signal completion and stop any global hotkey listener
        try:
            QApplication.beep()
        except Exception:
            pass
        self._stop_playback_hotkeys()
        # Restore window if it was backgrounded for playback
        if self._restore_on_top_after_play:
            self._restore_on_top_after_play = False
            if not self.always_on_top_action.isChecked():
                self.always_on_top_action.setChecked(True)
            self.show()
            self.raise_()
            self.activateWindow()

    def _prepare_for_playback(self):
        """Lower window, manage top-most state, and enable F5 stop hotkey."""
        # Send window to background and manage always-on-top, then enable F5 stop
        if self.isVisible():
            if self.always_on_top_action.isChecked():
                self._restore_on_top_after_play = True
                self.always_on_top_action.setChecked(False)
            self.lower()
        self._start_playback_hotkeys()

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
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Show Log")
            self.toggle_log_action.setChecked(False)
            self.toggle_log_action.blockSignals(False)

            # If we hid the window earlier, restore it on failure
            if self.was_hidden_for_recording:
                self.was_hidden_for_recording = False
                self.show()

    def run(self):
        """Capture previous app (macOS) and show the GUI window."""
        # Don't setup global hotkeys in GUI mode - they conflict with PyQt6
        # Capture the app currently in front, so we can reactivate it when we hide ourselves
        self.capture_prev_front_app()
        self.show()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = MacroGUI()
    gui.run()
    sys.exit(app.exec())
