"""Article and PDF capture for generic /share urls.

The fetch is the risky half: a shared url is untrusted, so every hop is
checked against a public-address rule and the socket connects to the address
that passed, never to the hostname a second time. No cookie jar and no
credentials are ever attached."""
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

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
        raise FetchError(f"fetch exceeded the {MAX_FETCH_S}s deadline")


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
        conn = None
        try:
            conn = connector(ip, port, host, scheme)
            conn.request("GET", path, headers=_headers(host, port, scheme))
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
            return _read(resp, status, current, host, deadline)
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"fetch failed for {host}: {e}") from e
        finally:
            if conn is not None:
                conn.close()
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")
