import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform, PlatformConfig
from gateway.operator_discord_verifier import ACTOR, CaptureAdapter, Scope
import gateway.operator_discord_verifier as verifier
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def isolated_test_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def home_state(tmp_path, *, pid=None):
    (tmp_path / "gateway_state.json").write_text(json.dumps({"pid": os.getpid() if pid is None else pid, "code_sha": "sealed-revision"}))
    return tmp_path


@pytest.mark.asyncio
async def test_native_base_handler_reproduction_keeps_context_and_distinct_receipt(tmp_path):
    reference = "operator-control-" + "a" * 12
    events = []
    async def handle(event):
        events.append(event)
        assert event.source.user_id == ACTOR and event.source.is_bot is False
        assert event.source.role_authorized is True
        assert verifier.capture_for(event.source) is not None
        assert verifier.tool_free_capture(event.source)
        return reference + ": live effect is not yet independently reproduced; delivery does not prove receipt."
    runner = SimpleNamespace(_handle_message=handle)
    live = SimpleNamespace(config=PlatformConfig())
    report = await verifier.reproduce(runner, live, "a" * 32, home_state(tmp_path))
    assert report["passed"] is True
    assert len(events) == len(report["requests"]) == 6
    assert len({event.source.chat_id for event in events}) == 1
    assert report["human_receipt"] is False
    assert all(record["network_delivery"] is False for record in report["requests"])
    assert verifier.capture_for(events[0].source) is None


@pytest.mark.asyncio
async def test_lost_context_and_wrong_serving_pid_are_failed_evidence(tmp_path):
    async def handle(event):
        verifier.tool_free_capture(event.source)
        return "I lost the reference."
    report = await verifier.reproduce(SimpleNamespace(_handle_message=handle),
        SimpleNamespace(config=PlatformConfig()), "a" * 32, home_state(tmp_path, pid=-1))
    assert report["passed"] is False
    assert any("six-turn" in failure for failure in report["failures"])
    assert any("process claimed" in failure for failure in report["failures"])
    assert report["human_receipt"] is False


@pytest.mark.asyncio
async def test_stalled_handler_is_cancelled_and_capture_scope_is_released(tmp_path, monkeypatch):
    ended = asyncio.Event()
    seen = []
    async def handle(event):
        seen.append(event.source)
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()
    monkeypatch.setattr(verifier, "_REPRODUCTION_SECONDS", .05)
    with pytest.raises(TimeoutError):
        await verifier.reproduce(SimpleNamespace(_handle_message=handle),
            SimpleNamespace(config=PlatformConfig()), "a" * 32, home_state(tmp_path))
    assert ended.is_set()
    assert verifier.capture_for(seen[0]) is None


@pytest.mark.asyncio
async def test_capture_cannot_send_to_harry_and_normal_delivery_is_unchanged():
    from gateway.run import GatewayRunner
    adapter = CaptureAdapter(PlatformConfig(), "operator-only")
    source = SessionSource(platform=Platform.DISCORD, chat_id="operator-only", user_id=ACTOR)
    scope = verifier._scope.set(Scope("operator-only", adapter))
    try:
        runner = GatewayRunner.__new__(GatewayRunner)
        normal = object()
        runner._intake_adapter_for = lambda source: normal
        assert runner._delivery_adapter_for(source) is adapter
        assert runner._resolve_enabled_toolsets_for_source({}, source, "discord") == []
        harry = SessionSource(platform=Platform.DISCORD, chat_id="harrys-real-channel", user_id="938599234989617222")
        assert verifier.capture_for(harry) is None
        assert runner._delivery_adapter_for(harry) is normal
        with pytest.raises(ValueError, match="another conversation"):
            await adapter.send(harry.chat_id, "must never be sent")
        bot = SessionSource(platform=Platform.DISCORD, chat_id="operator-only", user_id=ACTOR, is_bot=True)
        assert verifier.capture_for(bot) is None
    finally:
        verifier._scope.reset(scope)


