"""Build and start the v20 GPU startup repair on gpu02."""
from base64 import b64decode
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import traceback

HOST = socket.gethostname().split(".")[0]
if HOST != "gpu02":
    raise RuntimeError("Expected gpu02")
ROOT = Path("/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/"
            "ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1")
SOURCE = ROOT / "theia50per-ppt-hybrid-v19-r2"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v20")
NEW_FILES = {"prepare_v20.py", "v20_patch.py", "v20_handoff.py", "V20Resident.inc", "V20Snapshot.inc"}


def read(path):
    return json.loads(path.read_bytes())


def save(name, data):
    path = WORK / name
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(data, indent=2) + "\n")
    pending.replace(path)


def now():
    return datetime.now().astimezone().isoformat()


def sha(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def status(stage, **details):
    save("prepare_status.json", dict(host=HOST, work=str(WORK), stage=stage, at=now(), **details))


def require_parent_stopped():
    stop = read(SOURCE / "status.json")
    if stop["host"] != HOST or stop["work"] != str(SOURCE) or stop["status"] != "failed":
        raise RuntimeError("Expected terminal v19-r2 with preserved failure evidence")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.getuid() or (proc / "cwd").resolve(strict=True) != SOURCE:
                continue
            command = (proc / "cmdline").read_bytes()
            if any(role in command for role in (b"worker.py", b"PartialRouter", b"/hybrid", b"/direct_paths")):
                raise RuntimeError(f"v19-r2 routing process is still live: {proc.name}")
        except OSError:
            continue


def build():
    require_parent_stopped()
    base = read(SOURCE / "corridor_build_receipt.json")
    if base["work"] != str(SOURCE):
        raise RuntimeError("Unexpected v19-r2 build receipt identity")
    status("copying")
    for name, digest in base["sha256"].items():
        source, target = SOURCE / name, WORK / name
        if sha(source) != digest:
            raise RuntimeError(f"Pinned parent changed: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(SOURCE / "access-layout.txt", WORK / "parent-access-layout.txt")

    import v20_patch as patch
    for name, edit in {
        "hybrid.cu": lambda src: patch.cuda(src, WORK),
        "RWRoute.java": patch.router,
        "F4Theia50perCapture.java": patch.capture,
        "worker.py": patch.worker,
    }.items():
        path = WORK / name
        path.write_text(edit(path.read_text()))

    runtime = read(SOURCE / "build_receipt.json")
    env = os.environ | runtime["environment"] | {"CUDA_VISIBLE_DEVICES": ""}
    old_cuda = read(ROOT / "theia50per-ppt-hybrid-v15-r3/build_command.json")["cuda"]
    cuda_argv = old_cuda[:-1] + ["hybrid.v20"]
    java = Path(read(SOURCE / "native_multichannel.command.json")["argv"][0])
    classpath = os.pathsep.join([
        str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
        (WORK / "runtime_classpath.txt").read_text().strip(),
    ])
    java_argv = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", classpath,
                 "-d", str(WORK / "classes"), "RWRoute.java", "F4Theia50perCapture.java"]
    save("build_command.json", dict(java=java_argv, cuda=cuda_argv, cuda_reused=False))
    status("compiling_cuda")
    with (WORK / "compile_cuda.log").open("xb") as log:
        subprocess.run(cuda_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    (WORK / "hybrid.v20").replace(WORK / "hybrid")
    status("compiling_java")
    with (WORK / "compile_java.log").open("xb") as log:
        subprocess.run(java_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    names = set(base["sha256"]) | NEW_FILES | {"parent-access-layout.txt"}
    names.update(str(path.relative_to(WORK)) for path in (WORK / "classes").rglob("*.class"))
    pins = {name: sha(WORK / name) for name in sorted(names)}
    save("build_receipt.json", dict(runtime, sha256=runtime["sha256"] | {"hybrid": pins["hybrid"]}))
    save("corridor_build_receipt.json",
         dict(base, work=str(WORK), candidate=str(WORK), source_run=str(SOURCE), sha256=pins))
    status("compiled", java_rc=0, cuda_rc=0, worker_sha256=pins["worker.py"], cuda_sha256=pins["hybrid"])
    return pins


def launch(pins):
    require_parent_stopped()
    if any((WORK / name).exists() for name in ("status.json", "worker.log", "launch_receipt.json")):
        raise RuntimeError("Runtime files already present")
    with (WORK / "worker.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-B", str(WORK / "worker.py")],
            cwd=WORK, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    save("launch_receipt.json", dict(host=HOST, work=str(WORK), worker_pid=process.pid,
         started_at=now(), parent=str(SOURCE), worker_sha256=pins["worker.py"],
         hybrid_sha256=pins["hybrid"], budgets=[8000, 262144, 1048576],
         ready_horizon=256, starting_resident_lanes=1, max_gpu_trials=8))


if "PAYLOAD" in globals():
    if set(PAYLOAD) != NEW_FILES:
        raise RuntimeError("Unexpected v20 upload files")
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status("queued")
    with (WORK / "prepare.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-B", str(WORK / "prepare_v20.py"), "--prepare-and-launch"],
            cwd=WORK, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps(dict(host=HOST, work=str(WORK), prepare_pid=process.pid)))
elif "--prepare-and-launch" in sys.argv:
    WORK = Path(__file__).resolve().parent
    try:
        launch(build())
    except Exception as error:
        phase = "launch" if (WORK / "corridor_build_receipt.json").is_file() else "build"
        status("failed", phase=phase, error=str(error), traceback=traceback.format_exc())
        raise

