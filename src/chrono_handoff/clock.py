"""Precise clock and deadline scheduler.

The whole project rests on one primitive: *fire a non-blocking callback at an
absolute monotonic instant, with sub-millisecond error, without the callback
blocking the next deadline.*

Everything else (cue grid, windowed actions, timeouts) is built on top.

Measured on Windows 11 / CPython 3.13:
    time.perf_counter() resolution ....... 0.1 us
    time.sleep(0.001) overshoot .......... 0.62 ms median
    spin-wait to deadline ................ 0.001 ms median, 0.003 ms max
    winsound.PlaySound(file, ASYNC) ...... 0.25 ms call, non-blocking
    winsound.Beep(880, 50) ............... blocks 61.6 ms  <-- banned here
"""

from __future__ import annotations

import heapq
import threading
from collections import deque
import time
from dataclasses import dataclass, field
from typing import Callable

#: The one clock the whole process agrees on.  Monotonic: NTP / DST / manual
#: clock changes cannot make a deadline move.  Never use time.time() for
#: scheduling in this codebase.
CLOCK: Callable[[], float] = time.perf_counter

#: Below this remaining time we stop sleeping and burn CPU to land exactly.
#: Windows sleep granularity is ~0.5-15 ms, so sleeping the last millimetres
#: would destroy the whole point of the project.
SPIN_THRESHOLD_S = 0.002

#: Coarse sleep used while far from the deadline (we still re-check often so
#: a newly scheduled earlier deadline cannot be missed for long).
COARSE_SLEEP_S = 0.0005

#: CPython's GIL hand-off interval.  This is a hard ceiling on scheduling
#: precision: if any other Python thread is CPU-bound, the scheduler cannot run
#: until the interpreter switches.  Measured on this machine, 2 CPU-saturating
#: threads alongside the scheduler, 50 ms grid:
#:
#:     switchinterval    median offset    worst offset
#:         5.0 ms          10.455 ms        29.497 ms
#:         1.0 ms           9.014 ms        43.138 ms
#:         0.5 ms           0.260 ms         4.905 ms
#:         0.1 ms           0.253 ms         5.171 ms
#:
#: So the default 5 ms timeslice turns sub-millisecond scheduling into
#: 10-30 ms scheduling.  Dropping to 0.5 ms recovers essentially all of it.
#: Call :func:`tune_interpreter` once at conductor start; keep heavy CPU work
#: in other processes, not in other threads of this one.
#:
#: **The honest floor.**  Measured over 300 beats at 20 ms, with a *no-op*
#: callback (so nothing of ours is in the measurement):
#:
#:     median 0.019 ms | p90 0.055 ms | p99 1.808 ms | max 3.136 ms
#:     2.7% of firings exceed 1 ms
#:
#: The tail survives with an empty callback, which proves it is interpreter/OS
#: level (GIL hand-off, gen-2 GC, the Windows scheduler) and not caused by
#: anything the conductor does.  Adding a queue-push for the journal changed
#: p90 from 0.055 to 0.075 ms -- i.e. our own work is not the bottleneck.
#:
#: Report the median *and* a tail percentile.  A timer that is 0.019 ms typical
#: and 2 ms at p99 is a different instrument from one that is uniformly
#: sub-millisecond, and quoting only the median would hide that.  Both numbers
#: are, needless to say, two orders of magnitude below the human's ~50 ms.
GIL_SWITCH_INTERVAL_S = 0.0005


def tune_interpreter(freeze_gc: bool = True) -> float:
    """Reduce interpreter-level sources of scheduling jitter.

    Two known contributors, both measured rather than assumed:

    1. **GIL hand-off** (see :data:`GIL_SWITCH_INTERVAL_S`): the default 5 ms
       timeslice degrades sub-millisecond scheduling to 10-30 ms when another
       thread is CPU-bound.
    2. **Garbage collection**: full (generation-2) collections are the only GC
       pauses that can exceed a millisecond, and they show up as a sporadic
       multi-millisecond tail while the *median* stays at ~0.016 ms.  Measured
       on a 40-beat sample: max 6.4 ms with GC on vs 2.5 ms with automatic GC
       off.

    ``gc.freeze()`` moves all currently-live objects into a permanent
    generation that is never re-scanned, which shortens every subsequent full
    collection.  It is the standard mitigation for latency-sensitive Python
    services.  We do **not** disable GC: leaking a long-lived daemon to buy
    two milliseconds would be a bad trade.

    Returns the previous GIL switch interval.  Idempotent and cheap.
    """
    import gc
    import sys

    old = sys.getswitchinterval()
    if old > GIL_SWITCH_INTERVAL_S:
        sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)
    if freeze_gc:
        try:
            gc.freeze()
        except Exception:
            pass
    return old


def now() -> float:
    """Current monotonic instant, in seconds."""
    return CLOCK()


