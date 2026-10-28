"""MCP surface: a *thin client* over the conductor.

Two deliberate architectural choices, both of which come from the failure mode
described in docs/problem.md:

1. **The MCP server owns no clock.**  If the conductor lived inside the MCP
   server process, it would die with the MCP server -- which dies with the Pi
   session, which dies with a config reload (case-studies section 5.3).  So the
   server either attaches to an external conductor over JSON-lines IPC, or runs
   one in-process for convenience with the tradeoff stated explicitly.

2. **No tool blocks on the physical world.**  ``chrono_submit`` returns an id
   and a schedule.  The agent is expected to end its turn; the countdown
   carries on without it.  A "wait" tool exists but is bounded and returns a
   snapshot, so a client-side timeout costs one query, never the run.

Tool naming follows the project vocabulary: submit / observe / wait / cancel /
status / calibrate.  All offsets are in milliseconds and milliseconds are
signed (negative = early).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from pydantic import Field

from . import CONTRACT_VERSION, __version__
from .calibrate import CalibrationStore, task_signature
from .model import CuePlan, SignalSpec, WindowSpec

SERVER_NAME = "chrono-handoff"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_IPC_PORT = 8791


# --------------------------------------------------------------------------
# transport: JSON-lines over TCP
# --------------------------------------------------------------------------


class ConductorClient:
    """Blocking, short-timeout client for the conductor daemon.

    Every call is bounded; a dead daemon must produce an error message, not a
    hung agent turn.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_IPC_PORT, timeout_s: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        req = json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n"
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(req.encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if not buf.strip():
            raise RuntimeError("conductor closed the connection without a reply")
        resp = json.loads(buf.decode("utf-8"))
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "conductor error"))
        return resp["result"]

    def healthy(self) -> bool:
        try:
            self.call("ping")
            return True
        except Exception:
            return False


def _client() -> ConductorClient:
    host = os.environ.get("CHRONO_IPC_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CHRONO_IPC_PORT", str(DEFAULT_IPC_PORT)))
    return ConductorClient(host, port)


# --------------------------------------------------------------------------
# tool implementations (transport-agnostic, so they are unit-testable)
# --------------------------------------------------------------------------


def plan_from_dict(args: dict[str, Any]) -> CuePlan:
    """Normalise the agent-facing plan schema into a :class:`CuePlan`.

    One function, used by the MCP tools, the CLI and the IPC dispatcher, so the
    three entry points cannot drift into accepting different spellings of the
    same plan.  Agent-facing names are the snake_case ones from design.md
    (``expect_signal``, ``on_window``); the internal dataclass keeps its own
    names and is never exposed directly.
    """
    import dataclasses

    def _spec(d: Any) -> SignalSpec | None:
        if not isinstance(d, dict):
            return None
        known = {f.name for f in dataclasses.fields(SignalSpec)}
        return SignalSpec(**{k: v for k, v in d.items() if k in known})

    def _win(d: Any) -> WindowSpec | None:
        if not isinstance(d, dict):
            return None
        known = {f.name for f in dataclasses.fields(WindowSpec)}
        return WindowSpec(**{k: v for k, v in d.items() if k in known})

    known_plan = {f.name for f in dataclasses.fields(CuePlan)}
    kwargs = {k: v for k, v in args.items() if k in known_plan}
    kwargs.pop("id", None)
    kwargs["id"] = args.get("run_id") or args.get("id") or ""
    # Accept BOTH spellings: the agent-facing ``expect_signal`` and the nested
    # ``expect`` that ``dataclasses.asdict`` produces when the CLI/daemon round-
    # trips a plan.  Missing this is how a raw dict reaches ``plan.expect.source``
    # and blows up only on the cross-process path -- which is exactly the path
    # the MCP client uses.
    kwargs["expect"] = _spec(args.get("expect_signal", kwargs.get("expect")))
    kwargs["verify"] = _spec(args.get("verify", kwargs.get("verify")))
    kwargs["on_window"] = _win(args.get("on_window", kwargs.get("on_window")))
    kwargs.setdefault("duration_s", float(args.get("timeout_s", 60.0)))
    kwargs.setdefault("meta", dict(args.get("meta") or {}))
    return CuePlan(**kwargs)


def _plan_from_args(args: dict[str, Any]) -> CuePlan:
    return plan_from_dict(args)


