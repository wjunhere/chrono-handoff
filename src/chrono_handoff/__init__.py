"""chrono-handoff: time-anchored human-machine action handoff.

Slow planner (LLM) submits a declarative timeline; a fast, independent
conductor process owns the clock and fires cues/actions on absolute
monotonic deadlines.  The human is treated as an effector with a
measurable, calibratable latency offset.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
