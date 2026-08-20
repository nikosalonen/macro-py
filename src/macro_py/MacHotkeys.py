"""Native macOS global hotkeys via Carbon's RegisterEventHotKey.

pynput listens with a CGEventTap, and a tap receives no key presses at all
while macOS Secure Input is held - so a tap-based stop hotkey stops working,
silently, whenever any app takes that lock. On a managed Mac a login daemon
can hold it permanently, which makes the tap the wrong mechanism for a
control hotkey however many permissions the app has been granted.

Registered hotkeys are dispatched by the window server directly to the
registering process instead of being observed from the event stream, so they
keep firing with Secure Input on - verified on a locked session where a tap
saw 0 key events and this saw every press. They are also consumed rather
than observed, so the focused app never sees the key either, which is what
the hand-rolled darwin_intercept suppression was reaching for.

Carbon is reached through ctypes because PyObjC does not wrap
RegisterEventHotKey. Registration only succeeds for the current login
session, and a released EventHotKeyRef unregisters its hotkey, so the refs
are held for as long as the binding should live.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from typing import Callable

# macOS virtual keycodes for the function row. These are physical key
# positions, not characters, so they do not vary by keyboard layout.
KEYCODES = {
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
}

_EVENT_CLASS_KEYBOARD = 0x6B657962  # 'keyb'
_EVENT_HOTKEY_PRESSED = 5
_PARAM_DIRECT_OBJECT = 0x2D2D2D2D  # '----'
_TYPE_EVENT_HOTKEY_ID = 0x686B6964  # 'hkid'
_SIGNATURE = 0x4D43524F  # 'MCRO', identifies our registrations


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)


def supported() -> bool:
    """True when this platform has Carbon hotkeys (macOS only)."""
    return sys.platform == "darwin"


def _load_carbon() -> ctypes.CDLL | None:
    """Load Carbon with explicit prototypes, or None if unavailable.

    argtypes matter here: without them ctypes narrows pointer arguments to
    32 bits and the calls corrupt the event target.
    """
    path = ctypes.util.find_library("Carbon")
    if path is None:
        return None
    try:
        carbon = ctypes.CDLL(path)
    except OSError:
        return None

    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.GetApplicationEventTarget.argtypes = []
    carbon.InstallEventHandler.restype = ctypes.c_int32
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p,
        _HANDLER,
        ctypes.c_uint32,
        ctypes.POINTER(_EventTypeSpec),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    carbon.RegisterEventHotKey.restype = ctypes.c_int32
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        _EventHotKeyID,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    carbon.UnregisterEventHotKey.restype = ctypes.c_int32
    carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    carbon.GetEventParameter.restype = ctypes.c_int32
    carbon.GetEventParameter.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    return carbon


class GlobalHotkeys:
    """Global hotkeys that survive macOS Secure Input.

    Callbacks run on the thread draining the event loop - the Qt main thread
    in this app - so they may touch widgets directly. Registrations are
    deliberately short-lived: a registered key is swallowed system-wide, so
    binding F2 for the whole session would take it away from every other app.
    """

    def __init__(self) -> None:
        self._carbon = _load_carbon() if supported() else None
        self._handler: object | None = None
        self._installed = False
        self._refs: dict[str, ctypes.c_void_p] = {}
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._ids: dict[str, int] = {}
        self._next_id = 1

    @property
    def available(self) -> bool:
        """True when hotkeys can actually be registered on this system."""
        return self._carbon is not None

    def register(self, key: str, callback: Callable[[], None]) -> bool:
        """Bind a function-row key by name ("f2"), replacing any prior bind.

        Returns False rather than raising when the platform, the key name, or
        the OS declines, so callers can fall back to a listener.
        """
        carbon = self._carbon
        if carbon is None:
            return False
        keycode = KEYCODES.get(key)
        if keycode is None:
            logging.warning("No macOS keycode known for hotkey %r", key)
            return False
        if not self._install_handler():
            return False

        self.unregister(key)
        hotkey_id = self._next_id
        self._next_id += 1
        ref = ctypes.c_void_p()
        try:
            err = carbon.RegisterEventHotKey(
                keycode,
                0,  # no modifiers; the bare function key
                _EventHotKeyID(_SIGNATURE, hotkey_id),
                carbon.GetApplicationEventTarget(),
                0,
                ctypes.byref(ref),
            )
        except Exception:
            logging.exception("RegisterEventHotKey raised for %s", key)
            return False
        if err != 0 or not ref:
            logging.warning("RegisterEventHotKey failed for %s (err %s)", key, err)
            return False

        # Hold the ref: releasing it unregisters the hotkey.
        self._refs[key] = ref
        self._ids[key] = hotkey_id
        self._callbacks[hotkey_id] = callback
        logging.debug("Registered global hotkey %s", key.upper())
        return True

    def unregister(self, key: str) -> None:
        """Release one binding, giving the key back to the rest of the system."""
        carbon = self._carbon
        ref = self._refs.pop(key, None)
        hotkey_id = self._ids.pop(key, None)
        if hotkey_id is not None:
            self._callbacks.pop(hotkey_id, None)
        if carbon is None or ref is None:
            return
        try:
            carbon.UnregisterEventHotKey(ref)
        except Exception:
            logging.exception("UnregisterEventHotKey raised for %s", key)

    def is_registered(self, key: str) -> bool:
        """True while this key has a live binding."""
        return key in self._refs

    def unregister_all(self) -> None:
        """Release every binding. Safe to call more than once."""
        for key in list(self._refs):
            self.unregister(key)

    def _install_handler(self) -> bool:
        """Install the shared Carbon handler once, keeping it alive."""
        carbon = self._carbon
        if carbon is None:
            return False
        if self._installed:
            return True
        callback = _HANDLER(self._on_hotkey)
        spec = _EventTypeSpec(_EVENT_CLASS_KEYBOARD, _EVENT_HOTKEY_PRESSED)
        try:
            err = carbon.InstallEventHandler(
                carbon.GetApplicationEventTarget(),
                callback,
                1,
                ctypes.byref(spec),
                None,
                None,
            )
        except Exception:
            logging.exception("InstallEventHandler raised")
            return False
        if err != 0:
            logging.warning("InstallEventHandler failed (err %s)", err)
            return False
        # Keep the trampoline alive; if it is collected the OS calls freed code.
        self._handler = callback
        self._installed = True
        return True

    def _on_hotkey(self, next_handler: object, event: object, user_data: object) -> int:
        """Carbon entry point. Never let an exception cross back into C."""
        try:
            self._dispatch(self._hotkey_id_of(event))
        except Exception:
            logging.exception("Error handling global hotkey")
        return 0  # noErr - we consumed it

    def _hotkey_id_of(self, event: object) -> int | None:
        """Read the EventHotKeyID out of a Carbon event."""
        carbon = self._carbon
        if carbon is None:
            return None
        got = _EventHotKeyID()
        err = carbon.GetEventParameter(
            event,
            _PARAM_DIRECT_OBJECT,
            _TYPE_EVENT_HOTKEY_ID,
            None,
            ctypes.sizeof(got),
            None,
            ctypes.byref(got),
        )
        if err != 0:
            logging.warning("GetEventParameter failed for hotkey (err %s)", err)
            return None
        return int(got.id)

    def _dispatch(self, hotkey_id: int | None) -> None:
        """Run the callback bound to an id. Split out so tests can drive it."""
        if hotkey_id is None:
            return
        callback = self._callbacks.get(hotkey_id)
        if callback is None:
            # A key we unregistered can still deliver one in-flight press.
            logging.debug("Ignoring hotkey id %s with no callback", hotkey_id)
            return
        callback()
