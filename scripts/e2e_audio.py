"""Audio backend suite: mixer correctness, backend selection, and the
non-blocking invariant on every sink this machine can actually provide.

Run:  python scripts/e2e_audio.py

Why this is a separate suite rather than more cases in the core selftest: the
mixer math can be checked without any audio device (the callback is driven
directly), whereas the device-level checks must skip cleanly on a headless
machine.  Mixing the two would make one of them lie.
"""

from __future__ import annotations

import array
import io
import os
import statistics
import sys
import time
import wave
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chrono_handoff.audio import (  # noqa: E402
    APPROX,
    PRECISE,
    SAMPLES,
    CommandAudio,
    SoundDeviceAudio,
    WinsoundAudio,
    default_audio,
    describe,
    tone_pcm16,
    tone_wav,
)
from chrono_handoff.conductor import Conductor  # noqa: E402

FAILED: list[str] = []
EXCUSED: list[str] = []

#: Checks whose number is the audio device's rather than the product's.
#:
#: The `sample` sink is the only one that writes into a real device's buffer (on a
#: runner: a virtual one), and its landing offset is then dominated by that device's
#: buffer period -- measured 8.7ms median / 16.5ms max on the macOS runner, which is
#: one ~10ms buffer, while the sinks that touch no device stayed sub-millisecond on
#: the same machine.  CI sets CHRONO_TOLERANT_TIMING=1 and these are printed as FAIL
#: with their number and not counted: the number is the only way to tell a loaded
#: runner from a real regression, so it is never hidden, only excused.
TIMING_ONLY_CHECKS = {"sample: beat landing sub-millisecond"}


def check(label: str, ok: bool, detail: str = "") -> None:
    tolerant = os.environ.get("CHRONO_TOLERANT_TIMING") == "1"
    excused = (not ok) and tolerant and label in TIMING_ONLY_CHECKS
    mark = "PASS" if ok else "FAIL"
    note = "  (excused: measures the audio device -- see --tolerant-timing)" if excused else ""
    print(f"  [{mark}] {label:<52} {detail}{note}")
    if excused:
        EXCUSED.append(label)
    elif not ok:
        FAILED.append(label)


# ---------------------------------------------------------------------------
# 1. tone payload
# ---------------------------------------------------------------------------


