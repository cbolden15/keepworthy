#!/usr/bin/env python3
"""Read saved feeds over HTTP; keep cookies and private output outside Git."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sys
import stat
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
           "Upgrade-Insecure-Requests": "1"}


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def documents(body):
    for match in re.finditer(r'<script[^>]*type="application/json"[^>]*>([\s\S]*?)</script>', body):
        try:
            yield json.loads(match[1])
        except json.JSONDecodeError:
            continue  # A script can contain unrelated non-JSON content.


def connection(docs, name):
    for doc in docs:
        for obj in objects(doc):
            value = obj.get(name)
            if isinstance(value, dict) and "edges" in value and "page_info" in value:
                return value
    raise ValueError(f"Missing {name} feed; sign-in or response format needs checking")


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


class Client:
    def __init__(self, cookie_path):
        info = Path(cookie_path).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Cookies must be an owner-only regular file")
        self.cookies = http.cookiejar.MozillaCookieJar(str(cookie_path))
        self.cookies.load(ignore_discard=True, ignore_expires=False)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, url, headers=None, params=None):
        data = urllib.parse.urlencode(params).encode() if params is not None else None
        request = urllib.request.Request(url, data=data, headers=headers or HEADERS)
        with self.opener.open(request, timeout=25) as response:
            if "/login" in response.url or "/checkpoint" in response.url:
                raise ValueError("Login or account challenge required")
            body = response.read(20_000_001)
            if len(body) > 20_000_000:
                raise ValueError("Saved-feed response exceeds size limit")
            return body.decode()

    def instagram(self, limit):
        items, cursor, seen = [], None, set()
        while len(items) < limit:
            if len(seen) >= 20:
                raise ValueError("Instagram pagination exceeds page budget")
            query = "?" + urllib.parse.urlencode({"max_id": cursor}) if cursor else ""
            body = self.request("https://www.instagram.com/api/v1/feed/saved/posts/" + query,
                                {**HEADERS, "X-IG-App-ID": "936619743392459",
                                 "Referer": "https://www.instagram.com/"})
            data = json.loads(body)
            if data.get("status") != "ok":
                raise ValueError("Instagram rejected saved-feed request")
            items.extend(item["media"] for item in data.get("items", []))
            if not data.get("more_available"):
                break
            cursor = data.get("next_max_id")
            if not cursor or cursor in seen:
                raise ValueError("Instagram pagination cursor missing or repeated")
            seen.add(cursor)
            if len(items) < limit:
                time.sleep(6)
        self.has_more = len(items) > limit or bool(data.get("more_available"))
        return items[:limit]

    def facebook(self, limit):
        body = self.request("https://www.facebook.com/saved/")
        docs = list(documents(body))
        feed = connection(docs, "all_saves")
        nodes = [edge["node"] for edge in feed["edges"]]
        site = next(obj for doc in docs for obj in objects(doc) if "haste_session" in obj)
        def token(name):
            match = re.search(r'\["' + name + r'",\[\],\{"token":"([^"]+)"', body)
            if not match:
                raise ValueError(f"Missing {name}; sign-in or page format needs checking")
            return json.loads('"' + match[1] + '"')
        dtsg, lsd = token("DTSGInitialData"), token("LSD")
        uid = next(c.value for c in self.cookies if c.domain == ".facebook.com" and c.name == "c_user")
        seen = set()
        while len(nodes) < limit and feed["page_info"]["has_next_page"]:
            if len(seen) >= 20:
                raise ValueError("Facebook pagination exceeds page budget")
            cursor = feed["page_info"].get("end_cursor")
            if not cursor or cursor in seen:
                raise ValueError("Facebook pagination cursor missing or repeated")
            seen.add(cursor)
            operation = "CometSaveDashboardAllItemsPaginationQuery"
            params = {"av": uid, "__user": uid, "__aaid": "0", "__a": "1", "__req": "4",
                      "__hs": site["haste_session"], "dpr": "2", "__ccg": "MODERATE",
                      "__rev": site["client_revision"], "__hsi": site["hsi"],
                      "__comet_req": site["comet_env"], "fb_dtsg": dtsg, "lsd": lsd,
                      "jazoest": "2" + str(sum(map(ord, dtsg))),
                      "__crn": "comet.fbweb.CometSaveDashboardRoute",
                      "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": operation,
                      "server_timestamps": "true", "doc_id": "29296152856643047",
                      "variables": json.dumps({"content_filter": [], "count": 10, "cursor": cursor,
                                               "scale": 2, "use_case": "SAVE_DEFAULT"})}
            params.update({key: site[key] for key in ("__spin_r", "__spin_b", "__spin_t")})
            headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                       "Referer": "https://www.facebook.com/saved/", "Origin": "https://www.facebook.com",
                       "X-FB-LSD": lsd, "X-FB-Friendly-Name": operation, "X-ASBD-ID": "359341",
                       "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
            result = self.request("https://www.facebook.com/api/graphql/", headers, params)
            pages = [json.loads(line) for line in result.removeprefix("for (;;);").splitlines() if line.strip()]
            for page in pages:
                if page.get("error") or page.get("errors"):
                    raise ValueError(f"Facebook pagination rejected: code {page.get('error', 'GraphQL error')}")
            feed = connection(pages, "all_saves")
            nodes.extend(edge["node"] for edge in feed["edges"])
        self.has_more = len(nodes) > limit or bool(feed["page_info"]["has_next_page"])
        return nodes[:limit]

    def threads(self, limit):
        body = self.request("https://www.threads.com/saved")
        feed = connection(documents(body), "saved_media")
        if len(feed["edges"]) < limit and feed["page_info"]["has_next_page"]:
            raise ValueError("Threads needs another page; pagination replay is not verified yet")
        self.has_more = len(feed["edges"]) > limit or bool(feed["page_info"]["has_next_page"])
        return [edge["node"] for edge in feed["edges"][:limit]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("instagram", "facebook", "threads"))
    parser.add_argument("--cookies", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Private JSON output outside this repository")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("--output must be outside the repository")
    try:
        records = getattr(Client(args.cookies), args.platform)(args.limit)
        private_json(args.output, {"platform": args.platform, "records": records})
        print(json.dumps({"platform": args.platform, "count": len(records), "requested": args.limit,
                          "output": str(args.output), "transport": "plain HTTP"}))
    except (ValueError, OSError, KeyError, StopIteration) as error:
        # Never dump requests, headers, response bodies, or cookie values.
        print(f"Saved-feed probe failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
