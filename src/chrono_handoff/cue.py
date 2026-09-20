"""The human-facing cue channel: audio + visual.

Hard rule (measured, see clock.py docstring): **the audio call in the beat
callback must never block.**  ``winsound.Beep`` blocks for its full duration
(61.6 ms for a nominal 50 ms tone) and would therefore eat 20% of a 300 ms
window while being jittery.  We pre-render WAV files and drive them with
``PlaySound(..., SND_FILENAME | SND_ASYNC)``: 0.25 ms call, non-blocking.

The visual channel is deliberately decoupled behind a queue: writing to a
redirected stdout pipe can block when the reader is slow, which would stall
the beat grid.  Digits landing 30 ms late is invisible to the human; a stalled
grid is fatal.
"""

from __future__ import annotations

import io
import math
import os
import queue
import struct
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from typing import Protocol

from .clock import CLOCK, wait_until
from .model import Beat

# --------------------------------------------------------------------------
# tone generation
# --------------------------------------------------------------------------

SAMPLE_RATE = 22050


def _tone_wav(freq: float, dur_ms: float, amp: float = 0.55, rate: int = SAMPLE_RATE) -> bytes:
    """A windowed sine burst.  The 4 ms fade matters: an abrupt gate click is
    an extra transient the ear can latch onto, which pollutes the very
    measurement we are making."""
    n = max(1, int(rate * dur_ms / 1000.0))
    fade = max(1, int(rate * 0.004))
    frames = bytearray()
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = max(0.0, (n - i) / fade)
        s = math.sin(2 * math.pi * freq * i / rate) * amp * env
        frames += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


#: Sample palette.  The zero beat is *different* from the ticks on purpose:
#: a timbre change is a stronger temporal landmark than a volume change, so
#: the human can hear "this is the one" without counting.
SAMPLES: dict[str, tuple[float, float]] = {
    "tick": (880.0, 35.0),
    "accent": (1568.0, 90.0),
    "ready": (440.0, 60.0),
    "abort": (220.0, 150.0),
}


class AudioSink(Protocol):
    """Non-blocking audio.  ``play`` must return in well under a millisecond."""

    name: str

    def play(self, key: str) -> None: ...

    def close(self) -> None: ...


class NullAudio:
    name = "null"

    def play(self, key: str) -> None:  # noqa: D102
        return

    def close(self) -> None:
        return


class BellAudio:
    """Absolute last resort: ASCII BEL.  Non-blocking, latency unknown (it is
    the *terminal* that makes the sound, possibly over a serial link)."""

    name = "bell"

    def __init__(self, stream: io.TextIOBase | None = None) -> None:
        self._s = stream or sys.stderr

    def play(self, key: str) -> None:
        try:
            self._s.write("\a")
            self._s.flush()
        except Exception:
            pass

    def close(self) -> None:
        return


class WavAudio:
    """Primary implementation (Windows): pre-rendered WAVs + async PlaySound.

    Measured call cost: 0.25 ms median, 3 ms max, non-blocking.
    """

    name = "wav"

    def __init__(self, samples: dict[str, tuple[float, float]] | None = None) -> None:
        import winsound  # noqa: PLC0415  -- Windows-only by construction

        self._winsound = winsound
        self._flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        self._dir = os.path.join(tempfile.gettempdir(), "chrono_handoff")
        os.makedirs(self._dir, exist_ok=True)
        self._paths: dict[str, str] = {}
        for key, (freq, dur) in (samples or SAMPLES).items():
            path = os.path.join(self._dir, f"{key}_{int(freq)}_{int(dur)}.wav")
            if not os.path.exists(path):
                with open(path, "wb") as fh:
                    fh.write(_tone_wav(freq, dur))
            self._paths[key] = path
        self._fallback = self._paths.get("tick")

    def play(self, key: str) -> None:
        path = self._paths.get(key)
        if path is not None:
            self._winsound.PlaySound(path, self._flags)
        elif self._fallback is not None:
            self._winsound.PlaySound(self._fallback, self._flags)

    def close(self) -> None:
        try:
            self._winsound.PlaySound(None, self._winsound.SND_PURGE)
        except Exception:
            pass


def default_audio(force: str = "auto") -> AudioSink:
    """Pick the best available sink.  ``force``: auto|wav|bell|null."""
    if force == "null":
        return NullAudio()
    if force == "bell":
        return BellAudio()
    if force in ("auto", "wav"):
        try:
            return WavAudio()
        except Exception:
            if force == "wav":
                raise
    return BellAudio()


# --------------------------------------------------------------------------
# visual channel
# --------------------------------------------------------------------------

