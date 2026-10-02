"""Authenticated, fixed, tool-free reproduction of native Discord event handling.

The actor is an operator, never Harry. Replies stay in a request-local capture.
This is controlled handler evidence, not a Discord network or human receipt.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import re
import time

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

ACTOR = "operator-verification"
_REPRODUCTION_SECONDS = 120
_scope: ContextVar["Scope | None"] = ContextVar("operator_discord_capture", default=None)


def serving_home() -> Path:
    """Bind the capability to the sealed serving owner, outside HTTP profile scope."""
    root = Path(__file__).resolve().parents[1]
    home = root.parent.parent
    if not root.is_relative_to(home / "releases"):
        raise ValueError("Operator verification requires a managed serving release")
    return home


@dataclass
class Scope:
    chat_id: str
    adapter: "CaptureAdapter"
    toolset_resolutions: list = field(default_factory=list)


def capture_for(source):
    scope = _scope.get()
    if (scope is not None and source is not None and source.platform == Platform.DISCORD
            and source.user_id == ACTOR and source.chat_id == scope.chat_id
            and source.is_bot is False):
        return scope.adapter
    return None


def tool_free_capture(source):
    adapter = capture_for(source)
    if adapter is None:
        return False
    _scope.get().toolset_resolutions.append([])
    return True


def authorized(token: str, home: Path, *, now=None) -> bool:
    """A distinct configured operator capability; no body-supplied identity."""
    from dotenv import dotenv_values
    import yaml
    try:
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        peer = config["a2a"]["peer_identities"][ACTOR]
        expiry = config["a2a"].get("interactive_peer_expires_at", {}).get(ACTOR, float("inf"))
        values = dotenv_values(home / ".env")
        peers = dict(item.split(":", 1) for item in values["A2A_PEER_TOKENS"].split(",") if ":" in item)
        expected = peers[ACTOR]
        return bool(token and expected and peer["user"] == ACTOR
                    and not hmac.compare_digest(expected, peers["jarvis-interactive"])
                    and (time.time() if now is None else now) < float(expiry)
                    and hmac.compare_digest(token, expected))
    except (KeyError, TypeError, ValueError, OSError):
        return False


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self, config, chat_id):
        super().__init__(config, Platform.DISCORD)
        self.chat_id = chat_id
        self.outputs = {}
        self.order = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        await self.cancel_background_tasks()

    def _check_chat(self, chat_id):
        if str(chat_id) != self.chat_id:
            raise ValueError("operator capture cannot address another conversation")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._check_chat(chat_id)
        message_id = "operator-capture-" + str(len(self.order))
        self.outputs[message_id] = str(content)
        self.order.append(message_id)
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self._check_chat(chat_id)
        if message_id not in self.outputs:
            return SendResult(success=False, error="unknown captured message")
        self.outputs[message_id] = str(content)
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        self._check_chat(chat_id)

    async def get_chat_info(self, chat_id):
        self._check_chat(chat_id)
        return {"name": "Controlled operator verification", "type": "dm"}

    def toolsets_for_source(self, source):
        return []


def prompts(nonce):
    reference = "operator-control-" + nonce[:12]
    return reference, [
        "Controlled operator verification, not a Harry request. No tools, tasks, saved memory, modifications or messages to others. Our temporary reference is " + reference + ". Reply briefly with that reference.",
        "For this hypothetical example only, a task has started but has no independently checked result. What remains unproved? One sentence; no tools or actions.",
        "Hypothetically a worker stopped before producing its result. Who should repair an agent-owned failure? One sentence; no tools or actions.",
        "Hypothetically code was committed but its live behavior was not reproduced. Is the requested outcome verified? One sentence; no tools or actions.",
        "Hypothetically a result was delivered in the original thread. Has that alone proved the person received or accepted it? One sentence; no tools or actions.",
        "In this sixth turn, state our temporary reference from turn one and one unresolved verification issue from our hypothetical examples. No tools, tasks or actions.",
    ]


async def reproduce(runner, live_adapter, nonce, home: Path):
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("nonce must be 32 lowercase hexadecimal characters")
    chat_id = "operator-verification-discord-" + nonce
    adapter = CaptureAdapter(live_adapter.config, chat_id)
    adapter.set_message_handler(runner._handle_message)
    scope = Scope(chat_id, adapter)
    token = _scope.set(scope)
    reference, requests = prompts(nonce)
    records = []
    started = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    deadline = time.monotonic() + _REPRODUCTION_SECONDS
    try:
        for index, text in enumerate(requests):
            source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="dm",
                user_id=ACTOR, user_name=ACTOR, is_bot=False, role_authorized=True,
                message_id="operator-verification-" + nonce + "-" + str(index))
            event = MessageEvent(text=text, source=source, user_id=ACTOR,
                user_name=ACTOR, message_id=source.message_id)
            before = len(adapter.order)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("native Discord handler verification deadline exceeded")
            async with asyncio.timeout(remaining):
                await adapter.handle_message(event)
                tasks = tuple(adapter._background_tasks)
                if tasks:
                    await asyncio.gather(*tasks)
            if len(adapter.order) <= before:
                raise ValueError("native Discord handler produced no captured response")
            response = adapter.outputs[adapter.order[-1]]
            records.append({"transport": "discord-front-door", "actor": ACTOR,
                "controlled_reproduction": True, "network_delivery": False,
                "message_id": source.message_id, "chat_id": chat_id,
                "request": text, "response": response})
        ended = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
        failures = []
        if reference not in records[-1]["response"]:
            failures.append("Native Discord conversation lost its six-turn context")
        if len(scope.toolset_resolutions) < 6:
            failures.append("Native turns did not prove the explicit empty tool policy")
        if (started.get("pid"), started.get("code_sha")) != (ended.get("pid"), ended.get("code_sha")):
            failures.append("Serving process changed during the native handler reproduction")
        if ended.get("pid") != os.getpid():
            failures.append("Handler is not the process claimed by the serving receipt")
        return {"passed": not failures, "actor": ACTOR, "requests": records,
                "failures": failures, "pid": ended.get("pid"), "revision": ended.get("code_sha"),
                "nonce": nonce, "toolsets": [], "human_receipt": False,
                "proof_scope": "controlled-native-handler; actual Discord network evidence is separate"}
    finally:
        try:
            await asyncio.wait_for(adapter.cancel_background_tasks(), timeout=6)
        finally:
            _scope.reset(token)
