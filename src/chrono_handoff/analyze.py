"""Offline analysis of M1 probe journals.

Reads the JSONL journals written by the conductor and reconstructs, per trial,
the quantities needed to interpret the human offset honestly:

  * signed offset of the human's press relative to the cue ZERO beat
  * which beat that press actually followed (the task's real ambiguity)
  * the machine's cue-grid landing error, so machine and human error are never
    conflated

Usage:
    python -m chrono_handoff.analyze            # auto-discover logs/m1_*.jsonl
    python -m chrono_handoff.analyze --glob 'logs/*.jsonl'
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

KEY_LATENCY_S = 0.015


@dataclass
class Trial:
    run_id: str
    condition: str
    ioi_ms: float
    zero_t: float
    press_t: float
    offset_ms: float
    beats_after_zero: int
    follow_err_ms: float          #: press - nearest cue onset (on the grid)
    grid_err_ms: list[float]
    applied_offset_ms: float
    status: str

    @property
    def nearest_beat_offsets(self) -> list[float]:
        return self.grid_err_ms


def load_trial(path: Path) -> Trial | None:
    evs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                evs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not evs:
        return None

    def first(kind: str):
        return next((e for e in evs if e["kind"] == kind), None)

    zero = first("cue_zero")
    sig = first("human_signal")
    if zero is None or sig is None:
        return None
    sub = first("submitted")

    # cue onsets, as actually landed
    onset_times = []
    for e in evs:
        if e["kind"] in ("cue_beat", "cue_zero"):
            onset_times.append(e["t"])

    # offset as measured by the conductor (already corrected for key latency)
    offset_ms = float(sig["detail"].get("offset_ms", (sig["t"] - zero["t"]) * 1000.0))

    press_effective = sig["t"] - KEY_LATENCY_S
    # which grid onset did the human actually track?
    after = [t for t in onset_times if t <= press_effective]
    beats_after_zero = len(after) - onset_times.index(zero["t"]) - 1 if after else 0
    nearest = min(onset_times, key=lambda t: abs(t - press_effective)) if onset_times else zero["t"]
    follow_err = (press_effective - nearest) * 1000.0

    grid_err = [
        float(e["detail"].get("scheduled_offset_ms", 0.0))
        for e in evs
        if e["kind"] in ("cue_beat", "cue_zero")
    ]
    # Aggregated figure, emitted after the grid finishes (not at run_end, which
    # would truncate a tail).  Kept for cross-checking the per-beat values.
    audits = [
        e["detail"]
        for e in evs
        if e["kind"] == "cue_audit"
    ]
    applied = float(sub["detail"].get("offset_s", 0.0)) * 1000.0 if sub else 0.0
    cond = ""
    if sub:
        cond = str(sub["detail"].get("meta", {}).get("condition", "")) or path.stem.split("_")[-1]

    return Trial(
        run_id=path.stem,
        condition=cond,
        ioi_ms=float(sub["detail"].get("ioi_ms", 500.0)) if sub else 500.0,
        zero_t=zero["t"],
        press_t=sig["t"],
        offset_ms=offset_ms,
        beats_after_zero=beats_after_zero,
        follow_err_ms=follow_err,
        grid_err_ms=grid_err,
        applied_offset_ms=applied,
        status="satisfied",
    )


def describe(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    xs = sorted(xs)
    n = len(xs)
    med = statistics.median(xs)
    return (
        f"n={n:<3} median={med:+8.1f} mean={statistics.mean(xs):+8.1f} "
        f"sd={statistics.stdev(xs) if n > 1 else 0:6.1f} "
        f"min={xs[0]:+7.1f} max={xs[-1]:+7.1f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chrono-analyze")
    ap.add_argument("--glob", default="logs/m1_*.jsonl")
    ap.add_argument("--key-latency-ms", type=float, default=KEY_LATENCY_S * 1000)
    ap.add_argument(
        "--last",
        type=int,
        default=0,
        help="only the N most recently modified journals (use this: a directory "
        "accumulates stale runs -- dry runs, selftests, and journals written by "
        "older code with different semantics -- and pooling them silently "
        "corrupts the statistics).",
    )
    args = ap.parse_args(argv)

    paths = [Path(p) for p in glob.glob(args.glob)]
    if args.last:
        paths.sort(key=lambda p: p.stat().st_mtime)
        paths = paths[-args.last :]
    paths = sorted(paths)
    # a stale dir or duplicate trial ids: keep the latest file per run id
    trials = []
    for p in paths:
        t = load_trial(p)
        if t is not None:
            trials.append(t)
    # de-duplicate by run_id (rewritten journals)
    by_id: dict[str, Trial] = {}
    for t in trials:
        by_id[t.run_id] = t
    trials = sorted(by_id.values(), key=lambda t: t.run_id)

    if not trials:
        print(f"no usable trials matched {args.glob!r}", file=sys.stderr)
        return 1

    print(f"loaded {len(trials)} trials from {args.glob}")
    print(f"key-latency correction applied: -{args.key_latency_ms:.1f} ms\n")

    # ---------- machine vs human ----------
    grid = [g for t in trials for g in t.grid_err_ms]
    print("MACHINE (cue grid landing error)")
    print("  " + describe(grid) + "   [ms]")
    print()

    # ---------- offset by condition ----------
    print("HUMAN offset relative to the ZERO beat (signed: -=early, +=late)")
    conds = sorted({t.condition for t in trials})
    for c in conds:
        sub = [t.offset_ms for t in trials if t.condition == c]
        print(f"  {c:<8} {describe(sub)}   %early={100*sum(1 for x in sub if x<0)/len(sub):.0f}%")
    print(f"  {'ALL':<8} {describe([t.offset_ms for t in trials])}")
    print()

    # ---------- did the human track the right beat? ----------
    print("WHICH BEAT THE PRESS FOLLOWED (that's the task's ambiguity, not noise)")
    hist: dict[int, int] = {}
    for t in trials:
        hist[t.beats_after_zero] = hist.get(t.beats_after_zero, 0) + 1
    for k in sorted(hist):
        label = {0: "the GO beat", 1: "one beat AFTER GO", -1: "one beat BEFORE GO", -2: "two beats BEFORE GO"}.get(
            k, f"{k:+d} beats from GO"
        )
        print(f"  {hist[k]:>3} trials ({100*hist[k]/len(trials):>5.1f}%) followed {label}")
    print()

    # ---------- if they were tracking the WRONG beat, what does that imply? ----------
    wrong = [t for t in trials if t.beats_after_zero != 0]
    right = [t for t in trials if t.beats_after_zero == 0]
    print("SPLIT BY WHICH BEAT THEY TRACKED")
    print(f"  on-GO       n={len(right):<3} {describe([t.offset_ms for t in right])}")
    print(f"  off-GO      n={len(wrong):<3} {describe([t.offset_ms for t in wrong])}")
    print()

    # ---------- the honest measurement: error to the beat they followed ----------
    print("HONEST OFFSET = press minus the beat they actually followed (removes beat-lag)")
    print(f"  {describe([t.follow_err_ms for t in trials])}")
    print()
    print("=> If the 'offset' is dominated by beat-lag, the human is not inaccurate:")
    print("   they are synchronising fine, to the WRONG beat, which is a CUE DESIGN bug.")
    print()

    # ---------- drift over the session ----------
    print("DRIFT ACROSS THE SESSION (learning / fatigue)")
    half = len(trials) // 2
    for name, chunk in (("first half", trials[:half]), ("second half", trials[half:])):
        if chunk:
            print(f"  {name:<12} {describe([t.offset_ms for t in chunk])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
