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
from functools import partial
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
    QFrame,
    QMenu,
    QSizePolicy,
    QLayout,
    QLayoutItem,
)
from PyQt6.QtCore import (
    Qt,
    QObject,
    QTimer,
    QSize,
    QAbstractListModel,
    QModelIndex,
    QSettings,
    QRect,
    QRectF,
    QPoint,
)
from PyQt6.QtGui import (
    QKeySequence,
    QAction,
    QCloseEvent,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QIntValidator,
)
from PyQt6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem
from .MacroApp import MacroApp
from .MacroRecorder import secure_input_state
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

    # Row category, used by the delegate to pick a text colour
    CategoryRole = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._events: list[Event] = []
        self._formatted_cache: list[str] = []
        self._categories: list[str] = []
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

        if role == self.CategoryRole:
            return self._categories[index.row()]

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

        text, category = formatted
        # Notify views that we're adding a row
        row = len(self._events)
        self.beginInsertRows(QModelIndex(), row, row)
        self._events.append(event)
        self._formatted_cache.append(text)
        self._categories.append(category)
        self.endInsertRows()
        return True

    def append_system_message(self, message: str, category: str = "system") -> None:
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
        self._categories.append(category)
        self.endInsertRows()

    def clear_events(self) -> None:
        """Clear all events from the model."""
        if not self._events:
            return

        self.beginResetModel()
        self._events.clear()
        self._formatted_cache.clear()
        self._categories.clear()
        self.last_mouse_pos = None
        self.mouse_move_count = 0
        self.endResetModel()

    def _format_event(self, event: Event) -> tuple[str, str] | None:
        """Format a single event for display in the log.

        Args:
            event: Event dictionary from the recorder

        Returns:
            The display text paired with its colour category, or None to
            filter this event out.
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
                    f"[{timestamp}] Mouse Move "
                    f"#{self.mouse_move_count} → ({x}, {y})",
                    "mouse",
                )
            return None  # Skip this event

        elif event_type == "mouse_click":
            button = event.get("button", "unknown")
            action = "Press" if event.get("pressed") else "Release"
            x, y = event.get("x", 0), event.get("y", 0)
            return (
                f"[{timestamp}] Mouse {action} → {button} at ({x}, {y})",
                "mouse",
            )

        elif event_type == "mouse_scroll":
            dx, dy = event.get("dx", 0), event.get("dy", 0)
            x, y = event.get("x", 0), event.get("y", 0)
            return (
                f"[{timestamp}] Mouse Scroll → ({dx}, {dy}) at ({x}, {y})",
                "mouse",
            )

        elif event_type == "key_press":
            key = event.get("key", "unknown")
            return f"[{timestamp}] Key Press → {key}", "keyboard"

        elif event_type == "key_release":
            key = event.get("key", "unknown")
            return f"[{timestamp}] Key Release → {key}", "keyboard"

        else:
            return f"[{timestamp}] Unknown Event → {event_type}", "error"


class EventLogDelegate(QStyledItemDelegate):
    """Custom delegate for rendering event log items with enhanced styling."""

    # One colour per row category reported by EventLogModel, in two sets so
    # the log stays readable on a light desktop theme as well as a dark one.
    DARK_COLORS = {
        "mouse": QColor("#4A9EFF"),
        "keyboard": QColor("#50C878"),
        "system": QColor("#FFB84D"),
        "error": QColor("#FF6B6B"),
    }
    LIGHT_COLORS = {
        "mouse": QColor("#0B5FBF"),
        "keyboard": QColor("#1E7A3C"),
        "system": QColor("#A65A00"),
        "error": QColor("#C0342B"),
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.category_colors = self.colors_for(parent)

    @classmethod
    def colors_for(cls, widget: QObject | None) -> dict[str, QColor]:
        """Pick the colour set that suits the widget's background."""
        if not isinstance(widget, QWidget):
            return cls.DARK_COLORS
        base = widget.palette().color(QPalette.ColorRole.Base)
        return cls.DARK_COLORS if base.lightnessF() < 0.5 else cls.LIGHT_COLORS

    def initStyleOption(
        self, option: QStyleOptionViewItem | None, index: QModelIndex
    ) -> None:
        """Initialize style options with custom colors based on event type."""
        super().initStyleOption(option, index)
        if option is None:
            return
        color = self.category_colors.get(index.data(EventLogModel.CategoryRole))
        if color is not None:
            option.palette.setColor(
                QPalette.ColorGroup.All, QPalette.ColorRole.Text, color
            )


