"""End-to-end check of the cue panel over real HTTP.

Drives the panel the way a browser would -- clock sync, fetch schedule, post a
press with a client-side timestamp -- and verifies the conductor's recorded
offset matches the injected one.  This exercises the parts that unit tests miss:
the wall-clock <-> monotonic conversion and the client timestamp path.

Run:  python scripts/e2e_panel.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chrono_handoff.conductor import Conductor  # noqa: E402
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
                    "expect_signal": {"source": "none", "timeout_s": 5.0},
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
            check(
                "cue grid landed sub-millisecond (median)",
                audit["detail"]["median_ms"] < 1.0,
                f"median={audit['detail']['median_ms']}ms max={audit['detail']['max_ms']}ms",
            )

        # ---- 10. the cue() convenience path ---------------------------
        r2 = conductor.cue("second cue", target_in_s=1.0, lead_s=0.4, ioi_ms=200, tail_beats=2,
                           expect_source="none", duration_s=5, open_panel=False)
        check("conductor.cue() arms and starts", r2["status"] == "running", f"{r2['run_id']}")
        s2 = get(base + "/api/schedule?run_id=" + r2["run_id"])
        check("schedule available for the new run", len(s2.get("beats") or []) >= 3, f"{len(s2.get('beats') or [])} beats")

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
