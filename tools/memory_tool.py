#!/usr/bin/env python3
"""Memory Tool - persistent curated memory (MEMORY.md = agent notes, USER.md = user
profile). Both enter the system prompt as a FROZEN snapshot at session start;
mid-session writes hit disk but never change the prompt (prefix cache intact).
Single `memory` tool: add/replace/remove or a batch `operations` list."""

import copy
import json
import logging
import re
import time
from contextvars import ContextVar
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import is_truthy_value
from tools.registry import no_cache_check_fn

# fcntl is Unix-only; Windows uses msvcrt. MemoryStore reads both lazily from
# this module (tests patch ``memory_tool.fcntl``).
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# One tool-definition pass must use ONE config decision for availability and the
# dynamic target schema: the check_fn result flows to the immediately following
# dynamic_schema_overrides call; ContextVar isolates concurrent profile builds.
_memory_surface_flags: ContextVar[Optional[Tuple[bool, bool]]] = ContextVar("memory_surface_flags", default=None)


def get_memory_dir() -> Path:
    """Profile-scoped memories dir, resolved per call (HERMES_HOME may switch after import)."""
    return get_hermes_home() / "memories"


from tools.memory_tool_store import (  # noqa: E402,F401  (re-exports)
    ENTRY_DELIMITER, MEMORY_BLOCK_HEADERS, MemoryStore, _scan_memory_content)


def load_on_disk_store() -> "MemoryStore":
    """Fresh on-disk MemoryStore with configured limits/flags for contexts with no live
    agent (gateway, Desktop, ``/memory``) so approvals enforce the SAME caps as
    ``agent_init``. Falls back to defaults if config can't load; never raises."""
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        mem_cfg = get_builtin_memory_config(config)
        memory_enabled, user_profile_enabled = get_builtin_memory_store_flags(config)
        store = MemoryStore(int(mem_cfg.get("memory_char_limit", 2200)), int(mem_cfg.get("user_char_limit", 1375)),
                            memory_enabled=memory_enabled, user_profile_enabled=user_profile_enabled)
    except Exception:
        store = MemoryStore()  # config optional - fall back to defaults rather than break /memory
    store.load_from_disk()
    return store


def _pin_matched_entries(store: "MemoryStore", payload: Dict[str, Any]) -> Optional[str]:
    """Record on each staged replace/remove the FULL entry its old_text selects now. Approval
    then applies to exactly the entry the approver reviewed and refuses if it changed:
    re-running the old_text search at approve time could hit a newer entry that still
    contains it. Returns the JSON error when the search fails now, as the direct write would."""
    target = payload.get("target", "memory")
    if payload.get("action") == "batch":
        result = store.resolve_batch_entries(target, payload["operations"])
        if result.get("success"):
            payload["operations"] = [op if entry is None else {**op, "matched_entry": entry}
                                     for op, entry in zip(payload["operations"], result["matched_entries"])]
    elif payload.get("action") in _BG_DELETE_ACTIONS:
        result = store.resolve_entry(target, payload.get("old_text") or "", payload["action"])
        if result.get("success"):
            payload["matched_entry"] = result["matched_entry"]
    else:
        return None
    return None if result.get("success") else json.dumps(result, ensure_ascii=False)


def _gate_or_stage(store: "MemoryStore", summary: str, detail: str, payload: Dict[str, Any]) -> Optional[str]:
    """JSON tool-result string when the write must NOT proceed (blocked or staged
    for approval), None to proceed. Fails open if the gate module can't load."""
    try:
        from tools import write_approval as wa
    except Exception:
        return None
    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)
    if (unmatched := _pin_matched_entries(store, payload)) is not None:
        return unmatched
    record = wa.stage_write(wa.MEMORY, payload, summary=f"{summary}: {detail[:120]}", origin=wa.current_origin())
    return json.dumps({"success": True, "staged": True, "pending_id": record["id"], "message": decision.message},
                      ensure_ascii=False)


