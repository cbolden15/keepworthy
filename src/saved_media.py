"""Authenticated Meta metadata plus credential-free bounded CDN downloads."""
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request

from src.saved_feeds import Client, HEADERS, documents, objects


def cdn_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port
            or not any(host.endswith('.'+domain) or host == domain for domain in ('fbcdn.net','cdninstagram.com'))):
        raise ValueError('Media URL is outside Meta CDN')
    return url


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        cdn_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, path, limit=150*1024*1024):
    # No cookie jar, auth headers, environment proxy, or model in this path.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), CDNRedirect())
    request = urllib.request.Request(cdn_url(url), headers={'User-Agent': HEADERS['User-Agent']})
    total = 0
    deadline=time.monotonic()+120
    with opener.open(request, timeout=30) as response, Path(path).open('wb') as output:
        while chunk := response.read(1024*1024):
            if time.monotonic()>deadline:
                raise ValueError('Media download exceeded time budget')
            total += len(chunk)
            if total > limit:
                raise ValueError('Media exceeds download limit')
            output.write(chunk)
    Path(path).chmod(0o600)
    if not total:
        raise ValueError('Empty media download')


def instagram_id(code):
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',code):
        raise ValueError('Invalid media code')
    result = 0
    for char in code:
        result = result*64 + alphabet.index(char)
    return str(result)


def _ig_assets(media):
    assets = []
    for part in media.get('carousel_media') or [media]:
        if part.get('video_versions'):
            variants = sorted(part['video_versions'], key=lambda v:abs(v.get('width',0)-720))
            # Progressive media contains both audio and video; do not fetch an
            # audio-only track and lose visual instructions.
            assets.append(('video', variants[0]['url']))
        elif (part.get('image_versions2') or {}).get('candidates'):
            candidates = part['image_versions2']['candidates']
            image = min(candidates, key=lambda v:abs(v.get('width',0)-1080))
            assets.append(('image', image['url']))
    return assets


def metadata(url, cookies):
    c = Client(cookies)
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path.strip('/').split('/')
    if host in ('www.instagram.com','instagram.com') and len(path)==2 and path[0] in ('p','reel','reels'):
        code = path[1]
        data = json.loads(c.request(f'https://www.instagram.com/api/v1/media/{instagram_id(code)}/info/',
                         {**HEADERS,'X-IG-App-ID':'936619743392459'}))
        media = next(m for m in data['items'] if m.get('code') == code)
        return _ig_assets(media)
    if host in ('www.threads.com','threads.com','www.threads.net','threads.net') and len(path)==3 and path[1]=='post':
        docs = list(documents(c.request(url)))
        media = next(o for doc in docs for o in objects(doc)
                     if o.get('code') == path[2] and 'media_type' in o and 'image_versions2' in o)
        return _ig_assets(media)
    if host in ('www.facebook.com','facebook.com','m.facebook.com'):
        video_id = (parse_qs(parts.query).get('v') or [None])[0]
        if path and path[0]=='reel' and len(path)==2:
            video_id = path[1]
        docs = list(documents(c.request(url)))
        if video_id:
            nodes = [o for d in docs for o in objects(d) if o.get('id') == video_id]
            for node in nodes:
                for obj in objects(node):
                    if obj.get('progressive_urls'):
                        variants = obj['progressive_urls']
                        chosen = next((v for v in variants if v.get('metadata',{}).get('quality')=='HD' and v.get('progressive_url')),None)
                        chosen = chosen or next((v for v in variants if v.get('progressive_url')),None)
                        if chosen:return [('video',chosen['progressive_url'])]
                    legacy = obj.get('videoDeliveryLegacyFields') or {}
                    if legacy.get('browser_native_sd_url') or legacy.get('browser_native_hd_url'):
                        return [('video',legacy.get('browser_native_sd_url') or legacy['browser_native_hd_url'])]
            raise ValueError('Matching Facebook video media unavailable')
        # Photo attachments in the source story, excluding profile pictures and
        # unrelated feed thumbnails. Every Photo node carries its own image.
        assets = {}
        for d in docs:
            for story in objects(d):
                if (story.get('__typename') != 'Story' or
                        urlsplit(story.get('permalink_url') or '').path.rstrip('/') != parts.path.rstrip('/')):
                    continue
                for obj in objects(story.get('attachments') or []):
                    if obj.get('__typename')=='Photo':
                        for field in ('image','photo_image','viewer_image'):
                            image = obj.get(field)
                            if isinstance(image,dict) and image.get('uri'):
                                assets.setdefault(obj['id'],('image',image['uri']));break
        if not assets:
            raise ValueError('Matching Facebook photo attachments unavailable')
        return list(assets.values())
    raise ValueError('Unsupported saved media source')


def fetch(url, cookies, directory):
    assets = metadata(url,cookies)
    if len(assets)>20:
        raise ValueError('Post exceeds 20 media attachments')
    files, images = [], []
    deadline=time.monotonic()+300
    total=0
    for i,(kind,url) in enumerate(assets):
        if time.monotonic()>deadline:
            raise ValueError('Post exceeded download time budget')
        path = Path(directory)/f'saved-{i}.{"mp4" if kind=="video" else "jpg"}'
        download(url,path,150*1024*1024 if kind=='video' else 20*1024*1024)
        total+=path.stat().st_size
        if total>200*1024*1024:
            raise ValueError('Post exceeds 200 MiB')
        if kind=='image':
            data=path.read_bytes()
            ext = '.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else '.webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else '.jpg'
            if ext=='.jpg' and not data.startswith(b'\xff\xd8\xff'):
                raise ValueError('Unsupported saved image encoding')
            target=path.with_suffix(ext);path.rename(target);images.append(target)
        else:files.append(path)
    return {'kind':'video' if files else 'images' if images else 'none', 'files':files or images,
            'images':images,'meta':{'title':'','author':'','caption':'','date':''}}
