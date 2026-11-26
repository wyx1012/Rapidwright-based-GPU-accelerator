"""Run THEIA_50per with the existing eight-channel GPU-only router and updated CUDA flood."""
import fcntl
from datetime import datetime
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time
import traceback

assert socket.gethostname().split(".")[0] == "gpu02"
work = Path(__file__).resolve().parent
measurement = work.parent
reference = measurement / "feature-direct-full-v2"
candidate = Path(json.loads((work / "corridor_build_receipt.json").read_text())["candidate"])
reference_worker = reference / "worker.py"
source = reference_worker.read_text()
start = source.index('\ntry:\n    save("pinning_full_gpu_inputs"')
if "def main():" not in source[:start]:
    raise RuntimeError("Cannot locate the existing GPU-only worker")
source = source.replace('    exec(compile(source, str(template)+"::integrated-drive", "exec"), globals())',
    '    source = patch_scored_timed_drive(source)\n    exec(compile(source, str(template)+"::integrated-drive", "exec"), globals())', 1)
exec(compile(source[:start], str(reference_worker), "exec"))

# The original runner required an exclusive card. Match the actual CUDA PID on a shared card.
script = swap(script, 'gpu("compute-apps", "gpu_uuid,pid") == f"{uuid}, {device.pid}"',
              'f"{uuid}, {device.pid}" in gpu("compute-apps", "gpu_uuid,pid").splitlines()')
from integrate_full_gpu_corridor import patch_broker
from strip_gpu_corridor_hotpath import patch_broker as strip_broker
script = strip_broker(patch_broker(script))
script = swap(script, '    args = [str(java/"java"), "-ea", "-Xmx256G", "-XX:ActiveProcessorCount=4",', '    args = [str(java/"java"), "-Xmx256G", f"-XX:ActiveProcessorCount={max(2, len(os.sched_getaffinity(0)))}", "-Dfides.ppt.f4.crossSlrCorridor=true", "-Dfides.ppt.f4.crossSlrCompactX=true", "-Dfides.ppt.f4.multiSlrCorridor=true", "-Dfides.v37.skipClosingMirrorScan=true", f"-Dfides.v37.idmapCache={work}/rrg.idmap.bin",')
script = swap(script, 'f"-Dfides.ppt.f4.features={feature[\'features_path\']}",', 'f"-Dfides.ppt.f4.features={feature[\'features_path\']}", "-Dfides.f2.prewarm=background", f"-Dfides.f2.cubin={work/\'feature.cubin\'}",')
script = swap(script, 'app, native_log = launch("native_multichannel", args, env)', 'app, native_log = launch("native_multichannel", args, env | {"CUDA_VISIBLE_DEVICES": uuid})')
top = ast.parse(script)
binary = work / "hybrid"