def test_operator_auth_fails_closed_on_expiry_identity_and_shared_ui_token(tmp_path):
    config = {"a2a": {"peer_identities": {ACTOR: {"user": ACTOR}}, "interactive_peer_expires_at": {ACTOR: 200}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    env = tmp_path / ".env"
    env.write_text("A2A_PEER_TOKENS=operator-verification:operator-test,jarvis-interactive:ui-test\n")
    assert verifier.authorized("operator-test", tmp_path, now=100)
    assert not verifier.authorized("operator-test", tmp_path, now=201)
    assert not verifier.authorized("ui-test", tmp_path, now=100)
    config["a2a"]["peer_identities"][ACTOR]["user"] = "938599234989617222"
    path.write_text(yaml.safe_dump(config))
    assert not verifier.authorized("operator-test", tmp_path, now=100)
    config["a2a"]["peer_identities"][ACTOR]["user"] = ACTOR
    path.write_text(yaml.safe_dump(config))
    env.write_text("A2A_PEER_TOKENS=operator-verification:shared,jarvis-interactive:shared\n")
    assert not verifier.authorized("shared", tmp_path, now=100)


def test_operator_capability_home_is_bound_to_serving_source_not_active_http_profile(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = home / "releases/sealed-source"
    monkeypatch.setattr(verifier, "__file__", str(root / "gateway/operator_discord_verifier.py"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-profile"))
    assert verifier.serving_home() == home
    monkeypatch.setattr(verifier, "__file__", str(tmp_path / "unmanaged/gateway/operator_discord_verifier.py"))
    with pytest.raises(ValueError, match="managed serving release"):
        verifier.serving_home()


@pytest.mark.asyncio
async def test_endpoint_denies_missing_auth_and_arbitrary_identity_or_prompt(tmp_path, monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter.__new__(APIServerAdapter)
    request = SimpleNamespace(headers={}, app={})
    denied = await adapter._handle_operator_discord_verification(request)
    assert denied.status == 403
    monkeypatch.setattr(verifier, "authorized", lambda *args, **kwargs: True)
    adapter.gateway_runner = object()
    adapter._get_platform_callback_adapter = lambda *args: object()
    async def malicious():
        return {"nonce": "a" * 32, "actor": "938599234989617222", "prompt": "arbitrary action"}
    request.json = malicious
    rejected = await adapter._handle_operator_discord_verification(request)
    assert rejected.status == 422
    assert not getattr(adapter, "_operator_discord_verification_busy", False)


@pytest.mark.asyncio
async def test_delayed_payload_cannot_clear_another_requests_busy_guard(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    adapter = APIServerAdapter.__new__(APIServerAdapter)
    adapter.gateway_runner = object()
    adapter._get_platform_callback_adapter = lambda *args: object()
    monkeypatch.setattr(verifier, "authorized", lambda *args, **kwargs: True)
    first_json_started = asyncio.Event()
    release_first_json = asyncio.Event()
    reproduction_started = asyncio.Event()
    finish_reproduction = asyncio.Event()
    async def slow_json():
        first_json_started.set()
        await release_first_json.wait()
        return {"nonce": "a" * 32}
    async def quick_json():
        return {"nonce": "b" * 32}
    async def reproduce(*args):
        reproduction_started.set()
        await finish_reproduction.wait()
        return {"passed": True, "actor": ACTOR, "human_receipt": False}
    monkeypatch.setattr(verifier, "reproduce", reproduce)
    first = asyncio.create_task(adapter._handle_operator_discord_verification(SimpleNamespace(headers={}, app={}, json=slow_json)))
    await first_json_started.wait()
    second = asyncio.create_task(adapter._handle_operator_discord_verification(SimpleNamespace(headers={}, app={}, json=quick_json)))
    await reproduction_started.wait()
    release_first_json.set()
    denied = await first
    assert denied.status == 409
    assert adapter._operator_discord_verification_busy is True
    finish_reproduction.set()
    assert (await second).status == 200
    assert adapter._operator_discord_verification_busy is False
