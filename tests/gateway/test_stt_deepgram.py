"""Deepgram STT provider tests - the Discord voice-join spine (Nova-3).

The dash voice loop transcribes via Deepgram Nova-3 (``voice_deepgram.py`` in the Jarvis
repo). The hermes-agent Discord ``/voice join`` path shares the same STT through the
``deepgram`` built-in provider added in ``tools/transcription_cloud.py``. These tests pin
the request shape (raw-body POST, ``Token`` auth, ``nova-3``) and transcript extraction so
a regression can't silently switch the voice join back to another STT.
"""

from __future__ import annotations

import requests

from tools.transcription_cloud import _transcribe_deepgram


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self):
        return self._body


def test_deepgram_is_builtin_and_defaults_to_nova3():
    from tools.transcription_tools import BUILTIN_STT_PROVIDERS, _BUILTIN_MODEL_KEYS

    assert "deepgram" in BUILTIN_STT_PROVIDERS
    section, key, default = _BUILTIN_MODEL_KEYS["deepgram"][:3]
    assert (section, key, default) == ("deepgram", "model", "nova-3")


def test_transcribe_deepgram_posts_raw_wav_and_extracts_transcript(tmp_path, monkeypatch):
    import tools.transcription_tools as tt

    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"RIFF-test-WAVE-bytes")

    fake = _FakeResponse(200, {
        "results": {"channels": [{"alternatives": [{"transcript": "hello jarvis", "confidence": 0.99}]}]},
    })
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        data = kwargs.get("data")
        captured["body"] = data.read() if hasattr(data, "read") else data
        return fake

    monkeypatch.setattr(tt, "_resolve_provider_key", lambda env, pid: "test-key")
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {})
    monkeypatch.setattr(tt, "_resolve_stt_language", lambda *a, **k: None)
    monkeypatch.setattr(requests, "post", fake_post)

    result = _transcribe_deepgram(str(wav), "nova-3")

    assert result["success"] is True
    assert result["transcript"] == "hello jarvis"
    assert result["provider"] == "deepgram"
    # Deepgram is a raw-body endpoint (not multipart), authed with a Token header.
    assert "model=nova-3" in captured["url"]
    assert "punctuate=true" in captured["url"]
    assert captured["headers"]["Authorization"] == "Token test-key"
    assert captured["headers"]["Content-Type"] == "audio/wav"
    assert captured["body"] == b"RIFF-test-WAVE-bytes"


def test_transcribe_deepgram_returns_error_on_non_200(tmp_path, monkeypatch):
    import tools.transcription_tools as tt

    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"RIFF-test-WAVE-bytes")

    fake = _FakeResponse(400, {"err_code": "bad", "err_msg": "invalid audio"})
    monkeypatch.setattr(tt, "_resolve_provider_key", lambda env, pid: "test-key")
    monkeypatch.setattr(tt, "_load_stt_config", lambda: {})
    monkeypatch.setattr(tt, "_resolve_stt_language", lambda *a, **k: None)
    monkeypatch.setattr(requests, "post", lambda *a, **k: fake)

    result = _transcribe_deepgram(str(wav), "nova-3")

    assert result["success"] is False
    assert "invalid audio" in result["error"]


def test_transcribe_deepgram_missing_key_fails_closed(tmp_path, monkeypatch):
    import tools.transcription_tools as tt

    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"RIFF-test-WAVE-bytes")
    monkeypatch.setattr(tt, "_resolve_provider_key", lambda env, pid: "")

    result = _transcribe_deepgram(str(wav), "nova-3")

    assert result["success"] is False
    assert "DEEPGRAM_API_KEY not set" in result["error"]
