"""Social post fetching: URL classification, page text extraction, and
media download (yt-dlp video / gallery-dl image fallback)."""
import hashlib
import html as html_mod
import http.cookiejar
import json
import logging
import re
import subprocess
import tempfile
from functools import partial
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from src import config as config_mod
from src import article, gemini, ingest, open_brain_bridge, state
from src import meta as meta_mod, note as note_mod, summarize
from src import finance_content
from src import local_media, saved_media, visuals

log = logging.getLogger("yt-obsidian")

CONTENT_KINDS = frozenset({"event", "article", "product", "place", "post", "video", "other"})
_EVENT_HOSTS = frozenset({"stubhub.com", "ticketmaster.com", "eventbrite.com", "axs.com"})
_SOCIAL_PLATFORMS = frozenset({"instagram", "facebook", "threads", "twitter"})


def source_type_for(platform: str) -> str:
    if platform == "youtube":
        return "youtube"
    if platform in _SOCIAL_PLATFORMS or platform == "social":
        return "social"
    return "web"


def content_kind_for(url: str, platform: str, model_value: str | None = None) -> str:
    if model_value in CONTENT_KINDS:
        return model_value
    if platform == "youtube":
        return "video"
    if platform in _SOCIAL_PLATFORMS:
        return "post"
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if any(host == event_host or host.endswith(f".{event_host}") for event_host in _EVENT_HOSTS):
        return "event"
    return "other"


def filing_route(model_meta: dict, content_kind: str) -> tuple[str, str]:
    if content_kind == "event":
        return "Personal", "Events & Experiences"
    return model_meta.get("topic", "Other"), model_meta.get("subtopic", "General")

class FetchError(Exception):
    pass

class _CountedFailure(Exception):
    """A failure raised after the item already spent its attempt for this cycle.
    The drain's catch-all reads the count off it instead of recording a second
    one, which would halve the effective retry budget."""
    def __init__(self, attempts: int):
        super().__init__(f"attempt {attempts} already recorded")
        self.attempts = attempts

# apex domain -> platform kind. Matched against the parsed hostname (exact or
# subdomain), never as a substring of the whole URL, so a redirect/shortener
# URL carrying a platform name in its path or query is never misclassified.
_HOST_TO_KIND = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "threads.net": "threads",
    "threads.com": "threads",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

def classify_social_url(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return "generic"
    for apex, kind in _HOST_TO_KIND.items():
        if host == apex or host.endswith("." + apex):
            return kind
    return "generic"

_ATTR_RE = re.compile(r'([a-zA-Z][\w:-]*)\s*=\s*(["\'])(.*?)\2')

def _meta_tags(body: str) -> list[dict]:
    tags = []
    for tag in re.findall(r"<meta\b[^>]*>", body, re.I):
        attrs = {m.group(1).lower(): m.group(3) for m in _ATTR_RE.finditer(tag)}
        tags.append(attrs)
    return tags

def _meta_content(tags: list[dict], *keys: str) -> str:
    # meta attribute order isn't guaranteed (content before or after
    # property/name), so tags are pre-parsed into dicts rather than matched
    # with an order-sensitive regex.
    for key in keys:
        for attrs in tags:
            name = attrs.get("property") or attrs.get("name")
            if name and name.lower() == key.lower() and "content" in attrs:
                return html_mod.unescape(attrs["content"]).strip()
    return ""

def _build_opener(cookies_path):
    jar = http.cookiejar.MozillaCookieJar()
    if cookies_path:
        try:
            jar.load(str(cookies_path), ignore_discard=True, ignore_expires=True)
        except OSError as e:
            log.warning("could not load cookies from %s: %s", cookies_path, e)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def fetch_page_text(url, cookies_path, opener=None) -> dict:
    """Last-resort text extraction from og:/meta tags. A browser-like
    User-Agent avoids block pages on these sites, and the fixed timeout
    keeps a hung request from stalling the poll job."""
    opener = opener or _build_opener(cookies_path)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with opener.open(req, timeout=30) as resp:
            body = resp.read()[:1_000_000].decode("utf-8", errors="replace")
    except Exception as e:
        raise FetchError(f"page fetch failed for {url}: {e}") from e
    tags = _meta_tags(body)
    return {
        "title": _meta_content(tags, "og:title", "twitter:title"),
        "text": _meta_content(tags, "og:description", "twitter:description", "description"),
        "author": _meta_content(tags, "author", "og:site_name"),
    }

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Bounds each yt-dlp/gallery-dl invocation so a hung CLI can't stall the
# 5-minute poll job forever; several of these calls may run per post.
_TOOL_TIMEOUT_S = 120

def _cookie_args(cookies_path) -> list[str]:
    return ["--cookies", str(cookies_path)] if cookies_path else []

def _fmt_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{8}", raw):                      # yt-dlp upload_date
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw[:10]

# suffixes yt-dlp (and similar tools) use for in-progress/partial downloads;
# these share the "media." prefix and are non-empty, so they must be excluded
# explicitly rather than relying on the size check alone.
_INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".temp", ".download"}

