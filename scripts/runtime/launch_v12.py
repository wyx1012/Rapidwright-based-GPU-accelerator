"""Build a new THEIA_50per host overlay on gpu02, reusing the pinned v11 CUDA."""
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
SOURCE = ROOT / "theia50per-ppt-hybrid-v11-r3"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v12")
PKG = Path("com/xilinx/rapidwright/rwroute")


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    tmp = path.with_suffix(".pending")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def status(stage, **kw):
    write(WORK / "prepare_status.json",
          dict(host="gpu02", work=str(WORK), stage=stage, pid=os.getpid(),
               at=datetime.now().astimezone().isoformat(), **kw))


def build():
    manifest = read(SOURCE / "corridor_build_receipt.json")
    original = read(SOURCE / "build_receipt.json")
    if read(SOURCE / "status.json")["status"] != "stopped" or manifest["work"] != str(SOURCE):
        raise RuntimeError("The reference v11 run must be identified and stopped")
    for name, expected in manifest["sha256"].items():
        src, dst = SOURCE / name, WORK / name
        if sha(src) != expected:
            raise RuntimeError(f"Reference artifact changed: {name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    import v12_journal
    import v12_scheduler
    import v11_window
    import v12_window
    host_admission = None
    if (WORK / "v12_host_admission.py").is_file():
        import v12_host_admission as host_admission
    src_dir = Path(original["environment"]["RAPIDWRIGHT_PATH"]) / "src" / PKG
    for name, expected in {
        "RouteNode.java": "27da97ae15894529d33f823b50f2b297957b969c4ab4210da5e675b51df2fa26",
        "RouteNodeGraph.java": "cd5f7679c2d7f9ef5730afc83bc3b7cd3567f0b633a923baa1a40c52b5009e0a",
    }.items():
        src = src_dir / name
        if sha(src) != expected:
            raise RuntimeError(f"F5 base source changed: {name}")
        patch = (lambda s: v12_scheduler.patch_node(s, WORK)) if name == "RouteNode.java" else v12_scheduler.patch_graph
        materialized = patch(src.read_text())
        if host_admission is not None and name == "RouteNode.java":
            materialized = host_admission.patch_node(materialized)
        (WORK / name).write_text(materialized)
    for name, patch in {
        "RWRoute.java": lambda s: v12_scheduler.patch_router(s, WORK),
        "FidesLaneGate.java": v12_journal.patch_gate,
        "F4Theia50perCapture.java": v12_journal.patch_capture,
        "worker.py": lambda s: v11_window.replace_once(s,
            "    from v11_window import patch_drive as window_broker",
            "    from v12_window import patch_drive as window_broker"),
    }.items():
        path = WORK / name
        materialized = patch(path.read_text())
        if host_admission is not None and name == "RWRoute.java":
            materialized = host_admission.patch_router(materialized)
        path.write_text(materialized)

    java = Path(read(SOURCE / "native_multichannel.command.json")["argv"][0])
    cp = os.pathsep.join([str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
                           (WORK / "runtime_classpath.txt").read_text().strip()])
    # Compile explicit overlay sources together. Other already pinned classes
    # remain resolved through their previous normal classpath.
    command = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", cp, "-d",
               str(WORK / "classes"), "RWRoute.java", "FidesLaneGate.java", "F4Theia50perCapture.java",
               "F4HostJournal.java", "RouteNode.java", "RouteNodeGraph.java"]
    write(WORK / "build_command.json", dict(java=command, cuda_reused=original["sha256"]["hybrid"]))
    status("compiling")
    with (WORK / "compile_java.log").open("xb") as log:
        subprocess.run(command, cwd=WORK, stdout=log, stderr=subprocess.STDOUT, check=True)
    write(WORK / "build_receipt.json", original)
    old_windows = v11_window.MILESTONES
    try:
        v11_window.MILESTONES = v12_window.MILESTONES
        reference = v11_window.archived_reference(SOURCE / "dispatch.jsonl")
    finally:
        v11_window.MILESTONES = old_windows
    write(WORK / "reference_windows.json", reference)
    pins = {str(path.relative_to(WORK)): sha(path)
            for path in [*WORK.glob("*.py"), *WORK.glob("*.java"), *WORK.glob("*.inc"),
                         *WORK.glob("*.cu"), *WORK.glob("*.cuh"), *WORK.glob("classes/**/*.class"),
                         *(WORK / name for name in manifest["sha256"]
                           if not name.endswith((".class", ".py", ".java", ".cu", ".cuh")))]}
    write(WORK / "corridor_build_receipt.json",
          dict(manifest, work=str(WORK), source_run=str(SOURCE), candidate=str(WORK), sha256=pins))
    status("prepared")
    with (WORK / "worker.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-B", str(WORK / "worker.py")],
                                   cwd=WORK, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    write(WORK / "launch_receipt.json", dict(host="gpu02", work=str(WORK), pid=process.pid,
          worker_sha256=pins["worker.py"], hybrid_sha256=pins["hybrid"],
          started_at=datetime.now().astimezone().isoformat()))
    status("launched", worker_pid=process.pid)


if socket.gethostname().split(".")[0] != "gpu02":
    raise RuntimeError("Expected gpu02")
if "PAYLOAD" in globals():
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status("queued")
    with (WORK / "prepare.log").open("xb") as log:
        job = subprocess.Popen([sys.executable, "-B", str(WORK / "launch_v12.py"), "--prepare"],
                               cwd=WORK, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps(dict(host="gpu02", work=str(WORK), prepare_pid=job.pid)))
elif "--prepare" in sys.argv:
    WORK = Path(__file__).resolve().parent
    try:
        build()
    except Exception as error:
        status("failed", error=str(error), traceback=traceback.format_exc())
        raise
