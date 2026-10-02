import json
import os
from types import SimpleNamespace
import pytest
import yaml

from hermes_cli import kanban_discord_probe as probe


@pytest.mark.parametrize("network_fails", [False, True])
def test_native_client_uses_configured_key_and_serving_home_not_worker_profile(tmp_path, monkeypatch, network_fails):
    home = tmp_path / "home"
    root = home / "releases/sealed-candidate"
    script = root / "hermes_cli/kanban_discord_probe.py"
    script.parent.mkdir(parents=True)
    monkeypatch.setattr(probe, "__file__", str(script))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unrelated-worker-profile"))
    (home / ".env").write_text("A2A_PEER_TOKENS=operator-verification:operator-test,jarvis-interactive:ui-test\nDISCORD_BOT_TOKEN=discord-test\n")
    (home / "config.yaml").write_text(yaml.safe_dump({"platforms": {"api_server": {"extra": {"key": "configured-server-test"}}}}))
    (home / "gateway_state.json").write_text(json.dumps({"pid": os.getpid(), "code_sha": "sealed-source"}))
    monkeypatch.setattr(probe.psutil, "Process", lambda pid: SimpleNamespace(pid=pid, cwd=lambda: str(root), create_time=lambda: 1))
    origin = {"author_id": "fixture-human", "channel_id": "fixture-channel", "message_id": "fixture-origin"}
    seen = []
    def read(url, headers, *, payload=None, timeout=12):
        seen.append((url, headers))
        if payload is not None:
            assert headers["Authorization"] == "Bearer configured-server-test"
            assert headers["X-Hermes-Operator-Token"] == "operator-test"
            reference = "operator-control-" + payload["nonce"][:12]
            return {"passed": True, "actor": "operator-verification", "nonce": payload["nonce"],
                "pid": os.getpid(), "revision": "sealed-source", "toolsets": [], "human_receipt": False,
                "requests": [{"transport": "discord-front-door", "actor": "operator-verification",
                    "controlled_reproduction": True, "network_delivery": False, "chat_id": "fixture-control",
                    "response": reference + ": live outcome not proved; no receipt yet"} for _ in range(6)]}
        assert headers["Authorization"] == "Bot discord-test"
        if network_fails:
            raise OSError("controlled network failure")
        if url.endswith("users/@me"):
            return {"id": "fixture-bot"}
        return {"author": {"id": "fixture-human", "bot": False}, "channel_id": "fixture-channel", "content": "fixture request"}
    monkeypatch.setattr(probe, "read_json", read)
    report = probe.run({"discord_probe_url": "http://127.0.0.1:8642/api/operator/verify-discord", "discord_origin": origin})
    if network_fails:
        assert report["passed"] is False
        assert len(report["requests"]) == 6
        assert "controlled network failure" in report["failures"][-1]
        assert "origin_network_readback" not in report
    else:
        assert report["passed"] is True
        assert report["origin_network_readback"]["author_is_bot"] is False
        assert report["independent_process_readback"]["root"] == str(root)
    assert report["human_receipt"] is False
    assert len(seen) == (2 if network_fails else 3)


def test_http_refusal_preserves_native_agent_failure_evidence(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    body = {"passed": False, "actor": "operator-verification", "requests": [],
            "failures": ["native handler deadline exceeded"], "human_receipt": False}
    def refused(*args, **kwargs):
        raise HTTPError("http://127.0.0.1:8642/api/operator/verify-discord", 422, "failed", {}, BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr(probe, "urlopen", refused)
    assert probe.read_json("http://127.0.0.1:8642/api/operator/verify-discord", {}, payload={"nonce": "a" * 32}) == body


def test_discord_readback_sends_explicit_bot_user_agent(monkeypatch):
    from io import BytesIO
    def opened(request, **kwargs):
        assert request.get_header("User-agent").startswith("DiscordBot (")
        assert request.get_header("Authorization") == "Bot fixture-test"
        return BytesIO(b'{"id":"fixture-bot"}')
    monkeypatch.setattr(probe, "urlopen", opened)
    assert probe.read_json("https://discord.com/api/v10/users/@me", {"Authorization": "Bot fixture-test"}) == {"id": "fixture-bot"}
