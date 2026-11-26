"""Build and launch the v11 THEIA_50per worker in a new gpu02 directory."""
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
PREVIOUS = ROOT / "theia50per-ppt-hybrid-v10"
V9D_SOURCE = ROOT / "gpu-ppt-active-frontier-v9d-w2"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v11")


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def build():
    from v11_bridge import patch_capture as bridge_capture
    from v11_gpu import patch as patch_cuda, patch_frontier
    from v11_window import archived_reference, replace_once

    manifest = read(PREVIOUS / "corridor_build_receipt.json")
    source_state = read(PREVIOUS / "status.json")
    if source_state["status"] != "stopped" or manifest["work"] != str(PREVIOUS):
        raise RuntimeError("v10 must be stopped and identified before staging v11")
    build_ref = read(Path(manifest["candidate"]) / "build_receipt.json")
    source = V9D_SOURCE / "hybrid.cu"
    env = os.environ | build_ref["environment"] | {"CUDA_VISIBLE_DEVICES": ""}
    if digest(V9D_SOURCE / "hybrid") != build_ref["sha256"]["hybrid"]:
        raise RuntimeError("v9d source directory does not hold the pinned baseline executable")

    for name, expected in manifest["sha256"].items():
        if name in {"hybrid", "startup_source.py"}:
            continue
        original = PREVIOUS / name
        target = WORK / name
        if digest(original) != expected:
            raise RuntimeError(f"v10 reference changed: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)

    from v11_scheduler import patch_rwroute

    router_source = WORK / "RWRoute.java"
    capture_source = WORK / "F4Theia50perCapture.java"
    router_source.write_text(patch_rwroute(router_source.read_text()))
    capture_source.write_text(bridge_capture(capture_source.read_text()))
    worker_file = WORK / "worker.py"
    worker = replace_once(worker_file.read_text(),
        "    return hybrid_broker(active_broker(patch_host_drive(score_broker(source))))",
        "    from v11_bridge import patch_drive as bridge_broker\n"
        "    from v11_window import patch_drive as window_broker\n"
        "    return window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source))))))")
    worker = replace_once(worker, 'except KeyboardInterrupt:\n    save("operator_stop", status="stopped")',
        'except __import__("v11_window").NoBenefit as result:\n'
        '    save("prefix_no_benefit", status="stopped_no_benefit", java_pid=None, gpu_pid=None,\n'
        '         active_pid=None, prefix_result=result.summary)\n'
        'except KeyboardInterrupt:\n    save("operator_stop", status="stopped")')
    worker_file.write_text(worker)

    frontier = V9D_SOURCE / "active_frontier_v9.cuh"
    (WORK / frontier.name).write_text(patch_frontier(frontier.read_text()))
    shutil.copy2(V9D_SOURCE / "active_frontier_v9_trace.cuh", WORK / "active_frontier_v9_trace.cuh")
    (WORK / "hybrid.cu").write_text(patch_cuda(source.read_text()))
    nvcc = shutil.which("nvcc", path=env["PATH"])
    java = Path(read(PREVIOUS / "native_multichannel.command.json")["argv"][0])
    classpath = os.pathsep.join(
        [str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
         (WORK / "runtime_classpath.txt").read_text().strip()])
    java_sources = ["RWRoute.java", "FidesLaneGate.java", "F4Theia50perCapture.java"]
    for name in ("F4HybridBenefit.java", "F4BatchBenefitV11.java"):
        if (WORK / name).is_file():
            java_sources.append(name)
    javac_command = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", classpath,
                     "-d", str(WORK / "classes"), *java_sources]
    cuda_command = [nvcc, "-std=c++14", "-O3", "-arch=sm_70", "--default-stream", "per-thread",
                    "--fmad=false", "--prec-div=true", "--ftz=false", "-I",
                    str(Path(env["RAPIDWRIGHT_PATH"]) / "fides_cuda"), "-Xcompiler=-pthread",
                    "hybrid.cu", "-o", "hybrid"]
    write(WORK / "build_command.json", dict(cuda=cuda_command, java=javac_command,
                                         cuda_source_v9d_sha256=digest(source)))
    status("compiling")
    commands = [("java", javac_command)]
    previous_cuda = ROOT / "theia50per-ppt-hybrid-v11-r2"
    last_build = previous_cuda / "prepare_status.json"
    if (last_build.is_file() and previous_cuda != WORK
            and read(last_build).get("error") == "v11 compile failed: {'java': 1, 'cuda': 0}"
            and (previous_cuda / "hybrid").is_file()
            and all(digest(WORK / name) == digest(previous_cuda / name) for name in
                    ("hybrid.cu", "active_frontier_v9.cuh", "active_frontier_v9_trace.cuh"))
            and cuda_command == read(previous_cuda / "build_command.json")["cuda"]):
        shutil.copy2(previous_cuda / "hybrid", WORK / "hybrid")
        write(WORK / "cuda_reused.json", dict(from_run=str(previous_cuda), sha256=digest(WORK / "hybrid")))
    else:
        commands.append(("cuda", cuda_command))
    compile_tasks = []
    try:
        for name, command in commands:
            with (WORK / f"compile_{name}.log").open("xb") as log:
                compile_tasks.append((name, subprocess.Popen(command, cwd=WORK, env=env,
                                                             stdout=log, stderr=subprocess.STDOUT)))
        outcomes = {name: process.wait() for name, process in compile_tasks}
    finally:
        for _, process in compile_tasks:
            if process.poll() is None:
                process.terminate()
                process.wait()
    if any(outcomes.values()):
        raise RuntimeError(f"v11 compile failed: {outcomes}")
    write(WORK / "build_receipt.json", dict(ready=True, environment=build_ref["environment"],
         sha256={"hybrid": digest(WORK / "hybrid")}, reference=manifest["candidate"]))
    write(WORK / "reference_windows.json", archived_reference(PREVIOUS / "dispatch.jsonl"))
    pins = {str(path.relative_to(WORK)): digest(path)
            for path in [*WORK.glob("*.py"), *WORK.glob("*.java"), *WORK.glob("*.cu"),
                         *WORK.glob("*.cuh"), *WORK.glob("classes/**/*.class"),
                         *(WORK / name for name in manifest["sha256"]
                           if not name.endswith((".class", ".py", ".java", ".cu", ".cuh")))]}
    write(WORK / "corridor_build_receipt.json", dict(manifest, work=str(WORK), source_run=str(PREVIOUS),
         candidate=str(WORK), sha256=pins, active_frontier="cross_slr_and_same_slr"))
    sys.path.insert(0, str(WORK))
    prefix, boundary, _ = worker.partition('\ntry:\n    save("using_existing_gpu_only_router"')
    if not boundary:
        raise RuntimeError("Expected the existing whole-design worker main boundary")
    scope = {"__file__": str(worker_file), "__name__": "prepare_v11"}
    exec(compile(prefix, str(worker_file), "exec"), scope)
    scope["load_drive"]()
    write(WORK / "prepared.json", dict(host="gpu02", work=str(WORK),
          worker_sha256=pins["worker.py"], hybrid_sha256=pins["hybrid"],
          previous=str(PREVIOUS), configured_channels=8, cpu_fallback_allowed=False))
    status("prepared")


