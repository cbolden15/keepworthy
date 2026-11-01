import http.client
import re
import socket
import ssl
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import article, config
from src.article import BlockedURL, FetchError

# example.com's IANA address. The connector in these tests is always fake, so
# nothing is contacted. Documentation ranges (192.0.2.0/24 ...) cannot stand in
# here: they are not globally routable and the guard rightly refuses them.
PUBLIC_IP = "93.184.215.14"
FIXTURES = Path(__file__).parent / "fixtures"
HTML = {"Content-Type": "text/html; charset=utf-8"}


class FakeResponse:
    """http.client.HTTPResponse stand-in: status, getheader, read(amt)."""

    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        self.pos = 0

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, amt=None):
        end = len(self._body) if amt is None else self.pos + amt
        chunk = self._body[self.pos:end]
        self.pos += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response, requests):
        self._response = response
        self._requests = requests
        self.closed = False

    def request(self, method, path, headers=None):
        self._requests.append({"method": method, "path": path,
                               "headers": dict(headers or {})})

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


class FakeNet:
    """Scripted resolver and connector. Each connection serves the next
    scripted response; an exception in the script is raised by the
    connector itself, the way a refused or timed-out connect would be."""

    def __init__(self, *script, addresses=(PUBLIC_IP,)):
        self.script = list(script)
        self.addresses = list(addresses)
        self.connects = []
        self.requests = []
        self.lookups = []

    def resolver(self, host, port, *args, **kwargs):
        self.lookups.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (ip, port)) for ip in self.addresses]

    def connector(self, ip, port, host, scheme):
        self.connects.append((ip, port, host, scheme))
        if not self.script:
            raise AssertionError("unexpected connection")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FakeConnection(step, self.requests)

    def fetch_raw(self, url):
        return article._fetch(url, self.resolver, self.connector)

    def fetch(self, url):
        return article.fetch_article(url, resolver=self.resolver, connector=self.connector)


def _html(body=b"<html><head><title>t</title></head><body>hello</body></html>",
          status=200, headers=HTML):
    return FakeResponse(status, headers, body)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "192.168.0.1", "172.16.5.5", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "fd00:ec2::254",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "fe80::1", "ff02::1", "224.0.0.1",
])
def test_blocked_addresses(ip):
    net = FakeNet(addresses=(ip,))
    with pytest.raises(BlockedURL):
        net.fetch_raw("http://example.com/")
    assert net.connects == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://[::1]:8080/"])
def test_bare_ip_literal_is_checked_without_dns(url):
    net = FakeNet()
    with pytest.raises(BlockedURL):
        net.fetch_raw(url)
    assert net.lookups == [] and net.connects == []


def test_blocked_when_any_resolved_address_is_private():
    net = FakeNet(_html(), addresses=(PUBLIC_IP, "10.0.0.5"))
    with pytest.raises(BlockedURL):
        net.fetch_raw("https://example.com/")
    assert net.connects == []


def test_ipv4_mapped_public_address_connects_to_the_ipv4():
    net = FakeNet(_html(), addresses=(f"::ffff:{PUBLIC_IP}",))
    net.fetch_raw("https://example.com/")
    assert net.connects[0][0] == PUBLIC_IP


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "data:text/html,x"])
def test_blocked_scheme(url):
    net = FakeNet()
    with pytest.raises(BlockedURL):
        net.fetch_raw(url)
    assert net.lookups == [] and net.connects == []


def test_redirect_to_private_is_blocked():
    net = FakeNet(FakeResponse(302, {"Location": "http://169.254.169.254/latest/meta-data/"}))
    with pytest.raises(BlockedURL):
        net.fetch_raw("https://example.com/r")
    assert len(net.connects) == 1 and len(net.requests) == 1


def test_redirect_to_file_scheme_is_blocked():
    net = FakeNet(FakeResponse(301, {"Location": "file:///etc/passwd"}))
    with pytest.raises(BlockedURL):
        net.fetch_raw("https://example.com/r")
    assert len(net.connects) == 1


