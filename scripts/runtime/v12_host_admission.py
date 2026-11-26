"""Cover independent branches of the same net without charging scratch on ordinary searches."""
from v11_window import replace_once as once


def patch_node(source):
    source = source.replace("ACTIVE_SCRATCH.get()", "f4ActiveScratch()")
    source = once(source, "    private static final ThreadLocal<SearchScratch> ACTIVE_SCRATCH = new ThreadLocal<>();",
        """    private static final ThreadLocal<SearchScratch> ACTIVE_SCRATCH = new ThreadLocal<>();
    private static final java.util.concurrent.atomic.AtomicInteger F4_ACTIVE_SCRATCH =
            new java.util.concurrent.atomic.AtomicInteger();

    private static SearchScratch f4ActiveScratch() {
        return F4_ACTIVE_SCRATCH.get() == 0 ? null : ACTIVE_SCRATCH.get();
    }""")
    source = source.replace("            ACTIVE_SCRATCH.set(this);",
                            "            ACTIVE_SCRATCH.set(this);\n            F4_ACTIVE_SCRATCH.incrementAndGet();")
    source = source.replace("            ACTIVE_SCRATCH.remove();",
                            "            ACTIVE_SCRATCH.remove();\n            F4_ACTIVE_SCRATCH.decrementAndGet();")
    old = """        recordPairRead();
        SearchScratch scratch = f4ActiveScratch();"""
    new = """        SearchScratch scratch = f4ActiveScratch();
        if (scratch != null && scratch.capturePairFootprint) scratch.readNodes.add(this);"""
    if source.count(old) != 2:
        raise RuntimeError("F4 getter fast-path anchor changed")
    return source.replace(old, new)


def patch_router(source):
    source = once(source, "    private long f4HostPairs,", """    private NetWrapper f4LastRejectedSharedNet;
    private long f4HostPairs,""")
    start = source.index("    private boolean f4CanSpeculatePair(")
    end = source.index("    private F4HostSearch f4SearchHostReadOnly", start)
    source = source[:start] + """    private boolean f4CanSpeculatePair(Connection first, Connection second) {
        if (second == null || !config.isUseBoundingBox() || config.isTimingDriven()
                || !first.getSourceRnode().f4CachedChildren() || !second.getSourceRnode().f4CachedChildren()
                || second.getSourceRnode() == second.getSinkRnode() || second.hasAltSinks()
                || !second.getSinkRnode().getType().isAnyExclusiveSink() || !shouldRoute(second))
            return false;
        if (first.getNetWrapper() == second.getNetWrapper())
            return first.getNetWrapper() != f4LastRejectedSharedNet && f4CpuWindowRemaining >= 2
                    && first.isCrossSLR() == second.isCrossSLR()
                    && first.getSourceRnode() == second.getSourceRnode()
                    && first.getSinkRnode() != second.getSinkRnode()
                    && first.getSourceRnode().getPrev() == null;
        return f4MayProbeNextNet(second)
                && (first.getXMaxBB() <= second.getXMinBB() || second.getXMaxBB() <= first.getXMinBB()
                || first.getYMaxBB() <= second.getYMinBB() || second.getYMaxBB() <= first.getYMinBB());
    }

    private static boolean f4IntersectsExcept(Collection<RouteNode> first, Collection<RouteNode> second,
                                             RouteNode permitted) {
        for (RouteNode node : first)
            if (node != permitted && second.contains(node)) return true;
        return false;
    }

    private static boolean f4OnlyTerminalSource(List<RouteNode> path, RouteNode source) {
        return path.isEmpty() || path.indexOf(source) == path.size() - 1;
    }

    // For an exclusive-sink route the source terminal is not an occupancy
    // user. Each search reseeds its private visit tag and costs at that
    // terminal. Only that one shared node can be excluded from dependencies.
    private static RouteNode f4PermittedSharedSource(F4HostSearch a, F4HostSearch b) {
        Connection first = a.state.connection;
        Connection second = b.state.connection;
        RouteNode source = first.getSourceRnode();
        List<RouteNode> oldA = first.getRnodes();
        List<RouteNode> oldB = second.getRnodes();
        if (source != second.getSourceRnode() || source.getPrev() != null
                || first.getNetWrapper() != second.getNetWrapper() || first.hasAltSinks() || second.hasAltSinks()
                || (!oldA.isEmpty() && oldA.get(0) != first.getSinkRnode())
                || (!oldB.isEmpty() && oldB.get(0) != second.getSinkRnode())
                || a.path.get(0) != first.getSinkRnode() || b.path.get(0) != second.getSinkRnode()
                || !f4OnlyTerminalSource(oldA, source) || !f4OnlyTerminalSource(oldB, source)
                || !f4OnlyTerminalSource(a.path, source) || !f4OnlyTerminalSource(b.path, source))
            return null;
        return source;
    }

""" + source[end:]
    start = source.index("        if (a == null || b == null) {")
    end = source.index("        return new F4HostSearch[]{a, b};", start)
    source = source[:start] + """        if (a == null || b == null) {
            ++f4HostColdOrMisses;
            if (first.getNetWrapper() == second.getNetWrapper()) f4LastRejectedSharedNet = first.getNetWrapper();
            return new F4HostSearch[]{a, null};
        }
        RouteNode shared = f4PermittedSharedSource(a, b);
        var reads = b.scratch.readNodes();
        if (f4IntersectsExcept(a.scratch.writeNodes(), reads, shared)
                || f4IntersectsExcept(a.state.connection.getRnodes(), reads, shared)
                || f4IntersectsExcept(a.path, reads, shared)
                || f4IntersectsExcept(a.path, b.path, shared)
                || f4IntersectsExcept(a.state.connection.getRnodes(), b.path, shared)) {
            ++f4HostConflicts;
            if (first.getNetWrapper() == second.getNetWrapper()) f4LastRejectedSharedNet = first.getNetWrapper();
            b = null;
        }
""" + source[end:]
    return once(source, """    private void f4EndBenefitRound() {
        f4FinishCpuWindow(false);""",
        """    private void f4EndBenefitRound() {
        f4LastRejectedSharedNet = null;
        f4FinishCpuWindow(false);""")
