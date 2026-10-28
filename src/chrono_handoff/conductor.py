"""The Conductor: the fast controller that owns the clock.

This is the component the whole project exists to introduce.  Its defining
property is negative: **it never blocks waiting for the planner.**  There is no
code path here that waits on an LLM, an HTTP call to a model, or a tool call.
Plans arrive out of band; from that moment on the timing is entirely local.

Structure of a run, in the conductor's own clock (``t = 0`` is the cue origin,
i.e. the instant the countdown starts -- *not* the instant a tool call was
received):

    t=0 ──────────────────────────── zero_at ─────────── window ────── deadline
     │  ticks every ioi_ms            │  ACCENT          │  sweep        │
     │                                │                  │               │
     └── human sees the grid ─────────┴─ human fires ────┴─ machine acts ┴─ verdict

Three independent clocks are deliberately kept separate:

* **cue grid** -- anchored so the *zero beat* lands at ``target - offset``.  The
  measured human offset moves this, and only this.
* **machine window** -- anchored to the physical target.  A human who is
  habitually late must never delay a relay.  (calibrate.py rule 5.)
* **wall-clock journal** -- events carry both monotonic ``t`` and wall time, so
  the log is human-readable *and* analytically exact.

The scheduler thread only ever runs non-blocking callbacks; anything that might
block (serial reads, http posts) is polled from its own thread.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import CONTRACT_VERSION, __version__
from .calibrate import Calibration, CalibrationStore, task_signature
from .clock import CLOCK, Scheduler, now
from .cue import AudioSink, BellAudio, MultiVisual, VisualCue, default_audio
from .effectors import Effector, LogEffector, make_effector
from .home import journal_dir as journal_dir_default
from .inputs import (
    DEFAULT_KEY_LATENCY_S,
    InputSource,
    Observation,
    analyse_offsets,
    make_source,
)
from .model import (
    TERMINAL,
    Beat,
    CuePlan,
    Event,
    RunStatus,
    SignalSpec,
    WindowSpec,
)

__all__ = ["Conductor", "Run"]


class JournalWriter:
    """Appends events to per-run JSONL files from its own thread.

    Rationale, stated carefully because the obvious story turns out to be
    wrong.  A first measurement showed the beat callback taking up to 7.9 ms
    with a synchronous ``open()/write()/close()`` per event, which looked like
    the smoking gun.  It was not: ``executed_s`` measures wall time across the
    callback, so it includes any moment the thread was preempted, and a
    controlled re-measurement over 300 beats showed the multi-millisecond tail
    **survives with a completely empty callback**.  The tail is interpreter and
    OS level (see :data:`chrono_handoff.clock.GIL_SWITCH_INTERVAL_S`), not I/O.

    So this class is not a fix for a measured bug.  It is kept because placing
    disk I/O on the thread that owns the clock is wrong *by construction*: it
    makes scheduling jitter depend on disk latency, antivirus, and whether the
    journal happens to live on a network share -- failure modes that are absent
    from a clean benchmark and present on a real machine.  Cost of the
    indirection is ~0.02 ms at p90.

    The lesson recorded here: measure the *cause*, not the correlated timing.
    """

    def __init__(self, directory: Path, enabled: bool = True) -> None:
        self.dir = Path(directory)
        self.enabled = enabled
        self._q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._handles: dict[str, Any] = {}
        self.written = 0
        self.errors = 0

    def start(self) -> "JournalWriter":
        if not self.enabled or self._thread is not None:
            return self
        self.dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="chrono-journal", daemon=True)
        self._thread.start()
        return self

    def write(self, run_id: str, line: str) -> None:
        """Non-blocking.  Called from the scheduler thread."""
        if self.enabled:
            self._q.put((run_id, line))

    def _run(self) -> None:
        while not self._stop or not self._q.empty():
            try:
                run_id, line = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                fh = self._handles.get(run_id)
                if fh is None:
                    fh = open(self.dir / f"{run_id}.jsonl", "a", encoding="utf-8")
                    self._handles[run_id] = fh
                fh.write(line + "\n")
                fh.flush()
                self.written += 1
            except Exception:
                self.errors += 1

    def close(self, run_id: str | None = None) -> None:
        if run_id is None:
            self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        for key in [run_id] if run_id else list(self._handles):
            fh = self._handles.pop(key, None)
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass


#: How long a stand-down waits before tearing down the loop that is answering
#: the request which asked for it (see `Conductor.stand_down`).
_STAND_DOWN_GRACE_S = 0.25


@dataclass
class Run:
    """One submitted plan plus everything that happened to it."""

    id: str
    plan: CuePlan
    origin: float | None = None
    zero_at: float | None = None
    target_at: float | None = None
    status: RunStatus = RunStatus.PENDING
    events: list[Event] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)
    signal_seen: bool = False
    first_signal_t: float | None = None
    human_offset_ms: float | None = None
    verify_seen: bool = False
    actions_fired: int = 0
    cue_audited: bool = False
    ended_at: float | None = None
    notes: list[str] = field(default_factory=list)
    #: Plain-language reading of the terminal status, filled by ``_finalize``.
    #:
    #: ``status`` is a state machine and has to stay stable; ``verdict`` is what
    #: it *means for the human reading it*.  They are not the same word: a cue
    #: that asked nobody for anything and simply ran out its ``duration_s`` ends
    #: ``timeout`` -- which in English reads as a failure and was duly mistaken
    #: for one, even though the whole cue had gone exactly to plan.
    verdict: str = ""
    #: Why the run ended, straight from ``_finalize`` (``human_signal``,
    #: ``no_signal``, ``panel_cancel``...).  Kept because ``verdict`` is coarse
    #: and the reason is the part a caller acts on.
    verdict_reason: str = ""
    #: Set for a queued run: it is waiting for the run ahead of it to reach a
    #: terminal state before its clock may start.  A queued run is ``PENDING``
    #: and must not be handed a schedule -- two live grids would be two cues
    #: competing for the same pair of eyes and the same beat count.
    queued_behind: str | None = None
    _seq: int = 0

    # runtime handles (not serialised)
    source: InputSource | None = field(default=None, repr=False, compare=False)
    effector: Effector | None = field(default=None, repr=False, compare=False)
    window_job: Any = field(default=None, repr=False, compare=False)
    deadline_job: Any = field(default=None, repr=False, compare=False)
    beat_job: Any = field(default=None, repr=False, compare=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    # -- journal -----------------------------------------------------------

    def add(self, kind: str, **detail: Any) -> Event:
        self._seq += 1
        t = 0.0 if self.origin is None else CLOCK() - self.origin
        ev = Event(seq=self._seq, t=t, kind=kind, detail=detail)
        self.events.append(ev)
        return ev

    @property
    def rel(self) -> float:
        return 0.0 if self.origin is None else CLOCK() - self.origin

    def absolute(self, rel_s: float) -> float:
        assert self.origin is not None
        return self.origin + rel_s

    def snapshot(self, include_events: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.id,
            "status": self.status.value,
            # Read `verdict` before `status` when you want to know whether this
            # went to plan.  `status: "timeout"` on a cue that never asked for
            # a signal is the *successful* path.
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "instruction": self.plan.instruction,
            "origin_mono": self.origin,
            "zero_at_mono": self.zero_at,
            "target_at_mono": self.target_at,
            "t_rel": None if self.origin is None else round(self.rel, 6),
            "signal_seen": self.signal_seen,
            "verify_seen": self.verify_seen,
            "human_offset_ms": None if self.human_offset_ms is None else round(self.human_offset_ms, 1),
            "actions_fired": self.actions_fired,
            "beats_total": len(self.beats),
            "queued_behind": self.queued_behind,
            "notes": self.notes,
            "meta": self.plan.meta,
            # Which source owns the evidence.  CuePanel.press needs it to decide
            # whether a panel click is a measurement or just a note, and a
            # display client has no other way to learn it from a snapshot.
            "expect_source": (self.plan.expect.source if self.plan.expect is not None else "none"),
        }
        if self.events:
            d["last_seq"] = self.events[-1].seq
        if include_events:
            d["events"] = [asdict(e) for e in self.events]
        return d


class Conductor:
    """Owns the clock, the cue channel, the input sources and the journal.

    One Conductor can host several runs (sequentially or overlapping); it is
    designed to be a long-lived daemon, not a per-request object.  It holds no
    reference to any planner, model or transport.
    """

    def __init__(
        self,
        journal_dir: Path | str | None = None,
        audio: str | AudioSink = "auto",
        visual: bool | VisualCue = True,
        calibration: CalibrationStore | None = None,
        poll_interval_ms: float = 2.0,
        key_latency_s: float = DEFAULT_KEY_LATENCY_S,
        verbose: bool = False,
        journal_enabled: bool = True,
    ) -> None:
        self.runs: dict[str, Run] = {}
        self._order: deque[str] = deque(maxlen=256)
        self._lock = threading.RLock()
        self.scheduler = Scheduler("cue")
        self.calibration = calibration or CalibrationStore()
        self.key_latency_s = key_latency_s
        self.verbose = verbose
        self.poll_interval_ms = poll_interval_ms

        self.journal_dir = Path(journal_dir) if journal_dir else journal_dir_default()
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.journal = JournalWriter(self.journal_dir, enabled=journal_enabled)

        self.audio: AudioSink = audio if not isinstance(audio, str) else default_audio(audio)
        self.visual: VisualCue | MultiVisual = (
            visual if isinstance(visual, VisualCue) else VisualCue(enabled=bool(visual))
        )
        # A run may request a different visual style than the conductor default;
        # applied at run arm time so one conductor can serve A/B comparisons.
        self.default_visual_style = getattr(self.visual, "style", "marker")

        self._subscribers: list[Callable[[Run, Event], None]] = []
        self._observation_sink: Callable[[Run, Observation], None] | None = None
        #: Armed-cue observers.  A LIST, deliberately: more than one consumer
        #: needs this event (the HTTP panel pulls the schedule, and the native
        #: window raises itself).  It was a single slot before, so whoever
        #: registered last silently replaced the other -- the panel stopped
        #: receiving schedules the moment the window subscribed, and the page
        #: looked merely idle while a cue was actually running.
        self._armed_sinks: list[Callable[[Run], None]] = []
        self._panel_opener: Callable[[str], None] | None = None
        #: Set by `stand_down`: no new work is accepted, and whatever owns this
        #: process's event loop has been asked to leave (see `on_stand_down`).
        self._standing_down = False
        self._stand_down_hook: Callable[[], None] | None = None
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._started = False
        self._scheduler_firings_offset: list[float] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Conductor":
        if self._started:
            return self
        self._started = True
        self.scheduler.start()
        if isinstance(self.visual, VisualCue):
            self.visual.start()
        self.scheduler.on_firing(self._on_firing)
        self.journal.start()
        self._poller = threading.Thread(target=self._poll_loop, name="chrono-poller", daemon=True)
        self._poller.start()
        return self

    def stop(self, terminate_runs: bool = True) -> None:
        self._stop.set()
        if self._poller is not None:
            self._poller.join(timeout=1.0)
        if terminate_runs:
            # Detach the queue *first*.  `_finalize` releases a queued successor
            # so that a normal end hands over cleanly; during shutdown that would
            # arm a fresh grid in a process that is already leaving.
            for run in list(self.runs.values()):
                run.queued_behind = None
            for run in list(self.runs.values()):
                if run.status not in TERMINAL:
                    self._finalize(run, RunStatus.ABORTED, reason="conductor_shutdown")
        # Drop pending work *before* stopping the scheduler: a job left on the
        # heap would fire arbitrarily late during shutdown and pollute the
        # scheduling-accuracy audit that the M1 verdict reads.
        self.scheduler.cancel_all()
        self.scheduler.stop()
        # Scheduler is down, so no new events can be queued: safe to drain.
        self.journal.close()
        if isinstance(self.visual, VisualCue):
            self.visual.stop()
        self.audio.close()
        for run in self.runs.values():
            self._close_run_io(run)

    def __enter__(self) -> "Conductor":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- hooks -------------------------------------------------------------

    def subscribe(self, fn: Callable[[Run, Event], None]) -> None:
        self._subscribers.append(fn)

    def on_observation(self, fn: Callable[[Run, Observation], None]) -> None:
        """External hook (used by the MCP bridge to forward signals)."""
        self._observation_sink = fn

    def on_armed(self, fn: Callable[[Run], None]) -> None:
        """Subscribe to "a run's cue grid was resolved to absolute times".

        Fires *before* the first beat sounds (there is always a lead-in), which
        is what makes it possible to hand the whole schedule to the UI in one
        shot instead of streaming beats -- streaming would add IPC + render
        latency to every beat, i.e. exactly the jitter this project exists to
        eliminate.

        Multiple observers are supported and none can silently replace another.
        """
        self._armed_sinks.append(fn)

    # -- wall clock <-> monotonic clock -----------------------------------

    def wall_at(self, mono_t: float) -> float:
        """Convert a monotonic instant into a UNIX epoch timestamp.

        The client lives in wall-clock land (JavaScript only has epoch time),
        so the handoff has to bridge the two domains.  The mapping is sampled
        once per call rather than at process start, so a system clock change
        during a long daemon life cannot silently skew every schedule.
        """
        return time.time() + (mono_t - CLOCK())

    def mono_from_wall(self, wall_t: float) -> float:
        """Inverse of :meth:`wall_at`, used to bring client timestamps back."""
        return CLOCK() + (wall_t - time.time())

    def _emit(self, run: Run, kind: str, **detail: Any) -> Event:
        ev = run.add(kind, **detail)
        if self.verbose:
            print(f"[chrono {run.id}] t+{ev.t:7.3f}  {kind}  {json.dumps(detail, ensure_ascii=False)}", flush=True)
        for fn in list(self._subscribers):
            try:
                fn(run, ev)
            except Exception:
                pass
        self._write_journal(run, ev)
        return ev

    def _write_journal(self, run: Run, ev: Event) -> None:
        path = self.journal_dir / f"{run.id}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(ev.to_json() + "\n")
        except Exception:
            pass

    def _on_firing(self, firing: Any) -> None:
        self._scheduler_firings_offset.append(firing.offset)

    # -- submit ------------------------------------------------------------

    def submit(self, plan: CuePlan, paused: bool = False, queue: bool = True) -> Run:
        """Register a plan.  Returns immediately; nothing here blocks.

        ``paused=True`` leaves the run in ``PENDING`` with no clock running, so
        a human can get into position before the grid starts.  Call
        :meth:`go` to start it.  This matters experimentally: a subject who
        starts the countdown by themselves is not the same measurement as one
        who is told when to expect it.

        ``queue=True`` (the default) does not start this run while another grid
        is still sounding: it is registered as ``PENDING`` with ``queued_behind``
        set, and the conductor starts it once that run's stimulus has finished.
        Without this, two cues submitted back to back both went live, and the
        second silently overwrote the first on the panel -- the human saw one grid
        while two clocks were running.  ``queue=False`` is the caller's explicit
        "run alongside".
        """
        if self._standing_down:
            raise RuntimeError(
                "this conductor is standing down for an update and is not accepting new cues"
            )
        # Fail fast on the two ways an absolute target goes wrong, instead of
        # letting them become a plausible-looking cue.
        if plan.target_at is not None and plan.target_at > 1e9:
            raise ValueError(
                "target_at is a MONOTONIC instant on the conductor's own clock; "
                f"{plan.target_at!r} looks like an epoch timestamp.  Pass target_at_wall "
                "(epoch seconds) for a wall-clock deadline."
            )
        if plan.target_at_wall is not None and plan.target_at_wall <= time.time():
            raise ValueError(
                f"target_at_wall {plan.target_at_wall!r} is already in the past "
                f"(now {time.time():.3f}): a cue cannot be anchored to an instant that has gone"
            )
        if not plan.id:
            plan.id = f"run_{uuid.uuid4().hex[:8]}"
        run = Run(id=plan.id, plan=plan)

        # --- input source: opened and *listening* before anything can happen
        spec_source = (plan.expect.source if plan.expect else None) or plan.meta.get("source") or "console_key"
        source_kw = dict(plan.expect.opts) if plan.expect else {}
        if spec_source == "console_key":
            source_kw.setdefault("keys", "\r\n ")
            source_kw.setdefault("echo", False)
        try:
            run.source = make_source(spec_source, **source_kw)
            run.source.open()
        except Exception as exc:
            run.source = None
            run.notes.append(f"source {spec_source} unavailable: {type(exc).__name__}: {exc}")
            self._emit(run, "source_unavailable", source=spec_source, error=str(exc))
        else:
            self._emit(run, "source_armed", source=spec_source, detail=run.source.describe())

        # --- effector
        eff_kind = plan.meta.get("effector", "log")
        eff_kw = plan.meta.get("effector_opts", {})
        try:
            run.effector = make_effector(eff_kind, **eff_kw)
            if plan.on_window is not None:
                for action in plan.on_window.actions:
                    run.effector.arm(action)
        except Exception as exc:
            run.notes.append(f"effector {eff_kind} unavailable: {exc}")
            run.effector = LogEffector()
            self._emit(run, "effector_fallback", requested=eff_kind, error=str(exc))

        with self._lock:
            self.runs[run.id] = run
            self._order.append(run.id)

        self._emit(
            run,
            "submitted",
            instruction=plan.instruction,
            lead_s=plan.lead_s,
            ioi_ms=plan.ioi_ms,
            audio=plan.audio,
            visual=plan.visual,
            target_in_s=plan.target_in_s,
            offset_s=plan.offset_s,
            duration_s=plan.duration_s,
            version=__version__,
        )
        if not paused:
            # Only a plan that puts a grid in front of a person can be talked
            # over, so only such a plan queues.  A machine-only plan (no audio,
            # no visual) never blocks a cue and is never blocked by one -- which
            # is what makes `queue=True` safe as a default on the declarative
            # path too.
            human_facing = bool(plan.audio or plan.visual)
            # `queue` travels in meta as well as in the argument, so the decision
            # survives the paths that only carry a plan: `submit(paused=True)`
            # followed by `go()`, the panel's prepare/start pair, and the IPC
            # `submit` method.  A caller that says "run alongside" must not be
            # second-guessed by the start button it presses a moment later.
            plan.meta.setdefault("queue", bool(queue))
            behind = self._tail_run_id(exclude=run.id) if (queue and human_facing) else None
            if behind is not None:
                self._wait_behind(run, behind, "another cue is still running")
            else:
                self.go(run.id)
        return run

    def _wait_behind(self, run: Run, behind_id: str, reason: str) -> None:
        """Register ``run`` as queued behind ``behind_id``, and arm its release.

        The release normally comes from the predecessor's own ``_finalize``.  But
        a predecessor that is **already terminal while its grid still pulses**
        will never finalize again, and the job its finalize armed fired before
        this run existed -- so nothing would ever look for it.  Measured: a cue
        submitted just after another was cancelled sat ``PENDING`` forever.
        """
        run.queued_behind = behind_id
        self._emit(run, "queued", behind=behind_id, reason=reason)
        prev = self.runs.get(behind_id)
        if prev is not None and prev.status in TERMINAL:
            self._release_queue(prev)

    def _tail_run_id(self, exclude: str = "") -> str | None:
        """The last human-facing run that is still worth waiting for.

        Deliberately NOT "a run in RUNNING state": a run reaches a terminal
        verdict the moment the human answers, while its tail keeps pulsing for
        seconds afterwards.  Queuing on the *status* therefore let a second grid
        start while the first one's tail was still sounding -- two beat trains at
        once, which is the thing this queue exists to prevent.  The end of the
        stimulus is what matters, so that is what is checked.

        ``exclude`` is the run being submitted or started: it is already in
        ``self.runs`` and would otherwise answer "yes, wait for yourself".  Only
        runs submitted **before** it are considered -- a cue that arrived later is
        a successor, not something to wait for, and treating it as one deadlocked
        a three-cue chain: B released from A looked forward, found C waiting, and
        queued behind its own successor.

        A run that is *already queued* still counts as a predecessor.  Skipping
        those turned a chain into a fan-in: with A running and B queued behind A,
        a third cue C looked past B (it had ``queued_behind`` set) and queued
        behind A as well -- and once A ended only one of the two was released,
        leaving the other PENDING for good.
        """
        with self._lock:
            order = list(self._order)
            cutoff = order.index(exclude) if exclude in order else len(order)
            for rid in reversed(order[:cutoff]):
                run = self.runs.get(rid)
                if run is None:
                    continue
                if not (run.plan.audio or run.plan.visual):
                    continue
                if run.status in TERMINAL and not self._beats_pending(run):
                    continue
                return run.id
        return None

    def go(self, run_id: str, in_s: float = 0.0) -> Run:
        """Start the clock for a submitted (possibly paused) run.

        ``origin`` is taken **now** -- so the human-visible grid always starts
        from a fully settled state, never from the moment an RPC arrived.

        An explicit start still obeys the queue when the plan did not opt out of
        it.  This is the path a *human* takes -- the panel's "Start now" button,
        ``chrono window --paused``, the ``go`` IPC method -- and it used to bypass
        the queue entirely: pressing it on a prepared cue while another grid was
        running started both.  The person doing the pressing is exactly the one
        who would be confused by that.

        ``queue=False`` is the caller's explicit "run alongside"; it is carried in
        ``plan.meta`` so it survives the round trip through the plan schema (a
        bare ``go(run_id)`` has nowhere else to put it).
        """
        run = self._get(run_id)
        if run.status not in (RunStatus.PENDING,):
            return run
        if run.queued_behind is not None:
            # Asked to start while still queued: the answer is "not yet", and
            # saying so is the whole point -- starting it here would put two
            # grids in front of one person.
            return run
        if (run.plan.audio or run.plan.visual) and run.plan.meta.get("queue", True):
            behind = self._tail_run_id(exclude=run.id)
            if behind is not None:
                self._wait_behind(run, behind, "start pressed while another cue is running")
                return run

        def _start(_deadline: float) -> None:
            self._arm_run(run)

        if in_s > 0:
            run.notes.append(f"starting in {in_s:.1f}s")
            self.scheduler.after(in_s, _start, name=f"{run.id}:go")
            self._emit(run, "go_scheduled", in_s=in_s)
        else:
            _start(0.0)
        return run

    # -- run assembly ------------------------------------------------------

    def _arm_run(self, run: Run) -> None:
        plan = run.plan
        run.origin = CLOCK()

        if plan.target_at_wall is not None:
            # Wall clock -> our monotonic base, resolved *now* (i.e. when the grid
            # is armed, not when the cue was submitted): a queued cue keeps the
            # instant it asked for instead of sliding by however long it waited.
            target_in = plan.target_at_wall - time.time()
            if target_in < 0:
                # The instant passed while this cue was waiting for its turn.
                # Fail loudly.  It used to arm an empty grid (every beat already
                # in the past), fire `run_start`, and end `verdict: ok` -- a cue
                # nobody ever saw, reported as having gone to plan.
                self._emit(
                    run, "deadline_missed",
                    late_by_s=round(-target_in, 3),
                    target_at_wall=plan.target_at_wall,
                )
                self._finalize(run, RunStatus.ERROR, reason="deadline_missed")
                return
        else:
            target_in = plan.target_in_s
            if target_in is None:
                target_in = (plan.target_at - run.origin) if plan.target_at is not None else plan.lead_s + 1.0
        run.target_at = run.origin + target_in
        # The ONE place the human offset acts: it moves the zero beat only.
        run.zero_at = run.target_at - plan.offset_s

        self._emit(
            run,
            "cue_origin",
            target_in_s=round(target_in, 6),
            offset_s=plan.offset_s,
            zero_in_s=round(run.zero_at - run.origin, 6),
        )

        beats = self._build_grid(run)
        run.beats = beats
        if beats:
            self._emit(
                run,
                "beats_planned",
                count=len(beats),
                ioi_ms=plan.ioi_ms,
                first_in_s=round(beats[0].at - run.origin, 6),
                zero_in_s=round(run.zero_at - run.origin, 6),
            )

        if plan.audio or plan.visual:
            self._emit(run, "cue_armed", audio=plan.audio, visual=plan.visual, audio_sink=self.audio.name)
        # Hand the resolved schedule to any display client *now*, while there is
        # still a lead-in to spare.
        for sink in list(self._armed_sinks):
            try:
                sink(run)
            except Exception:
                pass
        if isinstance(self.visual, VisualCue) and plan.visual_style:
            self.visual.style = plan.visual_style
        if self.visual is not None:
            label = plan.instruction or "(no instruction)"
            self.visual.header(f"{label}\n  {len(beats)} beats @ {plan.ioi_ms:.0f} ms -- last one is GO")
            if plan.lead_s >= 1.0:
                self.visual.note("get ready...")

        # grid: one recurring job on an absolute, drift-free grid
        if beats:
            self._schedule_beats(run, beats)

        run.status = RunStatus.RUNNING
        self._emit(run, "run_start", beats=len(beats), source=run.source.describe() if run.source else "-")

        if plan.verify is not None:
            self._emit(run, "verify_armed", **self._signal_detail(plan.verify))
        if plan.on_window is not None:
            self._schedule_window(run, plan.on_window)

        deadline_rel = self._effective_deadline(run)
        run.deadline_job = self.scheduler.after(
            deadline_rel, lambda _d: self._on_deadline(run), name=f"{run.id}:deadline"
        )
        self._emit(run, "deadline_planned", at_s=round(deadline_rel, 6))

        # A synthetic source is armed against the ZERO beat, because that is
        # the instant the human was told to watch for.  A real source ignores
        # this: its timestamps come from the mechanism, not from us.
        if run.source is not None and hasattr(run.source, "arm"):
            try:
                run.source.arm(run.zero_at)  # type: ignore[attr-defined]
            except TypeError:
                pass

    def _signal_detail(self, spec: SignalSpec) -> dict[str, Any]:
        return {
            "source": spec.source,
            "pattern": spec.pattern,
            "timeout_s": spec.timeout_s,
            "interval_ms": spec.interval_ms,
        }

    def _build_grid(self, run: Run) -> list[Beat]:
        """Leader beats + the zero beat + **tail beats**.

        The tail is load-bearing, not cosmetic: entrained anticipation only
        appears when the pulse continues past the target.  With the target as
        the final beat the subject can only react to it, and the measurement
        silently becomes a reaction-time experiment.  See
        :attr:`chrono_handoff.model.CuePlan.tail_beats`.
        """
        plan = run.plan
        assert run.zero_at is not None and run.origin is not None
        ioi = max(0.02, plan.ioi_ms / 1000.0)
        n_tail = max(0, int(plan.tail_beats))

        # The countdown length is ``lead_s`` -- that is what the flag, the
        # settings slider and the docs all mean by it.  It used to be derived
        # from ``zero_at - origin`` (i.e. target_in_s), which silently ignored
        # lead_s: asking for an 8 s countdown before a target 12 s away produced
        # a 12 s countdown.  Anchor the grid on the GO beat and walk *backwards*
        # by lead_s, so both parameters do what they say.
        span = max(0.0, min(plan.lead_s, run.zero_at - run.origin))
        n_back = max(1, int(round(span / ioi)))

        beats: list[Beat] = []
        for k in range(n_back, -n_tail - 1, -1):
            at = run.zero_at - k * ioi
            if at < run.origin - 1e-9:
                continue
            accent = k == 0
            beats.append(
                Beat(
                    index=len(beats),
                    at=at,
                    accent=accent,
                    f_audio="accent" if accent else "tick",
                    label="GO" if accent else ("·" if k < 0 else ""),
                )
            )
        return beats

    def _schedule_beats(self, run: Run, beats: list[Beat]) -> None:
        # Grids are expressed relative to the origin, which _build_grid stamps
        # before calling us.  Assert it here rather than threading Optional
        # through every lattice computation below.
        assert run.origin is not None
        plan = run.plan
        total = len(beats)

        # The first beat must be in the *future*.  ``origin`` was stamped a few
        # events ago (including journal writes), so a grid starting at exactly
        # ``origin`` starts partly in the past and its leading beat fires late
        # -- measured at 13 ms, which is larger than everything else the
        # conductor does and would silently inflate the machine-error figure
        # that M1 uses to separate machine jitter from human jitter.
        #
        # Fix: start on the first **lattice point** that is still ahead of us.
        # The lattice is untouched (``zero_at`` stays exactly where it was, so
        # the human anchor does not move); we merely skip leading beats we
        # cannot honour.  Skipping a beat is harmless -- the human counts down
        # to the GO beat, not to the first tick.
        margin = 0.001
        start_idx = 0
        now_plus = CLOCK() + margin
        while start_idx < total - 1 and beats[start_idx].at < now_plus:
            start_idx += 1

        # A single recurring job keeps the grid drift-free (each firing is
        # re-derived from the grid origin, not accumulated).  The cursor is
        # per-run state captured in the closure: a conductor hosts many runs
        # and they must not share a beat index.
        cursor = [start_idx]

        def fire(_deadline: float) -> None:
            idx = cursor[0]
            cursor[0] += 1
            if idx >= total:
                return
            beat = beats[idx]
            # Stamp the landing offset FIRST, before any side effect.  Reading
            # the clock after the beep + visual + journal enqueue inflated the
            # recorded figure by up to 5 ms -- i.e. the journal was reporting
            # our own downstream work as if it were scheduling jitter, which is
            # precisely the number M1 uses to separate machine error from human
            # error.  Measure the thing you care about, then act.
            landed_ms = (CLOCK() - beat.at) * 1000.0
            if plan.audio:
                self.audio.play(beat.f_audio)
            if plan.visual:
                self.visual.beat(beat, total, total - 1 - idx)
            self._emit(
                run,
                "cue_zero" if beat.accent else "cue_beat",
                beat=beat.index,
                accent=beat.accent,
                scheduled_offset_ms=round(landed_ms, 3),
            )

        if start_idx:
            self._emit(
                run,
                "beats_leading_skipped",
                skipped=start_idx,
                reason="leading beats were already in the past when the grid was armed",
                first_kept_in_s=round(beats[start_idx].at - run.origin, 6),
            )
        first = beats[start_idx].at
        every = plan.ioi_ms / 1000.0
        run.beat_job = self.scheduler.grid(
            first, every, fire, count=total - start_idx, name=f"{run.id}:beats"
        )
        # Audit after the last beat has actually fired (the scheduler records a
        # firing only once its callback returns, so auditing inside the callback
        # would miss the final beat).
        last_at = first + (total - start_idx - 1) * every
        self.scheduler.at(
            last_at + 0.05, lambda _d: self._emit_cue_audit(run), name=f"{run.id}:audit"
        )

    def _schedule_window(self, run: Run, win: WindowSpec) -> None:
        assert run.origin is not None
        # Local binding, not ``run.origin``: ``open_window`` below runs later and
        # the assert's narrowing does not reach into a closure (the same trap the
        # beat grid hit).
        origin = run.origin
        start = origin + win.start_off_ms / 1000.0
        end = origin + win.end_off_ms / 1000.0
        interval = max(0.001, win.interval_ms / 1000.0)
        state = {"n": 0}

        def sweep(_deadline: float) -> None:
            if run.status in TERMINAL:
                return
            if CLOCK() > end:
                self._emit(run, "window_closed", reason="end", fired=state["n"])
                if run.window_job is not None:
                    self.scheduler.cancel(run.window_job)
                return
            if run.verify_seen and run.plan.meta.get("stop_window_on_verify", True):
                self._emit(run, "window_closed", reason="verified", fired=state["n"])
                self.scheduler.cancel(run.window_job)
                return
            if run.signal_seen and run.plan.meta.get("stop_window_on_signal", False):
                self._emit(run, "window_closed", reason="signal", fired=state["n"])
                self.scheduler.cancel(run.window_job)
                return
            idx = state["n"]
            state["n"] += 1
            for action in win.actions:
                try:
                    detail = run.effector.fire(action, idx) if run.effector else "no-effector"
                except Exception as exc:
                    detail = f"ERROR {type(exc).__name__}: {exc}"
                run.actions_fired += 1
                self._emit(run, "action_fired", index=idx, action=action.get("type", "?"), detail=detail)

        if start <= CLOCK():
            self._emit(run, "window_opened", at=round((CLOCK() - origin), 6), late_by_ms=round((CLOCK() - start) * 1000, 2))
            run.window_job = self.scheduler.grid(CLOCK(), interval, sweep, name=f"{run.id}:window")
        else:
            def open_window(_d: float) -> None:
                self._emit(run, "window_opened", at=round((CLOCK() - origin), 6), late_by_ms=round((CLOCK() - start) * 1000, 2))
                run.window_job = self.scheduler.grid(CLOCK(), interval, sweep, name=f"{run.id}:window")

            self.scheduler.at(start, open_window, name=f"{run.id}:window_open")

    def _effective_deadline(self, run: Run) -> float:
        """Seconds from origin at which the run must reach a verdict.

        Guarantees a terminal status even if the human walks away -- the
        "blocking wait" anti-pattern is not allowed to reappear as an
        unbounded run.

        A cue that expects **no observable signal** and has no machine work left
        after its grid is over when its last beat lands: nothing can arrive, so
        waiting out ``duration_s`` is pure silence.  That silence used to land in
        the *next* queued cue -- measured 12.0 s between two cues whose target was
        2 s, because ``duration_s=10`` was still being waited out long after the
        last beat (and ``chrono_cue`` defaults it to 30).  ``duration_s`` is the
        budget for *waiting for a signal*, not a cap on the stimulus: cutting a
        grid short because the budget is smaller would be worse than overrunning
        it, so the grid wins when they disagree.
        """
        # The origin is stamped before a deadline can be computed; binding it to
        # a local keeps the arithmetic below free of Optional.
        assert run.origin is not None
        origin = run.origin
        plan = run.plan
        observable = plan.expect is not None and plan.expect.source not in ("", "none")
        if (
            not observable
            and plan.on_window is None
            and plan.verify is None
            and run.zero_at is not None
        ):
            return max(0.0, run.zero_at - origin + plan.tail_beats * plan.ioi_ms / 1000.0 + 0.2)
        need = [plan.duration_s]
        # The tail must be allowed to finish even if nothing else constrains it.
        if run.zero_at is not None:
            need.append(run.zero_at - origin + plan.tail_beats * plan.ioi_ms / 1000.0 + 0.2)
        if plan.expect is not None:
            need.append((run.zero_at or origin) - origin + plan.expect.timeout_s)
        if plan.on_window is not None:
            need.append(plan.on_window.end_off_ms / 1000.0 + 4.0)
        if plan.verify is not None:
            need.append((plan.on_window.end_off_ms / 1000.0 if plan.on_window else 0.0) + plan.verify.timeout_s)
        return max(need)

    # -- observation loop --------------------------------------------------

    def _poll_loop(self) -> None:
        interval = self.poll_interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            time.sleep(interval)

    def poll_once(self) -> list[Observation]:
        """Read every active source once.  Public so tests and the MCP bridge
        can drive observation without owning the thread."""
        seen: list[Observation] = []
        with self._lock:
            active = [r for r in self.runs.values() if r.status == RunStatus.RUNNING and r.source is not None]
        for run in active:
            try:
                observations = run.source.poll()  # type: ignore[union-attr]
            except Exception as exc:
                self._emit(run, "source_error", error=f"{type(exc).__name__}: {exc}")
                continue
            for obs in observations:
                seen.append(obs)
                self._on_signal(run, obs)
        return seen

    def note_panel_press(self, run_id: str, wall_t: float | None = None) -> bool:
        """Journal that the operator clicked the panel, WITHOUT counting it as
        the evidence this run is waiting for.

        The panel is only the observation surface when the plan says so.  When
        a real sensor owns the evidence (a serial prompt, a console key), a
        click is still worth recording -- the human did something and the agent
        should be able to see it -- but letting it satisfy the run would swap
        the physical closed loop for a self-report.  Verified before this
        existed: a click on the panel satisfied a run that was waiting for
        ``PMON2020`` on a serial port that never answered.

        Returns True if the note was recorded against a live run.
        """
        run = self.get(run_id)
        if run is None or run.status in TERMINAL:
            return False
        t = self.mono_from_wall(wall_t) if wall_t is not None else CLOCK()
        spec = run.plan.expect
        self._emit(
            run,
            "panel_press_not_evidence",
            t_rel=None if run.origin is None else round(t - run.origin, 6),
            observed_by=(spec.source if spec is not None else "none"),
            note="the panel is not the evidence source for this run",
        )
        return True

    def inject(
        self,
        run_id: str,
        payload: str = "signal",
        t: float | None = None,
        human_latency: float | None = None,
        wall_t: float | None = None,
        latency_s: float | None = None,
    ) -> None:
        """External signal injection (HTTP bridge, MCP bridge, tests).

        ``wall_t`` lets a browser tell us *when the press actually happened* in
        its own clock domain, which is what a click handler can capture
        precisely.  Without it we would have to use the server-side receive
        time and inherit the network + event-loop latency.  The client's clock
        is aligned by :meth:`clock_sync_payload` first, so the value is
        trustworthy at the sub-millisecond level on loopback.
        """
        run = self._get(run_id)
        if wall_t is not None:
            t = self.mono_from_wall(wall_t)
        self._on_signal(
            run,
            Observation(
                t=t if t is not None else CLOCK(),
                source="inject",
                payload=payload,
                raw=payload,
                human_latency=human_latency,
                latency_s=latency_s,
            ),
        )

    # -- the one-call API the agent uses ----------------------------------

    def cue(
        self,
        instruction: str = "",
        target_in_s: float | None = None,
        lead_s: float = 3.0,
        ioi_ms: float = 500.0,
        tail_beats: int = 6,
        audio: bool = True,
        visual: bool = True,
        duration_s: float = 30.0,
        expect_source: str | None = None,
        target_at_wall: float | None = None,
        paused: bool = False,
        open_panel: bool = True,
        queue: bool = True,
        run_id: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """Submit a cue in one call and return its absolute schedule.

        Returns immediately -- never waits for the human.  The web panel is
        notified through :meth:`on_armed` the moment the grid is resolved, so
        the display is authoritative and the agent does not have to relay
        anything.

        ``queue=True`` (the default) means "do not talk over the cue the human
        is already following": if a grid is still running, this one is armed
        behind it and starts by itself when that grid ends.  A person can only
        follow one countdown, so two live cues are not two cues -- the second
        silently replaces the first on screen.  Pass ``queue=False`` for the
        genuinely simultaneous case (e.g. one cue per person).

        ``expect_source`` defaults to ``"none"``, which means **this cue only
        reminds**: nothing observes the human, nothing is recorded, and the page
        renders no record control.  That is the component's whole job in the
        common case -- "do X at T" -- and it must not be dressed up as a request
        to press a button.  Pass ``"http"`` when the panel itself should capture
        the moment, or a sensor source (``serial``, ``console_key``...) when the
        hardware is the evidence.

        ``target_at_wall`` anchors the action to an absolute **wall-clock** instant
        (epoch seconds) instead of ``target_in_s``.  It is resolved when the grid
        is armed, so a queued cue still hits the instant it asked for -- and if
        that instant passes while it waits, the run ends ``verdict: "missed"``
        rather than playing an empty grid and reporting success.
        """
        if not instruction:
            raise ValueError("instruction is required")
        if target_at_wall is not None:
            # The wall-clock instant *is* the target.  Computing a `target_in_s` as
            # well would be a second, contradictory answer to the same question
            # (and a wrong one as soon as the cue is queued).
            target_in_s = None
        elif target_in_s is None:
            target_in_s = lead_s + 3.0
        source = expect_source or "none"
        plan = CuePlan(
            id=run_id or f"cue_{uuid.uuid4().hex[:6]}",
            instruction=instruction,
            lead_s=float(lead_s),
            ioi_ms=float(ioi_ms),
            tail_beats=int(tail_beats),
            audio=bool(audio),
            visual=bool(visual),
            target_in_s=None if target_in_s is None else float(target_in_s),
            target_at_wall=target_at_wall,
            expect=SignalSpec(source=source, timeout_s=float(duration_s), min_offset_ms=-float(ioi_ms) / 2.0),
            duration_s=float(duration_s),
            # The cue IS the stimulus here, so it must outlive the response.
            # Cutting the pulse the instant the human presses teaches them that
            # "GO" was a one-off event, which pushes them back to reacting to
            # it instead of entraining on the grid.  Same reasoning as the M1
            # probe protocol (see docs/implementation-notes.md §3.5).
            cancel_cue_on_signal=False,
            meta={"target": "cue", "effector": "human", "calibrate": True,
                  "panel": bool(open_panel), **extra},
        )
        run = self.submit(plan, paused=paused, queue=queue)
        if open_panel and self._panel_opener is not None:
            try:
                self._panel_opener(run.id)
            except Exception:
                pass
        hint = (
            f"queued behind {run.queued_behind}; it starts when that cue ends"
            if run.queued_behind
            else "the cue runs without you; poll chrono_observe or read the journal"
        )
        return {
            "run_id": run.id,
            "status": run.status.value,
            "queued_behind": run.queued_behind,
            "instruction": instruction,
            "hint": hint,
        }

    def on_panel_open(self, fn: Callable[[str], None]) -> None:
        """Register the callback used to raise the cue panel.

        Kept as a hook rather than an import so the conductor stays transport
        agnostic -- the daemon wires it to the HTTP panel, tests leave it unset.
        """
        self._panel_opener = fn

    def on_stand_down(self, fn: Callable[[], None]) -> None:
        """Register what to do when an update asks this conductor to leave.

        A hook for the same reason as `on_panel_open`: a conductor does not own
        a process loop, so `stand_down` can only *ask*.  The daemon wires this
        to its own accept loop; anywhere else the answer says so honestly --
        "I have stopped working, but nobody here can exit".
        """
        self._stand_down_hook = fn

    # -- being stopped (the update path) ----------------------------------

    def update_ready(self) -> dict[str, Any]:
        """May this conductor be stopped right now?

        The answer is the conductor's to give, not the updater's.  A cue is a
        commitment made to a person at a deadline, so stopping in the middle of
        one leaves somebody who was told to press at T holding a dead window --
        which is the one failure this whole component exists to avoid.

        Terminal runs do not block.  Once the human has answered (or the budget
        expired) the commitment is over; the tail beats after GO are a courtesy
        pulse for entrainment, not a promise, so they may be cut short.  That is
        a judgement, not an oversight.
        """
        active: list[dict[str, Any]] = []
        for run in self.runs.values():
            if run.status in TERMINAL:
                continue
            ends_in = (
                None if run.origin is None
                else round(run.origin + run.plan.duration_s - CLOCK(), 3)
            )
            active.append({
                "run_id": run.id,
                "status": run.status.value,
                "instruction": run.plan.instruction,
                # A queued run is a commitment too: stopping now would drop a
                # cue the agent has already promised to deliver, so it blocks an
                # update exactly like a running one does.
                "queued_behind": run.queued_behind,
                "ends_in_s": ends_in,
            })
        # A submitted-but-not-yet-started run has no end time at all, and
        # inventing one would be worse than saying nothing: the honest answer is
        # "wait for a number you can trust", not a plausible guess.
        waits = [a["ends_in_s"] for a in active if a["ends_in_s"] is not None]
        return {
            "safe": not active and not self._standing_down,
            "quiescent": self._standing_down,
            "reason": (
                "standing down for an update" if self._standing_down
                else (f"{len(active)} cue(s) in flight: {active[0]['instruction']}" if active else "")
            ),
            "retry_in_s": (max(0.0, max(waits)) if waits else None) if active else 0.0,
            "active": active,
            # True because it is true by construction, not because anything was
            # flushed here: `_emit` writes every event with its own `open`/`write`
            # in the calling thread, so a run that is over is already on disk.
            # (`JournalWriter` exists and is started, but nothing feeds it.)
            "journal_flushed": True,
            "version": __version__,
            "contract": CONTRACT_VERSION,
        }

    def stand_down(self) -> dict[str, Any]:
        """Stop accepting work and ask the process owning this loop to leave.

        The hook is *scheduled*, not called here: the caller is answering a
        request over the very loop that is about to be torn down, and tearing it
        down synchronously would cut off the reply that says what happened.
        """
        if self._standing_down:
            return {"ok": True, "quiescent": True, "already": True,
                    "reason": "already standing down", "journal_flushed": True}
        ready = self.update_ready()
        if not ready["safe"]:
            return {"ok": False, **ready}
        self._standing_down = True
        hook = self._stand_down_hook
        if hook is None:
            return {
                "ok": True,
                "quiescent": True,
                "exiting": False,
                "journal_flushed": True,
                "note": ("no process loop is registered here; whoever asked must "
                         "stop the process itself"),
            }
        threading.Timer(_STAND_DOWN_GRACE_S, hook).start()
        return {
            "ok": True,
            "quiescent": True,
            "exiting": True,
            "journal_flushed": True,
            "exit_in_s": _STAND_DOWN_GRACE_S,
        }

    # -- client-facing payloads -------------------------------------------

    def clock_sync_payload(self) -> dict[str, float]:
        """One-shot clock alignment sample for a browser client.

        The client cannot measure its offset from us without knowing the
        round-trip delay, so this returns the *server's* receive and send
        instants; the client pairs them with its own and derives the offset
        (NTP-style, see panel.html).  Returning both lets the client compute
        the network delay and discard bad samples instead of trusting a single
        reading.
        """
        t_recv_wall = time.time()
        t_recv_mono = CLOCK()
        return {
            "server_recv_wall": t_recv_wall,
            "server_send_wall": self.wall_at(t_recv_mono),
            "server_mono": t_recv_mono,
        }

    def schedule_payload(self, run_id: str) -> dict[str, Any]:
        """Everything a display client needs to render and sound the cue.

        Absolute times are given in **epoch milliseconds** so a browser can
        compare them with ``performance.now() + performance.timeOrigin`` and
        schedule audio sample-accurately.
        """
        run = self._get(run_id)
        plan = run.plan
        beats = [
            {
                "index": b.index,
                "at_ms": round(self.wall_at(b.at) * 1000.0, 3),
                "accent": b.accent,
                "tone": b.f_audio,
                "label": b.label,
            }
            for b in run.beats
        ]
        return {
            "run_id": run.id,
            "instruction": plan.instruction,
            "status": run.status.value,
            "ioi_ms": plan.ioi_ms,
            "lead_s": plan.lead_s,
            "audio": plan.audio,
            "visual": plan.visual,
            "visual_style": plan.visual_style,
            "tail_beats": plan.tail_beats,
            "meta": plan.meta,
            #: Which channel is treated as *evidence that the human acted*.
            #: The panel needs this to know whether IT is the observation
            #: surface (show a press control) or merely the reminder while a
            #: real sensor watches (serial, console).  Without it the panel
            #: always looked like a button to push, which misrepresents every
            #: run whose evidence comes from the hardware.
            "expect_source": None if plan.expect is None else plan.expect.source,
            "origin_wall_ms": None if run.origin is None else round(self.wall_at(run.origin) * 1000.0, 3),
            "zero_wall_ms": None if run.zero_at is None else round(self.wall_at(run.zero_at) * 1000.0, 3),
            "target_wall_ms": None if run.target_at is None else round(self.wall_at(run.target_at) * 1000.0, 3),
            "beats": beats,
        }

    @staticmethod
    def match(spec: SignalSpec | None, obs: Observation) -> bool:
        """Does an observation satisfy a spec?

        A source reports *everything* it sees; matching is the conductor's
        policy.  This is what lets one source feed both ``expect_signal``
        ("the byte arrived") and ``verify`` ("the board answered with a
        prompt"), and it keeps the source free of run-specific knowledge.
        """
        if spec is None:
            return False
        if not spec.pattern:
            return True
        text = f"{obs.payload} {obs.raw}"
        try:
            return re.search(spec.pattern, text) is not None
        except re.error:
            return spec.pattern in text

    def _on_signal(self, run: Run, obs: Observation) -> None:
        if run.status in TERMINAL:
            return
        if run.origin is None:
            return

        # --- verify channel (defaults to the same source as expect) ---------
        if run.plan.verify is not None and not run.verify_seen:
            if self.match(run.plan.verify, obs):
                if run.first_signal_t is None:
                    # The verify string can legitimately precede the "human
                    # acted" evidence (e.g. a prompt that was already on the
                    # wire). Record it as the human signal only if the plan has
                    # no separate expect spec.
                    if run.plan.expect is None:
                        pass
                    else:
                        self._emit(run, "verify_matched", payload=obs.payload, before_signal=True)
                        run.verify_seen = True
                        self._finalize(run, RunStatus.SATISFIED, reason="verify_before_signal")
                        return
                else:
                    run.verify_seen = True
                    self._emit(run, "verify_matched", payload=obs.payload, t_rel=round(run.rel, 6))
                    self._finalize(run, RunStatus.SATISFIED, reason="verify")
                    return
            elif not self.match(run.plan.expect, obs):
                return  # unrelated noise on this source

        if run.first_signal_t is None:
            zero = run.zero_at or run.origin
            # The correction belongs to the measurement path: console keys go
            # through the OS input stack, a browser click handler does not.
            latency = self.key_latency_s if obs.latency_s is None else obs.latency_s
            off_ms = (obs.t - latency - zero) * 1000.0
            min_off = run.plan.expect.min_offset_ms if run.plan.expect else -300.0
            if off_ms < min_off:
                # Not a human acting on this cue: almost certainly a stray
                # keystroke from the previous trial.  Ignoring it is the whole
                # point of having min_offset_ms.
                self._emit(
                    run,
                    "signal_ignored",
                    source=obs.source,
                    payload=obs.payload,
                    offset_ms=round(off_ms, 1),
                    reason="before_min_offset",
                )
                return
            run.signal_seen = True
            run.first_signal_t = obs.t
            run.human_offset_ms = off_ms
            self._emit(
                run,
                "human_signal",
                source=obs.source,
                payload=obs.payload,
                offset_ms=round(off_ms, 1),
                latency_correction_ms=round(latency * 1000, 1),
                timing_usable=obs.timing_usable,
            )
        else:
            self._emit(run, "human_signal_extra", source=obs.source, payload=obs.payload)

        if self._observation_sink is not None:
            try:
                self._observation_sink(run, obs)
            except Exception:
                pass

        # If there is nothing to verify against, the human's action *is* the
        # closed loop -- finish immediately rather than idling until deadline.
        if run.plan.expect is not None and run.plan.verify is None and run.plan.on_window is None:
            self._finalize(run, RunStatus.SATISFIED, reason="human_signal")

    def _verify_matched(self, run: Run, obs: Observation) -> None:
        run.verify_seen = True
        self._emit(run, "verify_matched", payload=obs.payload, t_rel=round(run.rel, 6))
        self._finalize(run, RunStatus.SATISFIED, reason="verify")

    # -- verdict -----------------------------------------------------------

    def _on_deadline(self, run: Run) -> None:
        if run.status in TERMINAL:
            return
        if run.verify_seen:
            self._finalize(run, RunStatus.SATISFIED, reason="verify")
        elif run.signal_seen:
            self._finalize(run, RunStatus.PARTIAL, reason="signal_seen_no_verify")
        else:
            self._finalize(run, RunStatus.TIMEOUT, reason="no_signal")

    def verify(self, run_id: str, payload: str = "verified") -> None:
        """Mark the action as confirmed effective (sensor read-back)."""
        run = self._get(run_id)
        if run.status in TERMINAL or run.verify_seen:
            return
        run.verify_seen = True
        self._emit(run, "verify_matched", payload=payload, t_rel=round(run.rel, 6))
        self._finalize(run, RunStatus.SATISFIED, reason="verify")

    def _finalize(self, run: Run, status: RunStatus, **detail: Any) -> None:
        if run.status in TERMINAL:
            return
        run.status = status
        run.ended_at = CLOCK()
        run.verdict, run.verdict_reason = self._verdict_for(run, status, detail.get("reason", ""))

        # Rule 4: only closed-loop samples train the estimator.
        closed_loop = status == RunStatus.SATISFIED or (
            run.plan.expect is not None and run.plan.verify is None and run.plan.on_window is None
        )
        if run.human_offset_ms is not None and closed_loop and run.plan.meta.get("calibrate", True):
            key = task_signature(
                cue="beat" if run.plan.audio or run.plan.visual else "none",
                ioi_ms=run.plan.ioi_ms,
                effector=run.plan.meta.get("effector", "human"),
                target=run.plan.meta.get("target", ""),
            )
            cal = self.calibration.observe(key, run.human_offset_ms, source=run.source.name if run.source else "")
            detail["calibration"] = {"key": key, "offset_ms": round(cal.offset_ms, 1), "n": cal.n}



        if run.window_job is not None:
            self.scheduler.cancel(run.window_job)
        if run.deadline_job is not None:
            self.scheduler.cancel(run.deadline_job)
        if run.beat_job is not None and run.plan.cancel_cue_on_signal:
            self.scheduler.cancel(run.beat_job)

        # Only audit here if the grid is genuinely finished.  With a tail the
        # beats outlive the verdict (the human already acted), and auditing now
        # would report a grid that is still playing -- understating n and
        # missing the tail's firings entirely.  The scheduled audit job covers
        # that case.
        if run.plan.cancel_cue_on_signal or run.beat_job is None or not self._beats_pending(run):
            self._emit_cue_audit(run, reason="run_end")

        self._emit(run, "run_end", status=status.value, verdict=run.verdict,
                   verdict_reason=run.verdict_reason, human_offset_ms=run.human_offset_ms, **detail)
        if isinstance(self.visual, VisualCue):
            self.visual.done(f"[{run.verdict or status.value}] offset={self._fmt_offset(run.human_offset_ms)}")
        self._close_run_io(run)
        run.done.set()
        # A queued run's clock may only start once the one ahead of it is over.
        # Released *after* this run is fully closed, so the two grids can never
        # overlap by a beat.
        self._release_queue(run)

    @staticmethod
    def _verdict_for(run: Run, status: RunStatus, reason: str) -> tuple[str, str]:
        """Turn (status, why) into a sentence a caller cannot misread.

        ``status`` has to stay a stable state machine, so the meaning lives in a
        second field.  The case that forced this: a cue with ``expect_source
        == "none"`` asks the human for nothing the machine can observe, so when
        ``duration_s`` runs out it ends ``timeout`` -- which is the *successful*
        end of that cue, and was read as a failure by the first person who tried
        it.  ``verdict: "ok"`` says what happened; ``timeout`` only says which
        transition fired.
        """
        if status == RunStatus.SATISFIED:
            return "ok", reason or "signal_observed"
        if status == RunStatus.ABORTED:
            return "cancelled", reason or "cancelled"
        if status == RunStatus.ERROR:
            if reason == "deadline_missed":
                # A wall-clock instant that passed while this cue was queued.  It
                # gets its own verdict because "error" would hide the one thing
                # the caller must know: the cue never happened.
                return "missed", reason
            return "error", reason or "error"
        if status == RunStatus.PARTIAL:
            return "unverified", reason or "signal_seen_no_verify"
        # TIMEOUT, or anything still non-terminal that a caller is asking about.
        expects_signal = run.plan.expect is not None and run.plan.expect.source not in ("", "none")
        if expects_signal:
            return "signal_missing", reason or "no_signal"
        # Nothing was ever expected from the human: the cue delivered its grid
        # and stopped.  That is the whole job.  The internal reason is left out
        # of the verdict on purpose -- it says ``no_signal``, which is precisely
        # the wording that made a correct run read as a failure.
        return "ok", "cue_finished"

    def _release_queue(self, finished: Run) -> None:
        """Start the run that was waiting behind ``finished``, if any.

        Only one level is released per finish; the released run inherits the
        queue itself, so a chain of three cues plays in submission order without
        any of them being armed while another is still pulsing.

        The release waits for the *stimulus* to end, not for the verdict: with a
        tail, a run reaches its terminal status seconds before its last beat, and
        starting the next grid then would put two beat trains in the air at once.
        """
        with self._lock:
            nxt = next(
                (r for r in self.runs.values() if r.queued_behind == finished.id),
                None,
            )
        if nxt is None:
            return
        if self._beats_pending(finished):
            # The hand-over is re-armed for the moment the last beat lands.  A
            # dedicated job rather than a poll: the tail's length is known
            # exactly, and a spin loop here would be one more thing to get wrong.
            job = finished.beat_job
            remaining = getattr(job, "remaining", 0) if job is not None else 0
            every = max(0.02, finished.plan.ioi_ms / 1000.0)
            self.scheduler.after(
                remaining * every + 0.02,
                lambda _d: self._release_queue(finished),
                name=f"{finished.id}:release",
            )
            return
        nxt.queued_behind = None
        self._emit(nxt, "queue_released", behind=finished.id, status=finished.status.value)
        self.go(nxt.id)

    def _beats_pending(self, run: Run) -> bool:
        """Is the cue grid still going to fire again?"""
        job = run.beat_job
        if job is None or job.cancelled:
            return False
        return getattr(job, "remaining", 0) != 0

    def _emit_cue_audit(self, run: Run, reason: str = "grid_complete") -> None:
        """Record how well the *cue grid* landed.

        Scoped to ``{run.id}:beats`` for two reasons: a long-lived daemon's
        earlier runs must not inflate the number, and -- more subtly -- the
        deadline and window jobs are torn down on purpose, so their firings are
        late by construction and would masquerade as cue jitter.  This is the
        figure M1 uses to separate machine error from human error, so it must
        describe exactly one thing.

        Emitted when the grid finishes, not when the run ends: with a tail the
        two differ, and auditing at run_end would silently drop the tail beats.
        Idempotent per run.
        """
        if run.cue_audited:
            return
        from .clock import drift_report

        firings = [
            f for f in self.scheduler.firings if f.name == f"{run.id}:beats" and f.executed_s >= 0
        ]
        run.cue_audited = True
        if not firings:
            return
        rep = drift_report(firings)
        self._emit(
            run,
            "cue_audit",
            reason=reason,
            n=len(firings),
            median_ms=round(rep["offset_median_ms"], 3),
            p99_ms=round(rep["offset_p99_ms"], 3),
            max_ms=round(rep["offset_max_ms"], 3),
            exec_max_ms=round(rep["exec_max_ms"], 3),
            scope="cue_grid_only",
        )

    @staticmethod
    def _fmt_offset(ms: float | None) -> str:
        if ms is None:
            return "n/a"
        return f"{ms:+.1f} ms ({'early' if ms < 0 else 'late'})"

    def _close_run_io(self, run: Run) -> None:
        for obj in (run.source, run.effector):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass

    # -- queries -----------------------------------------------------------

    def _get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def events(self, run_id: str, since_seq: int = 0) -> list[Event]:
        run = self._get(run_id)
        return [e for e in run.events if e.seq > since_seq]

    def status(self, run_id: str, include_events: bool = False) -> dict[str, Any]:
        return self._get(run_id).snapshot(include_events=include_events)

    def wait(self, run_id: str, timeout_s: float = 10.0, poll_s: float = 0.01) -> dict[str, Any]:
        """Block **at most** ``timeout_s``, then return a snapshot.

        Never "wait until done": a truncated wait loses one query, not the run.
        This is what makes the MCP surface safe under client-side timeouts.
        """
        run = self._get(run_id)
        # `timed_out` is computed by the caller-facing snapshot below, so a
        # truncated wait returns a *partial* snapshot rather than raising.
        if poll_s <= 0:
            run.done.wait(timeout=timeout_s)
        else:
            self._wait_poll(run, timeout_s, poll_s)
        snap = run.snapshot(include_events=False)
        snap["timed_out"] = run.status not in TERMINAL
        return snap

    def _wait_poll(self, run: Run, timeout_s: float, poll_s: float) -> None:
        end = CLOCK() + timeout_s
        while CLOCK() < end:
            if run.status in TERMINAL:
                return
            run.done.wait(min(poll_s, max(0.0, end - CLOCK())))
        return

    def wait_cue_audit(self, run_id: str, timeout_s: float = 5.0) -> bool:
        """Wait until the cue grid has finished and been audited.

        Needed because the verdict and the stimulus end at different times: the
        run is SATISFIED the moment the human's action is observed, but a tail
        keeps pulsing afterwards.  Anything that wants to report how well the
        cue landed -- or that must deliver the whole stimulus before the next
        trial begins -- has to wait for the grid, not for the verdict.
        """
        run = self._get(run_id)
        end = CLOCK() + max(0.0, timeout_s)
        while CLOCK() < end:
            if run.cue_audited:
                return True
            time.sleep(0.005)
        return run.cue_audited

    def cancel(self, run_id: str, reason: str = "cancelled_by_request") -> dict[str, Any]:
        run = self._get(run_id)
        self._finalize(run, RunStatus.ABORTED, reason=reason)
        return run.snapshot(include_events=False)

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        ids = list(self._order)[-limit:]
        return [self.runs[i].snapshot(include_events=False) for i in ids if i in self.runs]

    def stats(self) -> dict[str, Any]:
        audit = [f for f in self.scheduler.firings if f.executed_s >= 0]
        offs = sorted(f.offset for f in audit)
        return {
            "runs": len(self.runs),
            "active": sum(1 for r in self.runs.values() if r.status not in TERMINAL),
            "scheduler_firings": len(audit),
            "sched_offset_median_ms": round(offs[len(offs) // 2] * 1000, 3) if offs else None,
            "sched_offset_max_ms": round(offs[-1] * 1000, 3) if offs else None,
            "audio": self.audio.name,
            "journal_dir": str(self.journal_dir),
            "calibration_path": str(self.calibration.path),
        }

    # -- experiment helpers ------------------------------------------------

    def analyse(self, run_id: str) -> dict[str, Any]:
        """Signed-offset statistics for one run, with the provenance rules of
        :func:`chrono_handoff.inputs.analyse_offsets` applied."""
        run = self._get(run_id)
        return analyse_offsets(run.events, key_latency_s=self.key_latency_s)
