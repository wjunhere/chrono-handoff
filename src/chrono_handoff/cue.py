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

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from .clock import CLOCK, wait_until
from .model import Beat

# --------------------------------------------------------------------------
# audio
#
# The audio channel now lives in ``audio.py`` (cross-platform, graded by
# precision).  It is re-exported here because this module is what the rest of
# the codebase imports the cue channels from.
# --------------------------------------------------------------------------

from .audio import (  # noqa: E402  (re-export; keep name compatibility)
    APPROX,
    PRECISE,
    SAMPLE_RATE,
    SAMPLES,
    SILENT,
    AudioSink,
    BellAudio,
    CommandAudio,
    NullAudio,
    SoundDeviceAudio,
    WinsoundAudio,
    default_audio,
    describe as describe_audio,
    tone_pcm16,
    tone_wav,
)

#: Back-compat aliases for the pre-split names.
WavAudio = WinsoundAudio
_tone_wav = tone_wav

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
