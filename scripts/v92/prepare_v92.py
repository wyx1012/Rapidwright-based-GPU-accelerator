"""Compile v92 (R3-R4 wave-spill & R5 CPU P1 + small-design GPU skip <100k + FIDES_* env passthrough) on gpu02."""
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import argparse
import json
import os
import py_compile
import shutil
import socket
import subprocess
import sys

HOST = socket.gethostname().split(".")[0]
if HOST != "gpu02":
    raise RuntimeError("Expected gpu02")
ROOT = Path("/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/"
            "ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1")
SOURCE = ROOT / "theia50per-ppt-hybrid-v22"
IDMAP_SRC = Path("/home/yxwang1/FIDES/RapidWright2/src/com/xilinx/rapidwright/rwroute/FidesRrgIdMap.java")
LOADER_SRC = Path("/home/yxwang1/FIDES/RapidWright2/src/com/xilinx/rapidwright/rwroute/FidesRrgLoader.java")
SNAP_DIR = ROOT.parent / "f4-theia50per-snapshot-v1"
WORK = ROOT / globals().get("RUN_NAME", "theia50per-ppt-hybrid-v92")
NEW_FILES = {
    "prepare_v92.py", "v92_patch.py", "v91_patch.py", "v89_patch.py", "v81_patch.py", "v77_patch.py", "v76_patch.py",
    "v73_patch.py", "v67_patch.py", "v66_patch.py", "v33_patch.py", "v27_patch.py", "v24_patch.py",
    "fides_gpu.h", "fides_gpu.cu", "fides_gpu.o", "fides_cpu.cpp",
    "FidesNativeCpu.java", "FidesRrgLoader.java", "FidesRrgIdMap.java", "F4BaselineDcpCheck.java",
    "libfides_cpu.so",
}


def read(path):
    return json.loads(path.read_bytes())


def save(name, data):
    path = WORK / name
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(data, indent=2) + "\n")
    pending.replace(path)


def now():
    return datetime.now().astimezone().isoformat()


def status(stage, **details):
    save("prepare_status.json", dict(host=HOST, work=str(WORK), stage=stage, at=now(), **details))


def sha(path):
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def require_parent_stopped():
    prior_dirs = (
        SOURCE,
        ROOT / "theia50per-ppt-hybrid-v23",
        ROOT / "theia50per-ppt-hybrid-v37",
        ROOT / "theia50per-ppt-hybrid-v76",
        ROOT / "theia50per-ppt-hybrid-v81",
        ROOT / "theia50per-ppt-hybrid-v83",
        ROOT / "theia50per-ppt-hybrid-v88",
        ROOT / "theia50per-ppt-hybrid-v89",
        ROOT / "theia50per-ppt-hybrid-v90",
        ROOT / "theia50per-ppt-hybrid-v91",
    )
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            cwd = (proc / "cwd").resolve(strict=True)
            if cwd not in prior_dirs:
                continue
            command = (proc / "cmdline").read_bytes()
            if any(role in command for role in (b"worker.py", b"PartialRouter", b"/hybrid", b"/direct_paths")):
                raise RuntimeError(f"Prior routing process is still live: {proc.name}")
        except OSError:
            continue