def test_redirect_is_followed_and_rechecked():
    net = FakeNet(FakeResponse(301, {"Location": "https://example.org/final?x=1"}), _html())
    got = net.fetch_raw("http://example.com/start")
    assert got.status == 200 and got.final_url == "https://example.org/final?x=1"
    assert net.lookups == ["example.com", "example.org"]
    assert net.connects == [(PUBLIC_IP, 80, "example.com", "http"),
                            (PUBLIC_IP, 443, "example.org", "https")]
    assert net.requests[1]["path"] == "/final?x=1"


def test_redirect_limit():
    net = FakeNet(*[FakeResponse(302, {"Location": f"/hop{n}"}) for n in range(6)])
    with pytest.raises(FetchError, match="redirects"):
        net.fetch_raw("https://example.com/hop")
    assert len(net.connects) == 6


def test_connects_to_validated_ip_with_host_header():
    net = FakeNet(_html())
    net.fetch_raw("https://example.com/a/b?c=1#frag")
    assert net.connects == [(PUBLIC_IP, 443, "example.com", "https")]
    [req] = net.requests
    assert req["method"] == "GET" and req["path"] == "/a/b?c=1"
    assert req["headers"]["Host"] == "example.com"


def test_host_header_keeps_a_non_default_port():
    net = FakeNet(_html())
    net.fetch_raw("http://example.com:8080/")
    assert net.connects == [(PUBLIC_IP, 8080, "example.com", "http")]
    assert net.requests[0]["headers"]["Host"] == "example.com:8080"


def test_no_cookie_jar_and_user_agent(tmp_path, monkeypatch):
    # a cookie file where the social path keeps one must change nothing: the
    # article fetch takes no cookie or credential input at all
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\tsecret\n")
    monkeypatch.setitem(config._DEFAULTS, "COOKIES_PATH", str(cookies))
    net = FakeNet(_html())
    net.fetch_raw("https://example.com/")
    headers = {k.lower(): v for k, v in net.requests[0]["headers"].items()}
    assert headers["user-agent"] == article.USER_AGENT
    assert headers["accept-encoding"] == "identity"
    assert "cookie" not in headers and "authorization" not in headers
    assert not any("secret" in v for v in headers.values())


def test_pinned_https_connection_verifies_url_host(monkeypatch):
    conn = article._connect(PUBLIC_IP, 443, "example.com", "https")
    assert (conn.host, conn.port, conn.timeout) == (PUBLIC_IP, 443, article.TIMEOUT_S)
    assert conn._context.check_hostname is True
    assert conn._context.verify_mode == ssl.CERT_REQUIRED
    wrapped = {}

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.update(sock=sock, server_hostname=server_hostname)
            return "tls-socket"

    monkeypatch.setattr(http.client.HTTPConnection, "connect",
                        lambda self: setattr(self, "sock", "tcp-socket"))
    conn._context = FakeContext()
    conn.connect()
    assert wrapped == {"sock": "tcp-socket", "server_hostname": "example.com"}
    assert conn.sock == "tls-socket"
    plain = article._connect(PUBLIC_IP, 80, "example.com", "http")
    assert type(plain) is http.client.HTTPConnection and plain.host == PUBLIC_IP


def test_timeout_is_fetch_error():
    net = FakeNet(socket.timeout("timed out"))
    with pytest.raises(FetchError):
        net.fetch_raw("https://example.com/slow")


