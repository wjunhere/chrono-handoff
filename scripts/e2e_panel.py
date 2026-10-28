"""End-to-end check of the cue panel over real HTTP.

Drives the panel the way a browser would -- clock sync, fetch schedule, post a
press with a client-side timestamp -- and verifies the conductor's recorded
offset matches the injected one.  This exercises the parts that unit tests miss:
the wall-clock <-> monotonic conversion and the client timestamp path.

Run:  python scripts/e2e_panel.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chrono_handoff import CONTRACT_VERSION, __version__  # noqa: E402
from chrono_handoff.conductor import Conductor  # noqa: E402
from chrono_handoff.inputs import HttpSource  # noqa: E402
from chrono_handoff.webui import CuePanel  # noqa: E402


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<46} {detail}", flush=True)
        if not ok:
            failures.append(name)

    conductor = Conductor(audio="null", visual=False, journal_dir="logs/_e2e", key_latency_s=0.0)
    conductor.start()
    panel = CuePanel(conductor, host="127.0.0.1", port=0)
    panel.start_background()
    base = f"http://127.0.0.1:{panel.port}"
    print(f"panel at {base}\n")

    try:
        # ---- 1. the panel page is served ---------------------------------
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode("utf-8")
        # Assert on the things the panel cannot work without, rather than on a
        # cosmetic title string: the timing core, the beat grid, and the
        # press button.  A title change should not read as a broken panel.
        check(
            "GET / serves a working panel",
            all(k in html for k in ("function syncClock", "function scheduleCue", "id=\"grid\"", "id=\"press\"")),
            f"{len(html)} bytes",
        )

        # ---- 1b. locally served fonts ------------------------------------
        # The component must look the same on an air-gapped bench as online, so
        # every face is served from our own /fonts/ route and the page must not
        # reference a font CDN at all.
        check("page does not depend on a font CDN",
              "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html)
        check("page declares local @font-face", "/fonts/inter-latin.woff2" in html)
        for font in ("inter-latin.woff2", "inter-latin-ext.woff2", "jbmono-latin.woff2"):
            with urllib.request.urlopen(f"{base}/fonts/{font}", timeout=5) as fr:
                body = fr.read()
                ctype = fr.headers.get("Content-Type")
            check(f"GET /fonts/{font}", len(body) > 5000 and ctype == "font/woff2"
                  and body[:4] == b"wOF2", f"{len(body)} bytes, {ctype}")
        try:
            urllib.request.urlopen(base + "/fonts/does-not-exist.woff2", timeout=5)
            check("missing font is a 404", False, "served something")
        except urllib.error.HTTPError as exc:
            check("missing font is a 404", exc.code == 404, f"HTTP {exc.code}")

        # ---- 1c. the shell seam stays a seam ------------------------------
        # panel.html must reach the host through exactly one probe.  The point of
        # the seam is that swapping the host (pywebview -> Tauri / Qt) touches one
        # array and nothing else; a second `window.pywebview` read, or a revived
        # settings bridge, silently re-couples the page to one framework.
        # docs/shell-contract.md states the contract these guard.
        check("pywebview is reached from exactly one place",
              html.count("window.pywebview &&") == 1,
              f"{html.count('window.pywebview &&')} pywebview probe(s)")
        check("the probe list covers every implemented host",
              '["pywebview", ' in html and '["tauri", ' in html and "function shellBackend" in html)
        check("settings are not on the seam",
              not any(s in html for s in ("bridgeAPI", "waitBridge", "get_settings",
                                          "save_settings", "reset_settings", "apply_window")),
              "no stale bridge symbols")
        # A bundled shell (Tauri) gives the page an origin of its own, so a
        # relative /api/* would be asked of the bundle and 404.  Everything must
        # go through apiURL(), which is the identity function when the conductor
        # serves the page itself -- so the rule costs nothing in the browser case
        # and is what keeps the two hosts from drifting apart.
        bare = [pat for pat in ('fetch("/api/', "fetch(`/api/") if pat in html]
        check("no bare /api fetch (all go through apiURL)",
              not bare, f"bare: {bare}" if bare else "all requests via apiURL()")

        # ---- 2. health ---------------------------------------------------
        h = get(base + "/api/health")
        check("GET /api/health", h.get("ok") is True, f"current_run={h.get('current_run')}")

        # ---- 3. clock alignment -----------------------------------------
        # Mimic the browser: take the sample with the smallest round trip.
        best = None
        for _ in range(7):
            t0 = time.time()
            c = get(base + "/api/clock")
            t1 = time.time()
            rtt = t1 - t0
            server_at_t1 = c["server_recv_wall"] + rtt / 2
            if best is None or rtt < best[0]:
                best = (rtt, server_at_t1 - t1)
        assert best is not None, "no clock sample was taken"
        rtt_ms, offset = best[0] * 1000, best[1] * 1000
        check("clock sync converges", abs(offset) < 5.0, f"offset={offset:+.3f}ms rtt={rtt_ms:.3f}ms on loopback")

        # ---- 4. prepare a cue (armed but not started) -------------------
        prep = post(
            base + "/api/prepare",
            {
                "plan": {
                    "instruction": "press RESET",
                    "lead_s": 0.8,
                    "ioi_ms": 200,
                    "tail_beats": 3,
                    "audio": True,
                    "visual": True,
                    "target_in_s": 1.6,
                    "duration_s": 6.0,
                    "expect_signal": {"source": "http", "timeout_s": 5.0},
                    "meta": {"calibrate": False},
                }
            },
        )
        run_id = prep["run_id"]
        check("POST /api/prepare arms a cue", prep.get("ok") is True, f"run_id={run_id} status={prep['status']}")
        st = get(base + "/api/schedule")
        check("prepared cue has no beats yet", not st.get("beats"), f"status={st.get('status')}")

        # ---- 5. wait-armed long-poll sees it ---------------------------
        # (armed == started; prepare leaves it paused)
        started = post(base + "/api/start", {})
        check("POST /api/start begins the grid", started.get("ok") is True, f"status={started['status']}")

        armed = get(base + "/api/wait-armed?timeout=3")
        check("GET /api/wait-armed returns the run", armed.get("run_id") == run_id, f"run_id={armed.get('run_id')}")

        # ---- 6. schedule carries absolute epoch times ------------------
        sched = get(base + "/api/schedule")
        beats = sched.get("beats") or []
        check("schedule delivers the whole grid at once", len(beats) >= 4, f"{len(beats)} beats")
        check(
            "beats are absolute epoch ms and strictly spaced",
            all(b["at_ms"] > 1.6e12 for b in beats)
            and all(
                abs((beats[i + 1]["at_ms"] - beats[i]["at_ms"]) - 200.0) < 1.0 for i in range(len(beats) - 1)
            ),
            "ioi preserved to <1ms",
        )
        accents = [b["index"] for b in beats if b["accent"]]
        check("exactly one accented GO beat", len(accents) == 1, f"index={accents}")
        check(
            "tail beats follow the GO beat",
            any(b["at_ms"] > sched["zero_wall_ms"] for b in beats),
            f"tail={sum(1 for b in beats if b['at_ms'] > sched['zero_wall_ms'])}",
        )

        # ---- 7. a press with a client-side timestamp --------------------
        # Simulate the human pressing 60 ms AFTER the GO beat, using the
        # browser's own clock (offset applied), which is the accurate path.
        # Units matter here: the schedule is in epoch MILLIseconds (that is what
        # a browser can compare against performance.timeOrigin) while time.time()
        # is in epoch SECONDS.  Mixing them makes the wait loop compare
        # 1.7e9 < 1.7e12 -- always true, i.e. an infinite busy-wait.
        # Aim at 60 ms after GO.  We must send the *server* a client-clock
        # timestamp meaning "the press happened at GO+60ms".  Reconstructing
        # that from the schedule is exact (no loop-timing error), whereas
        # sleeping and sampling the clock adds the sleep overshoot.
        zero_ms = sched["zero_wall_ms"]
        delay_s = 0.060
        press_server_wall_ms = zero_ms + delay_s * 1000.0
        client_wall_ms = press_server_wall_ms - offset     # convert to client domain
        wait_until_server_s = press_server_wall_ms / 1000.0 - 0.05
        while time.time() < wait_until_server_s:
            time.sleep(0.002)
        res = post(
            base + "/api/press",
            {"run_id": run_id, "payload": "space", "client_wall_ms": client_wall_ms, "clock_offset_ms": offset},
        )
        used = res.get("used_client_time")
        got = res.get("offset_ms")
        check("POST /api/press accepted", res.get("ok") is True, f"used_client_time={used}")
        check(
            "recorded offset matches the injected delay",
            got is not None and abs(got - delay_s * 1000) < 30.0,
            f"injected≈{delay_s*1000:.0f}ms recorded={got}ms",
        )

        # ---- 8. events stream ------------------------------------------
        ev = get(base + "/api/events?since=0")
        kinds = [e["kind"] for e in ev.get("events", [])]
        check(
            "GET /api/events returns the journal",
            "human_signal" in kinds and "cue_zero" in kinds,
            f"{len(kinds)} events",
        )

        # ---- 9. tail actually plays out (not cut by the verdict) -------
        # The property under test is specifically that the pulse CONTINUES past
        # the GO beat.  Asserting "every scheduled beat fired" would be wrong:
        # the first beat is at the cue origin and is deliberately dropped when
        # the grid is armed a fraction of a millisecond later (see the
        # leading-beat skip in Conductor._schedule_beats).
        time.sleep(1.2)
        ev2 = get(base + "/api/events?since=0")
        k2 = [e["kind"] for e in ev2["events"]]
        fired_beats = [e for e in ev2["events"] if e["kind"] in ("cue_beat", "cue_zero")]
        n_beats = len(fired_beats)
        tail_fired = sum(1 for e in fired_beats if e["t"] > (next(x["t"] for x in fired_beats if x["detail"].get("accent"))))
        check("cue grid played through its tail", tail_fired == sched["tail_beats"], f"{tail_fired} tail beats fired of {sched['tail_beats']} planned")
        check(
            "every non-leading beat fired",
            n_beats >= len(beats) - 1,
            f"{n_beats} fired of {len(beats)} scheduled (1 leading beat is intentionally dropped)",
        )
        check("machine grid accuracy recorded", "cue_audit" in k2, f"cue_audit present")
        audit = next((e for e in ev2["events"] if e["kind"] == "cue_audit"), None)
        if audit:
            # Sub-millisecond landing is a claim about this *machine* as much as
            # about the code: measured 0.018 ms on the author's box and 8.067 ms on
            # a shared macOS runner, with nothing different in the code.  CI sets
            # CHRONO_TOLERANT_TIMING=1 and the number is printed rather than
            # asserted -- a build that goes red over a shared runner's scheduler is
            # a build people learn to ignore.
            detail = f"median={audit['detail']['median_ms']}ms max={audit['detail']['max_ms']}ms"
            if os.environ.get("CHRONO_TOLERANT_TIMING") == "1":
                print(f"  [INFO] cue grid landing {detail} (not asserted on this machine)", flush=True)
            else:
                check("cue grid landed sub-millisecond (median)",
                      audit["detail"]["median_ms"] < 1.0, detail)

        # ---- 10b. which build is this, and can it keep what it is told ---
        #
        # The page and the conductor are two halves that can come from different
        # builds (stale files survive an upgrade -- docs/desktop-shell.md).  Both
        # sides declare a contract and the page refuses to arm when they differ,
        # so the declaration has to be present and the two have to agree.
        health = get(base + "/api/health")
        check(
            "health declares which build is answering",
            (health.get("version"), health.get("contract")) == (__version__, CONTRACT_VERSION),
            f"version={health.get('version')} contract={health.get('contract')}",
        )
        check(
            "health says the settings are writable",
            health.get("writable") is True and not health.get("why_not_writable"),
            repr(health.get("why_not_writable")),
        )
        declared = re.search(r"const PAGE_CONTRACT = (\d+);", html)
        check(
            "the served page speaks the contract of the conductor it talks to",
            declared is not None and int(declared.group(1)) == health.get("contract"),
            f"page={declared.group(1) if declared else 'absent'} conductor={health.get('contract')}",
        )

        # ---- 10c. a settings file from a newer build ---------------------
        #
        # The real thing over real HTTP: a file this build cannot read must be
        # neither used as if it were understood nor written over.
        newer = Path("logs/_e2e/settings-newer.json")
        newer.parent.mkdir(parents=True, exist_ok=True)
        newer.write_text(json.dumps({"schema": 99, "lead_s": 9.0}), encoding="utf-8")
        ro = CuePanel(conductor, host="127.0.0.1", port=0, settings_path=newer)
        ro.start_background()
        robase = f"http://127.0.0.1:{ro.port}"
        try:
            h2 = get(robase + "/api/health")
            check(
                "a newer settings file is reported, not silently accepted",
                h2.get("writable") is False and "schema 99" in (h2.get("why_not_writable") or ""),
                str(h2.get("why_not_writable"))[:70],
            )
            before = newer.read_bytes()
            code = 0
            try:
                post(robase + "/api/settings", {"lead_s": 1.0})
            except urllib.error.HTTPError as exc:
                code = exc.code
            check("saving is refused with an error, not a quiet success", code == 500, f"HTTP {code}")
            check("the newer file is left byte-identical", newer.read_bytes() == before)
        finally:
            ro.stop()

        # ---- 11. the cue() convenience path ---------------------------
        r2 = conductor.cue("second cue", target_in_s=1.0, lead_s=0.4, ioi_ms=200, tail_beats=2,
                           expect_source="none", duration_s=5, open_panel=False)
        check("conductor.cue() arms and starts", r2["status"] == "running", f"{r2['run_id']}")
        s2 = get(base + "/api/schedule?run_id=" + r2["run_id"])
        check("schedule available for the new run", len(s2.get("beats") or []) >= 3, f"{len(s2.get('beats') or [])} beats")

        # ---- 11b. a second cue waits its turn, and the page is told ------
        #
        # A person can follow one countdown, so two live grids are not two cues:
        # the second one silently replaced the first on screen, and the operator
        # was left mid-countdown on a grid that had changed under them.  The fix
        # has two halves and both are checked here: the conductor holds the second
        # cue back, and the panel tells the page that one is waiting (otherwise the
        # operator walks away after the first and misses the second countdown,
        # which starts by itself a second later).
        r3 = conductor.cue("third cue (queued)", target_in_s=0.8, lead_s=0.3, ioi_ms=200,
                           tail_beats=2, expect_source="none", duration_s=1.2, open_panel=False)
        check("a cue arriving during another is held back, not dropped",
              r3["status"] == "pending" and r3.get("queued_behind") == r2["run_id"],
              f"status={r3['status']} behind={r3.get('queued_behind')}")
        h3 = get(base + "/api/health")
        check("the panel shows the running cue, not the queued one",
              h3.get("current_run") == r2["run_id"], f"current={h3.get('current_run')}")
        check("the panel reports the queue so the operator knows one is coming",
              r3["run_id"] in (h3.get("queued") or []), f"queued={h3.get('queued')}")
        # A queued cue is PENDING, like an armed-but-unstarted one, and must not
        # count as holding the screen.  It did: `_is_live` only asked "is the
        # status terminal?", so with three cues the fallback in `_pick_current`
        # answered the *third*, still-queued run as current the moment the
        # remembered one ended -- the page would have painted an unarmed cue's
        # instruction over a live grid.  Both halves are checked: the predicate,
        # and what the panel answers when it has to pick without a live memory.
        check("a queued cue is not a cue holding the screen",
              panel._is_live(r3["run_id"]) is False, f"_is_live({r3['run_id']})={panel._is_live(r3['run_id'])}")
        with panel._lock:
            panel._current_run = ""      # forget what is on screen, as a restarted page would
        h3b = get(base + "/api/health")
        check("...so picking from scratch lands on the running cue, not the queue",
              h3b.get("current_run") == r2["run_id"],
              f"current={h3b.get('current_run')} queued={h3b.get('queued')}")
        s3 = get(base + "/api/schedule?run_id=" + r3["run_id"])
        check("a queued cue publishes no grid (it has none yet)",
              s3.get("status") == "pending" and not s3.get("beats"),
              f"status={s3.get('status')} beats={len(s3.get('beats') or [])}")

        # The hand-over: r2 ends, r3 starts and takes the screen by itself.
        handed = None
        deadline = time.time() + 25.0
        while time.time() < deadline:
            hh = get(base + "/api/health")
            if hh.get("current_run") == r3["run_id"]:
                handed = hh
                break
            time.sleep(0.1)
        check("the screen hands over to the queued cue when the first ends",
              handed is not None, f"current={(handed or {}).get('current_run')}")
        s3b = get(base + "/api/schedule?run_id=" + r3["run_id"])
        check("...and by then it has a grid of its own",
              s3b.get("status") == "running" and bool(s3b.get("beats")),
              f"status={s3b.get('status')} beats={len(s3b.get('beats') or [])}")

        # ---- 11c. the verdict says what the state name cannot -------------
        #
        # A cue that asked the human for nothing ends `timeout` when its budget
        # runs out -- which is the *successful* end of that cue, and reads as a
        # failure to anybody who has not memorised the state machine.  `verdict`
        # is the sentence, and it is what a caller should branch on.
        #
        # `r2` has already run out (the hand-over above waited for exactly that).
        st2 = conductor.status(r2["run_id"])
        check("a cue that expected no signal ends with verdict=ok",
              st2.get("verdict") == "ok",
              f"status={st2.get('status')} verdict={st2.get('verdict')} reason={st2.get('verdict_reason')}")
        check("...even though its state name is still 'timeout'",
              st2.get("status") == "timeout", f"status={st2.get('status')}")
        check("a cancelled run says cancelled, not timeout",
              conductor.cancel(r3["run_id"], "e2e_cancel").get("verdict") == "cancelled",
              conductor.status(r3["run_id"]).get("verdict") or "-")

        # ---- 11d. the MCP tools publish their real parameters ------------
        #
        # `add_tool` derives the schema from the function signature and has no
        # way to accept a hand-written one.  A hand-written table used to sit
        # beside the tools and was never published, so every tool advertised a
        # single property named `args` and the agent could not see `target_in_s`
        # or `expect_source` at all.  Asking the server what it publishes is the
        # only check that cannot be fooled by a comment claiming otherwise.
        try:
            from chrono_handoff.mcp_server import published_tool_schemas  # noqa: PLC0415

            schemas = published_tool_schemas()
            cue_schema = schemas.get("chrono_cue") or {}
            props = set(cue_schema.get("properties") or {})
            check("chrono_cue publishes its real parameters",
                  {"instruction", "target_in_s", "lead_s", "ioi_ms", "expect_source"} <= props,
                  f"{len(props)} properties")
            check("chrono_cue does not hide everything behind one 'args' object",
                  "args" not in props, f"properties={sorted(props)[:4]}...")
            check("the only required parameter is the instruction",
                  cue_schema.get("required") == ["instruction"],
                  str(cue_schema.get("required")))
            submit_props = set((schemas.get("chrono_submit") or {}).get("properties") or {})
            check("chrono_submit publishes its nested plan fields too",
                  {"expect_signal", "on_window", "offset_s", "meta"} <= submit_props,
                  f"{len(submit_props)} properties")
            defs = (schemas.get("chrono_submit") or {}).get("$defs") or {}
            check("the nested structures carry their own field docs",
                  "ExpectSignal" in defs and "source" in (defs["ExpectSignal"].get("properties") or {}),
                  f"$defs={sorted(defs)}")
        except ImportError:
            print("  [SKIP] MCP not installed; tool schemas not published", flush=True)

        # ---- 11e. a prepared cue reaches the page at once -----------------
        #
        # `prepare_only` is the "get ready, the agent decides when to start"
        # flow, and it is the one path that never arms anything -- so `on_armed`
        # cannot announce it and the page depended on the long-poll timing out.
        # Measured on the native window: a prepared cue appeared after ~25 s,
        # which is the poll budget, not a notification.  The page must be told
        # when the cue is submitted, not when the poll gives up.
        gen = panel.wait_for_armed(0.5, 0).get("generation", 0)
        box: dict = {}

        def poll() -> None:
            """What the page is doing while the cue is prepared: blocking."""
            started = time.time()
            box["res"] = panel.wait_for_armed(10.0, gen)
            box["dt"] = time.time() - started

        th = threading.Thread(target=poll, daemon=True)
        th.start()
        time.sleep(0.4)                      # the page is now parked on its long poll
        prepared = conductor.cue(instruction="armed, not started", target_in_s=5.0,
                                lead_s=2.0, ioi_ms=300, tail_beats=1, expect_source="none",
                                duration_s=10, open_panel=False, paused=True)
        th.join(timeout=12.0)
        res = box.get("res") or {}
        waited = box.get("dt", 99.0)
        check("a prepared cue reaches a page that is already polling",
              res.get("run_id") == prepared["run_id"] and waited < 2.0,
              f"run={res.get('run_id')} after {waited:.2f}s (the poll budget was 10s)")
        check("...and it is the armed cue the panel now shows",
              panel.current_run() == prepared["run_id"],
              f"current={panel.current_run()} status={conductor.status(prepared['run_id'])['status']}")
        conductor.cancel(prepared["run_id"], "e2e_cleanup")

        # ---- 11f. `none` only reminds: a click is journalled, not counted ----
        #
        # The default `expect_source` means "do X at T, nothing watches".  A click
        # on the panel must not become the evidence for that cue -- that is what
        # made every default cue a request to press a button, and it contradicted
        # the verdict rule that reads `none` as "expects no signal".  It is still
        # journalled, so the agent can see that the human thought they acted.
        #
        # Last, because it arms a run of its own: everything above reads "the
        # current run", and a cue armed here would answer instead of the one the
        # earlier checks armed.
        rem = conductor.cue(instruction="拍一下手", target_in_s=1.0, lead_s=0.3, ioi_ms=200,
                            tail_beats=1, duration_s=4, open_panel=False, queue=False)
        time.sleep(0.3)
        r_none = post(base + "/api/press", {"run_id": rem["run_id"], "payload": "done"})
        check("a click on a reminder-only cue is not the evidence",
              r_none.get("ok") is True and r_none.get("recorded") is False,
              f"recorded={r_none.get('recorded')} reason={r_none.get('reason')}")
        check("...but it is journalled, not dropped",
              any(e.kind == "panel_press_not_evidence" for e in conductor.events(rem["run_id"])),
              f"{len(conductor.events(rem['run_id']))} events")
        conductor.cancel(rem["run_id"], "e2e_cleanup")
        time.sleep(0.6)          # let its tail land, so it is no longer a predecessor

        # ---- 11g. a chained cue starts when the GRID ends, not when the budget does
        #
        # A cue that expects no observable signal has nothing left to wait for once
        # its last beat has landed, so it must end there -- not sit out
        # `duration_s`.  That silence used to land in the *next* queued cue:
        # measured 12.0 s between two cues whose target was 2 s, because
        # `duration_s=10` was still running down (and `chrono_cue` defaults it to
        # 30, so a real chain would idle for half a minute per segment).
        chained = dict(target_in_s=1.0, lead_s=0.4, ioi_ms=200, tail_beats=2,
                       expect_source="none", duration_s=30, open_panel=False)
        first = conductor.cue(instruction="A: chained", **chained)
        second = conductor.cue(instruction="B: chained", **chained)
        gap = None
        deadline = time.time() + 12.0
        while time.time() < deadline:
            sa = conductor.status(first["run_id"])
            sb = conductor.status(second["run_id"])
            if sa.get("origin_mono") and sb.get("origin_mono"):
                gap = sb["origin_mono"] - sa["origin_mono"]
                break
            time.sleep(0.05)
        check("the next chained cue starts when the grid ends, not when the budget does",
              gap is not None and gap < 4.0,
              f"B started {gap if gap is None else round(gap, 2)}s after A "
              f"(grid ends +1.4s, duration_s=30)")
        # `A` itself was submitted while the *cancelled* cue above was still
        # pulsing, so it queued behind a run that will never finalize again.  That
        # used to strand it in PENDING forever: the release job such a run arms
        # fires at the end of ITS grid, which is before this run existed.  If A
        # never started, `gap` is None -- this names the failure instead of
        # leaving it to the timing assertion.
        check("...and a cue queued behind a cancelled-but-pulsing cue is not stranded",
              bool(conductor.status(first["run_id"]).get("origin_mono")),
              f"A status={conductor.status(first['run_id'])['status']} "
              f"behind={conductor.status(first['run_id']).get('queued_behind')}")
        conductor.cancel(second["run_id"], "e2e_cleanup")


        # ---- 12. the bind path must not ask DNS who we are --------------
        # `HTTPServer.server_bind` calls `socket.getfqdn(host)`; measured at 35.0s on
        # a GitHub macOS runner, inside the bind, so the conductor served nothing for
        # 35s after it started.  Poisoning the lookup proves the fix on any machine,
        # rather than relying on this one's resolver happening to be fast.
        import socket

        calls: list[str] = []
        real_getfqdn = socket.getfqdn

        def poisoned(host: str = "") -> str:
            calls.append(host)
            raise RuntimeError("getfqdn must not be on a bind path")

        probe = Conductor(audio="null", visual=False,
                          journal_dir=str(Path(__file__).resolve().parents[1] / "logs" / "_panel" / "dns"))
        probe.start()
        src = HttpSource(host="127.0.0.1", port=0)
        socket.getfqdn = poisoned  # type: ignore[assignment]
        try:
            bound = CuePanel(probe, host="127.0.0.1", port=0)
            # `start_background`, not `bind`: `stop()` calls `shutdown()`, which waits
            # for a serve loop that a bind-only server never started.
            bound.start_background()
            bound_port = bound.port
            bound.stop()
            src.open()
            src_port = src.bound_port
        finally:
            socket.getfqdn = real_getfqdn
            src.close()
            probe.stop()
        check("the panel binds without resolving its own host name",
              bound_port > 0 and not calls, f"port {bound_port}, getfqdn calls: {calls}")
        check("the press source binds without resolving it either",
              src_port > 0 and not calls, f"port {src_port}, getfqdn calls: {calls}")

    finally:
        panel.stop()
        conductor.stop()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all panel checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
