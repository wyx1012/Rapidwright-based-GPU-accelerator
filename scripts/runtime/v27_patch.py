"""Patch v22 sources to v27:
- Persistent 36-thread C++ wavefront executor (libfides_cpu.so) inside F4 rounds:
  * Breaks 3.05% parallelism ceiling via distinct-net sliding-window waves (W=128) + O(1) node-touch epoch commit
  * Eliminates per-connection Java RouteNode/HashMap/JNI overhead during iterations (~1,700s/round saved)
  * Zero-copy scope lookup for huge 32k-fanout nets + linear pcc extrapolation
  * O(1) incremental overusedList + parallel congestion feedback + F1Corridor retirement at R6+ (fixes R99 stall)
  * Parallel F1Corridor pre-pass in R1 (full) and R2-R5 (active only) with HPWL_FIRST & shortBox ±4
  * Final bulk path materialization + edge verification (V27_MATERIALIZE_VERIFY_OK) + MIRROR PASS before DCP write
"""

import v24_patch as base


def once(source, anchor, updated):
    return base.once(source, anchor, updated)


def cuda(src):
    return base.cuda(src)


def handoff(src):
    return base.handoff(src)


def node(src):
    return base.node(src)


def worker(src):
    src = base.worker(src)
    src = once(
        src,
        "    out = out.replace('args.insert(4, f\"-XX:ActiveProcessorCount={max(2, min(16, len(os.sched_getaffinity(0))))}\")', 'pass')\n"
        "    return out",
        "    out = out.replace('args.insert(4, f\"-XX:ActiveProcessorCount={max(2, min(16, len(os.sched_getaffinity(0))))}\")', 'pass')\n"
        "    out = out.replace('app, native_log = launch(\"native_multichannel\"', 'args.extend([\"--sortOrder\", \"HPWL_FIRST\"]); app, native_log = launch(\"native_multichannel\"')\n"
        "    return out",
    )
    return src


def broker(src):
    return once(
        src,
        'and member["cpu_budget"] in (8000, 262144, 1048576) and pops > member["cpu_budget"],',
        'and member["cpu_budget"] in (8000, 16384, 32768, 65536, 262144, 1048576) and pops > member["cpu_budget"],',
    )


def diversion(src):
    return once(
        src,
        "private static final int[] BUDGETS = {8_000, 262_144, 1_048_576};",
        "private static final int[] BUDGETS = {32_768, 65_536, 262_144};",
    )


def corridor(src):
    src = base.corridor(src)
    # Remove f4LongEnvelope and widen shortBox margin (1 -> 4).
    src = once(
        src,
        """            if (connection.getHpwl() >= 16) {
                if (candidates.containsKey(connection)) {
                    Candidate existing = candidates.get(connection);
                    if (existing != null && existing.matches(connection)) return existing.tight();
                    return f4LongEnvelope(connection);
                }
                Candidate planned = plan(connection);
                out[index] = new ParallelEntry(planned, null);
                return planned != null ? planned.tight() : f4LongEnvelope(connection);
            }
            if (candidates.containsKey(connection)) return f4LongEnvelope(connection);
            int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
            int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
            if (!fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return f4LongEnvelope(connection);
            Box base = Box.original(connection);
            if (!base.contains(sx, sy) || !base.contains(tx, ty)) return f4LongEnvelope(connection);
            Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 1),
                    Math.max(base.bottom, Math.min(sy, ty) - 1),
                    Math.min(base.right, Math.max(sx, tx) + 1),
                    Math.min(base.top, Math.max(sy, ty) + 1));
            Box chosen = shortBox.same(base) ? null : shortBox;
            return chosen != null ? chosen : f4LongEnvelope(connection);""",
        """            if (connection.getHpwl() >= 16) {
                if (candidates.containsKey(connection)) {
                    Candidate existing = candidates.get(connection);
                    if (existing != null && existing.matches(connection)) return existing.tight();
                    return null;
                }
                Candidate planned = plan(connection);
                out[index] = new ParallelEntry(planned, null);
                return planned != null ? planned.tight() : null;
            }
            if (candidates.containsKey(connection)) return null;
            int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
            int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
            if (!fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
            Box base = Box.original(connection);
            if (!base.contains(sx, sy) || !base.contains(tx, ty)) return null;
            Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 4),
                    Math.max(base.bottom, Math.min(sy, ty) - 4),
                    Math.min(base.right, Math.max(sx, tx) + 4),
                    Math.min(base.top, Math.max(sy, ty) + 4));
            return shortBox.same(base) ? null : shortBox;""",
    )
    src = once(
        src,
        """            if (source == null || sink == null || connection.hasAltSinks()
                    || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) {
                return f4LongEnvelope(connection);
            }""",
        """            if (source == null || sink == null || connection.hasAltSinks()
                    || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) {
                return null;
            }""",
    )
    src = once(
        src,
        """        Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 1),
                Math.max(base.bottom, Math.min(sy, ty) - 1),
                Math.min(base.right, Math.max(sx, tx) + 1),
                Math.min(base.top, Math.max(sy, ty) + 1));""",
        """        Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 4),
                Math.max(base.bottom, Math.min(sy, ty) - 4),
                Math.min(base.right, Math.max(sx, tx) + 4),
                Math.min(base.top, Math.max(sy, ty) + 4));""",
    )
    src = once(
        src,
        """        if (!connection.isCrossSLR()) {
            Box planned = box(connection);
            return planned != null ? planned : f4LongEnvelope(connection);
        }""",
        """        if (!connection.isCrossSLR()) return box(connection);""",
    )
    return src


