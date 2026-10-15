"""M1: the measurement that decides whether this project is real.

The question M1 answers is deliberately narrow:

    If a machine says "act exactly now", does a human actually land at that
    instant, and is the error (a) small enough to matter and (b) *stable
    enough to be predicted*?

Everything else in the project (protocol, MCP surface, hardware effectors)
assumes the answer is yes.  If the offset were pure noise, the honest
conclusion would be "buy a relay, don't do this".

What this runner therefore does:

* renders a **beat grid** (not a one-shot countdown) so the human can
  anticipate instead of react,
* timestamps the response at the *mechanism* level (``msvcrt`` keystroke),
* reports the **signed** offset distribution per cue condition,
* measures whether the learned offset actually cancels the bias, and
* reports the machine's own scheduling error next to it, so the two are never
  confused.

Usage::

    python -m chrono_handoff.cli probe --trials 24
    python -m chrono_handoff.cli probe --trials 24 --conditions visual   # no beep
    python -m chrono_handoff.cli probe --trials 8  --source sim          # dry run
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .calibrate import CalibrationStore, task_signature
from .clock import CLOCK
from .conductor import Conductor
from .cue import VisualCue
from .inputs import DEFAULT_KEY_LATENCY_S, make_source
from .model import (
    CuePlan,
    Event,
    RunStatus,
    SignalSpec,
    WindowSpec,
    mad,
    median,
)

CONDITIONS: dict[str, tuple[bool, bool]] = {
    "both": (True, True),      # audio + visual
    "audio": (True, False),
    "visual": (False, True),
    "silent": (False, False),  # control: no cue at all (should be terrible)
}


@dataclass
class Trial:
    index: int
    condition: str
    run_id: str
    status: str
    offset_ms: float | None
    applied_offset_ms: float
    sched_median_ms: float | None
    sched_max_ms: float | None
    wall: float = field(default_factory=time.time)

    @property
    def residual_ms(self) -> float | None:
        if self.offset_ms is None:
            return None
        return self.offset_ms - self.applied_offset_ms


@dataclass
class Session:
    condition: str = "both"
    #: Unique per probe invocation, so journals from different runs can never
    #: collide or be appended to each other.
    session_id: str = field(default_factory=lambda: "m1s" + uuid.uuid4().hex[:6])
    ioi_ms: float = 500.0
    lead_s: float = 3.0
    target_in_s: float = 6.0
    key_latency_s: float = DEFAULT_KEY_LATENCY_S
    trials: list[Trial] = field(default_factory=list)
    warmup: int = 2
    stop_on_no_signal: bool = False
    visual_style: str = "marker"
    #: Beats kept running after the target so entrainment is possible.
    tail_beats: int = 6

    # -- plan construction -------------------------------------------------

    def make_plan(self, index: int, condition: str, offset_s: float, apply_offset: bool) -> CuePlan:
        audio, visual = CONDITIONS[condition]
        # Anything earlier than half a beat before GO is a response to the
        # *previous* tick, not to the target -- exclude that zone rather than
        # counting the previous beat's reaction as a very early press.
        min_off = -self.ioi_ms / 2.0
        return CuePlan(
            # Session-scoped id.  A deterministic id makes two different runs
            # write to the same journal file, and the second run APPENDS to the
            # first -- silently producing a journal that mixes a dry run with a
            # real session and poisons any offline analysis.  (That happened
            # while building this: sim trials ended up interleaved with real
            # ones in logs/m1_00*.jsonl.)  The session token is generated once
            # per probe invocation.
            id=f"{self.session_id}_{index:03d}_{condition}",
            instruction="PRESS SPACE exactly on the GO beat" if condition != "silent" else "press SPACE when you think the moment is right",
            lead_s=self.lead_s,
            ioi_ms=self.ioi_ms,
            tail_beats=self.tail_beats,
            audio=audio,
            visual=visual,
            visual_style=self.visual_style,
            target_in_s=self.target_in_s,
            offset_s=offset_s if apply_offset else 0.0,
            # The cue is the thing under study: let the pulse run past the
            # response so the subject learns GO is one beat in a series.
            cancel_cue_on_signal=False,
            expect=SignalSpec(
                source="console_key",
                pattern="space",
                timeout_s=self.target_in_s + 4.0,
                interval_ms=2.0,
                min_offset_ms=min_off,
                opts={"keys": " \r\n"},
            ),
            on_window=None,
            duration_s=self.target_in_s + 6.0,
            meta={
                "task": "m1_probe",
                "condition": condition,
                "target": "keypress",
                "effector": "human",
                "calibrate": False,  # the session owns calibration, not the run
            },
        )

    def record(self, index: int, condition: str, run: Any, applied_offset_ms: float) -> Trial:
        events = run.events
        sched = next((e for e in reversed(events) if e.kind == "cue_audit"), None)
        trial = Trial(
            index=index,
            condition=condition,
            run_id=run.id,
            status=run.status.value,
            offset_ms=run.human_offset_ms,
            applied_offset_ms=applied_offset_ms,
            sched_median_ms=(sched.detail["median_ms"] if sched else None),
            sched_max_ms=(sched.detail["max_ms"] if sched else None),
        )
        self.trials.append(trial)
        return trial


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def describe(xs: Iterable[float]) -> dict[str, Any]:
    xs = list(xs)
    if not xs:
        return {"n": 0}
    med = median(xs)
    return {
        "n": len(xs),
        "median_ms": round(med, 1),
        "mad_ms": round(mad(xs, med), 1),
        "sd_ms": round(statistics.stdev(xs), 1) if len(xs) > 1 else 0.0,
        "min_ms": round(min(xs), 1),
        "max_ms": round(max(xs), 1),
        "spread_ms": round(max(xs) - min(xs), 1),
        "early_frac": round(sum(1 for x in xs if x < 0) / len(xs), 3),
    }


def report(session: Session, excluded_warmup: bool = True) -> dict[str, Any]:
    trials = session.trials
    kept = [t for t in trials if not (excluded_warmup and t.index < session.warmup)]
    usable = [t for t in kept if t.offset_ms is not None]

    by_condition: dict[str, dict[str, Any]] = {}
    for cond in {t.condition for t in kept}:
        offs = [t.offset_ms for t in kept if t.condition == cond and t.offset_ms is not None]
        by_condition[cond] = describe(offs)

    # Machine side, reported next to the human side so the two are never
    # conflated: if this number is not ~0.1 ms, the bug is in the conductor.
    sched_med = [t.sched_median_ms for t in kept if t.sched_median_ms is not None]
    sched_max = [t.sched_max_ms for t in kept if t.sched_max_ms is not None]

    residuals = [t.residual_ms for t in kept if t.residual_ms is not None]
    applied = [t.applied_offset_ms for t in kept if t.offset_ms is not None]

    verdict = _verdict(usable)
    return {
        "trials_total": len(trials),
        "trials_counted": len(kept),
        "trials_with_signal": len(usable),
        "missed": len(kept) - len(usable),
        "by_condition": by_condition,
        "overall": describe([t.offset_ms for t in usable if t.offset_ms is not None]),
        "residual": describe(residuals) if residuals else {"n": 0},
        "applied_offset_ms": describe(applied) if applied else {"n": 0},
        "machine_sched": {
            "n": len(sched_med),
            "median_of_medians_ms": round(median(sched_med), 3) if sched_med else None,
            "worst_max_ms": round(max(sched_max), 3) if sched_max else None,
            "note": "cue-grid firings only; the median is the figure that matters for human anchoring",
        },
        "key_latency_ms": round(session.key_latency_s * 1000, 1),
        "verdict": verdict,
    }


def _verdict(usable: list[Trial]) -> dict[str, Any]:
    """Interpret the result against the project's own premise."""
    offs = [t.offset_ms for t in usable if t.offset_ms is not None]
    if len(offs) < 5:
        return {"status": "insufficient_data", "note": "need >=5 closed-loop trials"}
    med = median(offs)
    spread = mad(offs, med)
    # Pre-registered thresholds, so the conclusion is not chosen after the fact.
    if spread <= 60.0 and abs(med) <= 250.0:
        status = "premise_supported"
    elif spread <= 120.0:
        status = "marginal"
    else:
        status = "premise_weak"
    return {
        "status": status,
        "median_offset_ms": round(med, 1),
        "mad_ms": round(spread, 1),
        "note": (
            "offset is small and stable: scheduling against a beat grid is meaningful, "
            "and the constant part can be cancelled by calibrating offset_s"
            if status == "premise_supported"
            else "offset is larger or noisier than hoped; consider beat spacing, cue modality, or a hardware effector"
        ),
    }


