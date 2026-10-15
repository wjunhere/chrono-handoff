"""Effectors: the things that actually touch the physical world.

Kept behind a protocol from day one because "hardware instead of a human" is
not an edge case, it is the *preferred* answer whenever the window is narrower
than a human can hit (see docs/design.md section 6).  Making the human an
effector with a measurably worse jitter profile -- rather than the central
abstraction -- is what makes that upgrade path a one-class change instead of a
rewrite.

Implementations must be idempotent-friendly: a window sweeps the same action
repeatedly by design, so actions get an ``index`` and effectors that cannot be
repeated safely should say so via :meth:`Effector.repeatable`.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .clock import CLOCK


@dataclass
class EffectorCaps:
    """What this effector can be trusted with (all jitter figures are 1-sigma
    order-of-magnitude hints, not promises)."""

    jitter_ms: float
    repeatable: bool           #: may the same action be swept in a window?
    needs_human: bool
    description: str = ""


class Effector(Protocol):
    name: str

    def caps(self) -> EffectorCaps: ...

    def arm(self, action: dict[str, Any]) -> None:
        """Prepare before the deadline (open the port, preload the payload).
        Called outside any timing-critical path."""
        ...

    def fire(self, action: dict[str, Any], index: int) -> str:
        """Execute.  MUST be fast and non-blocking; returns a short detail
        string for the journal."""
        ...

    def close(self) -> None: ...


class LogEffector:
    """No physical effect, full journal trace.

    This is the M1 default: it proves the scheduling machinery works without
    pretending we already drive a board.  Also useful as a dry-run gate
    (``dry_run=True`` in the real system).
    """

    name = "log"

    def __init__(self) -> None:
        self.fired: list[tuple[float, int, dict[str, Any]]] = []

    def caps(self) -> EffectorCaps:
        return EffectorCaps(jitter_ms=0.01, repeatable=True, needs_human=False, description="journal only")

    def arm(self, action: dict[str, Any]) -> None:
        return

    def fire(self, action: dict[str, Any], index: int) -> str:
        self.fired.append((CLOCK(), index, dict(action)))
        return f"log:{action.get('type', 'noop')}#{index}"

    def close(self) -> None:
        return


class SerialEffector:
    """Writes bytes to a serial port.  The real case-study effector.

    Port is opened in :meth:`arm` so :meth:`fire` does no I/O setup.  ``write``
    on a small payload is effectively non-blocking; buffering behaviour is the
    driver's, which is why the window sweep exists at all.
    """

    name = "serial"

    def __init__(self, port: str, baud: int = 115200) -> None:
        self._port = port
        self._baud = baud
        self._ser: Any = None

    def caps(self) -> EffectorCaps:
        return EffectorCaps(jitter_ms=0.5, repeatable=True, needs_human=False, description=f"serial {self._port}")

    def arm(self, action: dict[str, Any]) -> None:
        if self._ser is None:
            import serial  # noqa: PLC0415

            self._ser = serial.Serial(self._port, self._baud, timeout=0)

    def fire(self, action: dict[str, Any], index: int) -> str:
        data = str(action.get("data", "")).encode("utf-8")
        if self._ser is None:
            raise RuntimeError("serial effector not armed")
        self._ser.write(data)
        self._ser.flush()
        return f"serial:{self._port}<-{data!r}#{index}"

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None


class HttpEffector:
    """POST a JSON payload to a local/remote endpoint.

    Non-blocking is a lie here (a socket write can stall), so the conductor
    treats network effectors as best-effort: they are fired from the scheduler
    thread only when the plan marks them urgent, otherwise they are dispatched
    to a worker.  Kept simple for M1: fire-and-forget with a 0.5 s timeout.
    """

    name = "http"

    def __init__(self, timeout_s: float = 0.5) -> None:
        self._timeout = timeout_s

    def caps(self) -> EffectorCaps:
        return EffectorCaps(jitter_ms=5.0, repeatable=True, needs_human=False, description="http post")

    def arm(self, action: dict[str, Any]) -> None:
        return

    def fire(self, action: dict[str, Any], index: int) -> str:
        url = str(action.get("url", ""))
        body = json.dumps({"index": index, **{k: v for k, v in action.items() if k != "url"}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=self._timeout).close()
            return f"http:{url}#{index}"
        except Exception as exc:
            return f"http:{url}#{index} FAILED {type(exc).__name__}"

    def close(self) -> None:
        return


class HumanEffector:
    """The human: driven by the cue channel, observed by an input source.

    It cannot be fired, only *entrained* -- which is why the entire cue channel
    exists.  ``caps().repeatable`` is False: replaying "the previous human
    action" means asking someone to press RESET again, which is a different
    physical act with different consequences.  This is the one place where the
    Two-Rate "keep tracking the previous plan" rule must NOT be inherited.
    """

    name = "human"

    def caps(self) -> EffectorCaps:
        return EffectorCaps(
            jitter_ms=50.0,
            repeatable=False,
            needs_human=True,
            description="entrained by cue; offset is calibrated, not commanded",
        )

    def arm(self, action: dict[str, Any]) -> None:
        return

    def fire(self, action: dict[str, Any], index: int) -> str:
        return "human:cannot-be-fired"

    def close(self) -> None:
        return


EFFECTORS: dict[str, type] = {
    "log": LogEffector,
    "serial": SerialEffector,
    "http": HttpEffector,
    "human": HumanEffector,
}


def make_effector(kind: str, **kw: Any) -> Effector:
    cls = EFFECTORS.get(kind)
    if cls is None:
        raise ValueError(f"unknown effector {kind!r}; known: {sorted(EFFECTORS)}")
    return cls(**kw)  # type: ignore[return-value]
