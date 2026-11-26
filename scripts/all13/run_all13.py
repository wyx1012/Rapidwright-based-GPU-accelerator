#!/usr/bin/env python3
"""Run a hybrid GPU/CPU router build (default v76) on every competition case on gpu02.

Since v73, F2Prewarm.java no longer hard-codes THEIA_50per connection counts (if an older engine
such as v70 is passed via --engine v70, unpin_f2_geometry automatically relaxes that pin).
Each design's static RRG closure (rrg.bin + rrg.manifest.txt + rrg.idmap.bin) is cached under
`all13-rrg-cache/<case>` via hardlinks so subsequent engine versions skip the ~500s export step.

Usage: run_all13.py [--engine v76] [--phase prepare|run|both] [--force] [--timeout SEC]
                    [--keep-rrg] [--no-rrg-cache] case [case ...]
"""
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

assert socket.gethostname().split(".")[0] == "gpu02", "must run on gpu02"
M = Path("/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/"
         "f4-ppt-runtime-measurement-v1")
CASES = Path("/home/yxwang1/FIDES/RapidWright2/cases")
ENGINE = "v76"
SRC = M / f"theia50per-ppt-hybrid-{ENGINE}"
SUMMARY = M / f"{ENGINE}all13-summary"
RRG_CACHE = M / "all13-rrg-cache"


def configure_engine(engine):
    global ENGINE, SRC, SUMMARY
    ENGINE = engine
    SRC = M / f"theia50per-ppt-hybrid-{ENGINE}"
    SUMMARY = M / f"{ENGINE}all13-summary"
    if not SRC.is_dir():
        raise RuntimeError(f"missing engine build dir: {SRC}")

SKIP_NAMES = {
    "status.json", "worker.log", "launch_receipt.json", "native_multichannel.log",
    "native_multichannel.command.json", "access_ordinals.log", "access_ordinals.command.json",
    "dispatch.jsonl", "dcp_legality.log", "cold_dcp_doublecheck.log", "features.live.bin",
    "rrg.idmap.bin", "direct_precompute.json", "direct_precompute_application.json",
    "direct_finalize.json", "direct_finalize_application.json", "device_selection.json",
    "f4_hybrid_cpu_final.json", "indirect_device_released", "indirect_device_release_requested",
    "result.json", "recorder.log", "recorder_status.json", "v37_round_history.md",
    "v37_round_history.jsonl", "resident_preflight.json", "preflight-access-layout.txt",
    "prepare_status.json", "__pycache__", "worker.py", "case.json",
}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError("worker anchor missing/ambiguous: " + old.splitlines()[0][:90])
    return text.replace(old, new, 1)


def make_worker(text):
    text = once(
        text,
        'exec(compile(source[:start], str(reference_worker), "exec"))\n',
        'exec(compile(source[:start], str(reference_worker), "exec"))\n'
        'case = json.loads((work / "case.json").read_text())\n'
        'dcp = Path(case["dcp"])\n'
        'CASE = case["name"]\n'
        '# the static RRG artifact is a per-design closure (nodes reachable from the design), so each case\n'
        '# uses its own dump; env0 holds a separate binding used by old_graph_table()\n'
        'rrg = work / "rrg.bin"\n'
        'manifest = work / "rrg.manifest.txt"\n'
        'env0["rrg"] = rrg\n'
        'env0["manifest"] = manifest\n')
    text = once(
        text,
        '    feature = read(features_dir / "result.json")\n'
        '    check(feature["status"] == "passed" and feature["design"] == "THEIA_50per"\n'
        '          and digest(dcp) == feature["input_sha256"]\n'
        '          and feature["initial_connections"] == 4952719, "Wrong THEIA feature reference or input")\n',
        '    feature = dict(status="passed", design=CASE, input_sha256=digest(dcp), features_sha256=None,\n'
        '                   initial_connections=None, resource_input_bytes=dcp.stat().st_size)\n')
    text = once(
        text,
        '        check(live_path.stat().st_size == 4952719 * 72, "Incomplete live GPU feature output")\n',
        '        live_bytes = live_path.stat().st_size\n'
        '        check(live_bytes > 0 and live_bytes % 72 == 0, "Incomplete live GPU feature output")\n'
        '        live_rows = live_bytes // 72\n')
    text = once(text, 'rows=4952719,', 'rows=live_rows,')
    text = once(
        text,
        '    if dcp_old not in out:\n        raise RuntimeError("Missing dcp_old anchor in drive")\n',
        '    out = out.replace("THEIA_50per_gpu_full.dcp", f"{CASE}_gpu_full.dcp")\n'
        '    # designs without direct (single-hop) connections legitimately report direct_connections == 0\n'
        '    out = out.replace(\'and direct["direct_connections"] > 0\', \'and direct["direct_connections"] >= 0\')\n'
        '    if dcp_old not in out:\n        raise RuntimeError("Missing dcp_old anchor in drive")\n')
    text = text.replace('"""Run THEIA_50per with', '"""Run the case-generic (see case.json) harness with', 1)
    return text


