"""v92 on v91 Java stack + small-design GPU init gate + FIDES_* runtime passthrough."""
import v91_patch as v91


def once(source, anchor, updated):
    return v91.once(source, anchor, updated)


def cuda(src):
    return v91.cuda(src)


def node(src):
    return v91.node(src)


def broker(src):
    return v91.broker(src)


def diversion(src):
    return v91.diversion(src)


def f2prewarm(src):
    return v91.f2prewarm(src)


def handoff(src):
    return v91.handoff(src)


def worker(src):
    src = v91.worker(src)
    src = once(
        src,
        """    env = {k: v for k, v in os.environ.items() if not k.startswith("FIDES_")
           and k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "GRADLE_OPTS")}""",
        """    _fides_keep = ("FIDES_V38_MAX_GPUS", "FIDES_V37_DISABLE_GPU", "FIDES_V92_GPU_MIN_CONNS", "FIDES_COLD_DCP_CHECK")
    env = {k: v for k, v in os.environ.items()
           if (not k.startswith("FIDES_") or k in _fides_keep)
           and k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "GRADLE_OPTS")}""",
    )
    return src


def rrg_loader(src):
    return v91.rrg_loader(src)


def idmap(src):
    return v91.idmap(src)


def partial_router(src):
    return v91.partial_router(src)


def corridor(src):
    return v91.corridor(src)


def gate(src):
    src = v91.gate(src)
    src = once(
        src,
        """    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg) {
        if (f4NativeReady || workDir == null || mirror == null || cfg.isTimingDriven() || routingGraph.isVersal) return;""",
        """    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg) {
        f4NativeInit(workDir, routingGraph, cfg, -1);
    }

    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg, int expectedConns) {
        if (f4NativeReady || workDir == null || mirror == null || cfg.isTimingDriven() || routingGraph.isVersal) return;""",
    )
    src = once(
        src,
        """            FidesNativeCpu.load(libPath);""",
        """            FidesNativeCpu.load(libPath);
            FidesNativeCpu.nativeSetDesignConnCount(expectedConns);""",
    )
    return src


def router(src):
    src = v91.router(src)
    src = once(
        src,
        """            getFidesLaneGate().f4NativeInit(System.getProperty("fides.ppt.f4.work"), routingGraph, config);""",
        """            getFidesLaneGate().f4NativeInit(System.getProperty("fides.ppt.f4.work"), routingGraph, config, sortedIndirectConnections.size());""",
    )
    return src