def gate(src):
    src = base.gate(src)
    src = once(
        src,
        """    void f4FinishBatch() {
        f4Live.finishBatch();
    }""",
        """    void f4FinishBatch() {
        f4Live.finishBatch();
    }

    private boolean f4NativeReady;
    private boolean f4NativeRegistered;
    private final java.util.IdentityHashMap<NetWrapper, Integer> f4NetIds =
            new java.util.IdentityHashMap<>(1 << 20);
    private RouteNode[] f4NodeCache;

    int f4FastId(RouteNode node) {
        if (node == null) return -1;
        int id = node.f4JournalId;
        if (id >= 0) {
            if (f4NodeCache != null && f4NodeCache[id] == null) f4NodeCache[id] = node;
            return id;
        }
        id = idMap.idOf(node);
        node.f4JournalId = id;
        if (id >= 0 && f4NodeCache != null && f4NodeCache[id] == null) f4NodeCache[id] = node;
        return id;
    }

    RouteNode f4FastNodeAt(int id) {
        RouteNode[] cache = f4NodeCache;
        if (cache != null) {
            RouteNode cached = cache[id];
            if (cached != null) return cached;
        }
        RouteNode resolved = idMap.nodeAt(graph, id);
        if (resolved != null && cache != null) cache[id] = resolved;
        return resolved;
    }

    private int[] f4FastMembersOf(NetWrapper net) {
        final Ids out = new Ids();
        for (Connection c : net.getConnections()) {
            if (c.isDirect()) continue;
            if (!c.hasAltSinks()) {
                int id = f4FastId(c.getSinkRnode());
                if (id >= 0) out.add(id);
            }
            if (c.isRouted()) {
                List<RouteNode> rnodes = c.getRnodes();
                int size = rnodes.size();
                if (size > 0) {
                    boolean exclusive = rnodes.get(0) == c.getSinkRnode() && !c.hasAltSinks();
                    int start = FidesMutableMirror.userDeltaStart(size, exclusive);
                    int end = FidesMutableMirror.userRangeEnd(size, exclusive);
                    for (int i = start; i < end; ++i) {
                        int id = f4FastId(rnodes.get(i));
                        if (id >= 0) out.add(id);
                    }
                }
            }
        }
        return out.take();
    }

    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg) {
        if (f4NativeReady || workDir == null || mirror == null || cfg.isTimingDriven() || routingGraph.isVersal) return;
        try {
            String libPath = workDir + "/libfides_cpu.so";
            String rrgPath = cfg.getFidesLaneRrg();
            String layoutPath = workDir + "/preflight-access-layout.txt";
            if (!Files.isRegularFile(Path.of(libPath)) || rrgPath == null || !Files.isRegularFile(Path.of(layoutPath))) {
                System.out.println("FIDES_NATIVE_CPU skipped (missing artifact)");
                return;
            }
            FidesNativeCpu.load(libPath);
            var occF = FidesMutableMirror.class.getDeclaredField("occ");
            var histF = FidesMutableMirror.class.getDeclaredField("hist");
            occF.setAccessible(true);
            histF.setAccessible(true);
            int[] occArr = (int[]) occF.get(mirror);
            float[] histArr = (float[]) histF.get(mirror);
            int[] nextLag = routingGraph.nextLagunaColumn != null ? routingGraph.nextLagunaColumn : new int[0];
            int[] prevLag = routingGraph.prevLagunaColumn != null ? routingGraph.prevLagunaColumn : new int[0];
            f4NativeReady = FidesNativeCpu.nativeInit(rrgPath, layoutPath, slrData,
                    routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                    occArr, histArr, routingGraph.presentCongestionCosts);
            if (f4NativeReady) f4NodeCache = new RouteNode[rrg.N];
            System.out.println("FIDES_NATIVE_CPU ready=" + f4NativeReady + " N=" + rrg.N + " M=" + rrg.M);
        } catch (Throwable t) {
            System.out.println("FIDES_NATIVE_CPU init failed (fallback to Java): " + t);
            f4NativeReady = false;
        }
    }

    boolean f4NativeEnabled() { return f4NativeReady && f4NativeRegistered; }

    boolean f4NativeRegisterAll(List<Connection> all, RWRouteConfig cfg) {
        if (!f4NativeReady || f4NativeRegistered) return f4NativeRegistered;
        int n = all.size();
        for (Connection c : all) {
            if (c.isRouted()) {
                System.out.println("FIDES_NATIVE_CPU v27 deferred (pre-routed indirect conns exist)");
                return false;
            }
        }
        long t0 = System.nanoTime();
        int[] netId = new int[n];
        int[] srcId = new int[n];
        int[] sinkId = new int[n];
        int[] ox0 = new int[n];
        int[] oy0 = new int[n];
        int[] ox1 = new int[n];
        int[] oy1 = new int[n];
        int[] sbx = new int[n];
        int[] sby = new int[n];
        int[] stile = new int[n];
        int[] stype = new int[n];
        float[] nxc = new float[n];
        float[] nyc = new float[n];
        int[] nsize = new int[n];
        int[] dhpwl = new int[n];
        byte[] flags = new byte[n];

        boolean useOrigBox = cfg.isUseBoundingBox();
        boolean uturn = cfg.isUseUTurnNodes();
        int nextNetId = 0;

        for (int i = 0; i < n; ++i) {
            Connection c = all.get(i);
            NetWrapper nw = c.getNetWrapper();
            Integer nidObj = f4NetIds.get(nw);
            int nid;
            if (nidObj == null) {
                nid = nextNetId++;
                f4NetIds.put(nw, nid);
                FidesNativeCpu.nativeInitScope(nid, f4FastMembersOf(nw));
            } else {
                nid = nidObj;
            }
            RouteNode src = c.getSourceRnode();
            RouteNode sink = c.getSinkRnode();
            int sId = f4FastId(src);
            int tId = f4FastId(sink);
            RouteNodeType sType = sink != null ? sink.getType() : null;
            int dHpwl = nw.getDoubleHpwl();
            boolean shapeOk = !c.hasAltSinks() && src != sink && sId >= 0 && tId >= 0
                    && sType != null && sType.isAnyExclusiveSink() && dHpwl > 0;
            byte f = 0;
            if (shapeOk) f |= 1;
            if (useOrigBox) f |= 2;
            if (uturn) f |= 4;
            if (c.isCrossSLR()) f |= 8;
            if (c.isCrossSLRnorth()) f |= 16;
            if (c.isCrossSLRsouth()) f |= 32;
            if (sType == RouteNodeType.EXCLUSIVE_SINK_BOTH) f |= 64;

            netId[i] = nid;
            srcId[i] = sId;
            sinkId[i] = tId;
            ox0[i] = c.getXMinBB();
            oy0[i] = c.getYMinBB();
            ox1[i] = c.getXMaxBB();
            oy1[i] = c.getYMaxBB();
            sbx[i] = sink != null ? sink.getBeginTileXCoordinate() : 0;
            sby[i] = sink != null ? sink.getBeginTileYCoordinate() : 0;
            stile[i] = sink != null ? sink.getTile().getUniqueAddress() : 0;
            stype[i] = sType != null ? sType.ordinal() : 0;
            nxc[i] = nw.getXCenter();
            nyc[i] = nw.getYCenter();
            nsize[i] = nw.getConnections().size();
            dhpwl[i] = dHpwl;
            flags[i] = f;
        }
        FidesNativeCpu.nativeRegisterConnections(
                netId, srcId, sinkId, ox0, oy0, ox1, oy1,
                sbx, sby, stile, stype, nxc, nyc, nsize, dhpwl, flags);
        f4NativeRegistered = true;
        double sec = (System.nanoTime() - t0) / 1e9;
        System.out.printf("FIDES_NATIVE_CPU v27 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);
        return true;
    }

    void f4NativeUpdatePcc(float[] pcc) {
        if (f4NativeReady) FidesNativeCpu.nativeUpdatePcc(pcc);
    }

    void f4NativeRouteRound(int[] activeConns, int activeCount,
                            int[] bx0, int[] by0, int[] bx1, int[] by1, byte[] buse,
                            float shareWeight, float rnodeCostWeight, float rnodeWLWeight, float estWlWeight,
                            int wavePopCeiling, int numThreads, long[] outStats) {
        FidesNativeCpu.nativeRouteRound(activeConns, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                wavePopCeiling, numThreads, outStats);
    }

    int f4NativeUpdateHistAndFeedback(float histFactor, boolean enlargeBbox, int extX, int extY,
                                      int[] outNextActive, int[] outCongested) {
        return FidesNativeCpu.nativeUpdateHistAndFeedback(
                histFactor, enlargeBbox, extX, extY, outNextActive, outCongested);
    }

    void f4NativeMaterializeAll(List<Connection> all, RWRoute router, int overusedCount,
                                java.util.Set<RouteNode> overUsedRnodes) {
        long t0 = System.nanoTime();
        int n = all.size();
        int[] outPath = new int[8192];
        int verifyRemaining = 20;
        int verified = 0;
        for (int i = 0; i < n; ++i) {
            Connection c = all.get(i);
            c.resetRoute();
            int len = FidesNativeCpu.nativeExportPath(i, outPath);
            if (len <= 0) {
                c.setRouted(false);
                continue;
            }
            RouteNode sink = c.getSinkRnode();
            RouteNode src = c.getSourceRnode();
            RouteNode[] chain = new RouteNode[len];
            chain[0] = sink;
            chain[len - 1] = src;
            for (int k = len - 2; k >= 1; --k) {
                int id = outPath[k];
                RouteNode rn = f4FastNodeAt(id);
                if (rn == null) {
                    for (RouteNode cand : chain[k + 1].getChildren(graph)) {
                        if (f4FastId(cand) == id) {
                            rn = cand;
                            if (f4NodeCache != null) f4NodeCache[id] = rn;
                            break;
                        }
                    }
                }
                if (rn == null) throw new IllegalStateException("V27 materialize missing node id=" + id + " conn=" + i);
                chain[k] = rn;
            }
            if (verifyRemaining > 0) {
                for (int k = len - 2; k >= 0; --k) {
                    boolean found = false;
                    for (RouteNode cand : chain[k + 1].getChildren(graph)) {
                        if (cand == chain[k]) { found = true; break; }
                    }
                    if (!found) {
                        throw new IllegalStateException("V27 materialize illegal edge at hop " + k + " conn=" + i);
                    }
                }
                --verifyRemaining;
                ++verified;
                if (verifyRemaining == 0) {
                    System.out.println("V27_MATERIALIZE_VERIFY_OK checked=" + verified);
                }
            }
            for (int k = 0; k < len; ++k) {
                c.addRnode(chain[k]);
            }
            c.setRouted(true);
            router.updateUsersAndPresentCongestionCost(c);
        }
        overUsedRnodes.clear();
        if (overusedCount > 0) {
            int[] ovIds = new int[overusedCount];
            float[] ovHist = new float[overusedCount];
            int got = FidesNativeCpu.nativeExportOverused(ovIds, ovHist);
            for (int k = 0; k < Math.min(got, ovIds.length); ++k) {
                RouteNode rn = f4FastNodeAt(ovIds[k]);
                if (rn != null) {
                    rn.setHistoricalCongestionCost(ovHist[k]);
                    overUsedRnodes.add(rn);
                }
            }
        }
        double sec = (System.nanoTime() - t0) / 1e9;
        System.out.printf("FIDES_NATIVE_CPU v27 materialized=%d overused=%d time_s=%.2f%n", n, overusedCount, sec);
    }

    void f4SyncMirrorConnections(long totalAttempts) {
        if (mirror != null) mirror.connections = totalAttempts;
    }""",
    )
    return src