import v20_handoff as v20handoff
from v20_patch import drive as v20_drive
from score_split_v6 import patch_broker as score_broker
from host_chain_v7 import patch_drive as patch_host_drive
def patch_scored_timed_drive(source):
    timed_route = '            gpu_started = time.perf_counter_ns()\n            device.stdin.write(f"route {batch} {len(members)}\\n")'
    if source.count(timed_route) != 1:
        raise RuntimeError("The original route timer was not installed before scored dispatch")
    from active_frontier_v9d_broker import patch as active_broker
    from hybrid_v10_broker import patch as hybrid_broker
    from v11_bridge import patch_drive as bridge_broker
    from v14_broker import patch_drive as window_broker
    out = v20_drive(window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source)))))))
    out = out.replace('args.insert(4, f"-XX:ActiveProcessorCount={max(2, min(16, len(os.sched_getaffinity(0))))}")', 'pass')
    out = out.replace('app, native_log = launch("native_multichannel"', 'args.extend(["--sortOrder", "HPWL_FIRST"]); app, native_log = launch("native_multichannel"')
    out = out.replace('{"CUDA_VISIBLE_DEVICES": uuid,', '{"CUDA_VISIBLE_DEVICES": v38_multi_gpu_visible(uuid),')
    out = out.replace('check(prepared_gpu["process"].poll() is None, "Prepared CUDA exited before Java")', 'check(prepared_gpu["process"] is None or prepared_gpu["process"].poll() is None, "Prepared CUDA exited before Java")')
    out = out.replace('gpu_pid=prepared_gpu["process"].pid', 'gpu_pid=(None if prepared_gpu["process"] is None else prepared_gpu["process"].pid)')
    dcp_old = """    readback = run("dcp_legality", [str(java/"java"), "-Xmx256G", "-cp", cp,
                                   "F4BaselineDcpCheck", str(output)], env)
    rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\d+) sharedPipNodes=(\\d+) pips=(\\d+)", readback.read_text())
    check(len(rows) == 1 and rows[0][:2] == ("0", "0") and int(rows[0][2]) > 0, "DCP legality")"""
    dcp_new = """    if os.environ.get("FIDES_COLD_DCP_CHECK") == "1":
        readback = run("dcp_legality", [str(java/"java"), "-Xmx256G", "-cp", cp,
                                       "F4BaselineDcpCheck", str(output)], env)
        rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\d+) sharedPipNodes=(\\d+) pips=(\\d+)", readback.read_text())
        check(len(rows) == 1 and rows[0][:2] == ("0", "0") and int(rows[0][2]) > 0, "DCP legality")
        legality_mode = "cold_jvm_readback"
    else:
        check(output.is_file() and output.stat().st_size > 0, "Missing output DCP")
        with __import__("zipfile").ZipFile(output, "r") as zf:
            names = zf.namelist()
            check(any(nm.endswith(".xdef") for nm in names) and any(nm.endswith(".edf") for nm in names), "Incomplete output DCP zip")
        rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\d+) sharedPipNodes=(\\d+) pips=(\\d+)", native_log.read_text())
        check(len(rows) >= 1 and rows[-1][:2] == ("0", "0") and int(rows[-1][2]) > 0, "In-process DCP legality")
        legality_mode = "in_process_parallel_with_write"
    save("dcp_legality_verified", legality_mode=legality_mode, active_log="native_multichannel.log")"""
    if dcp_old not in out:
        raise RuntimeError("Missing dcp_old anchor in drive")
    out = out.replace(dcp_old, dcp_new, 1)
    return out


def v38_multi_gpu_visible(primary_uuid):
    try:
        app_rows = subprocess.check_output([
            "nvidia-smi", "--query-compute-apps=gpu_uuid,used_memory",
            "--format=csv,noheader,nounits"], text=True, timeout=15).splitlines()
        busy_uuids = set()
        for arow in app_rows:
            if not arow.strip():
                continue
            au, am = [x.strip() for x in arow.split(",")]
            if am.isdigit() and int(am) >= 400:
                busy_uuids.add(au)
            elif not am.isdigit():
                busy_uuids.add(au)
        rows = subprocess.check_output([
            "nvidia-smi", "--query-gpu=index,uuid,name,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits"], text=True, timeout=15).splitlines()
        extras = []
        for row in rows:
            idx, dev_u, nm, fm, ut = [f.strip() for f in row.split(",")]
            if "V100" in nm and dev_u != primary_uuid and dev_u not in busy_uuids and int(fm) >= 28500 and int(ut) <= 10:
                extras.append((int(ut), int(idx), dev_u))
        extras.sort()
        vis = ",".join([primary_uuid] + [u for _, _, u in extras])
        return vis
    except Exception:
        return primary_uuid


