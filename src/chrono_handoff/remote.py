"""A conductor as seen from another process.

The panel and the native window must be able to run in their own process while
the **daemon stays the single clock authority**.  Two conductors would each
schedule their own copy of the cue and the human would hear the beep twice --
so the UI process never owns a clock; it reads schedule and status over IPC.

This also gives the right failure mode: if the UI process dies, cues still fire
(and are still observed); only the display is lost.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .model import Event, RunStatus


class RemoteConductor:
    """Duck-typed stand-in for :class:`~chrono_handoff.conductor.Conductor`.

    Implements exactly the surface :class:`~chrono_handoff.webui.CuePanel`
    touches, so the panel cannot tell the difference.  Methods the panel must
    not call (``submit``, ``cue``) raise rather than silently doing nothing --
    a UI process that thinks it can arm cues would be a second clock.
    """

    def __init__(self, client: Any, calibration_path: str | None = None) -> None:
        self._c = client
        self._armed_hooks: list[Callable[[str], None]] = []
        self.calibration_path = calibration_path
        self._last_seen: str | None = None

    # -- armed notification -------------------------------------------------

    def on_armed(self, fn: Callable[[Any], None]) -> None:
        """Register the panel's hook.  Fired by :meth:`poll_for_armed`."""
        self._armed_hooks.append(fn)

    def _fire_armed(self, run: Any) -> None:
        for fn in list(self._armed_hooks):
            try:
                fn(run)
            except Exception:
                pass

    def poll_for_armed(self) -> str | None:
        """Ask the daemon which run is current; notify hooks on a change.

        This is how a separate UI process learns that the agent armed a cue.
        Polling (not pushing) is deliberate: it needs no reverse channel, so the
        window cannot be broken by a firewall, a restarted daemon, or an IPC
        protocol change, and a missed poll is self-healing on the next tick.
        """
        try:
            runs = self._c.call("list")["runs"]
        except Exception:
            return None
        if not runs:
            return None
        # The daemon lists newest last; the most recent run is the interesting one.
        current = runs[-1]
        run_id = current.get("run_id")
        if not run_id or run_id == self._last_seen:
            return run_id
        self._last_seen = run_id
        self._fire_armed(_RunRef(run_id, current.get("status", ""), current))
        return run_id

    def forget(self) -> None:
        """Allow the same run id to notify again (used after an explicit reset)."""
        self._last_seen = None

    def watch_in_background(self, interval_s: float = 0.35) -> Callable[[], None]:
        """Drive :meth:`poll_for_armed` on a thread; returns a stop function.

        Both display hosts need exactly this loop -- the split-mode window and
        ``chrono panel`` -- so it lives next to the method it drives rather than
        being copied into each.  A copy is how ``chrono panel`` ended up with a
        conductor stub whose ``on_armed`` could never fire: the page then never
        learned that a cue had been armed, and waited forever.

        Deliberately a daemon thread: a display client that outlives its own
        process has nothing useful to do.
        """
        stop = threading.Event()

        def loop() -> None:
            while not stop.is_set():
                try:
                    self.poll_for_armed()
                except Exception:
                    pass
                stop.wait(interval_s)

        threading.Thread(target=loop, name="chrono-remote-watch", daemon=True).start()
        return stop.set

    # -- panel surface ------------------------------------------------------

    def clock_sync_payload(self) -> dict[str, float]:
        try:
            return self._c.call("clock")
        except Exception:
            # A clock sample is advisory; the page can still fall back to
            # estimate-from-HTTP timing rather than failing to arm.
            return {"server_recv_wall": 0.0, "server_send_wall": 0.0, "server_mono": 0.0}

    def schedule_payload(self, run_id: str) -> dict[str, Any]:
        return self._c.call("schedule", run_id=run_id)

    def status(self, run_id: str, include_events: bool = False) -> dict[str, Any]:
        return self._c.call("status", run_id=run_id, include_events=include_events)

    def get(self, run_id: str) -> Any:
        try:
            return _RunRef(run_id, "", self._c.call("status", run_id=run_id))
        except Exception:
            return None

    def events(self, run_id: str, since_seq: int = 0) -> list[Event]:
        raw = self._c.call("observe", run_id=run_id, since_seq=since_seq)["events"]
        return [Event.from_json(json.dumps(e)) for e in raw]

    def inject(self, run_id: str, payload: str = "signal", **kw: Any) -> Any:
        params: dict[str, Any] = {"run_id": run_id, "payload": payload}
        if kw.get("wall_t") is not None:
            params["wall_t"] = kw["wall_t"]
        return self._c.call("press", **params)

    def note_panel_press(self, run_id: str, wall_t: float | None = None) -> bool:
        """Journal a panel click that is NOT this run's evidence.

        CuePanel calls this whenever the run is observed by a real sensor
        instead of the panel.  It belongs to the "surface CuePanel touches"
        promised by the class docstring, but was missing -- so pressing the
        button on a display panel (``chrono panel``, split mode) raised
        AttributeError inside the HTTP handler: the click was neither recorded
        nor counted, it just failed.  Distinct from inject() on purpose: this
        path must never satisfy the run.
        """
        r = self._c.call("note_press", run_id=run_id, wall_t=wall_t)
        return bool(r.get("ok", False))

    def go(self, run_id: str, in_s: float = 0.0) -> Any:
        return self._c.call("go", run_id=run_id, in_s=in_s)

    def cancel(self, run_id: str, reason: str = "panel_cancel") -> dict[str, Any]:
        return self._c.call("cancel", run_id=run_id, reason=reason)

    def stats(self) -> dict[str, Any]:
        try:
            return self._c.call("list")["stats"]
        except Exception:
            return {}

    # -- calibration (read-only from the UI side) --------------------------

    @property
    def calibration(self) -> "_RemoteCalibration":
        return _RemoteCalibration(self._c)

    # -- things a UI must never do -----------------------------------------

    def submit(self, plan: Any, paused: bool = False) -> Any:
        raise RuntimeError(
            "this process is a display client and does not own the clock; "
            "arm cues through the daemon (chrono_cue / chrono submit)"
        )

    def cue(self, **kwargs: Any) -> Any:
        raise RuntimeError(
            "this process is a display client and does not own the clock; "
            "arm cues through the daemon (chrono_cue / chrono submit)"
        )

    def start(self) -> "RemoteConductor":
        return self

    def stop(self, terminate_runs: bool = False) -> None:
        return

    def wait_cue_audit(self, run_id: str, timeout_s: float = 5.0) -> bool:  # pragma: no cover
        return True

    def on_observation(self, fn: Callable[..., None]) -> None:
        return

    def on_panel_open(self, fn: Callable[[str], None]) -> None:
        return


