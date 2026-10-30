"""Tests for POST /api/transcribe (Day 3B voice entry).

The Meta Model API call is always mocked: tests replace the httpx module
reference inside services.transcribe, so no network or API key is needed.

The route function is called directly with a real starlette UploadFile
(the same class FastAPI injects for a multipart upload). The sandbox
forbids sockets, so no asyncio/anyio loop can start here: everything the
route awaits (in-memory UploadFile.read) completes synchronously, hence
the small send()-pump driver below instead of asyncio.run.
"""
import io
import json
import wave
from tempfile import SpooledTemporaryFile

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import app as appmod
from services import transcribe as transcribemod

META_URL = "https://api.meta.ai/v1/asr/transcribe"


def make_wav_bytes(seconds, rate=16000):
    """Mono 16-bit PCM WAV silence, built in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def make_upload(data, filename="recording.wav"):
    """Real UploadFile backed by memory (never rolls to disk)."""
    spool = SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    spool.write(data)
    spool.seek(0)
    return UploadFile(file=spool, filename=filename)


def run_route(coro):
    """Drive the route coroutine to completion without an event loop."""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    coro.close()
    raise AssertionError("route suspended on real async I/O")


class FakeMetaResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def install_meta_fake(monkeypatch, response=None, error=None):
    """Replace the Meta HTTP call and capture what the backend sent."""
    seen = {}

    def fake_post(url, headers=None, files=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["files"] = files
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return response

    fake_http = type(
        "FakeHttpx", (), {"post": staticmethod(fake_post), "HTTPError": httpx.HTTPError}
    )
    monkeypatch.setattr(transcribemod, "httpx", fake_http)
    return seen


def test_transcribe_route_registered():
    methods = {
        route.path: sorted(route.methods)
        for route in appmod.app.routes
        if hasattr(route, "methods")
    }
    assert methods.get("/api/transcribe") == ["POST"]


def test_transcribe_returns_text(monkeypatch):
    seen = install_meta_fake(
        monkeypatch,
        FakeMetaResponse(200, {"transcript": "sold 5kg sugar for 800 bob", "turns": []}),
    )

    result = run_route(
        appmod.transcribe_entry(make_upload(make_wav_bytes(1)))
    )

    assert result == {"text": "sold 5kg sugar for 800 bob"}
    assert seen["url"] == META_URL
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    assert len(seen["headers"]["Authorization"]) > len("Bearer ")
    assert seen["headers"]["Accept"] == "application/json"
    request_part = json.loads(seen["files"]["request"][1])
    assert request_part["model"] == "muse-voice-transcribe-1.0"
    assert request_part["audioEncoding"] == "WAV"
    assert request_part["mode"] == "PUSH_TO_TALK"
    assert request_part["languageBias"] == ["English"]
    assert seen["files"]["audio"][2] == "audio/wav"


def test_transcribe_too_large_is_413(monkeypatch):
    install_meta_fake(monkeypatch, FakeMetaResponse(200, {"transcript": "x"}))

    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(b"\x00" * (2 * 1024 * 1024 + 1))))

    assert exc_info.value.status_code == 413


def test_transcribe_longer_than_60s_is_413(monkeypatch):
    install_meta_fake(monkeypatch, FakeMetaResponse(200, {"transcript": "x"}))

    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(61))))

    assert exc_info.value.status_code == 413


def test_transcribe_meta_error_is_502(monkeypatch):
    install_meta_fake(monkeypatch, FakeMetaResponse(400, {"detail": "bad wav"}))

    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1))))

    assert exc_info.value.status_code == 502


def test_transcribe_meta_unreachable_is_502(monkeypatch):
    install_meta_fake(monkeypatch, error=httpx.ConnectError("no network"))

    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1))))

    assert exc_info.value.status_code == 502


def test_transcribe_missing_key_is_502(monkeypatch):
    install_meta_fake(monkeypatch, FakeMetaResponse(200, {"transcript": "x"}))
    monkeypatch.setenv("MODEL_API_KEY", "")

    with pytest.raises(HTTPException) as exc_info:
        run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1))))

    assert exc_info.value.status_code == 502


def test_transcribe_keeps_audio_in_memory(monkeypatch, tmp_path):
    install_meta_fake(
        monkeypatch, FakeMetaResponse(200, {"transcript": "sugar 800"})
    )
    monkeypatch.chdir(tmp_path)

    result = run_route(appmod.transcribe_entry(make_upload(make_wav_bytes(1))))

    assert result == {"text": "sugar 800"}
    assert list(tmp_path.iterdir()) == []
