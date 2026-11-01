import json
import socket
import threading
import urllib.request
import urllib.error
import pytest
from src import state
from src.config import load_config
from src.ingest_server import make_server
from src.ingest import rotate_queue

@pytest.fixture
def server(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(
        f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}\n"
        f"INGEST_TOKEN=sekrit\nINGEST_BIND=127.0.0.1\nINGEST_PORT=0\n"
        f"QUEUE_PATH={tmp_path}/queue.jsonl\n"
        f"STATE_PATH={tmp_path}/state.json\n"
    )
    cfg = load_config(env)
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, cfg
    srv.shutdown()
    srv.server_close()

def _post(port, path, body, token=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None

def _queue_is_empty(cfg) -> bool:
    p = cfg.queue_path
    return not p.exists() or p.read_text() == ""

def _raw_post(port, path, body: bytes, token=None, content_length=None,
              extra_header_lines=None, timeout=5):
    """Send a request over a bare socket so we can lie about Content-Length
    or ship non-ASCII header bytes — things urllib won't let us do."""
    if content_length is None:
        content_length = len(body)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        f"Content-Length: {content_length}",
        "Connection: close",
    ]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if extra_header_lines:
        lines.extend(extra_header_lines)
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(head + body)
        s.shutdown(socket.SHUT_WR)  # signal EOF on the body without waiting
        s.settimeout(timeout)
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
    return b"".join(chunks)

def _raw_status(raw: bytes):
    if not raw:
        return None  # connection closed with no HTTP response at all
    return int(raw.split(b"\r\n", 1)[0].split(b" ")[1])

def test_share_happy_path(server):
    srv, cfg = server
    status, body = _post(srv.server_address[1], "/share",
                         {"url": "https://instagram.com/p/abc/", "why": "Keep this."},
                         token="sekrit")
    assert status == 202 and body == {"queued": True, "pipeline": "social"}
    items = rotate_queue(cfg.queue_path)
    assert items[0]["url"] == "https://instagram.com/p/abc/"
    assert items[0]["why"] == "Keep this."
    assert items[0]["text"] is None
    assert "received_at" in items[0]

def test_share_preserves_explicit_why_and_optional_note(server):
    srv, cfg = server
    status, _ = _post(srv.server_address[1], "/share", {
        "url": "https://instagram.com/p/abc/",
        "why": "Adapt this offer framing.",
        "text": "Focus on the guarantee.",
    }, token="sekrit")
    assert status == 202
    item = rotate_queue(cfg.queue_path)[0]
    assert item["why"] == "Adapt this offer framing."
    assert item["text"] == "Focus on the guarantee."

def test_youtube_share_routes_to_playlist_pipeline_with_context(server):
    srv, cfg = server
    status, body = _post(srv.server_address[1], "/share", {
        "url": "https://youtu.be/abc123",
        "why": "Test this memory pattern in my Brain.",
    }, token="sekrit")
    assert status == 202
    assert body == {"queued": True, "pipeline": "youtube", "video_id": "abc123",
                    "duplicate": False}
    assert _queue_is_empty(cfg)
    data = state.load_state(cfg.state_path)
    assert data["requested_videos"] == ["abc123"]
    assert data["capture_context"]["abc123"]["why"] == "Test this memory pattern in my Brain."

def test_share_rejects_bad_token(server):
    srv, cfg = server
    assert _post(srv.server_address[1], "/share", {"url": "https://a"}, token="wrong")[0] == 401
    assert _post(srv.server_address[1], "/share", {"url": "https://a"})[0] == 401
    assert _queue_is_empty(cfg)

def test_share_rejects_bad_body_and_path(server):
    srv, cfg = server
    assert _post(srv.server_address[1], "/share", {"nope": 1}, token="sekrit")[0] == 400
    assert _post(srv.server_address[1], "/other", {"url": "https://a"}, token="sekrit")[0] == 404
    assert _queue_is_empty(cfg)