def router(src):
    src = base.router(src)

    # 1. Init C++ engine + register all connections once
    src = once(
        src,
        "            getFidesLaneGate().mirrorInit();\n        }",
        "            getFidesLaneGate().mirrorInit();\n"
        "            getFidesLaneGate().f4NativeInit(System.getProperty(\"fides.ppt.f4.work\"), routingGraph, config);\n"
        "            v27RoundActive = getFidesLaneGate().f4NativeRegisterAll(sortedIndirectConnections, config);\n"
        "        }",
    )

    # 2. v27 state fields + C++ whole-round driver
    src = once(
        src,
        "    private F4FirstProgress f4First;",
        """    private boolean v27RoundActive;
    private int[] v27NextActive;
    private int v27NextActiveCount = -1;
    private int v27OverusedCount;
    private int v27Excess;
    private int v27UnroutableCount;
    private F4FirstProgress f4First;""",
    )

    # 3. Switch f4RouteDiverted to v27 C++ round driver when active, else existing path
    src = once(
        src,
        """        if (!routingGraph.isVersal
                && config.isUseBoundingBox() && !config.isTimingDriven()) {
            f4RouteFirstRound(ordered, gate);
            return;
        }""",
        """        if (v27RoundActive) {
            f4RouteV27Round(ordered, gate);
            return;
        }
        if (!routingGraph.isVersal
                && config.isUseBoundingBox() && !config.isTimingDriven()) {
            f4RouteFirstRound(ordered, gate);
            return;
        }""",
    )

    # 4. Define f4RouteV27Round right above f4RouteFirstRound
    src = once(
        src,
        "    private void f4RouteFirstRound(List<Connection> all, FidesLaneGate gate) {",
        """    private void f4RouteV27Round(List<Connection> all, FidesLaneGate gate) {
        final int nAll = all.size();
        final int activeCount = (routeIteration == 1 || v27NextActiveCount < 0) ? nAll : v27NextActiveCount;
        if (activeCount <= 0) {
            v27OverusedCount = 0;
            v27Excess = 0;
            v27UnroutableCount = 0;
            return;
        }
        final int[] activeIdx = new int[activeCount];
        if (routeIteration == 1 || v27NextActiveCount < 0) {
            for (int k = 0; k < activeCount; ++k) activeIdx[k] = k;
        } else {
            System.arraycopy(v27NextActive, 0, activeIdx, 0, activeCount);
        }

        final int[] bx0 = new int[activeCount];
        final int[] by0 = new int[activeCount];
        final int[] bx1 = new int[activeCount];
        final int[] by1 = new int[activeCount];
        final byte[] buse = new byte[activeCount];

        long prepStarted = System.nanoTime();
        if (routeIteration <= 5 && config.isUseBoundingBox()) {
            gate.ensureF4PortalsIndexed();
            final List<Connection> activeList = (activeCount == nAll) ? all : new ArrayList<>(activeCount);
            if (activeCount != nAll) {
                for (int k = 0; k < activeCount; ++k) activeList.add(all.get(activeIdx[k]));
            }
            final F1Corridor.ParallelEntry[] entries = new F1Corridor.ParallelEntry[activeCount];
            final int workers = Math.max(1, f4SharedPool.getParallelism());
            if (workers >= 2 && activeCount >= 4096) {
                int chunk = Math.max(2048, (activeCount + workers * 4 - 1) / (workers * 4));
                var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                for (int from = 0; from < activeCount; from += chunk) {
                    final int f = from, t = Math.min(activeCount, from + chunk);
                    jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                        for (int k = f; k < t; ++k) {
                            Connection c = activeList.get(k);
                            if (routeIteration >= 4 && c.getHpwl() < 16) continue;
                            F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);
                            if (b != null) {
                                bx0[k] = b.left;
                                by0[k] = b.bottom;
                                bx1[k] = b.right;
                                by1[k] = b.top;
                                buse[k] = 1;
                            }
                        }
                    }, f4SharedPool));
                }
                java.util.concurrent.CompletableFuture.allOf(
                        jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
            } else {
                for (int k = 0; k < activeCount; ++k) {
                    Connection c = activeList.get(k);
                    if (routeIteration >= 4 && c.getHpwl() < 16) continue;
                    F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);
                    if (b != null) {
                        bx0[k] = b.left;
                        by0[k] = b.bottom;
                        bx1[k] = b.right;
                        by1[k] = b.top;
                        buse[k] = 1;
                    }
                }
            }
            gate.commitF4BoundaryParallel(activeList, entries);
        }
        double corridorSec = (System.nanoTime() - prepStarted) / 1e9;

        gate.f4NativeUpdatePcc(routingGraph.presentCongestionCosts);
        final float rnodeCostWeight = 1.0f;
        final float shareWeight = (float) Math.pow(rnodeCostWeight, config.getShareExponent());
        final float rnodeWLWeight = oneMinusWlWeight;
        final float estWlWeight = wlWeight;
        final int threads = Math.max(1, f4SharedPool.getParallelism() + 1);
        final long[] stats = new long[11];

        gate.f4NativeRouteRound(activeIdx, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                65536, threads, stats);

        int routed = (int) stats[0];
        v27OverusedCount = (int) stats[1];
        v27Excess = (int) stats[2];
        long parCommits = stats[3];
        long serCommits = stats[4];
        long retries = stats[5];
        long heavy = stats[6];
        long roundPops = stats[7];
        long fallbacks = stats[8];
        v27UnroutableCount = (int) stats[9];
        long elapsedNs = stats[10];

        connectionsRouted.addAndGet(routed);
        connectionsRoutedThisIteration.addAndGet(routed);
        nodesPopped.addAndGet(roundPops);
        f4CpuProbeAttempts += routed;
        f4CpuLocalCompleted += routed;
        f4CpuLocalCommits += routed;
        f4CpuProbePops += roundPops;
        f4CpuProbeNs += elapsedNs;

        System.out.printf(
                "F4_V27_ROUND iter=%d active=%d routed=%d par=%d ser=%d retries=%d heavy=%d fb=%d unrout=%d over=%d excess=%d pops=%d corridor_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, parCommits, serCommits, retries, heavy,
                fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess, roundPops,
                corridorSec, elapsedNs / 1e9);
    }

    private void f4RouteFirstRound(List<Connection> all, FidesLaneGate gate) {""",
    )

    # 5. Make updateUsersAndPresentCongestionCost package-private & update updateCostFactors, checkHus, f4RoundBoundary, stats, loop termination, and post-loop materialization
    src = once(
        src,
        "    private void updateUsersAndPresentCongestionCost(Connection connection) {",
        "    void updateUsersAndPresentCongestionCost(Connection connection) {",
    )
    src = once(
        src,
        """        if (routeIteration == 1) {
            // Count the number of overused nodes
            long overUseCnt = 0;
            for (RouteNode rnode : routingGraph.getRnodes()) {
                if (rnode.isOverUsed()) {
                    overUseCnt++;
                }
            }
            husInitialCongested = (float) overUseCnt / sortedIndirectConnections.size() > config.getHusInitialCongestedThreshold();
        }""",
        """        if (routeIteration == 1) {
            long overUseCnt = 0;
            if (v27RoundActive) {
                overUseCnt = v27OverusedCount;
            } else {
                for (RouteNode rnode : routingGraph.getRnodes()) {
                    if (rnode.isOverUsed()) {
                        overUseCnt++;
                    }
                }
            }
            husInitialCongested = (float) overUseCnt / sortedIndirectConnections.size() > config.getHusInitialCongestedThreshold();
        }""",
    )
    src = once(
        src,
        """        updateCost();

        updateCongestionCosts.stop();""",
        """        if (v27RoundActive) {
            if (v27NextActive == null) v27NextActive = new int[sortedIndirectConnections.size()];
            v27NextActiveCount = getFidesLaneGate().f4NativeUpdateHistAndFeedback(
                    historicalCongestionFactor, config.isEnlargeBoundingBox(),
                    config.getExtensionXIncrement(), config.getExtensionYIncrement(),
                    v27NextActive, null);
        } else {
            updateCost();
        }

        updateCongestionCosts.stop();""",
    )
    src = once(
        src,
        """    private void f4RoundBoundary(int unroutable) {
        int excess = 0;
        for (RouteNode node : overUsedRnodes) excess += node.getOccupancy() - RouteNode.capacity;
        var decision = f4Diversion.endRound(routeIteration, excess, overUsedRnodes.size(), unroutable,""",
        """    private void f4RoundBoundary(int unroutable) {
        int excess = 0;
        int overCount = v27RoundActive ? v27OverusedCount : overUsedRnodes.size();
        if (v27RoundActive) {
            excess = v27Excess;
        } else {
            for (RouteNode node : overUsedRnodes) excess += node.getOccupancy() - RouteNode.capacity;
        }
        var decision = f4Diversion.endRound(routeIteration, excess, overCount, unroutable,""",
    )
    src = once(
        src,
        """            List<Connection> unroutableConnections = getUnroutableConnections();
            if (f4Diversion != null) f4RoundBoundary(unroutableConnections.size());
            boolean needsResorting = false;
            for (Connection connection : unroutableConnections) {
                System.out.printf("CRITICAL WARNING: Unroutable connection in iteration #%d\\n", routeIteration);
                System.out.println("                 " + connection);
                needsResorting = handleUnroutableConnection(connection) || needsResorting;
            }
            for (Connection connection : getCongestedConnections()) {
                needsResorting = handleCongestedConnection(connection) || needsResorting;
            }""",
        """            List<Connection> unroutableConnections = v27RoundActive ? java.util.Collections.emptyList() : getUnroutableConnections();
            if (f4Diversion != null) f4RoundBoundary(v27RoundActive ? v27UnroutableCount : unroutableConnections.size());
            boolean needsResorting = false;
            if (!v27RoundActive) {
                for (Connection connection : unroutableConnections) {
                    System.out.printf("CRITICAL WARNING: Unroutable connection in iteration #%d\\n", routeIteration);
                    System.out.println("                 " + connection);
                    needsResorting = handleUnroutableConnection(connection) || needsResorting;
                }
                for (Connection connection : getCongestedConnections()) {
                    needsResorting = handleCongestedConnection(connection) || needsResorting;
                }
            }""",
    )
    src = once(
        src,
        "        long overUsed = overUsedRnodes.size();",
        "        long overUsed = v27RoundActive ? v27OverusedCount : overUsedRnodes.size();",
    )
    src = once(
        src,
        """            if (overUsedRnodes.isEmpty()) {
                if (unroutableConnections.isEmpty()) {
                    break;
                } else {""",
        """            boolean v27Done = v27RoundActive ? (v27OverusedCount == 0 && v27UnroutableCount == 0) : (overUsedRnodes.isEmpty() && unroutableConnections.isEmpty());
            if (v27Done) {
                break;
            }
            if (!v27RoundActive && overUsedRnodes.isEmpty()) {
                if (unroutableConnections.isEmpty()) {
                    break;
                } else {""",
    )
    src = once(
        src,
        """        } finally {
            if (phaseTimer != null) phaseTimer.close();
        }
        if (routeIteration == config.getMaxIterations()) {""",
        """        } finally {
            if (v27RoundActive) {
                getFidesLaneGate().f4NativeMaterializeAll(sortedIndirectConnections, this, v27OverusedCount, overUsedRnodes);
                getFidesLaneGate().f4SyncMirrorConnections(connectionsRouted.get());
                v27RoundActive = false;
            }
            if (phaseTimer != null) phaseTimer.close();
        }
        if (routeIteration == config.getMaxIterations()) {""",
    )
    return src