def wait_until(deadline: float, spin_threshold: float = SPIN_THRESHOLD_S) -> float:
    """Block until ``deadline`` (monotonic).  Returns the actual landing time.

    Sleeps coarsely while far away, then spin-waits.  Never raises on a
    deadline in the past -- returns immediately.
    """
    while True:
        remaining = deadline - CLOCK()
        if remaining <= 0:
            return CLOCK()
        if remaining > spin_threshold:
            time.sleep(min(COARSE_SLEEP_S, remaining - spin_threshold))
        else:
            break
    while CLOCK() < deadline:
        pass  # deliberate spin: this is the precision-critical last 2 ms
    return CLOCK()


@dataclass(order=True)
class _Job:
    deadline: float
    seq: int
    fn: Callable[[float], None] = field(compare=False)
    every: float | None = field(default=None, compare=False)
    name: str = field(default="", compare=False)
    #: Beats still owed, including the one about to fire.  ``None`` = unlimited.
    remaining: int | None = field(default=None, compare=False)
    cancelled: bool = field(default=False, compare=False)
    #: Beats dropped just before this firing because we were more than one
    #: interval late.  Dropped, never burst -- a stalled process must not
    #: machine-gun the cue at the human.
    skipped: int = field(default=0, compare=False)


@dataclass
class Firing:
    """A single execution of a job, as observed by the scheduler."""

    name: str
    at: float          #: actual landing instant (monotonic)
    offset: float      #: at - deadline, i.e. lateness in seconds
    executed_s: float  #: how long the callback took
    skipped: int = 0   #: beats dropped just before this firing