def _try_ytdlp(url, cookies_path, tmpdir, runner) -> dict | None:
    tmpdir = Path(tmpdir)
    meta_r = runner(["yt-dlp", "--dump-json", "--skip-download",
                     *_cookie_args(cookies_path), url],
                    capture_output=True, text=True, check=True, timeout=_TOOL_TIMEOUT_S)
    j = json.loads(meta_r.stdout.splitlines()[0])
    runner(["yt-dlp", "-f", "ba[ext=m4a]/ba/b", "--max-filesize", "19m",
            *_cookie_args(cookies_path), "-o", str(tmpdir / "media.%(ext)s"), url],
           capture_output=True, text=True, check=True, timeout=_TOOL_TIMEOUT_S)
    files = sorted(p for p in tmpdir.iterdir()
                   if p.name.startswith("media.") and p.stat().st_size > 0
                   and p.suffix not in _INCOMPLETE_SUFFIXES)
    if not files:
        return None                                       # e.g. over the size cap
    return {"kind": "video", "files": files[:1], "meta": {
        "title": j.get("title", ""), "author": j.get("uploader", "") or j.get("channel", ""),
        "caption": j.get("description", ""), "date": _fmt_date(j.get("upload_date", "")),
    }}

def _try_gallerydl(url, cookies_path, tmpdir, runner) -> dict | None:
    tmpdir = Path(tmpdir)
    runner(["gallery-dl", "--write-info-json", "-D", str(tmpdir),
            *_cookie_args(cookies_path), url],
           capture_output=True, text=True, check=True, timeout=_TOOL_TIMEOUT_S)
    images = sorted(p for p in tmpdir.iterdir() if p.suffix.lower() in _IMG_EXTS)
    if not images:
        return None
    meta = {"title": "", "author": "", "caption": "", "date": ""}
    infos = sorted(tmpdir.glob("*.json"))
    if infos:
        try:
            j = json.loads(infos[0].read_text())
            meta = {"title": j.get("title", ""),
                    "author": j.get("username", "") or j.get("uploader", "") or j.get("author", ""),
                    "caption": j.get("content", "") or j.get("description", ""),
                    "date": _fmt_date(str(j.get("date", "")))}
        except (json.JSONDecodeError, OSError):
            pass
    return {"kind": "images", "files": images[:8], "meta": meta}

def fetch_social_media(url, cookies_path, tmpdir, runner=subprocess.run) -> dict:
    """Try yt-dlp (video) first, then gallery-dl (images) as a fallback.
    Either tool may be missing (FileNotFoundError), fail outright
    (CalledProcessError), hang (TimeoutExpired), or return unparseable
    output (JSONDecodeError/IndexError) -- none of those may abort the
    attempt at the next tool. Returning kind="none" lets the caller fall
    back to page text instead of raising."""
    for attempt in (_try_ytdlp, _try_gallerydl):
        try:
            out = attempt(url, cookies_path, tmpdir, runner)
            if out:
                return out
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                 json.JSONDecodeError, OSError, IndexError) as e:
            log.info("%s failed for %s: %s", attempt.__name__, url, str(e)[:200])
    return {"kind": "none", "files": [],
            "meta": {"title": "", "author": "", "caption": "", "date": ""}}

def _default_deps(cfg=None):
    return SimpleNamespace(
        fetch_article=article.fetch_article,
        fetch_social_media=fetch_social_media,
        fetch_page_text=fetch_page_text,
        transcribe_audio=gemini.transcribe_audio,
        run_claude_social=partial(summarize.run_claude_social, sandbox=getattr(cfg,"model_sandbox",False)),
        fetch_saved_media=saved_media.fetch,
        extract_local_media=local_media.extract,
        parse_model_output=meta_mod.parse_model_output,
        build_social_note=note_mod.build_social_note,
        build_social_stub_note=note_mod.build_social_stub_note,
    )

