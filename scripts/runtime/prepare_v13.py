"""Compile the v13 isolated Java overlay on gpu02 without starting routing."""
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

ROOT = Path("/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/"
            "ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1")
SOURCE = ROOT / "theia50per-ppt-hybrid-v12-r3"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v13")


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, indent=2) + "\n")
    pending.replace(path)


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def status(stage, **kw):
    write(WORK / "prepare_status.json",
          dict(host="gpu02", work=str(WORK), stage=stage, pid=os.getpid(),
               at=datetime.now().astimezone().isoformat(), routing_started=False, **kw))


def build():
    stopped = read(SOURCE / "operator_stop.json")
    base = read(SOURCE / "corridor_build_receipt.json")
    original = read(SOURCE / "build_receipt.json")
    if stopped["terminal"] != "stopped" or any(stopped["remaining"].values()) or base["work"] != str(SOURCE):
        raise RuntimeError("Expected a stopped, identified v12-r3 reference")
    for name, expected in base["sha256"].items():
        src, dst = SOURCE / name, WORK / name
        if sha(src) != expected:
            raise RuntimeError(f"Reference v12 artifact changed: {name}")
        if name == "F4HostJournal.java":
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    write(WORK / "build_receipt.json", original)

    import v13_patch
    import v11_window
    import v12_window
    for name, patch in {
        "RWRoute.java": lambda s: v13_patch.patch_router(s, WORK),
        "RouteNode.java": lambda s: v13_patch.patch_node(s, WORK),
        "FidesLaneGate.java": v13_patch.patch_gate,
        "F4Theia50perCapture.java": v13_patch.patch_capture,
        "worker.py": lambda s: v11_window.replace_once(s,
            "from v12_window import patch_drive as window_broker",
            "from v13_window import patch_drive as window_broker"),
    }.items():
        path = WORK / name
        path.write_text(patch(path.read_text()))

    java = Path(read(SOURCE / "native_multichannel.command.json")["argv"][0])
    cp = os.pathsep.join([str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
                           (WORK / "runtime_classpath.txt").read_text().strip()])
    command = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", cp, "-d", str(WORK / "classes"),
               "RWRoute.java", "FidesLaneGate.java", "F4Theia50perCapture.java", "F4SpatialSchedule.java",
               "F4HostJournal.java", "RouteNode.java", "RouteNodeGraph.java"]
    write(WORK / "build_command.json", dict(java=command, cuda_reused=original["sha256"]["hybrid"]))
    status("compiling")
    with (WORK / "compile_java.log").open("xb") as log:
        subprocess.run(command, cwd=WORK, stdout=log, stderr=subprocess.STDOUT, check=True)

    old = v11_window.MILESTONES
    try:
        v11_window.MILESTONES = v12_window.MILESTONES
        reference = v11_window.archived_reference(SOURCE / "dispatch.jsonl")
    finally:
        v11_window.MILESTONES = old
    write(WORK / "reference_windows.json", reference)
    paths = [*WORK.glob("*.py"), *WORK.glob("*.java"), *WORK.glob("*.inc"), *WORK.glob("classes/**/*.class"),
             *(WORK / name for name in base["sha256"]
               if not name.endswith((".py", ".java", ".inc", ".class")))]
    pins = {str(path.relative_to(WORK)): sha(path) for path in paths}
    write(WORK / "corridor_build_receipt.json",
          dict(base, work=str(WORK), source_run=str(SOURCE), candidate=str(WORK), sha256=pins))

    # Compile the layered broker without starting Java or taking a GPU lease.
    worker_path = WORK / "worker.py"
    prefix, boundary, _ = worker_path.read_text().partition('\ntry:\n    save("using_existing_gpu_only_router"')
    if not boundary:
        raise RuntimeError("Expected the existing worker boundary")
    scope = {"__file__": str(worker_path), "__name__": "prepare_v13"}
    exec(compile(prefix, str(worker_path), "exec"), scope)
    scope["load_drive"]()
    status("compiled", cuda_sha256=pins["hybrid"], worker_sha256=pins["worker.py"],
           classes=len(list((WORK / "classes").rglob("*.class"))), broker_materialized=True)


if socket.gethostname().split(".")[0] != "gpu02":
    raise RuntimeError("Expected gpu02")
if "PAYLOAD" in globals():
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status("queued")
    with (WORK / "prepare.log").open("xb") as log:
        job = subprocess.Popen([sys.executable, "-B", str(WORK / "prepare_v13.py"), "--prepare"],
                               cwd=WORK, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps(dict(host="gpu02", work=str(WORK), prepare_pid=job.pid, routing_started=False)))
elif "--prepare" in sys.argv:
    WORK = Path(__file__).resolve().parent
    try:
        build()
    except Exception as error:
        status("failed", error=str(error), traceback=traceback.format_exc())
        raise
