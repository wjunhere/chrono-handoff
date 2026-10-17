"""Input sources: how the conductor learns that the human actually acted.

**Timestamp provenance is a first-class concern here.**  There are two very
different instants for any physical action:

* ``t_payload`` -- when the *mechanism* registered the event (the serial
  driver saw the byte, the OS delivered the keystroke, the MCU took the GPIO
  edge).  This is inside the host clock domain, so it is comparable with the
  cue grid.
* ``t_host`` -- when a *human* read it and plugged it into a form ("I pressed
  the button").  This carries 150-400 ms of human reaction time and must
  never be used for timing analysis; it is only a coarse fallback.

Each observation reports both, and :func:`analyse_offsets` refuses to blend
them silently.  For M1 the payload timestamp is simply
``key_event_time - mean_key_latency``, where ``mean_key_latency`` is the
operating-system input stack's known lag (:data:`DEFAULT_KEY_LATENCY_S`).
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .local_http import LocalHTTPServer
from typing import Any, Callable, Iterable, Protocol

from .clock import CLOCK
from .model import Event, median

#: Typical Windows console input-stack lag (hardware interrupt -> buffer ->
#: msvcrt returns).  Refine it with ``chrono calibrate-input``; it shifts all
#: offsets by a constant, so a wrong value biases the calibration rather than
#: adding noise.
DEFAULT_KEY_LATENCY_S = 0.015


@dataclass
class Observation:
    """One raw stimulus from the world."""

    t: float                      #: mechanism timestamp, monotonic host clock
    source: str = ""
    payload: str = ""             #: the text that decided the action ("1", "reset")
    raw: str = ""
    #: Set when the source could not timestamp the event itself and the value
    #: came from a human-form entry.  Such observations are excluded from
    #: timing statistics by default.
    human_latency: float | None = None
    #: Measurement-path latency to subtract, in seconds.  ``None`` means "use
    #: the conductor's default" (correct for console keystrokes, which pass
    #: through the OS input stack).  A browser timestamp captured in the click
    #: handler has no such stack in the path, so it must pass ``0.0`` -- the
    #: correction is a property of the *measurement path*, not of the human.
    latency_s: float | None = None

    @property
    def timing_usable(self) -> bool:
        return self.human_latency is None


class InputSource(Protocol):
    name: str

    def open(self) -> None: ...
    def poll(self) -> list[Observation]: ...
    def close(self) -> None: ...
    def describe(self) -> str: ...


# --------------------------------------------------------------------------
# console / keyboard
# --------------------------------------------------------------------------


class ConsoleKeySource:
    """Keystroke capture with a *payload-level* timestamp.

    On Windows ``msvcrt.kbhit()`` + ``getwch()`` gives the key at the moment
    the input buffer delivers it, and both steps happen on the calling thread,
    so the stamp is as close to the mechanism as Python can get.  No wheel
    event queue is involved.

    Requires a real console.  If the daemon has none (detached, or stdout is a
    pipe), this source reports ``available() == False`` and the caller should
    fall back to :class:`StdinLineSource`.
    """

    name = "console_key"

    def __init__(self, keys: str = "\r\n ", echo: bool = True) -> None:
        self._keys = set(keys)
        self._echo = echo
        self._msvcrt: Any = None
        self._buf: list[Observation] = []
        self._lock = threading.Lock()

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import msvcrt  # noqa: PLC0415

            if not hasattr(msvcrt, "kbhit"):
                return False
            # A console must actually exist, otherwise kbhit() can never fire.
            import ctypes  # noqa: PLC0415

            return bool(ctypes.windll.kernel32.GetConsoleWindow())
        except Exception:
            return False

    def open(self) -> None:
        import msvcrt  # noqa: PLC0415

        self._msvcrt = msvcrt

    def poll(self) -> list[Observation]:
        if self._msvcrt is None:
            return []
        out: list[Observation] = []
        while self._msvcrt.kbhit():
            ch = self._msvcrt.getwch()          # <-- payload timestamp
            t = CLOCK()
            key = "space" if ch == " " else ch
            if ch in self._keys:
                out.append(Observation(t=t, source=self.name, payload=key, raw=repr(ch)))
                if self._echo:
                    try:
                        sys.stderr.write(f"\r  [key {key!r} @ t+{t:.3f}]        \n")
                        sys.stderr.flush()
                    except Exception:
                        pass
        return out

    def close(self) -> None:
        return

    def describe(self) -> str:
        return f"console keystrokes ({sorted(self._keys)})"


class StdinLineSource:
    """Line-based fallback: press Enter on a piped/redirected stdin.

    Timestamping is *line delivery* level, which is both coarser and laggier
    than a keystroke; recorded as such so the analysis can flag it.
    """

    name = "stdin_line"

    def __init__(self, echo: bool = True) -> None:
        self._q: queue.SimpleQueue[Observation] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._echo = echo

    def open(self) -> None:
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="chrono-stdin", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        stream = sys.stdin
        if stream is None:
            return
        while not self._stop:
            try:
                line = stream.readline()
            except Exception:
                return
            if line == "":
                return
            t = CLOCK()
            text = line.strip()
            self._q.put(Observation(t=t, source=self.name, payload=text or "enter", raw=line))

    def poll(self) -> list[Observation]:
        out: list[Observation] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self._stop = True

    def describe(self) -> str:
        return "stdin lines (press Enter)"


# --------------------------------------------------------------------------
# HTTP (for a phone / browser UI, or a future MCP Apps web panel)
# --------------------------------------------------------------------------


class HttpSource:
    """Tiny local HTTP endpoint: ``POST /signal?payload=x``.

    The server thread stamps the request on arrival, so the timestamp is
    server-side receive time -- good enough for a *modern* browser posting
    from a click handler (network RTT is <1 ms on loopback) and vastly better
    than a human re-typing what they did.
    """

    name = "http"

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._q: queue.SimpleQueue[Observation] = queue.SimpleQueue()
        self.bound_port: int = 0

    def open(self) -> None:
        q = self._q

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # Signature must match BaseHTTPRequestHandler.log_message(self,
            # format, *args) -- a bare ``*_a`` override is not compatible with it.
            # Silence the default per-request stderr spam.
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _respond(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _signal(self, payload: str, raw: bytes = b"") -> None:
                t = CLOCK()                    # stamp first, parse second
                q.put(Observation(t=t, source="http", payload=payload or "signal", raw=raw.decode("utf-8", "replace")))
                self._respond(200, b'{"ok":true}')

            @staticmethod
            def _payload_from(path: str, raw: bytes) -> str:
                payload = ""
                qs = path.split("?", 1)[1] if "?" in path else ""
                for part in qs.split("&"):
                    if part.startswith("payload="):
                        from urllib.parse import unquote_plus  # noqa: PLC0415

                        payload = unquote_plus(part[len("payload=") :])
                if not payload and raw:
                    try:
                        payload = str(json.loads(raw.decode("utf-8")).get("payload", ""))
                    except Exception:
                        payload = raw.decode("utf-8", "replace").strip()
                return payload

            def do_GET(self) -> None:  # noqa: N802
                # GET /signal is deliberate: it makes the human's press reachable
                # from a phone by tapping a link or bookmark, with no JavaScript
                # and no page load. That is the cheapest possible "physical act
                # inside the host clock domain" -- the click handler on the
                # device stamps the request, not a human re-typing what they did.
                if self.path.startswith("/signal"):
                    self._signal(self._payload_from(self.path, b""))
                elif self.path.startswith("/health"):
                    self._respond(200, b'{"ok":true}')
                else:
                    self._respond(404, b'{"error":"not found"}')

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.path.startswith("/signal"):
                    self._signal(self._payload_from(self.path, raw), raw)
                else:
                    self._respond(404, b'{"error":"not found"}')

        self._server = LocalHTTPServer((self._host, self._port), _Handler)
        self._server.daemon_threads = True
        self.bound_port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="chrono-http", daemon=True)
        self._thread.start()

    def poll(self) -> list[Observation]:
        out: list[Observation] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def describe(self) -> str:
        return f"http://{self._host}:{self.bound_port}/signal?payload=<x>"


# --------------------------------------------------------------------------
# serial (used by the real case study; kept minimal for M1)
# --------------------------------------------------------------------------


class SerialSource:
    """Non-blocking serial reader with a regex/substring trigger.

    Deliberately opened and reading *before* any action that could produce the
    signal (see the case study: the port must already be listening when the
    board resets).  Reads on the caller's thread -- this is only ever polled
    from the conductor's non-critical loop, never from a beat callback.
    """

    name = "serial"

    def __init__(self, port: str, baud: int = 115200, pattern: str = "", timeout_s: float = 0.0) -> None:
        self._port = port
        self._baud = baud
        self._pattern = re.compile(pattern) if pattern else None
        self._timeout = timeout_s
        self._ser: Any = None
        self._acc = bytearray()

    def open(self) -> None:
        import serial  # noqa: PLC0415  -- optional dependency

        self._ser = serial.Serial(self._port, self._baud, timeout=self._timeout)

    def poll(self) -> list[Observation]:
        if self._ser is None:
            return []
        out: list[Observation] = []
        pending = self._ser.in_waiting
        data = self._ser.read(pending or 1)
        if data:
            t = CLOCK()
            self._acc += data
            text = bytes(self._acc).decode("utf-8", "replace")
            if self._pattern is not None:
                m = self._pattern.search(text)
                if m:
                    out.append(Observation(t=t, source=f"serial:{self._port}", payload=m.group(0), raw=text[-200:]))
                    self._acc.clear()
            else:
                out.append(Observation(t=t, source=f"serial:{self._port}", payload=text.strip(), raw=text[-200:]))
                self._acc.clear()
        return out

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()

    def describe(self) -> str:
        return f"serial {self._port}@{self._baud} pattern={self._pattern.pattern if self._pattern else '-'}"


# --------------------------------------------------------------------------
# synthetic (for self-tests and for CI)
# --------------------------------------------------------------------------


class SimSource:
    """Fires a synthetic observation at a planned offset from each cue origin.

    Used by ``chrono selftest`` to exercise the *entire* pipeline (grid ->
    offset -> statistics -> journal) without a human.  Offsets can be drawn
    from a fixed list or a distribution so the calibration maths is tested
    against a known ground truth.
    """

    name = "sim"

    def __init__(self, offsets_ms: Iterable[float] | None = None, jitter_ms: float = 0.0, seed: int = 0) -> None:
        self._offsets = list(offsets_ms) if offsets_ms is not None else None
        self._jitter = jitter_ms
        self._rng = __import__("random").Random(seed)
        self._i = 0
        self._armed: Callable[[], float] | None = None
        self._pending: list[tuple[float, str]] = []

    def arm(self, zero_at: float) -> None:
        """Schedule the next synthetic press relative to the ZERO beat.

        Named for the human-visible anchor on purpose: the synthetic subject
        is told "press when you hear GO", so its offset is measured from GO,
        exactly like a real one.  Offsets are therefore directly comparable
        with a real session's.
        """
        off = self._next_offset()
        self._pending.append((zero_at + off, "space"))

    def _next_offset(self) -> float:
        if self._offsets:
            v = self._offsets[self._i % len(self._offsets)]
            self._i += 1
            return v / 1000.0
        return self._rng.gauss(-0.03, 0.05)

    def open(self) -> None:
        return

    def poll(self) -> list[Observation]:
        out: list[Observation] = []
        keep: list[tuple[float, str]] = []
        for at, payload in self._pending:
            if CLOCK() >= at:
                out.append(Observation(t=at, source=self.name, payload=payload, raw="sim"))
            else:
                keep.append((at, payload))
        self._pending = keep
        return out

    def close(self) -> None:
        return

    def describe(self) -> str:
        return f"synthetic ({self._offsets or 'gauss(-30ms,50ms)'})"


class NullSource:
    name = "none"

    def open(self) -> None:
        return

    def poll(self) -> list[Observation]:
        return []

    def close(self) -> None:
        return

    def describe(self) -> str:
        return "no input source"


SOURCES: dict[str, Callable[..., InputSource]] = {
    "console_key": ConsoleKeySource,
    "stdin_line": StdinLineSource,
    "http": HttpSource,
    "serial": SerialSource,
    "sim": SimSource,
    "none": NullSource,
}


def make_source(kind: str, **kw: Any) -> InputSource:
    fn = SOURCES.get(kind)
    if fn is None:
        raise ValueError(f"unknown input source {kind!r}; known: {sorted(SOURCES)}")
    return fn(**kw)  # type: ignore[return-value]


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def analyse_offsets(
    events: list[Event],
    key_latency_s: float = DEFAULT_KEY_LATENCY_S,
    min_offset_ms: float = -300.0,
) -> dict[str, Any]:
    """Turn raw ``human_signal`` events into signed offsets.

    ``offset = (payload_time - key_latency) - cue_zero``

    Conventions:
      * positive -> the human fired **late**
      * negative -> the human fired **early**

    ``min_offset_ms`` discards the physically implausible "early" tail: those
    are almost always a stray keystroke from the previous trial, not a human
    who pressed 900 ms before the beat.
    """
    zero = next((e.t for e in events if e.kind == "cue_zero"), None)
    if zero is None:
        return {"n": 0, "reason": "run never reached cue_zero"}
    offsets: list[float] = []
    discarded = 0
    for e in events:
        if e.kind != "human_signal":
            continue
        if e.detail.get("timing_usable") is False:
            discarded += 1
            continue
        off_ms = (e.t - key_latency_s - zero) * 1000.0
        if off_ms < min_offset_ms:
            discarded += 1
            continue
        offsets.append(off_ms)
    if not offsets:
        return {"n": 0, "discarded": discarded}
    return {
        "n": len(offsets),
        "discarded": discarded,
        "offsets_ms": offsets,
        "median_ms": median(offsets),
        "min_ms": min(offsets),
        "max_ms": max(offsets),
        "spread_ms": max(offsets) - min(offsets),
        "stdev_ms": _stdev(offsets),
        "key_latency_ms": key_latency_s * 1000.0,
    }


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def reconcile_timestamps(obs: Observation, host_offset_s: float = 0.0) -> Observation:
    """Shift an observation from host time into the conductor's clock domain.

    Single-host M1: a no-op.  Split-host deployments must supply the measured
    offset, otherwise the whole scheme degrades (Albert's warning: without
    clock sync, time-triggered jitter can be *worse* than event-triggered).
    """
    if host_offset_s:
        return Observation(
            t=obs.t - host_offset_s,
            source=obs.source,
            payload=obs.payload,
            raw=obs.raw,
            human_latency=obs.human_latency,
        )
    return obs