JAVAC = ("/home/yxwang1/FIDES/recovery-2026-10-22/toolchains/amazon-corretto-11.0.31.11.1-linux-x64/"
         "bin/javac")


def patch_java(work, edits):
    """Apply exact-once text edits to Java overlay sources in the per-case dir, then recompile
    only those files into the per-case classes dir."""
    files = []
    for name, old, new in edits:
        path = work / name
        text = path.read_text()
        if text.count(old) != 1:
            raise RuntimeError(f"{name}: anchor missing/ambiguous: {old[:70]}")
        path.write_text(text.replace(old, new, 1))
        if path not in files:
            files.append(path)
    cp = os.pathsep.join([str(work / "classes"), str(M / "theia50per-gpu-corridor-full-v3/classes"),
                          (work / "runtime_classpath.txt").read_text().strip()])
    done = subprocess.run([JAVAC, "-encoding", "UTF-8", "-cp", cp, "-d", str(work / "classes")]
                          + [str(f) for f in files], capture_output=True, text=True)
    (work / "compile_case_patch.log").write_text(done.stdout + done.stderr)
    if done.returncode:
        raise RuntimeError("javac case patch failed: " + done.stderr[-600:])


def unpin_f2_geometry(work):
    """Relax THEIA_50per-specific pins only if still present (already relaxed in v73+).
    FidesLaneGate: name the first unmapped node in the fatal message (diagnostic only).
    RWRoute: enable -Dfides.rrgDump.exitAfter=true exit hook after static RRG dump."""
    edits = []
    f2_text = (work / "F2Prewarm.java").read_text()
    if "if (indirectCount != 4_909_155 || nAll != 4_952_719) {" in f2_text:
        edits.append(("F2Prewarm.java", "if (indirectCount != 4_909_155 || nAll != 4_952_719) {",
                      "if (indirectCount < 0 || nAll <= 0) {"))
    gate_text = (work / "FidesLaneGate.java").read_text()
    if 'throw new IllegalStateException("Initial live nodes missing from the RRG manifest: " + unmappedLive);' in gate_text:
        edits.append(("FidesLaneGate.java",
                      'throw new IllegalStateException("Initial live nodes missing from the RRG manifest: " + unmappedLive);',
                      'throw new IllegalStateException("Initial live nodes missing from the RRG manifest: " + unmappedLive'
                      ' + " e.g. " + firstUnmapped);'))
    rw_text = (work / "RWRoute.java").read_text()
    if 'fides.rrgDump.exitAfter' not in rw_text:
        edits.append(("RWRoute.java",
                      "            FidesRrgFlattener.flattenAndDump(routingGraph, config.getFidesRrgDumpPrefix(), presentCongestionFactor);\n",
                      "            FidesRrgFlattener.flattenAndDump(routingGraph, config.getFidesRrgDumpPrefix(), presentCongestionFactor);\n"
                      "            if (Boolean.getBoolean(\"fides.rrgDump.exitAfter\")) {\n"
                      "                System.out.println(\"F4_ALL13_RRG_DUMP_DONE\");\n"
                      "                System.out.flush();\n"
                      "                System.exit(0);\n"
                      "            }\n"))
    if edits:
        patch_java(work, edits)


