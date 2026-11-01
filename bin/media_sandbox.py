#!/usr/bin/python3 -I
"""Fixed root launcher. Accept media bytes only; no commands, paths or network."""
import base64
import json
import os
from pathlib import Path
import resource
import signal
import stat
import subprocess
import sys
import tempfile

ROOT = Path('/opt/youtube-obsidian/media')
CONFIG = Path('/etc/youtube-obsidian/media-sandbox.json')
MAX_INPUT = 214 * 1024 * 1024


def command(work, output, probe=False):
    cmd = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--uid', '65534', '--gid', '65534',
           '--disable-userns', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
           '--ro-bind', '/usr/bin', '/usr/bin', '--ro-bind', '/usr/lib', '/usr/lib',
           '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib',
           '--symlink', 'usr/lib64', '/lib64', '--ro-bind', str(ROOT/'site-packages'), '/runtime',
           '--ro-bind', str(ROOT/'model'), '/model', '--ro-bind', str(ROOT/'worker.py'), '/worker.py',
           '--ro-bind', str(work), '/input', '--bind', str(output), '/output',
           '--proc', '/proc', '--dev', '/dev', '--size', str(256*1024*1024), '--tmpfs', '/tmp', '--chdir', '/output']
    for path in ('/usr/lib64', '/etc/ld.so.cache', '/etc/alternatives'):
        if Path(path).exists():
            cmd += ['--ro-bind', path, path]
    return cmd + ['--', '/usr/bin/python3', '-I', '/worker.py'] + (['probe'] if probe else [])


def limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64*1024*1024, 64*1024*1024))
    resource.setrlimit(resource.RLIMIT_AS, (8*1024*1024*1024, 8*1024*1024*1024))


def execute(request):
    if os.geteuid() != 0:
        raise ValueError('identity')
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4096:
            raise ValueError('install-config')
        settings=json.load(stream)
    if set(settings)!={'operator_uid'} or type(settings['operator_uid']) is not int or settings['operator_uid']<=0:
        raise ValueError('install-config')
    if os.environ.get('SUDO_UID') not in (str(settings['operator_uid']), '0'):
        raise ValueError('identity')
    if not isinstance(request, dict) or set(request) != {'version', 'operation', 'base64'} or request['version'] != 1:
        raise ValueError('request')
    if request['operation'] not in ('extract', 'probe'):
        raise ValueError('operation')
    data = base64.b64decode(request['base64'], validate=True)
    if len(data) > 150*1024*1024 or (not data and request['operation'] != 'probe'):
        raise ValueError('media-limit')
    for path in (ROOT, ROOT/'site-packages', ROOT/'model', ROOT/'worker.py', Path('/usr/bin/bwrap')):
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('runtime-integrity')
    with tempfile.TemporaryDirectory(prefix='yt-media-') as td:
        work, output = Path(td)/'input', Path(td)/'output'
        work.mkdir(); output.mkdir()
        (work/'media').write_bytes(data)
        cmd = command(work, output, request['operation'] == 'probe')
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/tmp', 'LANG': 'C.UTF-8', 'HF_HUB_OFFLINE': '1',
               'OMP_NUM_THREADS': '4', 'OPENBLAS_NUM_THREADS': '4'}
        with tempfile.TemporaryFile() as stream:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.DEVNULL,
                                    env=env, cwd='/', close_fds=True, start_new_session=True, preexec_fn=limits)
            try:
                proc.wait(timeout=600)
            except BaseException:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait(); raise
            if proc.returncode:
                raise ValueError('worker-failed')
            stream.seek(0)
            raw = stream.read(12*1024*1024+1)
            if len(raw) > 12*1024*1024:
                raise ValueError('output-limit')
            return json.loads(raw)


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError('arguments')
        raw = sys.stdin.buffer.read(MAX_INPUT+1)
        if len(raw) > MAX_INPUT:
            raise ValueError('input-limit')
        result = {'ok': True, 'output': execute(json.loads(raw))}
    except Exception:
        result = {'ok': False, 'error': 'media-sandbox-failed'}
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
