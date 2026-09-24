"""Deterministic fault injection.

The brief's central claim about this environment is that the UI is stable and
the *runtime conditions* are what make replay hard. A public demo site cannot
produce a permission denial or a session expiry on demand, so the mock owns a
switchboard that does.

Every fault is armed explicitly and, by default, fires once and disarms. That
makes a test like "replay, hit a transient interstitial, recover, succeed"
reproducible rather than a matter of timing luck.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

FaultName = Literal[
    "validation_error",
    "permission_denied",
    "interstitial",
    "session_timeout",
    "slow_response",
    "server_error",
    "renamed_control",
]

ALL_FAULTS: tuple[FaultName, ...] = (
    "validation_error",
    "permission_denied",
    "interstitial",
    "session_timeout",
    "slow_response",
    "server_error",
    "renamed_control",
)

FAULT_DESCRIPTIONS: dict[str, str] = {
    "validation_error": "Search rejects the input with a field-level validation message.",
    "permission_denied": "The record exists but the operator is not entitled to view it.",
    "interstitial": "A message-of-the-day panel covers the page and must be dismissed.",
    "session_timeout": "The session has expired and the app bounces to the sign-in screen.",
    "slow_response": "The page takes several seconds to respond.",
    "server_error": "The application returns an unhandled error page.",
    "renamed_control": "A control's caption changes, simulating a version upgrade (UI drift).",
}


@dataclass
class _Fault:
    armed: bool = False
    sticky: bool = False
    remaining: int = 0


@dataclass
class FaultBoard:
    """Thread-safe registry of armed faults."""

    _faults: dict[str, _Fault] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        for name in ALL_FAULTS:
            self._faults.setdefault(name, _Fault())

    def arm(self, name: str, times: int = 1, sticky: bool = False) -> None:
        with self._lock:
            f = self._faults.setdefault(name, _Fault())
            f.armed = True
            f.sticky = sticky
            f.remaining = times if not sticky else 0

    def disarm(self, name: str) -> None:
        with self._lock:
            self._faults[name] = _Fault()

    def clear(self) -> None:
        with self._lock:
            for name in list(self._faults):
                self._faults[name] = _Fault()

    def peek(self, name: str) -> bool:
        """Is this fault armed, without consuming it?"""
        with self._lock:
            f = self._faults.get(name)
            return bool(f and f.armed)

    def consume(self, name: str) -> bool:
        """Check and, for non-sticky faults, use up one firing."""
        with self._lock:
            f = self._faults.get(name)
            if not f or not f.armed:
                return False
            if f.sticky:
                return True
            f.remaining -= 1
            if f.remaining <= 0:
                f.armed = False
            return True

    def state(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                name: {
                    "armed": f.armed,
                    "sticky": f.sticky,
                    "remaining": f.remaining,
                    "description": FAULT_DESCRIPTIONS.get(name, ""),
                }
                for name, f in self._faults.items()
            }


BOARD = FaultBoard()
