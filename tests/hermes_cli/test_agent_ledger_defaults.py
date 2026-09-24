"""Tests for the M.4 router defaults file + chooser integration (slice c).

Proves the three acceptance invariants from the card:

(a) defaults file present for a kind -> ``choose_agent`` returns that
    entry and the reason cites the evidence numbers ("3-1 vs
    anthropic/claude-opus-4-7");
(b) defaults file absent -> ``choose_agent`` output equals legacy
    behaviour for the same board state (byte-identical);
(c) corrupt defaults file -> legacy behaviour, no exception.

All hermetic: HERMES_ROUTER_DEFAULTS points at tmp paths, HERMES_HOME is
sandboxed by the suite conftest, no live board touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import agent_ledger as al


@pytest.fixture(autouse=True)
def _no_live_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point load_defaults at a per-test path so the committed seed in the
    repo checkout never leaks into these assertions."""
    monkeypatch.setenv("HERMES_ROUTER_DEFAULTS", str(tmp_path / "defaults.json"))


def _write_defaults(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _entry(provider="openrouter", model="openrouter/qwen/qwen3-coder",
           wins=3, losses=1, challenger="anthropic/claude-opus-4-7"):
    return {"provider": provider, "model": model, "updated_at": "now",
            "evidence": {"wins": wins, "losses": losses,
                         "challenger": challenger, "incumbent": challenger}}


def _cell(kind, provider, model, attempts, pass_, fail, cost=None):
    total = pass_ + fail
    return {
        "kind": kind, "provider": provider, "model": model,
        "attempts": attempts, "completed": attempts,
        "review_pass": pass_, "review_fail": fail,
        "review_pass_rate": (pass_ / total) if total else None,
        "blocked_needs_input": 0, "median_duration_s": 100,
        "tokens_in": None, "tokens_out": None, "cost_usd": cost,
        "cost_per_attempt": (cost / attempts) if cost is not None else None,
    }


def _fake_ledger(rows):
    return {"window_days": 14, "generated_at": 0, "rows": rows,
            "totals": {"attempts": sum(r["attempts"] for r in rows),
                       "runs_considered": sum(r["attempts"] for r in rows),
                       "tasks_considered": 0}}


# --- load_defaults -----------------------------------------------------------

def test_load_defaults_returns_dict(tmp_path):
    p = _write_defaults(tmp_path / "d.json", {"build": _entry()})
    assert al.load_defaults(str(p)) == {"build": _entry()}


def test_load_defaults_missing_file_is_empty(tmp_path):
    assert al.load_defaults(str(tmp_path / "nope.json")) == {}


def test_load_defaults_corrupt_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert al.load_defaults(str(p)) == {}


def test_load_defaults_non_dict_is_empty(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    assert al.load_defaults(str(p)) == {}


# --- (a) defaults file wins ---------------------------------------------------

def test_choose_agent_returns_defaults_entry_with_evidence_reason(tmp_path):
    p = _write_defaults(tmp_path / "defaults.json",
                        {"build": _entry(wins=3, losses=1,
                                         challenger="anthropic/claude-opus-4-7")})
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert (r.provider, r.model) == ("openrouter", "openrouter/qwen/qwen3-coder")
    assert r.reason == "defaults-file 3-1 vs anthropic/claude-opus-4-7"


def test_choose_agent_defaults_beats_even_a_populated_ledger(tmp_path):
    """The defaults file wins FIRST: a ledger that would rank another model
    must not override the promoted default."""
    _write_defaults(tmp_path / "defaults.json", {"build": _entry()})
    ledger = _fake_ledger([_cell("build", "prov_a", "mA", 5, 4, 1)])
    r = al.choose_agent("build", ledger=ledger, cards_of_kind_created_so_far=0)
    assert (r.provider, r.model) == ("openrouter", "openrouter/qwen/qwen3-coder")
    assert r.reason.startswith("defaults-file ")


def test_choose_agent_reason_mentions_evidence_numbers(tmp_path):
    _write_defaults(tmp_path / "defaults.json", {"build": _entry(wins=5, losses=0)})
    r = al.choose_agent("build", ledger=_fake_ledger([]))
    assert "5-0" in r.reason
    assert "anthropic/claude-opus-4-7" in r.reason


# --- (b) absent -> byte-identical legacy --------------------------------------

def test_absent_defaults_ledger_behaviour_unchanged(tmp_path):
    """No defaults file: same ranking, same probe rule, same reason string as
    the pre-slice-c chooser."""
    # two cells: prov_a better, prov_b runner-up (the exact shape the legacy
    # tests assert against in test_agent_ledger.py).
    ledger = _fake_ledger([
        _cell("build", "prov_a", "mA", 5, 4, 1),
        _cell("build", "prov_b", "mB", 5, 1, 2),
    ])
    r = al.choose_agent("build", ledger=ledger, cards_of_kind_created_so_far=0)
    assert (r.provider, r.model) == ("prov_a", "mA")
    assert r.reason == "ledger"
    assert r.probe is False
    assert r.runner_up == ("prov_b", "mB")

    # 5th card -> probe on runner-up.
    r5 = al.choose_agent("build", ledger=ledger,
                         cards_of_kind_created_so_far=5)
    assert r5.probe is True
    assert (r5.provider, r5.model) == ("prov_b", "mB")


def test_absent_defaults_static_fallback_unchanged(tmp_path):
    r = al.choose_agent("unknown", ledger=_fake_ledger([]),
                        cards_of_kind_created_so_far=0)
    assert r.reason == "static-no-data"
    assert (r.provider, r.model) == al._STATIC_ROLES["unknown"]


def test_default_path_resolves_committed_seed_in_checkout(
        tmp_path, monkeypatch):
    """Without the env override, load_defaults() resolves the committed seed
    at <repo>/lessons/router_defaults.json (day one is present, not absent)."""
    monkeypatch.delenv("HERMES_ROUTER_DEFAULTS", raising=False)
    data = al.load_defaults()
    assert isinstance(data, dict) and data  # non-empty: the seed
    assert set(data) == set(al._KINDS)


# --- (c) corrupt -> legacy, no exception --------------------------------------

def test_corrupt_defaults_file_falls_back_to_legacy(tmp_path):
    p = tmp_path / "defaults.json"
    p.write_text("{oops", encoding="utf-8")
    r = al.choose_agent("build", ledger=_fake_ledger([
        _cell("build", "prov_a", "mA", 5, 4, 1),
    ]), cards_of_kind_created_so_far=0)
    assert (r.provider, r.model) == ("prov_a", "mA")
    assert r.reason == "ledger"


def test_corrupt_defaults_file_no_crash_static_kind(tmp_path):
    p = tmp_path / "defaults.json"
    p.write_text("not valid json at all", encoding="utf-8")
    r = al.choose_agent("unknown", ledger=_fake_ledger([]),
                        cards_of_kind_created_so_far=0)
    assert r.reason == "static-no-data"


# --- seed file (committed data) matches current chooser output ---------------

def test_seed_file_shape_and_values(monkeypatch, tmp_path):
    """The committed seed (day one) must carry every kind with the static
    role defaults -- byte-identical decisions to today."""
    repo_lessons = Path(__file__).resolve().parent.parent.parent / "lessons" \
        / "router_defaults.json"
    data = json.loads(repo_lessons.read_text(encoding="utf-8"))
    assert set(data) == set(al._KINDS)
    for kind in al._KINDS:
        e = data[kind]
        assert (e["provider"], e["model"]) == al._STATIC_ROLES[kind]
        assert e["evidence"]["wins"] == 0 and e["evidence"]["losses"] == 0
