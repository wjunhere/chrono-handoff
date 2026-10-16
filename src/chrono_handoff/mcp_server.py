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
from typing import Any

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


def tool_submit(args: dict[str, Any]) -> dict[str, Any]:
    # Validated client-side (fast, clear errors) and re-normalised server-side
    # (defence in depth: the daemon must not trust its callers).
    plan = _plan_from_args(args)
    return _client().call("submit", plan=_plan_to_wire(plan), paused=bool(args.get("paused", False)))


def _plan_to_wire(plan: CuePlan) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(plan)


def tool_observe(args: dict[str, Any]) -> dict[str, Any]:
    return _client().call("observe", run_id=args["run_id"], since_seq=int(args.get("since_seq", 0)))


def tool_wait(args: dict[str, Any]) -> dict[str, Any]:
    return _client().call(
        "wait",
        run_id=args["run_id"],
        timeout_s=min(float(args.get("timeout_ms", 8000)) / 1000.0, 25.0),
    )


def tool_cancel(args: dict[str, Any]) -> dict[str, Any]:
    return _client().call("cancel", run_id=args["run_id"], reason=args.get("reason", "cancelled"))


def tool_status(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("run_id"):
        return _client().call("status", run_id=args["run_id"], include_events=bool(args.get("include_events", False)))
    return _client().call("list")


def tool_cue(args: dict[str, Any]) -> dict[str, Any]:
    """One-call cue: pop the panel, arm the grid, start the clock.

    This is the tool the agent should reach for in the common case.  Note what
    it does **not** do: it never waits for the human.  It returns the schedule
    (absolute times) so the agent can carry on and check back later.  Blocking
    here would reintroduce exactly the problem this project exists to solve.

    ``prepare_only=True`` arms without starting, which is what the panel's own
    "Start now" button is for -- useful when the agent wants the human to be
    ready before the countdown begins.
    """
    return _client().call("cue", **args)


def tool_calibrate(args: dict[str, Any]) -> dict[str, Any]:
    """Report (or reset) the adaptive human-offset estimate.

    Kept in the model-visible surface on purpose: the *plan* the agent writes
    next depends on it.  ``suggested_offset_ms`` for a task signature is the
    number the agent should pass back as ``offset_s`` so that the human and the
    machine converge on the same instant.
    """
    store = CalibrationStore()
    if args.get("reset"):
        store.reset(args.get("key") or None)
        return {"reset": True, "items": {k: v.offset_ms for k, v in store.all().items()}}
    if args.get("key") and args.get("offset_ms") is not None:
        cal = store.observe(args["key"], float(args["offset_ms"]), source="manual")
        return {"key": cal.key, "offset_ms": cal.offset_ms, "n": cal.n, "mad_ms": cal.spread_ms()}
    key = args.get("key")
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


TOOLS: dict[str, dict[str, Any]] = {
    "chrono_submit": {
        "fn": tool_submit,
        "description": (
            "Submit a time-anchored action plan and return immediately. "
            "The conductor renders the beat cue to the human and fires machine actions on absolute "
            "deadlines WITHOUT waiting for you. Do not block on the result: end your turn, then poll "
            "chrono_observe / chrono_wait. Time fields are offsets in seconds from the cue origin. "
            "`offset_s` is the predicted human offset (positive = the human tends to fire late) and "
            "shifts the countdown so human and machine meet at the same instant."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "What the human should physically do, e.g. 'press RESET once'."},
                "lead_s": {"type": "number", "description": "Countdown length before the GO beat (default 3)."},
                "ioi_ms": {"type": "number", "description": "Beat interval in ms (default 500). ~500ms lets humans anticipate rather than react."},
                "audio": {"type": "boolean", "description": "Beep on each beat. Strongly recommended: it roughly halves human timing error."},
                "visual": {"type": "boolean", "description": "Terminal countdown bar."},
                "target_in_s": {"type": "number", "description": "Seconds from cue start at which the PHYSICAL action should land."},
                "offset_s": {"type": "number", "description": "Predicted human offset in seconds (positive = human fires late). Moves the cue only, never machine actions."},
                "duration_s": {"type": "number", "description": "Hard budget from cue start; guarantees a terminal verdict (default 60)."},
                "expect_signal": {
                    "type": "object",
                    "description": "Physical evidence that the human acted. May never arrive; timeouts are a normal outcome.",
                    "properties": {
                        "source": {"type": "string", "enum": ["console_key", "stdin_line", "http", "serial", "sim", "none"]},
                        "pattern": {"type": "string"},
                        "timeout_s": {"type": "number"},
                        "min_offset_ms": {"type": "number", "description": "Ignore signals earlier than this (stray input from a previous step)."},
                    },
                },
                "on_window": {
                    "type": "object",
                    "description": "Machine actions swept between two offsets. The machine does not wait for the human.",
                    "properties": {
                        "start_off_ms": {"type": "number"},
                        "end_off_ms": {"type": "number"},
                        "interval_ms": {"type": "number"},
                        "actions": {"type": "array", "items": {"type": "object"}},
                    },
                },
                "meta": {"type": "object", "description": "Echoed back; use for task id / target / effector selection."},
            },
            "required": ["instruction"],
        },
    },
    "chrono_observe": {
        "fn": tool_observe,
        "description": (
            "Incrementally read a run's event journal. This is the intended way to follow a run: "
            "cheap, non-blocking, resumable with since_seq."
        ),
        "schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "since_seq": {"type": "integer"}},
            "required": ["run_id"],
        },
    },
    "chrono_wait": {
        "fn": tool_wait,
        "description": (
            "Block for AT MOST timeout_ms, then return a snapshot (never 'wait until done'). "
            "A client-side timeout therefore costs one query, not the run. Keep it short."
        ),
        "schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "timeout_ms": {"type": "integer"}},
            "required": ["run_id"],
        },
    },
    "chrono_cancel": {
        "fn": tool_cancel,
        "description": "Abort an in-flight run. Safe to call at any time; idempotent.",
        "schema": {"type": "object", "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["run_id"]},
    },
    "chrono_status": {
        "fn": tool_status,
        "description": "Status of one run, or the list of recent runs. Non-blocking.",
        "schema": {"type": "object", "properties": {"run_id": {"type": "string"}, "include_events": {"type": "boolean"}}},
    },
    "chrono_cue": {
        "fn": tool_cue,
        "description": (
            "Cue a human to perform a physical action at a precise instant, by arming an interactive "
            "countdown panel they can see and hear. Returns IMMEDIATELY with a run_id and the absolute "
            "schedule -- do NOT wait for the human. The cue keeps running without you. Use it whenever "
            "you need a person to press / plug / power-cycle something at a moment you specify."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "What the person should do, e.g. 'press RESET once'."},
                "target_in_s": {"type": "number", "description": "Seconds from now until the physical action should happen (default 6)."},
                "lead_s": {"type": "number", "description": "Countdown length before the GO beat (default 3)."},
                "ioi_ms": {"type": "number", "description": "Beat interval ms (default 500)."},
                "tail_beats": {"type": "number", "description": "Beats to keep pulsing after GO so the person can entrain instead of react (default 6)."},
                "audio": {"type": "boolean", "description": "Beep in the browser panel (default true)."},
                "visual": {"type": "boolean", "description": "Render the grid in the panel (default true)."},
                "open_panel": {"type": "boolean", "description": "Open/raise the panel window (default true)."},
                "prepare_only": {"type": "boolean", "description": "Arm without starting; the human presses Start in the panel."},
                "expect_source": {"type": "string", "description": "console_key | stdin_line | http | serial | sim | none -- how the human's action is observed (default: the panel itself)."},
                "duration_s": {"type": "number", "description": "Hard budget (default 30)."},
            },
            "required": ["instruction"],
        },
    },
    "chrono_calibrate": {
        "fn": tool_calibrate,
        "description": (
            "Read or update the learned human offset. Pass key to read one signature, or key+offset_ms "
            "to fold in a manual measurement, or reset=true to clear. Use the reported offset_ms as "
            "`offset_s` in the next chrono_submit so the human and the machine converge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "offset_ms": {"type": "number"},
                "reset": {"type": "boolean"},
            },
        },
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
        if method == "submit":
            plan = plan_from_dict(dict(params.get("plan") or {}))
            run = c.submit(plan, paused=bool(params.get("paused", False)))
            return {
                "run_id": run.id,
                "status": run.status.value,
                "journal": str(c.journal_dir / f"{run.id}.jsonl"),
                "hint": "end your turn; poll chrono_observe(run_id) later",
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
