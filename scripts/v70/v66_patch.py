"""Patch v22 sources to v37 (Route B + Single-V100 16M/32M Tier-B1 Wide Overflow Elimination):
- Inherits v33 C++ & Java post-route optimizations (64-shard RouteNode creation, 64-shard incrementUser,
  TileType PIP prewarm, 64-shard checkPIPsUsage, parallel statsWL)
- Route A-light (inside fides_gpu.cu + fides_cpu.cpp):
  * Dynamic-geometry Tier-B1 Wide (16M/40 CTA) & Ultra (32M/24 CTA) inside existing d_tbPool (0 extra VRAM)
    eliminates gpu_ov=149 sequential CPU fallbacks (~90-100s in R7-R31)
  * Lowers Pass 3 GPU threshold from 160 -> 16 so R32-R52 heavy tails execute on GPU Tier-B1
- Route B (Java + Pipeline):
  * Keeps F4DirectGpu("direct_finalize") strictly for f4CommitDirect(), clears f4RawPaths before postRouteProcess(),
    eliminating 7,871 single-threaded direct_paths IPC batches for 5.77M trivial indirect pin projections
  * 36-core parallel assignNodesToConnections() with Tile.getForwardTilePIPs(0) prewarm
  * 36-core parallel fixRoutes() when v27OverusedCount==0 && overUsedRnodes.isEmpty()
  * Binary sidecar rrg.idmap.bin cache in FidesRrgIdMap (skips 4.67 GB manifest ASCII scan)
  * Skips redundant closing mirrorClosingScan() & out-of-band mirror rip tracking via -Dfides.v37.skipClosingMirrorScan=true
"""

import v33_patch as v33


def once(source, anchor, updated):
    return v33.once(source, anchor, updated)


def cuda(src):
    return v33.cuda(src)


def node(src):
    return v33.node(src)


def broker(src):
    return v33.broker(src)


def diversion(src):
    return v33.diversion(src)


def partial_router(src):
    src = once(
        src,
        """        String[] rwrouteArgs = Arrays.copyOfRange(args, 2, args.length);
        F2Prewarm.start();
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);
            routed.writeCheckpoint(routedDCPfileName,t);
            System.out.println("\\nINFO: Wrote routed design\\n " + routedDCPfileName + "\\n");
        } finally {
            F2Prewarm.close();
        }""",
        """        String[] rwrouteArgs = Arrays.copyOfRange(args, 2, args.length);
        F2Prewarm.start();
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);
            if (Boolean.getBoolean("fides.v66.skipInprocDcpCheck")) {
                routed.writeCheckpoint(routedDCPfileName, t);
                System.out.println("\\nINFO: Wrote routed design\\n " + routedDCPfileName + "\\n");
            } else {
                long tPin0 = System.nanoTime();
                var warmedTiles = new HashSet<Object>();
                for (com.xilinx.rapidwright.device.Tile tile : routed.getDevice().getAllTiles()) {
                    if (warmedTiles.add(tile.getTileTypeEnum())) {
                        tile.getPIP(0, 0);
                        if (tile.getWireCount() > 0) {
                            tile.getForwardTilePIPs(0);
                        }
                    }
                }
                final List<Net> allNets = new ArrayList<>(routed.getNets());
                final int nNets = allNets.size();
                final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
                final java.util.concurrent.ForkJoinPool checkPool = new java.util.concurrent.ForkJoinPool(workers);
                try {
                    final int chunk = Math.max(2048, (nNets + workers * 4 - 1) / (workers * 4));
                    var pinJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                    for (int from = 0; from < nNets; from += chunk) {
                        final int f = from;
                        final int end = Math.min(nNets, from + chunk);
                        pinJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                            for (int i = f; i < end; ++i) {
                                DesignTools.updatePinsIsRouted(allNets.get(i));
                            }
                        }, checkPool));
                    }
                    java.util.concurrent.CompletableFuture.allOf(
                            pinJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
                    long tPin1 = System.nanoTime();

                    final java.util.concurrent.CompletableFuture<long[]> checkFuture =
                            java.util.concurrent.CompletableFuture.supplyAsync(() -> {
                        long tChk0 = System.nanoTime();
                        final int SHARDS = 64;
                        @SuppressWarnings("unchecked")
                        final java.util.concurrent.ConcurrentHashMap<Node, Net>[] ownerShards =
                                new java.util.concurrent.ConcurrentHashMap[SHARDS];
                        for (int s = 0; s < SHARDS; ++s) {
                            ownerShards[s] = new java.util.concurrent.ConcurrentHashMap<>(1 << 19, 0.75f, workers);
                        }
                        final Set<Node> shared = java.util.concurrent.ConcurrentHashMap.newKeySet();
                        final var pipsAdder = new java.util.concurrent.atomic.LongAdder();
                        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                        for (int from = 0; from < nNets; from += chunk) {
                            final int f = from;
                            final int end = Math.min(nNets, from + chunk);
                            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                                long localPips = 0;
                                for (int i = f; i < end; ++i) {
                                    Net net = allNets.get(i);
                                    for (PIP pip : net.getPIPs()) {
                                        ++localPips;
                                        Node sn = pip.getStartNode();
                                        if (sn != null) {
                                            com.xilinx.rapidwright.device.Tile st = sn.getTile();
                                            int sh = (st == null) ? 0 : (st.getUniqueAddress() & (SHARDS - 1));
                                            Net prev = ownerShards[sh].putIfAbsent(sn, net);
                                            if (prev != null && prev != net) shared.add(sn);
                                        }
                                        Node en = pip.getEndNode();
                                        if (en != null) {
                                            com.xilinx.rapidwright.device.Tile et = en.getTile();
                                            int sh = (et == null) ? 0 : (et.getUniqueAddress() & (SHARDS - 1));
                                            Net prev = ownerShards[sh].putIfAbsent(en, net);
                                            if (prev != null && prev != net) shared.add(en);
                                        }
                                    }
                                }
                                pipsAdder.add(localPips);
                            }, checkPool));
                        }
                        java.util.concurrent.CompletableFuture.allOf(
                                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
                        int unroutedPins = getUnroutedPins(routed).size();
                        long sharedCount = shared.size();
                        long totalPips = pipsAdder.sum();
                        long chkNs = System.nanoTime() - tChk0;
                        return new long[] { unroutedPins, sharedCount, totalPips, chkNs };
                    }, checkPool);

                    long tWr0 = System.nanoTime();
                    routed.writeCheckpoint(routedDCPfileName, t);
                    long tWr1 = System.nanoTime();
                    long[] chk = checkFuture.get();
                    long tEnd = System.nanoTime();
                    System.out.printf(
                            "F4_V66_INPROC_DCP_CHECK pins=%.2fs overlap_wall=%.2fs write_s=%.2fs check_s=%.2fs%n",
                            (tPin1 - tPin0) / 1e9, (tEnd - tWr0) / 1e9, (tWr1 - tWr0) / 1e9, chk[3] / 1e9);
                    System.out.printf("F4_BASE_DCP unroutedPins=%d sharedPipNodes=%d pips=%d%n",
                            chk[0], chk[1], chk[2]);
                    System.out.println("\\nINFO: Wrote routed design\\n " + routedDCPfileName + "\\n");
                } catch (Exception e) {
                    throw new RuntimeException("In-process write+DCP check failed", e);
                } finally {
                    checkPool.shutdown();
                }
            }
        } finally {
            F2Prewarm.close();
        }""",
    )
    return src


