"""First-round scheduler, original-order commits, and native concurrent cold adjacency."""
from v11_window import replace_once as once


def router(src, work):
    src = once(src, "    private void f4RouteDiverted(Collection<Connection> connections) {",
        (work / "F4FirstRound.inc").read_text() +
        "    private void f4RouteDiverted(Collection<Connection> connections) {")
    src = once(src, "        f4NextSpatial = 0;\n        for (int position",
        """        if (routeIteration == 1 && !armed && !routingGraph.isVersal
                && config.isUseBoundingBox() && !config.isTimingDriven()) {
            f4RouteFirstRound(ordered, gate);
            return;
        }
        f4NextSpatial = 0;
        for (int position""")
    src = once(src, "        int f4PrivatePopCeiling;",
        "        int f4PrivatePopCeiling;\n        long f4OutsideChecks;\n        int f4RetriedPops;")
    src = once(src, "        state.f4PrivatePopCeiling = 0;",
        "        state.f4PrivatePopCeiling = 0;\n        state.f4OutsideChecks = 0;\n        state.f4RetriedPops = 0;")
    src = once(src, """                if (!isAccessible(childRNode, connection)
                        || (state.f4Boundary != null && !state.f4Boundary.contains(childRNode))) {
                    continue;
                }""",
        """                if (!isAccessible(childRNode, connection)) continue;
                if (state.f4Boundary != null && !state.f4Boundary.contains(childRNode)) {
                    ++state.f4OutsideChecks;
                    continue;
                }""")
    src = once(src, """            ++state.nodesPopped;
            if (target.isTarget()) break;
            exploreAndExpand(state, target);""",
        """            ++state.nodesPopped;
            if (!state.f4PrivateSearch && f4First != null && (state.nodesPopped & 8191) == 0)
                f4First.report(routeIteration, f4FirstPosition, f4FirstTotal, state.nodesPopped, false);
            if (target.isTarget()) break;
            exploreAndExpand(state, target);""")
    return once(src, "        ++f4Widen;\n        f4RetireCorridor(state.connection);",
        "        ++f4Widen;\n        state.f4RetriedPops = state.nodesPopped;\n        f4RetireCorridor(state.connection);")


def node(src):
    src = once(src, "    protected RouteNode[] children;", "    protected volatile RouteNode[] children;")
    src = once(src, "        private boolean capturePairFootprint;",
        """        private boolean capturePairFootprint;
        private boolean f4AllowCold;
        long f4ColdBuilt, f4ColdNs;""")
    src = once(src, "            capturePairFootprint = false;\n            ACTIVE_SCRATCH.set(this);",
        """            capturePairFootprint = false;
            f4AllowCold = false;
            f4ColdBuilt = f4ColdNs = 0;
            ACTIVE_SCRATCH.set(this);""")
    src = once(src, "        void beginPrivateRipUp(NetWrapper net) {",
        """        void f4OpenWarmPair() {
            openFrozenPair();
            f4AllowCold = true;
        }

        void beginPrivateRipUp(NetWrapper net) {""")
    start = src.index("    public RouteNode[] getChildren(RouteNodeGraph routingGraph) {")
    end = src.index("    /**\n     * Clears the children", start)
    old = src[start:end]
    fragment_start = old.index("            long start = RuntimeTracker.now();")
    fragment_end = old.index("\n        }\n        return children;", fragment_start)
    materialize = old[fragment_start:fragment_end]
    new = """    public RouteNode[] getChildren(RouteNodeGraph routingGraph) {
        recordPairRead();
        RouteNode[] current = children;
        if (current != null) return current;
        SearchScratch scratch = f4ActiveScratch();
        if (scratch != null && scratch.readOnlyReplay && !scratch.f4AllowCold) {
            long key = ((long) getTile().getUniqueAddress() << 32)
                    | Integer.toUnsignedLong(getWireIndex());
            throw new F5ColdAdjacency(key);
        }
        synchronized (this) {
            if (children != null) return children;
            // Cold materialization owns graph topology only. Native partial-router
            // exclusions see the committed route, not this worker's private labels.
            if (scratch != null) ACTIVE_SCRATCH.remove();
            long t0 = System.nanoTime();
            try {
                f4BuildNativeChildren(routingGraph);
            } finally {
                if (scratch != null) {
                    ACTIVE_SCRATCH.set(scratch);
                    ++scratch.f4ColdBuilt;
                    scratch.f4ColdNs += System.nanoTime() - t0;
                }
            }
            return children;
        }
    }

    private void f4BuildNativeChildren(RouteNodeGraph routingGraph) {
""" + "\n".join(s[4:] for s in materialize.splitlines()) + """
    }

"""
    return src[:start] + new + src[end:]