_REFUSED_REASON = "This url was refused (private or non-http address); saved for manual review."
_TRUNCATED_FOR_MODEL = (f"[truncated for the model at {article.MAX_MODEL_CHARS:,} characters; "
                        "the note keeps the full text]")


def _payload(platform, url, meta, transcript, page, why, shared_text,
             article_text: str = "", article_meta: dict | None = None) -> str:
    parts = [f"## Platform\n{platform}", f"## URL\n{url}"]
    if why:
        parts.append(f"## Why saved\n{why}")
    if meta.get("title"):
        parts.append(f"## Title\n{meta['title']}")
    if article_meta and article_meta.get("site"):
        parts.append(f"## Site\n{article_meta['site']}")
    if article_meta and article_meta.get("date"):
        parts.append(f"## Date\n{article_meta['date']}")
    if meta.get("caption"):
        parts.append(f"## Caption\n{meta['caption']}")
    if transcript:
        parts.append(f"## Transcript\n{transcript}")
    if page and page.get("text"):
        parts.append(f"## Page text\n{page.get('title', '')}\n{page['text']}")
    if article_text:
        # untrusted page text, in the user payload exactly like transcripts
        section = f"## Source text\n{article_text}"
        if len((article_meta or {}).get("text") or "") > len(article_text):
            section += "\n\n" + _TRUNCATED_FOR_MODEL
        parts.append(section)
    if shared_text:
        parts.append(f"## Shared note\n{shared_text}")
    return "\n\n".join(parts)