def test_refuses_empty_token(tmp_path):
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\n")
    with pytest.raises(SystemExit):
        make_server(load_config(env))

# --- Fix 1: a non-dict JSON body must 400, not crash the request ---

@pytest.mark.parametrize("body", [[], "hi", None, 1])
def test_non_dict_json_body_returns_400(server, body):
    srv, cfg = server
    status, _ = _post(srv.server_address[1], "/share", body, token="sekrit")
    assert status == 400
    assert _queue_is_empty(cfg)

def test_non_json_bytes_body_returns_400(server):
    srv, cfg = server
    raw = _raw_post(srv.server_address[1], "/share", b"not json at all", token="sekrit")
    assert _raw_status(raw) == 400
    assert _queue_is_empty(cfg)

def test_empty_body_returns_400(server):
    srv, cfg = server
    raw = _raw_post(srv.server_address[1], "/share", b"", token="sekrit")
    assert _raw_status(raw) == 400
    assert _queue_is_empty(cfg)

# --- Fix 2: a non-ASCII Authorization header must 401, not crash ---

def test_non_ascii_auth_header_returns_401(server):
    srv, cfg = server
    body = json.dumps({"url": "https://a"}).encode()
    raw = _raw_post(srv.server_address[1], "/share", body,
                     extra_header_lines=["Authorization: Bearer s\xe9krit"])
    assert _raw_status(raw) == 401
    assert _queue_is_empty(cfg)

# --- Fix 3: an enqueue failure must 500, not drop the connection ---

def test_enqueue_failure_returns_500(server, monkeypatch):
    srv, cfg = server
    import src.ingest_server as ingest_server_mod

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ingest_server_mod.ingest, "append_item", _boom)
    status, _ = _post(srv.server_address[1], "/share",
                       {"url": "https://a", "why": "Save this."}, token="sekrit")
    assert status == 500
    assert _queue_is_empty(cfg)

# --- Fix 4: Content-Length sanity (no negative/oversized lengths) ---

def test_negative_content_length_returns_400(server):
    srv, cfg = server
    body = json.dumps({"url": "https://a"}).encode()
    raw = _raw_post(srv.server_address[1], "/share", body, token="sekrit", content_length=-1)
    assert _raw_status(raw) == 400
    assert _queue_is_empty(cfg)

def test_oversized_content_length_returns_400(server):
    srv, cfg = server
    body = json.dumps({"url": "https://a"}).encode()
    raw = _raw_post(srv.server_address[1], "/share", body, token="sekrit",
                     content_length=1_000_000_000)
    assert _raw_status(raw) == 400
    assert _queue_is_empty(cfg)

# --- Fix 6: consistent url validation ---

@pytest.mark.parametrize("url", [None, 123, "", "https://"])
def test_invalid_url_shapes_return_400(server, url):
    srv, cfg = server
    status, _ = _post(srv.server_address[1], "/share", {"url": url}, token="sekrit")
    assert status == 400
    assert _queue_is_empty(cfg)

def test_url_is_stripped_before_validation(server):
    srv, cfg = server
    status, body = _post(srv.server_address[1], "/share",
                         {"url": "  https://a  ", "why": "Save this."}, token="sekrit")
    assert status == 202
    items = rotate_queue(cfg.queue_path)
    assert items[0]["url"] == "https://a"

def test_non_string_text_returns_400(server):
    srv, cfg = server
    status, _ = _post(srv.server_address[1], "/share",
                       {"url": "https://a", "text": {"a": 1}}, token="sekrit")
    assert status == 400
    assert _queue_is_empty(cfg)

@pytest.mark.parametrize("why", [None, "", "line one\nline two", "x" * 281, 42])
def test_missing_or_invalid_why_returns_400(server, why):
    srv, cfg = server
    status, _ = _post(srv.server_address[1], "/share",
                       {"url": "https://a", "why": why}, token="sekrit")
    assert status == 400
    assert _queue_is_empty(cfg)