def build():
    require_parent_stopped()
    base = read(SOURCE / "corridor_build_receipt.json")
    if base["work"] != str(SOURCE):
        raise RuntimeError("Unexpected v22 build receipt identity")
    status("copying")
    for name, digest in base["sha256"].items():
        source, target = SOURCE / name, WORK / name
        if sha(source) != digest:
            raise RuntimeError(f"Pinned parent changed: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(SOURCE / "build_receipt.json", WORK / "build_receipt.json")

    shutil.copy2(IDMAP_SRC, WORK / "FidesRrgIdMap.java")
    shutil.copy2(LOADER_SRC, WORK / "FidesRrgLoader.java")

    import v92_patch as patch
    java_sources = {
        "PartialRouter.java": patch.partial_router,
        "RWRoute.java": patch.router,
        "F1Corridor.java": patch.corridor,
        "F2Prewarm.java": patch.f2prewarm,
        "FidesLaneGate.java": patch.gate,
        "FidesRrgLoader.java": patch.rrg_loader,
        "FidesRrgIdMap.java": patch.idmap,
        "RouteNode.java": patch.node,
        "F4RoundDiversion.java": patch.diversion,
    }
    py_sources = {
        "worker.py": patch.worker,
        "v20_handoff.py": patch.handoff,
        "v14_broker.py": patch.broker,
    }
    cuda_sources = {
        "hybrid.cu": patch.cuda,
    }
    for name, edit in {**java_sources, **py_sources, **cuda_sources}.items():
        path = WORK / name
        path.write_text(edit(path.read_text()))
    for py_name in py_sources:
        py_compile.compile(str(WORK / py_name), doraise=True)

    runtime = read(WORK / "build_receipt.json")
    env = os.environ | runtime["environment"] | {"CUDA_VISIBLE_DEVICES": ""}
    java = Path(read(SOURCE / "native_multichannel.command.json")["argv"][0])
    java_home = java.parent.parent

    gpu_obj_argv = [
        "/usr/local/cuda-12.3/bin/nvcc",
        "-std=c++17", "-O3", "-arch=sm_70", "--fmad=false", "--prec-div=true", "--ftz=false",
        "-I", "/home/yxwang1/FIDES/RapidWright2/fides_cuda",
        "-Xcompiler=-fPIC,-O3,-pthread,-Wall,-Wextra",
        "-c", "fides_gpu.cu", "-o", "fides_gpu.o",
    ]
    status("compiling_fides_gpu_cu")
    with (WORK / "compile_gpu_obj.log").open("wb") as log:
        subprocess.run(gpu_obj_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    cpp_argv = [
        "/usr/bin/g++", "-O3", "-march=native", "-std=c++17", "-ffp-contract=off", "-fno-math-errno",
        "-Wall", "-Wextra", "-Werror", "-pthread", "-shared", "-fPIC",
        f"-I{java_home / 'include'}", f"-I{java_home / 'include/linux'}",
        "fides_cpu.cpp", "fides_gpu.o",
        "-L/usr/local/cuda-12.3/lib64", "-lcudart", "-Wl,-rpath,/usr/local/cuda-12.3/lib64",
        "-o", "libfides_cpu.so",
    ]
    status("compiling_cpp_jni")
    with (WORK / "compile_cpp.log").open("wb") as log:
        subprocess.run(cpp_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    cuda_argv = [
        "/usr/local/cuda-12.3/bin/nvcc", "-std=c++14", "-O3", "-arch=sm_70",
        "--default-stream", "per-thread", "--fmad=false", "--prec-div=true", "--ftz=false",
        "-I", "/home/yxwang1/FIDES/RapidWright2/fides_cuda", "-Xcompiler=-pthread",
        "hybrid.cu", "-o", "hybrid",
    ]
    status("compiling_cuda")
    with (WORK / "compile_cuda.log").open("wb") as log:
        subprocess.run(cuda_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    runtime["sha256"]["hybrid"] = sha(WORK / "hybrid")
    save("build_receipt.json", runtime)

    classpath = os.pathsep.join([
        str(WORK / "classes"), str(ROOT / "theia50per-gpu-corridor-full-v3/classes"),
        (WORK / "runtime_classpath.txt").read_text().strip(),
    ])
    all_java = ["FidesNativeCpu.java", "F4BaselineDcpCheck.java", *java_sources]
    java_argv = [str(java.with_name("javac")), "-encoding", "UTF-8", "-cp", classpath,
                 "-d", str(WORK / "classes"), *all_java]
    save("build_command.json", dict(java=java_argv, cpp=cpp_argv, gpu_obj=gpu_obj_argv, cuda=cuda_argv,
                                    cuda_reused=False, parent_source=str(SOURCE)))
    status("compiling_java")
    with (WORK / "compile_java.log").open("wb") as log:
        subprocess.run(java_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    idmap_cache = WORK / "rrg.idmap.bin"
    if not idmap_cache.is_file():
        status("building_idmap_cache")
        idmap_argv = [
            str(java), "-Xmx32G", "-cp", classpath,
            f"-Dfides.v37.buildIdmapCache={idmap_cache}",
            "com.xilinx.rapidwright.rwroute.FidesRrgIdMap",
            str(SNAP_DIR / "rrg.manifest.txt"), str(SNAP_DIR / "rrg.bin"),
        ]
        with (WORK / "build_idmap_cache.log").open("wb") as log:
            subprocess.run(idmap_argv, cwd=WORK, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    names = set(base["sha256"]) | NEW_FILES | set(java_sources) | set(py_sources) | set(cuda_sources)
    names.update(str(path.relative_to(WORK)) for path in (WORK / "classes").rglob("*.class"))
    pins = {name: sha(WORK / name) for name in sorted(names)}
    save("corridor_build_receipt.json",
         dict(base, work=str(WORK), candidate=str(WORK), source_run=str(SOURCE), sha256=pins))
    status("compiled", java_rc=0, cpp_rc=0, gpu_obj_rc=0, cuda_rc=0, cuda_reused=False,
           worker_sha256=pins["worker.py"], cuda_sha256=pins["hybrid"],
           cpp_sha256=pins["libfides_cpu.so"], fides_gpu_cu_sha256=pins["fides_gpu.cu"])
    return pins


def launch(pins):
    require_parent_stopped()
    if any((WORK / name).exists() for name in ("status.json", "worker.log", "launch_receipt.json")):
        raise RuntimeError("Runtime files already present")
    with (WORK / "worker.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-B", str(WORK / "worker.py")],
            cwd=WORK, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    save("launch_receipt.json", dict(host=HOST, work=str(WORK), worker_pid=process.pid, started_at=now(),
         parent=str(SOURCE), engine="v92_r3_r4_wavespill_small_gpu_skip",
         worker_sha256=pins["worker.py"], hybrid_sha256=pins["hybrid"],
         cpp_sha256=pins["libfides_cpu.so"], fides_gpu_cu_sha256=pins["fides_gpu.cu"],
         budgets=[65536, 262144, 0],
         sort_order="HPWL_FIRST", short_box_pad=4, corridor_max_round=999,
         single_gpu=False, legacy_preflight_spawned=False))
    status("launched", worker_pid=process.pid)
    print(json.dumps({"worker_pid": process.pid, "work": str(WORK)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    pins = build()
    if not args.compile_only:
        launch(pins)
