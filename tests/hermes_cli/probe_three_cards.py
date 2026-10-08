#!/usr/bin/env python3
"""Lane M.1 acceptance probe: three consecutive `hermes kanban create` cards
of the same kind each carry the chooser's scoreboard reason at create.

Builds, under the system temp dir:
  - a TEMP board database (HERMES_KANBAN_DB pinned at a temp file)
  - a fixture scoreboard: two models x >= 3 kind=build cards each, different
    land rates, one cost tie-break case
  - a 0-0 seed defaults fixture (HERMES_ROUTER_DEFAULTS)

then invokes the REAL `hermes kanban create` CLI three times (subprocess,
distinct titles, kind: build bodies, no --model/--provider args), reads each
card back and asserts its event log carries a chooser line matching
``scoreboard kind=build .+ land=0\\.\\d+ n=\\d+``.  Prints the three reasons
verbatim.  Exit 0 only when all three match AND name the fixture's computed
winner.

This fails when the create path skips the chooser (no chooser event) or when
seed defaults still short-circuit (the reason would read "defaults-file ...").
Never touches a live board: everything lives under one temp dir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Fixture, computed by hand and re-derived in-process below:
#   winner  prov "probe-winner" model "model-w" : 2 landed / 3 = land 0.67,
#           priced cards 0.30+0.30+0.30 -> cost/landed 0.60/2 = 0.3000
#   tiebrk  prov "probe-tiebreak" model "model-t" : also 2/3 = land 0.67 but
#           priced 0.90 each -> loses the cost tie-break (0.90 mean > 0.30)
#   runner  prov "probe-losser" model "model-l" : 1 landed / 3 = land 0.33
# The winner's land rate is deliberately BELOW 1.00: the card's acceptance
# regex is ``land=0\.\d+``, which a perfect 1.00 cell can never match.
WINNER = ("probe-winner", "model-w")
TIEBREAK_LOSER = ("probe-tiebreak", "model-t")
RUNNER = ("probe-losser", "model-l")


def _card(kind, outcome, provider, model, cost, cost_status="estimated"):
    return {"card_id": f"t_{provider}{model}{outcome[:3]}", "board": "probe",
            "kind": kind, "outcome": outcome, "harry": "none",
            "ts": "2026-10-08T00:00:00+00:00", "runs": 1, "model": model,
            "provider": provider, "tokens_in": 100, "tokens_out": 100,
            "cost_usd": cost, "cost_status": cost_status, "latency_s": 1.0}


def build_fixtures(tmp: Path) -> dict:
    cards = [
        # winner cell: 2/3 landed (0.67 - deliberately < 1.00 so the
        # acceptance regex land=0\.\d+ matches), priced 0.30 each
        _card("build", "landed", *WINNER, 0.30),
        _card("build", "landed", *WINNER, 0.30),
        _card("build", "crashed", *WINNER, 0.30),
        # cost tie-break cell: also 2/3 landed but priced 0.90 each - same
        # land rate, loses on mean priced cost (0.90 > 0.30)
        _card("build", "landed", *TIEBREAK_LOSER, 0.90),
        _card("build", "landed", *TIEBREAK_LOSER, 0.68),
        _card("build", "crashed", *TIEBREAK_LOSER, 0.68),
        # runner-up cell: 1/3 landed
        _card("build", "landed", *RUNNER, 0.10),
        _card("build", "crashed", *RUNNER, 0.10),
        _card("build", "crashed", *RUNNER, 0.10),
    ]
    scoreboard = tmp / "scoreboard.json"
    scoreboard.write_text(json.dumps({
        "generated_at": "2026-10-08T13:42:31+00:00", "window_days": 7,
        "cards_done": len(cards), "cards": cards,
    }), encoding="utf-8")

    defaults = tmp / "router_defaults.json"
    defaults.write_text(json.dumps({
        kind: {"provider": "zai", "model": "glm-5.1", "updated_at": "now",
               "evidence": {"wins": 0, "losses": 0, "challenger": None,
                            "incumbent": "seed=probe"}}
        for kind in ("build", "review", "research", "draft", "ops", "unknown")
    }), encoding="utf-8")

    # Hand-computed expectation, independently re-derived below.
    return {"scoreboard": scoreboard, "defaults": defaults}


def expected_winner() -> tuple[str, str]:
    """Re-derive the fixture's winner with the same arithmetic the tier uses
    (kept independent of the implementation under test's ranking call)."""
    cells = {
        WINNER: {"landed": 2, "cards": 3, "cost_sum": 0.90},
        TIEBREAK_LOSER: {"landed": 2, "cards": 3, "cost_sum": 2.26},
        RUNNER: {"landed": 1, "cards": 3, "cost_sum": 0.30},
    }
    ranked = sorted(
        cells.items(),
        key=lambda kv: (-(kv[1]["landed"] / kv[1]["cards"]),
                        kv[1]["cost_sum"] / kv[1]["cards"]),
    )
    return ranked[0][0]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="m1_probe_"))
    fixtures = build_fixtures(tmp)
    db = tmp / "kanban.db"

    env = os.environ.copy()
    env["HERMES_KANBAN_DB"] = str(db)
    env["HERMES_ROUTER_DEFAULTS"] = str(fixtures["defaults"])
    env["HERMES_SCOREBOARD"] = str(fixtures["scoreboard"])
    # Keep the probe hermetic: no gateway/session side channels, and no
    # delegated-child fencing inherited from a kanban-worker parent process
    # (the marker would make the CLI refuse mutations).
    env.pop("HERMES_KANBAN_BOARD", None)
    env.pop("HERMES_KANBAN_TASK", None)
    env.pop("HERMES_SESSION_KEY", None)
    env.pop("HERMES_SESSION_ID", None)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

    py = sys.executable
    titles = ["M.1 probe card one", "M.1 probe card two", "M.1 probe card three"]
    reasons: list[str] = []
    tids: list[str] = []

    for title in titles:
        body = "kind: build\n\nprobe fixture card (temp board only)"
        r = subprocess.run(
            [py, str(REPO / "hermes"), "kanban", "create", title,
             "--assignee", "pilot", "--body", body, "--json"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=180,
        )
        if r.returncode != 0:
            print(f"FAIL: create {title!r} rc={r.returncode}")
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
            return 1
        m = re.search(r"(t_[a-f0-9]+)", r.stdout)
        if not m:
            print(f"FAIL: no task id in create output:\n{r.stdout[-1000:]}")
            return 1
        tids.append(m.group(1))

        s = subprocess.run(
            [py, str(REPO / "hermes"), "kanban", "show", m.group(1), "--json"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=180,
        )
        if s.returncode != 0:
            print(f"FAIL: show {m.group(1)} rc={s.returncode}")
            print(s.stderr[-2000:])
            return 1
        data = json.loads(s.stdout)
        chooser_events = [e for e in data.get("events", [])
                          if e.get("kind") == "chooser"]
        if not chooser_events:
            print(f"FAIL: {m.group(1)} has no chooser event at create; "
                  "the create path skipped the chooser.")
            return 1
        reason = (chooser_events[-1].get("payload") or {}).get("reason", "")
        reasons.append(reason)
        task = data.get("task") or {}
        prov, model = task.get("provider_override"), task.get("model_override")
        print(f"{m.group(1)}: provider={prov} model={model}")
        print(f"  chooser: {reason}")

    pat = re.compile(r"scoreboard kind=build .+ land=0\.\d+ n=\d+")
    exp_prov, exp_model = expected_winner()
    for tid, reason in zip(tids, reasons):
        if not pat.search(reason):
            print(f"FAIL: {tid} chooser reason does not cite the scoreboard "
                  f"land rate: {reason!r}")
            return 1
        if f"{exp_prov}/{exp_model}" not in reason:
            print(f"FAIL: {tid} reason does not name the fixture's computed "
                  f"winner {exp_prov}/{exp_model}: {reason!r}")
            return 1

    print("\nPASS: three consecutive kind=build creates each carry the "
          "chooser's scoreboard reason; all name the computed winner "
          f"{exp_prov}/{exp_model}.")
    print("Reasons verbatim:")
    for reason in reasons:
        print(f"  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
