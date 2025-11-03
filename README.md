macro-py
========

Macro recorder and player for macOS and Windows. It captures keyboard and mouse events, then replays them on demand. The package ships with a PyQt5 GUI and a CLI mode that share the same core engine.

Requirements
------------

- Python 3.12+
- `pynput` for global keyboard and mouse hooks
- `PyQt5` for the default GUI

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
- `F4` play forever (CLI only)
- `F5` stop playback
- `Esc` exit CLI

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
- PyQt5 conflicts with the CLI hotkeys, so global shortcuts are disabled while the GUI is open.

Development
-----------

- Install extras: `uv sync --extra dev`
- Code style: `uv run black .` (line length 88).
- Static checks: `uv run flake8` and `uv run mypy --strict`.
- Tests (placeholder): `uv run pytest -v` (no tests yet, this command currently reports “no tests collected”).

Alternative GUIs
----------------

- `macro_py.MacroGUI_tkinter.MacroGUI` offers a Tkinter fallback if PyQt5 is not available.
- `macro_py.MacroGUI_pyqt5_backup.MacroGUI` keeps a lighter PyQt5 window without the advanced logging view.
