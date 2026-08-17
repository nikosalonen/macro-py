"""PyQt6 GUI for recording and playing macros.

Compact window with toolbar, options, and a log section.
"""

from __future__ import annotations

import sys
import os
import signal
import subprocess
import logging
import multiprocessing as mp
import threading
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

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
    QListView,
    QSplitter,
    QCheckBox,
    QToolBar,
    QMessageBox,
    QSpinBox,
    QDoubleSpinBox,
)
from PyQt6.QtCore import (
    Qt,
    QObject,
    QTimer,
    QSize,
    QAbstractListModel,
    QModelIndex,
    QSettings,
)
from PyQt6.QtGui import (
    QKeySequence,
    QAction,
    QCloseEvent,
    QColor,
    QPalette,
    QIntValidator,
)
from PyQt6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem
from .MacroApp import MacroApp
from pynput import keyboard

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event as MpEvent

Event = dict[str, Any]

# Virtual key codes for F5 used for OS-level suppression of the stop hotkey
F5_KEYCODE_MACOS = 96
F5_VKCODE_WINDOWS = 0x74


def _f5_hotkey_subprocess(
    stop_signal_queue: mp.Queue[str], stop_event: MpEvent
) -> None:
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

    def on_key_press(key: kb.Key | kb.KeyCode | None) -> None:
        try:
            if key == kb.Key.f5:
                # Send stop signal to main process with timeout
                try:
                    stop_signal_queue.put("STOP", timeout=0.1)
                except queue.Full:
                    logger.warning(
                        "F5 hotkey subprocess: Queue full, STOP signal dropped"
                    )
                except (OSError, ValueError):
                    # Queue closed or invalid state
                    logger.exception(
                        "F5 hotkey subprocess: Queue error when sending STOP"
                    )
        except AttributeError:
            # Key doesn't have the expected attributes
            pass
        except Exception:
            logger.exception("F5 hotkey subprocess: Unexpected error in on_key_press")

    def darwin_intercept(event_type: int, event: Any) -> Any:
        """Swallow F5 at the event-tap level so the focused app never sees it.

        Without this the keystroke still reaches the frontmost app (e.g. a
        browser refreshing the page) even though playback stops. Returning
        None suppresses the event system-wide; our on_press callback has
        already run by then.
        """
        try:
            import Quartz

            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            if keycode == F5_KEYCODE_MACOS:
                return None
        except Exception:
            logger.exception("F5 hotkey subprocess: Error in darwin_intercept")
        return event

    listener: kb.Listener | None = None
    try:
        listener = kb.Listener(on_press=on_key_press, darwin_intercept=darwin_intercept)
        listener.start()
        logger.debug("F5 hotkey subprocess: Listener started")

        # Wait for stop event
        while not stop_event.is_set():
            stop_event.wait(timeout=0.1)

        logger.debug("F5 hotkey subprocess: Stop event received, shutting down")
    except Exception:
        logger.exception("F5 hotkey subprocess: Error in main loop")
    finally:
        # Always stop the listener on exit
        if listener is not None:
            try:
                listener.stop()
                # Wait for listener thread to finish
                if hasattr(listener, "join"):
                    listener.join(timeout=1.0)
                logger.debug("F5 hotkey subprocess: Listener stopped")
            except Exception:
                logger.exception("F5 hotkey subprocess: Error stopping listener")


