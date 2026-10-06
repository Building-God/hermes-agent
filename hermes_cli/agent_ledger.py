"""Board-derived agent performance ledger + chooser.

Builds a rolling 14-day view over the Kanban runs/events/comments the board
already carries, per (kind, provider, model), through the existing
``hermes_cli.kanban_db`` API - never raw SQL. The **served** model reported by
the completing run wins over the requested one when the two differ, so a
server-side route (Z.ai coding endpoint routing ``glm-5.1`` to ``glm-5.3``)
does not mislabel a row.

Task ``kind`` is opt-in: a card body may carry a ``kind:`` line whose value is
one of ``build|review|research|draft|ops``; anything else - including a
missing line - lands under ``unknown``. There is no keyword classifier.

``choose_agent(kind)`` returns a ``(provider, model)`` pair by review-pass
rate given >= 3 attempts; ties break by cheaper cost/attempt where a receipt
exists; if no data, callers fall back to static role routing. Every Nth
create for a kind (N=5, tunable) runs on the runner-up as a ``probe``. Ops
work is never probed.

M.4 router defaults (promote-on-evidence): ``load_defaults(path=None)``
reads the committed ``lessons/router_defaults.json`` (``{kind: {provider,
model, updated_at, evidence: {wins, losses, challenger}, pinned_until?}}``).
``choose_agent`` consults it FIRST for a kind; the Jarvis repo's
``soul/tools/router_promote.py`` writes it (apply_promotion after >= 3 net
shadow wins at <= cost; apply_pin after Harry thumbs-downs the digest line
for a 30-day pin).  Absent/corrupt file -> today's behaviour, byte-identical.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


# ---------------------------------------------------------------------------
# M.4 router defaults file (promote-on-evidence, slice c)
# ---------------------------------------------------------------------------
#
# The per-kind DEFAULT (provider, model) is not a static in this module and
# not config.yaml: it is one JSON file, COMMITTED data (defaults survive
# checkout; the runtime router ledger is ignored, the defaults file is
# truth).  Shape:
#
#     {"<kind>": {"provider": str, "model": str, "updated_at": iso,
#                 "evidence": {"wins": int, "losses": int,
#                              "challenger": "provider/model" | null,
#                              "incumbent": "provider/model" | null},
#                 "pinned_until": iso | omitted}}
#
# choose_agent reads it FIRST for a kind; delete the file -> today's
# behaviour (byte-identical fallback, proven in tests).  M.4 (the Jarvis
# repo's soul/tools/router_promote.py) is the only writer: apply_promotion
# after >= 3 net shadow wins at <= cost, apply_pin after Harry thumbs-downs
# the digest line (30-day pinned_until).  Pinned kinds are skipped by
# promotion, not by this chooser.
#
# Note (deviation, documented): the slice decision named the path
# ``hermes/lessons/router_defaults.json``.  In THIS repo the name ``hermes``
# is a tracked launcher file, so a ``hermes/lessons/`` directory cannot
# exist here; the committed file lives at ``<repo>/lessons/
# router_defaults.json``.  router_promote.write_defaults resolves the same
# location in the live checkout.

DEFAULTS_FILENAME = "router_defaults.json"


def _defaults_path() -> Path:
    """The committed defaults file in this checkout (env-overridable for the
    M.4 loop and tests)."""
    env = os.environ.get("HERMES_ROUTER_DEFAULTS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "lessons" / DEFAULTS_FILENAME


def load_defaults(path: Optional[str] = None) -> dict:
    """Read the M.4 router defaults file.

    Missing / corrupt JSON / non-dict all return ``{}`` (never raise) so the
    chooser falls back to today's behaviour.  ``path`` overrides the default
    location (tests, and the M.4 promote loop writes the live copy).
    """
    src = Path(path) if path else _defaults_path()
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}



LEDGER_WINDOW_DAYS = 14
DEFAULT_PROBE_EVERY_N = 5
_KINDS = ("build", "review", "research", "draft", "ops", "unknown")
_KIND_LINE = re.compile(r"^\s*kind:\s*([A-Za-z_-]+)\s*$", re.MULTILINE)


def extract_kind(body: Optional[str]) -> str:
    """Return the ``kind:`` value declared in a card body (build|review|
    research|draft|ops), else ``unknown``. Missing body -> unknown. Never
    guesses from title/keywords."""
    if not body:
        return "unknown"
    m = _KIND_LINE.search(body)
    if not m:
        return "unknown"
    val = m.group(1).lower()
    return val if val in _KINDS else "unknown"


def _served_provider_model(run: kb.Run, task: kb.Task) -> tuple[Optional[str], Optional[str]]:
    """The (provider, model) the ledger should credit. Prefers what the model
    actually served (from the run's metadata / summary) over what was
    requested via ``tasks.model_override``/``provider_override``."""
    md = run.metadata or {}
    # Look for the receipt shape used elsewhere (see t_5eb9182a handoff).
    served_model = (md.get("model") or md.get("served_model") or md.get("model_reported")
                    or md.get("resolved_model"))
    served_provider = md.get("provider") or md.get("served_provider") or md.get("resolved_provider")
    # A worker's kanban_complete metadata may also carry a nested receipt.
    receipts = md.get("receipts") or []
    if isinstance(receipts, list) and receipts:
        last = receipts[-1] if isinstance(receipts[-1], dict) else {}
        served_model = served_model or last.get("model")
        served_provider = served_provider or last.get("provider")
    model = served_model or task.model_override
    provider = served_provider or task.provider_override
    return (str(provider).lower() if provider else None,
            str(model) if model else None)


def _reviewer_verdict(conn, child_id: str) -> Optional[str]:
    """A reviewer child's verdict for the ledger's review-pass column:
    ``pass`` (child status=done and no changes_requested outcome on any run),
    ``fail`` (had a changes_requested outcome), or ``None`` (still open)."""
    t = kb.get_task(conn, child_id)
    if t is None:
        return None
    # Anything with a changes_requested run counts as "fail" (rework).
    for r in kb.list_runs(conn, child_id, include_active=True):
        if r.outcome == "changes_requested":
            return "fail"
    if t.status == "done":
        return "pass"
    return None


@dataclass
class LedgerCell:
    kind: str
    provider: str
    model: str
    attempts: int = 0
    completed: int = 0
    review_pass: int = 0
    review_fail: int = 0
    blocked_needs_input: int = 0
    durations_s: list[int] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cost_samples: int = 0

    def as_row(self) -> dict[str, Any]:
        med = int(statistics.median(self.durations_s)) if self.durations_s else None
        review_total = self.review_pass + self.review_fail
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "attempts": self.attempts,
            "completed": self.completed,
            "review_pass": self.review_pass,
            "review_fail": self.review_fail,
            "review_pass_rate": (self.review_pass / review_total) if review_total else None,
            "blocked_needs_input": self.blocked_needs_input,
            "median_duration_s": med,
            "tokens_in": self.tokens_in or None,
            "tokens_out": self.tokens_out or None,
            "cost_usd": round(self.cost_usd, 6) if self.cost_usd else None,
            "cost_per_attempt": (
                round(self.cost_usd / self.attempts, 6) if self.cost_usd and self.attempts else None
            ),
        }


def _harvest_receipt(md: dict) -> tuple[int, int, float]:
    """Return (tokens_in, tokens_out, cost_usd) from a run metadata dict."""
    tin = int(md.get("tokens_in") or md.get("in") or md.get("input_tokens") or 0)
    tout = int(md.get("tokens_out") or md.get("out") or md.get("output_tokens") or 0)
    cost = float(md.get("cost_usd") or md.get("cost") or 0.0)
    receipts = md.get("receipts") or []
    if isinstance(receipts, list):
        for r in receipts:
            if not isinstance(r, dict):
                continue
            tin += int(r.get("in") or r.get("tokens_in") or r.get("input_tokens") or 0)
            tout += int(r.get("out") or r.get("tokens_out") or r.get("output_tokens") or 0)
            cost += float(r.get("cost_usd") or r.get("cost") or 0.0)
    return tin, tout, cost


def _latest_block_reason_is_needs_input(conn, task_id: str) -> bool:
    for ev in reversed(kb.list_events(conn, task_id)):
        if ev.kind == "blocked":
            payload = ev.payload or {}
            return str(payload.get("kind") or "").lower() == "needs_input"
    return False


def build_ledger(
    conn=None, *, now: Optional[int] = None,
    window_days: int = LEDGER_WINDOW_DAYS,
) -> dict[str, Any]:
    """Compute the ledger from the current board. Returns
    ``{"window_days": N, "generated_at": ts, "rows": [cell, ...],
       "totals": {"attempts": ..., "runs_considered": ...}}``.

    Caller may pass an already-open connection (tests); otherwise the
    module opens the active board itself.
    """
    def _run(_conn):
        now_ts = now if now is not None else int(time.time())
        cutoff = now_ts - window_days * 86400
        cells: dict[tuple[str, str, str], LedgerCell] = {}
        runs_considered = 0

        tasks = kb.list_tasks(_conn, include_archived=False)
        task_by_id = {t.id: t for t in tasks}
        for task in tasks:
            kind = extract_kind(task.body)
            runs = kb.list_runs(_conn, task.id, include_active=True)
            for run in runs:
                # Roll-off by start time; a run outside the window is skipped.
                if run.started_at < cutoff:
                    continue
                prov, model = _served_provider_model(run, task)
                if not prov or not model:
                    continue
                key = (kind, prov, model)
                cell = cells.setdefault(key, LedgerCell(kind=kind, provider=prov, model=model))
                cell.attempts += 1
                runs_considered += 1
                if run.outcome == "completed":
                    cell.completed += 1
                if run.ended_at:
                    cell.durations_s.append(int(run.ended_at - run.started_at))
                if run.metadata:
                    tin, tout, cost = _harvest_receipt(run.metadata)
                    cell.tokens_in += tin
                    cell.tokens_out += tout
                    if cost > 0:
                        cell.cost_usd += cost
                        cell.cost_samples += 1
            # Task-level accounting: needs-input blocks and reviewer verdicts.
            if task.status == "blocked" and _latest_block_reason_is_needs_input(_conn, task.id):
                # Credit the LAST run's cell (if any) with the needs-input strike.
                if runs:
                    r = runs[-1]
                    prov, model = _served_provider_model(r, task)
                    if prov and model:
                        cells.setdefault((kind, prov, model),
                                         LedgerCell(kind=kind, provider=prov, model=model)
                                         ).blocked_needs_input += 1
            # Reviewer-pass: this task's reviewer CHILDREN feed back into the
            # PARENT's cell (the parent is the card whose work was reviewed).
            child_ids = kb.child_ids(_conn, task.id)
            for cid in child_ids:
                child = task_by_id.get(cid)
                if child is None or extract_kind(child.body) != "review":
                    continue
                verdict = _reviewer_verdict(_conn, cid)
                if verdict is None:
                    continue
                # Attribute to the most recent completed run of the parent.
                last_run = None
                for r in reversed(runs):
                    if r.outcome == "completed":
                        last_run = r
                        break
                if last_run is None:
                    continue
                prov, model = _served_provider_model(last_run, task)
                if not prov or not model:
                    continue
                cell = cells.setdefault((kind, prov, model),
                                        LedgerCell(kind=kind, provider=prov, model=model))
                if verdict == "pass":
                    cell.review_pass += 1
                else:
                    cell.review_fail += 1
        rows = sorted(
            (c.as_row() for c in cells.values()),
            key=lambda r: (r["kind"], -(r["attempts"] or 0), r["provider"], r["model"]),
        )
        return {
            "window_days": window_days,
            "generated_at": now_ts,
            "rows": rows,
            "totals": {"attempts": sum(c.attempts for c in cells.values()),
                       "runs_considered": runs_considered,
                       "tasks_considered": len(tasks)},
        }

    if conn is not None:
        return _run(conn)
    with kbc.connect_closing() as _c:
        return _run(_c)


# --- Chooser -----------------------------------------------------------------

# Static routing when we have no signal (matches t_5eb9182a's role table).
_STATIC_ROLES: dict[str, tuple[str, str]] = {
    # 2026-09-24: workers run on API, never Harry's Claude subscription
    # (Jarvis roadmap rule 20). GLM-5.1 is the live default on both machines.
    "build": ("zai", "glm-5.1"),
    "review": ("zai", "glm-5.1"),
    "research": ("zai", "glm-5.1"),
    "draft": ("zai", "glm-5.1"),
    "ops": ("zai", "glm-5.1"),
    "unknown": ("zai", "glm-5.1"),
}


@dataclass
class ChooseResult:
    provider: str
    model: str
    reason: str            # "ledger" | "probe" | "static-no-data" | "static-ops-probe-refused"
    probe: bool = False
    runner_up: Optional[tuple[str, str]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "probe": self.probe,
            "runner_up": list(self.runner_up) if self.runner_up else None,
        }


def _rank_for_kind(ledger: dict[str, Any], kind: str, *, min_attempts: int = 3):
    """Return ``[(cell_row, ...), ...]`` for this kind, ordered by review pass
    rate desc then cheaper cost-per-attempt asc (ties). Only cells with at
    least ``min_attempts`` attempts are considered."""
    contenders = [
        r for r in ledger["rows"]
        if r["kind"] == kind and r["attempts"] >= min_attempts and r["review_pass_rate"] is not None
    ]

    def key(r):
        cpa = r["cost_per_attempt"] if r["cost_per_attempt"] is not None else float("inf")
        return (-r["review_pass_rate"], cpa, r["provider"], r["model"])
    return sorted(contenders, key=key)


def choose_agent(
    kind: str,
    *,
    ledger: Optional[dict[str, Any]] = None,
    cards_of_kind_created_so_far: int = 0,
    probe_every_n: int = DEFAULT_PROBE_EVERY_N,
    conn=None,
) -> ChooseResult:
    """Pick (provider, model) for a card of this ``kind``.

    - The M.4 defaults file (``load_defaults``) is consulted FIRST: when it
      has an entry for the kind, that (provider, model) wins outright and
      the reason cites the promotion evidence numbers (``3-1 vs ...``).
    - Best review-pass rate at >= 3 attempts wins; ties -> cheaper.
    - Every Nth card of a kind (``cards_of_kind_created_so_far % N == N-1``,
      i.e. the 5th when N=5) runs on the runner-up as a ``probe``.
    - Ops work is never probed.
    - Zero signal -> static role routing.
    """
    kind = kind if kind in _KINDS else "unknown"

    # M.4 defaults file wins first (absent/corrupt -> today's behaviour).
    entry = load_defaults().get(kind)
    if isinstance(entry, dict) and entry.get("provider") and entry.get("model"):
        ev = entry.get("evidence") or {}
        wins = ev.get("wins", 0)
        losses = ev.get("losses", 0)
        challenger = ev.get("challenger") or ev.get("incumbent") or "seed"
        reason = f"defaults-file {wins}-{losses} vs {challenger}"
        return ChooseResult(entry["provider"], entry["model"], reason=reason)

    if ledger is None:
        ledger = build_ledger(conn=conn)
    ranked = _rank_for_kind(ledger, kind)
    if not ranked:
        prov, model = _STATIC_ROLES[kind]
        return ChooseResult(prov, model, reason="static-no-data")
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    trip_probe = (
        kind != "ops"
        and runner_up is not None
        and probe_every_n > 0
        and cards_of_kind_created_so_far > 0
        and (cards_of_kind_created_so_far % probe_every_n == 0)
    )
    if trip_probe and runner_up:
        return ChooseResult(
            runner_up["provider"], runner_up["model"], reason="probe", probe=True,
            runner_up=(best["provider"], best["model"]),
        )
    return ChooseResult(
        best["provider"], best["model"], reason="ledger",
        runner_up=(runner_up["provider"], runner_up["model"]) if runner_up else None,
    )


# --- Rendering ---------------------------------------------------------------

def render_ledger_table(ledger: dict[str, Any]) -> str:
    rows = ledger["rows"]
    if not rows:
        return (
            f"(no ledger data in the last {ledger['window_days']} days; "
            "chooser falls back to static role routing)"
        )
    cols = ("kind", "provider", "model", "attempts", "completed", "review_pass",
            "review_fail", "review_pass_rate", "blocked_needs_input",
            "median_duration_s", "tokens_in", "tokens_out", "cost_usd")
    headers = {c: c for c in cols}

    def fmt(v):
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)
    widths = {c: max(len(headers[c]), *(len(fmt(r.get(c))) for r in rows)) for c in cols}
    line = "  ".join(headers[c].ljust(widths[c]) for c in cols)
    sep = "-" * len(line)
    out = [
        f"Agent performance ledger  (window: {ledger['window_days']}d, "
        f"generated_at={ledger['generated_at']}, tasks={ledger['totals']['tasks_considered']}, "
        f"runs={ledger['totals']['runs_considered']})",
        "",
        line,
        sep,
    ]
    for r in rows:
        out.append("  ".join(fmt(r.get(c)).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def cmd_ledger(args) -> int:
    """``hermes kanban ledger`` handler."""
    with kbc.connect_closing() as conn:
        data = build_ledger(conn, window_days=int(getattr(args, "window_days", None)
                                                  or LEDGER_WINDOW_DAYS))
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=True))
    else:
        print(render_ledger_table(data))
    return 0


__all__ = [
    "LEDGER_WINDOW_DAYS", "DEFAULT_PROBE_EVERY_N",
    "extract_kind", "build_ledger", "render_ledger_table",
    "choose_agent", "ChooseResult", "cmd_ledger",
    "load_defaults",
]
