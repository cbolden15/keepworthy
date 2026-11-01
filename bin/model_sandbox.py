#!/usr/bin/python3 -I
"""Optional root-owned Linux launcher: data on stdin, no caller-controlled paths."""
import base64
import json
import os
from pathlib import Path
import resource
import shlex
import signal
import stat
import subprocess
import sys
import tempfile

BINARY = Path('/opt/youtube-obsidian/claude')
BWRAP = Path('/usr/bin/bwrap')
AUTH = Path('/var/lib/youtube-obsidian/claude-oauth.env')
CONFIG = Path('/etc/youtube-obsidian/model-sandbox.json')
OPERATOR_UID = None
MAX_INPUT, MAX_OUTPUT = 48 * 1024 * 1024, 8 * 1024 * 1024
PROBE = '''
import json, os, pathlib, subprocess, sys
paths=json.loads(sys.stdin.read())
assert not any(pathlib.Path(p).exists() for p in paths), 'private-path-visible'
images=list(pathlib.Path('/work').glob('image-*'))
assert all(p.is_file() and p.read_bytes() for p in images), 'media-unreadable'
assert not any(k in os.environ for k in ('SURE_API_KEY','GEMINI_API_KEY','ANTHROPIC_API_KEY','SSH_AUTH_SOCK')), 'private-env-visible'
result=subprocess.run(['/opt/claude','--safe-mode','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--disable-slash-commands','--no-session-persistence','--permission-mode','dontAsk','--tools','Read','--allowedTools','Read(//work/**)','--version'],capture_output=True,text=True,timeout=20)
assert result.returncode==0, 'claude-runtime-failed'
auth=subprocess.run(['/opt/claude','auth','status'],capture_output=True,text=True,timeout=20)
status=json.loads(auth.stdout)
assert status.get('loggedIn') is True, 'login-required'
print(json.dumps({'blocked_paths':len(paths),'readable_images':len(images),'claude_version':result.stdout.strip(),'authenticated':True,'auth_method':status.get('authMethod'),'subscription_type':status.get('subscriptionType')}))
'''


def check(condition, code):
    if not condition:
        raise ValueError(code)


def configure():
    global BINARY, AUTH, OPERATOR_UID
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and info.st_size <= 4096, 'install-config')
        settings = json.load(stream)
    check(set(settings) == {'binary','auth_file','operator_uid'}, 'install-config')
    check(type(settings['operator_uid']) is int and settings['operator_uid'] > 0, 'operator-uid')
    check(all(isinstance(settings[k],str) and Path(settings[k]).is_absolute() for k in ('binary','auth_file')), 'install-path')
    BINARY, AUTH, OPERATOR_UID = Path(settings['binary']), Path(settings['auth_file']), settings['operator_uid']


def validate(request):
    check(isinstance(request, dict) and set(request) <= {'version','operation','model','prompt','payload','images','probe_paths'}, 'request-fields')
    check(type(request.get('version')) is int and request['version'] == 1, 'request-version')
    check(request.get('operation') in {'summarize','probe'}, 'request-operation')
    for key in ('prompt','payload'):
        check(isinstance(request.get(key), str) and len(request[key].encode()) <= 2 * 1024 * 1024, 'text-limit')
    check(request.get('model') in {'sonnet','haiku','opus'}, 'model-not-approved')
    images = request.get('images')
    check(isinstance(images, list) and len(images) <= 8, 'image-limit')
    decoded, total = [], 0
    for image in images:
        check(isinstance(image, dict) and set(image) == {'extension','base64'}, 'image-fields')
        extension = image['extension']
        check(extension in {'.png','.jpg','.jpeg','.webp'}, 'image-extension')
        check(isinstance(image['base64'], str) and len(image['base64']) <= 28 * 1024 * 1024, 'image-limit')
        data = base64.b64decode(image['base64'], validate=True)
        total += len(data)
        check(0 < len(data) <= 20 * 1024 * 1024 and total <= 32 * 1024 * 1024, 'image-limit')
        valid = (data.startswith(b'\x89PNG\r\n\x1a\n') if extension == '.png' else
                 data.startswith(b'\xff\xd8\xff') if extension in {'.jpg','.jpeg'} else
                 data.startswith(b'RIFF') and data[8:12] == b'WEBP')
        check(valid, 'image-format')
        decoded.append((extension, data))
    paths = request.get('probe_paths', [])
    check(isinstance(paths,list) and len(paths) <= 32 and all(isinstance(p,str) and p.startswith('/') and len(p) <= 1024 for p in paths), 'probe-paths')
    check(request['operation'] == 'probe' or not paths, 'probe-only')
    return decoded