class EventLogModel(QAbstractListModel):
    """Qt Model for displaying macro events efficiently.

    This model implements the Qt Model-View architecture for event display,
    providing better performance for large event lists compared to appending
    to a QTextEdit widget.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._events: list[Event] = []
        self._formatted_cache: list[str] = []
        self.last_mouse_pos: tuple[float, float] | None = None
        self.mouse_move_count = 0

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Return the number of events in the model."""
        return len(self._events)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Return formatted event data for the given index."""
        if not index.isValid() or index.row() >= len(self._events):
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            # Return cached formatted string
            return self._formatted_cache[index.row()]

        return None

    def add_event(self, event: Event) -> bool:
        """Add a new event to the model.

        Args:
            event: Event dictionary from the recorder

        Returns:
            bool: True if event was added, False if filtered out
        """
        formatted = self._format_event(event)
        if formatted is None:
            # Event was filtered (e.g., insignificant mouse move)
            return False

        # Notify views that we're adding a row
        row = len(self._events)
        self.beginInsertRows(QModelIndex(), row, row)
        self._events.append(event)
        self._formatted_cache.append(formatted)
        self.endInsertRows()
        return True

    def append_system_message(self, message: str) -> None:
        """Append a pre-formatted system message to the model."""
        row = len(self._events)
        self.beginInsertRows(QModelIndex(), row, row)
        self._events.append(
            {
                "type": "__system_message__",
                "message": message,
                "time": 0.0,
            }
        )
        self._formatted_cache.append(message)
        self.endInsertRows()

    def clear_events(self) -> None:
        """Clear all events from the model."""
        if not self._events:
            return

        self.beginResetModel()
        self._events.clear()
        self._formatted_cache.clear()
        self.last_mouse_pos = None
        self.mouse_move_count = 0
        self.endResetModel()

    def _format_event(self, event: Event) -> str | None:
        """Format a single event for display in the log.

        Args:
            event: Event dictionary from the recorder

        Returns:
            str: Formatted event string, or None to filter out this event
        """
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
                    f"🖱️  [{timestamp}] Mouse Move "
                    f"#{self.mouse_move_count} → ({x}, {y})"
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


class EventLogDelegate(QStyledItemDelegate):
    """Custom delegate for rendering event log items with enhanced styling."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Define colors for different event types
        self.mouse_color = QColor("#4A9EFF")  # Blue for mouse events
        self.keyboard_color = QColor("#50C878")  # Green for keyboard events
        self.system_color = QColor("#FFB84D")  # Orange for system messages
        self.unknown_color = QColor("#FF6B6B")  # Red for unknown events

    def initStyleOption(
        self, option: QStyleOptionViewItem | None, index: QModelIndex
    ) -> None:
        """Initialize style options with custom colors based on event type."""
        super().initStyleOption(option, index)
        if option is None:
            return

        # Get the display text to determine event type
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            # Color code based on emoji/event type
            if text.startswith("🖱️"):
                option.palette.setColor(
                    QPalette.ColorGroup.All, QPalette.ColorRole.Text, self.mouse_color
                )
            elif text.startswith("⌨️"):
                option.palette.setColor(
                    QPalette.ColorGroup.All,
                    QPalette.ColorRole.Text,
                    self.keyboard_color,
                )
            elif (
                text.startswith("📝")
                or text.startswith("✅")
                or text.startswith("⏳")
                or text.startswith("💡")
            ):
                option.palette.setColor(
                    QPalette.ColorGroup.All, QPalette.ColorRole.Text, self.system_color
                )
            elif text.startswith("❌") or text.startswith("❓"):
                option.palette.setColor(
                    QPalette.ColorGroup.All, QPalette.ColorRole.Text, self.unknown_color
                )


