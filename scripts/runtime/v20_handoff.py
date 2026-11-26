"""Prepare the actual resident process before launching THEIA routing."""
import os
from pathlib import Path
import re
import struct
import time

MIB = 1 << 20
RESERVE = 1 << 30
START_COLUMNS = ("offset", "edge", "occupancy", "historicalCongestionCost", "presentCongestionCosts")
READY = re.compile(r"V20_CUDA column=context api=preflight-ready bytes=(\d+) rc=0 free=(\d+)")


def rrg_columns(path):
    with path.open("rb") as file:
        magic, version, n, m, count = struct.unpack("<8sIIII", file.read(24))
        if (magic, version) != (b"FIDESRRG", 2):
            raise RuntimeError("Unsupported static RRG format")
        columns = {}
        for _ in range(count):
            raw, kind, _, words, offset = struct.unpack("<32sIIQQ", file.read(56))
            columns[raw.rstrip(b"\0").decode("ascii")] = (words, offset, kind)
    pcc = columns["presentCongestionCosts"][0]
    for name, words, kind in zip(START_COLUMNS, (n + 1, m, n, n, pcc), (256, 256, 256, 257, 257)):
        length, offset, actual_kind = columns[name]
        if (length, actual_kind) != (words, kind) or offset + 4 * words > path.stat().st_size:
            raise RuntimeError(f"Unexpected static RRG column {name}")
    return n, m, pcc, [columns[name][1] for name in START_COLUMNS]


def wait_ready(path, device, app=None):
    while not path.is_file():
        if device.poll() is not None:
            if path.is_file():
                return
            raise RuntimeError(f"CUDA child exited {device.returncode} before {path.name}")
        if app is not None and app.poll() is not None:
            raise RuntimeError(f"Routing JVM exited {app.returncode} before {path.name}")
        time.sleep(0.02)


def start(ctx, env, shape, fields, java, classpath):
    work = ctx["work"]
    rrg = Path(ctx["rrg"])
    save, publish, launch = (ctx[key] for key in ("save", "publish", "launch"))
    n, m, pcc, offsets = rrg_columns(rrg)
    if (n, m) != tuple(shape[:2]):
        raise RuntimeError("Prepared RRG differs from the routing input")
    ords = fields(ctx["run"]("access_ordinals", [
        str(java / "java"), "-cp", classpath, "com.xilinx.rapidwright.rwroute.F4AccessOrdinals",
    ], env))
    slr_words = int((work / "parent-access-layout.txt").read_text().split()[0])
    layout = work / "preflight-access-layout.txt"
    values = ((slr_words, len(ords["TYPES"]), len(ords["TILES"])),
              ords["ORD"], ords["TYPES"], ords["TILES"], shape[2])
    layout.write_text("\n".join(" ".join(map(str, row)) for row in values) + "\n")
    shared = 4 * (n + 1 + m) + 13 * n + 4 * pcc + 4 * (
        10 * n + slr_words + len(ords["ORD"]) + len(ords["TYPES"]) + len(ords["TILES"]))
    lane = 4 * (n + m)
    target_lanes = min(8, int(ctx["width"]))
    minimum = 28 << 30
    rejected = set()
    original_affinity = os.sched_getaffinity(0)
    for attempt in range(1, 129):
        os.sched_setaffinity(0, original_affinity)
        locks = ctx["lease_available_device"](rejected, (minimum + MIB - 1) // MIB)
        uuid = ctx["uuid"]
        folder = work / f"resident-preflight-{attempt}"
        folder.mkdir()
        started = time.perf_counter_ns()
        free_mib_card = int(ctx["gpu"]("gpu", "memory.free"))
        device = None
        try:
            if (free_mib_card << 20) < (28 << 30):
                raise RuntimeError(f"out of memory: V100 free {free_mib_card} MiB < 28 GiB required for v37 in-process CUDA")
            os.sched_setaffinity(0, original_affinity)
            startup_ns = time.perf_counter_ns() - started
            receipt = dict(pid=None, gpu_uuid=uuid, attempt=attempt, startup_ns=startup_ns,
                           pcc=pcc, nodes=n, edges=m, slr_words=slr_words, resident_lanes=0,
                           inprocess_v37_cuda=True,
                           shared_bytes=0, per_lane_bytes=0,
                           actual_bytes=0, free_bytes=free_mib_card << 20,
                           reserved_headroom_bytes=RESERVE, no_route_issued=True, log=None,
                           restored_affinity=sorted(os.sched_getaffinity(0)))
            publish(work / "resident_preflight.json", receipt)
            save("resident_preflight_ready", gpu_pid=None, gpu_uuid=uuid,
                 resident_preflight=receipt)
            return locks, receipt | {"process": None}
        except Exception as error:
            tail = log.read_text(errors="replace").splitlines()[-8:] if device is not None else []
            publish(folder / "failure.json", dict(gpu_uuid=uuid, error=str(error), cuda_tail=tail))
            if device is not None and device.poll() is None:
                device.terminate()
                device.wait()
            for lock in locks:
                lock.close()
            if not re.search(r"out of memory|one[- ]GiB|scratch allocation exhausted", "\n".join(tail), re.I):
                raise
            rejected.add(uuid)
            save("resident_capacity_rejected", gpu_pid=None, gpu_uuid=uuid,
                 rejected_device_uuids=sorted(rejected), last_resident_error="\n".join(tail))
    raise RuntimeError("V20 exhausted distinct device attempts")


def load_first(device, root, prepared, app):
    expected = {
        "occ.i32le": prepared["nodes"], "same.i32le": prepared["nodes"],
        "hist.i32le": prepared["nodes"], "pcc.i32le": prepared["pcc"],
        "slr.i32le": prepared["slr_words"],
    }
    for name, words in expected.items():
        if (root / name).stat().st_size != words * 4:
            raise RuntimeError(f"Live first-snapshot size mismatch: {name}")
    device.stdin.write(f"load_first {root}\n")
    device.stdin.flush()
    wait_ready(root / "resident_snapshot_loaded", device, app)


def finish(device):
    if device is None:
        return
    if device.poll() is None:
        device.stdin.write("finish\n")
        device.stdin.flush()
    if device.wait() != 0:
        raise RuntimeError("Prepared CUDA failed before it could exit cleanly")

