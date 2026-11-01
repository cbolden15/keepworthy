"""Article and PDF capture for generic /share urls.

The fetch is the risky half: a shared url is untrusted, so every hop is
checked against a public-address rule and the socket connects to the address
that passed, never to the hostname a second time. No cookie jar and no
credentials are ever attached."""
import http.client
import importlib.metadata
import io
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

try:                                     # optional extra: requirements-capture.txt
    import trafilatura as _trafilatura
except ImportError:
    _trafilatura = None
try:
    import pypdf as _pypdf
except ImportError:
    _pypdf = None

log = logging.getLogger("yt-obsidian")


class BlockedURL(Exception):
    """Scheme is not http/https, or the host resolves to a disallowed address
    (initially or after a redirect). Not retryable."""


class FetchError(Exception):
    """Timeout, connection error, TLS error, HTTP 5xx, too many redirects.
    Retryable."""


MIN_WORDS = 150
TIMEOUT_S = 20
MAX_FETCH_S = 60
MAX_REDIRECTS = 5
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_MODEL_CHARS = 60_000
USER_AGENT = "Keepworthy/0.3 (+https://github.com/cbolden15/keepworthy)"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_PARTIAL_HTTP = frozenset({401, 402, 403})       # read anyway: a teaser is worth keeping
_SNIFF_BYTES = 512
_CHUNK = 64 * 1024
_CGNAT = ipaddress.ip_network("100.64.0.0/10")   # is_private is False for it on 3.12
_URL_SAFE = "/%:@!$&'()*+,;=-._~?"
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.I)
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_HTML_MARKERS = (b"<html", b"<!doctype", b"<head")
_clock = time.monotonic                          # tests swap in a fake clock


@dataclass(frozen=True)
class Fetched:
    """One completed GET after redirects. fmt is None when the body was not
    read: an unsupported content type, or a 4xx other than 401-403."""
    status: int
    final_url: str
    host: str
    fmt: str | None
    data: bytes = b""
    capped: bool = False
    charset: str = "utf-8"


def _bad_address(ip) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or not ip.is_global
            or (ip.version == 4 and ip in _CGNAT))


def _disallowed(ip) -> bool:
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _bad_address(ip.ipv4_mapped)
        embedded = [v4 for v4 in (ip.sixtofour, ip.teredo[1] if ip.teredo else None) if v4]
        if any(_bad_address(v4) for v4 in embedded):
            return True
    return _bad_address(ip)


