"""Join public finance extraction to private Sure context after model execution."""
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from src.sure_context import SureContext, uuid

log = logging.getLogger("yt-obsidian")
_SYMBOL = re.compile(r"(?<![\w$])(?:(NASDAQ|NYSE|XNAS|XNYS):|\$)([A-Z][A-Z0-9.-]{0,15})(?![\w.-])")
_MICS = {"NASDAQ": "XNAS", "NYSE": "XNYS", "XNAS": "XNAS", "XNYS": "XNYS"}


def entities_from_source(source):
    """Explicit symbols only; no ticker guesses from ordinary words or model prose."""
    found = {}
    for match in _SYMBOL.finditer(source):
        exchange, ticker = match.groups()
        key = (ticker, _MICS.get(exchange))
        found.setdefault(key, {"entity_id": f"symbol-{len(found) + 1}", "ticker": ticker})
        if exchange:
            found[key]["exchange_mic"] = _MICS[exchange]
        if len(found) >= 50:
            break
    return list(found.values())


class FinanceContent:
    def __init__(self, reader, ui_origin):
        if not isinstance(ui_origin, str) or any(c.isspace() for c in ui_origin):
            raise ValueError("Sure UI origin must be a string")
        parsed = urlsplit(ui_origin)
        parsed.port  # Validate an explicitly supplied port before constructing links.
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("Sure UI origin must be an HTTPS origin without a path or credentials")
        self.reader, self.ui_origin = reader, ui_origin.rstrip("/")

    def annotate(self, model_meta, body, source, content_key, *, retry_dir=None):
        if model_meta.get("topic") != "Finance":
            return body
        request = {"schema_version": 1, "request_id": str(uuid4()),
                   "content_id": hashlib.sha256(content_key.encode()).hexdigest(),
                   "entities": entities_from_source(source)}
        try:
            context = self.reader.context(request)
            section = self._render(context)
            if retry_dir is not None:
                retry_path = self._retry_path(retry_dir, request["content_id"])
                if any(issue["retryable"] for issue in context["issues"]):
                    self._queue_retry(retry_path, request)
                elif context["snapshot"]["status"] == "available":
                    retry_path.unlink(missing_ok=True)
        except Exception as error:
            # A financial-context failure must not discard an otherwise useful source.
            # Exception messages may contain private data, so log only the class.
            log.warning("Sure finance context failed (%s)", type(error).__name__)
            section = "## Your financial context\nSure context is unavailable; this source note was saved without it."
        return body.rstrip() + "\n\n" + section

    def _retry_path(self, directory, content_id):
        identity = hashlib.sha256((self.reader.scope_id + ":" + content_id).encode()).hexdigest()
        return Path(directory) / (identity + ".json")

    def _queue_retry(self, path, request):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".finance-retry-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"schema_version": 1, "scope_id": self.reader.scope_id, "request": request,
                           "created_at": datetime.now(timezone.utc).isoformat(),
                           "attempts": 0, "max_attempts": 3}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Reprocessing must not reset an existing retry budget.
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _render(self, context):
        snapshot = context["snapshot"]
        lines = ["## Your financial context", "Source: your connected Sure accounts.",
                 f"Snapshot: {snapshot['fetched_at']}. Availability: {snapshot['status']}.",
                 "Current ownership is unverified; dated observations below describe Sure records."]
        provenance = json.dumps({"snapshot_id": snapshot["id"], "scope_id": context["scope_id"],
                                 "content_id": context["content_id"],
                                 "fetched_at": snapshot["fetched_at"], "schema_version": 1}, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
        lines.append(f"<!-- sure-context: {provenance} -->")
        groups = Counter((row["type"], row["subtype"]) for row in context["accounts"])
        if groups:
            descriptions = []
            for (kind, subtype), count in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] or "")):
                # Types and subtypes are data, escaped so they cannot create Markdown links.
                label = re.sub(r"[^a-zA-Z0-9 _-]", "", kind + (f" / {subtype}" if subtype else ""))
                descriptions.append(f"{label}: {count}")
            lines.append("Connected account types: " + "; ".join(descriptions) + ".")
        for match in context["matches"]:
            identity, ownership = match["identity"], match["ownership"]
            symbol = re.sub(r"[^A-Z0-9.-]", "", identity.get("ticker") or "") or "Unresolved company"
            if identity["state"] != "resolved":
                lines.append(f"- {symbol}: identity {identity['state']}; ownership unknown.")
                continue
            observation = ownership["observation"]
            if observation in {"positive_position", "zero_position"}:
                description = "positive position" if observation == "positive_position" else "zero position"
                lines.append(f"- {symbol}: {description} observed in Sure on {ownership['observation_date']}; current ownership unknown.")
            else:
                lines.append(f"- {symbol}: no conclusive holding observation; current ownership unknown.")
            for ref in ownership["record_refs"]:
                resource = ref["resource"]
                if resource not in {"account", "holding"}:
                    raise ValueError("Unsupported Sure record reference")
                ident = uuid(ref["id"])
                lines.append(f"  - [Open Sure {resource}]({self.ui_origin}/{resource}s/{ident})")
        if context["issues"]:
            lines.append("Context issues: " + ", ".join(sorted({issue["code"] for issue in context["issues"]})) + ".")
        return "\n".join(lines)


def load_finance_content(path):
    """Owner configuration contains secret-file references, never secret values."""
    if not Path(path).is_absolute():
        raise ValueError("Sure context configuration path must be absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError("Sure context configuration must be a private owner-controlled file under 16 KiB")
        options = json.load(stream)
    if not isinstance(options, dict) or not {"key_file", "scope_id", "ui_origin"} <= set(options) or not set(options) <= {
        "key_file", "scope_id", "ui_origin", "base_url", "account_ids", "cache_file", "max_source_age_seconds"}:
        raise ValueError("Invalid Sure context configuration fields")
    options = dict(options)
    origin = options.pop("ui_origin")
    if "account_ids" in options and (not isinstance(options["account_ids"], list) or len(options["account_ids"]) > 2000):
        raise ValueError("Sure account_ids must be a bounded list")
    for field in ("key_file", "cache_file"):
        if field in options and (not isinstance(options[field], str) or not Path(options[field]).is_absolute()):
            raise ValueError("Sure key and cache paths must be absolute")
    return FinanceContent(SureContext(**options), origin)


def annotate(cfg, model_meta, body, source, content_key):
    processor = getattr(cfg, "finance_content", None)
    if processor is None:
        return body
    return processor.annotate(model_meta, body, source, content_key,
                              retry_dir=Path(cfg.state_path).parent / "finance-context-retries")
