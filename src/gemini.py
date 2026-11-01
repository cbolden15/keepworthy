"""Audio transcription via the Gemini REST API (inline base64, no SDK)."""
import base64
import json
import urllib.request
from pathlib import Path

_MAX_BYTES = 19 * 1024 * 1024
_MIME = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".mp3": "audio/mpeg",
         ".webm": "audio/webm", ".ogg": "audio/ogg", ".wav": "audio/wav"}

class TranscribeError(Exception):
    pass

def transcribe_audio(path, api_key, model, opener=None) -> str:
    if not api_key:
        raise TranscribeError("GEMINI_API_KEY is empty")
    p = Path(path)
    data = p.read_bytes()
    if len(data) > _MAX_BYTES:
        raise TranscribeError(f"audio too large for inline upload: {len(data)} bytes")
    body = json.dumps({"contents": [{"parts": [
        {"inline_data": {"mime_type": _MIME.get(p.suffix.lower(), "audio/mp4"),
                         "data": base64.b64encode(data).decode()}},
        {"text": "Transcribe this audio verbatim. Output only the transcript text."},
    ]}]}).encode()
    # The API key travels in a header, never the URL: HTTPError carries the
    # request URL, and a URL-embedded key would leak into exception text
    # and any log derived from it.
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            out = json.loads(resp.read())
    except Exception as e:
        raise TranscribeError(f"gemini request failed: {e}") from e
    try:
        text = out["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise TranscribeError(f"unexpected gemini response shape: {e}") from e
    if not text:
        raise TranscribeError("empty transcript from gemini")
    return text
