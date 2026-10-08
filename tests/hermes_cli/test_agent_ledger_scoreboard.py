"""Lane M.1: scoreboard-aware chooser tier (choose_agent + load_scoreboard).

Proves the card's acceptance invariants:

- a defaults entry with real evidence (wins+losses >= 1) still wins outright;
- a 0-0 SEED defaults entry does NOT short-circuit: the scoreboard tier
  ranks (provider, model) cells for the kind by land rate (>= 3 cards),
  tie-break cheaper mean cost over PRICED cards only;
- the reason string cites the scoreboard numbers it saw;
- assignee slicing only when rows carry the field AND >= 3 survive,
  otherwise kind-only and the reason never claims assignee data;
- missing/corrupt scoreboard file -> tier skipped, legacy behaviour,
  no exception;
- the every-Nth probe still fires (scoreboard tier, runner-up);
- no defaults entry at all -> scoreboard tier NOT consulted (that path
  stays byte-identical legacy, matching test_agent_ledger_defaults.py).

All hermetic: HERMES_ROUTER_DEFAULTS and HERMES_SCOREBOARD point at
per-test tmp paths; no live board, no live scoreboard touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import agent_ledger as al


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin both files to per-test paths so nothing on disk leaks in."""
    monkeypatch.setenv("HERMES_ROUTER_DEFAULTS", str(tmp_path / "defaults.json"))
    monkeypatch.setenv("HERMES_SCOREBOARD", str(tmp_path / "scoreboard.json"))


def _write_defaults(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _seed_entry(provider="zai", model="glm-5.1"):
    return {"provider": provider, "model": model, "updated_at": "now",
            "evidence": {"wins": 0, "losses": 0, "challenger": None,
                         "incumbent": "seed=live default 2026-09-24"}}


def _evidence_entry(wins=3, losses=1, provider="openrouter",
                    model="openrouter/qwen/qwen3-coder",
                    challenger="anthropic/claude-opus-4-7"):
    return {"provider": provider, "model": model, "updated_at": "now",
            "evidence": {"wins": wins, "losses": losses,
                         "challenger": challenger, "incumbent": challenger}}


def _card(kind="build", outcome="landed", provider="zai", model="glm-5.1",
          cost=0.10, cost_status="estimated", **extra):
    row = {"card_id": "t_x", "board": "b", "kind": kind, "outcome": outcome,
           "harry": "none", "ts": "2026-10-08T00:00:00+00:00", "runs": 1,
           "model": model, "provider": provider, "tokens_in": 1,
           "tokens_out": 1, "cost_usd": cost, "cost_status": cost_status,
           "latency_s": 1.0}
    row.update(extra)
    return row


def _write_scoreboard(path: Path, cards: list[dict], window_days: int = 7) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": "2026-10-08T13:42:31+00:00", "window_days": window_days,
        "cards_done": len(cards), "cards": cards,
    }), encoding="utf-8")
    return path


def _fake_ledger(rows):
    return {"window_days": 14, "generated_at": 0, "rows": rows,
            "totals": {"attempts": 0, "runs_considered": 0, "tasks_considered": 0}}


# --- load_scoreboard -----------------------------------------------------------

def test_load_scoreboard_reads_cards(tmp_path):
    p = _write_scoreboard(tmp_path / "scoreboard.json", [_card()])
    data = al.load_scoreboard(str(p))
    assert data["window_days"] == 7 and len(data["cards"]) == 1


def test_load_scoreboard_missing_file_is_empty(tmp_path):
    assert al.load_scoreboard(str(tmp_path / "nope.json")) == {}


def test_load_scoreboard_corrupt_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert al.load_scoreboard(str(p)) == {}


def test_load_scoreboard_no_cards_list_is_empty(tmp_path):
    p = tmp_path / "nodict.json"
    p.write_text(json.dumps({"window_days": 7}), encoding="utf-8")
    assert al.load_scoreboard(str(p)) == {}


# --- precedence (a): real evidence still wins -----------------------------------

