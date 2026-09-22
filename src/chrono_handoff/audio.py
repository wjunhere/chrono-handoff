"""Cross-platform, non-blocking audio for the cue channel.

Hard rule (measured, see clock.py): **the audio call in the beat callback must
never block.**  ``winsound.Beep`` blocks 61.6 ms for a nominal 50 ms tone, i.e.
20% of a 300 ms window, on every beat.  Every sink below is therefore
asynchronous with respect to the beat grid.

Backends, best first:

``sample``
    A persistent PortAudio stream (``sounddevice``).  Tones are mixed into the
    output buffer at an exact frame offset, so placement is sample-accurate and
    ``play()`` is a ``deque.append`` -- no syscall, no process, no allocation on
    the audio thread.  This is the portable answer.
``wav``
    Platform-native pre-rendered WAV playback (``winsound`` on Windows).
    Measured 0.25 ms call, 3 ms max.  Windows default because it is proven and
    needs no dependency.
``cmd``
    An external player process (``afplay`` / ``pw-play`` / ``paplay``
    / ``aplay``).  Last resort, and graded *approximate* on purpose: spawning
    costs 9.6 ms median **and up to 246 ms**, measured on this machine, which is
    more than enough to smear a beat grid.
``bell``
    ASCII BEL.  The terminal makes the sound; latency unknown.
``null``
    Silence, for measurement runs.

``AudioSink.precision`` reports what the caller actually got.  A silent
downgrade to the approximate path would corrupt precisely the timing numbers
this project exists to produce, so the grade travels with the sink and is
surfaced by ``chrono doctor``.
"""

from __future__ import annotations

import array
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import math
import threading
import wave
from collections import deque
from typing import Protocol

# --------------------------------------------------------------------------
# tone generation
# --------------------------------------------------------------------------

SAMPLE_RATE = 22050

#: Fade applied to both ends of every tone.  An abrupt gate click is an extra
#: transient the ear can latch onto, which pollutes the very measurement we are
#: making, so the envelope is not cosmetic.
FADE_MS = 4.0


def tone_pcm16(freq: float, dur_ms: float, amp: float = 0.55, rate: int = SAMPLE_RATE) -> bytes:
    """A windowed sine burst as raw mono int16 little-endian samples."""
    n = max(1, int(rate * dur_ms / 1000.0))
    fade = max(1, int(rate * FADE_MS / 1000.0))
    out = array.array("h", bytes(2 * n))
    two_pi_f_over_r = 2.0 * math.pi * freq / rate
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = max(0.0, (n - i) / fade)
        s = math.sin(two_pi_f_over_r * i) * amp * env
        out[i] = int(max(-1.0, min(1.0, s)) * 32767)
    if sys.byteorder == "big":  # keep the byte order stable across platforms
        out.byteswap()
    return out.tobytes()