def format_report(rep: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 66)
    add("  M1 RESULT -- human offset against a beat grid")
    add("=" * 66)
    add(f"  counted {rep['trials_counted']}  with-signal {rep['trials_with_signal']}  missed {rep['missed']}")
    add(f"  (offsets are signed: NEGATIVE = human fired early, POSITIVE = late)")
    add(f"  keystroke-latency correction: -{rep['key_latency_ms']:.1f} ms")
    add("")
    add(f"  {'condition':<10} {'n':>3} {'median':>9} {'MAD':>7} {'sd':>7} {'min':>8} {'max':>8} {'%early':>7}")
    for cond, d in sorted(rep["by_condition"].items()):
        if not d.get("n"):
            add(f"  {cond:<10} {0:>3}      (no signal)")
            continue
        add(
            f"  {cond:<10} {d['n']:>3} {d['median_ms']:>7.1f}ms {d['mad_ms']:>5.1f}ms "
            f"{d['sd_ms']:>5.1f}ms {d['min_ms']:>6.1f}ms {d['max_ms']:>6.1f}ms {d['early_frac']*100:>6.1f}%"
        )
    add("")
    o = rep["overall"]
    if o.get("n"):
        add(f"  overall     n={o['n']}  median={o['median_ms']:+.1f}ms  MAD={o['mad_ms']:.1f}ms  spread={o['spread_ms']:.1f}ms")
    r = rep["residual"]
    if r.get("n"):
        a = rep["applied_offset_ms"]
        if a.get("n") and abs(a["median_ms"]) > 0.5:
            add(f"  residual after applying the learned offset: median={r['median_ms']:+.1f}ms MAD={r['mad_ms']:.1f}ms")
            add("    (residual ~0 means the calibration actually cancelled the human's bias)")
        else:
            add(f"  residual: {r['median_ms']:+.1f}ms -- but no offset was applied, so this equals the raw offset")
            add("    (use --calibrate-every N to run the adaptive variant and see it shrink)")
    ms = rep["machine_sched"]
    if ms.get("n"):
        add(f"  machine scheduling error: median-of-medians={ms['median_of_medians_ms']}ms worst={ms['worst_max_ms']}ms")
        add("    (median tells you the typical accuracy; worst is a rare tail, usually GC. Both << the human's ~50ms.)")
    add("")
    v = rep["verdict"]
    add(f"  VERDICT: {v['status']}")
    if v.get("note"):
        add(f"    {v['note']}")
    add("=" * 66)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# interactive runner
