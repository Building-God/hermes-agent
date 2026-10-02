"""Independent live readback of a fixed native-handler reproduction and origin."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid

from dotenv import dotenv_values
import psutil


def read_json(url, headers, *, payload=None, timeout=12):
    request = Request(url, headers=headers, data=None if payload is None else json.dumps(payload).encode())
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        if payload is not None:
            body = json.load(error)
            if body.get("passed") is False and body.get("actor") == "operator-verification":
                return body
            return {"passed": False, "actor": "operator-verification", "requests": [],
                    "failures": ["Native verifier HTTP " + str(error.code) + ": " + str(body.get("error", {}).get("message", "request refused"))],
                    "human_receipt": False}
        raise


def run(config):
    import yaml
    root = Path(__file__).resolve().parents[1]
    home = root.parent.parent
    assert root.is_relative_to(home / "releases"), "Native verifier requires a sealed release runtime"
    values = dotenv_values(home / ".env")
    native_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    api_key = native_config.get("platforms", {}).get("api_server", {}).get("extra", {}).get("key") or values.get("API_SERVER_KEY")
    assert api_key, "Native API server credential is missing; agent-owned release repair required"
    peers = dict(item.split(":", 1) for item in values["A2A_PEER_TOKENS"].split(",") if ":" in item)
    url = config["discord_probe_url"]
    assert urlparse(url).hostname in ("localhost", "127.0.0.1", "::1"), "Verifier interface must be local"
    before = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    process = psutil.Process(before["pid"])
    assert Path(process.cwd()).resolve() == root, "Verifier must reproduce the serving sealed source"
    nonce = uuid.uuid4().hex
    report = read_json(url, {"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
                            "X-Hermes-Operator-Token": peers["operator-verification"]},
                       payload={"nonce": nonce}, timeout=150)
    if report.get("passed") is False:
        return report
    records = report.get("requests", [])
    reference = "operator-control-" + nonce[:12]
    assert report.get("actor") == "operator-verification" and report.get("nonce") == nonce
    assert len(records) == 6 and reference in records[-1]["response"], "Native six-turn context failed"
    assert all(record["actor"] == "operator-verification" and record["transport"] == "discord-front-door"
               and record["controlled_reproduction"] is True and record["network_delivery"] is False for record in records)
    assert len({record["chat_id"] for record in records}) == 1
    assert report.get("toolsets") == [] and report.get("human_receipt") is False
    assert any(word in records[3]["response"].lower() for word in ("live", "reproduc", "not", "no")), "Live-outcome reasoning failed"
    assert any(word in records[4]["response"].lower() for word in ("receipt", "accept", "not", "no")), "Delivery and receipt reasoning collapsed"
    origin = config["discord_origin"]
    token = values["DISCORD_BOT_TOKEN"]
    bot = read_json("https://discord.com/api/v10/users/@me", {"Authorization": "Bot " + token})
    message = read_json("https://discord.com/api/v10/channels/" + origin["channel_id"] + "/messages/" + origin["message_id"],
                        {"Authorization": "Bot " + token})
    assert message["author"]["id"] == origin["author_id"] and message["author"].get("bot", False) is False
    assert message["channel_id"] == origin["channel_id"]
    after = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    assert (before["pid"], before["code_sha"]) == (after["pid"], after["code_sha"])
    assert report["pid"] == before["pid"] and report["revision"] == before["code_sha"]
    report.update(origin_network_readback={**origin, "author_is_bot": False, "transport_bot_id": bot["id"],
                  "content_sha256": hashlib.sha256(message["content"].encode()).hexdigest(), "checked_at": time.time()},
                  independent_process_readback={"pid": process.pid, "root": str(root), "started_at": process.create_time()})
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({"passed": False, "actor": "operator-verification", "requests": [],
                          "failures": [type(error).__name__ + ": " + str(error)[:240]], "human_receipt": False}))
        raise SystemExit(1)
