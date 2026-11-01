"""Unprivileged data-only client for the optional Linux model launcher."""
import base64
import json
import os
from pathlib import Path
import stat
import subprocess

HELPER = '/usr/local/sbin/yt-model-sandbox'


def available():
    try:
        info = Path(HELPER).lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and os.access(HELPER, os.X_OK)
    except OSError:
        return False


def run(prompt, payload, model, image_paths=(), *, runner=subprocess.run):
    if len(image_paths) > 8:
        raise ValueError('At most eight images can be summarized')
    images, total = [], 0
    for path in image_paths:
        path = Path(path)
        fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 20*1024*1024:
                raise ValueError('Invalid model image input')
            data = stream.read(20*1024*1024+1)
        total += len(data)
        if total > 32*1024*1024:
            raise ValueError('Model images exceed 32 MiB')
        images.append({'extension':path.suffix.lower(),'base64':base64.b64encode(data).decode()})
    request = {'version':1,'operation':'summarize','model':model,'prompt':prompt,'payload':payload,'images':images}
    if len(prompt.encode()) > 2*1024*1024 or len(payload.encode()) > 2*1024*1024:
        raise ValueError('Model text exceeds sandbox limit')
    result = runner(['/usr/bin/sudo','-n',HELPER],input=json.dumps(request),text=True,
                    capture_output=True,cwd='/',env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                    timeout=200,close_fds=True)
    if result.returncode:
        raise RuntimeError('Model sandbox failed; no unsandboxed fallback is permitted')
    try:
        response = json.loads(result.stdout)
        if response.get('ok') is not True or not isinstance(response.get('output'),str):
            raise ValueError()
        return response['output']
    except (ValueError,AttributeError):
        raise RuntimeError('Invalid model sandbox response') from None