class MacroGUI(QMainWindow):
    """Main window for recording and playback controls with logging."""

    def __init__(self) -> None:
        super().__init__()
        self.app = MacroApp()
        self.setWindowTitle("Macro Recorder")
        self.setGeometry(100, 100, 620, 320)
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

        # Log console using Model-View architecture
        self.log_model = EventLogModel(self)
        self.log_console = QListView()
        self.log_console.setModel(self.log_model)

        # Set custom delegate for colored event rendering
        self.log_delegate = EventLogDelegate(self.log_console)
        self.log_console.setItemDelegate(self.log_delegate)

        self.log_console.setMaximumHeight(200)
        self.log_console.setStyleSheet("""
            QListView {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: 'Monaco', 'Consolas', monospace;
                font-size: 11px;
                border: 1px solid #444;
            }
            QListView::item:alternate {
                background-color: #252525;
            }
        """)
        # Enable alternating row colors for better readability
        self.log_console.setAlternatingRowColors(True)
        # Disable editing
        self.log_console.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.log_section = QGroupBox("Log")
        self.log_section.setStyleSheet("""
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
        """)
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
        # self.last_mouse_pos = None
        # self.mouse_move_count = 0
        self.was_hidden_for_recording = False
        self._restore_on_top_after_record = False
        self._restore_on_top_after_play = False
        self._play_hotkey_listener: keyboard.Listener | None = None
        self.prev_front_app_name: str | None = None

        # Subprocess components for F5 hotkey on macOS
        self._f5_subprocess: mp.process.BaseProcess | None = None
        self._f5_stop_event: MpEvent | None = None
        self._f5_signal_queue: mp.Queue[str] | None = None
        self._f5_consumer_thread: threading.Thread | None = None
        self._f5_consumer_stop_event: threading.Event | None = None

        # Timer for updating playback progress in the status bar
        self.play_progress_timer = QTimer()
        self.play_progress_timer.timeout.connect(self.update_play_progress)

        # Macro file state for title bar and unsaved-changes warnings
        self.current_macro_path: str | None = None
        self.macro_dirty = False
        self._countdown_active = False

        # Persisted settings (geometry, options, last used directory)
        self._settings = QSettings("macro-py", "MacroRecorder")
        self._restore_settings()
        self._update_window_title()

    def _restore_settings(self) -> None:
        """Restore persisted UI options from the previous session."""
        s = self._settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.loop_entry.setText(s.value("loops", "5", str))
        self.max_idle_entry.setText(s.value("maxIdleMs", "", str))
        try:
            self.speed_spin.setValue(float(s.value("speed", 1.0)))
        except (TypeError, ValueError):
            self.speed_spin.setValue(1.0)
        try:
            self.countdown_spin.setValue(int(s.value("countdown", 3)))
        except (TypeError, ValueError):
            self.countdown_spin.setValue(3)
        self.compress_moves_checkbox.setChecked(s.value("compressMoves", True, bool))
        self.activate_on_record_checkbox.setChecked(
            s.value("activateOnRecord", True, bool)
        )
        self.activate_on_play_checkbox.setChecked(s.value("activateOnPlay", True, bool))
        self.always_on_top_action.setChecked(s.value("alwaysOnTop", True, bool))
        if s.value("showLog", False, bool):
            self.toggle_log_action.setChecked(True)

    def _save_settings(self) -> None:
        """Persist UI options for the next session."""
        s = self._settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("loops", self.loop_entry.text())
        s.setValue("maxIdleMs", self.max_idle_entry.text())
        s.setValue("speed", self.speed_spin.value())
        s.setValue("countdown", self.countdown_spin.value())
        s.setValue("compressMoves", self.compress_moves_checkbox.isChecked())
        s.setValue("activateOnRecord", self.activate_on_record_checkbox.isChecked())
        s.setValue("activateOnPlay", self.activate_on_play_checkbox.isChecked())
        s.setValue("alwaysOnTop", self.always_on_top_action.isChecked())
        s.setValue("showLog", self.toggle_log_action.isChecked())

    def _update_window_title(self, state: str | None = None) -> None:
        """Reflect app state, loaded file, and event count in the title bar."""
        title = "Macro Recorder"
        if self.current_macro_path:
            title += f" — {os.path.basename(self.current_macro_path)}"
        if self.app.macro_data:
            count = len(self.app.macro_data)
            title += f" ({count} event{'s' if count != 1 else ''})"
        if self.macro_dirty:
            title += " *"
        if state:
            title = f"{state} — {title}"
        self.setWindowTitle(title)

    def _begin_countdown(self, label: str, on_done: Callable[[], None]) -> None:
        """Run the configured countdown in the status bar, then call on_done.

        A countdown of 0 invokes on_done immediately. Setting
        self._countdown_active to False cancels a pending countdown.
        """
        seconds = self.countdown_spin.value()
        if seconds <= 0:
            on_done()
            return
        self._countdown_active = True
        remaining = {"n": seconds}

        def tick() -> None:
            if not self._countdown_active:
                return
            if remaining["n"] <= 0:
                self._countdown_active = False
                on_done()
                return
            self.status_bar.showMessage(f"{label} in {remaining['n']}…")
            remaining["n"] -= 1
            QTimer.singleShot(1000, tick)

        tick()

    def _build_toolbar(self) -> None:
        """Create the main toolbar and wire up actions and shortcuts."""
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(toolbar)
        # Visual separator for topbar + visible extension button
        toolbar.setStyleSheet("""
            QToolBar { border-bottom: 1px solid #c8c8c8; }
            QToolButton#qt_toolbar_ext_button {
                background: #666;
                border-radius: 2px;
                min-width: 16px;
                padding: 2px;
            }
            QToolButton#qt_toolbar_ext_button:hover {
                background: #888;
            }
        """)

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
        self.action_play_infinite.setShortcut(QKeySequence("F4"))
        self.action_play_infinite.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
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

    def setup_ui(self, layout: QVBoxLayout) -> None:
        """Build compact central controls, options panel, and shortcuts strip."""
        # Compact playback row
        playback_row = QHBoxLayout()
        playback_row.addWidget(QLabel("Loops:"))
        self.loop_entry = QLineEdit("5")
        self.loop_entry.setFixedWidth(60)
        self.loop_entry.setValidator(QIntValidator(1, 9999, self))
        self.loop_entry.setToolTip("Number of times to repeat the macro (min 1)")
        playback_row.addWidget(self.loop_entry)

        playback_row.addWidget(QLabel("Speed:"))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.25, 4.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix("×")
        self.speed_spin.setToolTip("Playback speed multiplier (1× = recorded speed)")
        playback_row.addWidget(self.speed_spin)

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

        # Max idle time setting (caps delays during playback)
        options_layout.addWidget(QLabel("Max idle (ms):"))
        self.max_idle_entry = QLineEdit("")
        self.max_idle_entry.setFixedWidth(60)
        self.max_idle_entry.setPlaceholderText("none")
        self.max_idle_entry.setToolTip(
            "Max delay between events during playback (empty = no limit, min 1)"
        )
        self.max_idle_entry.setValidator(QIntValidator(1, 999999, self))
        options_layout.addWidget(self.max_idle_entry)

        # Countdown before recording/playback starts
        options_layout.addWidget(QLabel("Countdown (s):"))
        self.countdown_spin = QSpinBox()
        self.countdown_spin.setRange(0, 10)
        self.countdown_spin.setValue(3)
        self.countdown_spin.setToolTip(
            "Seconds to wait before recording/playback starts (0 = immediately)"
        )
        options_layout.addWidget(self.countdown_spin)

        # Drop insignificant mouse moves after recording
        self.compress_moves_checkbox = QCheckBox("Compress mouse moves")
        self.compress_moves_checkbox.setChecked(True)
        self.compress_moves_checkbox.setToolTip(
            "Drop tiny mouse movements after recording to shrink macro files"
        )
        options_layout.addWidget(self.compress_moves_checkbox)

        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.clicked.connect(self.clear_log)
        options_layout.addWidget(clear_log_btn)

        options_layout.addStretch()
        self.options_group.setVisible(False)
        layout.addWidget(self.options_group)

        # Shortcuts (compact, always visible)
        self.shortcuts_group = QGroupBox("Shortcuts")
        self.shortcuts_group.setStyleSheet("""
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
        """)
        shortcuts_layout = QVBoxLayout(self.shortcuts_group)
        shortcuts_layout.setContentsMargins(8, 8, 8, 8)
        shortcuts_layout.setSpacing(4)
        shortcuts_label = QLabel(
            "F1 - Start • F2 - Stop Rec • F3 - Play Once • F4 - Play ∞ • F5 - Stop"
        )
        shortcuts_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        shortcuts_label.setStyleSheet("color: #666;")
        shortcuts_layout.addWidget(shortcuts_label)
        layout.addWidget(self.shortcuts_group)

    def start_recording_gui(self) -> None:
        """Start recording and update UI/log state accordingly."""
        if (
            not self.app.recorder.recording
            and not self.app.player.playing
            and not self._countdown_active
        ):
            # Warn before an unsaved macro is overwritten by a new recording
            if self.macro_dirty and self.app.macro_data:
                reply = QMessageBox.question(
                    self,
                    "Discard unsaved macro?",
                    "The current macro has not been saved. "
                    "Start a new recording and discard it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            try:
                self.status_bar.showMessage("🔄 Starting recording...")

                # Instead of hiding, drop always-on-top and send window to background
                if self.isVisible():
                    # Remember if we need to restore always-on-top after recording
                    if self.always_on_top_action.isChecked():
                        self._restore_on_top_after_record = True
                        # Uncheck triggers flag update
                        self.always_on_top_action.setChecked(False)
                    # Send behind other windows but keep taskbar entry
                    # and shortcuts active
                    self.lower()

                # On macOS, bring back the previously active app so the
                # user can start right away
                if self.activate_on_record_checkbox.isChecked():
                    self.activate_previous_app()

                # Show log console first
                self.log_section.show()
                self._log_clear()
                self._log_append("📝 Initializing Recording Session...")
                self._log_append("=" * 50)

                # Update button text immediately
                self.toggle_log_action.blockSignals(True)
                self.toggle_log_action.setChecked(True)
                self.toggle_log_action.setText("Hide Log")
                self.toggle_log_action.blockSignals(False)

                # Initialize counters first
                self.last_event_count = 0
                # self.last_mouse_pos = None
                # self.mouse_move_count = 0

                # Defer recording startup to avoid PyQt6 event loop conflicts,
                # after an optional countdown so the user can get in position
                self._begin_countdown(
                    "🔴 Recording starting",
                    lambda: QTimer.singleShot(100, self._start_recording_delayed),
                )

                self.status_bar.showMessage("🔄 Initializing listeners...")
                self._log_append("⏳ Starting listeners in background...")

            except Exception as e:
                # Recording failed - show error and clean up
                error_msg = f"❌ Failed to start recording: {str(e)}"
                print(error_msg)
                self.status_bar.showMessage("❌ Recording failed - Check permissions")

                # If we hid the window, restore it on failure
                if self.was_hidden_for_recording:
                    self.was_hidden_for_recording = False
                    self.show()

                self._log_append(f"❌ Recording Failed: {str(e)}")
                self._log_append("")
                self._log_append("💡 Troubleshooting Tips:")
                self._log_append(
                    "• Go to System Settings → Privacy & Security → Accessibility"
                )
                self._log_append("• Add your Terminal or Python to the list")
                self._log_append("• Restart the application after granting permissions")

                # Reset button state
                self.toggle_log_action.blockSignals(True)
                self.toggle_log_action.setText("Show Log")
                self.toggle_log_action.setChecked(False)
                self.toggle_log_action.blockSignals(False)

        else:
            self.status_bar.showMessage(
                "Cannot start recording - already recording or playing"
            )

    def stop_recording_gui(self) -> None:
        """Stop recording and restore window/topmost state if needed."""
        # Cancel a pending recording countdown
        if self._countdown_active and not self.app.recorder.recording:
            self._countdown_active = False
            self.status_bar.showMessage("Recording cancelled")
            if self._restore_on_top_after_record:
                self._restore_on_top_after_record = False
                if not self.always_on_top_action.isChecked():
                    self.always_on_top_action.setChecked(True)
                self.show()
                self.raise_()
                self.activateWindow()
            return
        if self.app.recorder.recording:
            self.app.compress_moves = self.compress_moves_checkbox.isChecked()
            self.app.stop_recording()
            self.macro_dirty = bool(self.app.macro_data)
            self._update_window_title()
            self.status_bar.showMessage(
                f"⏹️ Recording stopped - {len(self.app.macro_data)} events recorded"
            )

            # Stop log timer and add summary
            if self.log_timer.isActive():
                self.log_timer.stop()
            self._log_append("=" * 50)
            self._log_append(
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

    def _start_playback_flow(
        self, start_fn: Callable[[], None], running_msg: str
    ) -> None:
        """Shared playback startup: prepare UI, count down, then play."""
        if not self.app.macro_data or self.app.player.playing or self._countdown_active:
            self.status_bar.showMessage("No macro to play or already playing")
            return

        # Prepare UI and hotkeys, and focus the target app during the countdown
        self._prepare_for_playback()
        if self.activate_on_play_checkbox.isChecked():
            self.activate_previous_app()

        def go() -> None:
            start_fn()
            self._update_window_title("▶ Playing")
            self.status_bar.showMessage(running_msg)
            if not self.play_progress_timer.isActive():
                self.play_progress_timer.start(200)

        self._begin_countdown("▶️ Playback starting", go)

    def play_once_gui(self) -> None:
        """Prepare and play current macro once."""
        self._start_playback_flow(self.app.play_once, "▶️ Running 1/1 loops")

    def play_infinite_gui(self) -> None:
        """Prepare and play current macro in infinite loop until stopped."""
        self._start_playback_flow(self.app.play_infinite, "🔁 Running 1/∞ loops")

    def stop_playback_gui(self) -> None:
        """Stop playback, clean up hotkeys, and restore window state."""
        # Cancel a pending playback countdown
        if self._countdown_active and not self.app.player.playing:
            self._countdown_active = False
            self.status_bar.showMessage("⏹️ Playback cancelled")
            self._cleanup_after_playback()
            return
        if self.app.player.playing:
            self.app.stop_playback()
            self.status_bar.showMessage("⏹️ Playback stopped")
            if self.play_progress_timer.isActive():
                self.play_progress_timer.stop()
            self._cleanup_after_playback()
        else:
            self.status_bar.showMessage("Not currently playing")

    def play_x(self) -> None:
        """Play current macro a user-specified number of loops."""
        try:
            loops = int(self.loop_entry.text())
        except ValueError:
            self.status_bar.showMessage("Invalid loop count")
            return
        if loops < 1:
            self.status_bar.showMessage("Loop count must be at least 1")
            return
        self._start_playback_flow(
            lambda: self.app.play_x_times(loops), f"🔄 Running 1/{loops} loops"
        )

    def update_play_progress(self) -> None:
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

    def save_macro(self) -> None:
        """Save the current macro to a JSON file chosen by the user."""
        default_name = datetime.now().strftime("macro_%Y-%m-%d_%H%M.json")
        last_dir = self._settings.value("lastDir", "", str)
        default_path = (
            os.path.join(last_dir, default_name) if last_dir else default_name
        )
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Macro", default_path, "JSON files (*.json)"
        )
        if filename:
            if not filename.endswith(".json"):
                filename += ".json"
            with open(filename, "w") as f:
                json.dump(list(self.app.macro_data or []), f, indent=2)
            self._settings.setValue("lastDir", os.path.dirname(filename))
            self.current_macro_path = filename
            self.macro_dirty = False
            self._update_window_title()
            self.status_bar.showMessage(f"Saved to {filename}")

    def load_macro(self) -> None:
        """Load a macro from a JSON file chosen by the user."""
        last_dir = self._settings.value("lastDir", "", str)
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Macro", last_dir, "JSON files (*.json)"
        )
        if filename:
            try:
                self.app.recorder.load_macro(filename)
            except (OSError, json.JSONDecodeError) as e:
                self.status_bar.showMessage(f"Failed to load macro: {e}")
                return
            with self.app.recorder._events_lock:
                self.app.macro_data = self.app.recorder.events.copy()
            self._settings.setValue("lastDir", os.path.dirname(filename))
            self.current_macro_path = filename
            self.macro_dirty = False
            self._update_window_title()
            self.status_bar.showMessage(f"Loaded {len(self.app.macro_data)} events")

    def update_log(self) -> None:
        """Update the log console with new events in real-time"""
        if not self.app.recorder.recording:
            return

        new_events, current_count = self.app.recorder.get_events_since(
            self.last_event_count
        )
        if new_events:
            # Add new events to log
            any_added = False
            for event in new_events:
                # Handle control stop request coming from subprocess (F2)
                if event.get("type") == "__stop_request__":
                    # Stop and restore window
                    self.stop_recording_gui()
                    # Skip logging this control event
                    continue
                # Add event to model (handles filtering internally)
                added = self.log_model.add_event(event)
                if added:
                    any_added = True
            if any_added:
                # Auto-scroll to bottom once after processing batch
                self.log_console.scrollToBottom()

            self.last_event_count = current_count

    def toggle_log_console(self) -> None:
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

    def clear_log(self) -> None:
        """Clear the log console"""
        self.log_model.clear_events()
        if not self.app.recorder.recording:
            self._log_append("📝 Log Cleared - Ready for recording")

    def _log_append(self, message: str) -> None:
        """Append a message to the log console.

        Args:
            message: String message to append (can be a status/system message)
        """
        self.log_model.append_system_message(message)
        # Auto-scroll to bottom
        self.log_console.scrollToBottom()

    def _log_clear(self) -> None:
        """Clear the log console completely."""
        self.log_model.clear_events()

    def capture_prev_front_app(self) -> None:
        """Capture the frontmost app (macOS) to reactivate when recording starts."""
        if sys.platform != "darwin":
            return
        try:
            name = (
                subprocess.check_output(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to get name of '
                        "(first process whose frontmost is true)",
                    ]
                )
                .decode("utf-8")
                .strip()
            )
            if name:
                self.prev_front_app_name = name
        except Exception:
            pass

    def activate_previous_app(self) -> None:
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
                        'tell application "System Events" to '
                        "key code 48 using {command down}",
                    ],
                    check=False,
                )
        except Exception:
            pass

    def on_always_on_top_toggled(self, checked: bool) -> None:
        """Apply the always-on-top flag and re-show the window to take effect."""
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        # setWindowFlag hides the window, so re-show it
        if was_visible:
            self.show()

    def _toggle_log_from_action(self, checked: bool) -> None:
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

    def _toggle_options_panel(self, checked: bool) -> None:
        """Show or hide the advanced options pane."""
        if hasattr(self, "options_group"):
            self.options_group.setVisible(bool(checked))

    def _show_help(self) -> None:
        """Display a small dialog with keyboard shortcuts."""
        QMessageBox.information(
            self,
            "Shortcuts",
            "F1 - Start Recording (backgrounds window)\n"
            "F2 - Stop Recording (restores window)\n"
            "F3 - Play Once\n"
            "F4 - Play Forever\n"
            "F5 - Stop Playback",
        )

    def _f5_signal_consumer(self) -> None:
        """Thread that monitors the F5 signal queue from subprocess."""
        import queue

        stop_event = self._f5_consumer_stop_event
        signal_queue = self._f5_signal_queue
        if stop_event is None or signal_queue is None:
            return
        while not stop_event.is_set():
            try:
                msg = signal_queue.get(timeout=0.1)
                if msg == "STOP":
                    # Schedule stop on the Qt main thread
                    QTimer.singleShot(0, self.stop_playback_gui)
            except queue.Empty:
                # Timeout - normal operation, continue polling
                continue
            except (OSError, ValueError):
                # Queue closed or invalid state - exit gracefully
                logging.debug("F5 consumer thread: Queue closed, exiting")
                break
            except Exception:
                # Unexpected error - log and exit to avoid infinite loop
                logging.exception("F5 consumer thread: Unexpected error")
                break

    def _start_playback_hotkeys(self) -> None:
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
                )
                self._f5_consumer_thread.start()

                logging.debug("Started F5 hotkey subprocess on macOS")
            except Exception as e:
                logging.warning("Failed to start F5 hotkey subprocess: %s", e)
                self._cleanup_f5_subprocess()
        else:
            # Windows/Linux: use in-process listener (no CGEventTap conflict)
            def on_key_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
                try:
                    if key == keyboard.Key.f5:
                        # Schedule stop on the Qt main thread
                        QTimer.singleShot(0, self.stop_playback_gui)
                except Exception:
                    logging.exception("Error in global hotkey on_key_press handler")

            def win32_event_filter(msg: int, data: Any) -> bool:
                # Windows only (ignored on Linux): swallow F5 so the focused
                # app doesn't also react (e.g. a browser refreshing the page).
                # suppress_event() raises to signal suppression and skips
                # on_press, so schedule the stop here and don't catch it.
                if getattr(data, "vkCode", None) == F5_VKCODE_WINDOWS:
                    QTimer.singleShot(0, self.stop_playback_gui)
                    listener = self._play_hotkey_listener
                    if listener is not None:
                        listener.suppress_event()
                return True

            try:
                self._play_hotkey_listener = keyboard.Listener(
                    on_press=on_key_press, win32_event_filter=win32_event_filter
                )
                self._play_hotkey_listener.start()
            except Exception as e:
                # If listener fails, continue without global hotkey
                # but log for diagnostics
                logging.warning("Failed to start global hotkey listener: %s", e)
                self._play_hotkey_listener = None

    def _cleanup_f5_subprocess(self) -> None:
        """Clean up the F5 hotkey subprocess and associated resources."""
        # Stop consumer thread
        if self._f5_consumer_stop_event is not None:
            self._f5_consumer_stop_event.set()
        if self._f5_consumer_thread is not None and self._f5_consumer_thread.is_alive():
            self._f5_consumer_thread.join(timeout=1.0)
            if self._f5_consumer_thread.is_alive():
                logging.warning("F5 consumer thread did not stop within timeout")
        # Stop subprocess with verification
        if self._f5_stop_event is not None:
            self._f5_stop_event.set()

        if self._f5_subprocess is not None and self._f5_subprocess.is_alive():
            # First attempt: wait for graceful shutdown
            self._f5_subprocess.join(timeout=1.0)

            # Second attempt: terminate if still alive
            if self._f5_subprocess.is_alive():
                self._f5_subprocess.terminate()
                self._f5_subprocess.join(timeout=0.5)

            # Third attempt: force kill if still alive
            if self._f5_subprocess.is_alive():
                pid = self._f5_subprocess.pid
                if hasattr(os, "kill") and pid is not None:
                    # POSIX systems
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass  # Process already terminated
                else:
                    # Fallback for non-POSIX or if kill() fails
                    self._f5_subprocess.kill()

                self._f5_subprocess.join(timeout=0.5)

            # Verify termination
            if self._f5_subprocess.exitcode is None:
                logging.warning(
                    "F5 subprocess did not terminate cleanly (exitcode: %s)",
                    self._f5_subprocess.exitcode,
                )
            else:
                logging.debug(
                    "F5 subprocess terminated with exitcode: %s",
                    self._f5_subprocess.exitcode,
                )

        # Close and cleanup the queue
        if self._f5_signal_queue is not None:
            try:
                self._f5_signal_queue.close()
                # Release background thread resources for multiprocessing.Queue
                if hasattr(self._f5_signal_queue, "join_thread"):
                    self._f5_signal_queue.join_thread()
                logging.debug("F5 signal queue closed and joined")
            except Exception as e:
                logging.warning("Error closing F5 signal queue: %s", e)

        # Clear references
        self._f5_subprocess = None
        self._f5_stop_event = None
        self._f5_signal_queue = None
        self._f5_consumer_thread = None
        self._f5_consumer_stop_event = None

    def _stop_playback_hotkeys(self) -> None:
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

    def _cleanup_after_playback(self) -> None:
        # Beep to signal completion and stop any global hotkey listener
        try:
            QApplication.beep()
        except Exception:
            pass
        self._update_window_title()
        self._stop_playback_hotkeys()
        # Restore window if it was backgrounded for playback
        if self._restore_on_top_after_play:
            self._restore_on_top_after_play = False
            if not self.always_on_top_action.isChecked():
                self.always_on_top_action.setChecked(True)
            self.show()
            self.raise_()
            self.activateWindow()

    def _prepare_for_playback(self) -> None:
        """Lower window, manage top-most state, and enable F5 stop hotkey."""
        # Pass playback speed to the app
        self.app.playback_speed = self.speed_spin.value()

        # Parse max idle time from GUI and pass to app
        max_idle_text = self.max_idle_entry.text().strip()
        if max_idle_text:
            try:
                # Convert ms to seconds for the player
                ms_value = int(max_idle_text)
                # Treat negative or zero as no limit
                self.app.max_idle_time = ms_value / 1000.0 if ms_value > 0 else None
            except ValueError:
                self.app.max_idle_time = None
        else:
            self.app.max_idle_time = None

        # Send window to background and manage always-on-top, then enable F5 stop
        if self.isVisible():
            if self.always_on_top_action.isChecked():
                self._restore_on_top_after_play = True
                self.always_on_top_action.setChecked(False)
            self.lower()
        self._start_playback_hotkeys()

    def _start_recording_delayed(self) -> None:
        """Start recording after PyQt6 event loop is fully initialized"""
        try:
            self.app.start_recording()

            # Recording started successfully
            self._update_window_title("● Recording")
            self.status_bar.showMessage("🔴 Recording started successfully!")
            self._log_append("✅ Recording Session Started Successfully")
            self._log_append("Monitoring mouse and keyboard events...")

            # Start the log update timer
            if not self.log_timer.isActive():
                self.log_timer.start(100)

        except Exception as e:
            # Recording failed - show error and clean up
            error_msg = f"❌ Failed to start recording: {str(e)}"
            print(error_msg)
            self.status_bar.showMessage("❌ Recording failed - Check permissions")

            self._log_append(f"❌ Recording Failed: {str(e)}")
            self._log_append("")
            self._log_append("💡 Troubleshooting Tips:")
            self._log_append(
                "• Go to System Settings → Privacy & Security → Accessibility"
            )
            self._log_append("• Add your Terminal or Python to the list")
            self._log_append("• Restart the application after granting permissions")

            # Reset button state
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Show Log")
            self.toggle_log_action.setChecked(False)
            self.toggle_log_action.blockSignals(False)

            # If we hid the window earlier, restore it on failure
            if self.was_hidden_for_recording:
                self.was_hidden_for_recording = False
                self.show()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Handle application exit by cleaning up subprocess and threads."""
        # Cancel any pending countdown and persist settings
        self._countdown_active = False
        self._save_settings()

        # Stop playback if active
        if self.app.player.playing:
            self.app.stop_playback()

        # Stop recording if active
        if self.app.recorder.recording:
            self.app.stop_recording()

        # Clean up F5 hotkey resources
        self._stop_playback_hotkeys()

        # Stop timers
        if self.log_timer.isActive():
            self.log_timer.stop()
        if self.play_progress_timer.isActive():
            self.play_progress_timer.stop()

        # Accept the close event
        if event is not None:
            event.accept()

    def run(self) -> None:
        """Capture previous app (macOS) and show the GUI window."""
        # Don't setup global hotkeys in GUI mode - they conflict with PyQt6
        # Capture the app currently in front, so we can reactivate it
        # when we hide ourselves
        self.capture_prev_front_app()
        self.show()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    gui = MacroGUI()
    gui.run()
    sys.exit(app.exec())
