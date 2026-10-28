"""chrono-handoff: time-anchored human-machine action handoff.

Slow planner (LLM) submits a declarative timeline; a fast, independent
conductor process owns the clock and fires cues/actions on absolute
monotonic deadlines.  The human is treated as an effector with a
measurable, calibratable latency offset.
"""

__version__ = "0.1.1"

#: Version of the contract between parts that are shipped separately.
#:
#: ``__version__`` moves with every release; this moves only when the *meaning*
#: of an interface does -- the host operations in ``docs/shell-contract.md``, or
#: the conductor's HTTP API and IPC methods.  It exists because the two halves of
#: one install can disagree: upgrade-time *stale files* are a measured behaviour
#: (``docs/desktop-shell.md`` §升级 -- 0.1.0 -> 0.1.1 left the old files behind),
#: so a window can end up talking to a conductor from a different build.  Every
#: client that acts on an answer compares this first; without it, "which clock am
#: I talking to" is a matter of luck and the symptom is a merely odd-looking UI.
#:
#: Declared in exactly two places -- ``/api/health`` (HTTP) and ``ping`` (IPC) --
#: both from this constant.
#:
#: 2 -> 3: a second cue no longer runs alongside the first; it is queued behind it
#: (``queue`` defaults to true) and the page reports the queue.  Both are changes
#: to what an existing answer *means*: a page from build 2 would show the newest
#: armed cue as if it were the only one, and would not know to say a second one
#: is coming -- which is exactly the stale-file mismatch this number exists to
#: catch.
CONTRACT_VERSION = 3

__all__ = ["CONTRACT_VERSION", "__version__"]