# --------------------------------------------------------------------------
# tool signatures ARE the published schema
# --------------------------------------------------------------------------
#
# These used to be ``def tool_cue(args: dict[str, Any])`` with a hand-written
# JSON Schema parked in ``TOOLS[name]["schema"]``.  ``FastMCP.add_tool`` has no
# ``inputSchema`` parameter -- it derives the schema from the function signature
# via ``func_metadata(fn)`` -- so the hand-written schemas were never published:
# every tool advertised one property called ``args``, the agent could not see a
# single parameter (``target_in_s``, ``lead_s``, ``expect_source``...), and every
# call had to be wrapped in an extra ``args`` object to satisfy validation.
#
# So the signature is now the single source of truth, and the descriptions live
# where pydantic reads them: ``Annotated[..., Field(description=...)]`` on each
# parameter, ``TypedDict`` docstrings for the nested structures.  Nothing here is
# duplicated -- changing a default changes both the schema and the behaviour.


class ExpectSignal(TypedDict, total=False):
    """Physical evidence that the human acted.  May never arrive.

    A timeout is a normal, well-defined outcome of this spec, not a failure of
    the cue: the conductor does not wait for a person, it stops waiting for one.
    """

    source: Annotated[
        Literal["console_key", "stdin_line", "http", "serial", "sim", "none"],
        Field(description="Where the evidence comes from. 'none' means the panel's own press counts."),
    ]
    pattern: Annotated[str, Field(description="Regex matched against the payload; empty = any reading counts.")]
    timeout_s: Annotated[float, Field(description="Seconds after the GO beat to keep listening.")]
    min_offset_ms: Annotated[
        float,
        Field(description="Ignore readings earlier than this offset (stray input from a previous step)."),
    ]


class WindowAction(TypedDict, total=False):
    """One machine-side action, handed to an effector."""

    type: Annotated[str, Field(description="Effector-specific action kind, e.g. 'serial_write' or 'log'.")]
    value: Annotated[str, Field(description="Payload for the action.")]


class OnWindow(TypedDict, total=False):
    """Machine actions swept between two offsets.  The machine does not wait."""

    start_off_ms: Annotated[float, Field(description="Window opens this many ms after the cue origin.")]
    end_off_ms: Annotated[float, Field(description="Window closes this many ms after the cue origin.")]
    interval_ms: Annotated[float, Field(description="Retry interval inside the window (default 300).")]
    actions: Annotated[list[WindowAction], Field(description="Actions to try, in order, on every sweep.")]


def tool_submit(
    instruction: Annotated[str, Field(description="What the human should physically do, e.g. 'press RESET once'.")],
    target_in_s: Annotated[
        float | None, Field(description="Seconds from cue start at which the PHYSICAL action should land.")
    ] = None,
    lead_s: Annotated[float, Field(description="Countdown length before the GO beat (default 3).")] = 3.0,
    ioi_ms: Annotated[
        float, Field(description="Beat interval in ms (default 500). ~500ms lets humans anticipate rather than react.")
    ] = 500.0,
    tail_beats: Annotated[
        int, Field(description="Beats kept pulsing after GO so the human entrains instead of reacting (default 6).")
    ] = 6,
    audio: Annotated[
        bool, Field(description="Beep on each beat. Strongly recommended: it roughly halves human timing error.")
    ] = True,
    visual: Annotated[bool, Field(description="Terminal/panel countdown display.")] = True,
    offset_s: Annotated[
        float,
        Field(description="Predicted human offset in seconds (positive = human fires late). Moves the cue only, never machine actions."),
    ] = 0.0,
    duration_s: Annotated[
        float, Field(description="Hard budget from cue start; guarantees a terminal verdict (default 60).")
    ] = 60.0,
    expect_signal: Annotated[
        ExpectSignal | None,
        Field(description="Physical evidence that the human acted. May never arrive; a timeout is a normal outcome."),
    ] = None,
    on_window: Annotated[
        OnWindow | None, Field(description="Machine actions swept between two offsets. The machine does not wait for the human.")
    ] = None,
    meta: Annotated[
        dict[str, Any], Field(description="Echoed back; use for task id / target / effector selection.")
    ] = Field(default_factory=dict),
    paused: Annotated[bool, Field(description="Register but do not start the clock yet.")] = False,
    queue: Annotated[
        bool,
        Field(
            description="Wait for the cue already on screen to finish instead of overwriting it "
            "(default true). Only matters for plans with audio or visual; set false to let two "
            "cues run at once."
        ),
    ] = True,
) -> dict[str, Any]:
    """Submit a time-anchored action plan and return immediately.

    The conductor renders the beat cue to the human and fires machine actions on
    absolute deadlines WITHOUT waiting for you.  Do not block on the result: end
    your turn, then poll chrono_observe / chrono_wait.  Time fields are offsets
    in seconds from the cue origin.  ``offset_s`` is the predicted human offset
    (positive = the human tends to fire late) and shifts the countdown so human
    and machine meet at the same instant.
    """
    # Validated client-side (fast, clear errors) and re-normalised server-side
    # (defence in depth: the daemon must not trust its callers).
    plan = _plan_from_args(
        {
            "instruction": instruction,
            "target_in_s": target_in_s,
            "lead_s": lead_s,
            "ioi_ms": ioi_ms,
            "tail_beats": tail_beats,
            "audio": audio,
            "visual": visual,
            "offset_s": offset_s,
            "duration_s": duration_s,
            "expect_signal": expect_signal,
            "on_window": on_window,
            "meta": meta,
        }
    )
    return _client().call("submit", plan=_plan_to_wire(plan), paused=paused, queue=queue)


