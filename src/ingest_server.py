"""Tailnet ingest endpoint: POST /share queues a shared URL for the poll job."""
import hmac
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src import config as config_mod
from src import ingest, media, state

log = logging.getLogger("brain-ingest")

MAX_BODY_BYTES = 65536
MAX_WHY_CHARS = 280

def _why(value) -> str:
    if not isinstance(value, str):
        raise ValueError("why must be a string")
    value = value.strip()
    if not value:
        raise ValueError("why is required")
    if "\n" in value or "\r" in value:
        raise ValueError("why must be one line")
    if len(value) > MAX_WHY_CHARS:
        raise ValueError(f"why must be <= {MAX_WHY_CHARS} characters")
    return value

class ShareHandler(BaseHTTPRequestHandler):
    cfg = None  # set by make_server
    timeout = 10  # bound idle/slow connections so a scan can't hold threads forever

    def _reply(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.cfg.ingest_token}"
        if not self.cfg.ingest_token:
            return False
        try:
            # headers arrive latin-1 decoded; compare bytes so a non-ASCII
            # header can never raise instead of just failing the check
            return hmac.compare_digest(auth.encode("latin-1", "replace"), expected.encode())
        except TypeError:
            return False

    def do_POST(self):
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        if self.path != "/share":
            return self._reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not (0 < length <= MAX_BODY_BYTES):
                raise ValueError("missing or invalid Content-Length")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            url = data.get("url")
            if isinstance(url, str):
                url = url.strip()
            if not isinstance(url, str) or not url:
                raise ValueError("missing or invalid url")
            scheme, sep, rest = url.partition("://")
            if not sep or scheme not in ("http", "https") or not rest:
                raise ValueError("missing or invalid url")
            text = data.get("text")
            if text is not None and not isinstance(text, str):
                raise ValueError("text must be a string")
            why = _why(data.get("why"))
        except (ValueError, json.JSONDecodeError) as e:
            return self._reply(400, {"error": str(e)})
        received_at = datetime.now(timezone.utc).isoformat()
        try:
            video_id = media.youtube_video_id(url)
            if video_id:
                queued = state.queue_video(self.cfg.state_path, video_id, why, received_at)
                log.info("queued youtube %s", video_id)
                return self._reply(202, {
                    "queued": queued,
                    "pipeline": "youtube",
                    "video_id": video_id,
                    "duplicate": not queued,
                })
            ingest.append_item(self.cfg.queue_path, {
                "url": url,
                "why": why,
                "text": text or None,
                "received_at": received_at,
            })
        except Exception:
            log.exception("enqueue failed")
            return self._reply(500, {"error": "enqueue failed"})
        log.info("queued %s", url[:120])
        self._reply(202, {"queued": True, "pipeline": "social"})

    def log_message(self, fmt, *args):  # route http.server noise to logging
        log.debug(fmt, *args)

def make_server(cfg) -> ThreadingHTTPServer:
    if not cfg.ingest_token:
        log.error("INGEST_TOKEN is empty in config.env — refusing to start")
        raise SystemExit(1)
    handler = type("BoundShareHandler", (ShareHandler,), {"cfg": cfg})
    return ThreadingHTTPServer((cfg.ingest_bind, cfg.ingest_port), handler)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    project_root = Path(__file__).resolve().parent.parent
    cfg = config_mod.load_config(project_root / "config.env")
    srv = make_server(cfg)
    log.info("brain-ingest listening on %s:%d", cfg.ingest_bind, srv.server_address[1])
    srv.serve_forever()

if __name__ == "__main__":
    main()