def test_evidence_defaults_beat_scoreboard(tmp_path):
    _write_defaults(tmp_path / "defaults.json",
                    {"build": _evidence_entry(wins=3, losses=1)})
    _write_scoreboard(tmp_path / "scoreboard.json",
                      [_card(provider="deepseek", model="deepseek-v4-pro")
                       for _ in range(5)])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert (r.provider, r.model) == ("openrouter", "openrouter/qwen/qwen3-coder")
    assert r.reason == "defaults-file 3-1 vs anthropic/claude-opus-4-7"


# --- precedence (b): seed defers to scoreboard ----------------------------------

def test_seed_defaults_lose_to_scoreboard_tier(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="zai", model="glm-5.1", outcome="landed"),
        _card(provider="zai", model="glm-5.1", outcome="landed"),
        _card(provider="zai", model="glm-5.1", outcome="crashed"),
        _card(provider="deepseek", model="deepseek-v4-pro", outcome="landed"),
        _card(provider="deepseek", model="deepseek-v4-pro", outcome="landed"),
        _card(provider="deepseek", model="deepseek-v4-pro", outcome="landed"),
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    # deepseek 3/3 = 1.00 beats glm 2/3 = 0.67
    assert (r.provider, r.model) == ("deepseek", "deepseek-v4-pro")
    assert r.reason.startswith("scoreboard kind=build deepseek/deepseek-v4-pro ")
    assert "land=1.00" in r.reason and "n=3" in r.reason


def test_scoreboard_reason_cites_every_number(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="zai", model="glm-5.1", outcome="landed", cost=0.2),
        _card(provider="zai", model="glm-5.1", outcome="landed", cost=0.2),
        _card(provider="zai", model="glm-5.1", outcome="crashed", cost=0.2),
    ], window_days=7)
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert r.reason == ("scoreboard kind=build zai/glm-5.1 land=0.67 n=3 "
                        "cost/landed=0.3000 window=7d")


def test_ranking_uses_landed_or_review_passed(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"review": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(kind="review", provider="a", model="mA", outcome="review-passed"),
        _card(kind="review", provider="a", model="mA", outcome="review-passed"),
        _card(kind="review", provider="a", model="mA", outcome="review-passed"),
        _card(kind="review", provider="b", model="mB",
              outcome="review-requested-changes"),
        _card(kind="review", provider="b", model="mB",
              outcome="review-requested-changes"),
        _card(kind="review", provider="b", model="mB",
              outcome="review-requested-changes"),
    ])
    r = al.choose_agent("review", ledger=_fake_ledger([]))
    assert (r.provider, r.model) == ("a", "mA")
    assert "land=1.00" in r.reason


def test_cost_tie_break_cheaper_mean_priced_wins(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="a", model="mA", outcome="landed", cost=0.30),
        _card(provider="a", model="mA", outcome="landed", cost=0.30),
        _card(provider="a", model="mA", outcome="landed", cost=0.30),
        _card(provider="b", model="mB", outcome="landed", cost=0.10),
        _card(provider="b", model="mB", outcome="landed", cost=0.10),
        _card(provider="b", model="mB", outcome="landed", cost=0.10),
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    # same land rate 1.00 -> cheaper mean cost wins: b (0.10) < a (0.30)
    assert (r.provider, r.model) == ("b", "mB")


def test_unknown_cost_status_never_fakes_a_cheap_average(tmp_path):
    """cost_status 'unknown' cards are excluded from the priced mean; a cell
    whose cards are all unpriced reports cost/landed=? (never 0.0000)."""
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="zai", model="glm-5.1", outcome="landed",
              cost=0.0, cost_status="unknown"),
        _card(provider="zai", model="glm-5.1", outcome="landed",
              cost=0.0, cost_status="unknown"),
        _card(provider="zai", model="glm-5.1", outcome="crashed",
              cost=0.0, cost_status="unknown"),
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert (r.provider, r.model) == ("zai", "glm-5.1")
    assert "cost/landed=?" in r.reason


