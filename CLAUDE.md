# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

macro-py is a cross-platform macro recorder and player for macOS and Windows. It captures keyboard and mouse events using `pynput` and replays them on demand. The package provides both a PyQt5 GUI and a CLI mode that share the same core engine.

## Development Commands

### Setup
```bash
# Clone and install
uv python pin 3.14.4
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
uv run mypy

# Run tests
uv run pytest -v
# Note: Test suite is currently a placeholder with no tests implemented
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

4. **MacroGUI** (`MacroGUI.py`) - PyQt5 graphical interface
   - Primary GUI using PyQt5 with advanced logging and event display
   - Hides window during recording to avoid capturing UI interactions
   - Alternative implementations available:
     - `MacroGUI_tkinter.py` - Tkinter fallback if PyQt5 unavailable
     - `MacroGUI_pyqt5_backup.py` - Lighter PyQt5 version without logging

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
- F4: Play forever (CLI only)
- F5: Stop playback
- Esc: Exit (CLI only)

## Known Issues

- No test suite currently implemented (pytest reports "no tests collected")
- macOS requires Accessibility permissions (System Settings → Privacy & Security → Accessibility)
- Windows may require Administrator privileges for hooks
- PyQt5 conflicts with CLI global shortcuts, so they're disabled when GUI is open

## File Persistence

Macros are saved/loaded as JSON arrays via `MacroRecorder.save_macro()` and `MacroRecorder.load_macro()`. Format is simple enough to version control or hand-edit if needed.

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
