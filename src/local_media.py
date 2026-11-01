"""Data-only client for free, offline transcription and sampled video frames."""
import base64
import json
import os
from pathlib import Path
import stat
import subprocess

HELPER = '/usr/local/sbin/yt-media-sandbox'


def available():
    try:
        info=Path(HELPER).lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022 and os.access(HELPER,os.X_OK)
    except OSError:
        return False


def extract(path, directory, runner=subprocess.run):
    path = Path(path)
    if not 0 < path.stat().st_size <= 150*1024*1024:
        raise ValueError('Media exceeds 150 MiB')
    request = {'version': 1, 'operation': 'extract', 'base64': base64.b64encode(path.read_bytes()).decode()}
    proc = runner(['/usr/bin/sudo', '-n', HELPER], input=json.dumps(request), text=True,
                  capture_output=True, cwd='/', env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                  close_fds=True, timeout=630)
    if proc.returncode:
        raise RuntimeError('Offline media worker failed; paid fallback is disabled')
    result = json.loads(proc.stdout)
    if result.get('ok') is not True or not isinstance(result.get('output'), dict):
        raise RuntimeError('Invalid offline media response')
    output = result['output']
    frames = output.pop('frames')
    if len(frames) > 8 or not isinstance(output.get('transcript'), str):
        raise ValueError('Invalid media response shape')
    paths, stamps = [], []
    for i, frame in enumerate(frames):
        data = base64.b64decode(frame['base64'], validate=True)
        if not data.startswith(b'\xff\xd8\xff') or len(data) > 2*1024*1024:
            raise ValueError('Invalid extracted frame')
        target = Path(directory)/f'video-frame-{i}.jpg'
        target.write_bytes(data); target.chmod(0o600)
        paths.append(target); stamps.append(frame['timestamp'])
    return {**output, 'images': paths, 'frame_timestamps': stamps}