def _target(url: str) -> tuple[str, str, int, str]:
    """(scheme, ascii host, port, request path) or BlockedURL."""
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as e:
        raise BlockedURL(f"malformed url: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {scheme or '(none)'} is not allowed")
    if not host:
        raise BlockedURL("url has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise BlockedURL(f"unusable host name: {e}") from e
    path = quote(parts.path or "/", safe=_URL_SAFE)
    if parts.query:
        path += "?" + quote(parts.query, safe=_URL_SAFE)
    return scheme, host, port or (443 if scheme == "https" else 80), path


def _check_host(host: str, port: int, resolver) -> str:
    """Every address the host resolves to must be public; return the one to
    connect to. An IP-literal host skips DNS and gets the same check."""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise FetchError(f"could not resolve {host}: {e}") from e
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    if not addresses:
        raise FetchError(f"no addresses for {host}")
    for ip in addresses:
        if _disallowed(ip):
            raise BlockedURL(f"{host} resolves to a disallowed address ({ip})")
    first = addresses[0]
    return str(getattr(first, "ipv4_mapped", None) or first)


def _headers(host: str, port: int, scheme: str) -> dict:
    shown = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        shown = f"{shown}:{port}"
    return {"Host": shown, "User-Agent": USER_AGENT,
            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity"}


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP to the checked IP; TLS SNI and certificate check against the url
    host, so virtual hosting and verification work without a second lookup."""

    def __init__(self, ip: str, port: int, server_hostname: str):
        super().__init__(ip, port, timeout=TIMEOUT_S, context=ssl.create_default_context())
        self._server_hostname = server_hostname

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def _connect(ip: str, port: int, host: str, scheme: str):
    if scheme == "https":
        return _PinnedHTTPSConnection(ip, port, host)
    return http.client.HTTPConnection(ip, port, timeout=TIMEOUT_S)


def _sniff(ctype: str, head: bytes) -> str | None:
    if ctype == "application/pdf" or head.startswith(b"%PDF-"):
        return "pdf"
    lowered = head[:_SNIFF_BYTES].lower()
    if ctype in _HTML_TYPES or any(marker in lowered for marker in _HTML_MARKERS):
        return "html"
    return None


def _check_deadline(deadline: float) -> None:
    # TIMEOUT_S bounds each socket operation; this bounds the whole fetch, so
    # a server trickling a byte every few seconds cannot hold a worker
    if _clock() > deadline:
        raise _past_deadline()


def _past_deadline() -> FetchError:
    return FetchError(f"fetch exceeded the {MAX_FETCH_S}s deadline")


def _abort(conn, expired: threading.Event) -> None:
    """Timer callback at the deadline. Shutting the socket down wakes a recv
    blocked in the fetching thread, which a per-recv timeout never does for a
    server that trickles. socket.socket.shutdown skips SSLSocket's override,
    so the TLS object is left for the reader to fail on cleanly."""
    expired.set()
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass    # not connected, or detached mid-handshake: conn.timeout bounds that


def _read_capped(resp, head: bytes, cap: int, length: str | None,
                 deadline: float) -> tuple[bytes, bool]:
    try:
        declared = int(length) if length else None
    except ValueError:
        declared = None
    chunks, total = [head[:cap]], min(len(head), cap)
    while total < cap:
        _check_deadline(deadline)
        chunk = resp.read(min(_CHUNK, cap - total))
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        total += len(chunk)
    capped = declared > cap if declared is not None else bool(resp.read(1))
    return b"".join(chunks), capped


def _read(resp, status: int, url: str, host: str, deadline: float) -> Fetched:
    header = resp.getheader("Content-Type") or ""
    ctype = header.split(";", 1)[0].strip().lower()
    match = _CHARSET.search(header)
    charset = match.group(1) if match else "utf-8"
    encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":  # sent despite Accept-Encoding: identity
        return Fetched(status, url, host, None, charset=charset)
    head = resp.read(_SNIFF_BYTES)          # decide before reading the rest
    fmt = _sniff(ctype, head)
    if fmt is None:
        return Fetched(status, url, host, None, charset=charset)
    cap = MAX_PDF_BYTES if fmt == "pdf" else MAX_HTML_BYTES
    data, capped = _read_capped(resp, head, cap, resp.getheader("Content-Length"), deadline)
    return Fetched(status, url, host, fmt, data, capped, charset)


def _fetch(url: str, resolver, connector) -> Fetched:
    """One GET with redirect following; every hop re-runs the scheme and
    address checks before any connection is made."""
    deadline = _clock() + MAX_FETCH_S
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        _check_deadline(deadline)
        scheme, host, port, path = _target(current)
        ip = _check_host(host, port, resolver)
        remaining = deadline - _clock()
        if remaining <= 0:                      # DNS used it up; 0 would mean non-blocking
            raise _past_deadline()
        expired = threading.Event()
        conn = timer = None
        try:
            conn = connector(ip, port, host, scheme)
            conn.timeout = min(TIMEOUT_S, remaining)    # bounds connect and TLS handshake
            timer = threading.Timer(remaining, _abort, (conn, expired))
            timer.daemon = True
            timer.start()
            conn.request("GET", path, headers=_headers(host, port, scheme))
            if expired.is_set():                # fired before the socket existed
                raise _past_deadline()
            resp = conn.getresponse()
            status = resp.status
            if status in _REDIRECTS:
                location = resp.getheader("Location")
                if not location:
                    raise FetchError(f"http {status} without a Location header")
                current = urljoin(current, location.strip())
                continue
            if status >= 500:
                raise FetchError(f"http {status} from {host}")
            if 400 <= status < 500 and status not in _PARTIAL_HTTP:
                return Fetched(status, current, host, None)
            got = _read(resp, status, current, host, deadline)
            if expired.is_set():                # the cut-off body looks like EOF
                raise _past_deadline()
            return got
        except (OSError, http.client.HTTPException) as e:
            if expired.is_set():
                raise _past_deadline() from e
            raise FetchError(f"fetch failed for {host}: {e}") from e
        finally:
            if timer is not None:
                timer.cancel()
            if conn is not None:
                conn.close()
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


def _version(module, dist: str) -> str | None:
    if module is None:
        return None
    found = getattr(module, "__version__", None)
    if found:
        return str(found)
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def capture_mode() -> dict:
    """Which extractors this interpreter has. Full needs both."""
    full = _trafilatura is not None and _pypdf is not None
    return {"mode": "full" if full else "fallback",
            "trafilatura": _version(_trafilatura, "trafilatura"),
            "pypdf": _version(_pypdf, "pypdf")}


def fetch_article(url: str, *, resolver=socket.getaddrinfo, connector=None) -> dict | None:
    """One GET with redirect following. Returns None for an unsupported
    content type (image, archive, audio, anything not HTML or PDF). Raises
    BlockedURL or FetchError. Otherwise returns title, author, date
    (YYYY-MM-DD or ""), site, text, kind ("article"|"pdf"), source_format
    ("html"|"pdf"), extraction ("full"|"partial"), word_count, status and
    reason ("" when full). "full" always means at least MIN_WORDS words."""
    got = _fetch(url, resolver, connector or _connect)
    if got.fmt is None:
        if got.status >= 400:
            return _result(got, "html", "", {}, f"http {got.status}")
        return None
    if got.fmt == "pdf":
        text, meta, problem = _extract_pdf(got.data)
    else:
        text, meta, problem = _extract_html(got.data, got.charset, got.final_url)
    return _result(got, got.fmt, text, meta, _reason(got, problem, text))


def _reason(got: Fetched, problem: str, text: str) -> str:
    if got.status >= 400:
        return f"http {got.status}"
    if got.capped:
        return "size cap"
    if problem:
        return problem
    if len(text.split()) < MIN_WORDS:
        return "short"
    return ""


def _result(got: Fetched, fmt: str, text: str, meta: dict, reason: str) -> dict:
    return {
        "title": str(meta.get("title") or ""),
        "author": str(meta.get("author") or ""),
        "date": str(meta.get("date") or "")[:10],
        "site": str(meta.get("site") or got.host),
        "text": text,
        "kind": "pdf" if fmt == "pdf" else "article",
        "source_format": fmt,
        "extraction": "partial" if reason else "full",
        "word_count": len(text.split()),
        "status": got.status,
        "reason": reason,
    }


def _decode(data: bytes, charset: str) -> str:
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _meta_tag_reader(html: str) -> tuple[str, dict]:
    from src import social      # social imports this module; a top-level import would be circular
    tags = social._meta_tags(html)
    meta = {"title": social._meta_content(tags, "og:title", "twitter:title"),
            "author": social._meta_content(tags, "author", "article:author"),
            "date": social._meta_content(tags, "article:published_time"),
            "site": social._meta_content(tags, "og:site_name")}
    text = social._meta_content(tags, "og:description", "twitter:description", "description")
    return text, meta


def _extract_html(data: bytes, charset: str, url: str) -> tuple[str, dict, str]:
    if _trafilatura is None:
        text, meta = _meta_tag_reader(_decode(data, charset))
        return text, meta, "fallback"
    # raw bytes: trafilatura does its own charset detection
    try:
        text = _trafilatura.extract(data, url=url, output_format="txt", include_comments=False,
                                    include_tables=True, favor_recall=True) or ""
        doc = _trafilatura.extract_metadata(data, default_url=url)
    except Exception as e:       # it catches only TypeError/ValueError; hostile HTML raises more
        log.warning("html extraction failed for %s: %s", url, type(e).__name__)
        return "", {}, "unreadable html"
    meta = {"title": doc.title, "author": doc.author, "date": doc.date, "site": doc.sitename}
    return text.strip(), meta, ""


def _pdf_meta(reader) -> dict:
    info = reader.metadata
    if info is None:
        return {}
    try:
        created = info.creation_date
    except Exception:            # a malformed date string must not cost the text
        created = None
    return {"title": info.title or "", "author": info.author or "",
            "date": created.strftime("%Y-%m-%d") if created else ""}


def _extract_pdf(data: bytes) -> tuple[str, dict, str]:
    if _pypdf is None:
        return "", {}, "fallback"
    try:
        reader = _pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:       # hostile input can raise far outside PyPdfError
        log.info("pdf unreadable: %s", e)
        return "", {}, "unreadable pdf"
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")      # owner-password-only PDFs open with ""
        except Exception:
            opened = 0
        if not opened:
            return "", {}, "encrypted"
    try:
        pages = reader.pages
        count = len(pages)
        texts = [(pages[i].extract_text() or "").strip() for i in range(min(count, MAX_PDF_PAGES))]
        meta = _pdf_meta(reader)
    except Exception as e:
        log.info("pdf unreadable: %s", e)
        return "", {}, "unreadable pdf"
    text = "\n\n".join(t for t in texts if t)
    return text, meta, "page cap" if count > MAX_PDF_PAGES else ""
