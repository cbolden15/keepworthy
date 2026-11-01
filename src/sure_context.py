"""Read-only, minimal Sure context. Public extraction runs before this adapter."""
import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

ACCOUNT_TYPES = {"depository", "investment", "crypto", "credit_card", "loan", "other"}
KINDS = {"equity", "etf", "fund", "crypto", "option", "bond", "other", "unknown"}


class ContextError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def require(condition, code="invalid_response"):
    if not condition:
        raise ContextError(code)


def uuid(value):
    require(isinstance(value, str))
    try:
        require(str(UUID(value)) == value.lower())
    except ValueError:
        raise ContextError("invalid_response") from None
    return value.lower()


def text(value, maximum):
    require(isinstance(value, str) and 0 < len(value) <= maximum)
    return value


def validate_request(request):
    """Reject arbitrary request fields before accessing any financial data."""
    require(isinstance(request, dict) and set(request) == {"schema_version", "request_id", "content_id", "entities"})
    require(type(request["schema_version"]) is int and request["schema_version"] == 1)
    uuid(request["request_id"])
    text(request["content_id"], 128)
    require(isinstance(request["entities"], list) and len(request["entities"]) <= 50)
    seen = set()
    for entity in request["entities"]:
        require(isinstance(entity, dict) and set(entity) <= {
            "entity_id", "sure_security_id", "ticker", "name", "kind", "exchange_mic", "country_code"})
        ident = text(entity.get("entity_id"), 128)
        require(ident not in seen and bool(set(entity) & {"sure_security_id", "ticker", "name"}))
        seen.add(ident)
        for key, maximum in (("ticker", 64), ("name", 256)):
            if key in entity:
                text(entity[key], maximum)
        if "sure_security_id" in entity:
            uuid(entity["sure_security_id"])
        if "kind" in entity:
            require(isinstance(entity["kind"], str) and entity["kind"] in KINDS)
        for key, pattern in (("exchange_mic", r"[A-Z0-9]{4}"), ("country_code", r"[A-Z]{2}")):
            if key in entity:
                require(isinstance(entity[key], str) and re.fullmatch(pattern, entity[key]) is not None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_key(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_size <= 513, "auth_failed")
            key = stream.read(513).strip()
    except (OSError, UnicodeError):
        raise ContextError("auth_failed") from None
    require(0 < len(key) <= 512 and key.isprintable() and not any(c.isspace() for c in key), "auth_failed")
    return key


class SureContext:
    """Only three enumerated GET endpoints; configuration is owner-controlled.

    The installed key must have exactly Sure's read scope. No upstream freshness
    attestation is currently available: current ownership always remains unknown.
    """
    def __init__(self, *, key_file, scope_id, base_url="http://127.0.0.1:3000",
                 account_ids=None, cache_file=None, max_source_age_seconds=129600, opener=None, now=None,
                 monotonic=time.monotonic, sleep=time.sleep):
        parsed = urllib.parse.urlsplit(base_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            parsed.port
        except ValueError:
            loopback = False
        require(parsed.scheme == "http" and loopback and not parsed.username and not parsed.password
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment, "scope_mismatch")
        self.base_url = base_url.rstrip("/")
        self.key_file, self.scope_id = Path(key_file), text(scope_id, 128)
        require(type(max_source_age_seconds) is int and 3600 <= max_source_age_seconds <= 172800)
        self.max_source_age_seconds = max_source_age_seconds
        self.account_ids = None if account_ids is None else frozenset(uuid(x) for x in account_ids)
        self.cache_file = Path(cache_file) if cache_file is not None else None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.sleep = monotonic, sleep

    def _get(self, collection, page, key, budget):
        require(collection in {"accounts", "holdings", "securities"}, "scope_denied")
        url = self.base_url + "/api/v1/" + collection + "?" + urllib.parse.urlencode({"page": page, "per_page": 100})
        for attempt in range(2):
            remaining = budget["deadline"] - self.monotonic()
            require(remaining > 0, "timeout")
            request = urllib.request.Request(url, headers={"X-Api-Key": key, "Accept": "application/json"}, method="GET")
            try:
                with self.opener.open(request, timeout=min(10, remaining)) as response:
                    require(response.geturl() == url)
                    raw = response.read(2 * 1024 * 1024 + 1)
                budget["bytes"] += len(raw)
                require(len(raw) <= 2 * 1024 * 1024 and budget["bytes"] <= 16 * 1024 * 1024)
                require(self.monotonic() <= budget["deadline"], "timeout")
                result = json.loads(raw)
                require(isinstance(result, dict))
                return result
            except urllib.error.HTTPError as error:
                code = {401: "auth_failed", 403: "scope_denied", 429: "rate_limited"}.get(error.code, "network_error" if error.code >= 500 else "invalid_response")
                delay = 0
                if error.code == 429:
                    value = error.headers.get("Retry-After", "")
                    if value.isdigit() and 0 < int(value) <= 5:
                        delay = int(value)
                    else:
                        raise ContextError(code, True) from None
                retryable = error.code == 429 or error.code >= 500
                if not retryable or attempt or self.monotonic() + delay >= budget["deadline"]:
                    raise ContextError(code, retryable) from None
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
                if attempt:
                    raise ContextError("timeout" if isinstance(error, TimeoutError) else "network_error", True) from None
            except (ValueError, UnicodeError):
                raise ContextError("invalid_response") from None
        raise ContextError("network_error", True)

    def _pages(self, collection, key, budget):
        rows, ids, total = [], set(), None
        for page in range(1, 21):
            body = self._get(collection, page, key, budget)
            chunk, pagination = body.get(collection), body.get("pagination")
            require(isinstance(chunk, list) and len(chunk) <= 100 and isinstance(pagination, dict))
            require(all(type(pagination.get(k)) is int for k in ("page", "per_page", "total_count", "total_pages")))
            require(pagination["page"] == page and pagination["per_page"] == 100 and pagination["total_count"] >= 0)
            require(pagination["total_pages"] == max(1, (pagination["total_count"] + 99) // 100))
            if total is None:
                total = pagination["total_count"]
            require(total == pagination["total_count"], "changing_snapshot")
            require(pagination["total_pages"] <= 20, "pagination_limit")
            for row in chunk:
                require(isinstance(row, dict))
                ident = uuid(row.get("id"))
                require(ident not in ids, "changing_snapshot")
                ids.add(ident)
            rows.extend(chunk)
            if page == pagination["total_pages"]:
                require(len(rows) == total, "changing_snapshot")
                return rows
            require(len(chunk) == 100, "changing_snapshot")
        raise ContextError("pagination_limit")

    def _refresh(self, key):
        budget = {"deadline": self.monotonic() + 60, "bytes": 0}
        snapshot = {"accounts": [], "securities": [], "positions": [], "status": "unavailable", "issues": []}
        try:
            accounts = self._pages("accounts", key, budget)
            all_ids = {uuid(row["id"]) for row in accounts}
            require(self.account_ids is None or self.account_ids <= all_ids, "scope_mismatch")
            selected = all_ids if self.account_ids is None else self.account_ids
            projected = []
            for row in accounts:
                if uuid(row["id"]) in selected:
                    subtype = row.get("subtype")
                    require(subtype is None or isinstance(subtype, str) and len(subtype) <= 128)
                    kind = row.get("account_type")
                    require(isinstance(kind, str))
                    projected.append({"id": uuid(row["id"]), "type": kind if kind in ACCOUNT_TYPES else "other", "subtype": subtype})
            snapshot.update(accounts=projected, status="partial")
            holdings = self._pages("holdings", key, budget)
            securities = self._pages("securities", key, budget)
            security_ids = {uuid(row["id"]) for row in securities}
            groups = {}
            for row in holdings:
                account, security = uuid(row.get("account", {}).get("id")), uuid(row.get("security", {}).get("id"))
                require(account in all_ids and security in security_ids, "missing_security_metadata")
                if account not in selected:
                    continue
                day = date.fromisoformat(row["date"])
                require(day <= self.now().date())
                qty = Decimal(str(row["qty"]))
                require(qty.is_finite())
                item = {"account": account, "security": security, "date": day.isoformat(), "id": uuid(row["id"]),
                        "sign": 1 if qty > 0 else 0 if qty == 0 else -1, "conflict": False}
                previous = groups.get((account, security))
                if previous is None or previous[0]["date"] < item["date"]:
                    groups[(account, security)] = (item, qty)
                elif previous[0]["date"] == item["date"]:
                    previous[0]["conflict"] |= previous[1] != qty
            snapshot["positions"] = [item for item, _ in groups.values()]
            scoped_ids = {item["security"] for item in snapshot["positions"]}
            for row in securities:
                if self.account_ids is not None and uuid(row["id"]) not in scoped_ids:
                    continue  # Historical securities cannot be assigned to an allowlisted account via this API.
                ticker = text(row.get("ticker"), 64).strip().upper()
                require(bool(ticker))
                mic, country = row.get("exchange_operating_mic") or None, row.get("country_code") or None
                for value in (mic, country):
                    require(value is None or isinstance(value, str) and len(value) <= 128)
                mic = mic.strip().upper() if mic is not None else None
                country = country.strip().upper() if country is not None else None
                # Sure also stores vendor exchange codes (e.g. OPR/BTS), not only MICs.
                if mic is not None and re.fullmatch(r"[A-Z0-9]{4}", mic) is None:
                    mic = None
                    issue = {"code": "missing_security_metadata", "retryable": False}
                    if issue not in snapshot["issues"]:
                        snapshot["issues"].append(issue)
                if country is not None and re.fullmatch(r"[A-Z]{2}", country) is None:
                    country = None
                kind = row.get("kind")
                require(isinstance(kind, str))
                snapshot["securities"].append({"id": uuid(row["id"]), "ticker": ticker,
                    "kind": kind if kind in KINDS else "unknown", "exchange_mic": mic, "country_code": country})
            snapshot["status"] = "available"
        except (ContextError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as error:
            failure = error if isinstance(error, ContextError) else ContextError("invalid_response")
            snapshot["issues"].append({"code": failure.code, "retryable": failure.retryable})
            if failure.code in {"auth_failed", "scope_denied", "scope_mismatch"}:
                snapshot.update(accounts=[], securities=[], positions=[], status="unavailable")
        now = self.now()
        snapshot.update(id=str(uuid4()) if snapshot["status"] != "unavailable" else None,
                        fetched_at=now.isoformat(), cache_expires_at=(now + timedelta(hours=1)).isoformat())
        return snapshot

    def _snapshot(self, key, force_refresh):
        if self.cache_file is None:
            return self._refresh(key)
        path = self.cache_file
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        binding = json.dumps([self.scope_id, self.base_url, sorted(self.account_ids) if self.account_ids is not None else None])
        def signature(raw):
            return hmac.new(key.encode(), (binding + raw).encode(), hashlib.sha256).hexdigest()
        lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock, "r+") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ContextError("timeout", True) from None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as cached:
                    info = os.fstat(cached.fileno())
                    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
                    envelope = json.loads(cached.read(16 * 1024 * 1024 + 1))
                raw = envelope["payload"]
                require(isinstance(raw, str) and hmac.compare_digest(signature(raw), envelope["signature"]))
                snapshot = json.loads(raw)
                require(datetime.fromisoformat(snapshot["fetched_at"]) <= self.now() < datetime.fromisoformat(snapshot["cache_expires_at"]))
                if not force_refresh:
                    return snapshot
            except (OSError, ContextError, ValueError, KeyError, TypeError):
                pass  # Invalid, expired, or differently scoped caches are never used.
            path.unlink(missing_ok=True)
            snapshot = self._refresh(key)
            if snapshot["status"] == "available":
                raw = json.dumps(snapshot, sort_keys=True)
                fd, name = tempfile.mkstemp(dir=path.parent, prefix=".sure-context-")
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump({"payload": raw, "signature": signature(raw)}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, path)
                finally:
                    Path(name).unlink(missing_ok=True)
            return snapshot

    def context(self, request, *, force_refresh=False):
        validate_request(request)
        try:
            snapshot = self._snapshot(read_key(self.key_file), force_refresh)
        except (OSError, ContextError) as error:
            failure = error if isinstance(error, ContextError) else ContextError("invalid_response")
            # Invalid key material immediately invalidates any persisted projection.
            if self.cache_file is not None and failure.code in {"auth_failed", "scope_mismatch"}:
                self.cache_file.unlink(missing_ok=True)
            now = self.now()
            snapshot = {"id": None, "status": "unavailable", "fetched_at": now.isoformat(),
                        "cache_expires_at": now.isoformat(), "accounts": [], "positions": [], "securities": [],
                        "issues": [{"code": failure.code, "retryable": failure.retryable}]}
        issues = copy.deepcopy(snapshot["issues"])
        issues.append({"code": "freshness_unverified", "retryable": False})
        matches = [self._match(entity, snapshot) for entity in request["entities"]]
        return {"schema_version": 1, "request_id": request["request_id"], "content_id": request["content_id"],
                "scope_id": self.scope_id, "snapshot": {k: snapshot[k] for k in ("id", "status", "fetched_at", "cache_expires_at")} | {
                    "freshness": {"state": "unverified", "oldest_source_sync_at": None, "max_source_age_seconds": self.max_source_age_seconds}},
                "accounts": copy.deepcopy(snapshot["accounts"]), "matches": matches, "issues": issues}

    def _match(self, entity, snapshot):
        candidates = snapshot["securities"]
        requested_id = entity.get("sure_security_id")
        ticker = entity.get("ticker", "").strip().upper()
        if requested_id:
            candidates = [row for row in candidates if row["id"] == requested_id.lower()]
        elif ticker:
            candidates = [row for row in candidates if row["ticker"] == ticker]
        else:
            candidates = []
        had_candidate = bool(candidates)
        for field in ("exchange_mic", "country_code"):
            if field in entity:
                candidates = [row for row in candidates if row[field] == entity[field]]
        if ticker:
            candidates = [row for row in candidates if row["ticker"] == ticker]
        if entity.get("kind", "unknown") != "unknown":
            candidates = [row for row in candidates if row["kind"] == entity["kind"]]
        if not requested_id and not (entity.get("exchange_mic") or entity.get("country_code") or entity.get("kind", "unknown") != "unknown"):
            candidates = []  # A bare ticker is not sufficient identity evidence.
        state = "resolved" if len(candidates) == 1 else "ambiguous" if candidates else (
            "no_sure_record" if ticker and not requested_id and not had_candidate and snapshot["status"] == "available" else "unresolved")
        identity = {"state": state, "sure_security_id": None, "ticker": ticker or None, "kind": "unknown", "exchange_mic": None, "country_code": None}
        ownership = {"current_state": "unknown", "observation": "unknown", "observation_date": None, "record_refs": []}
        if state == "resolved":
            security = candidates[0]
            identity.update({k: security[k] for k in ("ticker", "kind", "exchange_mic", "country_code")}, sure_security_id=security["id"])
            positions = [row for row in snapshot["positions"] if row["security"] == security["id"]]
            if positions and not any(row["conflict"] or row["sign"] < 0 for row in positions):
                relevant = [row for row in positions if row["sign"] > 0] or positions
                ownership.update(observation="positive_position" if any(row["sign"] > 0 for row in relevant) else "zero_position",
                                 observation_date=min(row["date"] for row in relevant))
                refs = {(resource, row[field]) for row in relevant for resource, field in (("account", "account"), ("holding", "id"))}
                if len(refs) <= 2000:
                    ownership["record_refs"] = [{"resource": resource, "id": ident} for resource, ident in sorted(refs)]
                else:
                    ownership.update(observation="unknown", observation_date=None)
            elif not positions and snapshot["status"] == "available":
                ownership["observation"] = "no_position"
        return {"entity_id": entity["entity_id"], "identity": identity, "ownership": ownership}
