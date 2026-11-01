import json
import urllib.error
import pytest
from src.gemini import transcribe_audio, TranscribeError


class FakeResponse:
    def __init__(self, payload): self._p = json.dumps(payload).encode()
    def read(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_opener(payload, captured=None, timeouts=None):
    class O:
        def open(self, req, timeout=None):
            if captured is not None:
                captured.append(req)
            if timeouts is not None:
                timeouts.append(timeout)
            return FakeResponse(payload)
    return O()


def test_transcribe_audio_happy(tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"\x00\x01")
    captured = []
    payload = {"candidates": [{"content": {"parts": [{"text": "hello world"}]}}]}
    out = transcribe_audio(audio, "key", "gemini-2.5-flash",
                           opener=_fake_opener(payload, captured))
    assert out == "hello world"
    req = captured[0]
    assert "gemini-2.5-flash:generateContent" in req.full_url
    body = json.loads(req.data)
    assert body["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "audio/mp4"


def test_transcribe_audio_requires_key(tmp_path):
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "", "m", opener=_fake_opener({}))


def test_transcribe_audio_rejects_oversize(tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x" * (20 * 1024 * 1024))
    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "k", "m", opener=_fake_opener({}))


def test_transcribe_audio_empty_response(tmp_path):
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "k", "m", opener=_fake_opener({"candidates": []}))


def test_transcribe_audio_missing_nested_keys(tmp_path):
    # well-formed JSON, but the shape the code expects isn't there -- must
    # take the TranscribeError path, not raise a bare KeyError/IndexError.
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "k", "m",
                          opener=_fake_opener({"candidates": [{"content": {}}]}))


def test_transcribe_audio_malformed_json(tmp_path):
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")

    class BadJSONResponse:
        def read(self): return b"not json"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class O:
        def open(self, req, timeout=None):
            return BadJSONResponse()

    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "k", "m", opener=O())


def test_transcribe_audio_http_error(tmp_path):
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")

    class O:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "server error", {}, None)

    with pytest.raises(TranscribeError):
        transcribe_audio(audio, "k", "m", opener=O())


def test_transcribe_audio_applies_timeout(tmp_path):
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    timeouts = []
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    transcribe_audio(audio, "k", "m", opener=_fake_opener(payload, timeouts=timeouts))
    assert timeouts[0] is not None
    assert timeouts[0] > 0


@pytest.mark.parametrize("ext,expected", [
    (".mp3", "audio/mpeg"),
    (".webm", "audio/webm"),
    (".ogg", "audio/ogg"),
    (".wav", "audio/wav"),
])
def test_transcribe_audio_mime_by_extension(tmp_path, ext, expected):
    audio = tmp_path / f"a{ext}"
    audio.write_bytes(b"x")
    captured = []
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    transcribe_audio(audio, "k", "m", opener=_fake_opener(payload, captured))
    body = json.loads(captured[0].data)
    assert body["contents"][0]["parts"][0]["inline_data"]["mime_type"] == expected


def test_transcribe_audio_key_not_in_url(tmp_path):
    # the API key must travel in a header, never in the request URL, so it
    # can't leak into an HTTPError's URL or into logs derived from the URL.
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    captured = []
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    transcribe_audio(audio, "super-secret-key", "m",
                      opener=_fake_opener(payload, captured))
    req = captured[0]
    assert "super-secret-key" not in req.full_url