def clone(case, force):
    work = M / f"{ENGINE}all13-{case}"
    if work.exists():
        if not force:
            raise RuntimeError(f"{work} exists (use --force)")
        shutil.rmtree(work)
    work.mkdir()
    for entry in SRC.iterdir():
        if entry.name in SKIP_NAMES or entry.name.startswith("resident-preflight-") \
                or entry.suffix == ".dcp":
            continue
        if entry.is_dir():
            shutil.copytree(entry, work / entry.name)
        else:
            shutil.copy2(entry, work / entry.name)
    dcp = CASES / case / f"{case}_partial_routed.dcp"
    if not dcp.is_file():
        raise RuntimeError(f"missing input {dcp}")
    (work / "case.json").write_text(json.dumps(dict(name=case, dcp=str(dcp), engine=ENGINE), indent=1) + "\n")
    (work / "worker.py").write_text(make_worker((SRC / "worker.py").read_text()))
    unpin_f2_geometry(work)
    py_compile = subprocess.run([sys.executable, "-m", "py_compile", str(work / "worker.py")],
                                capture_output=True, text=True)
    if py_compile.returncode:
        raise RuntimeError("worker.py syntax: " + py_compile.stderr)
    return work, dcp


def java_cp(work):
    return os.pathsep.join([str(work / "classes"), str(M / "theia50per-gpu-corridor-full-v3/classes"),
                            (work / "runtime_classpath.txt").read_text().strip()])


def _link_or_copy(src_file, dst_file):
    if dst_file.exists():
        dst_file.unlink()
    try:
        os.link(src_file, dst_file)
    except OSError:
        shutil.copy2(src_file, dst_file)


def export_rrg(work, dcp, case, use_cache=True):
    """Dump or reuse this design's static RRG artifact (rrg.bin + rrg.manifest.txt + rrg.idmap.bin)."""
    cache_dir = RRG_CACHE / case
    cache_files = ("rrg.bin", "rrg.manifest.txt", "rrg.idmap.bin")
    if use_cache and all((cache_dir / fn).is_file() and (cache_dir / fn).stat().st_size > 0 for fn in cache_files):
        t0 = time.time()
        for fn in cache_files:
            _link_or_copy(cache_dir / fn, work / fn)
        return dict(cache_hit=True, export_s=round(time.time() - t0, 2), idmap_s=0.0,
                    rrg_bytes=(work / "rrg.bin").stat().st_size,
                    manifest_bytes=(work / "rrg.manifest.txt").stat().st_size)

    java = str(Path(JAVAC).with_name("java"))
    t0 = time.time()
    argv = [java, "-Xmx256G", "-Dfides.rrgDump.exitAfter=true", "-cp", java_cp(work),
            "com.xilinx.rapidwright.MainEntrypoint", "PartialRouter", str(dcp), str(work / "rrg_export_unused.dcp"),
            "--nonTimingDriven", "--fidesLaneMirror", "--fidesLaneRrg", str(work / "rrg.bin"),
            "--fidesLaneManifest", str(work / "rrg.manifest.txt"), "--sortOrder", "HPWL_FIRST",
            "--fidesRrgDump", str(work / "rrg")]
    (work / "rrg_export.command.json").write_text(json.dumps(argv, indent=1))
    with (work / "rrg_export.log").open("wb") as stream:
        rc = subprocess.run(argv, cwd=work, stdout=stream, stderr=subprocess.STDOUT).returncode
    log = (work / "rrg_export.log").read_text(errors="replace")
    if rc or "F4_ALL13_RRG_DUMP_DONE" not in log or not (work / "rrg.bin").is_file() \
            or not (work / "rrg.manifest.txt").is_file():
        raise RuntimeError(f"RRG export failed rc={rc}; see {work}/rrg_export.log")
    t1 = time.time()
    idmap = [java, "-Xmx32G", "-cp", java_cp(work), f"-Dfides.v37.buildIdmapCache={work / 'rrg.idmap.bin'}",
             "com.xilinx.rapidwright.rwroute.FidesRrgIdMap", str(work / "rrg.manifest.txt"), str(work / "rrg.bin")]
    with (work / "build_idmap_cache.log").open("wb") as stream:
        rc = subprocess.run(idmap, cwd=work, stdout=stream, stderr=subprocess.STDOUT).returncode
    if rc or not (work / "rrg.idmap.bin").is_file():
        raise RuntimeError(f"idmap cache build failed rc={rc}; see {work}/build_idmap_cache.log")
    idmap_s = round(time.time() - t1, 1)
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for fn in cache_files:
            _link_or_copy(work / fn, cache_dir / fn)
    return dict(cache_hit=False, export_s=round(t1 - t0, 1), idmap_s=idmap_s,
                rrg_bytes=(work / "rrg.bin").stat().st_size,
                manifest_bytes=(work / "rrg.manifest.txt").stat().st_size)