class _PlanRef:
    """The sliver of CuePlan a display client can read from a status snapshot.

    Deliberately tiny: a remote panel needs to know *what the run is waiting
    for* so it can decide whether a click is evidence, and nothing else.
    """

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.meta: dict[str, Any] = snapshot.get("meta") or {}
        self.expect = _ExpectRef(snapshot)


class _ExpectRef:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.source: str = snapshot.get("expect_source") or "none"


class _RunRef:
    """Minimal run object so shared code can read ``run.id`` / ``run.status`` / ``run.plan``.

    ``status`` is a :class:`~chrono_handoff.model.RunStatus`, not the raw string
    from the snapshot, and ``plan`` carries the expect source.  CuePanel reads
    ``run.status.value`` and ``run.plan.expect.source``, so a display client that
    exposed only strings raised AttributeError inside ``/api/press`` -- the
    "panel cannot tell the difference" promise in RemoteConductor's docstring was
    true only for the hosted conductor.
    """

    def __init__(self, run_id: str, status: str, snapshot: dict[str, Any]) -> None:
        self.id = run_id
        self.run_id = run_id
        raw = status or snapshot.get("status") or RunStatus.PENDING.value
        # Fail fast on an unknown status rather than inventing one: a display
        # client acting on a guess is worse than one that says it is confused.
        self.status = RunStatus(raw)
        self.plan = _PlanRef(snapshot)
        self._snap = snapshot

    def snapshot(self, include_events: bool = False) -> dict[str, Any]:
        return dict(self._snap)

    def __getattr__(self, item: str) -> Any:
        try:
            return self._snap[item]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(item) from exc


class _RemoteCalibration:
    """Read-only view of the daemon's calibration store."""

    def __init__(self, client: Any) -> None:
        self._c = client
        self.path = ""

    def all(self) -> dict[str, Any]:
        try:
            items = self._c.call("calibration").get("items", {})
        except Exception:
            return {}
        return {k: _Cal(v) for k, v in items.items()}


class _Cal:
    def __init__(self, d: dict[str, Any]) -> None:
        self.offset_ms = float(d.get("offset_ms", 0.0))
        self.n = int(d.get("n", 0))
        self._mad = float(d.get("mad_ms", 0.0))

    def spread_ms(self) -> float:
        return self._mad
