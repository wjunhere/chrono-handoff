"""Declarative data model: the timeline the planner submits, and the events
the conductor returns.

Design rules encoded here (see docs/design.md):

* Time is expressed as **offsets from an anchor**, never as absolute instants
  the planner computes itself.  The conductor owns the clock.
* The anchor for anything the *human* sees is ``cue_start``, not the instant
  the tool call was received.  A human who walks back to their desk 40 s after
  submission must still get a full countdown.
* Every deadline is against the monotonic clock (``clock.CLOCK``), so NTP
  cannot move it.
* ``human_offset_ms`` is signed.  Feedback that is anchored on a *periodic*
  cue makes people fire slightly **early** (negative mean asynchrony), not
  late -- so the field is deliberately not called "latency".
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


class RunStatus(str, Enum):
    PENDING = "pending"          #: submitted, waiting for the cue grid to start
    RUNNING = "running"
    SATISFIED = "satisfied"      #: the physical signal was observed -> closed loop
    TIMEOUT = "timeout"          #: expected signal never arrived within budget
    PARTIAL = "partial"          #: signal arrived but was never confirmed
    ABORTED = "aborted"          #: user/agent cancelled
    ERROR = "error"


TERMINAL = {RunStatus.SATISFIED, RunStatus.TIMEOUT, RunStatus.ABORTED, RunStatus.ERROR}


@dataclass
class SignalSpec:
    """What physical evidence proves the human actually did the thing.

    ``expect_signal`` is an *observation*, not a promise: it may never arrive.
    A run that ends ``TIMEOUT`` is a legitimate, well-defined outcome -- not a
    crash and not "please keep waiting".
    """

    source: str = "console_key"       #: console_key | stdin_line | http | serial | sim | none
    #: Regex matched against ``payload + raw`` of an observation.  Empty means
    #: "any observation from this source counts".  Matching lives here (in the
    #: conductor) rather than in the source because a source's only job is to
    #: produce *raw readings with mechanism-level timestamps*; deciding what a
    #: reading means is per-run policy (the same serial port serves
    #: expect_signal and verify with different patterns).
    pattern: str = ""
    timeout_s: float = 30.0
    #: Poll granularity for sources that must be polled (kept well below the
    #: width of a typical physical window so we do not smear the measurement).
    interval_ms: float = 2.0
    #: Any signal arriving earlier than this offset from the cue origin is
    #: treated as stale (e.g. leftover input from a previous step) and ignored.
    min_offset_ms: float = -3000.0
    opts: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowSpec:
    """A machine-side action window: start..end, retried every ``interval_ms``.

    "Give the human an anchor, let the machine sweep."  The window exists so a
    single missed deadline is not fatal; it is *not* an excuse to sweep for 75
    seconds when 3 would do (see case-studies/loongson-2k1000.md 2.3).
    """

    start_off_ms: float
    end_off_ms: float
    interval_ms: float = 300.0
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CuePlan:
    """The whole submitted plan.  Declarative, immutable in spirit."""

    id: str
    instruction: str = ""

    # ---- what the human hears/sees ---------------------------------------
    lead_s: float = 3.0              #: length of the countdown before the zero beat
    ioi_ms: float = 500.0            #: inter-onset interval of the beat grid
    #: Beats to keep playing AFTER the zero beat.  Not decoration -- this is
    #: what makes anticipation possible at all.
    #:
    #: Sensorimotor literature is unambiguous that negative mean asynchrony
    #: (people firing slightly *early*, the signature of entrained anticipation)
    #: only emerges when the rhythm **continues**.  If the target beat is the
    #: final beat, there is nothing to entrain to and the only available
    #: strategy is "wait for the event, then react" -- which measures reaction
    #: time, not synchronisation ability.
    #:
    #: M1 originally ended the grid on the GO beat and duly measured a
    #: +105 ms / sd 58 ms offset: a reaction-time signature.  Keeping the grid
    #: running past the target lets the subject lock onto the pulse, so the
    #: measured offset reflects what the project actually claims to measure.
    tail_beats: int = 6
    #: Cancel the cue grid as soon as the human's action is observed.
    #: ``True`` is correct in production (stop bothering the operator once the
    #: goal is met).  ``False`` is required when the cue itself is under study,
    #: because the *tail* is part of the stimulus: if the pulse is cut the
    #: instant the subject responds, they never learn that the pulse continues
    #: past the target, and the next trial reverts to reacting to it.
    cancel_cue_on_signal: bool = True
    #: Beep on each beat.  Why audio is the default: not because vision cannot
    #: carry a beat -- it can, given a decent onset -- but because **audio does
    #: not occupy the eyes**.  In the target scenario (PMON boot window) the
    #: operator is already watching serial output; a visual cue competes for
    #: the same channel as the thing being observed.  Audio is a parallel
    #: channel.  Set False for hearing-impaired operators, noisy environments,
    #: or silent/secret operations -- then raise ``visual_style`` to compensate.
    audio: bool = True
    #: Terminal beat display.  ``visual_style``: "marker" (best metronome,
    #: default) | "bar" (classic progress bar) | "flash" (marker + blank frame).
    visual: bool = True
    visual_style: str = "marker"

    # ---- when the physical action should land ----------------------------
    #: Absolute monotonic instant the *physical action* should land at.
    #: If None, it is derived from ``target_in_s`` relative to cue start.
    #: Internal: no out-of-process caller can compute our monotonic base, and a
    #: wall-clock value here is silently treated as an instant ~57 years away.
    #: ``submit`` refuses such a value; agents use ``target_at_wall``.
    target_at: float | None = None
    #: Absolute **wall-clock** instant (epoch seconds) the physical action should
    #: land at.  This is the one a caller can express: "at 10:00:00, do X".
    #: Resolved against the wall clock when the grid is *armed*, so a queued cue
    #: keeps the instant it asked for instead of sliding by however long it
    #: waited -- and a cue whose instant passed while it waited ends
    #: ``verdict: "missed"`` instead of quietly arming an empty grid.
    target_at_wall: float | None = None
    target_in_s: float | None = None
    #: Predicted human offset, seconds.  Measured (signal - cue_zero) last
    #: time.  Positive = human fires late.  The cue zero beat is scheduled at
    #: ``target_at - offset_s`` so that human *and* machine meet at target_at.
    offset_s: float = 0.0

    # ---- closed loop -----------------------------------------------------
    #: Evidence that the human actually acted (may never arrive -- a TIMEOUT
    #: verdict is a normal outcome, not a crash).
    expect: SignalSpec | None = None
    #: Evidence that the action actually *took effect* on the device.  A
    #: separate spec because "the byte arrived" and "the board entered the
    #: bootloader" are different claims; conflating them is how you end up
    #: reporting success on a command that was never consumed.
    verify: SignalSpec | None = None
    on_window: WindowSpec | None = None

    #: Hard budget for the whole run, measured from cue origin.  Guarantees a
    #: terminal status even if the human walks away.
    duration_s: float = 60.0

    #: Arbitrary user data echoed back in the result (task id, block, ...).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Beat:
    """One scheduled cue tick, resolved to absolute time by the conductor."""

    index: int
    at: float            #: scheduled absolute monotonic instant
    accent: bool = False #: True on the zero beat
    f_audio: str = "tick"  #: audio sample key
    label: str = ""


@dataclass
class Event:
    """One line of the run journal."""

    seq: int
    t: float             #: monotonic seconds since run origin
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
    wall: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(line: str) -> "Event":
        d = json.loads(line)
        return Event(
            seq=d["seq"], t=d["t"], kind=d["kind"], detail=d.get("detail", {}), wall=d.get("wall", 0.0)
        )


def median(xs: Iterable[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        raise ValueError("median of empty sequence")
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def mad(xs: Iterable[float], center: float | None = None) -> float:
    """Median absolute deviation -- robust spread, immune to the one trial
    where the user went to get coffee."""
    xs = list(xs)
    if not xs:
        return 0.0
    c = median(xs) if center is None else center
    return median([abs(x - c) for x in xs])