def gpu_snapshot():
    try:
        return subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu", "--format=csv,noheader"],
            text=True, timeout=20).strip().splitlines()
    except Exception as exc:  # pragma: no cover
        return [f"nvidia-smi failed: {exc}"]


def shm_dirs():
    return {p.name for p in Path("/dev/shm").glob("fides-f4-cuda-corridor-*")}


def kill_leftovers(work):
    out = subprocess.run(["pgrep", "-f", str(work)], capture_output=True, text=True).stdout.split()
    for pid in out:
        if int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def grab(pattern, text, cast=float, last=False):
    rows = re.findall(pattern, text, re.M)
    if not rows:
        return None
    value = rows[-1] if last else rows[0]
    return cast(value) if cast else value


def cold_check(work, dcp_out):
    command = read_json(work / "native_multichannel.command.json")
    if not command or not dcp_out.is_file():
        return None
    argv = command["argv"]
    cp = argv[argv.index("-cp") + 1]
    log = work / "cold_dcp_check.log"
    started = time.time()
    with log.open("wb") as stream:
        subprocess.run([argv[0], "-Xmx256G", "-cp", cp, "F4BaselineDcpCheck", str(dcp_out)],
                       stdout=stream, stderr=subprocess.STDOUT, cwd=work)
    rows = re.findall(r"F4_BASE_DCP unroutedPins=(\d+) sharedPipNodes=(\d+) pips=(\d+)",
                      log.read_text(errors="replace"))
    return dict(seconds=round(time.time() - started, 2),
                line=None if not rows else dict(unroutedPins=int(rows[-1][0]),
                                                sharedPipNodes=int(rows[-1][1]), pips=int(rows[-1][2])))


def collect(case, work, dcp_in, rc, wall, timed_out):
    status = read_json(work / "status.json") or {}
    result = read_json(work / "result.json") or {}
    log_path = work / "native_multichannel.log"
    log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    rounds = re.findall(r"^F4_V37_ROUND\s+iter=(\d+).*?\bover=(\d+)", log, re.M)
    dcp_out = work / f"{case}_gpu_full.dcp"
    inproc = re.findall(r"F4_BASE_DCP unroutedPins=(\d+) sharedPipNodes=(\d+) pips=(\d+)", log)
    direct = result.get("direct_precompute") or {}
    row = dict(
        engine=ENGINE, case=case, finished_at=now(), input_bytes=dcp_in.stat().st_size,
        worker_rc=rc, timed_out=timed_out, wall_s=round(wall, 2),
        status=status.get("status"), stage=status.get("stage"), error=status.get("error"),
        e2e_s=result.get("end_to_end_with_preparation_seconds"),
        route_wall_s=(result["route_wall_ns"] / 1e9) if "route_wall_ns" in result else None,
        route_design_s=grab(r"^Route design:\s+([\d.]+)s", log),
        wirelength=grab(r"^Total wirelength:\s+(\d+)", log, int),
        nodes_popped=grab(r"^Nodes popped:\s+(\d+)", log, int),
        routing_profile=grab(r"Auto-tuned routing profile by design size \((.*?)\)", log, None),
        rounds=len(rounds), last_round_iter=int(rounds[-1][0]) if rounds else None,
        last_round_over=int(rounds[-1][1]) if rounds else None,
        direct_connections=direct.get("direct_connections"),
        feature_rows=(result.get("live_features") or {}).get("rows"),
        gpu_commits=result.get("v37_gpu_commits"), gpu_pops=result.get("v37_gpu_pops"),
        gpu_ns=result.get("v37_gpu_ns"), gpu_waves=result.get("v37_gpu_waves"),
        legality_mode=result.get("legality_mode"),
        inproc_check_stats=grab(r"F4_V66_INPROC_DCP_CHECK\s+(.*)", log, str),
        inproc_legality=None if not inproc else dict(
            unroutedPins=int(inproc[-1][0]), sharedPipNodes=int(inproc[-1][1]), pips=int(inproc[-1][2])),
        out_dcp_bytes=dcp_out.stat().st_size if dcp_out.is_file() else None,
        out_dcp_sha256=result.get("dcp_sha256"), work=str(work),
    )
    if dcp_out.is_file() and status.get("status") == "passed":
        row["cold_check"] = cold_check(work, dcp_out)
    return row


