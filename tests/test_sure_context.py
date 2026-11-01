import copy
from datetime import datetime, timedelta, timezone
import io
import json
import stat
from urllib.error import HTTPError
from uuid import UUID

import pytest

from src.sure_context import ContextError, SureContext

def ident(n):
    return str(UUID(int=n))

A, S, H = ident(1), ident(2), ident(3)
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)

def fixture():
    return {
        "accounts": [{"id": A, "account_type": "investment", "subtype": "brokerage", "name": "PRIVATE NAME", "balance": "$99999"}],
        "holdings": [{"id": H, "date": "2026-10-31", "qty": "0.00000000000001", "account": {"id": A}, "security": {"id": S}, "amount": "$99999"}],
        "securities": [{"id": S, "ticker": "EXM", "kind": "standard", "country_code": "US", "exchange_operating_mic": "XNAS"}],
    }

def request(**entity):
    return {"schema_version": 1, "request_id": ident(100), "content_id": "post-1", "entities": [{"entity_id": "company-1", **(entity or {"sure_security_id": S})}]}

class Response(io.BytesIO):
    def __init__(self, url, body):
        super().__init__(json.dumps(body).encode())
        self.url = url
    def geturl(self):
        return self.url

class API:
    def __init__(self):
        self.rows, self.calls, self.fail, self.edit = fixture(), [], None, None
    def open(self, req, timeout):
        self.calls.append(req)
        assert req.method == "GET"
        assert 0 < timeout <= 10
        name = req.full_url.split("/api/v1/")[1].split("?")[0]
        if self.fail:
            code = self.fail
            raise HTTPError(req.full_url, code, "PRIVATE ERROR", {}, None)
        rows = self.rows[name]
        body = {name: rows, "pagination": {"page": 1, "per_page": 100, "total_count": len(rows), "total_pages": 1}}
        if self.edit:
            self.edit(name, body)
        return Response(req.full_url, body)

