import http.client
import socket
import ssl
from pathlib import Path

import pytest

from src import article
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


def test_no_cookie_jar_and_user_agent(tmp_path):
    # a cookie file where the social path keeps one must change nothing: the
    # article fetch takes no cookie or credential input at all
    (tmp_path / "cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\tsecret\n")
    net = FakeNet(_html())
    net.fetch_raw("https://example.com/")
    headers = {k.lower(): v for k, v in net.requests[0]["headers"].items()}
    assert headers["user-agent"] == article.USER_AGENT
    assert headers["accept-encoding"] == "identity"
    assert "cookie" not in headers and "authorization" not in headers


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
