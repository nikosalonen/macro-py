"""Tests for core recording/playback logic (non-GUI)."""

from __future__ import annotations

import time
from importlib import import_module
from typing import Any

import pytest
from pynput import keyboard

from macro_py.MacroPlayer import MacroPlayer
from macro_py.MacroRecorder import (
    MacroRecorder,
    _pid_is_alive,
    _process_name,
    _secure_input_pid,
    compress_mouse_moves,
    secure_input_state,
    trim_leading_idle,
)

# The package __init__ rebinds the name "MacroRecorder" to the class, so the
# module itself has to be fetched explicitly for monkeypatching.
recorder_module = import_module("macro_py.MacroRecorder")

Event = dict[str, Any]


def move(x: float, y: float, t: float) -> Event:
    return {"type": "mouse_move", "x": x, "y": y, "time": t}


def click(x: float, y: float, t: float, pressed: bool = True) -> Event:
    return {
        "type": "mouse_click",
        "x": x,
        "y": y,
        "button": "Button.left",
        "pressed": pressed,
        "time": t,
    }


class TestCompressMouseMoves:
    def test_drops_tiny_close_moves(self) -> None:
        events = [move(0, 0, 0.0), move(1, 0, 0.01), move(2, 0, 0.02)]
        result = compress_mouse_moves(events, min_distance=3.0, min_interval=0.05)
        # First kept, middle dropped, last promoted as trailing pending move
        assert result == [events[0], events[2]]

    def test_keeps_distant_moves(self) -> None:
        events = [move(0, 0, 0.0), move(100, 100, 0.01)]
        assert compress_mouse_moves(events) == events

    def test_keeps_slow_moves(self) -> None:
        events = [move(0, 0, 0.0), move(1, 0, 1.0)]
        assert compress_mouse_moves(events) == events

    def test_keeps_move_before_click(self) -> None:
        # The dropped move right before a click must be promoted so the
        # click happens at the recorded cursor position.
        events = [
            move(0, 0, 0.0),
            move(1, 1, 0.01),
            click(1, 1, 0.02),
        ]
        result = compress_mouse_moves(events)
        assert result == events

    def test_non_move_events_untouched(self) -> None:
        events = [
            {"type": "key_press", "key": "a", "time": 0.0},
            {"type": "__stop_request__", "time": 1.0},
        ]
        assert compress_mouse_moves(events) == events


class TestTrimLeadingIdle:
    def test_shifts_times_to_zero(self) -> None:
        events = [move(0, 0, 5.0), click(0, 0, 6.5)]
        result = trim_leading_idle(events)
        assert result[0]["time"] == 0.0
        assert result[1]["time"] == pytest.approx(1.5)

    def test_no_shift_when_first_event_at_zero(self) -> None:
        events = [move(0, 0, 0.0), move(1, 1, 1.0)]
        assert trim_leading_idle(events) == events

    def test_stop_request_shifted_but_not_used_as_offset(self) -> None:
        events = [
            {"type": "__stop_request__", "time": 10.0},
            move(0, 0, 4.0),
        ]
        result = trim_leading_idle(events)
        assert result[0]["time"] == pytest.approx(6.0)
        assert result[1]["time"] == 0.0

    def test_empty_events(self) -> None:
        assert trim_leading_idle([]) == []


class TestRecorderStopHotkey:
    def test_f2_release_not_recorded(self) -> None:
        recorder = MacroRecorder()
        recorder.recording = True
        recorder.start_time = time.time()
        recorder.on_key_release(keyboard.Key.f2)
        assert recorder.events == []

    def test_f2_press_becomes_stop_request(self) -> None:
        recorder = MacroRecorder()
        recorder.recording = True
        recorder.start_time = time.time()
        recorder.on_key_press(keyboard.Key.f2)
        assert [e["type"] for e in recorder.events] == ["__stop_request__"]

    def test_other_key_release_recorded(self) -> None:
        recorder = MacroRecorder()
        recorder.recording = True
        recorder.start_time = time.time()
        recorder.on_key_release(keyboard.Key.f6)
        assert [e["type"] for e in recorder.events] == ["key_release"]


class TestPlayerTiming:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: list[Event],
        **kwargs: Any,
    ) -> tuple[list[float], list[Event]]:
        player = MacroPlayer()
        sleeps: list[float] = []
        executed: list[Event] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(player, "execute_event", executed.append)
        player.play_macro(events, **kwargs)
        return sleeps, executed

    def test_speed_scales_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = [move(0, 0, 0.0), move(1, 1, 1.0)]
        sleeps, executed = self._run(monkeypatch, events, speed=2.0)
        assert sleeps == [pytest.approx(0.5)]
        assert len(executed) == 2

    def test_max_idle_caps_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = [move(0, 0, 0.0), move(1, 1, 10.0)]
        sleeps, _ = self._run(monkeypatch, events, max_idle_time=0.5)
        assert sleeps == [pytest.approx(0.5)]

    def test_stop_request_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events = [move(0, 0, 0.0), {"type": "__stop_request__", "time": 1.0}]
        _, executed = self._run(monkeypatch, events)
        assert executed == [events[0]]


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_secure_input_state_is_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder_module.sys, "platform", "win32")
    assert secure_input_state() is None


def test_secure_input_pid_reads_the_session_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ioreg = '"kCGSSessionSecureInputPID"=1338,"kCGSSessionOnConsoleKey"=Yes'
    monkeypatch.setattr(
        recorder_module.subprocess, "run", lambda cmd, **kw: _Result(ioreg)
    )
    assert _secure_input_pid() == 1338


def test_secure_input_pid_treats_zero_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recorder_module.subprocess,
        "run",
        lambda cmd, **kw: _Result('"kCGSSessionSecureInputPID"=0'),
    )
    assert _secure_input_pid() is None


def test_secure_input_pid_survives_a_missing_ioreg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(cmd: list[str], **kwargs: Any) -> _Result:
        raise OSError("ioreg not found")

    monkeypatch.setattr(recorder_module.subprocess, "run", boom)
    assert _secure_input_pid() is None


def test_process_name_is_the_executable_basename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recorder_module.subprocess,
        "run",
        lambda cmd, **kw: _Result("/Applications/Dia.app/Contents/MacOS/Dia\n"),
    )
    assert _process_name(1338) == "Dia"


def test_pid_is_alive_reports_a_dead_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(recorder_module.os, "kill", gone)
    assert _pid_is_alive(1338) is False


def test_pid_is_alive_counts_a_foreign_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_ours(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(recorder_module.os, "kill", not_ours)
    assert _pid_is_alive(1) is True
