"""Prepare and compile an isolated PPT page 11 candidate on gpu02."""
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
SOURCE = ROOT / "theia50per-ppt-hybrid-v13"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v14")


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, indent=2) + "\n")
    pending.replace(path)


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def status(stage, **details):
    write(WORK / "prepare_status.json", dict(host="gpu02", work=str(WORK),
          stage=stage, at=datetime.now().astimezone().isoformat(), routing_started=False, **details))


def quote():
    log = (SOURCE / "worker.log").read_text().splitlines()
    def timestamp(label):
        line = next(row for row in log if row.endswith(" " + label))
        return datetime.fromisoformat(line.split(" ", 1)[0])
    start = timestamp("gpu_direct_precompute_complete")
    end = timestamp("direct_gpu_multichannel")
    warm = [[] for _ in range(9)]
    with (SOURCE / "dispatch.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["batch"] != 1:
                warm[row["width"]].append(row["host_chain_ns"])
    samples = [len(values) for values in warm]
    quotes = [sorted(values)[(9 * len(values) - 1) // 10] if values else 0 for values in warm]
    elapsed = int((end - start).total_seconds() * 1_000_000_000)
    if elapsed <= 0 or not quotes[1]:
        raise RuntimeError("Missing v13 startup or width-one timing")
    return dict(source=str(SOURCE), cold_start=start.isoformat(), cold_end=end.isoformat(),
                cold_upper_ns=elapsed, warm_samples_by_width=samples, warm_ns_by_width=quotes,
                warm_statistic="P90 host_chain_ns excluding first batch")


def build():
    stop = read(SOURCE / "operator_stop.json")
    base = read(SOURCE / "corridor_build_receipt.json")
    original = read(SOURCE / "build_receipt.json")
    if stop["terminal"] != "stopped" or any(stop["remaining"].values()) or base["work"] != str(SOURCE):
        raise RuntimeError("Expected the stopped v13 reference")
    for name, expected in base["sha256"].items():
        src, dst = SOURCE / name, WORK / name
        if sha(src) != expected:
            raise RuntimeError(f"v13 reference changed: {name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    write(WORK / "build_receipt.json", original)
    write(WORK / "gpu_quote.json", quote())

    import v14_patch as patch
    from v11_window import replace_once
    for name, transform in {
        "RWRoute.java": lambda source: patch.router(source, WORK),
        "FidesLaneGate.java": patch.gate,
        "F4Theia50perCapture.java": patch.capture,
        "v11_bridge.py": patch.bridge,
        "worker.py": lambda source: replace_once(source,
            "from v13_window import patch_drive as window_broker",
            "from v14_broker import patch_drive as window_broker"),
    }.items():
        path = WORK / name
        path.write_text(transform(path.read_text()))

    java = Path(read(SOURCE / "native_multichannel.command.json")["argv"][0])
    cp = os.pathsep.join([str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
                          (WORK / "runtime_classpath.txt").read_text().strip()])
    command = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", cp, "-d",
               str(WORK / "classes"), "RWRoute.java", "FidesLaneGate.java", "F4Theia50perCapture.java",
               "F3RoundAdmission.java", "F4RoundDiversion.java", "F4DiversionSmoke.java"]
    write(WORK / "build_command.json", dict(java=command, cuda_reused=original["sha256"]["hybrid"]))
    status("compiling")
    with (WORK / "compile_java.log").open("xb") as log:
        subprocess.run(command, cwd=WORK, stdout=log, stderr=subprocess.STDOUT, check=True)

    paths = [*WORK.glob("*.py"), *WORK.glob("*.java"), *WORK.glob("*.inc"), *WORK.glob("classes/**/*.class"),
             WORK / "gpu_quote.json", *(WORK / name for name in base["sha256"]
                                        if not name.endswith((".py", ".java", ".inc", ".class")))]
    pins = {str(path.relative_to(WORK)): sha(path) for path in paths}
    write(WORK / "corridor_build_receipt.json",
          dict(base, work=str(WORK), source_run=str(SOURCE), candidate=str(WORK), sha256=pins))
    worker_file = WORK / "worker.py"
    prefix, boundary, _ = worker_file.read_text().partition('\ntry:\n    save("using_existing_gpu_only_router"')
    if not boundary:
        raise RuntimeError("Expected the routing worker entry point")
    scope = {"__file__": str(worker_file), "__name__": "prepare_v14"}
    exec(compile(prefix, str(worker_file), "exec"), scope)
    scope["load_drive"]()
    with (WORK / "diversion_smoke.log").open("xb") as log:
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")}
        subprocess.run([str(java), "-cp", cp, "com.xilinx.rapidwright.rwroute.F4DiversionSmoke"],
                       cwd=WORK, env=clean, stdout=log, stderr=subprocess.STDOUT, check=True)
    status("compiled", cuda_sha256=pins["hybrid"], worker_sha256=pins["worker.py"],
           quote=read(WORK / "gpu_quote.json"), broker_materialized=True,
           policy_smoke=True, classes=len(list((WORK / "classes").rglob("*.class"))))


if socket.gethostname().split(".")[0] != "gpu02":
    raise RuntimeError("Expected gpu02")
if "PAYLOAD" in globals():
    WORK.mkdir()
    for name, blob in PAYLOAD.items():
        (WORK / name).write_bytes(b64decode(blob))
    status("queued")
    with (WORK / "prepare.log").open("xb") as log:
        job = subprocess.Popen([sys.executable, "-B", str(WORK / "prepare_v14.py"), "--prepare"],
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