# action -> (store call, gate (summary, detail) text) for the live tool path and staged replay.
_STORE_ACTIONS = {
    "add": (lambda store, target, content, old_text, entry=None: store.add(target, content),
            lambda label, content, old_text: (f"add to {label}", content or "")),
    "replace": (lambda store, target, content, old_text, entry=None: store.replace(target, old_text, content, entry),
                lambda label, content, old_text: (f"replace in {label}",
                                                  f"entry matching: {old_text}\nwhole entry becomes: {content}")),
    "remove": (lambda store, target, content, old_text, entry=None: store.remove(target, old_text, entry),
               lambda label, content, old_text: (f"remove from {label}", old_text or ""))}


def _batch_op_line(op: Dict[str, Any]) -> str:
    op = op or {}
    act, content, old = op.get("action", "?"), op.get("content") or op.get("new_text") or "", op.get("old_text", "")
    if act == "remove":
        return f"- remove: {old}"
    # Whole-entry contract (#117952): the approver must not read this as a span patch.
    return (f"- replace entry matching '{old}' -> whole entry becomes: {content}" if act == "replace"
            else f"- {act}: {content}")


def _apply_write_gate(store: "MemoryStore", action: str, target: str, content: Optional[str],
                      old_text: Optional[str], operations: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """Gate one mutating op, or (``operations`` set) a whole batch as a single unit."""
    label = "user profile" if target == "user" else "memory"
    if operations is not None:
        return _gate_or_stage(store, f"apply {len(operations)} op(s) to {label}",
                              "\n".join(_batch_op_line(op) for op in operations),
                              {"action": "batch", "target": target, "operations": operations})
    return _gate_or_stage(store, *_STORE_ACTIONS[action][1](label, content, old_text),
                          {"action": action, "target": target, "content": content, "old_text": old_text})


def _validate_single_op(store, action, target, content, old_text) -> Optional[str]:
    """Validate BEFORE the gate so an invalid write is rejected now, not at approve time.
    Missing ``old_text`` is recoverable (it can't be schema-required - needs a combinator
    the Codex backend rejects): return the inventory plus a retry instruction."""
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action in ("replace", "remove") and not old_text:
        replace_hint = (" For 'replace', content is the COMPLETE new entry -- the whole "
                        "matched entry is overwritten, not just the old_text span."
                        if action == "replace" else "")
        return json.dumps({
            "success": False,
            "error": (f"'{action}' needs old_text -- a short unique substring of the entry "
                      f"to {action}. None was provided. Reissue the {action} with old_text "
                      f"set to part of one of the current_entries below.{replace_hint}"),
            "current_entries": store._entries_for(target), "usage": store._usage(target)}, ensure_ascii=False)
    if action == "replace" and not content:
        return tool_error("content is required for 'replace' action.", success=False)
    return None


_BG_DELETE_ACTIONS = ("replace", "remove")


def destructive_ops(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The replace/remove ops of a staged memory payload, single-op or batch shape."""
    ops = (payload.get("operations") or []) if payload.get("action") == "batch" else [payload]
    return [op for op in ops if (op or {}).get("action") in _BG_DELETE_ACTIONS]


# --- Load-bearing guard for unattended background review (#105921 follow-up) ---
# Routine memory housekeeping (consolidation, deduplication, shortening, and removing clearly
# stale/superseded entries) applies automatically. An entry that encodes a locked decision,
# standing rule, credential/key reference, or any load-bearing fact the agent relies on every
# session is never auto-applied: it is DISCARDED (never staged, never asked) and written to the
# audit log, so nothing is silently lost without a record.

_LOAD_BEARING_PATTERNS = (
    # credentials, keys, secrets, tokens
    r"\b(api[ _-]?key|apikey|access[ _-]?key|client[ _-]?secret|secret|password|passwd|"
    r"credential|private[ _-]?key|bearer[ _-]?token|auth[ _-]?token|oauth[ _-]?token)\b",
    # stable IDs / tokens / endpoints the agent looks up
    r"\b(channel[ _-]?id|server[ _-]?id|discord[ _-]?id|webhook[ _-]?url|token)\b",
    # locked decisions / standing rules / source-of-truth / freeze markers
    r"\b(locked[ -]?decision|standing[ -]?rule|standing[ -]?decision|source[ -]of[ -]truth|"
    r"handoff|canon|do[ -]not[ -]change|never[ -]change|freeze|hard[ -]?freeze)\b",
    # imperative standing-rule verbs (case-insensitive)
    r"\b(never|always|mustn't|must not|must|required|forbidden|mandatory|rule)\b",
)

_LOAD_BEARING_RE = re.compile("|".join(_LOAD_BEARING_PATTERNS), re.IGNORECASE)


def _is_load_bearing_entry(entry: Optional[str]) -> bool:
    """Conservative guard: True when *entry* carries something the agent must not lose to
    unattended housekeeping (a credential/key, stable ID, locked decision, standing rule, or
    imperative rule). Over-matching only leaves an entry untouched - it can never delete a
    load-bearing entry. Deletion safety outranks how aggressively memory consolidates."""
    return bool(entry) and _LOAD_BEARING_RE.search(entry) is not None


def _audit_background_review(decision: str, op: Dict[str, Any], matched_entry: Optional[str],
                             target: str) -> None:
    """Best-effort append-only audit of an unattended background review's destructive memory
    ops: one JSON line per op recording whether the gate auto-applied it or discarded it, plus
    the full entry text, so nothing is silently lost without a record."""
    try:
        from hermes_constants import get_hermes_home, mkdir_under_hermes_home
        audit_dir = get_hermes_home() / "logs"
        mkdir_under_hermes_home(audit_dir)
        record = {
            "ts": time.time(), "decision": decision, "origin": "background_review",
            "action": (op or {}).get("action", ""), "target": target,
            "old_text": (op or {}).get("old_text", ""),
            "content": (op or {}).get("content") or (op or {}).get("new_text") or "",
            "entry": matched_entry or "",
        }
        with open(audit_dir / "memory_review_audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("Failed to write memory review audit record", exc_info=True)


def _held_result() -> str:
    """Benign (non-asking, non-surfacing) result for a load-bearing op the gate held."""
    return json.dumps({
        "success": True, "done": True, "held": True, "auto_discarded": True,
        "message": ("Held: this entry looks like a locked decision, standing rule, or "
                    "credential/key reference and was left unchanged."),
    }, ensure_ascii=False)


def _background_delete_gate(store, action, operations, target="memory", content=None,
                            old_text=None) -> Optional[str]:
    """Unattended background-review operation gate (#105921): routine memory housekeeping
    applies automatically and load-bearing entries are never touched. ``add`` stays available
    (it is all any review prompt asks for); a ``replace``/``remove`` - single or inside a batch -
    auto-applies when its matched entry is NOT load-bearing, and is discarded (audited, never
    staged, never asked) when it is. The old behaviour staged every destructive op for approval,
    surfacing a "staged for your approval" prompt the user must never see again."""
    from tools.skill_provenance import is_unattended_review

    if not is_unattended_review():
        return None
    payload = ({"action": "batch", "target": target, "operations": operations}
               if operations is not None else
               {"action": action, "target": target, "content": content, "old_text": old_text})
    if not destructive_ops(payload):
        return None
    try:
        if (unmatched := _pin_matched_entries(store, payload)) is not None:
            return unmatched
    except Exception:
        logger.warning("Failed to pin entries for the background-review memory gate; holding",
                       exc_info=True)
        return _held_result()

    if operations is not None:
        # Batch: drop load-bearing ops (audited as discarded), auto-apply the rest. The pinned
        # ops align 1:1 with ``operations``, so the filtered list is rebuilt from the caller's
        # list in place - the downstream apply then sees only the safe ops (+ any adds).
        pinned = payload["operations"]
        kept: List[Dict[str, Any]] = []
        for op, pinned_op in zip(operations, pinned):
            matched = (pinned_op or {}).get("matched_entry")
            if (op or {}).get("action") in _BG_DELETE_ACTIONS and _is_load_bearing_entry(matched):
                _audit_background_review("discarded", op, matched, target)
                continue
            kept.append(op)
            if (op or {}).get("action") in _BG_DELETE_ACTIONS:
                _audit_background_review("auto_applied", op, matched, target)
        if not kept:
            return _held_result()
        operations[:] = kept
        return None

    # Single destructive op.
    matched = payload.get("matched_entry")
    if _is_load_bearing_entry(matched):
        _audit_background_review("discarded", payload, matched, target)
        return _held_result()
    _audit_background_review("auto_applied", payload, matched, target)
    return None


def memory_tool(action: str = None, target: str = "memory", content: str = None, old_text: str = None,
                new_text: str = None, operations: Optional[List[Dict[str, Any]]] = None,
                store: Optional[MemoryStore] = None) -> str:
    """Tool entry point; returns a JSON string. Single op (action + content/old_text)
    or batch (``operations``, atomic against the final budget). ``new_text``
    aliases ``content`` -- for 'replace' both mean the COMPLETE new entry (the
    whole matched entry is overwritten; old_text only locates it)."""
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)
    outcome, result = _memory_tool(action, target, content, old_text, new_text, operations, store)
    from hermes_cli.observability.shared_metrics_loop import record_builtin_memory_call
    record_builtin_memory_call(action, operations, outcome=outcome)
    return result


def _applied(result: Dict[str, Any]) -> Tuple[str, str]:
    return ("success" if result.get("success") else "failed"), json.dumps(result, ensure_ascii=False)


def _memory_tool(action, target, content, old_text, new_text, operations, store) -> Tuple[str, str]:
    """``(outcome, result_json)``: ``rejected`` when refused or held before touching the store."""
    # An omitted optional string can arrive as "" (#90468): let the new_text alias fill it.
    if not content and new_text:
        content = new_text
    # Strict providers send JSON null for optional fields; treat as omitted.
    target = "memory" if target is None else target
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return "rejected", json.dumps(target_error)
    if operations:
        if not isinstance(operations, list):
            return "rejected", tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        denied = _background_delete_gate(store, action, operations, target)
        if denied is not None:
            return "rejected", denied
        # Approval gate: stages (background/gateway) or prompts inline (CLI); off by default.
        gate_result = _apply_write_gate(store, "batch", target, None, None, operations)
        if gate_result is not None:
            return "rejected", gate_result
        return _applied(store.apply_batch(target, operations))
    # Reject calls that provide neither action (single-op) nor operations
    # (batch).  Without this guard the dispatch falls through to the generic
    # "Unknown action 'None'" error, which gives the model no signal about
    # *why* the call was malformed and can trigger repeated retries. (#64291)
    if not action and not operations:
        return "rejected", tool_error(
            "Missing required parameter: provide 'action' (add/replace/remove) "
            "or 'operations' (batch list). Got neither.",
            success=False,
        )
    if action not in _STORE_ACTIONS:
        return "rejected", tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)
    invalid = (_validate_single_op(store, action, target, content, old_text)
               or _background_delete_gate(store, action, None, target, content, old_text)
               or _apply_write_gate(store, action, target, content, old_text))
    if invalid is not None:
        return "rejected", invalid
    return _applied(_STORE_ACTIONS[action][0](store, target, content, old_text))


def get_builtin_memory_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalized ``memory`` config section ({} when missing/malformed → flags default to
    enabled). ``agent_init`` reads the same section so availability and store cannot diverge."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except Exception:
            logger.debug("Could not read memory config for availability", exc_info=True)
            return {}
    section = config.get("memory") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def get_builtin_memory_store_flags(config: Optional[Dict[str, Any]] = None) -> Tuple[bool, bool]:
    """Return ``(memory_enabled, user_profile_enabled)`` from resolved config."""
    section = get_builtin_memory_config(config)
    return tuple(is_truthy_value(section.get(k), default=True) for k in ("memory_enabled", "user_profile_enabled"))


@no_cache_check_fn
def check_memory_requirements() -> bool:
    """Snapshot store flags and report whether the built-in tool is available."""
    _memory_surface_flags.set(None)
    flags = get_builtin_memory_store_flags()
    _memory_surface_flags.set(flags)
    return flags[0] or flags[1]


def _memory_target_error(store: "MemoryStore", target: str) -> Optional[Dict[str, Any]]:
    """Return a shared validation error for an invalid or disabled target."""
    if target not in {"memory", "user"}:
        from tools.registry import _bound_error_text
        return {"success": False,
                "error": _bound_error_text(f"Invalid memory target '{target}'. Use 'memory' or 'user'.")}
    if store.target_enabled(target):
        return None
    label = "USER.md" if target == "user" else "MEMORY.md"
    return {"success": False, "error": f"Built-in {label} writes are disabled in memory config.", "target": target}


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """Replay a staged write against the store, bypassing the gate (/memory approve). A
    replace/remove applies to exactly its pinned ``matched_entry`` or is refused; a record
    staged before pinning has no verifiable target, so it is refused rather than replayed by
    old_text (which could hit a newer entry the approver never saw)."""
    action, target = payload.get("action"), payload.get("target", "memory")
    target_error = _memory_target_error(store, target)
    if target_error is not None:
        return target_error
    if any(not op.get("matched_entry") for op in destructive_ops(payload)):
        return {"success": False, "error": "This destructive pending write predates entry pinning and cannot be "
                                           "verified; nothing was applied. Reject it and recreate the change."}
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action not in _STORE_ACTIONS:
        return {"success": False, "error": f"Unknown staged action '{action}'."}
    return _STORE_ACTIONS[action][0](store, target, payload.get("content") or "", payload.get("old_text") or "",
                                     payload.get("matched_entry"))


MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result - so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change.\n\n"
        "WHEN: only for facts that apply to EVERY session regardless of task: who the user "
        "is, stable environment facts, standing conventions with no task home. Anything "
        "learned while doing a task (procedures, pitfalls, and the user's preferences and "
        "corrections for that kind of work) belongs in the task's skill via skill_manage, "
        "where it loads only when relevant; memory is injected into every turn and must "
        "stay small.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform (single-op shape). Omit when using 'operations'."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'. For 'replace' it is the COMPLETE new entry text: the whole matched entry is overwritten, so include everything you want to keep. Alias: 'new_text' is also accepted (same full-entry meaning)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring IDENTIFYING the existing entry to modify -- it locates the entry, it is not spliced out. Omit only for 'add'."
            },
            "new_text": {
                "type": "string",
                "description": "Alias for 'content' (single-op shape): the COMPLETE new entry for 'replace', not a patch of old_text. If both are set, 'content' wins."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace. For replace, the COMPLETE new entry (whole entry is overwritten). Alias: 'new_text'."},
                        "new_text": {"type": "string", "description": "Alias for 'content' in a batch op."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}


# Schema text when only one built-in store is enabled: (target description, TARGETS replacement).
_SINGLE_TARGET_TEXT = {
    ("memory",): ("The enabled built-in store: 'memory' for personal notes.",
                  "TARGET: only 'memory' is enabled for personal notes (environment, conventions, "
                  "tool quirks, lessons)."),
    ("user",): ("The enabled built-in store: 'user' for user profile.",
                "TARGET: only 'user' is enabled for user profile facts (name, role, preferences, style).")}


def _build_memory_schema_overrides() -> Dict[str, Any]:
    """Narrow the advertised target surface using the availability snapshot."""
    flags = _memory_surface_flags.get() or get_builtin_memory_store_flags()
    _memory_surface_flags.set(None)
    targets = [t for t, on in zip(("memory", "user"), flags) if on]
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target_schema, description = parameters["properties"]["target"], MEMORY_SCHEMA["description"]
    target_schema["enum"] = targets
    if narrowed := _SINGLE_TARGET_TEXT.get(tuple(targets)):
        target_schema["description"], replacement = narrowed
        description = description.replace(
            "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
            "notes (environment, conventions, tool quirks, lessons).", replacement)
    return {"description": description, "parameters": parameters}


from tools.registry import registry, tool_error  # noqa: E402  (registration at import time)

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""), target=args.get("target", "memory"), store=kw.get("store"),
        **{k: args.get(k) for k in ("content", "old_text", "new_text", "operations")}),
    check_fn=check_memory_requirements,
    emoji="🧠",
    dynamic_schema_overrides=_build_memory_schema_overrides)