def _plan_to_wire(plan: CuePlan) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(plan)


def tool_observe(
    run_id: Annotated[str, Field(description="Run to read.")],
    since_seq: Annotated[int, Field(description="Return only events with a sequence number above this.")] = 0,
) -> dict[str, Any]:
    """Incrementally read a run's event journal.

    This is the intended way to follow a run: cheap, non-blocking, resumable
    with ``since_seq``.
    """
    return _client().call("observe", run_id=run_id, since_seq=int(since_seq))


def tool_wait(
    run_id: Annotated[str, Field(description="Run to wait on.")],
    timeout_ms: Annotated[int, Field(description="Longest block in ms (capped at 25000). Default 8000.")] = 8000,
) -> dict[str, Any]:
    """Block for AT MOST timeout_ms, then return a snapshot (never 'wait until done').

    A client-side timeout therefore costs one query, not the run.  Keep it short.
    """
    return _client().call(
        "wait",
        run_id=run_id,
        timeout_s=min(float(timeout_ms) / 1000.0, 25.0),
    )


def tool_cancel(
    run_id: Annotated[str, Field(description="Run to abort.")],
    reason: Annotated[str, Field(description="Recorded in the journal next to the abort.")] = "cancelled",
) -> dict[str, Any]:
    """Abort an in-flight run.  Safe to call at any time; idempotent."""
    return _client().call("cancel", run_id=run_id, reason=reason)


def tool_status(
    run_id: Annotated[str, Field(description="One run; omit for the list of recent runs.")] = "",
    include_events: Annotated[bool, Field(description="Include the full journal in the reply.")] = False,
) -> dict[str, Any]:
    """Status of one run, or the list of recent runs.  Non-blocking."""
    if run_id:
        return _client().call("status", run_id=run_id, include_events=include_events)
    return _client().call("list")


def tool_cue(
    instruction: Annotated[str, Field(description="What the person should do, e.g. 'press RESET once'.")],
    target_in_s: Annotated[
        float, Field(description="Seconds from now until the physical action should happen (default 6).")
    ] = 6.0,
    lead_s: Annotated[float, Field(description="Countdown length before the GO beat (default 3).")] = 3.0,
    ioi_ms: Annotated[float, Field(description="Beat interval ms (default 500).")] = 500.0,
    tail_beats: Annotated[
        int, Field(description="Beats to keep pulsing after GO so the person can entrain instead of react (default 6).")
    ] = 6,
    audio: Annotated[bool, Field(description="Beep in the browser panel (default true).")] = True,
    visual: Annotated[bool, Field(description="Render the grid in the panel (default true).")] = True,
    open_panel: Annotated[bool, Field(description="Open/raise the panel window (default true).")] = True,
    prepare_only: Annotated[
        bool, Field(description="Arm without starting; the human presses Start in the panel.")
    ] = False,
    expect_source: Annotated[
        Literal["console_key", "stdin_line", "http", "serial", "sim", "none"] | None,
        Field(description="How the human's action is observed (default: the panel's own press counts as 'none')."),
    ] = None,
    duration_s: Annotated[float, Field(description="Hard budget (default 30).")] = 30.0,
    queue: Annotated[
        bool,
        Field(
            description="Wait for the cue already on screen to finish instead of overwriting it "
            "(default true). Set false to let two cues run at once."
        ),
    ] = True,
    run_id: Annotated[str, Field(description="Reuse a caller-chosen run id instead of a generated one.")] = "",
) -> dict[str, Any]:
    """Cue a human to perform a physical action at a precise instant.

    Arms an interactive countdown panel the person can see and hear.  Returns
    IMMEDIATELY with a run_id and the absolute schedule -- do NOT wait for the
    human.  The cue keeps running without you.  Use it whenever you need a person
    to press / plug / power-cycle something at a moment you specify.

    ``prepare_only=True`` arms without starting, which is what the panel's own
    "Start now" button is for -- useful when the agent wants the human to be
    ready before the countdown begins.
    """
    params: dict[str, Any] = {
        "instruction": instruction,
        "target_in_s": target_in_s,
        "lead_s": lead_s,
        "ioi_ms": ioi_ms,
        "tail_beats": tail_beats,
        "audio": audio,
        "visual": visual,
        "open_panel": open_panel,
        "paused": prepare_only,
        "duration_s": duration_s,
        "queue": queue,
    }
    if expect_source is not None:
        params["expect_source"] = expect_source
    if run_id:
        params["run_id"] = run_id
    return _client().call("cue", **params)