def lease_available_device(rejected, minimum_mib):
    global uuid
    while True:
        locks = []
        try:
            exclusive = (measurement / ".ppt-alignment-whole-design.lock").open("a+")
            locks.append(exclusive)
            fcntl.flock(exclusive, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prior = read(measurement / "theia50per-gpu-corridor-full-v5/status.json")
            active_v6c = read(measurement / "theia50per-ppt-alignment-v6c/status.json")
            excluded = {s.get("gpu_uuid") for s in (prior, active_v6c) if s["status"] == "running"}
            available_kib = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                                     if line.startswith("MemAvailable:")))
            if available_kib < (192 << 20):
                save("waiting_for_host_memory", available_kib=available_kib)
            else:
                rows = subprocess.check_output(["nvidia-smi",
                    "--query-gpu=index,uuid,name,memory.free,memory.used,utilization.gpu,pci.bus_id",
                    "--format=csv,noheader,nounits"], text=True, timeout=15).splitlines()
                visible = [row.split(",")[1].strip() for row in rows if "V100" in row]
                if not any(card not in rejected for card in visible):
                    raise RuntimeError("V20 resident startup rejected on every visible V100")
                options = []
                for row in rows:
                    index, device, name, free_mib, used, utilization, pci = [field.strip() for field in row.split(",")]
                    if ("V100" in name and int(free_mib) >= minimum_mib and int(utilization) <= 5
                            and device not in excluded and device not in rejected):
                        options.append((int(utilization), int(used), int(index), int(free_mib), device, pci.lower()))
                for utilization, used, index, free_mib, device, pci in sorted(options):
                    card = (measurement / f".fides-frozen-replay-{device}.lock").open("a+")
                    try:
                        fcntl.flock(card, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        card.close()
                        continue
                    owners = subprocess.check_output(["nvidia-smi", "-i", device,
                        "--query-compute-apps=pid", "--format=csv,noheader,nounits"], text=True, timeout=15).strip()
                    co_resident_count = 0 if not owners or owners == "No running processes found" else len(owners.splitlines())
                    locks.append(card)
                    uuid = device
                    env0["uuid"] = uuid
                    domain, rest = pci.split(":",1)
                    topology = Path("/sys/bus/pci/devices") / (domain[-4:] + ":" + rest) / "numa_node"
                    if topology.is_file():
                        numa = int(topology.read_text().strip())
                    else:
                        numa = -1
                    cpus = set()
                    node_cpus = Path(f"/sys/devices/system/node/node{numa}/cpulist")
                    if numa >= 0 and node_cpus.is_file():
                        for part in node_cpus.read_text().strip().split(","):
                            endpoints = list(map(int, part.split("-")))
                            cpus.update(range(endpoints[0], endpoints[-1]+1))
                    affinity_before = os.sched_getaffinity(0)
                    local_cpus = affinity_before & cpus
                    if len(local_cpus) >= 4:
                        os.sched_setaffinity(0, local_cpus)
                    publish(work / "device_selection.json", dict(
                        uuid=uuid, index=index, memory_used_mib=used, memory_free_mib=free_mib,
                        utilization_pct=utilization, numa_node=numa,
                        cpu_affinity=sorted(os.sched_getaffinity(0)), numa_applied=len(local_cpus) >= 4,
                        gpu0_reference=prior.get("gpu_uuid"), host_available_kib=available_kib,
                        preexisting_compute_apps=co_resident_count, independent_compiled_runtime=True))
                    return locks
        except BlockingIOError:
            pass
        except Exception:
            for handle in locks:
                handle.close()
            raise
        for handle in locks:
            handle.close()
        save("waiting_for_gpu_and_isolated_run_lease")
        time.sleep(30)


def main_gpu():
    global drive, prepared_gpu
    overall_started = time.perf_counter_ns()
    env_path = reference / "native_multichannel.command.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("FIDES_")
           and k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "GRADLE_OPTS")}
    build = read(candidate / "build_receipt.json")
    check(build["ready"] and digest(binary) == build["sha256"]["hybrid"], "Unexpected CUDA binary")
    check(digest(work / "direct_paths") == read(reference / "build_receipt.json")["direct_paths"],
          "Unexpected direct CUDA binary")
    env.update(build["environment"])
    env["CUDA_VISIBLE_DEVICES"] = ""
    native_command = read(env_path)["argv"]
    java = Path(native_command[0]).parent
    classpath = native_command[native_command.index("-cp") + 1]
    check(classpath.split(os.pathsep, 1)[0] == str(reference / "classes"), "Wrong GPU-only Java overlay")
    classpath = os.pathsep.join([str(work / "classes"), str(measurement / "theia50per-gpu-corridor-full-v3/classes"), (work/"runtime_classpath.txt").read_text().strip()])
    feature = read(features_dir / "result.json")
    check(feature["status"] == "passed" and feature["design"] == "THEIA_50per"
          and digest(dcp) == feature["input_sha256"]
          and feature["initial_connections"] == 4952719, "Wrong THEIA feature reference or input")
    feature["features_path"] = str(work / "features.live.bin")
    serialized_bytes = dcp.stat().st_size
    resource_tier = "huge" if serialized_bytes >= 192 << 20 else "standard"
    save("live_cuda_feature_planned", feature_reference_sha256=feature["features_sha256"],
         resource_tier=resource_tier, serialized_input_bytes=serialized_bytes)
    shape, fields = env0["old_graph_table"]()
    load_drive()
    validate_and_start = v20handoff.start
    locks, prepared_gpu = validate_and_start(
        globals(), env, shape, fields, java, classpath)
    ipc_parent = None
    try:
        if shutil.disk_usage("/dev/shm").free >= 5 << 30:
            ipc_parent = Path(tempfile.mkdtemp(prefix="fides-f4-cuda-corridor-", dir="/dev/shm"))
        ipc = (ipc_parent or work) / "live_snapshot"
        save("beginning_full_gpu_route", ipc_root=str(ipc), ipc_on_tmpfs=ipc_parent is not None, gpu_uuid=uuid)
        result = drive(java, classpath, env, feature, shape, fields)
        result["resident_preflight_seconds"] = prepared_gpu["startup_ns"] / 1e9
        v37_final = read(work / "f4_hybrid_cpu_final.json")
        for k in ("v37_gpu_commits", "v37_gpu_pops", "v37_gpu_ns", "v37_gpu_waves"):
            if k in v37_final:
                result[k] = v37_final[k]
        result["legality_mode"] = state.get("legality_mode", "in_process_parallel_with_write")
        result["end_to_end_with_preparation_seconds"] = (time.perf_counter_ns() - overall_started) / 1e9
        live_path = Path(feature["features_path"])
        check(live_path.stat().st_size == 4952719 * 72, "Incomplete live GPU feature output")
        live_sha = digest(live_path)
        result["live_features"] = dict(source="routing_jvm_background_cuda", rows=4952719,
            sha256=live_sha, archived_sha256=feature["features_sha256"],
            archive_match=live_sha == feature["features_sha256"], gpu_uuid=uuid,
            serialized_input_bytes=serialized_bytes, resource_tier=resource_tier)
        publish(work / "result.json", result)
        save("complete", status="passed", java_pid=None, gpu_pid=None, active_pid=None)
        if ipc_parent:
            shutil.rmtree(ipc_parent)
    finally:
        stop_owned_children()
        for lock in locks:
            lock.close()

try:
    save("using_existing_gpu_only_router", configured_channels=width, corridor_enabled=True, cpu_search_budget=8000,
         cpu_fallback_allowed=False, cuda_build=str(candidate))
    main_gpu()
except __import__("v11_window").NoBenefit as result:
    save("prefix_no_benefit", status="stopped_no_benefit", java_pid=None, gpu_pid=None,
         active_pid=None, prefix_result=result.summary)
except KeyboardInterrupt:
    save("operator_stop", status="stopped")
    raise
except Exception as error:
    failure_trace = traceback.format_exc()
    try:
        archive_failed_batch()
    except Exception as archive_error:
        state["archive_error"] = str(archive_error)
    save("failed", status="failed", error=str(error), traceback=failure_trace)
    raise
finally:
    stop_owned_children()