@pytest.fixture
def setup(tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic-key")
    key.chmod(0o600)
    api = API()
    def adapter(**kw):
        return SureContext(key_file=key, scope_id="owner", opener=api, now=lambda: NOW, **kw)
    return api, adapter, key

def test_positive_observation_is_not_a_current_ownership_claim(setup):
    api, adapter, _ = setup
    result = adapter().context(request())
    assert result["snapshot"]["status"] == "available"
    assert result["snapshot"]["freshness"]["state"] == "unverified"
    match = result["matches"][0]
    assert match["identity"]["state"] == "resolved"
    assert match["identity"]["kind"] == "unknown"
    assert match["ownership"]["current_state"] == "unknown"
    assert match["ownership"]["observation"] == "positive_position"
    assert {r["resource"] for r in match["ownership"]["record_refs"]} == {"account", "holding"}
    assert len(api.calls) == 3
    for forbidden in ("PRIVATE NAME", "$99999", "synthetic-key", "qty", "amount"):
        assert forbidden not in json.dumps(result)

def test_newer_zero_supersedes_old_positive_and_conflicts_are_unknown(setup):
    api, adapter, _ = setup
    api.rows["holdings"][0]["date"] = "2026-10-30"
    zero = {**api.rows["holdings"][0], "id": ident(4), "date": "2026-10-31", "qty": "0"}
    api.rows["holdings"].append(zero)
    assert adapter().context(request())["matches"][0]["ownership"]["observation"] == "zero_position"
    api.rows["holdings"].append({**zero, "id": ident(5), "qty": "2"})
    assert adapter().context(request())["matches"][0]["ownership"]["observation"] == "unknown"

@pytest.mark.parametrize("entity,state", [
    ({"name": "Example"}, "unresolved"),
    ({"ticker": "EXM"}, "unresolved"),
    ({"ticker": "EXM", "kind": "equity"}, "unresolved"),
    ({"sure_security_id": S, "ticker": "WRONG"}, "unresolved"),
    ({"ticker": "NEW", "country_code": "US"}, "no_sure_record"),
    ({"ticker": "EXM", "exchange_mic": "XNAS"}, "resolved"),
])
def test_identity_requires_supported_nonconflicting_evidence(setup, entity, state):
    _, adapter, _ = setup
    result = adapter().context(request(**entity))
    assert result["matches"][0]["identity"]["state"] == state
    assert result["matches"][0]["ownership"]["current_state"] == "unknown"

def test_ambiguous_ticker_does_not_pick_first_match(setup):
    api, adapter, _ = setup
    api.rows["securities"].append({**api.rows["securities"][0], "id": ident(8)})
    assert adapter().context(request(ticker="EXM", country_code="US"))["matches"][0]["identity"]["state"] == "ambiguous"

def test_scope_filters_accounts_positions_and_historical_security_metadata(setup):
    api, adapter, _ = setup
    api.rows["accounts"].append({**api.rows["accounts"][0], "id": ident(10)})
    api.rows["securities"].append({**api.rows["securities"][0], "id": ident(11)})
    result = adapter(account_ids=[A]).context(request(sure_security_id=ident(11)))
    assert [a["id"] for a in result["accounts"]] == [A]
    assert result["matches"][0]["identity"]["state"] == "unresolved"
    result = adapter(account_ids=[ident(99)]).context(request())
    assert result["snapshot"]["status"] == "unavailable"
    assert result["accounts"] == []

def test_unknown_account_or_security_reference_degrades_to_partial(setup):
    api, adapter, _ = setup
    api.rows["holdings"][0]["security"]["id"] = ident(99)
    result = adapter().context(request())
    assert result["snapshot"]["status"] == "partial"
    assert result["issues"][0]["code"] == "missing_security_metadata"

@pytest.mark.parametrize("code,issue,calls", [(401, "auth_failed", 1), (403, "scope_denied", 1), (500, "network_error", 2), (429, "rate_limited", 1)])
def test_failures_are_bounded_codes_without_private_response(setup, code, issue, calls):
    api, adapter, _ = setup
    api.fail = code
    result = adapter().context(request())
    assert result["snapshot"]["status"] == "unavailable"
    assert result["issues"][0]["code"] == issue
    assert len(api.calls) == calls
    assert "PRIVATE" not in json.dumps(result)

def test_repeated_ids_and_bad_pagination_are_not_complete(setup):
    api, adapter, _ = setup
    api.rows["holdings"].append(copy.deepcopy(api.rows["holdings"][0]))
    assert adapter().context(request())["snapshot"]["status"] == "partial"
    api.rows = fixture()
    api.edit = lambda name, body: body["pagination"].update(total_count=2001, total_pages=21)
    assert adapter().context(request())["issues"][0]["code"] == "pagination_limit"

def test_cache_is_private_minimal_scoped_and_does_not_reset_fetch_time(setup, tmp_path):
    api, adapter, _ = setup
    cache = tmp_path / "cache.json"
    reader = adapter(cache_file=cache)
    first = reader.context(request())
    reader.now = lambda: NOW + timedelta(minutes=5)
    second = reader.context(request())
    assert first == second and len(api.calls) == 3
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert "PRIVATE NAME" not in cache.read_text() and "$99999" not in cache.read_text()
    assert "0.00000000000001" not in cache.read_text()
    changed = adapter(cache_file=cache, account_ids=[]).context(request())
    assert changed["accounts"] == [] and len(api.calls) == 6

def test_expired_tampered_and_revoked_cache_is_not_used(setup, tmp_path):
    api, adapter, _ = setup
    cache = tmp_path / "cache.json"
    reader = adapter(cache_file=cache)
    reader.context(request())
    reader.now = lambda: NOW + timedelta(hours=2)
    reader.context(request())
    assert len(api.calls) == 6
    cache.write_text('{"payload":"{}","signature":"tampered"}')
    reader.context(request())
    assert len(api.calls) == 9
    api.fail = 401
    assert reader.context(request(), force_refresh=True)["snapshot"]["status"] == "unavailable"
    assert not cache.exists()

def test_insecure_key_and_url_are_rejected_before_network(setup, tmp_path):
    api, adapter, key = setup
    key.chmod(0o644)
    assert adapter().context(request())["issues"][0]["code"] == "auth_failed"
    assert api.calls == []
    with pytest.raises(ContextError):
        adapter(base_url="http://evil.example:3000")
    link = tmp_path / "link"
    link.symlink_to(key)
    assert SureContext(key_file=link, scope_id="owner", opener=api).context(request())["snapshot"]["status"] == "unavailable"

def test_injected_fields_and_duplicate_entities_are_rejected_before_reads(setup):
    api, adapter, _ = setup
    for bad in ({**request(), "url": "http://evil.example"}, {**request(), "entities": request()["entities"] * 2}):
        with pytest.raises(ContextError):
            adapter().context(bad)
    assert api.calls == []

def test_source_age_configuration_is_bounded_and_returned(setup):
    _, adapter, _ = setup
    assert adapter(max_source_age_seconds=86400).context(request())["snapshot"]["freshness"]["max_source_age_seconds"] == 86400
    for invalid in (True, 0, 172801):
        with pytest.raises(ContextError):
            adapter(max_source_age_seconds=invalid)

def test_non_utf8_key_returns_a_bounded_auth_error(setup):
    api, adapter, key = setup
    key.write_bytes(b"\xff\xfe")
    assert adapter().context(request())["issues"][0]["code"] == "auth_failed"
    assert api.calls == []

def test_empty_entities_still_get_account_context(setup):
    _, adapter, _ = setup
    result = adapter().context({**request(), "entities": []})
    assert result["accounts"] and result["matches"] == []

def test_vendor_exchange_code_does_not_become_a_fabricated_mic(setup):
    api, adapter, _ = setup
    api.rows["securities"][0]["exchange_operating_mic"] = "OPR"
    result = adapter().context(request())
    assert result["snapshot"]["status"] == "available"
    assert result["matches"][0]["identity"]["exchange_mic"] is None
    assert result["matches"][0]["ownership"]["observation"] == "positive_position"
    assert {"code": "missing_security_metadata", "retryable": False} in result["issues"]

def test_full_pagination_does_not_stop_after_first_page(setup):
    api, adapter, _ = setup
    original = api.open
    def paginated(req, timeout):
        if "/accounts?" not in req.full_url:
            return original(req, timeout)
        page = int(req.full_url.split("page=")[1].split("&")[0])
        rows = [{**fixture()["accounts"][0], "id": ident(1000 + n)} for n in range(100)] if page == 1 else fixture()["accounts"]
        return Response(req.full_url, {"accounts": rows, "pagination": {"page": page, "per_page": 100, "total_count": 101, "total_pages": 2}})
    api.open = paginated
    result = adapter().context(request())
    assert result["snapshot"]["status"] == "available"
    assert len(result["accounts"]) == 101

def test_redirect_is_rejected_without_sending_key_to_target(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/private-target")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    key = tmp_path / "key"
    key.write_text("fixture")
    key.chmod(0o600)
    try:
        result = SureContext(key_file=key, scope_id="owner", base_url=f"http://127.0.0.1:{server.server_port}").context(request())
        assert result["snapshot"]["status"] == "unavailable"
        assert calls == ["/api/v1/accounts?page=1&per_page=100"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

def test_real_http_reads_work_without_using_environment_proxy(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    rows = fixture()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            assert self.headers["X-Api-Key"] == "fixture"
            name = self.path.split("/api/v1/")[1].split("?")[0]
            calls.append(name)
            body = json.dumps({name: rows[name], "pagination": {"page": 1, "per_page": 100, "total_count": len(rows[name]), "total_pages": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    key = tmp_path / "key"
    key.write_text("fixture")
    key.chmod(0o600)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    try:
        result = SureContext(key_file=key, scope_id="owner", base_url=f"http://127.0.0.1:{server.server_port}", now=lambda: NOW).context(request())
        assert result["snapshot"]["status"] == "available"
        assert result["matches"][0]["ownership"]["observation"] == "positive_position"
        assert calls == ["accounts", "holdings", "securities"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
