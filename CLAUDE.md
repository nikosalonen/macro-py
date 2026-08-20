# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

macro-py is a cross-platform macro recorder and player for macOS and Windows. It captures keyboard and mouse events using `pynput` and replays them on demand. The package provides both a PyQt6 GUI and a CLI mode that share the same core engine.

## Development Commands

### Setup
```bash
# Clone and install
uv python pin 3.12
uv sync

# Install dev dependencies
uv sync --extra dev
```

### Running the Application
```bash
# GUI mode (default)
uv run python -m macro_py

# CLI mode
uv run python -m macro_py --cli
```

### Code Quality
```bash
# Format code (Black, line length 88)
uv run black .

# Static analysis
uv run flake8
uv run mypy --strict

# Run tests
uv run pytest -v
```

## Architecture

### Core Components

The codebase is organized into separate, focused modules under `src/macro_py/`:

1. **MacroRecorder** (`MacroRecorder.py`) - Event capture engine
   - Uses `pynput` listeners for keyboard and mouse events
   - **macOS-specific**: Runs listeners in a subprocess to avoid CGEventTap conflicts with Qt
   - **Windows**: Uses in-process listeners
   - Events are stored as JSON-serializable dictionaries with timestamps
   - Subprocess communication via `multiprocessing.Queue` and consumer thread

2. **MacroPlayer** (`MacroPlayer.py`) - Event playback engine
   - Uses `pynput` controllers to replay mouse and keyboard events
   - Supports loop counts (including infinite) and playback speed adjustment
   - Parses button/key strings back to pynput objects

3. **MacroApp** (`MacroApp.py`) - CLI application coordinator
   - Bridges recorder and player with global hotkeys (F1-F5, Esc)
   - Manages application state and threading for playback
   - Provides save/load functionality

4. **GlobalHotkeys** (`MacHotkeys.py`) - native macOS global hotkeys
   - Wraps Carbon's `RegisterEventHotKey` through `ctypes` (PyObjC does not expose it)
   - **Why it exists**: a CGEventTap receives no key presses at all while macOS
     Secure Input is held, so the tap-based F2/F5 hotkeys stopped working
     whenever any app took that lock. Registered hotkeys are dispatched by the
     window server to the registering process instead of being observed from
     the event stream, and keep firing with Secure Input on - measured on a
     locked session where a tap saw 0 key events and this saw every press.
   - Registered keys are **consumed**, so the recorded app never sees them
     either; this replaces the hand-rolled `darwin_intercept` suppression
   - Bindings are deliberately short-lived (F2 only while recording, F5 only
     while playing) because a registration takes the key from every other app
   - Dispatch is keyed on `EventHotKeyID`; `_dispatch()` is split out so tests
     can drive it without a running application event target

5. **MacroGUI** (`MacroGUI.py`) - PyQt6 graphical interface
   - Primary GUI using PyQt6 with advanced logging and event display
   - Backgrounds its window during recording to avoid capturing UI interactions
   - Persists UI options (geometry, loops, speed, toggles) via QSettings

### Platform-Specific Behavior

**macOS Critical Detail**: The recorder spawns a separate subprocess (`_macro_listener_subprocess`) to run pynput listeners because CGEventTap and Qt event loops conflict. Events are queued back to the main process via `multiprocessing.Queue` and consumed by a dedicated thread.

**Windows**: Uses direct in-process listeners without subprocess isolation.

### Event Data Model

Events are dictionaries with these fields:
- `type`: Event type (`mouse_move`, `mouse_click`, `mouse_scroll`, `key_press`, `key_release`)
- `time`: Timestamp relative to recording start
- Type-specific fields: `x`, `y`, `button`, `pressed`, `key`, `dx`, `dy`
- Special internal events: `__stop_request__`, `__error__`, `__child_exit__`

### Hotkey Mappings

- F1: Start recording
- F2: Stop recording
- F3: Play once
- F4: Play forever
- F5: Stop playback
- Ctrl/Cmd+O / Ctrl/Cmd+S: Open / save macro (GUI)
- Ctrl/Cmd+L: Toggle the log pane (GUI)
- Ctrl+Shift+S / Ctrl+Shift+L: Save / load macro (CLI only)
- Esc: Exit (CLI only)

The GUI builds one `QAction` per command in `_create_actions()`, then shares
them between the menu bar (`_build_menubar()`, native on macOS) and the
toolbar (`_build_toolbar()`). `_refresh_action_states()` greys out whatever
does not apply to the current recording/playback state; it runs from
`_update_window_title()`, which every state transition already calls.

### GUI Conventions

- **No hardcoded colours.** Widgets take their look from the desktop palette so
  the window works in a light theme as well as a dark one. `EventLogDelegate`
  keeps two colour sets and picks one from the log view's `Base` lightness. The
  countdown overlay is the one deliberate exception: it is an always-dark HUD.
- **No stylesheets on native controls.** A stylesheet opts the widget out of
  native rendering; the toolbar and group boxes rely on the platform style
  instead. Toolbar glyphs are drawn in `_draw_glyph()` using the palette ink
  rather than `QStyle.standardIcon`, whose artwork looks dated.
- **Unsaved work goes through `_confirm_discard_unsaved()`** - close, load,
  drop, and re-record all funnel through it, so there is one Save/Discard/
  Cancel prompt to maintain.
- **The title uses Qt's `[*]` placeholder** plus `setWindowModified()` and
  `setWindowFilePath()`, which gives the macOS proxy icon and modified dot for
  free. Do not hand-append an asterisk.
- Recent files live in the `recentFiles` QSettings key as a JSON list, capped
  by `MacroGUI.MAX_RECENT_FILES`.

## Known Issues

- macOS requires Accessibility permissions (System Settings → Privacy & Security → Accessibility)
- **macOS Secure Input silently blocks key capture.** While any app holds Secure
  Event Input (a focused password field, a browser, a terminal with secure
  keyboard entry), event taps still receive mouse events and modifier keys but
  no key presses. Recording then looks like it works while every keystroke is
  dropped, and F2 never reaches the recorder, so stopping from the background
  appears broken. `MacroRecorder.secure_input_state()` detects this and names
  the offending app; the GUI warns in the log on recording start. The lock can
  also go **stale** - an app enables Secure Input then exits without releasing
  it, leaving the session stuck until the user logs out - which is why the
  state carries a `stale` flag and the advice differs. Since F2 is now a
  registered hotkey it still stops the recording under Secure Input - only the
  keystrokes themselves are lost - and `_warn_if_keys_are_blocked()` says which
  of the two applies. Note the CLI is still tap-based and has no equivalent:
  Carbon hotkeys need a Carbon/CF run loop, which its `while` sleep loop does
  not provide.
- **F2 and F5 are registered hotkeys on macOS** (`MacHotkeys.GlobalHotkeys`),
  not tap observations, so they survive Secure Input and are swallowed before
  the recorded app sees them. `_start_playback_hotkeys()` prefers the
  registration and only falls back to `_f5_hotkey_subprocess` if it fails, so
  that subprocess is normally dead code on macOS. The `darwin_intercept`
  suppression in `_macro_listener_subprocess` likewise only matters on the
  fallback path, since a registered F2 never reaches the tap. Windows has no
  equivalent: `suppress` there is all-or-nothing, so F2 still passes through.
- Windows may require Administrator privileges for hooks
- PyQt6 conflicts with CLI global shortcuts, so they're disabled when GUI is open

## File Persistence

Macros are saved/loaded as JSON arrays via `MacroRecorder.save_macro()` and `MacroRecorder.load_macro()`. Format is simple enough to version control or hand-edit if needed.