# --------------------------------------------------------------------------


def _drain_keys() -> None:
    """Swallow anything already buffered so a stray Enter cannot register as a
    response (this is the practical form of 'min_offset_ms')."""
    try:
        import msvcrt  # noqa: PLC0415

        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:
        pass


def run_probe(
    trials: int = 24,
    conditions: list[str] | None = None,
    ioi_ms: float = 500.0,
    lead_s: float = 3.0,
    target_in_s: float = 6.0,
    source: str = "console_key",
    warmup: int = 2,
    audio: str = "auto",
    visual: bool = True,
    visual_style: str = "marker",
    tail_beats: int = 6,
    calibrate: bool = True,
    calibrate_every: int = 0,
    journal_dir: str | Path | None = None,
    auto: bool = False,
    sim_offsets_ms: list[float] | None = None,
    key_latency_ms: float | None = None,
    store_path: str | Path | None = None,
    out: Any = None,
) -> dict[str, Any]:
    """Run the M1 protocol.  Returns the machine-readable report.

    ``calibrate_every > 0`` enables the *adaptive* variant: after every N trials
    the learned offset is applied to the remaining ones, which is the actual
    claim of the project ("the second run is better than the first").
    """
    out = out or sys.stdout
    conditions = conditions or ["both"]
    for c in conditions:
        if c not in CONDITIONS:
            raise SystemExit(f"unknown condition {c!r}; choose from {sorted(CONDITIONS)}")

    session = Session(
        ioi_ms=ioi_ms,
        lead_s=lead_s,
        target_in_s=target_in_s,
        warmup=warmup,
        visual_style=visual_style,
        tail_beats=tail_beats,
    )
    if key_latency_ms is not None:
        session.key_latency_s = key_latency_ms / 1000.0

    store = CalibrationStore(path=store_path) if calibrate else CalibrationStore(path=store_path)
    sig = task_signature(cue="beat", ioi_ms=ioi_ms, effector="human", target="keypress")

    source_kw: dict[str, Any] = {}
    if source == "sim":
        source_kw = {"offsets_ms": sim_offsets_ms, "jitter_ms": 0.0, "seed": 1}
    elif source == "console_key":
        source_kw = {"keys": " \r\n", "echo": False}

    conductor = Conductor(
        journal_dir=journal_dir,
        audio=audio,
        visual=visual,
        calibration=store,
        key_latency_s=session.key_latency_s,
    )
    conductor.start()
    # The session drives the source itself so calibration can be applied between
    # trials; the conductor's own poller would otherwise consume the keystroke.
    conductor.poll_interval_ms = 1.0

    print(f"M1 probe: {trials} trials, conditions={conditions}, ioi={ioi_ms:.0f}ms, "
          f"lead={lead_s:.1f}s, target=t+{target_in_s:.1f}s", file=out)
    print(f"  source={source} audio={conductor.audio.name} visual={visual}", file=out)
    print(f"  calibration store: {store.path}", file=out)
    print("  (uncalibrated offsets are recorded per trial; residuals are reported separately)", file=out)

    try:
        for i in range(trials):
            cond = conditions[i % len(conditions)]
            apply = calibrate and calibrate_every > 0 and i >= calibrate_every
            cal = store.get(sig)
            offset_s = cal.predict_s() if apply else 0.0
            applied_ms = offset_s * 1000.0

            plan = session.make_plan(i, cond, offset_s, apply_offset=True)
            if plan.expect is not None and source == "sim":
                plan.expect.source = "sim"
                # Fresh seed per trial: a synthetic subject that draws the same
                # number every time would make MAD report 0.0 and hide exactly
                # the spread the experiment exists to measure.
                plan.expect.opts = {**source_kw, "seed": 1000 + i}

            _drain_keys()
            if not auto and source != "sim":
                print(f"\n--- trial {i+1}/{trials}  [{cond}] ---", file=out)
                print(f"    {plan.instruction}", file=out)
                print(f"    GO beat lands t+{plan.target_in_s:.1f}s"
                      + (f"  (cue advanced by {-applied_ms:+.0f} ms from your learned offset)" if apply and applied_ms else ""),
                      file=out)
                try:
                    input("    press Enter when ready... ")
                except EOFError:
                    pass
                _drain_keys()

            run = conductor.submit(plan)
            result = conductor.wait(run.id, timeout_s=plan.duration_s + 3.0, poll_s=0.005)
            # The verdict arrives with the human's response, but the cue grid
            # (including its tail) may still be pulsing.  Let it finish before
            # recording: otherwise the scheduling audit has not been emitted
            # and the next trial's countdown would overlap the previous one's
            # tail, so the subject hears a continuing pulse into a new trial.
            tail_s = plan.tail_beats * plan.ioi_ms / 1000.0 + 0.6
            conductor.wait_cue_audit(run.id, timeout_s=tail_s)
            trial = session.record(i, cond, run, applied_ms)
            if trial.offset_ms is None:
                print(f"    -> no signal ({trial.status})", file=out)
                if session.stop_on_no_signal:
                    break
            else:
                print(f"    -> {trial.offset_ms:+.1f} ms  (applied {applied_ms:+.1f})  status={trial.status}", file=out)

            # Fold the measurement in *after* recording, so the applied offset
            # for this trial stays auditable.
            if calibrate and trial.offset_ms is not None:
                store.observe(sig, trial.offset_ms, source="m1_probe")
            if result.get("timed_out"):
                print("    (run never reached a terminal state -- check the journal)", file=out)
    except KeyboardInterrupt:
        print("\ninterrupted; reporting what we have", file=out)
    finally:
        conductor.stop(terminate_runs=False)

    rep = report(session)
    rep["session"] = {
        "session_id": session.session_id,
        "journal_glob": f"{conductor.journal_dir}/{session.session_id}_*.jsonl",
        "ioi_ms": ioi_ms,
        "tail_beats": tail_beats,
        "lead_s": lead_s,
        "target_in_s": target_in_s,
        "conditions": conditions,
        "source": source,
        "trials_requested": trials,
        "calibrate": calibrate,
        "calibrate_every": calibrate_every,
        "signature": sig,
    }
    rep["trials"] = [t.__dict__ for t in session.trials]
    print("", file=out)
    print(format_report(rep), file=out)
    return rep


def save_report(rep: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return p
