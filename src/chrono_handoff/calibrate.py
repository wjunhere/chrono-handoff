"""Human-latency calibration: the part that makes the second run better than
the first.

The measurable claim of this project is not "the machine can count down"; it is
*"after one trial, the machine knows how this particular human is biased, and
can therefore place the cue so that human and machine meet at the same
instant."*

Estimation rules (all of them exist because the naive version misbehaves):

1. **Median, not mean.**  One trial where the user went for coffee must not
   drag the estimate.
2. **Segment by task signature.**  Tapping to a 500 ms beep grid and pressing
   a physical RESET button do not share an offset; pooling them produces a
   number that is wrong for both.
3. **Clamp hard, move slowly.**  A single trial may not shift the estimate
   more than ``max_step_ms``; the total offset never exceeds
   ``max_abs_offset_ms``.  Without this, one outlier early in the session
   causes a runaway (the cue moves, which changes the measured offset, which
   moves the cue further).
4. **Only closed-loop samples count.**  A trial where the expected signal
   never arrived says nothing about *when* the human acted.
5. **The offset biases the human's cue only.**  Machine actions stay anchored
   to the physical truth.  Observed lateness of a human must never delay a
   relay.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .home import chrono_home
from .inputs import DEFAULT_KEY_LATENCY_S
from .model import mad, median
from .schema import SchemaMismatch, adopt, envelope, write_json


def default_store_path() -> Path:
    return chrono_home() / "calibration.json"


@dataclass
class Calibration:
    """Calibration state for one task signature."""

    key: str
    offset_ms: float = 0.0          #: signed; positive = human fires late
    n: int = 0                      #: number of samples folded in
    samples_ms: list[float] = field(default_factory=list)
    key_latency_ms: float = DEFAULT_KEY_LATENCY_S * 1000.0
    last_source: str = ""

    def predict_s(self) -> float:
        return self.offset_ms / 1000.0

    def spread_ms(self) -> float:
        return mad(self.samples_ms, self.offset_ms) if self.samples_ms else 0.0


def task_signature(
    cue: str = "beat",
    ioi_ms: float = 500.0,
    effector: str = "human",
    target: str = "",
) -> str:
    """Stable key for "which kind of action is this"."""
    return f"{effector}|{cue}|ioi={int(round(ioi_ms))}|{target or '-'}"


class CalibrationStore:
    """Tiny JSON-backed store.  One file, human-inspectable, no daemon needed."""

    def __init__(self, path: Path | str | None = None, max_abs_offset_ms: float = 300.0, max_step_ms: float = 30.0, window: int = 12) -> None:
        self.path = Path(path) if path else default_store_path()
        self.max_abs_offset_ms = max_abs_offset_ms
        self.max_step_ms = max_step_ms
        self.window = window
        self._items: dict[str, Calibration] = {}
        #: Why `save` will refuse (empty = writable).  Set by `load` when the file
        #: was written by a newer version: the readings in it are measurements of
        #: a person's timing, so they are neither guessed at nor written over.
        self.readonly_reason = ""
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        adopted = adopt(raw, path=self.path, name="calibration.json")
        if adopted.refuse_write:
            self.readonly_reason = adopted.reason
            return
        for key, d in (adopted.payload.get("items") or {}).items():
            try:
                self._items[key] = Calibration(**d)
            except TypeError:
                continue

    def save(self) -> None:
        if self.readonly_reason:
            raise SchemaMismatch(self.readonly_reason)
        write_json(self.path, envelope(items={k: asdict(v) for k, v in self._items.items()}))

    # -- api ---------------------------------------------------------------

    def get(self, key: str) -> Calibration:
        return self._items.get(key) or Calibration(key=key)

    def observe(self, key: str, offset_ms: float, source: str = "") -> Calibration:
        """Fold one closed-loop measurement into the estimate.

        Returns the updated calibration.  ``offset_ms`` must already be
        corrected for measurement-path latency (see
        :func:`chrono_handoff.inputs.analyse_offsets`).
        """
        cal = self.get(key)
        cal.samples_ms.append(float(offset_ms))
        if len(cal.samples_ms) > self.window * 3:
            cal.samples_ms = cal.samples_ms[-self.window * 3 :]
        recent = cal.samples_ms[-self.window :]
        target = median(recent)
        step = max(-self.max_step_ms, min(self.max_step_ms, target - cal.offset_ms))
        cal.offset_ms = max(-self.max_abs_offset_ms, min(self.max_abs_offset_ms, cal.offset_ms + step))
        cal.n = len(cal.samples_ms)
        cal.last_source = source
        self._items[key] = cal
        self.save()
        return cal

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._items.clear()
        else:
            self._items.pop(key, None)
        self.save()

    def all(self) -> dict[str, Calibration]:
        return dict(self._items)

    def report(self) -> str:
        if not self._items:
            return "no calibration data yet"
        lines = [f"{'key':<44} {'offset':>9} {'±MAD':>7} {'n':>4}"]
        for key, c in sorted(self._items.items()):
            lines.append(f"{key:<44} {c.offset_ms:>7.1f}ms {c.spread_ms():>5.1f}ms {c.n:>5}")
        return "\n".join(lines)


def simulate_calibration(true_offset_ms: float = 45.0, noise_ms: float = 25.0, n: int = 20, seed: int = 7) -> dict[str, Any]:
    """Exercise the estimator against a known ground truth.

    Used by ``chrono selftest --calibration``: if the estimator converges to
    the injected offset within ``noise/√n + max_step``, the arithmetic and the
    clamping behave as documented.  Also demonstrates rule 1: we inject one
    wild outlier and verify the estimate barely moves.
    """
    import random

    rng = random.Random(seed)
    store = CalibrationStore(path=Path(tempfile.gettempdir()) / "chrono_cal_selftest.json")
    store.reset()
    key = "human|beat|ioi=500|selftest"
    trace: list[tuple[int, float, float]] = []
    for i in range(n):
        measured = rng.gauss(true_offset_ms, noise_ms)
        cal = store.observe(key, measured)
        trace.append((i + 1, measured, cal.offset_ms))
    # outlier immunity
    before = store.get(key).offset_ms
    after = store.observe(key, 5000.0).offset_ms
    final = store.get(key)
    return {
        "true_offset_ms": true_offset_ms,
        "noise_ms": noise_ms,
        "n": n,
        "final_estimate_ms": final.offset_ms,
        "final_mad_ms": final.spread_ms(),
        "abs_error_ms": abs(final.offset_ms - true_offset_ms),
        "outlier_shift_ms": after - before,
        "trace": trace,
    }


def merge_samples(values: Iterable[float]) -> float:
    """Robust central estimate for a batch (used by ``chrono calibrate``)."""
    return median(list(values))