def graph(src):
    src = once(src, "    protected final RouteNode[][] nodesMap;",
        """    protected final RouteNode[][] nodesMap;
    private static final java.lang.invoke.VarHandle F4_TILES =
            java.lang.invoke.MethodHandles.arrayElementVarHandle(RouteNode[][].class);
    private static final java.lang.invoke.VarHandle F4_WIRES =
            java.lang.invoke.MethodHandles.arrayElementVarHandle(RouteNode[].class);
    private final Object[] f4NodeLocks = f4NewNodeLocks();

    private static Object[] f4NewNodeLocks() {
        Object[] locks = new Object[1024];
        for (int i = 0; i < locks.length; ++i) locks[i] = new Object();
        return locks;
    }""")
    src = once(src, """        RouteNode[] rnodes = nodesMap[tile.getUniqueAddress()];
        return rnodes != null ? rnodes[wireIndex] : null;""",
        """        RouteNode[] rnodes = (RouteNode[]) F4_TILES.getAcquire(nodesMap, tile.getUniqueAddress());
        return rnodes != null ? (RouteNode) F4_WIRES.getAcquire(rnodes, wireIndex) : null;""")
    old = """        RouteNode[] rnodes = nodesMap[tileAddress];
        if (rnodes == null) {
            int baseWireCount = getHighestBaseWireIndex(tile, wireIndex);
            rnodes = new RouteNode[baseWireCount];
            nodesMap[tileAddress] = rnodes;
        }
        RouteNode rnode = rnodes[wireIndex];
        if (rnode == null) {
            rnode = create(node, type);
            rnodes[wireIndex] = rnode;
            nodesMapSize.incrementAndGet();
        }
        return rnode;"""
    src = once(src, old, """        RouteNode[] rnodes = (RouteNode[]) F4_TILES.getAcquire(nodesMap, tileAddress);
        RouteNode rnode = rnodes == null ? null : (RouteNode) F4_WIRES.getAcquire(rnodes, wireIndex);
        if (rnode != null) return rnode;
        synchronized (f4NodeLocks[tileAddress & (f4NodeLocks.length - 1)]) {
            rnodes = (RouteNode[]) F4_TILES.getAcquire(nodesMap, tileAddress);
            if (rnodes == null) {
                int baseWireCount = getHighestBaseWireIndex(tile, wireIndex);
                rnodes = new RouteNode[baseWireCount];
                F4_TILES.setRelease(nodesMap, tileAddress, rnodes);
            }
            rnode = (RouteNode) F4_WIRES.getAcquire(rnodes, wireIndex);
            if (rnode == null) {
                rnode = create(node, type);
                F4_WIRES.setRelease(rnodes, wireIndex, rnode);
                nodesMapSize.incrementAndGet();
            }
            return rnode;
        }""")
    src = once(src, "    protected void addCreateRnodeTime(long time) {",
        "    protected synchronized void addCreateRnodeTime(long time) {")
    return once(src, "    protected long getCreateRnodeTime() {",
        "    protected synchronized long getCreateRnodeTime() {")


def corridor(src):
    # Keep planned portal corridors. Use a clipped endpoint envelope only when a
    # same-SLR long route otherwise has no corridor. One full-box retry remains.
    src = once(src, """        if (!connection.isCrossSLR()) return box(connection);""",
        """        if (!connection.isCrossSLR()) {
            Box planned = box(connection);
            return planned != null ? planned : f4LongEnvelope(connection);
        }""")
    return once(src, "    void disable(Connection connection) {",
        """    private final java.util.Set<Connection> f4Retired =
            java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());

    private Box f4LongEnvelope(Connection c) {
        if (c.getHpwl() < 16 || c.hasAltSinks() || f4Retired.contains(c)) return null;
        RouteNode s = c.getSourceRnode(), t = c.getSinkRnode();
        if (s == null || t == null || s.getTile().getSLR().getId() != t.getTile().getSLR().getId())
            return null;
        int sx = s.getEndTileXCoordinate(), sy = s.getEndTileYCoordinate();
        int tx = t.getEndTileXCoordinate(), ty = t.getEndTileYCoordinate();
        Box base = Box.original(c);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        int margin = Math.min(16, Math.max(4, c.getHpwl() / 8));
        Box effective = new Box(Math.max(base.left, Math.min(sx, tx) - margin - 1),
                Math.max(base.bottom, Math.min(sy, ty) - margin - 1),
                Math.min(base.right, Math.max(sx, tx) + margin + 1),
                Math.min(base.top, Math.max(sy, ty) + margin + 1));
        return (effective.area() * 8 <= base.area() * 7) ? effective : null;
    }

    void disable(Connection connection) {
        f4Retired.add(connection);""")