class CountdownOverlay(QWidget):
    """Frameless, click-through, always-on-top countdown display.

    The main window is lowered while a pre-start countdown runs, so the
    status bar is out of sight; this overlay keeps the remaining seconds
    visible on screen. It is transparent to input and never takes focus,
    so the app about to be recorded or played into stays active.
    """

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        frame = QFrame(self)
        frame.setObjectName("countdownFrame")
        frame.setStyleSheet("""
            QFrame#countdownFrame {
                background-color: rgba(20, 20, 20, 240);
                border: 1px solid rgba(255, 255, 255, 60);
                border-radius: 18px;
            }
            QLabel { color: #ffffff; background: transparent; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        inner = QVBoxLayout(frame)
        inner.setContentsMargins(28, 20, 28, 20)
        inner.setSpacing(4)

        self.caption_label = QLabel("")
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        inner.addWidget(self.caption_label)

        self.number_label = QLabel("")
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_label.setStyleSheet("font-size: 64px; font-weight: 700;")
        inner.addWidget(self.number_label)

    def show_countdown(self, caption: str, seconds: int) -> None:
        """Show the overlay with the given caption and starting count."""
        self.caption_label.setText(caption)
        self.set_remaining(seconds)
        self.show()

    def set_remaining(self, seconds: int) -> None:
        """Update the displayed count and keep the overlay centered."""
        self.number_label.setText(str(seconds))
        self.adjustSize()
        self._center_on_cursor_screen()

    def _center_on_cursor_screen(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.move(geometry.center() - self.rect().center())


class FlowLayout(QLayout):
    """Left-to-right layout that wraps to a new row when it runs out of width.

    Keeps the options pane from forcing the window wider than the user sized
    it; a QHBoxLayout would instead push the window's minimum width out to the
    sum of every control.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(10)

    # QLayout plumbing -----------------------------------------------------
    def addItem(self, a0: QLayoutItem | None) -> None:
        if a0 is not None:
            self._items.append(a0)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    # Sizing ---------------------------------------------------------------
    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, a0: int) -> int:
        return self._arrange(QRect(0, 0, a0, 0), apply_geometry=False)

    def minimumSize(self) -> QSize:
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def setGeometry(self, a0: QRect) -> None:
        super().setGeometry(a0)
        used = self._arrange(a0, apply_geometry=True)
        # heightForWidth() does not survive the trip through a QSplitter, so
        # claim the height the wrapped rows need directly. The value settles
        # after one extra layout pass and only changes when the rows do.
        parent = self.parentWidget()
        if parent is not None:
            needed = used + parent.height() - a0.height()
            if parent.minimumHeight() != needed:
                parent.setMinimumHeight(needed)

    def _arrange(self, rect: QRect, apply_geometry: bool) -> int:
        """Place the items row by row and return the total height used."""
        margins = self.contentsMargins()
        area = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        spacing = max(self.spacing(), 0)
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if row_height and x + hint.width() > area.right():
                x = area.x()
                y += row_height + spacing
                row_height = 0
            if apply_geometry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class MacroGUI(QMainWindow):
    """Main window for recording and playback controls with logging."""

    # Fallback height for the log pane when no previous height is known
    LOG_PANE_DEFAULT_HEIGHT = 220
    # How many entries the File > Open Recent submenu keeps
    MAX_RECENT_FILES = 8

    def __init__(self) -> None:
        super().__init__()
        self.app = MacroApp()
        self.setWindowTitle("Macro Recorder")
        self.setGeometry(100, 100, 620, 320)
        self.setAcceptDrops(True)  # drop a .json macro onto the window
        # Default to always-on-top
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # Recently opened macros, newest first (persisted in QSettings)
        self._recent_paths: list[str] = []

        # Menu bar and toolbar (both driven by the same QActions)
        self._create_actions()
        self._build_menubar()
        self._build_toolbar()

        # Create splitter for controls and log
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        splitter = self.splitter

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

        # No stylesheet: the view then takes its background, frame and
        # alternating rows from the desktop theme's palette.
        log_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        log_font.setPointSizeF(max(9.0, log_font.pointSizeF() - 1.0))
        self.log_console.setFont(log_font)
        # Enable alternating row colors for better readability
        self.log_console.setAlternatingRowColors(True)
        # Disable editing
        self.log_console.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.log_section = QGroupBox("Log")
        log_section_layout = QVBoxLayout(self.log_section)
        log_section_layout.setContentsMargins(8, 8, 8, 8)
        log_section_layout.setSpacing(6)
        log_section_layout.addWidget(self.log_console)
        self.log_section.hide()  # Initially hidden
        splitter.addWidget(self.log_section)

        # Extra height belongs to the log, not to the fixed-size controls
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter)

        # Shortcuts hint: a one-line footer pinned just above the status bar
        self.shortcuts_label = QLabel(
            "F1 Start • F2 Stop Rec • F3 Play Once • F4 Play ∞ • F5 Stop"
        )
        self.shortcuts_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.shortcuts_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        hint_font = self.shortcuts_label.font()
        hint_font.setPointSizeF(max(9.0, hint_font.pointSizeF() - 1.5))
        self.shortcuts_label.setFont(hint_font)
        # Dim the hint through the palette rather than a hardcoded grey, so it
        # stays legible whichever theme the desktop is using.
        hint_palette = self.shortcuts_label.palette()
        hint_palette.setColor(
            QPalette.ColorRole.WindowText,
            hint_palette.color(QPalette.ColorRole.PlaceholderText),
        )
        self.shortcuts_label.setPalette(hint_palette)

        # A real separator line, drawn by the platform style
        footer_rule = QFrame()
        footer_rule.setFrameShape(QFrame.Shape.HLine)
        footer_rule.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(footer_rule)
        main_layout.addWidget(self.shortcuts_label)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
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
        self._countdown_overlay = CountdownOverlay()

        # Window height bookkeeping for showing/hiding the log pane
        self._height_with_log = 0
        self._geometry_restored = False

        # Persisted settings (geometry, options, last used directory)
        self._settings = QSettings("macro-py", "MacroRecorder")
        self._restore_settings()
        self._update_window_title()
        # Start snug: without a remembered geometry the default height would
        # leave dead space under the controls while the log pane is hidden.
        if not self._geometry_restored and not self.log_section.isVisible():
            self._fit_window_height(False)

    def _restore_settings(self) -> None:
        """Restore persisted UI options from the previous session."""
        s = self._settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
            self._geometry_restored = True
        self.loop_entry.setText(s.value("loops", "5", str))
        self.max_idle_entry.setText(s.value("maxIdleMs", "", str))
        try:
            self.speed_spin.setValue(float(s.value("speed", 1.0)))
        except (TypeError, ValueError):
            self.speed_spin.setValue(1.0)
        # Key renamed from "countdown" (which briefly shipped with a
        # surprising default of 3) so stale values don't delay recording.
        try:
            self.countdown_spin.setValue(int(s.value("countdownSeconds", 0)))
        except (TypeError, ValueError):
            self.countdown_spin.setValue(0)
        self.compress_moves_checkbox.setChecked(s.value("compressMoves", True, bool))
        self.activate_on_record_checkbox.setChecked(
            s.value("activateOnRecord", True, bool)
        )
        self.activate_on_play_checkbox.setChecked(s.value("activateOnPlay", True, bool))
        self.always_on_top_action.setChecked(s.value("alwaysOnTop", True, bool))
        try:
            stored = json.loads(s.value("recentFiles", "[]", str))
        except (json.JSONDecodeError, TypeError):
            stored = []
        self._recent_paths = [p for p in stored if isinstance(p, str)][
            : self.MAX_RECENT_FILES
        ]
        self._rebuild_recent_menu()
        # Keep the restored geometry: the saved height already includes the
        # log pane, so don't grow the window on top of it.
        if s.value("showLog", False, bool):
            self._set_log_visible(True, fit_window=False)

    def _save_settings(self) -> None:
        """Persist UI options for the next session."""
        s = self._settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("loops", self.loop_entry.text())
        s.setValue("maxIdleMs", self.max_idle_entry.text())
        s.setValue("speed", self.speed_spin.value())
        s.setValue("countdownSeconds", self.countdown_spin.value())
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
        # "[*]" is Qt's unsaved-changes placeholder: macOS turns it into the
        # dot in the close button, Windows and Linux into a literal asterisk.
        title += "[*]"
        if self.app.macro_data:
            count = len(self.app.macro_data)
            title += f" ({count} event{'s' if count != 1 else ''})"
        if state:
            title = f"{state} — {title}"
        # Gives macOS the draggable proxy icon for the open document
        self.setWindowFilePath(self.current_macro_path or "")
        self.setWindowTitle(title)
        self.setWindowModified(self.macro_dirty)
        # Every caller here has just changed recording/playback/file state,
        # which is exactly when the menus and toolbar need re-evaluating.
        self._refresh_action_states()

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
        self._refresh_action_states()
        self._countdown_overlay.show_countdown(label, seconds)
        remaining = {"n": seconds}

        def tick() -> None:
            if not self._countdown_active:
                self._countdown_overlay.hide()
                return
            if remaining["n"] <= 0:
                self._countdown_active = False
                self._countdown_overlay.hide()
                on_done()
                return
            self.status_bar.showMessage(f"{label} in {remaining['n']}…")
            self._countdown_overlay.set_remaining(remaining["n"])
            remaining["n"] -= 1
            QTimer.singleShot(1000, tick)

        tick()

    def _cancel_countdown(self) -> None:
        """Abort a pending countdown and remove its overlay immediately."""
        self._countdown_active = False
        self._countdown_overlay.hide()
        self._refresh_action_states()

    GLYPH_SIZE = 16  # logical size of the hand-drawn toolbar glyphs

    def _glyph_icon(self, kind: str, color: QColor | None = None) -> QIcon:
        """Draw a monochrome toolbar glyph.

        Qt's standard icon set ships Fusion-era artwork - a floppy disk for
        save, a blue swirl for reload - which sits badly next to everything
        else in the window, so the toolbar draws its own in the palette's ink.
        """
        scale = 3  # supersample so the glyphs stay crisp on retina screens
        pixmap = QPixmap(self.GLYPH_SIZE * scale, self.GLYPH_SIZE * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = color or self.palette().color(QPalette.ColorRole.ButtonText)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        self._draw_glyph(painter, kind, ink, float(scale))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return QIcon(pixmap)

    def _draw_glyph(
        self, painter: QPainter, kind: str, ink: QColor, scale: float
    ) -> None:
        """Paint one glyph in a 16x16 logical box scaled up by `scale`."""

        def u(value: float) -> float:
            return value * scale

        def triangle(points: list[tuple[float, float]]) -> None:
            path = QPainterPath()
            path.moveTo(u(points[0][0]), u(points[0][1]))
            for x, y in points[1:]:
                path.lineTo(u(x), u(y))
            path.closeSubpath()
            painter.fillPath(path, ink)

        def bar(x: float, y: float, w: float, h: float) -> None:
            radius = u(min(w, h) / 2)
            painter.drawRoundedRect(QRectF(u(x), u(y), u(w), u(h)), radius, radius)

        if kind == "record":
            painter.drawEllipse(QRectF(u(2.5), u(2.5), u(11), u(11)))
        elif kind == "stop":
            painter.drawRoundedRect(QRectF(u(3), u(3), u(10), u(10)), u(1.5), u(1.5))
        elif kind == "play":
            triangle([(4, 2.5), (13, 8), (4, 13.5)])
        elif kind == "loop":
            pen = QPen(ink, u(1.7))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(u(3), u(4), u(10), u(10)), 30 * 16, 300 * 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            triangle([(9.2, 1.2), (13.8, 4.4), (8.8, 6.4)])
        elif kind == "list":
            for row in (3.7, 7.2, 10.7):
                bar(3, row, 10, 1.6)
        elif kind in ("save", "open"):
            bar(3, 12.4, 10, 1.6)
            if kind == "save":
                bar(7.2, 2.2, 1.6, 6.2)
                triangle([(4.6, 7.4), (11.4, 7.4), (8, 11.2)])
            else:
                bar(7.2, 5.6, 1.6, 5.2)
                triangle([(4.6, 6.4), (11.4, 6.4), (8, 2.2)])
        else:  # pragma: no cover - guards against a typo in a caller
            raise ValueError(f"unknown glyph: {kind}")

    def _create_actions(self) -> None:
        """Create the actions shared by the menu bar and the toolbar.

        Each action carries a full command name for the menus plus a short
        icon text for the toolbar button underneath it.
        """

        def make(
            text: str,
            icon_text: str,
            icon: QIcon,
            shortcut: QKeySequence | QKeySequence.StandardKey | str,
            slot: Callable[[], None],
        ) -> QAction:
            action = QAction(icon, text, self)
            action.setIconText(icon_text)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            action.triggered.connect(slot)
            return action

        # Recording
        self.action_start_rec = make(
            "Start Recording",
            "Record",
            self._glyph_icon("record", QColor("#e0443e")),
            "F1",
            self.start_recording_gui,
        )
        self.action_stop_rec = make(
            "Stop Recording",
            "Stop Rec",
            self._glyph_icon("stop", QColor("#e0443e")),
            "F2",
            self.stop_recording_gui,
        )

        # Playback
        self.action_play_once = make(
            "Play Once",
            "Play",
            self._glyph_icon("play"),
            "F3",
            self.play_once_gui,
        )
        self.action_play_infinite = make(
            "Play Forever",
            "Loop",
            self._glyph_icon("loop"),
            "F4",
            self.play_infinite_gui,
        )
        self.action_play_loops = make(
            "Play Loop Count",
            "Play Loops",
            self._glyph_icon("play"),
            "",
            self.play_x,
        )
        self.action_play_loops.setToolTip("Play the number of loops set below")
        self.action_stop_play = make(
            "Stop Playback",
            "Stop",
            self._glyph_icon("stop"),
            "F5",
            self.stop_playback_gui,
        )

        # File
        self.action_save = make(
            "Save Macro…",
            "Save",
            self._glyph_icon("save"),
            QKeySequence.StandardKey.Save,
            self.save_macro,
        )
        self.action_load = make(
            "Open Macro…",
            "Open",
            self._glyph_icon("open"),
            QKeySequence.StandardKey.Open,
            self.load_macro,
        )

        # View toggles. The label stays put and the checkmark carries the
        # state, the way native View menus do it.
        self.toggle_log_action = QAction(self._glyph_icon("list"), "Show Log", self)
        self.toggle_log_action.setIconText("Log")
        self.toggle_log_action.setCheckable(True)
        self.toggle_log_action.setShortcut(QKeySequence("Ctrl+L"))
        self.toggle_log_action.toggled.connect(self._toggle_log_from_action)

        self.action_toggle_options = QAction("Show Advanced Options", self)
        self.action_toggle_options.setCheckable(True)
        # macOS would otherwise guess from the word "options" that this
        # belongs in the application menu as Preferences.
        self.action_toggle_options.setMenuRole(QAction.MenuRole.NoRole)
        self.action_toggle_options.toggled.connect(self._toggle_options_panel)

        self.always_on_top_action = QAction("Always on Top", self)
        self.always_on_top_action.setCheckable(True)
        self.always_on_top_action.setChecked(True)
        self.always_on_top_action.toggled.connect(self.on_always_on_top_toggled)

        self.action_help = QAction("Keyboard Shortcuts…", self)
        self.action_help.triggered.connect(self._show_help)

        self.action_about = QAction("About Macro Recorder", self)
        # macOS lifts this into the application menu; elsewhere it stays in Help
        self.action_about.setMenuRole(QAction.MenuRole.AboutRole)
        self.action_about.triggered.connect(self._show_about)

    def _build_menubar(self) -> None:
        """Populate the menu bar; macOS moves it into the system menu bar."""
        # Built before the menu bar so _rebuild_recent_menu() always has it
        self.recent_menu = QMenu("Open Recent", self)
        self.recent_menu.setToolTipsVisible(True)  # entries show their full path

        bar = self.menuBar()
        if bar is None:
            return

        def add_menu(title: str, actions: list[QAction | None]) -> None:
            """Add one menu; a None entry becomes a separator."""
            menu = bar.addMenu(title)
            if menu is None:
                return
            for action in actions:
                if action is None:
                    menu.addSeparator()
                else:
                    menu.addAction(action)

        file_menu = bar.addMenu("&File")
        if file_menu is not None:
            file_menu.addAction(self.action_load)
            file_menu.addMenu(self.recent_menu)
            file_menu.addSeparator()
            file_menu.addAction(self.action_save)
        add_menu("&Record", [self.action_start_rec, self.action_stop_rec])
        add_menu(
            "&Playback",
            [
                self.action_play_once,
                self.action_play_infinite,
                self.action_play_loops,
                None,
                self.action_stop_play,
            ],
        )
        add_menu(
            "&View",
            [
                self.toggle_log_action,
                self.action_toggle_options,
                None,
                self.always_on_top_action,
            ],
        )
        add_menu("&Help", [self.action_about, self.action_help])

    def _build_toolbar(self) -> None:
        """Build the transport toolbar. No stylesheet here on purpose: one
        would opt the tool buttons out of their native rendering."""
        toolbar = QToolBar("Main")
        toolbar.setObjectName("MainToolBar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        toolbar.addAction(self.action_start_rec)
        toolbar.addAction(self.action_stop_rec)
        toolbar.addSeparator()
        toolbar.addAction(self.action_play_once)
        toolbar.addAction(self.action_play_infinite)
        toolbar.addAction(self.action_stop_play)
        toolbar.addSeparator()
        toolbar.addAction(self.action_load)
        toolbar.addAction(self.action_save)

        # Push the view toggle to the trailing edge, away from the commands
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addAction(self.toggle_log_action)

    def _rebuild_recent_menu(self) -> None:
        """Refill the Open Recent submenu from the stored path list."""
        menu = self.recent_menu
        menu.clear()
        menu.setEnabled(bool(self._recent_paths))
        for path in self._recent_paths:
            entry = menu.addAction(os.path.basename(path))
            if entry is None:
                continue
            entry.setToolTip(path)
            entry.triggered.connect(partial(self._open_recent, path))
        if not self._recent_paths:
            return
        menu.addSeparator()
        clear_entry = menu.addAction("Clear Menu")
        if clear_entry is not None:
            clear_entry.triggered.connect(self._clear_recent_files)

    def _schedule_recent_menu_rebuild(self) -> None:
        """Rebuild the Open Recent submenu on the next event loop turn.

        Its entries ask for the rebuild from their own `triggered` signal, and
        the rebuild deletes the very action that is still emitting, so this
        must not run while Qt is inside that emission.
        """
        QTimer.singleShot(0, self._rebuild_recent_menu)

    def _note_recent_file(self, path: str) -> None:
        """Move a path to the top of the recent list and persist it."""
        full = os.path.abspath(path)
        remaining = [p for p in self._recent_paths if p != full]
        self._recent_paths = [full, *remaining][: self.MAX_RECENT_FILES]
        self._settings.setValue("recentFiles", json.dumps(self._recent_paths))
        self._schedule_recent_menu_rebuild()

    def _clear_recent_files(self, checked: bool = False) -> None:
        """Empty the Open Recent submenu."""
        del checked  # the triggered signal passes a checked flag
        self._recent_paths = []
        self._settings.setValue("recentFiles", json.dumps(self._recent_paths))
        self._schedule_recent_menu_rebuild()

    def _open_recent(self, path: str, checked: bool = False) -> None:
        """Open one entry from the Open Recent submenu."""
        del checked  # the triggered signal passes a checked flag
        if not os.path.exists(path):
            self.status_bar.showMessage(f"Macro no longer exists: {path}")
            self._recent_paths = [p for p in self._recent_paths if p != path]
            self._settings.setValue("recentFiles", json.dumps(self._recent_paths))
            self._schedule_recent_menu_rebuild()
            return
        if not self._confirm_discard_unsaved("Open another macro and discard it?"):
            return
        self._load_macro_path(path)

    def _refresh_action_states(self) -> None:
        """Grey out commands that do not apply to the current state."""
        recording = self.app.recorder.recording
        playing = self.app.player.playing
        busy = recording or playing or self._countdown_active
        has_macro = bool(self.app.macro_data)

        self.action_start_rec.setEnabled(not busy)
        self.action_stop_rec.setEnabled(recording or self._countdown_active)
        self.action_play_once.setEnabled(has_macro and not busy)
        self.action_play_infinite.setEnabled(has_macro and not busy)
        self.action_play_loops.setEnabled(has_macro and not busy)
        self.action_stop_play.setEnabled(playing or self._countdown_active)
        self.action_save.setEnabled(has_macro and not recording)
        self.action_load.setEnabled(not busy)
        self.play_x_btn.setEnabled(has_macro and not busy)
        self.stop_btn.setEnabled(playing or self._countdown_active)

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

        self.play_x_btn = QPushButton("Play")
        self.play_x_btn.clicked.connect(self.play_x)
        playback_row.addWidget(self.play_x_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_playback_gui)
        playback_row.addWidget(self.stop_btn)

        playback_row.addStretch()
        layout.addLayout(playback_row)

        # Options panel (advanced)
        self.options_group = QGroupBox("Options")
        options_layout = FlowLayout(self.options_group)
        options_layout.setContentsMargins(8, 8, 8, 8)

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
        self.countdown_spin.setValue(0)
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

        self.options_group.setVisible(False)
        layout.addWidget(self.options_group)

        # Soak up leftover vertical space here so the panes below keep their
        # natural height instead of stretching to fill the window.
        layout.addStretch(1)

    def start_recording_gui(self) -> None:
        """Start recording and update UI/log state accordingly."""
        if (
            not self.app.recorder.recording
            and not self.app.player.playing
            and not self._countdown_active
        ):
            # Warn before an unsaved macro is overwritten by a new recording
            if not self._confirm_discard_unsaved(
                "Start a new recording and discard it?"
            ):
                return
            try:
                self.status_bar.showMessage("Starting recording...")

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
                self._set_log_visible(True)
                self._log_clear()
                self._log_append("Initializing Recording Session...")
                self._log_append("=" * 50)

                # Initialize counters first
                self.last_event_count = 0
                # self.last_mouse_pos = None
                # self.mouse_move_count = 0

                # Defer recording startup to avoid PyQt6 event loop conflicts,
                # after an optional countdown so the user can get in position
                self._begin_countdown(
                    "Recording starting",
                    lambda: QTimer.singleShot(100, self._start_recording_delayed),
                )

                self.status_bar.showMessage("Initializing listeners...")
                self._log_append("Starting listeners in background...")

            except Exception as e:
                # Recording failed - show error and clean up
                error_msg = f"Failed to start recording: {str(e)}"
                print(error_msg)
                self.status_bar.showMessage("Recording failed - Check permissions")

                # If we hid the window, restore it on failure
                if self.was_hidden_for_recording:
                    self.was_hidden_for_recording = False
                    self.show()

                self._log_append(f"Recording Failed: {str(e)}", "error")
                self._log_append("")
                self._log_append("Troubleshooting Tips:", "error")
                self._log_append(
                    "• Go to System Settings → Privacy & Security → Accessibility"
                )
                self._log_append("• Add your Terminal or Python to the list")
                self._log_append("• Restart the application after granting permissions")

                # The log stays up so the tips above remain readable; just
                # make sure the toolbar toggle still reflects that.
                self._sync_log_action(self.log_section.isVisible())

        else:
            self.status_bar.showMessage(
                "Cannot start recording - already recording or playing"
            )

    def _warn_if_keys_are_blocked(self) -> None:
        """Say so when macOS Secure Input will swallow every keystroke.

        Without this the recording silently captures mouse events only, and
        F2 never reaches the recorder, so stopping from the background looks
        broken rather than blocked.
        """
        state = secure_input_state()
        if state is None:
            return
        if state.stale:
            self._log_append(
                "Secure Input is stuck on: the app that enabled it has already "
                "exited without releasing it, so macOS is still withholding "
                "key presses. Log out and back in to clear it.",
                "error",
            )
            blame = "a closed app"
        else:
            who = state.holder or "another app"
            self._log_append(
                f"Secure Input is on (held by {who}) - macOS is withholding "
                f"key presses from this app. Defocus its password field, quit "
                f"{who}, or - if it is a background helper that reclaims the "
                "lock on relaunch - turn the feature off in its own settings.",
                "error",
            )
            blame = who
        self._log_append(
            "Mouse events still record. Keystrokes and the F2 stop hotkey do "
            "not, so stop with F2 only while this window has focus, or use the "
            "Stop Rec button.",
            "error",
        )
        self.status_bar.showMessage(f"Recording - keys blocked by {blame}")

    def stop_recording_gui(self) -> None:
        """Stop recording and restore window/topmost state if needed."""
        # Cancel a pending recording countdown
        if self._countdown_active and not self.app.recorder.recording:
            self._cancel_countdown()
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
                f"Recording stopped - {len(self.app.macro_data)} events recorded"
            )

            # Stop log timer and add summary
            if self.log_timer.isActive():
                self.log_timer.stop()
            self._log_append("=" * 50)
            self._log_append(
                f"Recording Complete: {len(self.app.macro_data)} events captured"
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
            self._update_window_title("Playing")
            self.status_bar.showMessage(running_msg)
            if not self.play_progress_timer.isActive():
                self.play_progress_timer.start(200)

        self._begin_countdown("Playback starting", go)

    def play_once_gui(self) -> None:
        """Prepare and play current macro once."""
        self._start_playback_flow(self.app.play_once, "Running 1/1 loops")

    def play_infinite_gui(self) -> None:
        """Prepare and play current macro in infinite loop until stopped."""
        self._start_playback_flow(self.app.play_infinite, "Running 1/∞ loops")

    def stop_playback_gui(self) -> None:
        """Stop playback, clean up hotkeys, and restore window state."""
        # Cancel a pending playback countdown
        if self._countdown_active and not self.app.player.playing:
            self._cancel_countdown()
            self.status_bar.showMessage("Playback cancelled")
            self._cleanup_after_playback()
            return
        if self.app.player.playing:
            self.app.stop_playback()
            self.status_bar.showMessage("Playback stopped")
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
            lambda: self.app.play_x_times(loops), f"Running 1/{loops} loops"
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
            self.status_bar.showMessage(f"Running {current}/∞ loops")
        else:
            self.status_bar.showMessage(f"Running {current}/{total} loops")

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
            self._note_recent_file(filename)
            self._update_window_title()
            self.status_bar.showMessage(f"Saved to {filename}")

    def load_macro(self) -> None:
        """Load a macro from a JSON file chosen by the user."""
        if not self._confirm_discard_unsaved("Open another macro and discard it?"):
            return
        last_dir = self._settings.value("lastDir", "", str)
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Macro", last_dir, "JSON files (*.json)"
        )
        if filename:
            self._load_macro_path(filename)

    def _load_macro_path(self, path: str) -> bool:
        """Read a macro file into the app. False means it could not be read."""
        try:
            self.app.recorder.load_macro(path)
        except (OSError, json.JSONDecodeError) as e:
            self.status_bar.showMessage(f"Failed to load macro: {e}")
            return False
        with self.app.recorder._events_lock:
            self.app.macro_data = self.app.recorder.events.copy()
        self._settings.setValue("lastDir", os.path.dirname(path))
        self.current_macro_path = path
        self.macro_dirty = False
        self._note_recent_file(path)
        self._update_window_title()
        self.status_bar.showMessage(f"Loaded {len(self.app.macro_data)} events")
        return True

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
        self._set_log_visible(not self.log_section.isVisible())

    def _sync_log_action(self, visible: bool) -> None:
        """Keep the toolbar/menu toggle in step with the pane's visibility."""
        self.toggle_log_action.blockSignals(True)
        self.toggle_log_action.setChecked(visible)
        self.toggle_log_action.blockSignals(False)

    def _set_log_visible(self, visible: bool, fit_window: bool = True) -> None:
        """Show or hide the log pane and resize the window to match it."""
        changed = self.log_section.isVisible() != visible
        if changed and not visible:
            # Remember the current height so re-showing the log restores it
            self._height_with_log = self.height()
        self.log_section.setVisible(visible)
        self._sync_log_action(visible)
        if changed and fit_window:
            self._fit_window_height(visible)

    def _fit_window_height(self, log_visible: bool) -> None:
        """Resize the window so no pane has to absorb leftover space."""
        if log_visible:
            target = self._height_with_log or (
                self.height() + self.LOG_PANE_DEFAULT_HEIGHT
            )
            self.resize(self.width(), max(self.height(), target))
        else:
            # Hiding a widget only invalidates the cached layout hints on the
            # next event loop pass, and until then the stale (taller) minimum
            # height blocks the shrink - so collapse one tick later.
            QTimer.singleShot(0, self._shrink_to_content_height)

    def _shrink_to_content_height(self) -> None:
        """Collapse the window down to the height its widgets actually need."""
        if self.log_section.isVisible():
            return  # re-shown before this fired; nothing to collapse
        # Drop every cached size hint below this window first: the nested
        # layouts still describe the pane we just hid, so sizeHint() would
        # come back with the old, taller height.
        for child_layout in self.findChildren(QLayout):
            child_layout.invalidate()
        self.splitter.refresh()  # the splitter caches its child sizes too
        central = self.centralWidget()
        for layout in (self.layout(), central.layout() if central else None):
            if layout is not None:
                layout.invalidate()
                layout.activate()
        # The window's minimum height is refreshed a tick later than the
        # hints, and while stale it would clamp the shrink away.
        self.setMinimumHeight(0)
        # Never grow while collapsing: honour a window the user shrank
        self.resize(self.width(), min(self.height(), self.sizeHint().height()))

    def clear_log(self) -> None:
        """Clear the log console"""
        self.log_model.clear_events()
        if not self.app.recorder.recording:
            self._log_append("Log Cleared - Ready for recording")

    def _log_append(self, message: str, category: str = "system") -> None:
        """Append a message to the log console.

        Args:
            message: String message to append (can be a status/system message)
            category: Colour category, "system" or "error"
        """
        self.log_model.append_system_message(message, category)
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
        self._set_log_visible(bool(checked))

    def _toggle_options_panel(self, checked: bool) -> None:
        """Show or hide the advanced options pane."""
        if hasattr(self, "options_group"):
            self.options_group.setVisible(bool(checked))
            # With the log hidden there is nothing to absorb the freed space,
            # so collapse the window back down to the remaining controls.
            if not self.log_section.isVisible():
                self._fit_window_height(False)

    def _confirm_discard_unsaved(self, question: str) -> bool:
        """Offer to save an unsaved macro. False means the user cancelled."""
        if not (self.macro_dirty and self.app.macro_data):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved macro")
        box.setText("The current macro has not been saved.")
        box.setInformativeText(question)
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        choice = box.exec()
        if choice == QMessageBox.StandardButton.Save.value:
            self.save_macro()
            # The file dialog may itself have been cancelled
            return not self.macro_dirty
        return choice == QMessageBox.StandardButton.Discard.value

    def _show_about(self) -> None:
        """Show the About panel; macOS shows it from the application menu."""
        # Imported here because the package __init__ imports this module, so a
        # module-level import would be circular.
        from . import __version__

        QMessageBox.about(
            self,
            "About Macro Recorder",
            f"<b>Macro Recorder {__version__}</b>"
            "<p>Records keyboard and mouse input and replays it on demand.</p>"
            "<p>Macros are saved as plain JSON, so they can be hand-edited "
            "or kept in version control.</p>",
        )

    def _show_help(self) -> None:
        """Display a small dialog with keyboard shortcuts."""
        QMessageBox.information(
            self,
            "Shortcuts",
            "F1 - Start Recording (backgrounds window)\n"
            "F2 - Stop Recording (restores window)\n"
            "F3 - Play Once\n"
            "F4 - Play Forever\n"
            "F5 - Stop Playback\n"
            "\n"
            "Ctrl/Cmd+O - Open Macro\n"
            "Ctrl/Cmd+S - Save Macro\n"
            "Ctrl/Cmd+L - Show or Hide Log",
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
            self._update_window_title("Recording")
            self.status_bar.showMessage("Recording started successfully")
            self._log_append("Recording Session Started Successfully")
            self._log_append("Monitoring mouse and keyboard events...")
            self._warn_if_keys_are_blocked()

            # Start the log update timer
            if not self.log_timer.isActive():
                self.log_timer.start(100)

        except Exception as e:
            # Recording failed - show error and clean up
            error_msg = f"Failed to start recording: {str(e)}"
            print(error_msg)
            self.status_bar.showMessage("Recording failed - Check permissions")

            self._log_append(f"Recording Failed: {str(e)}", "error")
            self._log_append("")
            self._log_append("Troubleshooting Tips:", "error")
            self._log_append(
                "• Go to System Settings → Privacy & Security → Accessibility"
            )
            self._log_append("• Add your Terminal or Python to the list")
            self._log_append("• Restart the application after granting permissions")
            self._log_append("• Check that macOS Secure Input is not switched on")

            # Reset button state
            self.toggle_log_action.blockSignals(True)
            self.toggle_log_action.setText("Show Log")
            self.toggle_log_action.setChecked(False)
            self.toggle_log_action.blockSignals(False)

            # If we hid the window earlier, restore it on failure
            if self.was_hidden_for_recording:
                self.was_hidden_for_recording = False
                self.show()

    @staticmethod
    def _dragged_macro_path(event: QDropEvent) -> str | None:
        """Return the dragged macro path, if the payload is exactly one .json."""
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return None
        paths = [
            url.toLocalFile()
            for url in mime.urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".json")
        ]
        return paths[0] if len(paths) == 1 else None

    def dragEnterEvent(self, a0: QDragEnterEvent | None) -> None:
        """Accept a single dragged .json macro, unless we are mid-run."""
        if a0 is None:
            return
        if self.action_load.isEnabled() and self._dragged_macro_path(a0) is not None:
            a0.acceptProposedAction()

    def dropEvent(self, a0: QDropEvent | None) -> None:
        """Load a macro dropped onto the window."""
        if a0 is None:
            return
        path = self._dragged_macro_path(a0)
        if path is None or not self.action_load.isEnabled():
            return
        a0.acceptProposedAction()
        if self._confirm_discard_unsaved("Open the dropped macro and discard it?"):
            self._load_macro_path(path)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Handle application exit by cleaning up subprocess and threads."""
        # Offer to keep an unsaved recording before anything is torn down
        if not self._confirm_discard_unsaved("Quit and discard it?"):
            if event is not None:
                event.ignore()
            return

        # Cancel any pending countdown and persist settings
        self._cancel_countdown()
        self._countdown_overlay.close()
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