class Scheduler:
    """A dedicated thread that fires jobs on absolute monotonic deadlines.

    Callbacks run *on* the scheduler thread, so they must be short and
    non-blocking.  Anything audible/visible must be fire-and-forget (e.g.
    ``PlaySound(..., ASYNC)``); if you block in a callback you delay the
    next beat, which is exactly the failure this module exists to prevent.

    Every firing is recorded so latency could be audited after the fact --
    the conductor turns these into ``firing`` events.
    """

    def __init__(self, name: str = "sched") -> None:
        self.name = name
        self._heap: list[_Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._seq = 0
        self._on_firing: Callable[[Firing], None] | None = None
        #: Audit trail of every firing, bounded: a daemon that runs for days
        #: must not grow its own memory until it dies.
        self.firings: deque[Firing] = deque(maxlen=20000)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Scheduler":
        if self._thread is not None:
            return self
        tune_interpreter()
        self._thread = threading.Thread(target=self._run, name=f"chrono-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self, join: bool = True, timeout: float = 1.0) -> None:
        self._stop = True
        self._wake.set()
        if join and self._thread is not None:
            self._thread.join(timeout=timeout)

    def __enter__(self) -> "Scheduler":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def on_firing(self, fn: Callable[[Firing], None]) -> None:
        """Register an observer called after every firing (same thread)."""
        self._on_firing = fn

    # -- scheduling --------------------------------------------------------

    def at(self, deadline: float, fn: Callable[[float], None], name: str = "") -> _Job:
        """Run ``fn(deadline)`` at an absolute monotonic ``deadline``."""
        with self._lock:
            self._seq += 1
            job = _Job(deadline, self._seq, fn, None, name or f"job{self._seq}")
            heapq.heappush(self._heap, job)
        self._wake.set()
        return job

    def after(self, delay: float, fn: Callable[[float], None], name: str = "") -> _Job:
        """Run ``fn`` ``delay`` seconds from now."""
        return self.at(now() + delay, fn, name)

    def grid(
        self,
        first: float,
        every: float,
        fn: Callable[[float], None],
        count: int | None = None,
        name: str = "",
    ) -> _Job:
        """Recurring job on a **drift-free absolute grid**.

        Beat ``k`` lands at ``first + k*every`` -- re-derived from the grid
        origin, not accumulated from the previous firing, so a late beat never
        pushes the whole remaining grid late.  If the process stalls for more
        than one interval, missed beats are skipped and counted, never
        replayed in a burst.
        """
        with self._lock:
            self._seq += 1
            job = _Job(first, self._seq, fn, every, name or f"grid{self._seq}", remaining=count)
            heapq.heappush(self._heap, job)
        self._wake.set()
        return job

    def cancel(self, job: _Job | None) -> None:
        if job is not None:
            job.cancelled = True

    def cancel_all(self) -> None:
        with self._lock:
            for job in self._heap:
                job.cancelled = True

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop:
            with self._lock:
                self._heap = [j for j in self._heap if not j.cancelled]
                heapq.heapify(self._heap)
                job = self._heap[0] if self._heap else None
            if job is None:
                self._wake.wait(0.05)
                self._wake.clear()
                continue

            if not self._wait_for_top(job):
                continue          # newer, earlier work arrived -- re-evaluate
            if self._stop:
                return

            # Pop every job that is due now (normally exactly one).
            due: list[_Job] = []
            with self._lock:
                while self._heap and self._heap[0].deadline <= CLOCK():
                    j = heapq.heappop(self._heap)
                    if not j.cancelled:
                        due.append(j)
            for j in due:
                self._fire(j)

    def _wait_for_top(self, job: _Job) -> bool:
        """Wait for ``job.deadline`` without ever sleeping through new work.

        The naive version -- ``wait_until(heap[0].deadline)`` -- has a failure
        mode that would silently destroy the whole experiment: if the top job is
        far in the future, any job submitted *later with an earlier deadline*
        has to wait for that far deadline before it is even looked at.  Beats
        then fire seconds late (observed: 3.8 s) or, worse, arrive in a burst.

        So the wait is sliced.  Above the spin threshold we re-inspect the heap
        every couple of milliseconds; below it we spin, because that is where
        the precision actually comes from.  Worst-case latency for newly
        submitted work is therefore one slice (~2 ms), not the horizon of
        whatever job happened to be on top.
        """
        while True:
            if self._stop:
                return False
            remaining = job.deadline - CLOCK()
            if remaining <= SPIN_THRESHOLD_S:
                break
            time.sleep(min(0.002, remaining - SPIN_THRESHOLD_S))
            with self._lock:
                top = self._heap[0] if self._heap else None
            if top is not job:
                return False
        wait_until(job.deadline)
        return True

    def _fire(self, job: _Job) -> None:
        at = CLOCK()
        offset = at - job.deadline
        try:
            job.fn(job.deadline)
        except Exception as exc:  # a broken callback must not kill the clock
            # A marker for the failed execution, not a beat: it carries no drop count,
            # which belongs to the firing below -- the one that is being scheduled.
            self.firings.append(Firing(job.name, at, offset, -1.0, 0))
            _report_broken_callback(self.name, job.name, exc)
        executed = CLOCK() - at
        # Advance the grid *before* recording this firing.  `_reschedule` is what
        # decides how many beats this firing dropped, so recording first attributed
        # the *previous* firing's drops to this one -- and when the grid ended here
        # (its remaining count ran out) the last drop was never recorded at all.
        # Measured on a loaded macOS runner as `fired=3 dropped=0 accounted=False`
        # for a run that had dropped 7 of its 10 beats, and reproduced locally with a
        # 240ms stall.
        if job.every is not None and not job.cancelled:
            self._reschedule(job, at)
        firing = Firing(job.name, at, offset, executed, job.skipped)
        self.firings.append(firing)
        if self._on_firing is not None:
            try:
                self._on_firing(firing)
            except Exception:
                pass
        job.skipped = 0

    def _reschedule(self, job: _Job, fired_at: float) -> None:
        """Advance a recurring job on its absolute grid.

        The next deadline is derived from ``deadline + every``, never from
        ``fired_at``: a single late firing therefore does not shift every
        subsequent beat (no accumulated drift).

        If we are more than one interval late, the missed beats are **dropped
        and counted**, not replayed.  Machine-gunning a burst of beeps at a
        human who was briefly distracted is worse than skipping them: it
        destroys the grid's meaning as a temporal reference.
        """
        every = job.every
        assert every is not None
        nxt = job.deadline + every
        behind = 0
        if nxt <= fired_at:
            behind = int((fired_at - nxt) // every) + 1
        if job.remaining is not None:
            # `behind` comes from the lateness alone, so a stall longer than the whole
            # grid would count beats that were never going to fire (measured:
            # `dropped=12` for a grid that had 8 beats left).  A beat that did not
            # exist was not dropped, and the count has to stay true.
            behind = min(behind, max(0, job.remaining - 1))
        nxt += behind * every
        job.skipped = behind
        if job.remaining is not None:
            job.remaining -= 1 + behind
            if job.remaining <= 0:
                job.remaining = 0
                return
        with self._lock:
            job.deadline = nxt
            if not job.cancelled:
                heapq.heappush(self._heap, job)
        self._wake.set()


def _report_broken_callback(thread: str, job: str, exc: BaseException) -> None:
    """A bug in a cue callback must never kill the clock -- report and carry
    on, because the *next* beat still has to land on time."""
    import sys

    print(f"[chrono/{thread}] callback {job!r} raised {type(exc).__name__}: {exc}", file=sys.stderr)


def offset_percentile(offsets_s: list[float], pct: float) -> float:
    """Percentile of a list of offsets (seconds).  ``pct`` in [0, 100]."""
    if not offsets_s:
        return float("nan")
    xs = sorted(offsets_s)
    idx = min(len(xs) - 1, max(0, int(round(pct / 100.0 * len(xs))) - 1))
    return xs[idx]


def drift_report(firings: list[Firing]) -> dict[str, float]:
    """Summarise scheduling accuracy (seconds -> milliseconds).

    Reports the **tail**, not just the median.  A timer whose median is
    0.016 ms and whose p99 is 4 ms is a different instrument from one that is
    uniformly sub-millisecond, and only reporting the median would hide that.
    """
    good = [f for f in firings if f.executed_s >= 0]
    if not good:
        return {"n": 0}
    offs = [f.offset for f in good]
    execs = sorted(f.executed_s for f in good)
    return {
        "n": float(len(good)),
        "offset_median_ms": offset_percentile(offs, 50) * 1e3,
        "offset_p90_ms": offset_percentile(offs, 90) * 1e3,
        "offset_p99_ms": offset_percentile(offs, 99) * 1e3,
        "offset_max_ms": max(offs) * 1e3,
        "offset_min_ms": min(offs) * 1e3,
        "exec_max_ms": (execs[-1] if execs else 0.0) * 1e3,
    }