def prepare_case(case, force, use_cache=True):
    """Clone the configured engine build, patch the harness, and dump or reuse the design's RRG."""
    SUMMARY.mkdir(exist_ok=True)
    work, dcp_in = clone(case, force)
    info = export_rrg(work, dcp_in, case, use_cache=use_cache)
    (work / "prepared.json").write_text(json.dumps(dict(engine=ENGINE, case=case, prepared_at=now(), **info), indent=1) + "\n")
    with (SUMMARY / "prepare.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(engine=ENGINE, case=case, prepared_at=now(), **info)) + "\n")
    print(now(), ENGINE, case, "PREPARED", json.dumps(info), flush=True)


def run_case(case, timeout, keep_rrg=False):
    SUMMARY.mkdir(exist_ok=True)
    work = M / f"{ENGINE}all13-{case}"
    if not (work / "prepared.json").is_file():
        raise RuntimeError(f"{work} is not prepared (run --phase prepare first)")
    dcp_in = Path(json.loads((work / "case.json").read_text())["dcp"])
    prepared = json.loads((work / "prepared.json").read_text())
    before = shm_dirs()
    gpus = gpu_snapshot()
    print(now(), ENGINE, case, "GPU before:", gpus, flush=True)
    with (work / "worker.log").open("xb") as log:
        proc = subprocess.Popen([sys.executable, "-B", str(work / "worker.py")], cwd=work,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
    started = time.time()
    timed_out = False
    while proc.poll() is None:
        if time.time() - started > timeout:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(5)
    rc = proc.wait()
    wall = time.time() - started
    kill_leftovers(work)
    for name in shm_dirs() - before:
        shutil.rmtree(Path("/dev/shm") / name, ignore_errors=True)
    row = collect(case, work, dcp_in, rc, wall, timed_out)
    row["gpu_before"] = gpus
    row["prepare"] = prepared
    for big in ["features.live.bin"] + ([] if keep_rrg else ["rrg.bin", "rrg.manifest.txt"]):
        if (work / big).is_file():
            (work / big).unlink()
    with (SUMMARY / "summary.jsonl").open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    print(now(), ENGINE, case, json.dumps({k: row[k] for k in (
        "status", "stage", "e2e_s", "route_design_s", "wirelength", "rounds", "last_round_over",
        "inproc_legality", "cold_check", "error") if k in row}), flush=True)
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="v73")
    parser.add_argument("--phase", choices=("prepare", "run", "both"), default="both")
    parser.add_argument("--keep-rrg", action="store_true")
    parser.add_argument("--no-rrg-cache", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=float, default=5400)
    parser.add_argument("cases", nargs="+")
    args = parser.parse_args()
    configure_engine(args.engine)
    for name in args.cases:
        try:
            if args.phase in ("prepare", "both"):
                prepare_case(name, args.force, use_cache=not args.no_rrg_cache)
            if args.phase in ("run", "both"):
                run_case(name, args.timeout, args.keep_rrg)
        except Exception as exc:  # keep going with the remaining cases
            SUMMARY.mkdir(exist_ok=True)
            with (SUMMARY / "summary.jsonl").open("a") as stream:
                stream.write(json.dumps(dict(engine=ENGINE, case=name, finished_at=now(), status="driver_error",
                                             error=repr(exc))) + "\n")
            print(now(), ENGINE, name, "DRIVER_ERROR", repr(exc), flush=True)