def test_cells_under_three_cards_are_dropped(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="a", model="mA", outcome="landed"),  # only 2 -> no rank
        _card(provider="b", model="mB", outcome="crashed"),
        _card(provider="b", model="mB", outcome="crashed"),
        _card(provider="b", model="mB", outcome="crashed"),  # 3 but 0.00 rate
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    # only b qualifies (3 cards); it wins even at land=0.00
    assert (r.provider, r.model) == ("b", "mB")
    assert "land=0.00" in r.reason


# --- fallback: missing/corrupt scoreboard -> legacy ------------------------------

def test_missing_scoreboard_falls_back_to_ledger(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    # no scoreboard file at the pinned path
    ledger = _fake_ledger([{
        "kind": "build", "provider": "prov_ledger", "model": "mLedger",
        "attempts": 5, "completed": 5, "review_pass": 4, "review_fail": 1,
        "review_pass_rate": 0.8, "blocked_needs_input": 0,
        "median_duration_s": 100, "tokens_in": None, "tokens_out": None,
        "cost_usd": 1.0, "cost_per_attempt": 0.2,
    }])
    r = al.choose_agent("build", ledger=ledger)
    assert (r.provider, r.model) == ("prov_ledger", "mLedger")
    assert r.reason == "ledger"


def test_corrupt_scoreboard_falls_back_no_exception(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    p = tmp_path / "scoreboard.json"
    p.write_text("]]] not json {{{", encoding="utf-8")
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert r.reason == "static-no-data"
    assert (r.provider, r.model) == al._STATIC_ROLES["build"]


def test_scoreboard_no_qualifying_cell_falls_back_to_ledger(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(kind="research", provider="a", model="mA", outcome="landed"),
        _card(kind="research", provider="a", model="mA", outcome="landed"),
        _card(kind="research", provider="a", model="mA", outcome="landed"),
    ])
    ledger = _fake_ledger([{
        "kind": "build", "provider": "prov_ledger", "model": "mLedger",
        "attempts": 4, "completed": 4, "review_pass": 4, "review_fail": 0,
        "review_pass_rate": 1.0, "blocked_needs_input": 0,
        "median_duration_s": 100, "tokens_in": None, "tokens_out": None,
        "cost_usd": 1.0, "cost_per_attempt": 0.25,
    }])
    r = al.choose_agent("build", ledger=ledger)
    assert r.reason == "ledger"
    assert (r.provider, r.model) == ("prov_ledger", "mLedger")


def test_no_defaults_entry_at_all_skips_scoreboard(tmp_path):
    """No defaults entry -> byte-identical legacy: the scoreboard is NOT
    consulted (test_agent_ledger_defaults.py pins this contract)."""
    # defaults file exists but has no 'build' key
    _write_defaults(tmp_path / "defaults.json", {"review": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json",
                      [_card(provider="x", model="mX") for _ in range(5)])
    ledger = _fake_ledger([{
        "kind": "build", "provider": "prov_ledger", "model": "mLedger",
        "attempts": 4, "completed": 4, "review_pass": 4, "review_fail": 0,
        "review_pass_rate": 1.0, "blocked_needs_input": 0,
        "median_duration_s": 100, "tokens_in": None, "tokens_out": None,
        "cost_usd": 1.0, "cost_per_attempt": 0.25,
    }])
    r = al.choose_agent("build", ledger=ledger)
    assert r.reason == "ledger"
    assert (r.provider, r.model) == ("prov_ledger", "mLedger")


# --- assignee slicing -------------------------------------------------------------

def test_assignee_absent_is_kind_only(tmp_path):
    """Rows carry no assignee field (today's scoreboard shape): the filter
    is skipped and the reason never claims assignee data."""
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="a", model="mA", outcome="landed"),
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]), assignee="pilot")
    assert (r.provider, r.model) == ("a", "mA")
    assert "assignee" not in r.reason


