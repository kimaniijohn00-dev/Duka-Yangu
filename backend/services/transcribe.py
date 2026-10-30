"""Voice transcription via Muse Voice Transcribe (Meta Model API).

Doc sources (nothing here is guessed):
- Endpoints table (https://dev.meta.ai/docs/speech-to-text): the
  recorded-audio endpoint is POST https://api.meta.ai/v1/asr/transcribe,
  a multipart upload authenticated with the Authorization header.
- Multipart shape (https://dev.meta.ai/docs/api-reference/voice/transcribe):
  two parts, `request` (transcription settings as JSON) and `audio` (the
  clip); Accept: application/json returns the buffered TranscribeResponse.
- Schemas (https://dev.meta.ai/docs/api-reference/voice/schemas):
  audioEncoding WAV means a RIFF/WAVE container of mono integer PCM at
  16 or 24 kHz; the JSON response carries the final `transcript` string.

MODEL_API_KEY is read from the repo-root .env and only ever sent as the
Authorization header. It is never logged or returned. Audio is held in
memory only: never written to disk, the database, or logs.
"""
import io
import json
import os
import struct
import wave
from pathlib import Path

import httpx
from dotenv import load_dotenv

META_ASR_URL = "https://api.meta.ai/v1/asr/transcribe"
MODEL = "muse-voice-transcribe-1.0"
AUDIO_ENCODING = "WAV"
MODE = "PUSH_TO_TALK"
LANGUAGE_BIAS = ["English"]

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_DURATION_SECONDS = 60


def _get_api_key() -> str:
    root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(root / ".env")
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set. Add it to .env.")
    return api_key


def wav_duration_seconds(data: bytes) -> float | None:
    """Return the duration of WAV audio, or None if it is not readable WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            if not rate:
                return None
            return wav.getnframes() / rate
    except (wave.Error, EOFError, struct.error):
        return None


def transcribe_audio(audio_bytes: bytes, filename: str = "recording.wav") -> str:
    """Forward a WAV clip to Muse Voice Transcribe; return the transcript text."""
    api_key = _get_api_key()
    request_part = {
        "model": MODEL,
        "audioEncoding": AUDIO_ENCODING,
        "mode": MODE,
        "languageBias": LANGUAGE_BIAS,
    }
    try:
        response = httpx.post(
            META_ASR_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            files={
                "request": ("request", json.dumps(request_part), "application/json"),
                "audio": (filename, audio_bytes, "audio/wav"),
            },
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("Could not transcribe that recording. Try again.") from exc
    if response.status_code != 200:
        raise RuntimeError(
            "Could not transcribe that recording."
            f" (service said {response.status_code}). Try again."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Could not transcribe that recording. Try again."
        ) from exc
    text = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise RuntimeError("Could not transcribe that recording. Try again.")
    return text