def test_tone() -> None:
    print("\n-- tone generation --")

    # Byte-identical to the pre-refactor implementation.  The WAV files the
    # Windows sink plays are cached in %TEMP% across versions, so a change here
    # would silently keep using the old files on an existing machine while new
    # machines got different tones.
    def legacy(freq: float, dur_ms: float, amp: float = 0.55, rate: int = 22050) -> bytes:
        import math
        import struct

        n = max(1, int(rate * dur_ms / 1000.0))
        fade = max(1, int(rate * 0.004))
        out = bytearray()
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = max(0.0, (n - i) / fade)
            s = math.sin(2 * math.pi * freq * i / rate) * amp * env
            out += struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
        return bytes(out)

    ok = all(tone_pcm16(f, d) == legacy(f, d) for f, d in SAMPLES.values())
    check("tone PCM unchanged vs previous implementation", ok)

    raw = tone_pcm16(880.0, 35.0)
    check("tone length matches duration", len(raw) == 2 * int(22050 * 0.035),
          f"{len(raw) // 2} frames")

    # The envelope must remove the gate click.  Asserting the *last sample* is
    # exactly zero would be wrong: the linear fade reaches 1/fade of full scale
    # (about 205 LSB here) rather than zero, which is inaudible but not exact.
    # What matters is that both edges sit far below the peak, i.e. the tone was
    # actually faded rather than cut.
    import struct as _struct

    samples = _struct.unpack(f"<{len(raw) // 2}h", raw)
    peak = max(abs(s) for s in samples)
    edge = max(abs(samples[0]), abs(samples[-1]))
    fade_frames = int(22050 * 0.004)
    check("tone is windowed at both edges (no gate click)",
          edge < 0.03 * peak,
          f"edge={edge} peak={peak} in {fade_frames}-frame fade")

    buf = tone_wav(880.0, 35.0)
    with wave.open(io.BytesIO(buf)) as w:
        hdr_ok = (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
        frames_ok = w.readframes(w.getnframes()) == raw
    check("WAV container round-trips", hdr_ok and frames_ok)


# ---------------------------------------------------------------------------
# 2. mixer math, no device required
# ---------------------------------------------------------------------------


def _offline_mixer(blocksize: int = 64) -> SoundDeviceAudio:
    """A SoundDeviceAudio with the stream bypassed, so _callback can be driven
    by hand.  This is the only way to test placement deterministically."""
    o = SoundDeviceAudio.__new__(SoundDeviceAudio)
    o._rate, o._blocksize, o._lookahead, o._frame = 22050, blocksize, blocksize, 0
    o._closed, o._underruns = False, 0
    o._pending, o._active = deque(), []
    tick = array.array("h")
    tick.frombytes(tone_pcm16(880.0, 35.0))
    o._voices = {k: tick for k in SAMPLES}
    o._fallback = tick
    o._zero = array.array("h", bytes(2 * blocksize))
    return o


def _render(o: SoundDeviceAudio, blocks: int, blocksize: int = 64) -> array.array:
    total = array.array("h")
    for _ in range(blocks):
        buf = bytearray(2 * blocksize)
        o._callback(buf, blocksize, None, None)
        total.frombytes(bytes(buf))
    return total


def test_mixer() -> None:
    print("\n-- mixer (offline, no device) --")

    o = _offline_mixer()
    out = _render(o, 3)
    check("idle stream is silent", all(s == 0 for s in out))

    o = _offline_mixer()
    o.play("tick")                              # starts at frame 0 + lookahead
    out = _render(o, 6)
    check("tone starts exactly at the requested frame",
          all(s == 0 for s in out[:64]) and any(s != 0 for s in out[64:100]))

    # Offsets that are not a multiple of the block size: this is what makes the
    # placement sample-accurate rather than block-accurate.
    o = _offline_mixer()
    o._pending.append((70, "tick"))
    out = _render(o, 6)
    check("sub-block (sample-exact) offset respected",
          all(s == 0 for s in out[:70]) and any(s != 0 for s in out[70:100]))

    o = _offline_mixer()
    o._pending.append((0, "tick"))
    o._pending.append((0, "tick"))
    out = _render(o, 3)
    check("overlapping voices sum without int16 wraparound",
          max(abs(s) for s in out) > 0 and all(-32768 <= s <= 32767 for s in out))

    # A beat submitted while its own block was being rendered must be clamped,
    # not written at a negative offset.
    o = _offline_mixer()
    o._pending.append((-500, "tick"))
    out = _render(o, 3)
    check("stale/late start is clamped, not out of bounds", any(s != 0 for s in out))

    o = _offline_mixer()
    o.play("tick")
    _render(o, 60)
    check("finished voices are reaped", len(o._active) == 0, f"{len(o._active)} left")


# ---------------------------------------------------------------------------
# 3. backend selection
# ---------------------------------------------------------------------------


def test_selection() -> None:
    print("\n-- backend selection --")

    sink = default_audio("auto")
    print(f"       auto -> {describe(sink)}")
    check("auto picks a graded sink", sink.precision in (PRECISE, APPROX, "none"))

    for forced in ("bell", "null"):
        got = default_audio(forced).name
        check(f"forced {forced} is honoured", got == forced, f"got {got}")

    # A forced backend that this platform cannot provide must raise, because a
    # silent substitution is how a run ends up graded wrongly.
    if sys.platform != "win32":
        try:
            default_audio("wav")
            check("forcing an unavailable backend raises", False, "returned a sink")
        except RuntimeError:
            check("forcing an unavailable backend raises", True)
        check("wav backend refused off Windows", True)
    else:
        check("wav backend available on Windows", default_audio("wav").name == "wav")

    players = CommandAudio._pick_player()
    print(f"       external player for {sys.platform}: {players or 'none found'}")
    check("external-player lookup does not raise", isinstance(players, list))

    # The cmd backend is the last resort and graded APPROX -- but "approximate"
    # must not mean "silent".  It used to build argv as [*player, [wav_path]],
    # a nested list, so Popen raised TypeError; play()'s bare ``except`` swallowed
    # it and doctor still reported cmd as the active backend.  Nothing caught it
    # because this file never constructed a CommandAudio.  Assert the observable
    # effect -- a spawned process -- rather than trusting the call to have worked.
    print("\n-- cmd backend: play() must actually spawn a player --")
    if not players:
        print("  [SKIP] no external player on PATH for this platform")
    else:
        cmd = CommandAudio()
        check("cmd: every cue maps to a single path",
              all(isinstance(p, str) for p in cmd._cmds.values()),
              f"keys={sorted(cmd._cmds)}")
        cmd.play("tick")
        time.sleep(0.3)
        check("cmd: play() spawned a process (argv is not nested)",
              len(cmd._procs) > 0, f"{len(cmd._procs)} process(es) alive")
        cmd.close()


# ---------------------------------------------------------------------------
# 4. device-level: the non-blocking invariant
# ---------------------------------------------------------------------------


def _measure_calls(sink, n: int = 40) -> tuple[float, float]:
    sink.play("tick")
    time.sleep(0.15)
    lat = []
    for _ in range(n):
        t = time.perf_counter()
        sink.play("tick")
        lat.append((time.perf_counter() - t) * 1000)
        time.sleep(0.02)
    return statistics.median(lat), max(lat)


def test_device_sinks() -> None:
    print("\n-- device sinks: play() must not block --")

    for label, factory in (("sample", SoundDeviceAudio), ("wav", WinsoundAudio)):
        try:
            sink = factory()
        except Exception as exc:
            print(f"  [SKIP] {label:<52} {type(exc).__name__}: {exc}")
            continue
        try:
            med, worst = _measure_calls(sink)
            check(f"{label}: play() median sub-millisecond", med < 1.0,
                  f"median={med:.4f}ms max={worst:.4f}ms")
            check(f"{label}: no blocking outlier", worst < 20.0, f"max={worst:.3f}ms")
            check(f"{label}: graded device/precise", sink.precision == PRECISE)
        finally:
            sink.close()


# ---------------------------------------------------------------------------
# 5. end to end through the conductor
# ---------------------------------------------------------------------------


def test_conductor(sink_label: str, factory) -> None:
    try:
        sink = factory()
    except Exception as exc:
        print(f"  [SKIP] {sink_label:<52} {type(exc).__name__}: {exc}")
        return
    c = Conductor(audio=sink, visual=False, journal_dir=str(ROOT / f"logs/_aud_{sink_label}"))
    try:
        c.start()
        r = c.cue(instruction="press RESET", target_in_s=3.0, lead_s=1.0, ioi_ms=100,
                  tail_beats=12, expect_source="none", duration_s=8, open_panel=False)
        time.sleep(4.2)
        ev = [e for e in c.events(r["run_id"]) if e.kind in ("cue_beat", "cue_zero")]
        offs = [abs(e.detail.get("scheduled_offset_ms", 0.0)) for e in ev]
        med = statistics.median(offs) if offs else float("nan")
        check(f"{sink_label}: drives a real cue grid", len(offs) >= 10, f"{len(offs)} beats")
        check(f"{sink_label}: beat landing sub-millisecond", bool(offs) and med < 1.0,
              f"median={med:.3f}ms max={max(offs) if offs else float('nan'):.3f}ms")
        ur = getattr(sink, "underruns", None)
        if ur is not None:
            check(f"{sink_label}: zero audio underruns", ur == 0, f"{ur} underruns")
    finally:
        sink.close()
        c.stop()


def main() -> int:
    print("chrono-handoff :: audio backend suite")
    test_tone()
    test_mixer()
    test_selection()
    test_device_sinks()
    print("\n-- through the conductor --")
    test_conductor("sample", SoundDeviceAudio)
    test_conductor("wav", WinsoundAudio)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all audio checks passed"
          + (f", {len(EXCUSED)} excused as environment noise: {EXCUSED}" if EXCUSED else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
