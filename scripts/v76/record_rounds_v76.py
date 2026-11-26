#!/usr/bin/env python3
"""Daemon monitor that records every completed v37 routing round (including GPU + CPU metrics) and post-route check on gpu02."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORK = Path(__file__).resolve().parent
LOG_PATH = WORK / "native_multichannel.log"
DCP_LOG_PATH = WORK / "dcp_legality.log"
STATUS_JSON = WORK / "status.json"
MD_OUT = WORK / "v37_round_history.md"
JSONL_OUT = WORK / "v37_round_history.jsonl"
STATUS_OUT = WORK / "recorder_status.json"

ROUND_RE = re.compile(
    r"^F4_V37_ROUND\s+iter=(\d+)\s+active=(\d+)\s+routed=(\d+)\s+skip=(\d+)\s+"
    r"gpu_ok=(\d+)\s+cpu_ok=(\d+)\s+par=(\d+)\s+ser=(\d+)\s+retries=(\d+)\s+same_ok=(\d+)\s+"
    r"base_h=(\d+)\s+rel_h=(\d+)\s+fb=(\d+)\s+unrout=(\d+)\s+"
    r"over=(\d+)\s+excess=(\d+)\s+pops=(\d+)\s+gpu_pops=(\d+)\s+waves=(\d+)\s+"
    r"corridor_s=([\d.]+)\s+gpu_s=([\d.]+)\s+cpp_s=([\d.]+)"
)
RW_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$"
)
WAVE_RE = re.compile(r"^F4_V37_WAVE(?:_CPU)?\s+(.+)$")

STOP = False


def _handle_sig(_signum, _frame):
    global STOP
    STOP = True


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _live_procs() -> list[dict]:
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,pcpu,rss,etime,comm,args"],
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    res = []
    target = str(WORK)
    my_pid = os.getpid()
    for line in out.splitlines()[1:]:
        if target not in line or "record_rounds" in line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid = int(parts[0])
        if pid == my_pid:
            continue
        res.append(
            {
                "pid": pid,
                "pcpu": parts[1],
                "rss_mb": round(int(parts[2]) / 1024.0, 1),
                "etime": parts[3],
                "comm": parts[4],
            }
        )
    return res


def _gpu_status() -> str | None:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.free,utilization.gpu,power.draw",
                "--format=csv,noheader",
            ],
            text=True,
            timeout=5,
        ).strip()
        return " | ".join(line.strip() for line in out.splitlines())
    except Exception:
        return None


def _parse_log(text: str):
    rounds: dict[int, dict] = {}
    latest_wave = None
    tail_events: list[str] = []
    finished = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("F4_PPT11_ROUND "):
            continue
        m = ROUND_RE.match(line)
        if m:
            it = int(m.group(1))
            rounds[it] = {
                "iter": it,
                "active": int(m.group(2)),
                "routed": int(m.group(3)),
                "skip": int(m.group(4)),
                "gpu_ok": int(m.group(5)),
                "cpu_ok": int(m.group(6)),
                "par": int(m.group(7)),
                "ser": int(m.group(8)),
                "retries": int(m.group(9)),
                "same_ok": int(m.group(10)),
                "base_h": int(m.group(11)),
                "rel_h": int(m.group(12)),
                "fb": int(m.group(13)),
                "unrout": int(m.group(14)),
                "over": int(m.group(15)),
                "excess": int(m.group(16)),
                "pops": int(m.group(17)),
                "gpu_pops": int(m.group(18)),
                "waves": int(m.group(19)),
                "corridor_s": float(m.group(20)),
                "gpu_s": float(m.group(21)),
                "cpp_s": float(m.group(22)),
                "round_s": None,
            }
            latest_wave = None
            continue

        rm = RW_ROW_RE.match(line)
        if rm:
            it = int(rm.group(1))
            if it in rounds:
                rounds[it]["round_s"] = float(rm.group(6))
            continue

        wm = WAVE_RE.match(line)
        if wm:
            latest_wave = wm.group(1)
            continue

        if any(
            tok in line
            for tok in (
                "FIDES_V37_GPU",
                "FIDES_V40_GPU",
                "F4_V39_HOST",
                "F4_V41_META",
                "F4_V37_GPU_ROUND",
                "F4_V37_IDMAP_CACHE",
                "V37_MATERIALIZE",
                "FIDES_NATIVE_CPU v37",
                "F4_V37_ASSIGN_NODES",
                "F4_V37_FIX_ROUTES",
                "F4_V37_SETPIPS",
                "F4_V37_CHECKPIPS",
                "F4_V37_STATS_WL",
                "No PIP overlaps",
                "MIRROR RESULT",
                "UNROUTED",
                "Total wirelength",
                "Route design:",
                "Initialization:",
                "Routing:",
                "Nodes popped:",
                "fallbackAttempts",
                "unroutable=",
                "Wrote routed design",
                "Exception",
                "ERROR:",
                "CRITICAL",
            )
        ):
            if "INFO: Routing indirect" not in line and not line.startswith("------"):
                tail_events.append(line)
                if "Wrote routed design" in line:
                    finished = True

    if DCP_LOG_PATH.exists():
        for raw in DCP_LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "F4_BASE_DCP" in raw or "F4_V37_DCP_CHECK_TIMING" in raw or "ERROR" in raw:
                tail_events.append("[dcp_legality] " + raw.strip())

    run_status = None
    if STATUS_JSON.exists():
        try:
            st = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
            run_status = f"{st.get('status')} / {st.get('stage')}"
            if st.get("status") in ("passed", "failed"):
                finished = True
        except Exception:
            pass

    return rounds, latest_wave, tail_events[-40:], finished, run_status


def _atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _render_md(
    rounds: dict[int, dict],
    first_seen: dict[int, str],
    latest_wave: str | None,
    tail_events: list[str],
    procs: list[dict],
    gpu_stat: str | None,
    finished: bool,
    run_status: str | None,
) -> str:
    now = _now_iso()
    cum_s = 0.0
    total_pops = 0
    total_gpu_pops = 0
    total_gpu_ok = 0
    total_cpu_ok = 0
    total_gpu_s = 0.0
    total_retries = 0
    total_same_ok = 0

    lines = [
        f"# v37.1 每轮运行自动记录 (`{WORK.name}`, Route B + v36 Tier-B1 8M / Pass 3 >= 160)",
        "",
        f"- **最后检查时间**: `{now}`",
        f"- **运行状态**: `{run_status or ('已完成' if finished else ('运行中' if procs else '已停止/未检测到进程'))}`",
    ]
    if procs:
        pstr = ", ".join(
            f"{p['comm']}(pid={p['pid']}, cpu={p['pcpu']}%, rss={p['rss_mb']}MB, etime={p['etime']})"
            for p in procs
        )
        lines.append(f"- **存活进程**: `{pstr}`")
    if gpu_stat:
        lines.append(f"- **V100 GPU 实时状态**: `{gpu_stat}`")
    if latest_wave:
        lines.append(f"- **当前轮中采样 (`F4_V37_WAVE`)**: `{latest_wave}`")
    lines.extend(
        [
            "",
            "| Iter | 记录时间 | active | routed | skip | gpu_ok | cpu_ok | waves | retries | same_ok | base_h | rel_h | overused | pops | gpu_pops | corridor_s | gpu_s | cpp_s | round_s | cum_s |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    for it in sorted(rounds):
        r = rounds[it]
        dt = r["round_s"] if r["round_s"] is not None else (r["corridor_s"] + r["cpp_s"])
        cum_s += dt
        total_pops += r["pops"]
        total_gpu_pops += r["gpu_pops"]
        total_gpu_ok += r["gpu_ok"]
        total_cpu_ok += r["cpu_ok"]
        total_gpu_s += r["gpu_s"]
        total_retries += r["retries"]
        total_same_ok += r["same_ok"]
        rs = f"{r['round_s']:.2f}" if r["round_s"] is not None else f"~{dt:.2f}"
        seen = first_seen.get(it, "-")
        lines.append(
            f"| {it} | {seen} | {r['active']:,} | {r['routed']:,} | {r['skip']:,} | "
            f"{r['gpu_ok']:,} | {r['cpu_ok']:,} | {r['waves']:,} | {r['retries']:,} | {r['same_ok']:,} | "
            f"{r['base_h']:,} | {r['rel_h']:,} | {r['over']:,} | {r['pops']/1e9:.3f}B | {r['gpu_pops']/1e9:.3f}B | "
            f"{r['corridor_s']:.2f} | {r['gpu_s']:.2f} | {r['cpp_s']:.2f} | {rs} | {cum_s:.2f} |"
        )

    lines.extend(
        [
            "",
            f"- **汇总（已完成 {len(rounds)} 轮）**: 累计轮次耗时 `{cum_s:.2f} s` (GPU 搜索 `{total_gpu_s:.2f} s`) | "
            f"GPU commits `{total_gpu_ok:,}` vs CPU commits `{total_cpu_ok:,}` | "
            f"累计 pops `{total_pops/1e9:.3f}B` (GPU pops `{total_gpu_pops/1e9:.3f}B`) | "
            f"累计 retries `{total_retries:,}` | 累计 `same_ok` `{total_same_ok:,}`",
        ]
    )
    if tail_events:
        lines.extend(["", "## 收尾 / 关键事件日志", "```text", *tail_events, "```"])
    lines.append("")
    return "\n".join(lines)


def main():
    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    poll_sec = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    max_hours = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    deadline = time.monotonic() + max_hours * 3600.0

    written_iters: set[int] = set()
    first_seen: dict[int, str] = {}
    if JSONL_OUT.exists():
        for raw in JSONL_OUT.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
                it = int(rec["iter"])
                written_iters.add(it)
                first_seen[it] = rec.get("recorded_at", "-")
            except Exception:
                pass

    last_sig = None
    dead_streak = 0

    while not STOP and time.monotonic() < deadline:
        procs = _live_procs()
        gpu_stat = _gpu_status()
        text = LOG_PATH.read_text(encoding="utf-8", errors="ignore") if LOG_PATH.exists() else ""
        rounds, latest_wave, tail_events, finished, run_status = _parse_log(text)

        new_records = []
        now = _now_iso()
        for it in sorted(rounds):
            ready = (rounds[it]["round_s"] is not None) or ((it + 1) in rounds) or finished
            if ready and it not in written_iters:
                rec = dict(rounds[it], recorded_at=now)
                new_records.append(rec)
                written_iters.add(it)
                first_seen[it] = now

        if new_records:
            with JSONL_OUT.open("a", encoding="utf-8") as fh:
                for rec in new_records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        sig = (len(rounds), max(rounds) if rounds else 0, latest_wave, len(procs), len(tail_events), finished, run_status)
        if sig != last_sig or new_records:
            md = _render_md(rounds, first_seen, latest_wave, tail_events, procs, gpu_stat, finished, run_status)
            _atomic_write(MD_OUT, md)
            _atomic_write(
                STATUS_OUT,
                json.dumps(
                    {
                        "updated_at": now,
                        "recorder_pid": os.getpid(),
                        "rounds_seen": len(rounds),
                        "last_round": max(rounds) if rounds else None,
                        "last_overused": rounds[max(rounds)]["over"] if rounds else None,
                        "live_procs": len(procs),
                        "gpu_status": gpu_stat,
                        "finished": finished,
                        "run_status": run_status,
                    },
                    indent=2,
                )
                + "\n",
            )
            last_sig = sig

        if not procs and finished:
            dead_streak += 1
            if dead_streak >= 2:
                break
        elif not procs:
            dead_streak += 1
            if dead_streak >= 4:
                break
        else:
            dead_streak = 0

        time.sleep(poll_sec)


if __name__ == "__main__":
    main()