BAR_WIDTH = 28

#: Visual cue styles.  A countdown of digits is a poor *metronome*: it changes
#: by one glyph per beat, so the beat has almost no visual onset.  A travelling
#: marker changes the whole line every beat, and a blank/reset frame around the
#: beat adds an off-on transient -- which is what the visual system actually
#: latches onto for timing.
VISUAL_STYLES = ("bar", "marker", "flash")


def _render_bar(ticks_left: int, total: int) -> str:
    filled = max(0, min(BAR_WIDTH, int(round(BAR_WIDTH * ticks_left / max(1, total)))))
    return "#" * filled + "." * (BAR_WIDTH - filled)


def _render_marker(beat_index: int, total: int) -> str:
    """A block that hops one cell per beat: maximum per-beat change, and its
    position encodes how many beats remain without any reading."""
    total = max(1, total)
    pos = min(BAR_WIDTH - 1, beat_index * BAR_WIDTH // total)
    return "." * pos + "o" + "." * (BAR_WIDTH - 1 - pos)


def _plain_bar_with_digits(ticks_left: int, total: int) -> str:
    return f"[{_render_bar(ticks_left, total)}] {max(0, ticks_left):>2} beats"


@dataclass
class _VisualMsg:
    kind: str            #: header | beat | zero | done | note
    text: str = ""
    beat_index: int = 0
    beats_total: int = 0
    beats_left: int = 0


class VisualCue:
    """Renders the countdown to stderr (never stdout: stdout may be the MCP
    transport).  A dedicated thread drains a queue so the beat callback only
    ever does ``put_nowait``."""

    def __init__(
        self,
        enabled: bool = True,
        stream: io.TextIOBase | None = None,
        banner_s: float = 1.2,
        style: str = "marker",
        flash: bool = True,
    ) -> None:
        self.enabled = enabled
        self._stream = stream or sys.stderr
        self._q: queue.SimpleQueue[_VisualMsg] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._banner_s = banner_s
        self._banner_until = 0.0
        #: "marker" (default) is the best visual metronome; "bar" keeps the
        #: classic progress bar; "flash" is marker plus a blanked frame.
        self.style = style if style in VISUAL_STYLES else "marker"
        #: Draw a reset frame right before the peaking frame.  This creates an
        #: off->on transition, which is markedly easier to time than a change
        #: between two "on" states.
        self.flash = flash

    def start(self) -> "VisualCue":
        if not self.enabled or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="chrono-visual", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop = True
        self._q.put(_VisualMsg("quit"))
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def submit(self, msg: _VisualMsg) -> None:
        if self.enabled:
            self._q.put(msg)

    # convenience API used by the conductor (all non-blocking)
    def header(self, text: str) -> None:
        self.submit(_VisualMsg("header", text=text))

    def note(self, text: str) -> None:
        self.submit(_VisualMsg("note", text=text))

    def beat(self, beat: Beat, beats_total: int, beats_left: int) -> None:
        self.submit(
            _VisualMsg(
                "zero" if beat.accent else "beat",
                text=beat.label,
                beat_index=beat.index,
                beats_total=beats_total,
                beats_left=beats_left,
            )
        )

    def done(self, text: str) -> None:
        self.submit(_VisualMsg("done", text=text))

    # -- renderer ----------------------------------------------------------

    def _write(self, s: str) -> None:
        try:
            self._stream.write(s)
            self._stream.flush()
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop:
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg.kind == "quit":
                break
            self._render(msg)
        self._write("\n")

    def _render(self, msg: _VisualMsg) -> None:
        if msg.kind == "header":
            self._write("\n" + "-" * 60 + f"\n  {msg.text}\n" + "-" * 60 + "\n")
        elif msg.kind == "note":
            self._write(f"  {msg.text}\n")
        elif msg.kind in ("beat", "zero"):
            total = max(1, msg.beats_total)
            left = msg.beats_left
            if msg.kind == "zero":
                line = f"\n  >>> GO <<<  ({msg.text})" if msg.text else "\n  >>> GO <<<"
                self._write(line + "\n")
            else:
                self._write(f"\r  [{_render_bar(left, total)}]  {left:>2} beats   ")
        elif msg.kind == "done":
            self._write(f"\n  {msg.text}\n")


class MultiVisual:
    """Fan-out visual sink (terminal + web are independent consumers)."""

    def __init__(self, *sinks: object) -> None:
        self._sinks = [s for s in sinks if s is not None]

    def __getattr__(self, item: str):
        def call(*a, **kw):
            for s in self._sinks:
                fn = getattr(s, item, None)
                if fn is not None:
                    fn(*a, **kw)

        return call