def tool_calibrate(
    key: Annotated[str, Field(description="Task signature; omit to report every signature.")] = "",
    offset_ms: Annotated[
        float | None, Field(description="A manual measurement to fold in. Requires key.")
    ] = None,
    reset: Annotated[bool, Field(description="Clear the store (for this key, or all of it).")] = False,
) -> dict[str, Any]:
    """Report (or reset) the adaptive human-offset estimate.

    Kept in the model-visible surface on purpose: the *plan* the agent writes
    next depends on it.  ``offset_ms`` for a task signature is the number the
    agent should pass back as ``offset_s`` so that the human and the machine
    converge on the same instant.
    """
    store = CalibrationStore()
    if reset:
        store.reset(key or None)
        return {"reset": True, "items": {k: v.offset_ms for k, v in store.all().items()}}
    if key and offset_ms is not None:
        cal = store.observe(key, float(offset_ms), source="manual")
        return {"key": cal.key, "offset_ms": cal.offset_ms, "n": cal.n, "mad_ms": cal.spread_ms()}
    if key:
        cal = store.get(key)
        return {
            "key": key,
            "offset_ms": cal.offset_ms,
            "n": cal.n,
            "mad_ms": cal.spread_ms(),
            "stale": cal.n == 0,
        }
    return {
        "items": [
            {"key": k, "offset_ms": c.offset_ms, "mad_ms": c.spread_ms(), "n": c.n}
            for k, c in sorted(store.all().items())
        ],
        "path": str(store.path),
    }


#: Registry of tools, keyed by the name the agent sees.  Deliberately only
#: ``fn`` + ``description``: the parameter schema is derived from ``fn``'s own
#: signature, so there is exactly one place to change a parameter and no way for
#: a hand-written copy to drift out of sync with the code that runs.
TOOLS: dict[str, dict[str, Any]] = {
    "chrono_submit": {
        "fn": tool_submit,
        "description": tool_submit.__doc__ or "",
    },
    "chrono_observe": {
        "fn": tool_observe,
        "description": tool_observe.__doc__ or "",
    },
    "chrono_wait": {
        "fn": tool_wait,
        "description": tool_wait.__doc__ or "",
    },
    "chrono_cancel": {
        "fn": tool_cancel,
        "description": tool_cancel.__doc__ or "",
    },
    "chrono_status": {
        "fn": tool_status,
        "description": tool_status.__doc__ or "",
    },
    "chrono_cue": {
        "fn": tool_cue,
        "description": tool_cue.__doc__ or "",
    },
    "chrono_calibrate": {
        "fn": tool_calibrate,
        "description": tool_calibrate.__doc__ or "",
    },
}


# --------------------------------------------------------------------------
# IPC server side (runs inside the daemon process)
# --------------------------------------------------------------------------