def tone_wav(freq: float, dur_ms: float, amp: float = 0.55, rate: int = SAMPLE_RATE) -> bytes:
    """The same tone wrapped in a RIFF/WAVE container, for file-based players."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone_pcm16(freq, dur_ms, amp, rate))
    return buf.getvalue()


#: Sample palette.  The GO beat is *different* from the ticks on purpose: a
#: timbre change is a stronger temporal landmark than a volume change, so the
#: human can hear "this is the one" without counting.
SAMPLES: dict[str, tuple[float, float]] = {
    "tick": (880.0, 35.0),
    "accent": (1568.0, 90.0),
    "ready": (440.0, 60.0),
    "abort": (220.0, 150.0),
}

#: Precision grades.  ``device`` means the playback start is anchored in the
#: audio device's own clock; ``approx`` means something in the path (a process
#: spawn, a terminal) can add tens to hundreds of milliseconds of unknown
#: jitter; ``none`` means no sound at all.
PRECISE = "device"
APPROX = "approx"
SILENT = "none"


class AudioSink(Protocol):
    """Non-blocking audio.  ``play`` must return in well under a millisecond."""

    name: str
    precision: str

    def play(self, key: str) -> None: ...

    def close(self) -> None: ...


class NullAudio:
    name = "null"
    precision = SILENT

    def play(self, key: str) -> None:  # noqa: D102
        return

    def close(self) -> None:
        return


class BellAudio:
    """ASCII BEL.  Non-blocking, but the *terminal* makes the sound, possibly
    over a serial link -- so the grade is approximate by construction."""

    name = "bell"
    precision = APPROX

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


# --------------------------------------------------------------------------
# sample-accurate portable backend
# --------------------------------------------------------------------------


class SoundDeviceAudio:
    """PortAudio output stream with sample-accurate tone placement.

    Design notes, because two obvious approaches are wrong:

    * **Not one stream per beat.**  Opening a stream costs tens of ms.
    * **Not ``sd.play()``.**  It routes through a fresh buffer each call and
      gives no placement control relative to the stream clock.

    So: one persistent ``RawOutputStream``, and a queue of ``(frame, tone)``
    scheduled against the stream's own frame counter.  ``play()`` appends to
    that queue and returns -- the mixing happens on PortAudio's callback thread.

    Block size affects only how far ahead a beat must be submitted, because a
    tone may start at *any* frame offset inside a block (``k`` below), not just
    at a block boundary.  Placement precision is therefore one sample.
    """

    name = "sample"
    precision = PRECISE

    def __init__(
        self,
        samples: dict[str, tuple[float, float]] | None = None,
        device: int | str | None = None,
        rate: int = SAMPLE_RATE,
        blocksize: int = 256,
    ) -> None:
        import sounddevice as sd  # noqa: PLC0415  -- optional dependency

        self._sd = sd
        self._rate = rate
        self._blocksize = blocksize
        # Enough headroom that a beat submitted from the scheduler thread is
        # always placed in a block that has not been rendered yet.
        self._lookahead = blocksize
        self._frame = 0
        self._pending: deque[tuple[int, str]] = deque()   # (start frame, key)
        self._active: list[list] = []                     # [offset, cursor, tone]
        self._closed = False
        self._underruns = 0

        self._voices: dict[str, array.array] = {}
        for key, (freq, dur) in (samples or SAMPLES).items():
            a = array.array("h")
            a.frombytes(tone_pcm16(freq, dur, rate=rate))
            if sys.byteorder == "big":
                a.byteswap()
            self._voices[key] = a
        self._fallback = self._voices.get("tick")

        self._zero = array.array("h", bytes(2 * blocksize))
        try:
            self._stream = sd.RawOutputStream(
                samplerate=rate,
                blocksize=blocksize,
                channels=1,
                dtype="int16",
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            self._closed = True
            raise

    # -- audio thread ------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ARG002, ANN001
        if status:
            self._underruns += 1
        mv = memoryview(outdata).cast("h")
        pos = self._frame
        end = pos + frames

        # Adopt every voice whose start frame falls inside this block.  The
        # clamp matters: a beat submitted while this very block was being
        # rendered reads a stale frame counter, and rounding it backwards is
        # what turns "slightly late" into "wrote outside the buffer".
        while self._pending and self._pending[0][0] < end:
            start, key = self._pending.popleft()
            tone = self._voices.get(key) or self._fallback
            if tone is not None:
                self._active.append([max(0, start - pos), 0, tone])

        mv[:] = self._zero[:frames]

        for v in self._active:
            k, cur, tone = v
            if k >= frames:
                v[0] = k - frames
                continue
            take = min(len(tone) - cur, frames - k)
            for i in range(take):
                s = mv[k + i] + tone[cur + i]
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                mv[k + i] = s
            v[1] = cur + take
            v[0] = 0
        if self._active:
            self._active = [v for v in self._active if v[1] < len(v[2])]

        self._frame = end

    # -- caller thread -----------------------------------------------------
    def play(self, key: str) -> None:
        if self._closed:
            return
        # Appends are ordered by construction: _frame only increases and the
        # lookahead is constant, so the deque stays sorted without a sort.
        self._pending.append((self._frame + self._lookahead, key))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    @property
    def underruns(self) -> int:
        return self._underruns


# --------------------------------------------------------------------------
# platform-native backend
# --------------------------------------------------------------------------


class WinsoundAudio:
    """Windows: pre-rendered WAVs driven by async ``PlaySound``.

    Measured call cost: 0.25 ms median, 3 ms max, non-blocking.
    """

    name = "wav"
    precision = PRECISE

    def __init__(self, samples: dict[str, tuple[float, float]] | None = None) -> None:
        import winsound  # noqa: PLC0415  -- Windows-only by construction

        self._winsound = winsound
        self._flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        self._paths: dict[str, str] = {}
        for key, (freq, dur) in (samples or SAMPLES).items():
            self._paths[key] = _ensure_wav(key, freq, dur)
        self._fallback = self._paths.get("tick")

    def play(self, key: str) -> None:
        path = self._paths.get(key) or self._fallback
        if path is not None:
            self._winsound.PlaySound(path, self._flags)

    def close(self) -> None:
        try:
            self._winsound.PlaySound(None, self._winsound.SND_PURGE)
        except Exception:
            pass


#: Back-compat alias: the name before the cross-platform split.
WavAudio = WinsoundAudio


def _ensure_wav(key: str, freq: float, dur: float) -> str:
    d = os.path.join(tempfile.gettempdir(), "chrono_handoff")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{key}_{int(freq)}_{int(dur)}.wav")
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(tone_wav(freq, dur))
        os.replace(tmp, path)  # atomic: concurrent sinks cannot see a partial file
    return path


# --------------------------------------------------------------------------
# external-player backend
# --------------------------------------------------------------------------

#: Candidates in preference order, per platform.  All accept
#: ``<player> <file>`` and exit on their own.
_CMD_PLAYERS: dict[str, list[list[str]]] = {
    "darwin": [["afplay"]],
    "linux": [["pw-play"], ["paplay"], ["aplay", "-q"]],
    "win32": [["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]],
}


class CommandAudio:
    """Last-resort playback through an external player process.

    Graded ``approx``: measured spawn cost on this machine is 9.6 ms median but
    246 ms worst case, and each player lives ~730 ms for a 35 ms tone.  Child
    processes are reaped on ``close`` so a long run cannot accumulate them.
    """

    name = "cmd"
    precision = APPROX

    def __init__(self, samples: dict[str, tuple[float, float]] | None = None) -> None:
        self._cmds: dict[str, list[str]] = {}
        for key, (freq, dur) in (samples or SAMPLES).items():
            self._cmds[key] = [_ensure_wav(key, freq, dur)]
        self._fallback = self._cmds.get("tick")
        self._cmd = self._pick_player()
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    @staticmethod
    def _pick_player() -> list[str]:
        for cand in _CMD_PLAYERS.get(sys.platform, []):
            if shutil.which(cand[0]):
                return cand
        return []

    def play(self, key: str) -> None:
        if not self._cmd:
            return
        path = self._cmds.get(key) or self._fallback
        if path is None:
            return
        try:
            p = subprocess.Popen(
                [*self._cmd, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            return
        with self._lock:
            self._procs.append(p)
            if len(self._procs) > 16:  # drop handles for processes that have exited
                self._procs = [q for q in self._procs if q.poll() is None]

    def close(self) -> None:
        with self._lock:
            for p in self._procs:
                try:
                    if p.poll() is None:
                        p.terminate()
                except Exception:
                    pass
            self._procs = []


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

#: Preference order per platform.  Windows keeps ``wav`` first because it is
#: measured, dependency-free and already the behaviour the journals were
#: written against; ``sample`` is preferred everywhere else.
_ORDER: dict[str, list[str]] = {
    "win32": ["wav", "sample", "cmd", "bell"],
    "linux": ["sample", "cmd", "bell"],
    "darwin": ["sample", "cmd", "bell"],
}

#: Valid values for every ``--audio`` flag, so the CLI, the daemon and the
#: panel settings cannot drift apart.
AUDIO_CHOICES = ("auto", "sample", "wav", "cmd", "bell", "null")

_FACTORY = {
    "wav": WinsoundAudio,
    "sample": SoundDeviceAudio,
    "cmd": CommandAudio,
}


def _build(kind: str) -> AudioSink | None:
    """Instantiate one backend, or ``None`` (with a reason) if unavailable."""
    if kind == "bell":
        return BellAudio()
    if kind == "null":
        return NullAudio()
    if kind == "wav" and sys.platform != "win32":
        return None
    cls = _FACTORY.get(kind)
    if cls is None:
        return None
    if kind == "cmd" and not CommandAudio._pick_player():
        return None
    try:
        return cls()  # type: ignore[return-value]
    except Exception:
        return None


#: Populated by the last ``default_audio`` call so ``chrono doctor`` can explain
#: *why* a given sink was chosen without re-probing.
LAST_REASONS: list[tuple[str, bool]] = []


def default_audio(force: str = "auto") -> AudioSink:
    """Pick the best available sink.

    ``force``: ``auto`` | ``sample`` | ``wav`` | ``cmd`` | ``bell`` | ``null``.

    Forcing a backend that this platform cannot provide raises, rather than
    quietly substituting a different one -- an explicit request that silently
    degrades is how a measurement run ends up with the wrong timing grade.
    """
    global LAST_REASONS
    LAST_REASONS = []

    if force in ("bell", "null"):
        return _build(force)  # type: ignore[return-value]

    if force != "auto":
        sink = _build(force)
        if sink is None:
            raise RuntimeError(f"audio backend {force!r} is not available on {sys.platform}")
        LAST_REASONS.append((force, True))
        return sink

    for kind in _ORDER.get(sys.platform, ["sample", "cmd", "bell"]):
        sink = _build(kind)
        LAST_REASONS.append((kind, sink is not None))
        if sink is not None:
            return sink
    return BellAudio()


def describe(sink: AudioSink) -> str:
    """One line for ``chrono doctor``."""
    detail = {
        "wav": "winsound PlaySound async (0.25ms call, measured)",
        "sample": "PortAudio stream, sample-accurate placement",
        "cmd": "external player process (APPROXIMATE: 10-250ms spawn jitter)",
        "bell": "ascii BEL (terminal makes the sound)",
        "null": "silent",
    }.get(sink.name, "?")
    return f"{sink.name} [{sink.precision}] -- {detail}"