def status(stage, **fields):
    write(WORK / "prepare_status.json",
          dict(host="gpu02", work=str(WORK), pid=os.getpid(),
               at=datetime.now().astimezone().isoformat(), stage=stage, **fields))


def start():
    prepared = read(WORK / "prepared.json")
    if (WORK / "launch_receipt.json").exists():
        return read(WORK / "launch_receipt.json")
    if prepared["worker_sha256"] != digest(WORK / "worker.py"):
        raise RuntimeError("Prepared worker changed")
    with (WORK / "worker.log").open("xb") as log:
        job = subprocess.Popen([sys.executable, "-B", str(WORK / "worker.py")], cwd=WORK,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    receipt = dict(prepared, pid=job.pid, started_at=datetime.now().astimezone().isoformat())
    write(WORK / "launch_receipt.json", receipt)
    status("launched", worker_pid=job.pid)
    return receipt


if socket.gethostname().split(".")[0] != "gpu02":
    raise RuntimeError("Expected gpu02")
if "PAYLOAD" in globals():
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status("queued")
    with (WORK / "prepare.log").open("xb") as log:
        job = subprocess.Popen([sys.executable, "-B", str(WORK / "launch_v11.py"), "--prepare"],
                               cwd=WORK, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps(dict(work=str(WORK), prepare_pid=job.pid)))
elif "--prepare" in sys.argv:
    WORK = Path(__file__).resolve().parent
    try:
        build()
        start()
    except Exception as error:
        status("failed", error=str(error), traceback=traceback.format_exc())
        raise
