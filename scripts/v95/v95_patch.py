"""v95 on v94 Java stack + Dual-GPU Spatial Region Disjoint Partitioning & 64-Cell Wave Spreading."""
import sys
from pathlib import Path

_V94_DIR = str(Path(__file__).resolve().parent.parent / "v94")
if _V94_DIR not in sys.path:
    sys.path.insert(0, _V94_DIR)

import v94_patch as v94  # noqa: E402


def cuda(src):
    return v94.cuda(src)


def node(src):
    return v94.node(src)


def broker(src):
    return v94.broker(src)


def diversion(src):
    return v94.diversion(src)


def f2prewarm(src):
    return v94.f2prewarm(src)


def handoff(src):
    return v94.handoff(src)


def worker(src):
    return v94.worker(src)


def partial_router(src):
    return v94.partial_router(src)


def corridor(src):
    return v94.corridor(src)


def rrg_loader(src):
    return v94.rrg_loader(src)


def idmap(src):
    return v94.idmap(src)


def gate(src):
    return v94.gate(src)


def router(src):
    return v94.router(src)