def test_assignee_field_present_slices_when_three_survive(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    cards = [
        _card(provider="a", model="mA", outcome="landed", assignee="pilot"),
        _card(provider="a", model="mA", outcome="landed", assignee="pilot"),
        _card(provider="a", model="mA", outcome="landed", assignee="pilot"),
        _card(provider="b", model="mB", outcome="landed", assignee="other"),
        _card(provider="b", model="mB", outcome="landed", assignee="other"),
        _card(provider="b", model="mB", outcome="landed", assignee="other"),
    ]
    _write_scoreboard(tmp_path / "scoreboard.json", cards)
    r = al.choose_agent("build", ledger=_fake_ledger([]), assignee="other")
    assert (r.provider, r.model) == ("b", "mB")
    # kind-only view would have been an arbitrary tie; assignee view picked b
    r2 = al.choose_agent("build", ledger=_fake_ledger([]), assignee="pilot")
    assert (r2.provider, r2.model) == ("a", "mA")


def test_assignee_slice_fewer_than_three_falls_back_to_kind_only(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    cards = [
        _card(provider="a", model="mA", outcome="landed", assignee="pilot"),
        _card(provider="b", model="mB", outcome="landed", assignee="other"),
        _card(provider="b", model="mB", outcome="crashed", assignee="other"),
        _card(provider="b", model="mB", outcome="crashed", assignee="other"),
    ]
    _write_scoreboard(tmp_path / "scoreboard.json", cards)
    # 'pilot' has only 1 row (< 3) -> kind-only view: a=1/1 (dropped, <3
    # cards), b=1/3 -> b wins kind-only.
    r = al.choose_agent("build", ledger=_fake_ledger([]), assignee="pilot")
    assert (r.provider, r.model) == ("b", "mB")


# --- probe still fires --------------------------------------------------------------

def test_probe_still_fires_on_nth_scoreboard(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="a", model="mA", outcome="landed"),
        _card(provider="b", model="mB", outcome="landed"),
        _card(provider="b", model="mB", outcome="landed"),
        _card(provider="b", model="mB", outcome="crashed"),
    ])
    r0 = al.choose_agent("build", ledger=_fake_ledger([]),
                         cards_of_kind_created_so_far=0)
    assert r0.probe is False and (r0.provider, r0.model) == ("a", "mA")
    r5 = al.choose_agent("build", ledger=_fake_ledger([]),
                         cards_of_kind_created_so_far=5)
    assert r5.probe is True and (r5.provider, r5.model) == ("b", "mB")
    assert r5.reason.startswith("scoreboard kind=build b/mB ")


def test_ops_never_probed_scoreboard(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"ops": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(kind="ops", provider="a", model="mA", outcome="landed"),
        _card(kind="ops", provider="a", model="mA", outcome="landed"),
        _card(kind="ops", provider="a", model="mA", outcome="landed"),
        _card(kind="ops", provider="b", model="mB", outcome="landed"),
        _card(kind="ops", provider="b", model="mB", outcome="landed"),
        _card(kind="ops", provider="b", model="mB", outcome="landed"),
    ])
    r = al.choose_agent("ops", ledger=_fake_ledger([]),
                        cards_of_kind_created_so_far=5)
    assert r.probe is False
    assert (r.provider, r.model) == ("a", "mA")  # tie -> cheaper.. both free? a by name


# --- reason format -------------------------------------------------------------------

def test_reason_matches_card_regex(tmp_path):
    """The acceptance regex from the card body (raw string, no escapes)."""
    import re
    _write_defaults(tmp_path / "defaults.json", {"build": _seed_entry()})
    _write_scoreboard(tmp_path / "scoreboard.json", [
        _card(provider="zai", model="glm-5.1", outcome="landed"),
        _card(provider="zai", model="glm-5.1", outcome="landed"),
        _card(provider="zai", model="glm-5.1", outcome="crashed"),
    ])
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert re.search(r"scoreboard kind=build .+ land=0\.\d+ n=\d+", r.reason)