def _write_social_note(cfg, post_meta, key, content, topic,
                       subtopic="General", processed="") -> Path:
    folder = note_mod.destination_dir(topic, subtopic,
                                      config_mod.allowed_topics(cfg),
                                      cfg.subtopics, cfg.content_root)
    out_dir = note_mod.safe_vault_dir(cfg.vault_dir, folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    title = post_meta.get("title") or (post_meta.get("caption") or "")[:60]
    fn = note_mod.content_filename(processed, title,
                                   post_meta.get("platform") or "generic")
    # keyed on url, not on the url digest the filename used to carry: a
    # redelivered item must rewrite its own note rather than make a second.
    # A url-less share (an invalid-stub item) has no url to key on at all, so
    # it keys on its own dedupe `key` instead, via the `item_key` frontmatter
    # field _write_stub already stamped into post_meta -- that satisfies both
    # halves of the constraint: the *same* url-less item redelivered carries
    # the same key and overwrites its own note, while two *different*
    # url-less items carry different keys and never collide onto one note.
    if post_meta.get("url"):
        identity, identity_key = post_meta["url"], "url"
    else:
        identity, identity_key = key, "item_key"
    if post_meta.get("_media_refresh") or post_meta.get("_private_media"):
        root = note_mod.safe_vault_dir(cfg.vault_dir, cfg.content_root)
        for existing in root.rglob("*.md"):
            if (not existing.is_symlink() and existing.resolve().is_relative_to(root.resolve())
                    and note_mod._frontmatter_value(existing,"url") == identity):
                note_mod.write_private_note(existing, content)
                return existing
    path = note_mod.resolve_collision(out_dir / fn, identity, identity_key)
    if post_meta.get("_sure_context_added") or post_meta.get("_private_media"):
        note_mod.write_private_note(path, content)
        return path
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(path)
    return path

def _write_stub(cfg, post_meta, key, status, shared_text, processed, deps, reason="") -> str:
    """Terminal note for a share that will never be summarized. The note is
    written before mark_seen, so a crash in between redelivers the item rather
    than losing it. A write failure here propagates: the item stays queued,
    because dropping it would be the one outcome that loses the share.

    No model output ran for this item. Deterministic content kinds such as an
    event still retain their known filing route; everything else falls back
    to Other/General. A non-empty reason replaces the status text in the body."""
    # stamped on every stub, not just the url-less ones: harmless when a real
    # url is also present (identity still keys on url there), and it is what
    # lets _write_social_note key a url-less item on itself.
    post_meta = {**post_meta, "item_key": key}
    if reason:
        post_meta["stub_reason"] = reason
    stub = deps.build_social_stub_note(post_meta, status, shared_text, processed)
    topic, subtopic = filing_route({}, post_meta.get("content_kind", "other"))
    _write_social_note(cfg, post_meta, key, stub, topic, subtopic, processed)
    state.mark_seen(cfg.state_path, key)
    return status

def _bare_post_meta(url: str) -> dict:
    """Stub metadata for an item that never got far enough to have any."""
    platform = classify_social_url(url) if url else "social"
    return {"platform": platform, "source_type": source_type_for(platform),
            "content_kind": content_kind_for(url, platform), "url": url or "",
            "author": "", "title": "", "caption": "", "date": "", "kind": "text"}

def _capture_fields(item) -> dict:
    return {"capture_why": item.get("why", ""),
            "capture_context_at": item.get("received_at", ""),
            "capture_source": "saved_feed" if item.get("capture_source") == "saved_feed" else "share"}


def _article_fields(art: dict) -> dict:
    """post_meta keys that switch on the capture frontmatter and name the
    reason a thin note gives."""
    return {"extraction": art["extraction"], "word_count": art["word_count"],
            "source_format": art["source_format"], "extraction_reason": art["reason"]}


def _article_fetch_failed(cfg, item, url, key, shared_text, processed, deps, err) -> str:
    """The retry policy of an empty media fetch, without the media tools:
    yt-dlp against a host that just timed out burns minutes for nothing."""
    attempts = state.record_attempt(cfg.state_path, key)
    log.warning("article fetch failed for %s (attempt %d): %s", url, attempts, err)
    if attempts < cfg.max_attempts:
        return "retry"
    post_meta = {**_bare_post_meta(url), **_capture_fields(item)}
    _emit_saved_item(cfg, item, post_meta, key, "failed")
    try:
        return _write_stub(cfg, post_meta, key, "fetch-failed", shared_text, processed, deps)
    except Exception as stub_err:    # still propagates, but the
        raise _CountedFailure(attempts) from stub_err   # attempt is spent

def _invalid_stub(cfg, item, url, processed, deps) -> str:
    # no url means no url_key, so the note is keyed by the item itself: a
    # redelivered batch then rewrites the same note instead of piling up copies.
    blob = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
    # surrogatepass: a queue line carrying a lone surrogate (e.g. a JSON \ud800
    # escape) must not raise on the error path -- that would escape the drain,
    # skip finish_queue, and wedge the queue the same way the last commit fixed.
    key = "item:" + hashlib.sha1(blob.encode(errors="surrogatepass")).hexdigest()
    # both the sharer's own note and the raw item are kept, not either/or: the
    # malformed url lives only inside the blob's json, so dropping it whenever
    # shared text is also present would lose the one thing the user shared.
    body = "\n\n".join(part for part in (item.get("text"), blob) if part)
    raw_url = url if isinstance(url, str) else ""
    return _write_stub(cfg, _bare_post_meta(raw_url), key, "invalid",
                       body, processed, deps)

_DEFAULT_SAVED_WHY = "Saved from the share sheet."


def _emit_saved_item(cfg, item, post_meta, key, fetch_status, *, summary=None,
                     body="", media_text="") -> bool:
    """Write a terminal saved item without affecting the share outcome."""
    try:
        if cfg.open_brain_saved_spool_dir is None:
            return False
        url = item.get("url") if isinstance(item.get("url"), str) else ""
        if not url.startswith("https://"):
            log.info("no saved item for a non-https url: %.120s", url)
            return False
        platform = post_meta.get("platform")
        open_brain_bridge.write_saved_item(
            cfg.open_brain_saved_spool_dir,
            key=key, url=url,
            why=(item.get("why") or "").strip() or _DEFAULT_SAVED_WHY,
            received_at=item.get("received_at") or datetime.now(timezone.utc).isoformat(),
            fetch_status=fetch_status,
            source_type=post_meta.get("source_type"),
            content_kind=post_meta.get("content_kind"),
            platform=None if platform in (None, "", "generic", "social") else platform,
            title=post_meta.get("title") or None,
            author=post_meta.get("author") or None,
            body=body, media_text=media_text, shared_text=item.get("text") or "",
            summary=summary,
        )
        return True
    except Exception as e:
        try:
            log.error("saved item not written: %s", e)
        except Exception:
            pass
        return False


def _emit_fetched_saved_item(cfg, item, post_meta, key, media, page, transcript, *, summary=None,
                             article_text=""):
    try:
        if cfg.open_brain_saved_spool_dir is None:
            return False
        body = article_text or "\n\n".join(
            part for part in (media["meta"].get("caption") or "",
                              (page or {}).get("text") or "") if part)
        fetch_status = ("partial" if media["kind"] == "video" and transcript is None
                        else "complete")
        return _emit_saved_item(cfg, item, post_meta, key, fetch_status,
                                summary=summary, body=body, media_text=transcript or "")
    except Exception as e:
        try:
            log.error("saved item not written: %s", e)
        except Exception:
            pass
        return False


def process_social_item(cfg, project_root, item, processed, deps, key, prompt_text) -> str:
    """Turn one queue item into a note. Every exit either writes a note or
    returns "retry"/"duplicate" — a share is never dropped on the floor. The
    dedupe key and the prompt text come from the drain: parsing the url here
    too would give the error path a second chance to raise, and re-reading the
    prompt per item turns one deploy fault into one failure per share."""
    url = item["url"]
    loaded = state.load_state(cfg.state_path)
    refresh = bool(item.get("media_refresh") and getattr(cfg,"free_media_enabled",False))
    if state.is_seen(loaded, key) and (not refresh or key in loaded.get("media_processed",[])):
        return "duplicate"                   # a crashed drain redelivers the batch
    free = getattr(cfg,"free_media_enabled",False)
    if item.get("transcription_policy") == "free_only" and item.get("saved_content_kind") == "video" and not free:
        return "waiting-free-transcription"
    platform = classify_social_url(url)
    shared_text = item.get("text")
    why = item.get("why", "")

    # Generic urls try the article reader first. A refused address or a dead
    # host ends here without the media tools; an unsupported type (None) or a
    # partial read falls through to them, in case the page is mostly a video.
    art = None
    if platform == "generic":
        try:
            art = deps.fetch_article(url)
        except article.BlockedURL as e:
            log.warning("refused %s: %s", url, e)
            return _write_stub(cfg, {**_bare_post_meta(url), **_capture_fields(item)}, key,
                               "invalid", shared_text, processed, deps, reason=_REFUSED_REASON)
        except article.FetchError as e:
            return _article_fetch_failed(cfg, item, url, key, shared_text, processed, deps, e)
    text_path = art is not None and art["extraction"] == "full"
    article_text = art["text"] if text_path else ""

    with tempfile.TemporaryDirectory() as td:   # media lives here; claude reads
        if text_path:                            # the article is the content
            media = {"kind": "none", "files": [], "meta": {
                "title": art["title"], "author": art["author"], "caption": "",
                "date": art["date"]}}
        elif free and item.get("capture_source") == "saved_feed":
            media = deps.fetch_saved_media(url, cfg.cookies_path, td)
        else:
            media = deps.fetch_social_media(url, cfg.cookies_path, td)
        # Captions obtained from the authenticated saved feed survive extractor
        # failures; they are source text, never the owner's capture intent.
        if item.get("capture_source") == "saved_feed":
            for field, saved_field in (("caption", "saved_caption"), ("author", "saved_author")):
                if not media["meta"].get(field):
                    media["meta"][field] = item.get(saved_field) or ""
        transcript, page, images = None, None, []
        extraction = []
        if media["kind"] == "video":
            if item.get("transcription_policy") == "free_only" and not free:
                item["saved_content_kind"] = "video"
                return "waiting-free-transcription"
            if free:
                if len(media["files"]) > 3:
                    raise FetchError("Post exceeds three video attachments; manual review required")
                texts=[]
                images=list(media.get("images",[]))
                for number,path in enumerate(media["files"],1):
                    directory=Path(td)/f"clip-{number}"
                    directory.mkdir()
                    result=deps.extract_local_media(path,directory)
                    if result["transcript"]:
                        texts.append(f"### Clip {number}\n{result['transcript']}")
                    images.extend(result["images"])
                    extraction.append({k:v for k,v in result.items() if k not in ("images","transcript")})
                transcript="\n\n".join(texts) or None
            else:
                try:
                    transcript = deps.transcribe_audio(media["files"][0],
                                                       cfg.gemini_api_key, cfg.gemini_model)
                except gemini.TranscribeError as e:
                    log.warning("transcription failed for %s: %s", url, e)
        elif media["kind"] == "images":
            images = media["files"]
        if not text_path and (media["kind"] == "none"
                              or (media["kind"] == "video" and transcript is None)):
            try:
                page = deps.fetch_page_text(url, cfg.cookies_path)
            except FetchError as e:
                log.info("page fetch failed for %s: %s", url, e)

        post_meta = {
            "platform": platform, "url": url,
            "source_type": source_type_for(platform),
            "content_kind": content_kind_for(url, platform),
            "author": media["meta"].get("author") or (page or {}).get("author", ""),
            "title": media["meta"].get("title") or (page or {}).get("title", ""),
            "caption": media["meta"].get("caption") or "",
            "date": media["meta"].get("date") or "",
            "kind": media["kind"] if media["kind"] != "none" else "text",
            "capture_why": why,
            "capture_context_at": item.get("received_at", ""),
            "capture_source": "saved_feed" if item.get("capture_source") == "saved_feed" else "share",
            "_media_refresh": refresh,
            "_private_media": free,
        }
        if art is not None:
            post_meta.update(_article_fields(art))
            for field in ("title", "author", "date"):
                post_meta[field] = post_meta[field] or art[field]
        if text_path:
            post_meta["content_kind"] = "article"       # pre-model default
        # the sharer's own note deliberately does not count as content: summarizing
        # it alone would produce a note about a post nothing ever managed to read.
        # The stub keeps that text, so it is preserved either way.
        has_content = text_path or bool(transcript or images or media["meta"].get("caption")
                                        or (page and page.get("text")))
        if not has_content and art is not None:
            # a partial article and nothing from the media tools: a thin note
            # now. Retrying would only fetch the same paywall or teaser again.
            _emit_saved_item(cfg, item, post_meta, key, "partial", body=art["text"])
            return _write_stub(cfg, post_meta, key, "partial", shared_text, processed, deps)
        if not has_content:                   # fetch failure → same retry policy
            attempts = state.record_attempt(cfg.state_path, key)  # as the claude path below
            log.warning("fetch failed for %s (attempt %d)", url, attempts)
            if attempts >= cfg.max_attempts:
                _emit_saved_item(cfg, item, post_meta, key, "failed")
                try:
                    return _write_stub(cfg, post_meta, key, "fetch-failed",
                                       shared_text, processed, deps)
                except Exception as stub_err:    # still propagates, but the
                    raise _CountedFailure(attempts) from stub_err   # attempt is spent
            return "retry"

        payload = _payload(platform, url, media["meta"], transcript, page, why, shared_text,
                           article_text=article_text[:article.MAX_MODEL_CHARS],
                           article_meta=art)
        try:
            observations=[]
            if free and images:
                observations=visuals.observe(images,cfg,project_root,deps.run_claude_social)
                payload += "\n\n## Media observations (source data)\n" + visuals.render(observations)
            if extraction:
                payload += "\n\n## Extraction coverage\n" + json.dumps(extraction)
            raw_out = deps.run_claude_social(prompt_text, payload, images,
                                             cfg.model, project_root) if not free else deps.run_claude_social(prompt_text,payload,[],cfg.model,project_root)
        except Exception as e:               # claude failure → retry policy
            attempts = state.record_attempt(cfg.state_path, key)
            log.warning("social summarize failed for %s (attempt %d): %s", url, attempts, e)
            if attempts >= cfg.max_attempts:
                _emit_fetched_saved_item(cfg, item, post_meta, key, media, page, transcript,
                                         article_text=article_text)
                try:
                    return _write_stub(cfg, post_meta, key, "summary-failed",
                                       shared_text, processed, deps)
                except Exception as stub_err:    # still propagates, but the
                    raise _CountedFailure(attempts) from stub_err   # attempt is spent
            return "retry"

    model_meta, body = deps.parse_model_output(raw_out)
    model_kind = model_meta.get("content_kind")
    content_kind = content_kind_for(url, platform, model_kind)
    if text_path and model_kind not in CONTENT_KINDS and content_kind == "other":
        content_kind = "article"             # the text path's default; event hosts keep "event"
    post_meta["content_kind"] = content_kind
    private_body = finance_content.annotate(cfg, model_meta, body, payload, key)
    if private_body != body:
        post_meta["_sure_context_added"] = True
    note_extra = {"source_text": article_text} if text_path else {}
    content = deps.build_social_note(post_meta, model_meta, private_body, transcript, processed,
                                     **note_extra)
    if free:
        if observations:
            content += "\n\n## Source image/frame observations\n\n" + visuals.render(observations) + "\n"
        if extraction:
            content += "\n\n## Media extraction coverage\n\nEight evenly spaced frames per clip; intervals between frames were not inspected.\n\n" + json.dumps(extraction,ensure_ascii=False) + "\n"
    # a crash between this write and mark_seen redelivers the item; the filename
    # leads with the processed date, so that only holds within one calendar
    # day -- a same-day rerun landing on the same topic rewrites this note,
    # but one landing on a different topic, or on a later day, leaves a second
    # copy behind.
    topic, subtopic = filing_route(model_meta, content_kind)
    _write_social_note(cfg, post_meta, key, content, topic, subtopic, processed)
    _emit_fetched_saved_item(cfg, item, post_meta, key, media, page, transcript,
                             summary=body, article_text=article_text)
    state.mark_seen(cfg.state_path, key)
    if free:
        state.mark_media_processed(cfg.state_path,key)
    return "summarized"

def _item_key(url) -> str | None:
    """The dedupe key, or None for anything that cannot have one. url_key parses
    the url, and the ingest server only checks for scheme://rest, so inputs it
    accepts (an unclosed IPv6 host, a homoglyph netloc) raise here on 3.14+."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return ingest.url_key(url)
    except ValueError:
        return None

def _count_attempt(cfg, key, err) -> tuple[int, BaseException]:
    """One attempt per item per cycle: a failure raised after the item already
    recorded one carries that count instead of earning a second."""
    if isinstance(err, _CountedFailure):
        return err.attempts, err.__cause__ or err
    return state.record_attempt(cfg.state_path, key), err

def drain_social_queue(cfg, project_root, processed, deps=None) -> list:
    """Process one rotated batch. The queue lock is not re-entrant, so every
    rotate/append/finish call here sits outside the others."""
    deps = deps or _default_deps(cfg)
    # read once, before anything is rotated: a missing or mistyped prompt path is
    # a deploy fault, not an item fault. Letting it propagate here leaves the
    # whole batch queued to self-heal on the next cycle, where absorbing it per
    # item would spend a transcription on each share and stub them all out.
    prompt_text = Path(project_root, cfg.social_prompt_path).read_text()
    # rotate_queue raises on read errors by design; letting that propagate skips
    # finish_queue, so an unprocessed batch survives to the next run.
    items = ingest.rotate_queue(cfg.queue_path)
    results = []
    limit=getattr(cfg,"social_batch_size",0) or len(items)
    for position,item in enumerate(items):
        if position >= limit:
            ingest.append_item(cfg.queue_path,item)
            continue
        url = item.get("url")
        # computed once, guarded, before any dispatch: an unparseable url must
        # not be re-parsed on the error path below, where raising would escape
        # the drain, skip finish_queue and wedge the queue permanently.
        key = _item_key(url)
        if key is None:
            # nothing to fetch, and re-queueing it would poison every later
            # drain — but the text the sharer typed still has to land somewhere.
            log.error("queue item without a usable url, filing a stub: %.120s", item)
            label = url if isinstance(url, str) and url.strip() else "(no url)"
            results.append((label, _invalid_stub(cfg, item, url, processed, deps)))
            continue
        try:                                 # one bad item may not abort the batch
            outcome = process_social_item(cfg, project_root, item, processed, deps,
                                          key, prompt_text)
        except Exception as e:
            # anything the item-level handlers did not cover. Recording the
            # attempt is what bounds it: without that the item never reaches
            # max_attempts and re-runs the whole pipeline every cycle forever.
            attempts, e = _count_attempt(cfg, key, e)
            log.error("unexpected error on %s (attempt %d): %s", url, attempts, e)
            if attempts >= cfg.max_attempts:
                bare = _bare_post_meta(url)
                _emit_saved_item(cfg, item, bare, key, "failed")
                outcome = _write_stub(cfg, bare, key, "summary-failed",
                                      item.get("text"), processed, deps)
            else:
                outcome = "retry"
        if outcome in ("retry", "waiting-free-transcription"):
            # back onto the live queue, which this run already rotated away from,
            # so it is not re-read here. Done before finish_queue: a crash in
            # between redelivers the item rather than dropping it.
            ingest.append_item(cfg.queue_path, item)
        results.append((url, outcome))
    ingest.finish_queue(cfg.queue_path)      # only once the whole batch is durable
    return results