def command(work):
    cmd = [str(BWRAP), '--unshare-all','--unshare-user','--uid','65534','--gid','65534',
           '--share-net','--disable-userns','--die-with-parent','--new-session','--cap-drop','ALL',
           '--ro-bind','/usr/bin','/usr/bin','--ro-bind','/usr/lib','/usr/lib',
           '--symlink','usr/bin','/bin',
           '--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64',
           '--ro-bind',str(BINARY),'/opt/claude','--ro-bind',str(work),'/work',
           '--ro-bind',str(work.parent / 'passwd'),'/etc/passwd',
           '--ro-bind',str(work.parent / 'group'),'/etc/group',
           '--proc','/proc','--dev','/dev','--size',str(256*1024*1024),'--tmpfs','/tmp',
           '--dir','/home','--size',str(128*1024*1024),'--tmpfs','/home/model','--chdir','/work']
    for path in ('/usr/lib64','/usr/share/ca-certificates','/usr/share/zoneinfo',
                 '/etc/ssl/certs','/etc/resolv.conf','/etc/hosts','/etc/nsswitch.conf','/etc/ld.so.cache'):
        if Path(path).exists():
            cmd += ['--ro-bind',path,path]
    return cmd


def auth_token():
    fd = os.open(AUTH, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_uid == OPERATOR_UID and not info.st_mode & 0o077 and info.st_size <= 8192, 'auth-file')
        values = [line.split('=',1)[1] for line in stream.read().splitlines() if line.startswith('CLAUDE_CODE_OAUTH_TOKEN=')]
    check(len(values) == 1, 'login-required')
    parsed = shlex.split(values[0])
    check(len(parsed) == 1 and parsed[0] and not any(c.isspace() for c in parsed[0]), 'login-required')
    return parsed[0]


def limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT,MAX_OUTPUT))


def execute(request):
    images = validate(request)
    check(os.geteuid() == 0, 'launcher-identity')
    configure()
    check(os.environ.get('SUDO_UID') in {str(OPERATOR_UID),'0'}, 'launcher-identity')
    for path in (BINARY,BWRAP):
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, 'runtime-integrity')
    check(not BINARY.stat().st_mode & 0o222, 'runtime-integrity')
    env = {'PATH':'/usr/bin:/bin','HOME':'/home/model','CLAUDE_CONFIG_DIR':'/home/model/.claude',
           'LANG':'C.UTF-8','CLAUDE_CODE_SAFE_MODE':'1','DISABLE_AUTOUPDATER':'1'}
    env['CLAUDE_CODE_OAUTH_TOKEN'] = auth_token()
    with tempfile.TemporaryDirectory(prefix='yt-model-') as td:
        work = Path(td) / 'media'
        work.mkdir(mode=0o700)
        (Path(td) / 'passwd').write_text('model:x:65534:65534:Model:/home/model:/bin/false\n')
        (Path(td) / 'group').write_text('model:x:65534:\n')
        for index,(extension,data) in enumerate(images,1):
            path = work / (f'image-{index}' + extension)
            path.write_bytes(data)
            path.chmod(0o400)
        cmd = command(work)
        if request['operation'] == 'probe':
            cmd += ['--','/usr/bin/python3','-c',PROBE]
            payload = json.dumps(request.get('probe_paths',[]))
        else:
            prompt = request['prompt']
            if images:
                prompt += '\n\n## Attached images\n' + '\n'.join(f'- /work/image-{i}{ext}' for i,(ext,_) in enumerate(images,1)) + '\nRead each image before writing the note.'
            cmd += ['--','/opt/claude','--safe-mode','--setting-sources','','--strict-mcp-config',
                    '--mcp-config','{"mcpServers":{}}','--disable-slash-commands','--no-session-persistence',
                    '--permission-mode','dontAsk','--tools','Read' if images else '',
                    '--disallowedTools','Read(//proc/**)','Read(//home/**)','Read(//etc/**)',
                    '--model',request['model']]
            if images:
                cmd += ['--allowedTools','Read(//work/**)']
            cmd += ['-p',prompt]
            payload = request['payload']
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL,
                                    env=env,cwd='/',close_fds=True,start_new_session=True,preexec_fn=limits)
            try:
                proc.communicate(payload.encode(),timeout=180)
            except BaseException:
                os.killpg(proc.pid,signal.SIGKILL)
                proc.wait()
                raise
            check(proc.returncode == 0,'sandbox-child-failed')
            output.seek(0)
            raw = output.read(MAX_OUTPUT + 1)
            check(len(raw) <= MAX_OUTPUT,'output-limit')
            return raw.decode()


def main():
    try:
        check(len(sys.argv) == 1,'arguments-forbidden')
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        check(len(raw) <= MAX_INPUT,'request-limit')
        result = {'ok':True,'output':execute(json.loads(raw))}
    except Exception:
        result = {'ok':False,'error':'model-sandbox-failed'}
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