class ConductorServer:
    """JSON-lines TCP front for a Conductor instance.

    Chosen over stdio because the conductor must outlive any single client.
    One short request per connection keeps it trivially robust; the volume here
    is a handful of calls per physical action.
    """

    def __init__(self, conductor: Any, host: str = DEFAULT_HOST, port: int = DEFAULT_IPC_PORT) -> None:
        self.conductor = conductor
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        #: Set by `shutdown` from another thread; checked by the accept loop.
        self._stopping = False

    def _looks_like_conductor(self) -> bool:
        """Is something already *speaking the chrono protocol* on our port?

        Used **only after a bind has failed**, to turn an errno into a readable
        message.  Never call it before binding: the connection it opens leaves a
        socket in ``TIME_WAIT`` on the very address we are about to claim, and
        with ``SO_EXCLUSIVEADDRUSE`` that makes the subsequent ``bind`` fail.
        That is not hypothetical -- it is exactly what happened here, and it made
        ``daemon --window`` refuse to start on a completely idle port.
        """
        if not self.port:
            return False
        try:
            with socket.create_connection((self.host, self.port), timeout=0.6) as sock:
                sock.settimeout(0.6)
                sock.sendall(b'{"method":"ping","params":{}}\n')
                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            reply = json.loads(data.decode("utf-8"))
            return bool(reply.get("ok")) and "pong" in (reply.get("result") or {})
        except Exception:
            return False

    def bind(self) -> None:
        """Bind and listen, refusing to shadow an existing conductor.

        Getting this right is not cosmetic.  Measured on Windows 11: two
        conductor processes both bound 127.0.0.1:8799 and both reported success,
        after which connections were split between them at random.  For a
        long-lived component an agent depends on, that is a heisenbug of the
        worst kind -- one run's journal lands in one daemon, the next in the
        other.

        The fix is platform-specific and the names are misleading:

        * **Windows**: default bind is *not* exclusive.  ``SO_REUSEADDR`` on
          Windows means "let me hijack an address in use", which is the exact
          opposite of its POSIX meaning.  ``SO_EXCLUSIVEADDRUSE`` is what
          actually prevents a second bind.
        * **POSIX**: ``SO_REUSEADDR`` is harmless and desirable (it lets us
          rebind after ``TIME_WAIT``); an actively listening socket already
          makes a second ``bind`` fail with ``EADDRINUSE``.

        **The bind is the authority.**  We attempt it first and let it decide.
        Only when it fails do we probe, and that is purely to choose a better
        error message (``already running`` vs ``port busy / permission``).  Doing
        it in the other order is actively harmful: see
        :meth:`_looks_like_conductor`.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Use the platform default.  Measured on Windows 11:
            #
            #   flags                 restart after close   second process hijack
            #   (none)                OK                    correctly FAILS
            #   SO_EXCLUSIVEADDRUSE   OK                    correctly FAILS
            #   SO_REUSEADDR          OK                    ALLOWS the hijack
            #
            # So the default is already safe here and SO_REUSEADDR is the
            # dangerous option on Windows -- it means "let me hijack an address
            # in use", the exact opposite of its POSIX meaning.  (That is how
            # two conductors once listened on the same port and split the
            # connection stream at random.)
            #
            # The earlier explicit SO_EXCLUSIVEADDRUSE was not wrong, but it
            # made the socket refuse to bind while any connection to that port
            # was still draining, which turned a restartable daemon into one
            # that needed a wait.  On Windows the default gives the same
            # exclusivity without that cost (measured above).
            #
            # On POSIX it does NOT, and that was found the hard way: measured on
            # Linux, running scripts/e2e_panel_attach.py twice in a row fails the
            # second time with "[Errno 98] Address already in use" while `ss`
            # shows nothing listening -- just a TIME-WAIT socket from the first
            # run's own client connection.  Worse, bind() then concludes
            # "the port is held by something that is not a chrono conductor" and
            # sends the operator hunting for a process that does not exist.
            #
            # So POSIX gets the flag the comment above already said it wanted.
            # It cannot be used to hijack: an actively listening socket still
            # makes this bind fail with EADDRINUSE, which is the exclusivity that
            # matters.  Windows deliberately does not get it (see above).
            if sys.platform != "win32":
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
        except OSError as exc:
            srv.close()
            live = self._looks_like_conductor()
            if live:
                raise SystemExit(
                    f"chrono-handoff: a conductor is already answering on "
                    f"{self.host}:{self.port}.\n"
                    f"  Inspect it:  chrono status --port {self.port}\n"
                    "  Stop it:     kill the pid in ~/.chrono-handoff/daemon.json"
                ) from exc
            raise SystemExit(
                f"chrono-handoff: cannot bind {self.host}:{self.port} ({exc}).\n"
                + (
                    "  The port is held by something that is not a chrono conductor, "
                    "or was recently closed.\n"
                    "  Pick another port:  --port 8800"
                    if getattr(exc, "winerror", None) == 10048 or exc.errno == 98
                    else "  Check permissions on that port."
                )
            ) from exc
        srv.listen(16)
        if self.port == 0:
            self.port = srv.getsockname()[1]
        self._sock = srv

    def shutdown(self) -> None:
        """Make `serve_forever` return, from another thread.

        Closing the listening socket is not enough -- a thread blocked in
        `accept()` does not reliably wake up for it (Linux in particular leaves
        it waiting until the next connection arrives).  A throwaway connection
        always does, so the flag is set first and the accept loop checks it.
        """
        self._stopping = True
        if self.port:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    pass
            except OSError:
                pass

    def serve_forever(self) -> None:
        import threading

        if self._sock is None:
            self.bind()
        srv = self._sock
        assert srv is not None
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            if self._stopping:
                conn.close()
                return
            threading.Thread(target=self._handle, args=(conn,), name="chrono-ipc", daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30.0)
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            req = json.loads(data.decode("utf-8"))
            result = self.dispatch(req.get("method", ""), req.get("params") or {})
            payload = {"ok": True, "result": result}
        except Exception as exc:
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall((json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        except Exception:
            pass
        finally:
            conn.close()

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        from dataclasses import asdict

        c = self.conductor
        if method == "cue":
            return c.cue(**params)
        if method == "ping":
            # `ping` is the "who is answering" method on this channel, so it
            # carries the same two fields `/api/health` does.  A client that acts
            # on the reply (a CLI, an agent) can then tell a matching conductor
            # from one bundled by a different build.
            return {"pong": True, "pid": os.getpid(),
                    "version": __version__, "contract": CONTRACT_VERSION,
                    **c.stats()}
        if method == "update_ready":
            return c.update_ready()
        if method == "stand_down":
            # Answering this *is* what stops the daemon.  The reply is written
            # first (the teardown inside `stand_down` is deferred), which is why
            # this one may be called over the loop it is about to end.
            return c.stand_down()
        if method == "submit":
            plan = plan_from_dict(dict(params.get("plan") or {}))
            run = c.submit(
                plan,
                paused=bool(params.get("paused", False)),
                queue=bool(params.get("queue", True)),
            )
            return {
                "run_id": run.id,
                "status": run.status.value,
                "queued_behind": run.queued_behind,
                "journal": str(c.journal_dir / f"{run.id}.jsonl"),
                "hint": (
                    f"queued behind {run.queued_behind}; it starts when that cue ends"
                    if run.queued_behind
                    else "end your turn; poll chrono_observe(run_id) later"
                ),
            }
        if method == "observe":
            return {
                "run_id": params["run_id"],
                "events": [asdict(e) for e in c.events(params["run_id"], int(params.get("since_seq", 0)))],
                "snapshot": c.status(params["run_id"]),
            }
        if method == "wait":
            return c.wait(params["run_id"], float(params.get("timeout_s", 8.0)))
        if method == "cancel":
            return c.cancel(params["run_id"], params.get("reason", "cancelled"))
        if method == "schedule":
            return c.schedule_payload(params["run_id"])
        if method == "press":
            run_id = params["run_id"]
            wall_t = params.get("wall_t")
            # latency_s=0: this path exists for browser/press-button clients
            # whose timestamps never touch the OS keyboard stack.
            c.inject(run_id, payload=params.get("payload", "signal"), wall_t=wall_t, latency_s=0.0)
            return c.status(run_id)
        if method == "note_press":
            # A click that must be *recorded* but must NOT satisfy the run: the
            # plan is waiting on a real sensor, so the panel is only a note.
            # Kept separate from "press" (which injects evidence) because
            # conflating them would let a UI click close a physical loop.
            run_id = params["run_id"]
            return {"ok": c.note_panel_press(run_id, wall_t=params.get("wall_t")), "run_id": run_id}
        if method == "status":
            return c.status(params["run_id"], include_events=bool(params.get("include_events", False)))
        if method == "list":
            return {"runs": c.list_runs(), "stats": c.stats()}
        if method == "go":
            run = c.go(params["run_id"], float(params.get("in_s", 0.0)))
            return {"run_id": run.id, "status": run.status.value}
        if method == "verify":
            c.verify(params["run_id"], params.get("payload", "verified"))
            return c.status(params["run_id"])
        if method == "inject":
            c.inject(params["run_id"], params.get("payload", "signal"))
            return {"injected": True}
        if method == "analyse":
            return c.analyse(params["run_id"])
        if method == "calibration":
            store = c.calibration
            return {
                "path": str(store.path),
                "report": store.report(),
                "items": {k: {"offset_ms": v.offset_ms, "n": v.n, "mad_ms": v.spread_ms()} for k, v in store.all().items()},
            }
        raise ValueError(f"unknown method {method!r}")


# --------------------------------------------------------------------------
# MCP wiring
# --------------------------------------------------------------------------



# ------------------------------------------------------------- daemon autostart
# The MCP server is a thin client: it owns no clock.  If no daemon is running
# there is nothing to arm, and the agent gets a confusing "connection refused"
# for a tool it was told exists.  So on first use we start one -- detached, with
# the native window -- exactly as the user would have.
#
# This is opt-out rather than opt-in because the failure mode without it is
# silent: the tool is registered, the model calls it, and nothing appears.  A
# user who prefers to manage the daemon themselves can set
# CHRONO_AUTOSTART=0 and get the explicit error back.


def _daemon_healthy(client: "ConductorClient") -> bool:
    return client.healthy()


#: Per-tool `searchKeywords`, in the exact shape pi's mcp.json expects.
#: They only affect how well `mcp({search})` ranks tools -- no prompt or schema
#: cost.  Pure ASCII English, because the adapter tokenises with
#: split(/[^a-z0-9]+/) and drops every non-ASCII byte.
KEYWORDS = {
    "*": ["human in the loop", "countdown timer", "operator cue", "timing", "schedule"],
    "chrono_cue": [
        "ask human physical action", "pop up panel", "press button",
        "reset device", "notify operator now",
    ],
    "chrono_submit": [
        "declarative timeline", "schedule serial write",
        "action window sweep", "expect signal pattern",
    ],
    "chrono_observe": ["read events journal", "poll run result", "check status progress"],
    "chrono_wait": ["bounded wait snapshot", "block until verdict"],
    "chrono_cancel": ["abort run stop"],
    "chrono_status": ["list runs health stats"],
    "chrono_calibrate": ["human offset learning", "latency compensation store"],
}

#: Server-level instructions.  FastMCP forwards these to the host, giving the
#: model the same routing signal as per-tool keywords at zero per-tool cost.
INSTRUCTIONS = (
    "Cue panel for time-anchored human handoff. "
    "Use chrono_cue when the agent needs a person to perform a physical action "
    "at a specific instant (press, plug, power-cycle); it pops an always-on-top "
    "countdown panel and returns immediately. Never block on a human: arm the "
    "cue, end the turn, then poll with chrono_observe. "
    "One cue at a time: a second cue is queued behind the one on screen and "
    "starts by itself when that one finishes (queue=false to run alongside). "
    "Read `verdict`, not `status`, to decide whether a run went to plan: a cue "
    "that expected no signal ends `status: timeout` with `verdict: ok`. "
    "Keyword families: countdown, timer, operator cue, human in the loop, "
    "physical action, schedule at time, press reset, notify operator."
)


def autostart_daemon(port: int) -> dict[str, Any]:
    """Start a detached daemon if none is running.  Idempotent."""
    if os.environ.get("CHRONO_AUTOSTART", "1") in ("0", "false", "no"):
        return {"started": False, "reason": "autostart disabled"}
    client = ConductorClient(port=port, timeout_s=1.5)
    if client.healthy():
        return {"started": False, "reason": "already running"}

    exe = sys.executable
    root = Path(__file__).resolve().parents[2]      # repo root (src/..)
    env = dict(os.environ)
    src = str(root / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if not (root / "src" / "chrono_handoff").is_dir():   # installed copy
        env.pop("PYTHONPATH", None)

    import subprocess

    # Window mode.  The daemon's visible native window is the point when a human
    # is watching, but it is noise when this autostart was triggered by a mere
    # health probe (`pi-mcp-adapter doctor`, `/reload`, lifecycle="lazy"
    # metadata refresh) -- the agent has not asked for anything yet.
    #
    # CHRONO_MCP_WINDOW:
    #   "1"/unset (default) -> --window, i.e. visible window (previous behavior)
    #   "0"/"false"/"no"    -> no window and no console (see the tradeoff below)
    #   "hidden"            -> --window-hidden: start hidden, reveal on cue
    mode = os.environ.get("CHRONO_MCP_WINDOW", "1").strip().lower()
    window_args: list[str] = []
    if mode in ("0", "false", "no"):
        pass
    elif mode == "hidden":
        window_args = ["--window", "--window-hidden"]
    else:
        window_args = ["--window"]

    # Console window.  A fresh console gives the daemon somewhere to read
    # keystrokes and beep, so it is tied to whether we want any cue channel at
    # all -- see daemon.py's module docstring and inputs.py's console_key
    # source ("requires a real console").
    #
    #   mode=0  -> CREATE_NO_WINDOW.  Deliberately gives up console keys and
    #              audio; use this for headless/automated runs.
    #   mode=hidden/1 -> CREATE_NEW_CONSOLE, keeping the cue channel intact.
    #
    # Note the IPC server and the native window are unaffected either way.
    if sys.platform == "win32":
        flags = (subprocess.CREATE_NO_WINDOW if mode in ("0", "false", "no")
                 else subprocess.CREATE_NEW_CONSOLE)
    else:
        flags = 0

    proc = subprocess.Popen(
        [exe, "-m", "chrono_handoff.daemon", "--port", str(port), *window_args],
        env=env, cwd=str(root), creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for IPC to come up, so the first tool call does not race the daemon.
    from .mcp_server import ConductorClient as _C   # noqa: PLC0415

    probe = _C(port=port, timeout_s=1.0)
    deadline = time.time() + 12.0
    while time.time() < deadline:
        if probe.healthy():
            return {"started": True, "pid": proc.pid, "port": port}
        if proc.poll() is not None:
            return {"started": False, "error": f"daemon exited with {proc.returncode}"}
        time.sleep(0.2)
    proc.terminate()
    return {"started": False, "error": "daemon did not become healthy in 12s"}


def build_mcp_server() -> Any:
    """Create the MCP server.  Imported lazily so the core has no MCP dep.

    mcp 2.0 renamed `FastMCP` to `MCPServer` and moved it to
    `mcp.server.mcpserver`.  The old location is gone, so importing it
    unconditionally made `chrono-mcp` die with ModuleNotFoundError on any
    modern install.  Both classes expose the same `add_tool`/`run` surface, so
    one alias covers the whole 1.x/2.x range instead of pinning an upper bound
    that would break users whose other tools already need mcp 2.
    """
    try:
        from mcp.server.mcpserver import MCPServer as _MCP
    except ImportError:  # mcp < 2
        # Not present under mcp 2.x, so static checkers flag the import; it is
        # the supported path on 1.x.  basedpyright ignores `# type: ignore`
        # unless enableTypeIgnoreComments is on, hence the `pyright:` form.
        from mcp.server.fastmcp import FastMCP as _MCP  # pyright: ignore[reportAttributeAccessIssue]

    mcp = _MCP(SERVER_NAME, instructions=INSTRUCTIONS)

    for name, spec in TOOLS.items():
        fn = spec["fn"]
        mcp.add_tool(fn, name=name, description=spec["description"])
    return mcp


def published_tool_schemas(server: Any | None = None) -> dict[str, dict[str, Any]]:
    """The input schemas the client will actually see, for tests and `chrono doctor`.

    Exists because "the schema is derived from the signature" is a claim that
    was already wrong once without anybody noticing: ``add_tool`` silently fell
    back to the signature of ``tool(args: dict)``, so every tool advertised a
    single property named ``args`` and the real parameters were undiscoverable.
    Asking the server what it publishes is the only check that cannot lie.
    """
    import asyncio

    srv = server if server is not None else build_mcp_server()
    tools = asyncio.run(srv.list_tools())
    return {t.name: t.input_schema for t in tools}


def main() -> int:
    """Entry point for the stdio MCP server.

    The server is a thin client over IPC: it owns no clock.  If the daemon is
    not running we start one, because an agent that has been told this tool
    exists should not be greeted with "connection refused".
    """
    try:
        server = build_mcp_server()
    except Exception as exc:
        print(f"chrono-handoff MCP: cannot start ({exc}). Install with `pip install mcp`.",
              file=sys.stderr)
        return 2

    port = int(os.environ.get("CHRONO_IPC_PORT", DEFAULT_IPC_PORT))
    try:
        boot = autostart_daemon(port)
        if boot.get("started"):
            print(f"chrono-handoff: started conductor daemon on port {port} "
                  f"(pid {boot.get('pid')})", file=sys.stderr, flush=True)
    except Exception as exc:
        # Not fatal: the tools will surface a clear error if the daemon is
        # genuinely absent, and a user may prefer to start it themselves.
        print(f"chrono-handoff: daemon autostart skipped ({exc})", file=sys.stderr, flush=True)

    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
