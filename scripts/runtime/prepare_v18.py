"""Compile and start the isolated v18 first-round candidate on gpu02."""
from base64 import b64decode
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import traceback

HOST = socket.gethostname().split('.')[0]
if HOST != 'gpu02':
    raise RuntimeError('Expected gpu02')
ROOT = Path('/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/'
            'ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1')
SOURCE = ROOT / 'theia50per-ppt-hybrid-v17-r2'
WORK = ROOT / globals().get('RUN_NAME', 'theia50per-ppt-hybrid-v18')

def read(path):
    return json.loads(path.read_bytes())

def save(name, data):
    path = WORK / name
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(data, indent=2) + '\n')
    pending.replace(path)

def now():
    return datetime.now().astimezone().isoformat()

def sha(path):
    return sha256(path.read_bytes()).hexdigest()

def status(stage, **kw):
    save('prepare_status.json', dict(host=HOST, work=str(WORK), stage=stage,
                                     at=now(), **kw))

def require_source_stopped():
    stop = read(SOURCE / 'operator_stop.json')
    if stop['terminal'] != 'stopped' or stop['remaining'] or stop['work'] != str(SOURCE):
        raise RuntimeError('Expected stopped v17-r2')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.getuid() or (proc / 'cwd').resolve(strict=True) != SOURCE:
                continue
            line = (proc / 'cmdline').read_bytes()
            if any(x in line for x in (b'worker.py', b'PartialRouter', b'/hybrid', b'/direct_paths')):
                raise RuntimeError(f'v17-r2 process still exists: {proc.name}')
        except OSError:
            continue

def build():
    require_source_stopped()
    base = read(SOURCE / 'corridor_build_receipt.json')
    if base['work'] != str(SOURCE):
        raise RuntimeError('Wrong parent compile receipt')
    status('copying')
    for name, old in sorted(base['sha256'].items()):
        source, target = SOURCE / name, WORK / name
        if sha(source) != old:
            raise RuntimeError(f'Pinned parent changed: {name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(SOURCE / 'build_receipt.json', WORK / 'build_receipt.json')

    import v18_patch as patch
    targets = {
        'RWRoute.java': lambda s: patch.router(s, WORK),
        'F4RoundDiversion.java': patch.policy,
    }
    for name, edit in targets.items():
        path = WORK / name
        path.write_text(edit(path.read_text()))
    java = Path(read(SOURCE / 'native_multichannel.command.json')['argv'][0])
    java_cp = os.pathsep.join([str(WORK / 'classes'), str(ROOT / 'theia50per-gpu-corridor-full-v3/classes'),
                               (WORK / 'runtime_classpath.txt').read_text().strip()])
    env = os.environ | read(WORK / 'build_receipt.json')['environment'] | {'CUDA_VISIBLE_DEVICES': ''}
    argv = [str(java.with_name('javac')), '-encoding', 'UTF-8', '-cp', java_cp,
            '-d', str(WORK / 'classes'), *targets]
    save('build_command.json', dict(java=argv, cuda_reused_sha256=base['sha256']['hybrid']))
    status('compiling')
    with (WORK / 'compile_java.log').open('xb') as log:
        subprocess.run(argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    names = set(base['sha256']) | {'F4RerouteControl.inc','v18_patch.py','prepare_v18.py'}
    names.update(str(f.relative_to(WORK)) for f in (WORK/'classes').rglob('*.class'))
    pins = {name: sha(WORK/name) for name in sorted(names)}
    save('corridor_build_receipt.json',
         dict(base, work=str(WORK), candidate=str(WORK), source_run=str(SOURCE), sha256=pins))
    status('compiled', java_rc=0, cuda_reused=True, worker_sha256=pins['worker.py'],
           cuda_sha256=pins['hybrid'])
    return pins

def launch(pins):
    require_source_stopped()
    with (WORK / '.operator-start.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any((WORK / name).exists() for name in ('status.json','worker.log','launch_receipt.json')):
            raise RuntimeError('Runtime files already present')
        if any(sha(WORK/name) != pins[name] for name in ('worker.py','hybrid')):
            raise RuntimeError('Runtime changed after compiling')
        with (WORK/'worker.log').open('xb') as log:
            process = subprocess.Popen([sys.executable,'-B',str(WORK/'worker.py')],
                    cwd=WORK, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
        save('launch_receipt.json', dict(host=HOST, work=str(WORK), worker_pid=process.pid,
            started_at=now(), parent=str(SOURCE), worker_sha256=pins['worker.py'],
            hybrid_sha256=pins['hybrid'], budgets=[8000,262144,1048576]))

if 'PAYLOAD' in globals():
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status('queued')
    with (WORK/'prepare.log').open('xb') as log:
        proc = subprocess.Popen([sys.executable,'-B',str(WORK/'prepare_v18.py'),'--prepare-and-launch'],
                    cwd=WORK, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
    print(json.dumps(dict(host=HOST, work=str(WORK), prepare_pid=proc.pid)))
elif '--prepare-and-launch' in sys.argv:
    WORK = Path(__file__).resolve().parent
    try:
        launch(build())
    except Exception as err:
        phase='launch' if (WORK/'corridor_build_receipt.json').is_file() else 'build'
        status('failed', phase=phase, error=str(err), traceback=traceback.format_exc())
        raise