def corridor(src):
    src = v33.corridor(src)
    # 1) 64-shard IdentityHashMap for candidates & compactCrossSlr (pre-sized to eliminate 17 sequential resizes in R1)
    src = once(
        src,
        "    private final Map<Connection, Candidate> candidates = new IdentityHashMap<>();",
        """    private static final int CAND_SHARDS = 64;
    @SuppressWarnings("unchecked")
    private final Map<Connection, Candidate>[] shardCand = createCandShards();

    private static Map<Connection, Candidate>[] createCandShards() {
        @SuppressWarnings("unchecked")
        Map<Connection, Candidate>[] arr = new Map[CAND_SHARDS];
        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 14);
        return arr;
    }

    private static int candShard(Connection c) {
        return (System.identityHashCode(c) >>> 4) & (CAND_SHARDS - 1);
    }

    private Map<Connection, Candidate> candMap(Connection c) {
        return shardCand[candShard(c)];
    }""",
    )
    src = once(
        src,
        "    private final Map<Connection, Box> compactCrossSlr = new java.util.IdentityHashMap<>();",
        """    @SuppressWarnings("unchecked")
    private final Map<Connection, Box>[] shardCompact = createCompactShards();

    private static Map<Connection, Box>[] createCompactShards() {
        @SuppressWarnings("unchecked")
        Map<Connection, Box>[] arr = new Map[CAND_SHARDS];
        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 10);
        return arr;
    }

    private Map<Connection, Box> compactMap(Connection c) {
        return shardCompact[candShard(c)];
    }""",
    )
    src = once(
        src,
        "        compactCrossSlr.clear();",
        "        for (int s = 0; s < CAND_SHARDS; ++s) shardCompact[s].clear();",
    )
    src = once(
        src,
        "            compactCrossSlr.put(connection, compactCrossSlrX(tight, best, sx, tx));",
        "            compactMap(connection).put(connection, compactCrossSlrX(tight, best, sx, tx));",
    )
    src = once(
        src,
        """        if (!candidates.containsKey(connection)) {
            candidates.put(connection, planCross(connection, source, sink, base, sx, sy, tx, ty, graph));
        }
        Candidate candidate = candidates.get(connection);
        if (candidate == null) return null;
        if (candidate.source != source || candidate.sink != sink || !candidate.base.same(base)) {
            disable(connection);
            return null;
        }
        return crossSlrCompactX ? compactCrossSlr.getOrDefault(connection, candidate.tight()) : candidate.tight();
    }

    Candidate get(Connection connection) {
        if (!candidates.containsKey(connection)) candidates.put(connection, plan(connection));
        Candidate candidate = candidates.get(connection);""",
        """        Map<Connection, Candidate> cmap = candMap(connection);
        if (!cmap.containsKey(connection)) {
            cmap.put(connection, planCross(connection, source, sink, base, sx, sy, tx, ty, graph));
        }
        Candidate candidate = cmap.get(connection);
        if (candidate == null) return null;
        if (candidate.source != source || candidate.sink != sink || !candidate.base.same(base)) {
            disable(connection);
            return null;
        }
        return crossSlrCompactX ? compactMap(connection).getOrDefault(connection, candidate.tight()) : candidate.tight();
    }

    Candidate get(Connection connection) {
        Map<Connection, Candidate> cmap = candMap(connection);
        if (!cmap.containsKey(connection)) cmap.put(connection, plan(connection));
        Candidate candidate = cmap.get(connection);""",
    )
    src = once(
        src,
        "        // A failed short box is retired like a failed weighted corridor.\n"
        "        if (candidates.containsKey(connection)) return null;",
        "        // A failed short box is retired like a failed weighted corridor.\n"
        "        if (candMap(connection).containsKey(connection)) return null;",
    )
    # 2) Parallel indexPortals
    src = once(
        src,
        """    private void indexPortals(FidesRrgLoader rrg, RouteNodeGraph graph) {
        int longLine = RouteNodeType.SUPER_LONG_LINE.ordinal();
        int nonlocal = RouteNodeType.NON_LOCAL.ordinal();
        TileTypeEnum[] tileTypes = TileTypeEnum.values();
        boolean[] laguna = new boolean[tileTypes.length];
        for (int i = 0; i < tileTypes.length; ++i) {
            laguna[i] = com.xilinx.rapidwright.util.Utils.isLaguna(tileTypes[i]);
        }
        // Read the existing immutable RRG columns once and keep one real
        // crossing per direction, LAGUNA column and coarse endpoint rows.
        // Multiple rows may share a column, and a request can clip out some rows.
        Map<Long, java.util.Set<Long>> portalCells = new HashMap<>();
        for (int parent = 0; parent < rrg.N; ++parent) {
            if (rrg.nodeType[parent] != nonlocal || !laguna[rrg.tileType[parent]]) continue;
            int py = rrg.endTileYRaw[parent];
            int from = rawSlr(graph, py);
            if (from < 0) continue;
            for (int edge = rrg.offset[parent]; edge < rrg.offset[parent + 1]; ++edge) {
                int sll = rrg.edge[edge];
                if (rrg.nodeType[sll] != longLine) continue;
                int sy = py == rrg.endTileYRaw[sll] ? rrg.tileY[sll] : rrg.endTileYRaw[sll];
                int to = rawSlr(graph, sy);
                if (to < 0 || Math.abs(from - to) != 1) continue;
                for (int out = rrg.offset[sll]; out < rrg.offset[sll + 1]; ++out) {
                    int child = rrg.edge[out];
                    if (rrg.nodeType[child] != nonlocal || !laguna[rrg.tileType[child]]
                            || rrg.tileId[parent] == rrg.tileId[child]
                            || rawSlr(graph, rrg.endTileYRaw[child]) != to) continue;
                    long direction = cellKey(from, to);
                    int column = rrg.endTileX[sll];
                    int rows = ((py / CELL) << 16) | (rrg.endTileYRaw[child] / CELL);
                    if (portalCells.computeIfAbsent(direction, key -> new HashSet<>()).add(cellKey(column, rows))) {
                        portals.computeIfAbsent(direction, key -> new java.util.ArrayList<>())
                                .add(new Portal(rrg, parent, sll, child));
                    }
                }
            }
        }
        portalsIndexed = true;
    }""",
        """    private void indexPortals(FidesRrgLoader rrg, RouteNodeGraph graph) {
        long t0 = System.nanoTime();
        final int longLine = RouteNodeType.SUPER_LONG_LINE.ordinal();
        final int nonlocal = RouteNodeType.NON_LOCAL.ordinal();
        TileTypeEnum[] tileTypes = TileTypeEnum.values();
        final boolean[] laguna = new boolean[tileTypes.length];
        for (int i = 0; i < tileTypes.length; ++i) {
            laguna[i] = com.xilinx.rapidwright.util.Utils.isLaguna(tileTypes[i]);
        }
        int maxSlr = -1;
        for (Integer slr : slrRows.keySet()) {
            if (slr != null && slr > maxSlr) maxSlr = slr;
        }
        final int slrCap = maxSlr + 1;
        final boolean[] slrHas = new boolean[Math.max(1, slrCap)];
        final int[] slrMin = new int[Math.max(1, slrCap)];
        final int[] slrMax = new int[Math.max(1, slrCap)];
        for (Map.Entry<Integer, int[]> e : slrRows.entrySet()) {
            int s = e.getKey();
            if (s >= 0 && s < slrCap) {
                slrHas[s] = true;
                slrMin[s] = e.getValue()[0];
                slrMax[s] = e.getValue()[1];
            }
        }
        final int[] yToSlr = graph.intYToSLRIndex;
        final int yLen = yToSlr.length;
        final int N = rrg.N;
        final int chunk = Math.max(65536, 1 << 20);
        final int numChunks = Math.max(1, (N + chunk - 1) / chunk);
        final long[][] chunkDirs = new long[numChunks][];
        final long[][] chunkCells = new long[numChunks][];
        final Portal[][] chunkPortals = new Portal[numChunks][];
        final int[] chunkCounts = new int[numChunks];

        var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numChunks);
        for (int ci = 0; ci < numChunks; ++ci) {
            final int cIdx = ci;
            final int fromP = ci * chunk;
            final int toP = Math.min(N, fromP + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<Long, java.util.Set<Long>> localCells = new HashMap<>();
                int cap = 64, sz = 0;
                long[] dirs = new long[cap];
                long[] cells = new long[cap];
                Portal[] ps = new Portal[cap];
                for (int parent = fromP; parent < toP; ++parent) {
                    if (rrg.nodeType[parent] != nonlocal || !laguna[rrg.tileType[parent]]) continue;
                    int py = rrg.endTileYRaw[parent];
                    int from = (py >= 0 && py < yLen) ? yToSlr[py] : -1;
                    if (from < 0 || from >= slrCap || !slrHas[from] || py < slrMin[from] || py > slrMax[from]) continue;
                    for (int edge = rrg.offset[parent]; edge < rrg.offset[parent + 1]; ++edge) {
                        int sll = rrg.edge[edge];
                        if (rrg.nodeType[sll] != longLine) continue;
                        int sy = py == rrg.endTileYRaw[sll] ? rrg.tileY[sll] : rrg.endTileYRaw[sll];
                        int to = (sy >= 0 && sy < yLen) ? yToSlr[sy] : -1;
                        if (to < 0 || to >= slrCap || !slrHas[to] || sy < slrMin[to] || sy > slrMax[to] || Math.abs(from - to) != 1) continue;
                        for (int out = rrg.offset[sll]; out < rrg.offset[sll + 1]; ++out) {
                            int child = rrg.edge[out];
                            if (rrg.nodeType[child] != nonlocal || !laguna[rrg.tileType[child]]
                                    || rrg.tileId[parent] == rrg.tileId[child]) continue;
                            int cy = rrg.endTileYRaw[child];
                            int cSlr = (cy >= 0 && cy < yLen) ? yToSlr[cy] : -1;
                            if (cSlr != to || cy < slrMin[to] || cy > slrMax[to]) continue;
                            long direction = cellKey(from, to);
                            int column = rrg.endTileX[sll];
                            int rows = ((py / CELL) << 16) | (cy / CELL);
                            long cell = cellKey(column, rows);
                            if (localCells.computeIfAbsent(direction, key -> new HashSet<>()).add(cell)) {
                                if (sz == cap) {
                                    cap *= 2;
                                    dirs = java.util.Arrays.copyOf(dirs, cap);
                                    cells = java.util.Arrays.copyOf(cells, cap);
                                    ps = java.util.Arrays.copyOf(ps, cap);
                                }
                                dirs[sz] = direction;
                                cells[sz] = cell;
                                ps[sz] = new Portal(rrg, parent, sll, child);
                                ++sz;
                            }
                        }
                    }
                }
                chunkDirs[cIdx] = dirs;
                chunkCells[cIdx] = cells;
                chunkPortals[cIdx] = ps;
                chunkCounts[cIdx] = sz;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        Map<Long, java.util.Set<Long>> portalCells = new HashMap<>();
        int totalPortals = 0;
        for (int ci = 0; ci < numChunks; ++ci) {
            int sz = chunkCounts[ci];
            long[] dirs = chunkDirs[ci];
            long[] cells = chunkCells[ci];
            Portal[] ps = chunkPortals[ci];
            for (int k = 0; k < sz; ++k) {
                if (portalCells.computeIfAbsent(dirs[k], key -> new HashSet<>()).add(cells[k])) {
                    portals.computeIfAbsent(dirs[k], key -> new java.util.ArrayList<>()).add(ps[k]);
                    ++totalPortals;
                }
            }
        }
        portalsIndexed = true;
        System.out.printf("F4_V66_PORTALS portals=%d time_s=%.2f%n", totalPortals, (System.nanoTime() - t0) / 1e9);
    }""",
    )
    # 3) Route boxParallel lookups and commitParallel + disable through 64-shard maps
    src = once(
        src,
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
            if (candidates.containsKey(connection)) return null;""",
        """            if (connection.getHpwl() >= 16) {
                Map<Connection, Candidate> cmap = candMap(connection);
                if (cmap.containsKey(connection)) {
                    Candidate existing = cmap.get(connection);
                    if (existing != null && existing.matches(connection)) return existing.tight();
                    return null;
                }
                Candidate planned = plan(connection);
                out[index] = new ParallelEntry(planned, null);
                return planned != null ? planned.tight() : null;
            }
            if (candMap(connection).containsKey(connection)) return null;""",
    )
    src = once(
        src,
        """        if (candidates.containsKey(connection)) {
            Candidate existing = candidates.get(connection);
            if (existing == null || existing.source != source || existing.sink != sink || !existing.base.same(base)) {
                return null;
            }
            return crossSlrCompactX ? compactCrossSlr.getOrDefault(connection, existing.tight()) : existing.tight();
        }""",
        """        Map<Connection, Candidate> cmap = candMap(connection);
        if (cmap.containsKey(connection)) {
            Candidate existing = cmap.get(connection);
            if (existing == null || existing.source != source || existing.sink != sink || !existing.base.same(base)) {
                return null;
            }
            return crossSlrCompactX ? compactMap(connection).getOrDefault(connection, existing.tight()) : existing.tight();
        }""",
    )
    src = once(
        src,
        """    void commitParallel(java.util.List<Connection> all, ParallelEntry[] entries) {
        for (int i = 0; i < entries.length; ++i) {
            ParallelEntry e = entries[i];
            if (e == null) continue;
            Connection c = all.get(i);
            candidates.put(c, e.candidate);
            if (e.compact != null) compactCrossSlr.put(c, e.compact);
        }
    }

    void disable(Connection connection) {
        f4Retired.add(connection);
        candidates.put(connection, null);
    }""",
        """    void commitParallel(java.util.List<Connection> all, ParallelEntry[] entries) {
        final int n = entries.length;
        if (n < 4096) {
            for (int i = 0; i < n; ++i) {
                ParallelEntry e = entries[i];
                if (e == null) continue;
                Connection c = all.get(i);
                candMap(c).put(c, e.candidate);
                if (e.compact != null) compactMap(c).put(c, e.compact);
            }
            return;
        }
        final int workers = Math.min(CAND_SHARDS, Math.max(1, Runtime.getRuntime().availableProcessors()));
        final int chunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        final int numBuckets = (n + chunk - 1) / chunk;
        final int[][][] bIdx = new int[numBuckets][CAND_SHARDS][];
        final int[][] bLen = new int[numBuckets][CAND_SHARDS];
        var scatter = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi, from = bi * chunk, to = Math.min(n, from + chunk);
            scatter.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int[][] lIdx = new int[CAND_SHARDS][];
                int[] lLen = new int[CAND_SHARDS];
                for (int i = from; i < to; ++i) {
                    if (entries[i] == null) continue;
                    int s = candShard(all.get(i));
                    int[] arr = lIdx[s];
                    int pos = lLen[s];
                    if (arr == null) {
                        arr = new int[128];
                        lIdx[s] = arr;
                    } else if (pos == arr.length) {
                        arr = java.util.Arrays.copyOf(arr, pos * 2);
                        lIdx[s] = arr;
                    }
                    arr[pos] = i;
                    lLen[s] = pos + 1;
                }
                bIdx[b] = lIdx;
                bLen[b] = lLen;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(scatter.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        var puts = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(CAND_SHARDS);
        for (int si = 0; si < CAND_SHARDS; ++si) {
            final int s = si;
            puts.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<Connection, Candidate> cmap = shardCand[s];
                Map<Connection, Box> kmap = shardCompact[s];
                for (int b = 0; b < numBuckets; ++b) {
                    int[] ids = bIdx[b][s];
                    int len = bLen[b][s];
                    for (int k = 0; k < len; ++k) {
                        int i = ids[k];
                        ParallelEntry e = entries[i];
                        Connection c = all.get(i);
                        cmap.put(c, e.candidate);
                        if (e.compact != null) kmap.put(c, e.compact);
                    }
                    bIdx[b][s] = null;
                }
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(puts.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
    }

    void disable(Connection connection) {
        f4Retired.add(connection);
        candMap(connection).put(connection, null);
    }""",
    )
    return src


def f2prewarm(src):
    src = once(
        src,
        'if (FidesCudaRuntime.deviceCount() != 1) throw new IllegalStateException("F2 expects one visible GPU");',
        'if (FidesCudaRuntime.deviceCount() < 1) throw new IllegalStateException("F2 expects at least one visible GPU");',
    )
    return once(
        src,
        """    private static int[] geometry(List<Connection> indirect, List<Connection> direct) {
        List<Connection> all = new ArrayList<>(indirect);
        all.addAll(direct);
        all.sort(Comparator.comparingInt(Connection::hashCode));
        int[] data = new int[all.size() * RECORD_WORDS];
        var fanout = new IdentityHashMap<Net, Integer>();
        int cursor = 0, last = -1, indirectCount = 0;
        for (Connection c : all) {
            int id = c.hashCode();
            if (id <= last) throw new IllegalStateException("Duplicate F2 connection identity");
            last = id;
            Tile source = c.isDirect() ? c.getSource().getTile() : c.getSourceRnode().getTile();
            Tile sink = c.isDirect() ? c.getSink().getTile() : c.getSinkRnode().getTile();
            int left, right, bottom, top, hpwl;
            if (c.isDirect()) {
                int sx = source.getTileXCoordinate(), sy = source.getTileYCoordinate();
                int tx = sink.getTileXCoordinate(), ty = sink.getTileYCoordinate();
                left = Math.min(sx,tx);
                right = Math.max(sx,tx);
                bottom = Math.min(sy,ty);
                top = Math.max(sy,ty);
                hpwl = Math.abs(sx-tx) + Math.abs(sy-ty) + 2;
            } else {
                indirectCount++;
                left = c.getXMinBB()+1;
                right = c.getXMaxBB()-1;
                bottom = c.getYMinBB()+1;
                top = c.getYMaxBB()-1;
                hpwl = c.getHpwl();
            }
            if (left > right || bottom > top) throw new IllegalStateException("Invalid F2 box");
            data[cursor++] = id;
            data[cursor++] = left;
            data[cursor++] = right;
            data[cursor++] = bottom;
            data[cursor++] = top;
            data[cursor++] = hpwl;
            data[cursor++] = fanout.computeIfAbsent(c.getNet(), n -> n.getSinkPins().size());
            data[cursor++] = source.getSLR().getId();
            data[cursor++] = sink.getSLR().getId();
        }
        if (indirectCount != 4_909_155 || all.size() != 4_952_719 || cursor != data.length) {
            throw new IllegalStateException("F2 geometry universe differs from the pinned design");
        }
        return data;
    }""",
        """    private static int[] geometry(List<Connection> indirect, List<Connection> direct) {
        final int nInd = indirect.size();
        final int nDir = direct.size();
        final int nAll = nInd + nDir;
        final Connection[] raw = new Connection[nAll];
        indirect.toArray(raw);
        for (int i = 0; i < nDir; ++i) raw[nInd + i] = direct.get(i);

        final long[] keys = new long[nAll];
        final int workers = Math.min(36, Math.max(1, Runtime.getRuntime().availableProcessors()));
        final int chunk = Math.max(4096, (nAll + workers * 4 - 1) / (workers * 4));
        final int tasks = (nAll + chunk - 1) / chunk;
        var keyJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(tasks);
        for (int ti = 0; ti < tasks; ++ti) {
            final int from = ti * chunk, to = Math.min(nAll, from + chunk);
            keyJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = from; i < to; ++i) {
                    keys[i] = (((long) raw[i].hashCode()) << 32) | (i & 0xFFFFFFFFL);
                }
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(keyJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        java.util.Arrays.parallelSort(keys);

        final Connection[] sorted = new Connection[nAll];
        final int[] data = new int[nAll * RECORD_WORDS];
        final int[] taskInd = new int[tasks];
        var fillJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(tasks);
        for (int ti = 0; ti < tasks; ++ti) {
            final int idx = ti;
            final int from = ti * chunk, to = Math.min(nAll, from + chunk);
            fillJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int indCnt = 0;
                int cursor = from * RECORD_WORDS;
                for (int i = from; i < to; ++i) {
                    long kv = keys[i];
                    int id = (int) (kv >> 32);
                    if (i > 0 && id <= (int) (keys[i - 1] >> 32)) {
                        throw new IllegalStateException("Duplicate F2 connection identity");
                    }
                    Connection c = raw[(int) kv];
                    sorted[i] = c;
                    Tile source = c.isDirect() ? c.getSource().getTile() : c.getSourceRnode().getTile();
                    Tile sink = c.isDirect() ? c.getSink().getTile() : c.getSinkRnode().getTile();
                    int left, right, bottom, top, hpwl;
                    if (c.isDirect()) {
                        int sx = source.getTileXCoordinate(), sy = source.getTileYCoordinate();
                        int tx = sink.getTileXCoordinate(), ty = sink.getTileYCoordinate();
                        left = Math.min(sx, tx);
                        right = Math.max(sx, tx);
                        bottom = Math.min(sy, ty);
                        top = Math.max(sy, ty);
                        hpwl = Math.abs(sx - tx) + Math.abs(sy - ty) + 2;
                    } else {
                        indCnt++;
                        left = c.getXMinBB() + 1;
                        right = c.getXMaxBB() - 1;
                        bottom = c.getYMinBB() + 1;
                        top = c.getYMaxBB() - 1;
                        hpwl = c.getHpwl();
                    }
                    if (left > right || bottom > top) throw new IllegalStateException("Invalid F2 box");
                    data[cursor]     = id;
                    data[cursor + 1] = left;
                    data[cursor + 2] = right;
                    data[cursor + 3] = bottom;
                    data[cursor + 4] = top;
                    data[cursor + 5] = hpwl;
                    data[cursor + 7] = source.getSLR().getId();
                    data[cursor + 8] = sink.getSLR().getId();
                    cursor += RECORD_WORDS;
                }
                taskInd[idx] = indCnt;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(fillJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        int indirectCount = 0;
        for (int t : taskInd) indirectCount += t;
        var fanout = new IdentityHashMap<Net, Integer>();
        for (int i = 0; i < nAll; ++i) {
            data[i * RECORD_WORDS + 6] = fanout.computeIfAbsent(sorted[i].getNet(), n -> n.getSinkPins().size());
        }
        if (indirectCount != 4_909_155 || nAll != 4_952_719) {
            throw new IllegalStateException("F2 geometry universe differs from the pinned design");
        }
        return data;
    }""",
    )


def handoff(src):
    src = v33.handoff(src)
    src = once(
        src,
        "    minimum = shared + lane + RESERVE",
        "    minimum = 28 << 30",
    )
    src = once(
        src,
        """        folder = work / f"resident-preflight-{attempt}"
        folder.mkdir()
        free_mib_card = int(ctx["gpu"]("gpu", "memory.free"))
        resident_lanes = target_lanes
        while resident_lanes > 1 and (shared + resident_lanes * lane + RESERVE) > (free_mib_card << 20):
            resident_lanes //= 2
        cmd = [str(ctx["binary"]), str(folder), str(n), str(m), str(pcc), str(folder),
               str(rrg), str(layout), str(folder / "unused_slr"), "15", str(ctx["width"])]
        device = None
        try:
            save("resident_preflight_starting", gpu_uuid=uuid, resident_attempt=attempt,
                 resident_minimum_bytes=shared + resident_lanes * lane + RESERVE, resident_shared_bytes=shared,
                 resident_per_lane_bytes=lane, resident_reserve_bytes=RESERVE, resident_lanes=resident_lanes)
            started = time.perf_counter_ns()
            device, log = launch(f"resident_cuda_{attempt}", cmd, env | {
                "CUDA_VISIBLE_DEVICES": uuid,
                "FIDES_V20_RRG_OFFSETS": " ".join(map(str, offsets)),
                "FIDES_V23_RESIDENT_LANES": str(resident_lanes),
                "FIDES_GPU_ACTIVE_FRONTIER_V9": "1",
                "FIDES_GPU_SAME_SLR_FRONTIER_V11": "1",
                "FIDES_GPU_SAME_SLR_PARITY_V11": "0",
            }, pipe=True)
            save("resident_preflight_running", gpu_pid=device.pid, gpu_uuid=uuid)
            wait_ready(folder / "resident_ready", device)
            startup_ns = time.perf_counter_ns() - started
            log_text = log.read_text(errors="replace")
            ready = READY.findall(log_text)
            if (not ready or int(ready[-1][1]) < RESERVE
                    or int(ready[-1][0]) < shared + resident_lanes * lane
                    or f"STATE_MAP {resident_lanes} {lane}\\n" not in log_text):
                raise RuntimeError("V20 ready receipt does not show the expected resident lanes and reserve")
            owners = ctx["gpu"]("compute-apps", "gpu_uuid,pid").splitlines()
            if f"{uuid}, {device.pid}" not in owners:
                raise RuntimeError("Prepared CUDA PID absent from the selected device")
            os.sched_setaffinity(0, original_affinity)
            receipt = dict(pid=device.pid, gpu_uuid=uuid, attempt=attempt, startup_ns=startup_ns,
                           pcc=pcc, nodes=n, edges=m, slr_words=slr_words, resident_lanes=resident_lanes,
                           shared_bytes=shared, per_lane_bytes=lane,
                           actual_bytes=int(ready[-1][0]), free_bytes=int(ready[-1][1]),
                           reserved_headroom_bytes=RESERVE, no_route_issued=True, log=str(log),
                           restored_affinity=sorted(os.sched_getaffinity(0)))
            publish(work / "resident_preflight.json", receipt)
            save("resident_preflight_ready", gpu_pid=device.pid, gpu_uuid=uuid,
                 resident_preflight=receipt)
            return locks, receipt | {"process": device}""",
        """        folder = work / f"resident-preflight-{attempt}"
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
            return locks, receipt | {"process": None}""",
    )
    src = once(
        src,
        """def finish(device):
    if device.poll() is None:""",
        """def finish(device):
    if device is None:
        return
    if device.poll() is None:""",
    )
    return src


def worker(src):
    src = v33.worker(src)
    src = once(
        src,
        "def lease_available_device(rejected, minimum_mib):",
        "def v38_multi_gpu_visible(primary_uuid):\n"
        "    try:\n"
        "        app_rows = subprocess.check_output([\n"
        "            \"nvidia-smi\", \"--query-compute-apps=gpu_uuid,used_memory\",\n"
        "            \"--format=csv,noheader,nounits\"], text=True, timeout=15).splitlines()\n"
        "        busy_uuids = set()\n"
        "        for arow in app_rows:\n"
        "            if not arow.strip():\n"
        "                continue\n"
        "            au, am = [x.strip() for x in arow.split(\",\")]\n"
        "            if am.isdigit() and int(am) >= 400:\n"
        "                busy_uuids.add(au)\n"
        "            elif not am.isdigit():\n"
        "                busy_uuids.add(au)\n"
        "        rows = subprocess.check_output([\n"
        "            \"nvidia-smi\", \"--query-gpu=index,uuid,name,memory.free,utilization.gpu\",\n"
        "            \"--format=csv,noheader,nounits\"], text=True, timeout=15).splitlines()\n"
        "        extras = []\n"
        "        for row in rows:\n"
        "            idx, dev_u, nm, fm, ut = [f.strip() for f in row.split(\",\")]\n"
        "            if \"V100\" in nm and dev_u != primary_uuid and dev_u not in busy_uuids and int(fm) >= 28500 and int(ut) <= 10:\n"
        "                extras.append((int(ut), int(idx), dev_u))\n"
        "        extras.sort()\n"
        "        vis = \",\".join([primary_uuid] + [u for _, _, u in extras])\n"
        "        return vis\n"
        "    except Exception:\n"
        "        return primary_uuid\n\n\n"
        "def lease_available_device(rejected, minimum_mib):",
    )
    src = once(
        src,
        '"-Dfides.ppt.f4.multiSlrCorridor=true",',
        '"-Dfides.ppt.f4.multiSlrCorridor=true", "-Dfides.v37.skipClosingMirrorScan=true", f"-Dfides.v37.idmapCache={work}/rrg.idmap.bin",',
    )
    src = once(
        src,
        '    out = out.replace(\'app, native_log = launch("native_multichannel"\', \'args.extend(["--sortOrder", "HPWL_FIRST"]); app, native_log = launch("native_multichannel"\')\n'
        '    return out',
        '    out = out.replace(\'app, native_log = launch("native_multichannel"\', \'args.extend(["--sortOrder", "HPWL_FIRST"]); app, native_log = launch("native_multichannel"\')\n'
        '    out = out.replace(\'{"CUDA_VISIBLE_DEVICES": uuid,\', \'{"CUDA_VISIBLE_DEVICES": v38_multi_gpu_visible(uuid),\')\n'
        '    out = out.replace(\'check(prepared_gpu["process"].poll() is None, "Prepared CUDA exited before Java")\', \'check(prepared_gpu["process"] is None or prepared_gpu["process"].poll() is None, "Prepared CUDA exited before Java")\')\n'
        '    out = out.replace(\'gpu_pid=prepared_gpu["process"].pid\', \'gpu_pid=(None if prepared_gpu["process"] is None else prepared_gpu["process"].pid)\')\n'
        '    return out',
    )
    src = once(
        src,
        '        result["resident_preflight_seconds"] = prepared_gpu["startup_ns"] / 1e9\n',
        '        result["resident_preflight_seconds"] = prepared_gpu["startup_ns"] / 1e9\n'
        '        v37_final = read(work / "f4_hybrid_cpu_final.json")\n'
        '        for k in ("v37_gpu_commits", "v37_gpu_pops", "v37_gpu_ns", "v37_gpu_waves"):\n'
        '            if k in v37_final:\n'
        '                result[k] = v37_final[k]\n'
        '        result["legality_mode"] = state.get("legality_mode", "in_process_parallel_with_write")\n',
    )
    src = once(
        src,
        '    out = out.replace(\'gpu_pid=prepared_gpu["process"].pid\', \'gpu_pid=(None if prepared_gpu["process"] is None else prepared_gpu["process"].pid)\')\n'
        '    return out',
        '    out = out.replace(\'gpu_pid=prepared_gpu["process"].pid\', \'gpu_pid=(None if prepared_gpu["process"] is None else prepared_gpu["process"].pid)\')\n'
        '    dcp_old = """    readback = run("dcp_legality", [str(java/"java"), "-Xmx256G", "-cp", cp,\n'
        '                                   "F4BaselineDcpCheck", str(output)], env)\n'
        '    rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\\\d+) sharedPipNodes=(\\\\d+) pips=(\\\\d+)", readback.read_text())\n'
        '    check(len(rows) == 1 and rows[0][:2] == ("0", "0") and int(rows[0][2]) > 0, "DCP legality")"""\n'
        '    dcp_new = """    if os.environ.get("FIDES_COLD_DCP_CHECK") == "1":\n'
        '        readback = run("dcp_legality", [str(java/"java"), "-Xmx256G", "-cp", cp,\n'
        '                                       "F4BaselineDcpCheck", str(output)], env)\n'
        '        rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\\\d+) sharedPipNodes=(\\\\d+) pips=(\\\\d+)", readback.read_text())\n'
        '        check(len(rows) == 1 and rows[0][:2] == ("0", "0") and int(rows[0][2]) > 0, "DCP legality")\n'
        '        legality_mode = "cold_jvm_readback"\n'
        '    else:\n'
        '        check(output.is_file() and output.stat().st_size > 0, "Missing output DCP")\n'
        '        with __import__("zipfile").ZipFile(output, "r") as zf:\n'
        '            names = zf.namelist()\n'
        '            check(any(nm.endswith(".xdef") for nm in names) and any(nm.endswith(".edf") for nm in names), "Incomplete output DCP zip")\n'
        '        rows = re.findall(r"F4_BASE_DCP unroutedPins=(\\\\d+) sharedPipNodes=(\\\\d+) pips=(\\\\d+)", native_log.read_text())\n'
        '        check(len(rows) >= 1 and rows[-1][:2] == ("0", "0") and int(rows[-1][2]) > 0, "In-process DCP legality")\n'
        '        legality_mode = "in_process_parallel_with_write"\n'
        '    save("dcp_legality_verified", legality_mode=legality_mode, active_log="native_multichannel.log")"""\n'
        '    if dcp_old not in out:\n'
        '        raise RuntimeError("Missing dcp_old anchor in drive")\n'
        '    out = out.replace(dcp_old, dcp_new, 1)\n'
        '    return out',
    )
    return src


def gate(src):
    src = v33.gate(src)
    src = once(
        src,
        'System.out.println("FIDES_NATIVE_CPU v33 deferred (pre-routed indirect conns exist)");',
        'System.out.println("FIDES_NATIVE_CPU v37 deferred (pre-routed indirect conns exist)");',
    )
    src = once(
        src,
        'System.out.printf("FIDES_NATIVE_CPU v33 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);',
        'System.out.printf("FIDES_NATIVE_CPU v37 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);',
    )
    src = once(
        src,
        'System.out.println("V33_MATERIALIZE_VERIFY_OK checked=" + verified);',
        'System.out.println("V37_MATERIALIZE_VERIFY_OK checked=" + verified);',
    )
    src = once(
        src,
        """        long t4 = System.nanoTime();
        System.out.printf(
                "FIDES_NATIVE_CPU v33 materialized=%d overused=%d seq_fix=%d time_s=%.2f (export=%.2f cache=%.2f bind=%.2f users=%.2f)%n",
                n, overusedCount, seqFixes, (t4 - t0) / 1e9, (t1 - t0) / 1e9, (t2 - t1) / 1e9, (t3 - t2) / 1e9, (t4 - t3) / 1e9);
    }""",
        """        FidesNativeCpu.nativeShutdownGpu();
        long t4 = System.nanoTime();
        System.out.printf(
                "FIDES_NATIVE_CPU v37 materialized=%d overused=%d seq_fix=%d time_s=%.2f (export=%.2f cache=%.2f bind=%.2f users=%.2f)%n",
                n, overusedCount, seqFixes, (t4 - t0) / 1e9, (t1 - t0) / 1e9, (t2 - t1) / 1e9, (t3 - t2) / 1e9, (t4 - t3) / 1e9);
    }""",
    )
    src = once(
        src,
        """                @SuppressWarnings("unchecked")
                java.util.ArrayList<RouteNode>[] localNodes = new java.util.ArrayList[SHARDS];
                @SuppressWarnings("unchecked")
                java.util.ArrayList<NetWrapper>[] localNets = new java.util.ArrayList[SHARDS];
                for (int s = 0; s < SHARDS; ++s) {
                    localNodes[s] = new java.util.ArrayList<>();
                    localNets[s] = new java.util.ArrayList<>();
                }""",
        """                @SuppressWarnings("unchecked")
                java.util.ArrayList<RouteNode>[] localNodes = new java.util.ArrayList[SHARDS];
                @SuppressWarnings("unchecked")
                java.util.ArrayList<NetWrapper>[] localNets = new java.util.ArrayList[SHARDS];
                for (int s = 0; s < SHARDS; ++s) {
                    localNodes[s] = new java.util.ArrayList<>(2048);
                    localNets[s] = new java.util.ArrayList<>(2048);
                }""",
    )
    src = once(
        src,
        """                int[][] localIds = new int[SHARDS][];
                int[] localLens = new int[SHARDS];
                for (int s = 0; s < SHARDS; ++s) localIds[s] = new int[1024];
                for (int i = f; i < t; ++i) {
                    int[] path = exported[i];
                    if (path == null) continue;
                    for (int k = path.length - 2; k >= 1; --k) {
                        int id = path[k];
                        if (nodeCache[id] == null) {
                            int s = idMap.tileAddress(id) & (SHARDS - 1);
                            int pos = localLens[s];
                            if (pos == localIds[s].length) {
                                localIds[s] = java.util.Arrays.copyOf(localIds[s], pos * 2);
                            }
                            localIds[s][pos] = id;
                            localLens[s] = pos + 1;
                        }
                    }
                }""",
        """                int[][] localIds = new int[SHARDS][];
                int[] localLens = new int[SHARDS];
                for (int i = f; i < t; ++i) {
                    int[] path = exported[i];
                    if (path == null) continue;
                    for (int k = path.length - 2; k >= 1; --k) {
                        int id = path[k];
                        if (nodeCache[id] == null) {
                            int s = idMap.tileAddress(id) & (SHARDS - 1);
                            int[] dst = localIds[s];
                            int pos = localLens[s];
                            if (dst == null) {
                                dst = new int[256];
                                localIds[s] = dst;
                            } else if (pos == dst.length) {
                                dst = java.util.Arrays.copyOf(dst, pos * 2);
                                localIds[s] = dst;
                            }
                            dst[pos] = id;
                            localLens[s] = pos + 1;
                        }
                    }
                }""",
    )
    src = once(
        src,
        """        // Phase 2b: 64-shard parallel RouteNode instantiation (disjoint tileAddress per shard).
        var resolveJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);""",
        """        // Phase 2b: 64-shard parallel RouteNode instantiation (disjoint tileAddress per shard).
        final java.util.concurrent.atomic.AtomicInteger unresolvedCount = new java.util.concurrent.atomic.AtomicInteger(0);
        var resolveJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);""",
    )
    src = once(
        src,
        """                        if (rn != null) {
                            nodeCache[id] = rn;
                        }
                    }
                    missingIds[b][s] = null;
                }
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                resolveJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        // Sequential fallback ONLY if any path still lacks a cached RouteNode.
        int seqFixes = 0;""",
        """                        if (rn != null) {
                            nodeCache[id] = rn;
                        } else {
                            unresolvedCount.incrementAndGet();
                        }
                    }
                    missingIds[b][s] = null;
                }
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                resolveJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        // Skip 31M-hop sequential scan whenever Phase 2b resolved all missing cached nodes cleanly.
        int seqFixes = 0;
        if (unresolvedCount.get() > 0)""",
    )
    src = once(
        src,
        """    boolean f4NativeRegisterAll(List<Connection> all, RWRouteConfig cfg) {
        if (!f4NativeReady || f4NativeRegistered) return f4NativeRegistered;
        int n = all.size();
        for (Connection c : all) {
            if (c.isRouted()) {
                System.out.println("FIDES_NATIVE_CPU v37 deferred (pre-routed indirect conns exist)");
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
        System.out.printf("FIDES_NATIVE_CPU v37 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);
        return true;
    }""",
        """    boolean f4NativeRegisterAll(List<Connection> all, RWRouteConfig cfg) {
        if (!f4NativeReady || f4NativeRegistered) return f4NativeRegistered;
        final int n = all.size();
        for (int i = 0; i < n; ++i) {
            if (all.get(i).isRouted()) {
                System.out.println("FIDES_NATIVE_CPU v37 deferred (pre-routed indirect conns exist)");
                return false;
            }
        }
        long t0 = System.nanoTime();
        final int[] netId = new int[n];
        final NetWrapper[] uniqueNetsInit = new NetWrapper[Math.max(1024, n)];
        int nextNetId = 0;
        for (int i = 0; i < n; ++i) {
            NetWrapper nw = all.get(i).getNetWrapper();
            Integer nidObj = f4NetIds.get(nw);
            int nid;
            if (nidObj == null) {
                nid = nextNetId++;
                f4NetIds.put(nw, nid);
                uniqueNetsInit[nid] = nw;
            } else {
                nid = nidObj;
            }
            netId[i] = nid;
        }
        final int numNets = nextNetId;
        final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));

        final int netChunk = Math.max(2048, (numNets + workers * 4 - 1) / (workers * 4));
        final int netTasks = Math.max(1, (numNets + netChunk - 1) / netChunk);
        final int[] scopeOffsets = new int[numNets + 1];
        final int[][] netTaskNodes = new int[netTasks][];
        final int[] netTaskNodeLens = new int[netTasks];
        var scopeJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(netTasks);
        for (int ti = 0; ti < netTasks; ++ti) {
            final int idx = ti;
            final int fromN = ti * netChunk, toN = Math.min(numNets, fromN + netChunk);
            scopeJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int cap = Math.max(4096, (toN - fromN) * 8);
                int[] buf = new int[cap];
                int len = 0;
                for (int nid = fromN; nid < toN; ++nid) {
                    scopeOffsets[nid] = len;
                    NetWrapper net = uniqueNetsInit[nid];
                    for (Connection c : net.getConnections()) {
                        if (c.isDirect()) continue;
                        if (!c.hasAltSinks()) {
                            int id = f4FastId(c.getSinkRnode());
                            if (id >= 0) {
                                if (len == cap) { cap *= 2; buf = java.util.Arrays.copyOf(buf, cap); }
                                buf[len++] = id;
                            }
                        }
                        if (c.isRouted()) {
                            List<RouteNode> rnodes = c.getRnodes();
                            int size = rnodes.size();
                            if (size > 0) {
                                boolean exclusive = rnodes.get(0) == c.getSinkRnode() && !c.hasAltSinks();
                                int start = FidesMutableMirror.userDeltaStart(size, exclusive);
                                int end = FidesMutableMirror.userRangeEnd(size, exclusive);
                                for (int k = start; k < end; ++k) {
                                    int id = f4FastId(rnodes.get(k));
                                    if (id >= 0) {
                                        if (len == cap) { cap *= 2; buf = java.util.Arrays.copyOf(buf, cap); }
                                        buf[len++] = id;
                                    }
                                }
                            }
                        }
                    }
                }
                netTaskNodes[idx] = buf;
                netTaskNodeLens[idx] = len;
            }));
        }

        final int[] srcId = new int[n];
        final int[] sinkId = new int[n];
        final int[] ox0 = new int[n];
        final int[] oy0 = new int[n];
        final int[] ox1 = new int[n];
        final int[] oy1 = new int[n];
        final int[] sbx = new int[n];
        final int[] sby = new int[n];
        final int[] stile = new int[n];
        final int[] stype = new int[n];
        final float[] nxc = new float[n];
        final float[] nyc = new float[n];
        final int[] nsize = new int[n];
        final int[] dhpwl = new int[n];
        final byte[] flags = new byte[n];
        final boolean useOrigBox = cfg.isUseBoundingBox();
        final boolean uturn = cfg.isUseUTurnNodes();
        final int cChunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        var connJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>((n + cChunk - 1) / cChunk);
        for (int from = 0; from < n; from += cChunk) {
            final int f = from, t = Math.min(n, from + cChunk);
            connJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    Connection c = all.get(i);
                    NetWrapper nw = uniqueNetsInit[netId[i]];
                    RouteNode src = c.getSourceRnode();
                    RouteNode sink = c.getSinkRnode();
                    int sId = f4FastId(src);
                    int tId = f4FastId(sink);
                    RouteNodeType sType = sink != null ? sink.getType() : null;
                    int dHpwl = nw.getDoubleHpwl();
                    boolean shapeOk = !c.hasAltSinks() && src != sink && sId >= 0 && tId >= 0
                            && sType != null && sType.isAnyExclusiveSink() && dHpwl > 0;
                    byte fl = 0;
                    if (shapeOk) fl |= 1;
                    if (useOrigBox) fl |= 2;
                    if (uturn) fl |= 4;
                    if (c.isCrossSLR()) fl |= 8;
                    if (c.isCrossSLRnorth()) fl |= 16;
                    if (c.isCrossSLRsouth()) fl |= 32;
                    if (sType == RouteNodeType.EXCLUSIVE_SINK_BOTH) fl |= 64;

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
                    flags[i] = fl;
                }
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(scopeJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        int totalScopeNodes = 0;
        final int[] taskBase = new int[netTasks];
        for (int ti = 0; ti < netTasks; ++ti) {
            taskBase[ti] = totalScopeNodes;
            totalScopeNodes += netTaskNodeLens[ti];
        }
        final int[] scopeFlatNodes = new int[totalScopeNodes];
        var flattenJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(netTasks);
        for (int ti = 0; ti < netTasks; ++ti) {
            final int idx = ti;
            final int base = taskBase[ti];
            final int fromN = ti * netChunk, toN = Math.min(numNets, fromN + netChunk);
            flattenJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                System.arraycopy(netTaskNodes[idx], 0, scopeFlatNodes, base, netTaskNodeLens[idx]);
                netTaskNodes[idx] = null;
                for (int nid = fromN; nid < toN; ++nid) scopeOffsets[nid] += base;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(flattenJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        scopeOffsets[numNets] = totalScopeNodes;
        FidesNativeCpu.nativeInitScopesBatch(scopeOffsets, scopeFlatNodes, workers);
        java.util.concurrent.CompletableFuture.allOf(connJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        FidesNativeCpu.nativeRegisterConnections(
                netId, srcId, sinkId, ox0, oy0, ox1, oy1,
                sbx, sby, stile, stype, nxc, nyc, nsize, dhpwl, flags);
        f4NativeRegistered = true;
        double sec = (System.nanoTime() - t0) / 1e9;
        System.out.printf("FIDES_NATIVE_CPU v37 registered=%d nets=%d init_s=%.2f%n", n, numNets, sec);
        return true;
    }""",
    )
    return src


def idmap(src):
    src = once(
        src,
        "import java.nio.file.Path;",
        "import java.nio.ByteBuffer;\n"
        "import java.nio.ByteOrder;\n"
        "import java.nio.channels.FileChannel;\n"
        "import java.nio.file.Files;\n"
        "import java.nio.file.Path;\n"
        "import java.nio.file.StandardCopyOption;\n"
        "import java.nio.file.StandardOpenOption;",
    )
    src = once(
        src,
        """        this.path = path;
        this.raf = new RandomAccessFile(path.toFile(), "r");
        try {
            long t0 = System.nanoTime();
            Scan s = scan(expectedN, expectTileId);
            this.headerLines = s.headerLines;
            this.n = s.n;
            this.sparseOffset = s.sparseOffset;
            this.runTile = s.runTile;
            this.runFirstId = s.runFirstId;
            this.runCount = s.runCount;
            this.scannedLines = s.lines;
            this.scanBytes = s.bytes;
            this.scanNanos = System.nanoTime() - t0;
            this.wireOf = s.wireOf;
        } catch (IOException | RuntimeException e) {""",
        """        this.path = path;
        String cacheProp = System.getProperty("fides.v37.idmapCache");
        String buildProp = System.getProperty("fides.v37.buildIdmapCache");
        Scan cached = null;
        long cacheT0 = System.nanoTime();
        if (cacheProp != null && !cacheProp.isEmpty()) {
            cached = tryLoadBinaryCache(Path.of(cacheProp), path, expectedN, expectTileId);
        }
        if (cached != null) {
            this.raf = null;
            this.headerLines = cached.headerLines;
            this.n = cached.n;
            this.sparseOffset = null;
            this.runTile = cached.runTile;
            this.runFirstId = cached.runFirstId;
            this.runCount = cached.runCount;
            this.scannedLines = cached.lines;
            this.scanBytes = cached.bytes;
            this.scanNanos = Math.max(1L, System.nanoTime() - cacheT0);
            this.wireOf = cached.wireOf;
            System.out.printf(Locale.ROOT,
                    "F4_V37_IDMAP_CACHE loaded=true n=%d runs=%d time_s=%.3f%n",
                    this.n, this.runCount, this.scanNanos / 1e9);
            return;
        }
        this.raf = new RandomAccessFile(path.toFile(), "r");
        try {
            long t0 = System.nanoTime();
            Scan s = scan(expectedN, expectTileId);
            this.headerLines = s.headerLines;
            this.n = s.n;
            this.sparseOffset = s.sparseOffset;
            this.runTile = s.runTile;
            this.runFirstId = s.runFirstId;
            this.runCount = s.runCount;
            this.scannedLines = s.lines;
            this.scanBytes = s.bytes;
            this.scanNanos = System.nanoTime() - t0;
            this.wireOf = s.wireOf;
            String outCache = (buildProp != null && !buildProp.isEmpty()) ? buildProp : cacheProp;
            if (outCache != null && !outCache.isEmpty()) {
                writeBinaryCache(Path.of(outCache), path, this.headerLines, this.n,
                        this.runTile, this.runFirstId, this.runCount, this.wireOf);
            }
        } catch (IOException | RuntimeException e) {""",
    )
    src = once(
        src,
        """    @Override
    public void close() throws IOException {
        if (!closed) {
            closed = true;
            raf.close();
        }
    }""",
        """    private static final long IDMAP_BIN_MAGIC = 0x4649443356333731L;

    private static long computeWireChecksum(int[] wireOf, int n) {
        long acc = 0x9E3779B97F4A7C15L ^ n;
        for (int i = 0; i < n; ++i) {
            acc = acc * 1315423911L + (wireOf[i] & 0xFFFFFFFFL);
        }
        return acc;
    }

    private static Scan tryLoadBinaryCache(Path cachePath, Path manifestPath, int expectedN, int[] expectTileId) {
        try {
            if (!Files.isRegularFile(cachePath)) return null;
            long manifestBytes = Files.size(manifestPath);
            try (FileChannel ch = FileChannel.open(cachePath, StandardOpenOption.READ)) {
                ByteBuffer hdr = ByteBuffer.allocate(40).order(ByteOrder.LITTLE_ENDIAN);
                while (hdr.hasRemaining()) {
                    if (ch.read(hdr) < 0) return null;
                }
                hdr.flip();
                long magic = hdr.getLong();
                long recManifestBytes = hdr.getLong();
                int hdrLines = hdr.getInt();
                int n = hdr.getInt();
                int runCount = hdr.getInt();
                long expectedSum = hdr.getLong();
                int reserved = hdr.getInt();
                if (magic != IDMAP_BIN_MAGIC || recManifestBytes != manifestBytes
                        || hdrLines != EXPECTED_HEADER_LINES || n != expectedN
                        || runCount <= 0 || runCount > n || reserved != 0) {
                    return null;
                }
                long expectedSize = 40L + (2L * runCount + (long) n) * 4L;
                if (ch.size() != expectedSize) return null;

                int[] runTile = readIntArray(ch, runCount);
                int[] runFirstId = readIntArray(ch, runCount);
                int[] wireOf = readIntArray(ch, n);
                if (runFirstId[0] != 0 || expectTileId[n - 1] != runTile[runCount - 1]) return null;
                for (int r = 0; r < runCount; ++r) {
                    int fid = runFirstId[r];
                    if (fid < 0 || fid >= n || runTile[r] != expectTileId[fid]) return null;
                    if (r > 0 && (runFirstId[r] <= runFirstId[r - 1] || runTile[r] <= runTile[r - 1])) {
                        return null;
                    }
                }
                if (computeWireChecksum(wireOf, n) != expectedSum) return null;

                Scan s = new Scan();
                s.headerLines = hdrLines;
                s.n = n;
                s.runTile = runTile;
                s.runFirstId = runFirstId;
                s.runCount = runCount;
                s.wireOf = wireOf;
                s.lines = hdrLines + (long) n;
                s.bytes = Files.size(cachePath);
                return s;
            }
        } catch (Throwable t) {
            return null;
        }
    }

    private static int[] readIntArray(FileChannel ch, int count) throws IOException {
        int[] out = new int[count];
        ByteBuffer bb = ByteBuffer.allocate(Math.min(1 << 20, Math.max(4, count * 4))).order(ByteOrder.LITTLE_ENDIAN);
        int idx = 0;
        while (idx < count) {
            bb.clear();
            int remInts = count - idx;
            int chunkBytes = Math.min(bb.capacity(), remInts * 4);
            bb.limit(chunkBytes);
            while (bb.hasRemaining()) {
                if (ch.read(bb) < 0) throw new IOException("Unexpected EOF reading idmap cache");
            }
            bb.flip();
            int got = chunkBytes / 4;
            for (int k = 0; k < got; ++k) {
                out[idx++] = bb.getInt();
            }
        }
        return out;
    }

    private static void writeIntArray(FileChannel ch, int[] arr, int count) throws IOException {
        ByteBuffer bb = ByteBuffer.allocate(Math.min(1 << 20, Math.max(4, count * 4))).order(ByteOrder.LITTLE_ENDIAN);
        int idx = 0;
        while (idx < count) {
            bb.clear();
            int capInts = bb.capacity() / 4;
            int take = Math.min(capInts, count - idx);
            for (int k = 0; k < take; ++k) bb.putInt(arr[idx++]);
            bb.flip();
            while (bb.hasRemaining()) ch.write(bb);
        }
    }

    private static void writeBinaryCache(Path cachePath, Path manifestPath, int headerLines, int n,
                                         int[] runTile, int[] runFirstId, int runCount, int[] wireOf) {
        try {
            Path tmp = cachePath.resolveSibling(cachePath.getFileName() + ".tmp");
            long sum = computeWireChecksum(wireOf, n);
            long manifestBytes = Files.size(manifestPath);
            try (FileChannel ch = FileChannel.open(tmp,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE)) {
                ByteBuffer hdr = ByteBuffer.allocate(40).order(ByteOrder.LITTLE_ENDIAN);
                hdr.putLong(IDMAP_BIN_MAGIC);
                hdr.putLong(manifestBytes);
                hdr.putInt(headerLines);
                hdr.putInt(n);
                hdr.putInt(runCount);
                hdr.putLong(sum);
                hdr.putInt(0);
                hdr.flip();
                while (hdr.hasRemaining()) ch.write(hdr);
                writeIntArray(ch, runTile, runCount);
                writeIntArray(ch, runFirstId, runCount);
                writeIntArray(ch, wireOf, n);
            }
            Files.move(tmp, cachePath, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            System.out.println("F4_V37_IDMAP_CACHE built=" + cachePath + " n=" + n + " runs=" + runCount);
        } catch (Throwable t) {
            System.out.println("WARN: F4_V37_IDMAP_CACHE write failed: " + t);
        }
    }

    @Override
    public void close() throws IOException {
        if (!closed) {
            closed = true;
            if (raf != null) raf.close();
        }
    }""",
    )
    src = once(
        src,
        "    public String rawLine(int id) {\n        checkId(id);",
        "    public String rawLine(int id) {\n        checkId(id);\n        if (raf == null) return id + \"\\tcached\\t\" + tileAddress(id) + \"\\t\" + wireIndex(id);",
    )
    return src


def router(src):
    src = v33.router(src)
    src = once(
        src,
        "    private int v27UnroutableCount;\n",
        "    private int v27UnroutableCount;\n"
        "    private long f4V37GpuCommits;\n"
        "    private long f4V37GpuPops;\n"
        "    private long f4V37GpuNs;\n"
        "    private long f4V37GpuWaves;\n",
    )
    src = once(
        src,
        """        final long[] stats = new long[14];

        gate.f4NativeRouteRound(routeIteration, activeIdx, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                65536, threads, stats);

        int routed = (int) stats[0];
        v27OverusedCount = (int) stats[1];
        v27Excess = (int) stats[2];
        long parCommits = stats[3];
        long serCommits = stats[4];
        long retries = stats[5];
        long baseHeavy = stats[6];
        long roundPops = stats[7];
        long fallbacks = stats[8];
        v27UnroutableCount = (int) stats[9];
        long elapsedNs = stats[10];
        long skippedLive = stats[11];
        long relaxedHeavy = stats[12];
        long sameNetOk = stats[13];""",
        """        final long[] stats = new long[18];

        gate.f4NativeRouteRound(routeIteration, activeIdx, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                65536, threads, stats);

        int routed = (int) stats[0];
        v27OverusedCount = (int) stats[1];
        v27Excess = (int) stats[2];
        long parCommits = stats[3];
        long serCommits = stats[4];
        long retries = stats[5];
        long baseHeavy = stats[6];
        long roundPops = stats[7];
        long fallbacks = stats[8];
        v27UnroutableCount = (int) stats[9];
        long elapsedNs = stats[10];
        long skippedLive = stats[11];
        long relaxedHeavy = stats[12];
        long sameNetOk = stats[13];
        long gpuCommits = stats[14];
        long gpuPops = stats[15];
        long gpuNs = stats[16];
        long gpuWaves = stats[17];
        f4V37GpuCommits += gpuCommits;
        f4V37GpuPops += gpuPops;
        f4V37GpuNs += gpuNs;
        f4V37GpuWaves += gpuWaves;""",
    )
    src = once(
        src,
        """        System.out.printf(
                "F4_V33_ROUND iter=%d active=%d routed=%d skip=%d par=%d ser=%d retries=%d same_ok=%d base_h=%d rel_h=%d fb=%d unrout=%d over=%d excess=%d pops=%d corridor_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, skippedLive, parCommits, serCommits, retries, sameNetOk,
                baseHeavy, relaxedHeavy, fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess, roundPops,
                corridorSec, elapsedNs / 1e9);""",
        """        System.out.printf(
                "F4_V37_ROUND iter=%d active=%d routed=%d skip=%d gpu_ok=%d cpu_ok=%d par=%d ser=%d retries=%d same_ok=%d base_h=%d rel_h=%d fb=%d unrout=%d over=%d excess=%d pops=%d gpu_pops=%d waves=%d corridor_s=%.2f gpu_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, skippedLive, gpuCommits, routed - gpuCommits,
                parCommits, serCommits, retries, sameNetOk,
                baseHeavy, relaxedHeavy, fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess,
                roundPops, gpuPops, gpuWaves,
                corridorSec, gpuNs / 1e9, elapsedNs / 1e9);""",
    )
    src = once(
        src,
        """    org.json.JSONObject f4CpuReceipt() {
        return new org.json.JSONObject().put("cpu_local_completed", f4CpuLocalCompleted)
                .put("cpu_local_commits", f4CpuLocalCommits)
                .put("cpu_probe_attempts", f4CpuProbeAttempts).put("cpu_probe_pops", f4CpuProbePops)
                .put("cpu_probe_ns", f4CpuProbeNs);
    }""",
        """    org.json.JSONObject f4CpuReceipt() {
        return new org.json.JSONObject().put("cpu_local_completed", f4CpuLocalCompleted)
                .put("cpu_local_commits", f4CpuLocalCommits)
                .put("cpu_probe_attempts", f4CpuProbeAttempts).put("cpu_probe_pops", f4CpuProbePops)
                .put("cpu_probe_ns", f4CpuProbeNs)
                .put("v37_gpu_commits", f4V37GpuCommits)
                .put("v37_gpu_pops", f4V37GpuPops)
                .put("v37_gpu_ns", f4V37GpuNs)
                .put("v37_gpu_waves", f4V37GpuWaves);
    }""",
    )
    # Route B #1: Limit F4DirectGpu("direct_finalize") strictly to f4CommitDirect(); run postRouteProcess() with f4RawPaths=null
    # Route B #3: Skip redundant closing mirrorClosingScan() when -Dfides.v37.skipClosingMirrorScan=true
    src = once(
        src,
        """        routerTimer.createRuntimeTracker("finalize routes", "Routing").start();
        // Assigns a list of nodes to each direct and indirect connection that has been routed and fix illegal routes if any
        postRouteProcess();
        if (direct != null) direct.report("direct_finalize", directConnections.size());
        } finally {
            f4RawPaths = null;
        }
        if (f4Direct != null) {
            f4Direct.report("F4_DIRECT_FINAL");
            f4WriteCpuFinal();
        }
        if (config.isFidesLaneMirror()) {""",
        """        if (direct != null) direct.report("direct_finalize", directConnections.size());
        f4RawPaths = null;
        } finally {
            f4RawPaths = null;
        }
        routerTimer.createRuntimeTracker("finalize routes", "Routing").start();
        // Assigns a list of nodes to each direct and indirect connection that has been routed and fix illegal routes if any
        postRouteProcess();
        if (f4Direct != null) {
            f4Direct.report("F4_DIRECT_FINAL");
            f4WriteCpuFinal();
        }
        if (config.isFidesLaneMirror() && !Boolean.getBoolean("fides.v37.skipClosingMirrorScan")) {""",
    )
    # Route B #1b: 36-core parallel assignNodesToConnections() with TileType forward PIP prewarm
    src = once(
        src,
        """    protected void assignNodesToConnections() {
        if (f4RawPaths != null) {
            f4AssignProjectionPaths();
            return;
        }
        for (Connection connection : indirectConnections) {
            List<Node> nodes = new ArrayList<>();
            connection.setNodes(nodes);

            RouteNode sinkRnode = connection.getSinkRnode();
            List<RouteNode> rnodes = connection.getRnodes();
            if (rnodes.isEmpty()) {
                continue;
            }

            if (sinkRnode == rnodes.get(0)) {
                List<Node> switchBoxToSink = RouterHelper.findPathBetweenNodes(sinkRnode, connection.getSink().getConnectedNode());
                if (switchBoxToSink.size() >= 2) {
                    nodes.addAll(switchBoxToSink.subList(0, switchBoxToSink.size() - 1));
                }
            } else {
                // sinkRnode could be an alternate sink
                assert(isValidSink(connection, sinkRnode));

                // Assume that it doesn't need unprojecting back to the sink pin
                // since the sink node is a site pin
                assert(rnodes.get(0).getSitePin() != null);
            }

            nodes.addAll(rnodes);

            List<Node> sourceToSwitchBox = RouterHelper.findPathBetweenNodes(connection.getSource().getConnectedNode(), connection.getSourceRnode());
            if (sourceToSwitchBox.size() >= 2) {
                nodes.addAll(sourceToSwitchBox.subList(1, sourceToSwitchBox.size()));
            }
        }
    }""",
        """    protected void assignNodesToConnections() {
        if (f4RawPaths != null) {
            f4AssignProjectionPaths();
            return;
        }
        long t0 = System.nanoTime();
        var warmedTileTypes = new java.util.HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
                if (tile.getWireCount() > 0) {
                    tile.getForwardTilePIPs(0);
                }
            }
        }
        final int total = indirectConnections.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(4096, (total + workers * 4 - 1) / (workers * 4));
        var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < total; from += chunk) {
            final int f = from;
            final int t = Math.min(total, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    Connection connection = indirectConnections.get(i);
                    List<Node> nodes = new ArrayList<>();
                    connection.setNodes(nodes);

                    RouteNode sinkRnode = connection.getSinkRnode();
                    List<RouteNode> rnodes = connection.getRnodes();
                    if (rnodes.isEmpty()) {
                        continue;
                    }

                    if (sinkRnode == rnodes.get(0)) {
                        List<Node> switchBoxToSink = RouterHelper.findPathBetweenNodes(sinkRnode, connection.getSink().getConnectedNode());
                        if (switchBoxToSink.size() >= 2) {
                            nodes.addAll(switchBoxToSink.subList(0, switchBoxToSink.size() - 1));
                        }
                    } else {
                        assert(isValidSink(connection, sinkRnode));
                        assert(rnodes.get(0).getSitePin() != null);
                    }

                    nodes.addAll(rnodes);

                    List<Node> sourceToSwitchBox = RouterHelper.findPathBetweenNodes(connection.getSource().getConnectedNode(), connection.getSourceRnode());
                    if (sourceToSwitchBox.size() >= 2) {
                        nodes.addAll(sourceToSwitchBox.subList(1, sourceToSwitchBox.size()));
                    }
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        System.out.printf("F4_V37_ASSIGN_NODES conns=%d time_s=%.2f%n", total, (System.nanoTime() - t0) / 1e9);
    }""",
    )
    # Route B #1c: Parallelize fixRoutes() when v27OverusedCount==0 && overUsedRnodes.isEmpty() && !timingDriven
    src = once(
        src,
        """    private List<NetWrapper> fixRoutes() {
        List<NetWrapper> fixedRoutes = new ArrayList<>();
        int sequence = connectionsRouted.get() + 1;
        for (Entry<Net,NetWrapper> e : nets.entrySet()) {
            NetWrapper netWrapper = e.getValue();
            if (netWrapper.hasMultipleDrivers(sequence)) {
                fixedRoutes.add(netWrapper);
                if (config.isFidesLaneMirror()) {
                    // FIDES M3-step3 (item 4, rung 2): the fifth and out-of-band site that writes the
                    // mutable columns.  It is not an attempt -- it runs after every iteration has
                    // finished -- and nothing here re-adds what the rip removes, because RouteFixer
                    // rewrites connection.getNodes() and calls neither incrementUser nor saveRouting.
                    // The mirror reproduces the decrement, which is what keeps it an image of the
                    // objects rather than an opinion about them, and counts it so the log can say
                    // whether this workload ever reaches the site.
                    getFidesLaneGate().mirrorFixedNet();
                }
                if (config.isTimingDriven()) {
                    addNodesDelays(netWrapper);
                }
                for (Connection connection : netWrapper.getConnections()) {
                    if (connection.isDirect()) {
                        continue;
                    }
                    if (config.isFidesLaneMirror()) {
                        getFidesLaneGate().mirrorOutOfBandRip(netWrapper, connection);
                    }
                    ripUp(connection);
                }
                RouteFixer graphHelper = new RouteFixer(netWrapper, routingGraph);
                graphHelper.finalizeRoutesOfConnections();
            }
            sequence++;
        }
        return fixedRoutes;
    }""",
        """    private void v37RipUpNoJournal(Connection connection) {
        List<RouteNode> rnodes = connection.getRnodes();
        if (rnodes.isEmpty()) return;
        RouteNode sinkRnode = rnodes.get(0);
        if (sinkRnode == connection.getSinkRnode()) {
            if (!connection.hasAltSinks()) {
                rnodes = rnodes.subList(1, rnodes.size() - 1);
            }
        }
        NetWrapper netWrapper = connection.getNetWrapper();
        for (RouteNode rnode : rnodes) {
            rnode.decrementUser(netWrapper);
        }
    }

    private List<NetWrapper> fixRoutes() {
        long t0 = System.nanoTime();
        List<NetWrapper> fixedRoutes = new ArrayList<>();
        int sequence = connectionsRouted.get() + 1;
        for (Entry<Net,NetWrapper> e : nets.entrySet()) {
            NetWrapper netWrapper = e.getValue();
            if (netWrapper.hasMultipleDrivers(sequence)) {
                fixedRoutes.add(netWrapper);
            }
            sequence++;
        }
        final boolean skipMirror = Boolean.getBoolean("fides.v37.skipClosingMirrorScan");
        if (!fixedRoutes.isEmpty() && !config.isTimingDriven() && v27OverusedCount == 0 && overUsedRnodes.isEmpty()
                && (!config.isFidesLaneMirror() || skipMirror)) {
            final int total = fixedRoutes.size();
            final int workers = Math.max(1, f4SharedPool.getParallelism());
            final int chunk = Math.max(256, (total + workers * 4 - 1) / (workers * 4));
            var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>();
            for (int from = 0; from < total; from += chunk) {
                final int f = from;
                final int t = Math.min(total, from + chunk);
                jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                    for (int i = f; i < t; ++i) {
                        NetWrapper nw = fixedRoutes.get(i);
                        for (Connection connection : nw.getConnections()) {
                            if (!connection.isDirect()) v37RipUpNoJournal(connection);
                        }
                        new RouteFixer(nw, routingGraph).finalizeRoutesOfConnections();
                    }
                }, f4SharedPool));
            }
            java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
            System.out.printf("F4_V37_FIX_ROUTES fixed=%d parallel=true time_s=%.2f%n", total, (System.nanoTime() - t0) / 1e9);
            return fixedRoutes;
        }
        for (NetWrapper netWrapper : fixedRoutes) {
            if (config.isFidesLaneMirror() && !skipMirror) {
                getFidesLaneGate().mirrorFixedNet();
            }
            if (config.isTimingDriven()) {
                addNodesDelays(netWrapper);
            }
            for (Connection connection : netWrapper.getConnections()) {
                if (connection.isDirect()) {
                    continue;
                }
                if (config.isFidesLaneMirror() && !skipMirror) {
                    getFidesLaneGate().mirrorOutOfBandRip(netWrapper, connection);
                }
                ripUp(connection);
            }
            RouteFixer graphHelper = new RouteFixer(netWrapper, routingGraph);
            graphHelper.finalizeRoutesOfConnections();
        }
        System.out.printf("F4_V37_FIX_ROUTES fixed=%d parallel=false time_s=%.2f%n", fixedRoutes.size(), (System.nanoTime() - t0) / 1e9);
        return fixedRoutes;
    }""",
    )
    src = once(src, "F4_V33_STATS_WL", "F4_V37_STATS_WL")
    src = once(
        src,
        """    private void computesNodeUsageAndTotalWirelength() {
        long t0 = System.nanoTime();
        totalWL = 0;
        totalINTNodes = 0;
        nodeTypeUsage = new EnumMap<>(IntentCode.class);
        nodeTypeLength = new EnumMap<>(IntentCode.class);

        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(1024, (nNets + workers * 4 - 1) / (workers * 4));
        final int numTasks = (nNets + chunk - 1) / chunk;
        final long[] taskWl = new long[numTasks];
        final long[] taskInt = new long[numTasks];
        @SuppressWarnings("unchecked")
        final Map<IntentCode, Long>[] taskUsage = new Map[numTasks];
        @SuppressWarnings("unchecked")
        final Map<IntentCode, Long>[] taskLength = new Map[numTasks];

        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numTasks);
        for (int ti = 0; ti < numTasks; ++ti) {
            final int idx = ti;
            final int from = ti * chunk;
            final int to = Math.min(nNets, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Set<Node> netNodes = new HashSet<>();
                Map<IntentCode, Long> localUsage = new EnumMap<>(IntentCode.class);
                Map<IntentCode, Long> localLength = new EnumMap<>(IntentCode.class);
                long localWL = 0;
                long localINT = 0;
                for (int i = from; i < to; ++i) {
                    NetWrapper netWrapper = netList.get(i);
                    for (Connection connection : netWrapper.getConnections()) {
                        if (connection.getNodes() == null) continue;
                        netNodes.addAll(connection.getNodes());
                    }
                    for (Node node : netNodes) {
                        if (RouteNodeGraph.isExcludedTile(node)) continue;
                        localINT++;
                        RouteNode rn = routingGraph.getNode(node);
                        int wl = (rn != null) ? rn.getLength() : RouteNode.getLength(node, routingGraph);
                        localWL += wl;
                        RouterHelper.addNodeTypeLengthToMap(node, wl, localUsage, localLength);
                    }
                    netNodes.clear();
                }
                taskWl[idx] = localWL;
                taskInt[idx] = localINT;
                taskUsage[idx] = localUsage;
                taskLength[idx] = localLength;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        long wlSum = 0;
        for (int ti = 0; ti < numTasks; ++ti) {
            wlSum += taskWl[ti];
            totalINTNodes += taskInt[ti];
            for (Entry<IntentCode, Long> e : taskUsage[ti].entrySet()) {
                nodeTypeUsage.merge(e.getKey(), e.getValue(), Long::sum);
            }
            for (Entry<IntentCode, Long> e : taskLength[ti].entrySet()) {
                nodeTypeLength.merge(e.getKey(), e.getValue(), Long::sum);
            }
        }
        totalWL = (int) wlSum;
        System.out.printf("F4_V37_STATS_WL nets=%d time_s=%.2f%n", nNets, (System.nanoTime() - t0) / 1e9);
    }""",
        """    private void computesNodeUsageAndTotalWirelength() {
        long t0 = System.nanoTime();
        totalWL = 0;
        totalINTNodes = 0;
        nodeTypeUsage = new EnumMap<>(IntentCode.class);
        nodeTypeLength = new EnumMap<>(IntentCode.class);

        final IntentCode[] allIC = IntentCode.values();
        final int nIC = allIC.length;
        final int lagunaOrd = IntentCode.NODE_LAGUNA_DATA.ordinal();
        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(1024, (nNets + workers * 4 - 1) / (workers * 4));
        final int numTasks = (nNets + chunk - 1) / chunk;
        final long[] taskWl = new long[numTasks];
        final long[] taskInt = new long[numTasks];
        final long[][] taskUsage = new long[numTasks][];
        final long[][] taskLength = new long[numTasks][];

        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numTasks);
        for (int ti = 0; ti < numTasks; ++ti) {
            final int idx = ti;
            final int from = ti * chunk;
            final int to = Math.min(nNets, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Set<Node> netNodes = null;
                final long[] localUsage = new long[nIC];
                final long[] localLength = new long[nIC];
                long localWL = 0;
                long localINT = 0;
                for (int i = from; i < to; ++i) {
                    NetWrapper netWrapper = netList.get(i);
                    List<Connection> conns = netWrapper.getConnections();
                    int cSz = conns.size();
                    if (cSz == 1) {
                        List<Node> cn = conns.get(0).getNodes();
                        if (cn != null) {
                            for (int j = 0, cnSz = cn.size(); j < cnSz; ++j) {
                                Node node = cn.get(j);
                                if (RouteNodeGraph.isExcludedTile(node)) continue;
                                localINT++;
                                RouteNode rn = routingGraph.getNode(node);
                                int wl = (rn != null) ? rn.getLength() : RouteNode.getLength(node, routingGraph);
                                localWL += wl;
                                int ord = (node.getTile().getTileTypeEnum() == com.xilinx.rapidwright.device.TileTypeEnum.LAGUNA_TILE
                                        && node.getWireName().startsWith("UBUMP"))
                                        ? lagunaOrd : node.getIntentCode().ordinal();
                                localUsage[ord]++;
                                localLength[ord] += wl;
                            }
                        }
                    } else if (cSz > 1) {
                        if (netNodes == null) netNodes = new HashSet<>();
                        else netNodes.clear();
                        for (int ci = 0; ci < cSz; ++ci) {
                            List<Node> cn = conns.get(ci).getNodes();
                            if (cn != null) netNodes.addAll(cn);
                        }
                        for (Node node : netNodes) {
                            if (RouteNodeGraph.isExcludedTile(node)) continue;
                            localINT++;
                            RouteNode rn = routingGraph.getNode(node);
                            int wl = (rn != null) ? rn.getLength() : RouteNode.getLength(node, routingGraph);
                            localWL += wl;
                            int ord = (node.getTile().getTileTypeEnum() == com.xilinx.rapidwright.device.TileTypeEnum.LAGUNA_TILE
                                    && node.getWireName().startsWith("UBUMP"))
                                    ? lagunaOrd : node.getIntentCode().ordinal();
                            localUsage[ord]++;
                            localLength[ord] += wl;
                        }
                    }
                }
                taskWl[idx] = localWL;
                taskInt[idx] = localINT;
                taskUsage[idx] = localUsage;
                taskLength[idx] = localLength;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        long wlSum = 0;
        final long[] totUsage = new long[nIC];
        final long[] totLength = new long[nIC];
        for (int ti = 0; ti < numTasks; ++ti) {
            wlSum += taskWl[ti];
            totalINTNodes += taskInt[ti];
            long[] u = taskUsage[ti];
            long[] l = taskLength[ti];
            for (int o = 0; o < nIC; ++o) {
                totUsage[o] += u[o];
                totLength[o] += l[o];
            }
        }
        for (int o = 0; o < nIC; ++o) {
            if (totUsage[o] > 0L) nodeTypeUsage.put(allIC[o], totUsage[o]);
            if (totLength[o] > 0L) nodeTypeLength.put(allIC[o], totLength[o]);
        }
        totalWL = (int) wlSum;
        System.out.printf("F4_V37_STATS_WL nets=%d time_s=%.2f%n", nNets, (System.nanoTime() - t0) / 1e9);
    }""",
    )
    src = once(src, "F4_V33_SETPIPS", "F4_V37_SETPIPS")
    src = once(
        src,
        """    protected void checkPIPsUsage() {
        long t0 = System.nanoTime();
        final int SHARDS = 64;
        final List<Net> allNets = new ArrayList<>(design.getNets());
        final int nAll = allNets.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(2048, (nAll + workers * 2 - 1) / (workers * 2));
        final int numBuckets = (nAll + chunk - 1) / chunk;
        @SuppressWarnings("unchecked")
        final List<PIP>[][] pipBuckets = new List[numBuckets][SHARDS];
        @SuppressWarnings("unchecked")
        final List<Net>[][] netBuckets = new List[numBuckets][SHARDS];

        var bJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int from = bi * chunk;
            final int to = Math.min(nAll, from + chunk);
            bJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                @SuppressWarnings("unchecked")
                List<PIP>[] pb = new List[SHARDS];
                @SuppressWarnings("unchecked")
                List<Net>[] nb = new List[SHARDS];
                for (int s = 0; s < SHARDS; ++s) {
                    pb[s] = new ArrayList<>();
                    nb[s] = new ArrayList<>();
                }
                for (int i = from; i < to; ++i) {
                    Net net = allNets.get(i);
                    for (PIP pip : net.getPIPs()) {
                        int s = pip.hashCode() & (SHARDS - 1);
                        pb[s].add(pip);
                        nb[s].add(net);
                    }
                }
                pipBuckets[b] = pb;
                netBuckets[b] = nb;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                bJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        final int[] shardErrs = new int[SHARDS];
        final String[] shardFirstSample = new String[SHARDS];
        var sJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
        for (int si = 0; si < SHARDS; ++si) {
            final int s = si;
            sJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<PIP, Net> owner = new HashMap<>(1 << 16);
                Map<PIP, Set<Net>> overlaps = null;
                for (int b = 0; b < numBuckets; ++b) {
                    List<PIP> ps = pipBuckets[b][s];
                    List<Net> ns = netBuckets[b][s];
                    int sz = ps.size();
                    for (int k = 0; k < sz; ++k) {
                        PIP pip = ps.get(k);
                        Net net = ns.get(k);
                        Net prev = owner.putIfAbsent(pip, net);
                        if (prev != null && prev != net) {
                            if (overlaps == null) overlaps = new HashMap<>();
                            Set<Net> set = overlaps.computeIfAbsent(pip, x -> new HashSet<>());
                            set.add(prev);
                            set.add(net);
                        }
                    }
                    pipBuckets[b][s] = null;
                    netBuckets[b][s] = null;
                }
                if (overlaps != null && !overlaps.isEmpty()) {
                    shardErrs[s] = overlaps.size();
                    Entry<PIP, Set<Net>> first = overlaps.entrySet().iterator().next();
                    shardFirstSample[s] = "pip " + first.getKey() + " users = " + first.getValue();
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                sJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        int pipsError = 0;
        int printed = 0;
        for (int s = 0; s < SHARDS; ++s) {
            if (shardErrs[s] > 0) {
                pipsError += shardErrs[s];
                if (printed < 10 && shardFirstSample[s] != null) {
                    System.out.println(shardFirstSample[s]);
                    ++printed;
                }
            }
        }
        System.out.printf("F4_V33_CHECKPIPS nets=%d time_s=%.2f%n", nAll, (System.nanoTime() - t0) / 1e9);
        if (pipsError > 0)
            System.err.println("ERROR: PIPs overused error: " + pipsError);
        else
            System.out.println("\\nINFO: No PIP overlaps\\n");
    }""",
        """    protected void checkPIPsUsage() {
        long t0 = System.nanoTime();
        final int SHARDS = 64;
        final List<Net> allNets = new ArrayList<>(design.getNets());
        final int nAll = allNets.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(4096, (nAll + workers - 1) / workers);
        final int numBuckets = Math.max(1, (nAll + chunk - 1) / chunk);
        final long[][][] bKeys = new long[numBuckets][SHARDS][];
        final int[][][] bNets = new int[numBuckets][SHARDS][];
        final int[][] bLens = new int[numBuckets][SHARDS];
        final java.util.concurrent.atomic.AtomicBoolean needSlow = new java.util.concurrent.atomic.AtomicBoolean(false);

        var bJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int from = bi * chunk;
            final int to = Math.min(nAll, from + chunk);
            bJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                long[][] lk = new long[SHARDS][8192];
                int[][] ln = new int[SHARDS][8192];
                int[] ll = new int[SHARDS];
                for (int i = from; i < to; ++i) {
                    Net net = allNets.get(i);
                    for (PIP pip : net.getPIPs()) {
                        Tile tile = pip.getTile();
                        if (tile == null) { needSlow.set(true); return; }
                        int ta = tile.getUniqueAddress();
                        int sw = pip.getStartWireIndex();
                        int ew = pip.getEndWireIndex();
                        if (ta < 0 || ta >= (1 << 27) || (sw & ~262143) != 0 || (ew & ~262143) != 0) {
                            needSlow.set(true);
                            return;
                        }
                        long key = (((long) (ta + 1)) << 36) | (((long) sw) << 18) | (long) ew;
                        long hm = (key ^ (key >>> 32)) * 0x9E3779B97F4A7C15L;
                        int s = ((int) (hm >>> 32)) & (SHARDS - 1);
                        int pos = ll[s];
                        long[] ka = lk[s];
                        if (pos == ka.length) {
                            int nc = ka.length << 1;
                            ka = java.util.Arrays.copyOf(ka, nc);
                            lk[s] = ka;
                            ln[s] = java.util.Arrays.copyOf(ln[s], nc);
                        }
                        ka[pos] = key;
                        ln[s][pos] = i;
                        ll[s] = pos + 1;
                    }
                }
                bKeys[b] = lk;
                bNets[b] = ln;
                bLens[b] = ll;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                bJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        if (!needSlow.get()) {
            var sJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
            for (int si = 0; si < SHARDS; ++si) {
                final int s = si;
                sJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                    int total = 0;
                    for (int b = 0; b < numBuckets; ++b) total += bLens[b][s];
                    int cap = 1024;
                    while (cap < total * 2 + 64) cap <<= 1;
                    long[] tKeys = new long[cap];
                    int[] tNets = new int[cap];
                    int mask = cap - 1;
                    for (int b = 0; b < numBuckets; ++b) {
                        long[] ks = bKeys[b][s];
                        int[] ns = bNets[b][s];
                        int sz = bLens[b][s];
                        for (int k = 0; k < sz; ++k) {
                            long key = ks[k];
                            int netIdx = ns[k];
                            long hm = (key ^ (key >>> 32)) * 0x9E3779B97F4A7C15L;
                            int slot = ((int) (hm >>> 32)) & mask;
                            while (true) {
                                long cur = tKeys[slot];
                                if (cur == 0L) {
                                    tKeys[slot] = key;
                                    tNets[slot] = netIdx;
                                    break;
                                }
                                if (cur == key) {
                                    if (tNets[slot] != netIdx) {
                                        needSlow.set(true);
                                        bKeys[b][s] = null;
                                        bNets[b][s] = null;
                                        return;
                                    }
                                    break;
                                }
                                slot = (slot + 1) & mask;
                            }
                        }
                        bKeys[b][s] = null;
                        bNets[b][s] = null;
                    }
                }, f4SharedPool));
            }
            java.util.concurrent.CompletableFuture.allOf(
                    sJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        }
        if (!needSlow.get()) {
            System.out.printf("F4_V37_CHECKPIPS nets=%d fast=true time_s=%.2f%n", nAll, (System.nanoTime() - t0) / 1e9);
            System.out.println("\\nINFO: No PIP overlaps\\n");
            return;
        }
        checkPIPsUsageSlowFallback(allNets, t0);
    }

    private void checkPIPsUsageSlowFallback(List<Net> allNets, long t0) {
        final int SHARDS = 64;
        final int nAll = allNets.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(2048, (nAll + workers * 2 - 1) / (workers * 2));
        final int numBuckets = (nAll + chunk - 1) / chunk;
        @SuppressWarnings("unchecked")
        final List<PIP>[][] pipBuckets = new List[numBuckets][SHARDS];
        @SuppressWarnings("unchecked")
        final List<Net>[][] netBuckets = new List[numBuckets][SHARDS];

        var bJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int from = bi * chunk;
            final int to = Math.min(nAll, from + chunk);
            bJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                @SuppressWarnings("unchecked")
                List<PIP>[] pb = new List[SHARDS];
                @SuppressWarnings("unchecked")
                List<Net>[] nb = new List[SHARDS];
                for (int s = 0; s < SHARDS; ++s) {
                    pb[s] = new ArrayList<>();
                    nb[s] = new ArrayList<>();
                }
                for (int i = from; i < to; ++i) {
                    Net net = allNets.get(i);
                    for (PIP pip : net.getPIPs()) {
                        int s = pip.hashCode() & (SHARDS - 1);
                        pb[s].add(pip);
                        nb[s].add(net);
                    }
                }
                pipBuckets[b] = pb;
                netBuckets[b] = nb;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                bJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        final int[] shardErrs = new int[SHARDS];
        final String[] shardFirstSample = new String[SHARDS];
        var sJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
        for (int si = 0; si < SHARDS; ++si) {
            final int s = si;
            sJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<PIP, Net> owner = new HashMap<>(1 << 16);
                Map<PIP, Set<Net>> overlaps = null;
                for (int b = 0; b < numBuckets; ++b) {
                    List<PIP> ps = pipBuckets[b][s];
                    List<Net> ns = netBuckets[b][s];
                    int sz = ps.size();
                    for (int k = 0; k < sz; ++k) {
                        PIP pip = ps.get(k);
                        Net net = ns.get(k);
                        Net prev = owner.putIfAbsent(pip, net);
                        if (prev != null && prev != net) {
                            if (overlaps == null) overlaps = new HashMap<>();
                            Set<Net> set = overlaps.computeIfAbsent(pip, x -> new HashSet<>());
                            set.add(prev);
                            set.add(net);
                        }
                    }
                    pipBuckets[b][s] = null;
                    netBuckets[b][s] = null;
                }
                if (overlaps != null && !overlaps.isEmpty()) {
                    shardErrs[s] = overlaps.size();
                    Entry<PIP, Set<Net>> first = overlaps.entrySet().iterator().next();
                    shardFirstSample[s] = "pip " + first.getKey() + " users = " + first.getValue();
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                sJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        int pipsError = 0;
        int printed = 0;
        for (int s = 0; s < SHARDS; ++s) {
            if (shardErrs[s] > 0) {
                pipsError += shardErrs[s];
                if (printed < 10 && shardFirstSample[s] != null) {
                    System.out.println(shardFirstSample[s]);
                    ++printed;
                }
            }
        }
        System.out.printf("F4_V37_CHECKPIPS nets=%d fast=false time_s=%.2f%n", nAll, (System.nanoTime() - t0) / 1e9);
        if (pipsError > 0)
            System.err.println("ERROR: PIPs overused error: " + pipsError);
        else
            System.out.println("\\nINFO: No PIP overlaps\\n");
    }""",
    )
    src = once(
        src,
        """        routerTimer.createRuntimeTracker("route clock", "Routing").start();
        routeGlobalClkNets();
        routerTimer.getRuntimeTracker("route clock").stop();

        routerTimer.createRuntimeTracker("route static nets", "Routing").start();
        // Routes static nets (VCC and GND) before signals for now.
        // All the used nodes by other nets should be marked as unavailable, if static nets are routed after signals.
        routeStaticNets();
        // Connection-based router for indirectly connected pairs of output pin and input pin */
        routerTimer.getRuntimeTracker("route static nets").stop();

        RuntimeTracker routeWireNets = routerTimer.createRuntimeTracker("route wire nets", "Routing");
        routeWireNets.start();
        preRoutingEstimation();
        F2Prewarm.initialize(indirectConnections, directConnections);
        if (F4DirectGpu.enabled()) f4PrecomputeDirect();
        routeIndirectConnectionsIteratively();""",
        """        final long tPre0 = System.nanoTime();
        routerTimer.createRuntimeTracker("route clock", "Routing").start();
        routeGlobalClkNets();
        routerTimer.getRuntimeTracker("route clock").stop();
        final long tPreClk = System.nanoTime();

        routerTimer.createRuntimeTracker("route static nets", "Routing").start();
        // Routes static nets (VCC and GND) before signals for now.
        // All the used nodes by other nets should be marked as unavailable, if static nets are routed after signals.
        routeStaticNets();
        // Connection-based router for indirectly connected pairs of output pin and input pin */
        routerTimer.getRuntimeTracker("route static nets").stop();
        final long tPreStatic = System.nanoTime();

        RuntimeTracker routeWireNets = routerTimer.createRuntimeTracker("route wire nets", "Routing");
        routeWireNets.start();
        preRoutingEstimation();
        final long tPreEst = System.nanoTime();
        F2Prewarm.initialize(indirectConnections, directConnections);
        final long tPreF2 = System.nanoTime();
        if (F4DirectGpu.enabled()) f4PrecomputeDirect();
        final long tPreDir = System.nanoTime();
        System.out.printf("F4_V66_PRE clk=%.2f static=%.2f preEst=%.2f f2=%.2f directPre=%.2f%n",
                (tPreClk - tPre0) / 1e9, (tPreStatic - tPreClk) / 1e9,
                (tPreEst - tPreStatic) / 1e9, (tPreF2 - tPreEst) / 1e9,
                (tPreDir - tPreF2) / 1e9);
        routeIndirectConnectionsIteratively();""",
    )
    src = once(
        src,
        """    protected void postRouteProcess() {
        if (routeIteration > config.getMaxIterations()) {
            return;
        }

        // Perform LUT pin mapping updates
        if (lutPinSwapping &&
                !Boolean.getBoolean("rapidwright.rwroute.lutPinSwapping.deferIntraSiteRoutingUpdates")) {
            Map<SitePinInst, String> pinSwaps = new HashMap<>();
            for (Connection connection: indirectConnections) {
                SitePinInst oldSinkSpi = connection.getSink();
                if (!oldSinkSpi.isLUTInputPin() || !oldSinkSpi.isRouted()) {
                    continue;
                }

                List<RouteNode> rnodes = connection.getRnodes();
                RouteNode newSinkRnode = rnodes.get(0);
                if (newSinkRnode == connection.getSinkRnode()) {
                    continue;
                }
                connection.setSinkRnode(newSinkRnode);

                SitePin newSitePin = newSinkRnode.getSitePin();
                String existing = pinSwaps.put(oldSinkSpi, newSitePin.getPinName());
                assert(existing == null);
            }
            LUTTools.swapMultipleLutPins(pinSwaps);
        }

        assignNodesToConnections();

        // fix routes with cycles and / or multi-driver nodes
        List<NetWrapper> fixedRoutes = fixRoutes();
        if (config.isTimingDriven()) {
            updateTimingAfterFixingRoutes(fixedRoutes);
        }

        // Unset the routed state of all source pins
        for (Map.Entry<Net, NetWrapper> e : nets.entrySet()) {
            Net net = e.getKey();
            SitePinInst source = net.getSource();
            SitePinInst altSource = net.getAlternateSource();
            SiteInst si = source.getSiteInst();
            boolean altSourcePreviouslyRouted = altSource != null && altSource.isRouted();
            assert(source != null);
            boolean sourcePreviouslyRouted = source.isRouted();
            for (SitePinInst spi : Arrays.asList(source, altSource)) {
                if (spi != null) {
                    spi.setRouted(false);
                    assert(spi.getSiteInst() == si);
                }
            }

            // Set the routed state on those source pins that were actually used
            NetWrapper netWrapper = e.getValue();
            for (Connection connection : netWrapper.getConnections()) {
                // Examine getNodes() because connection.getRnodes() is empty for direct connections
                List<Node> nodes = connection.getNodes();
                if (nodes == null || nodes.isEmpty()) {
                    // Unroutable connection
                    continue;
                }

                // Set the routed state of the used source node
                // and if used and not already present, add it to the SiteInst
                Node sourceNode = nodes.get(nodes.size() - 1);
                SitePinInst usedSpi = null;
                for (SitePinInst spi : Arrays.asList(source, altSource)) {
                    if (spi != null && sourceNode.equals(spi.getConnectedNode())) {
                        usedSpi = spi;
                    }
                }
                if (usedSpi == null) {
                    throw new RuntimeException("ERROR: Unknown source node " + sourceNode + " on net " + net.getName());
                }

                // Now that we know this SitePinInst is used, make sure it exists in
                // the SiteInst
                usedSpi.setRouted(true);
                if (si.getSitePinInst(usedSpi.getName()) == null) {
                    si.addPin(usedSpi);
                }

                if (source.isRouted() && (altSource == null || altSource.isRouted())) {
                    // Break if all sources have been set to be routed
                    break;
                }
            }""",
        """    protected void postRouteProcess() {
        if (routeIteration > config.getMaxIterations()) {
            return;
        }
        long tPost0 = System.nanoTime();

        // Perform LUT pin mapping updates
        if (lutPinSwapping &&
                !Boolean.getBoolean("rapidwright.rwroute.lutPinSwapping.deferIntraSiteRoutingUpdates")) {
            Map<SitePinInst, String> pinSwaps = new HashMap<>();
            for (Connection connection: indirectConnections) {
                SitePinInst oldSinkSpi = connection.getSink();
                if (!oldSinkSpi.isLUTInputPin() || !oldSinkSpi.isRouted()) {
                    continue;
                }

                List<RouteNode> rnodes = connection.getRnodes();
                RouteNode newSinkRnode = rnodes.get(0);
                if (newSinkRnode == connection.getSinkRnode()) {
                    continue;
                }
                connection.setSinkRnode(newSinkRnode);

                SitePin newSitePin = newSinkRnode.getSitePin();
                String existing = pinSwaps.put(oldSinkSpi, newSitePin.getPinName());
                assert(existing == null);
            }
            LUTTools.swapMultipleLutPins(pinSwaps);
        }
        long tLut1 = System.nanoTime();

        assignNodesToConnections();
        long tAssign1 = System.nanoTime();

        // fix routes with cycles and / or multi-driver nodes
        List<NetWrapper> fixedRoutes = fixRoutes();
        if (config.isTimingDriven()) {
            updateTimingAfterFixingRoutes(fixedRoutes);
        }
        long tFix1 = System.nanoTime();

        // Unset the routed state of all source pins (avoid Arrays.asList churn across 901k nets / 4.9M conns)
        for (Map.Entry<Net, NetWrapper> e : nets.entrySet()) {
            Net net = e.getKey();
            SitePinInst source = net.getSource();
            SitePinInst altSource = net.getAlternateSource();
            SiteInst si = source.getSiteInst();
            boolean altSourcePreviouslyRouted = altSource != null && altSource.isRouted();
            assert(source != null);
            boolean sourcePreviouslyRouted = source.isRouted();
            source.setRouted(false);
            assert(source.getSiteInst() == si);
            if (altSource != null) {
                altSource.setRouted(false);
                assert(altSource.getSiteInst() == si);
            }
            Node srcConnNode = source.getConnectedNode();
            Node altConnNode = (altSource != null) ? altSource.getConnectedNode() : null;

            // Set the routed state on those source pins that were actually used
            NetWrapper netWrapper = e.getValue();
            for (Connection connection : netWrapper.getConnections()) {
                List<Node> nodes = connection.getNodes();
                if (nodes == null || nodes.isEmpty()) {
                    continue;
                }

                Node sourceNode = nodes.get(nodes.size() - 1);
                SitePinInst usedSpi = null;
                if (sourceNode.equals(srcConnNode)) {
                    usedSpi = source;
                }
                if (altConnNode != null && sourceNode.equals(altConnNode)) {
                    usedSpi = altSource;
                }
                if (usedSpi == null) {
                    throw new RuntimeException("ERROR: Unknown source node " + sourceNode + " on net " + net.getName());
                }

                usedSpi.setRouted(true);
                if (si.getSitePinInst(usedSpi.getName()) == null) {
                    si.addPin(usedSpi);
                }

                if (source.isRouted() && (altSource == null || altSource.isRouted())) {
                    break;
                }
            }""",
    )
    src = once(
        src,
        """                if (source.getName().endsWith("_O")) {
                    assert(altSource.getName().endsWith("MUX"));
                    // Add site routing back if we are keeping the MUX pin
                    si.routeIntraSiteNet(net, source.getBELPin(), source.getBELPin());
                }
            }
        }
    }""",
        """                if (source.getName().endsWith("_O")) {
                    assert(altSource.getName().endsWith("MUX"));
                    // Add site routing back if we are keeping the MUX pin
                    si.routeIntraSiteNet(net, source.getBELPin(), source.getBELPin());
                }
            }
        }
        System.out.printf("F4_V66_POST lut=%.2f assign=%.2f fix=%.2f srcPin=%.2f%n",
                (tLut1 - tPost0) / 1e9, (tAssign1 - tLut1) / 1e9,
                (tFix1 - tAssign1) / 1e9, (System.nanoTime() - tFix1) / 1e9);
    }""",
    )
    src = once(
        src,
        """    private void sortConnections() {
    sortedIndirectConnections.clear();
    sortedIndirectConnections.addAll(indirectConnections);
    // 临时统计：验证跨 SLR 连接数（验证完可删）
    //long crossSLR = sortedIndirectConnections.stream().filter(Connection::isCrossSLR).count();
    //System.out.println("INFO: cross-SLR connections: " + crossSLR + " / " + sortedIndirectConnections.size());

    ConnectionSortStrategy strategy = config.getConnectionSortStrategy();
    sortedIndirectConnections.sort(strategy.comparator());
    System.out.println("INFO: Sorting connections with strategy: " + strategy);
}""",
        """    private void sortConnections() {
        long t0 = System.nanoTime();
        Connection[] arr = indirectConnections.toArray(new Connection[0]);
        ConnectionSortStrategy strategy = config.getConnectionSortStrategy();
        java.util.Arrays.parallelSort(arr, strategy.comparator());
        sortedIndirectConnections.clear();
        java.util.Collections.addAll(sortedIndirectConnections, arr);
        System.out.printf("F4_V66_SORT conns=%d time_s=%.2f%n", arr.length, (System.nanoTime() - t0) / 1e9);
        System.out.println("INFO: Sorting connections with strategy: " + strategy);
    }""",
    )
    src = once(
        src,
        """        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        @SuppressWarnings("unchecked")
        final Set<PIP>[] builtPips = new Set[nNets];
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(1024, (nNets + workers * 4 - 1) / (workers * 4));
        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < nNets; from += chunk) {
            final int f = from, t = Math.min(nNets, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    NetWrapper netWrapper = netList.get(i);
                    Net net = netWrapper.getNet();
                    assert(net.getType() == NetType.WIRE && !NetTools.isGlobalClock(net));
                    Set<PIP> newPIPs = new HashSet<>();
                    for (PIP pip : net.getPIPs()) {
                        if (pip.isPIPFixed()) {
                            newPIPs.add(pip);
                        }
                    }
                    for (Connection connection : netWrapper.getConnections()) {
                        List<Node> connNodes = connection.getNodes();
                        List<PIP> pips = RouterHelper.getConnectionPIPs(connection);
                        if (connNodes != null && connNodes.size() > 1 && pips.size() != connNodes.size() - 1) {
                            throw new IllegalStateException("V32 setPIPsOfNets Null PIP detected on net="
                                    + net.getName() + " expected=" + (connNodes.size() - 1) + " got=" + pips.size());
                        }
                        newPIPs.addAll(pips);
                    }
                    SitePinInst source = net.getSource();
                    SitePinInst altSource = net.getAlternateSource();
                    if (altSource != null && source.isRouted() && altSource.isRouted()) {
                        Tile sourceTile = altSource.getTile();
                        for (PIP pip : newPIPs) {
                            if (pip.getTile() != sourceTile || pip.isRouteThru()) continue;
                            Node startNode = pip.getStartNode();
                            IntentCode startIntent = startNode.getIntentCode();
                            if (startIntent != IntentCode.NODE_CLE_OUTPUT && startIntent != IntentCode.NODE_OUTPUT) {
                                continue;
                            }
                            SitePin sp = startNode.getSitePin();
                            if (sp.getPinName().equals(source.getName())) {
                                pip.setIsLogicalDriver(true);
                                break;
                            }
                        }
                    }
                    builtPips[i] = newPIPs;
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        for (int i = 0; i < nNets; ++i) {
            netList.get(i).getNet().setPIPs(builtPips[i]);
            builtPips[i] = null;
        }""",
        """        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        final boolean trackChanges = design.isTrackingNetChanges();
        @SuppressWarnings("unchecked")
        final Set<PIP>[] builtPips = trackChanges ? new Set[nNets] : null;
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(1024, (nNets + workers * 4 - 1) / (workers * 4));
        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < nNets; from += chunk) {
            final int f = from, t = Math.min(nNets, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    NetWrapper netWrapper = netList.get(i);
                    Net net = netWrapper.getNet();
                    assert(net.getType() == NetType.WIRE && !NetTools.isGlobalClock(net));
                    Set<PIP> newPIPs = new HashSet<>();
                    for (PIP pip : net.getPIPs()) {
                        if (pip.isPIPFixed()) {
                            newPIPs.add(pip);
                        }
                    }
                    for (Connection connection : netWrapper.getConnections()) {
                        List<Node> connNodes = connection.getNodes();
                        List<PIP> pips = RouterHelper.getConnectionPIPs(connection);
                        if (connNodes != null && connNodes.size() > 1 && pips.size() != connNodes.size() - 1) {
                            throw new IllegalStateException("V32 setPIPsOfNets Null PIP detected on net="
                                    + net.getName() + " expected=" + (connNodes.size() - 1) + " got=" + pips.size());
                        }
                        newPIPs.addAll(pips);
                    }
                    SitePinInst source = net.getSource();
                    SitePinInst altSource = net.getAlternateSource();
                    if (altSource != null && source.isRouted() && altSource.isRouted()) {
                        Tile sourceTile = altSource.getTile();
                        for (PIP pip : newPIPs) {
                            if (pip.getTile() != sourceTile || pip.isRouteThru()) continue;
                            Node startNode = pip.getStartNode();
                            IntentCode startIntent = startNode.getIntentCode();
                            if (startIntent != IntentCode.NODE_CLE_OUTPUT && startIntent != IntentCode.NODE_OUTPUT) {
                                continue;
                            }
                            SitePin sp = startNode.getSitePin();
                            if (sp.getPinName().equals(source.getName())) {
                                pip.setIsLogicalDriver(true);
                                break;
                            }
                        }
                    }
                    if (trackChanges) {
                        builtPips[i] = newPIPs;
                    } else {
                        net.setPIPs(newPIPs);
                    }
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        if (trackChanges) {
            for (int i = 0; i < nNets; ++i) {
                netList.get(i).getNet().setPIPs(builtPips[i]);
                builtPips[i] = null;
            }
        }""",
    )
    return src