def test_dns_failure_is_fetch_error():
    def resolver(host, port, *a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    with pytest.raises(FetchError):
        article._fetch("https://example.com/", resolver, FakeNet().connector)


def test_5xx_is_fetch_error():
    net = FakeNet(FakeResponse(503, HTML, b"busy"))
    with pytest.raises(FetchError, match="503"):
        net.fetch_raw("https://example.com/")


def test_other_4xx_body_is_not_read():
    resp = FakeResponse(404, HTML, b"<html>gone</html>")
    got = FakeNet(resp).fetch_raw("https://example.com/missing")
    assert (got.status, got.fmt, got.data) == (404, None, b"")
    assert resp.pos == 0


def test_html_size_cap():
    body = b"<html><body>" + b"x" * (6 * 1024 * 1024)
    resp = FakeResponse(200, {**HTML, "Content-Length": str(len(body))}, body)
    got = FakeNet(resp).fetch_raw("https://example.com/huge")
    assert got.fmt == "html" and got.capped
    assert len(got.data) == article.MAX_HTML_BYTES
    assert resp.pos <= article.MAX_HTML_BYTES


@pytest.mark.parametrize("ctype,body,expected", [
    ("application/pdf", b"%PDF-1.4 rest", "pdf"),
    ("application/octet-stream", b"%PDF-1.7 rest", "pdf"),
    ("", b"\n  <!DOCTYPE html><html><body>x</body></html>", "html"),
    ("image/png", b"\x89PNG\r\n\x1a\n" + b"\0" * 2048, None),
])
def test_sniff(ctype, body, expected):
    resp = FakeResponse(200, {"Content-Type": ctype} if ctype else {}, body)
    got = FakeNet(resp).fetch_raw("https://example.com/file")
    assert got.fmt == expected
    if expected is None:
        assert resp.pos <= 512 and got.data == b""


@pytest.mark.parametrize("ip", [
    "2002:7f00:1::",                            # 6to4 wrapping 127.0.0.1
    "2002:a00:5::",                             # 6to4 wrapping 10.0.0.5
    "2001:0:4136:e378:8000:63bf:80ff:fffe",     # Teredo, client 127.0.0.1
    "2001:0:4136:e378:8000:63bf:f5ff:fffa",     # Teredo, client 10.0.0.5
])
def test_blocked_embedded_ipv4(ip):
    net = FakeNet(addresses=(ip,))
    with pytest.raises(BlockedURL):
        net.fetch_raw("http://example.com/")
    assert net.connects == []


def test_redirect_without_location_is_fetch_error():
    net = FakeNet(FakeResponse(302, {}))
    with pytest.raises(FetchError, match="Location"):
        net.fetch_raw("https://example.com/r")
    assert len(net.connects) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_partial_status_body_is_read(status):
    body = b"<html><body>first paragraph of a paywalled story</body></html>"
    got = FakeNet(FakeResponse(status, HTML, body)).fetch_raw("https://example.com/paid")
    assert (got.status, got.fmt, got.data, got.capped) == (status, "html", body, False)


def test_pdf_size_cap():
    body = b"%PDF-1.4\n" + b"x" * (article.MAX_PDF_BYTES + 1024)
    resp = FakeResponse(200, {"Content-Type": "application/pdf"}, body)
    got = FakeNet(resp).fetch_raw("https://example.com/big.pdf")
    assert got.fmt == "pdf" and got.capped
    assert len(got.data) == article.MAX_PDF_BYTES
    assert resp.pos <= article.MAX_PDF_BYTES + 1


@pytest.mark.parametrize("ctype,expected", [
    ("text/html; charset=ISO-8859-1", "ISO-8859-1"),
    ('text/html; charset="windows-1252"', "windows-1252"),
    ("text/html;CHARSET = Shift_JIS", "Shift_JIS"),
    ("text/html", "utf-8"),
])
def test_charset_from_content_type(ctype, expected):
    got = FakeNet(_html(headers={"Content-Type": ctype})).fetch_raw("https://example.com/")
    assert got.charset == expected


@pytest.mark.parametrize("exc", [
    ssl.SSLError("tlsv1 alert internal error"),
    ssl.SSLCertVerificationError("certificate verify failed: hostname mismatch"),
])
def test_tls_error_is_fetch_error(exc):
    class TLSFailingConnection(FakeConnection):
        def request(self, method, path, headers=None):
            raise exc

    made = []

    def connector(ip, port, host, scheme):
        made.append(TLSFailingConnection(None, []))
        return made[-1]

    with pytest.raises(FetchError):
        article._fetch("https://example.com/", FakeNet().resolver, connector)
    assert made[0].closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate, gzip"])
def test_compressed_body_is_unsupported(encoding):
    resp = FakeResponse(200, {**HTML, "Content-Encoding": encoding},
                        b"\x1f\x8b\x08\x00" + b"\0" * 2048)
    got = FakeNet(resp).fetch_raw("https://example.com/")
    assert (got.fmt, got.data) == (None, b"")
    assert resp.pos == 0


def test_identity_content_encoding_is_read():
    resp = FakeResponse(200, {**HTML, "Content-Encoding": "identity"}, b"<html>ok</html>")
    got = FakeNet(resp).fetch_raw("https://example.com/")
    assert (got.fmt, got.data) == ("html", b"<html>ok</html>")


def _frozen_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(article, "_clock", lambda: now[0])
    return now


def test_deadline_stops_a_trickling_body(monkeypatch):
    now = _frozen_clock(monkeypatch)

    class Trickle(FakeResponse):
        def read(self, amt=None):
            now[0] += 15                        # each read takes 15 s of wall time
            return super().read(amt)

    body = b"<html><body>" + b"x" * (2 * 1024 * 1024)
    resp = Trickle(200, HTML, body)
    with pytest.raises(FetchError, match="deadline"):
        FakeNet(resp).fetch_raw("https://example.com/slow")
    assert resp.pos < len(body)
    assert now[0] <= article.MAX_FETCH_S + 15


def test_deadline_is_checked_before_each_hop(monkeypatch):
    now = _frozen_clock(monkeypatch)
    net = FakeNet(FakeResponse(302, {"Location": "/next"}), _html())

    def slow_connector(*args):
        now[0] += article.MAX_FETCH_S + 1
        return net.connector(*args)

    with pytest.raises(FetchError, match="deadline"):
        article._fetch("https://example.com/r", net.resolver, slow_connector)
    assert len(net.connects) == 1 and net.lookups == ["example.com"]


@pytest.mark.parametrize("where", ["headers", "body"])
def test_deadline_bounds_a_real_trickling_socket(monkeypatch, where):
    # a real socket pair: each byte arrives well inside the per-recv timeout,
    # so only the whole-fetch deadline can stop the read
    monkeypatch.setattr(article, "MAX_FETCH_S", 1)
    monkeypatch.setattr(article, "TIMEOUT_S", 0.5)
    client, server = socket.socketpair()
    head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 100000\r\n\r\n")

    def serve():
        try:
            server.recv(65536)                  # the GET
            if where == "body":
                server.sendall(head)
            trickle = head if where == "headers" else b"<html><body>" + b"x" * 100
            for byte in trickle[:60]:           # at most ~6 s, then go quiet
                server.sendall(bytes([byte]))
                time.sleep(0.1)
        except OSError:
            pass

    class PairConnection(http.client.HTTPConnection):
        def connect(self):
            client.settimeout(self.timeout)
            self.sock = client

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(FetchError, match="deadline"):
            article._fetch("http://example.com/", FakeNet().resolver,
                           lambda *args: PairConnection(PUBLIC_IP, 80, timeout=article.TIMEOUT_S))
        assert time.monotonic() - started < 3
    finally:
        server.close()
        client.close()
        thread.join(3)


# ---- extraction -----------------------------------------------------------

def _fixture(name):
    return (FIXTURES / name).read_bytes()


class _StubTrafilatura:
    """Stands in for trafilatura where the extra is absent: the body is the
    <article> text with tags stripped; metadata comes from <title> and the
    author meta tag."""
    __version__ = "0-test"

    @staticmethod
    def extract(data, url=None, **kwargs):
        match = re.search(r"<article>(.*?)</article>", data.decode("utf-8"), re.S)
        return re.sub(r"<[^>]+>", " ", match.group(1)) if match else None

    @staticmethod
    def extract_metadata(data, default_url=None):
        html = data.decode("utf-8")
        author = re.search(r'<meta name="author" content="([^"]+)"', html)
        return SimpleNamespace(title=re.search(r"<title>(.*?)</title>", html).group(1),
                               author=author.group(1) if author else None,
                               date="2026-10-01", sitename="Example Notes")


@pytest.fixture(params=["stub", "trafilatura"])
def extractor(request, monkeypatch):
    if request.param == "trafilatura":
        monkeypatch.setattr(article, "_trafilatura", pytest.importorskip("trafilatura"))
    else:
        monkeypatch.setattr(article, "_trafilatura", _StubTrafilatura)
    return request.param


def test_fetch_article_long_html_is_full(extractor):
    art = FakeNet(_html(_fixture("article-long.html"))).fetch("https://example.com/notebook")
    assert art["extraction"] == "full" and art["reason"] == ""
    assert art["word_count"] >= article.MIN_WORDS
    assert art["word_count"] == len(art["text"].split())
    assert art["title"] == "Keeping a Paper Notebook That Lasts"
    assert art["author"] == "Example Author"
    assert (art["kind"], art["source_format"], art["status"]) == ("article", "html", 200)
    assert "acid-free tape" in art["text"]


def test_fetch_article_short_html_is_partial_short(extractor):
    art = FakeNet(_html(_fixture("article-short.html"))).fetch("https://example.com/pencils")
    assert (art["extraction"], art["reason"]) == ("partial", "short")
    assert art["word_count"] < article.MIN_WORDS


def test_fetch_article_403_is_partial():
    resp = _html(_fixture("article-paywall.html"), status=403)
    art = FakeNet(resp).fetch("https://example.com/field-notes")
    assert (art["extraction"], art["reason"], art["status"]) == ("partial", "http 403", 403)
    assert (art["kind"], art["source_format"]) == ("article", "html")


def test_fetch_article_404_is_partial_without_text():
    art = FakeNet(_html(status=404)).fetch("https://example.com/missing")
    assert (art["extraction"], art["reason"], art["status"], art["text"]) == (
        "partial", "http 404", 404, "")
    assert art["site"] == "example.com" and art["word_count"] == 0


def test_fetch_article_fallback_without_extra(monkeypatch):
    monkeypatch.setattr(article, "_trafilatura", None)
    art = FakeNet(_html(_fixture("article-long.html"))).fetch("https://example.com/notebook")
    assert (art["extraction"], art["reason"]) == ("partial", "fallback")
    assert art["title"] == "Keeping a Paper Notebook That Lasts"
    assert art["text"] == "Plain habits for a notebook you will still read in ten years."
    assert (art["author"], art["site"], art["date"]) == (
        "Example Author", "Example Notes", "2026-10-01")


def test_fetch_article_fallback_decodes_declared_charset(monkeypatch):
    monkeypatch.setattr(article, "_trafilatura", None)
    body = '<html><head><meta property="og:title" content="Café notes"></head></html>'
    resp = _html(body.encode("latin-1"), headers={"Content-Type": "text/html; charset=ISO-8859-1"})
    assert FakeNet(resp).fetch("https://example.com/cafe")["title"] == "Café notes"


def test_fetch_article_unsupported_type_returns_none():
    png = FakeResponse(200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    assert FakeNet(png).fetch("https://example.com/photo.png") is None


def test_fetch_article_size_cap_is_partial(monkeypatch):
    monkeypatch.setattr(article, "_trafilatura", None)
    body = b"<html><head></head><body>" + b"x " * (3 * 1024 * 1024)
    art = FakeNet(_html(body)).fetch("https://example.com/huge")
    assert (art["extraction"], art["reason"]) == ("partial", "size cap")


# ---- PDF ------------------------------------------------------------------

PDF = {"Content-Type": "application/pdf"}
SAMPLE_PAGES = [" ".join(["Page one sentence about synthetic notes."] * 20),
                " ".join(["Page two sentence about synthetic notes."] * 20)]


def _pdf_string(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_pdf(pages, title="Synthetic Sample", author="Example Author"):
    """A minimal valid PDF with Helvetica text, built from the stdlib only, so
    fixtures never depend on the optional extra being installed."""
    objects = [b"", b""]                     # 1 catalog, 2 page tree; filled in below

    def add(body):
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for text in pages:
        words = text.split()
        lines = [" ".join(words[i:i + 12]) for i in range(0, len(words), 12)] or [""]
        ops = ["BT", "/F1 11 Tf", "14 TL", "72 760 Td"]
        ops += [f"({_pdf_string(line)}) Tj T*" for line in lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (font, content)))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids), len(kids))
    info = add(b"<< /Title (%s) /Author (%s) /CreationDate (D:20261001120000Z) >>"
               % (title.encode("latin-1"), author.encode("latin-1")))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += (b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, info, xref))
    return bytes(out)


