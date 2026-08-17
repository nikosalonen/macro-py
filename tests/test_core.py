"""Tests for core recording/playback logic (non-GUI)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pynput import keyboard

from macro_py.MacroPlayer import MacroPlayer
from macro_py.MacroRecorder import (
    MacroRecorder,
    compress_mouse_moves,
    trim_leading_idle,
)

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
