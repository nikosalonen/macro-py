macro-py
========

Macro recorder and player for macOS and Windows. It captures keyboard and mouse events, then replays them on demand. The package ships with a PyQt6 GUI and a CLI mode that share the same core engine.

Requirements
------------

- Python 3.12+
- `pynput` for global keyboard and mouse hooks
- `PyQt6` for the default GUI

Install
-------

```
git clone https://github.com/niko-salonen/macro-py.git
cd macro-py
uv python pin 3.12
uv sync
```

Quick Start
-----------

- GUI mode (default): `uv run python -m macro_py`
- CLI mode: `uv run python -m macro_py --cli`

When the program starts it asks for accessibility permissions on macOS. Grant keyboard and mouse access under **System Settings → Privacy & Security → Accessibility** and restart the app.

Hotkeys
-------

- `F1` start recording
- `F2` stop recording
- `F3` play once
- `F4` play forever
- `F5` stop playback
- `Ctrl/Cmd+O` / `Ctrl/Cmd+S` open / save macro (GUI)
- `Ctrl/Cmd+L` show or hide the log (GUI)
- `Ctrl+Shift+S` / `Ctrl+Shift+L` save / load macro (CLI only)
- `Esc` exit CLI

Every command also lives in the menu bar (File, Record, Playback, View, Help);
the toolbar carries the transport controls and the log toggle. Commands that
do not apply right now are greyed out, so Play stays disabled until a macro
is loaded or recorded.

File Handling
-------------

- Drop a `.json` macro onto the window to load it.
- **File > Open Recent** keeps the last eight macros you opened or saved.
- Closing the window with an unrecorded macro offers Save, Discard, or Cancel;
  the same prompt appears before a new recording or another file replaces it.
- The title bar shows the open file and marks it as edited until you save.

If F2 Does Not Stop Recording
-----------------------------

In the GUI on macOS, F2 and F5 are registered with the window server rather
than read from an event tap, so they keep working even while an app holds
**Secure Input**. If F2 still does nothing, check Secure Input anyway - it is
what stops your *keystrokes* being recorded. While it is on, macOS withholds
key presses from every event tap, so mouse actions record and keystrokes
silently do not. No permission grants past this; it is the OS anti-keylogger
mechanism and there is no prompt for it. The log warns on recording start and
names the app responsible. Password managers, browsers with a focused password field,
and terminals with secure keyboard entry all trigger it. So do some background
helpers that hold the lock permanently rather than around a text field -
Logitech's `LogiPluginService` (part of Logi Options+) is one observed example,
and quitting it is not enough because launchd restarts it and it takes the lock
again; disable the feature in its own settings instead. To find the holder
yourself:

```bash
ioreg -l -d 1 -k IOConsoleUsers | grep -o 'kCGSSessionSecureInputPID"=[0-9]*'
```

A non-zero PID is the app to close or defocus. If that PID no longer exists,
the lock is **stale**: an app enabled Secure Input and exited without releasing
it, and the session stays stuck until you log out and back in. The log says
which of the two you are looking at.

The CLI has no registered-hotkey path, so there F2 works only while the
window has focus. In the GUI the Stop Rec button always works.

Recording Flow
--------------

1. Press `F1` to begin. The GUI hides itself so your actions are recorded cleanly.
2. Use your mouse and keyboard. Events appear in the log console if it is visible.
3. Press `F2` to stop. Events are stored in memory and ready for playback.

Playback Flow
-------------

1. Press `F3` to play the captured events one time.
2. Press `F4` (CLI) or use the GUI loop field to repeat the macro.
3. Press `F5` to stop playback.

Saving and Loading
------------------

- GUI buttons write JSON recordings through `macro_py.MacroRecorder.save_macro` and load them back into memory.
- CLI mode prints prompts to enter filenames for save/load.
- All recordings are simple JSON arrays, so they can be versioned with your project files.

Troubleshooting
---------------

- macOS: if recording fails, re-check Accessibility privileges for your terminal or Python interpreter.
- Windows: make sure the script runs as Administrator if hooks cannot be installed.
- PyQt6 conflicts with the CLI hotkeys, so global shortcuts are disabled while the GUI is open.

Development
-----------

- Install extras: `uv sync --extra dev`
- Code style: `uv run black .` (line length 88).
- Static checks: `uv run flake8` and `uv run mypy --strict`.
- Tests: `uv run pytest -v`