def _stub_pypdf(pages=(), *, encrypted=False, error=None):
    class Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class Reader:
        def __init__(self, stream):
            if error is not None:
                raise error
            self.is_encrypted = encrypted
            self.pages = [Page(text) for text in pages]
            self.metadata = None

        def decrypt(self, password):
            return 0

    return SimpleNamespace(PdfReader=Reader, __version__="0-test")


def test_sample_pdf_fixture_is_reproducible():
    data = _fixture("sample.pdf")
    assert data == _make_pdf(SAMPLE_PAGES) and len(data) < 10 * 1024


def test_fetch_article_pdf(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", pytest.importorskip("pypdf"))
    art = FakeNet(FakeResponse(200, PDF, _fixture("sample.pdf"))).fetch(
        "https://example.com/sample.pdf")
    assert (art["kind"], art["source_format"], art["extraction"]) == ("pdf", "pdf", "full")
    assert "Page one sentence" in art["text"] and "Page two sentence" in art["text"]
    assert (art["title"], art["author"], art["date"]) == (
        "Synthetic Sample", "Example Author", "2026-10-01")


def test_pdf_page_cap(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", pytest.importorskip("pypdf"))
    data = _make_pdf([f"Marker {n:04d} page" for n in range(1, 302)])
    art = FakeNet(FakeResponse(200, PDF, data)).fetch("https://example.com/long.pdf")
    assert (art["extraction"], art["reason"]) == ("partial", "page cap")
    assert "Marker 0300" in art["text"] and "Marker 0301" not in art["text"]


def test_fetch_article_pdf_sniffed_from_magic_bytes(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", _stub_pypdf([" ".join(["word"] * 200)]))
    resp = FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"%PDF-1.4\n%stub\n")
    art = FakeNet(resp).fetch("https://example.com/download?id=7")
    assert (art["kind"], art["source_format"], art["extraction"], art["word_count"]) == (
        "pdf", "pdf", "full", 200)


def test_pdf_page_cap_with_stub_reader(monkeypatch):
    monkeypatch.setattr(article, "_pypdf",
                        _stub_pypdf([f"Marker {n:04d} page" for n in range(1, 302)]))
    art = FakeNet(FakeResponse(200, PDF, b"%PDF-1.4 stub")).fetch("https://example.com/long.pdf")
    assert (art["extraction"], art["reason"]) == ("partial", "page cap")
    assert "Marker 0300" in art["text"] and "Marker 0301" not in art["text"]


def test_pdf_encrypted_is_partial(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", _stub_pypdf(["secret words"], encrypted=True))
    art = FakeNet(FakeResponse(200, PDF, b"%PDF-1.4 stub")).fetch("https://example.com/locked.pdf")
    assert (art["kind"], art["extraction"], art["reason"], art["text"]) == (
        "pdf", "partial", "encrypted", "")


def test_pdf_unreadable_is_partial(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", _stub_pypdf(error=ValueError("bad xref")))
    art = FakeNet(FakeResponse(200, PDF, b"%PDF-1.4 junk")).fetch("https://example.com/bad.pdf")
    assert (art["extraction"], art["reason"], art["text"]) == ("partial", "unreadable pdf", "")


def test_pdf_without_extra_is_partial_fallback(monkeypatch):
    monkeypatch.setattr(article, "_pypdf", None)
    art = FakeNet(FakeResponse(200, PDF, b"%PDF-1.4 stub")).fetch("https://example.com/a.pdf")
    assert (art["kind"], art["extraction"], art["reason"], art["text"]) == (
        "pdf", "partial", "fallback", "")


# ---- capability report ------------------------------------------------------

def test_capture_mode_reports_versions():
    pytest.importorskip("trafilatura")
    pytest.importorskip("pypdf")
    mode = article.capture_mode()
    assert mode["mode"] == "full" and mode["trafilatura"] and mode["pypdf"]


def test_capture_mode_falls_back_when_either_import_is_missing(monkeypatch):
    monkeypatch.setattr(article, "_trafilatura", None)
    monkeypatch.setattr(article, "_pypdf", None)
    assert article.capture_mode() == {"mode": "fallback", "trafilatura": None, "pypdf": None}
    monkeypatch.setattr(article, "_trafilatura", _StubTrafilatura)
    assert article.capture_mode() == {"mode": "fallback", "trafilatura": "0-test", "pypdf": None}
    monkeypatch.setattr(article, "_pypdf", _stub_pypdf())
    assert article.capture_mode() == {"mode": "full", "trafilatura": "0-test", "pypdf": "0-test"}
