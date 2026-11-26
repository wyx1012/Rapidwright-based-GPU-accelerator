"""Patch v22 sources to v33:
- Inherits v28 FlatScopeData + DynamicExecutor + 32B NodeGeom + 8B DynNode
- Inherits v29/v30 cross-net conflict separation, F1Corridor retention, PPT P11 3-tier budgets
- v33 C++ search: 256-pause Dekker lock-free wakeup (zero 4096 spin bloat), removed out-of-box dyn prefetch,
  widened-heavy Pass-2/3 pre-routing, v31 early tight widening (R1-12) + tailPhase (iter>=22 || over<=1800)
- v33 Java post-route: 64-shard tileAddress-partitioned parallel RouteNode creation in f4NativeMaterializeAll,
  plus 64-shard incrementUser, TileType PIP prewarm + fail-closed setPIPsOfNets, 64-shard checkPIPsUsage, parallel statsWL
"""

import v27_patch as v27


def once(source, anchor, updated):
    return v27.once(source, anchor, updated)


def cuda(src):
    return v27.cuda(src)


def handoff(src):
    return v27.handoff(src)


def node(src):
    return v27.node(src)


def worker(src):
    return v27.worker(src)


def broker(src):
    return v27.broker(src)


def diversion(src):
    return v27.diversion(src)


def corridor(src):
    return v27.corridor(src)


def gate(src):
    src = v27.gate(src)
    src = once(
        src,
        'System.out.println("FIDES_NATIVE_CPU v27 deferred (pre-routed indirect conns exist)");',
        'System.out.println("FIDES_NATIVE_CPU v33 deferred (pre-routed indirect conns exist)");',
    )
    src = once(
        src,
        'System.out.printf("FIDES_NATIVE_CPU v27 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);',
        'System.out.printf("FIDES_NATIVE_CPU v33 registered=%d nets=%d init_s=%.2f%n", n, nextNetId, sec);',
    )
    src = once(
        src,
        """    int f4NativeUpdateHistAndFeedback(float histFactor, boolean enlargeBbox, int extX, int extY,
                                      int[] outNextActive, int[] outCongested) {
        return FidesNativeCpu.nativeUpdateHistAndFeedback(
                histFactor, enlargeBbox, extX, extY, outNextActive, outCongested);
    }""",
        """    int f4NativeUpdateHistAndFeedback(int routeIter, float histFactor, boolean enlargeBbox, int extX, int extY,
                                      int[] outNextActive, int[] outCongested) {
        return FidesNativeCpu.nativeUpdateHistAndFeedback(
                routeIter, histFactor, enlargeBbox, extX, extY, outNextActive, outCongested);
    }""",
    )
    src = once(
        src,
        """    void f4NativeRouteRound(int[] activeConns, int activeCount,
                            int[] bx0, int[] by0, int[] bx1, int[] by1, byte[] buse,
                            float shareWeight, float rnodeCostWeight, float rnodeWLWeight, float estWlWeight,
                            int wavePopCeiling, int numThreads, long[] outStats) {
        FidesNativeCpu.nativeRouteRound(activeConns, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                wavePopCeiling, numThreads, outStats);
    }""",
        """    void f4NativeRouteRound(int routeIter, int[] activeConns, int activeCount,
                            int[] bx0, int[] by0, int[] bx1, int[] by1, byte[] buse,
                            float shareWeight, float rnodeCostWeight, float rnodeWLWeight, float estWlWeight,
                            int wavePopCeiling, int numThreads, long[] outStats) {
        FidesNativeCpu.nativeRouteRound(routeIter, activeConns, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                wavePopCeiling, numThreads, outStats);
    }""",
    )
    src = once(
        src,
        """    void f4NativeMaterializeAll(List<Connection> all, RWRoute router, int overusedCount,
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
    }""",
        """    void f4NativeMaterializeAll(List<Connection> all, RWRoute router, int overusedCount,
                                java.util.Set<RouteNode> overUsedRnodes) {
        long t0 = System.nanoTime();
        final int n = all.size();
        final int[][] exported = new int[n][];
        final int workers = Math.max(1, router.f4SharedPool.getParallelism());
        final int chunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        if (f4NodeCache == null) {
            f4NodeCache = new RouteNode[rrg.N];
        }
        final RouteNode[] nodeCache = f4NodeCache;

        // Build fast tileAddress -> Tile lookup and prewarm highestBaseWireIndex per TileType once.
        final var allTiles = router.getDesign().getDevice().getAllTiles();
        int maxTileAddr = 0;
        for (com.xilinx.rapidwright.device.Tile tile : allTiles) {
            int ta = tile.getUniqueAddress();
            if (ta > maxTileAddr) maxTileAddr = ta;
        }
        final com.xilinx.rapidwright.device.Tile[] tileByAddr = new com.xilinx.rapidwright.device.Tile[maxTileAddr + 1];
        final var warmedWireTypes = new java.util.HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : allTiles) {
            tileByAddr[tile.getUniqueAddress()] = tile;
            if (warmedWireTypes.add(tile.getTileTypeEnum())) {
                graph.getHighestBaseWireIndex(tile, 0);
            }
        }

        // Phase 1: Parallel read-only JNI export across workers.
        var exportJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < n; from += chunk) {
            final int f = from, t = Math.min(n, from + chunk);
            exportJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int[] buf = new int[8192];
                for (int i = f; i < t; ++i) {
                    int len = FidesNativeCpu.nativeExportPath(i, buf);
                    if (len < 0) {
                        buf = new int[Math.max(buf.length * 2, -len)];
                        len = FidesNativeCpu.nativeExportPath(i, buf);
                    }
                    if (len > 0) {
                        exported[i] = java.util.Arrays.copyOf(buf, len);
                    }
                }
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                exportJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        long t1 = System.nanoTime();

        // Phase 2a: Parallel scatter of uncached node IDs into 64 disjoint tileAddress shards.
        final int SHARDS = 64;
        final int numBuckets = (n + chunk - 1) / chunk;
        final int[][][] missingIds = new int[numBuckets][SHARDS][];
        final int[][] missingLens = new int[numBuckets][SHARDS];
        var collectJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int f = bi * chunk;
            final int t = Math.min(n, f + chunk);
            collectJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int[][] localIds = new int[SHARDS][];
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
                }
                missingIds[b] = localIds;
                missingLens[b] = localLens;
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                collectJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        // Phase 2b: 64-shard parallel RouteNode instantiation (disjoint tileAddress per shard).
        var resolveJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
        for (int si = 0; si < SHARDS; ++si) {
            final int s = si;
            resolveJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int b = 0; b < numBuckets; ++b) {
                    int[] ids = missingIds[b][s];
                    int len = missingLens[b][s];
                    for (int idx = 0; idx < len; ++idx) {
                        int id = ids[idx];
                        if (nodeCache[id] != null) continue;
                        RouteNode rn = idMap.nodeAt(graph, id);
                        if (rn == null) {
                            int ta = idMap.tileAddress(id);
                            int wi = idMap.wireIndex(id);
                            com.xilinx.rapidwright.device.Tile tile =
                                    (ta >= 0 && ta < tileByAddr.length) ? tileByAddr[ta] : null;
                            if (tile != null) {
                                com.xilinx.rapidwright.device.Node devNode =
                                        com.xilinx.rapidwright.device.Node.getNode(tile, wi);
                                if (devNode != null) {
                                    rn = graph.getOrCreate(devNode);
                                }
                            }
                        }
                        if (rn != null) {
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
        int seqFixes = 0;
        for (int i = 0; i < n; ++i) {
            int[] path = exported[i];
            if (path == null) continue;
            RouteNode curParent = null;
            for (int k = path.length - 2; k >= 1; --k) {
                int id = path[k];
                if (nodeCache[id] == null) {
                    ++seqFixes;
                    if (curParent == null) {
                        curParent = (k == path.length - 2) ? all.get(i).getSourceRnode() : nodeCache[path[k + 1]];
                    }
                    RouteNode rn = f4FastNodeAt(id);
                    if (rn == null && curParent != null) {
                        for (RouteNode cand : curParent.getChildren(graph)) {
                            if (f4FastId(cand) == id) {
                                rn = cand;
                                f4NodeCache[id] = rn;
                                break;
                            }
                        }
                    }
                    if (rn == null) {
                        throw new IllegalStateException("V33 materialize missing node id=" + id + " conn=" + i);
                    }
                    curParent = rn;
                } else {
                    curParent = null;
                }
            }
        }
        long t2 = System.nanoTime();

        // Verify first 20 routed paths before parallel bind.
        int verifyRemaining = 20;
        int verified = 0;
        for (int i = 0; i < n && verifyRemaining > 0; ++i) {
            int[] path = exported[i];
            if (path == null) continue;
            Connection c = all.get(i);
            int len = path.length;
            RouteNode[] chain = new RouteNode[len];
            chain[0] = c.getSinkRnode();
            chain[len - 1] = c.getSourceRnode();
            for (int k = len - 2; k >= 1; --k) chain[k] = nodeCache[path[k]];
            for (int k = len - 2; k >= 0; --k) {
                boolean found = false;
                for (RouteNode cand : chain[k + 1].getChildren(graph)) {
                    if (cand == chain[k]) { found = true; break; }
                }
                if (!found) {
                    throw new IllegalStateException("V33 materialize illegal edge at hop " + k + " conn=" + i);
                }
            }
            --verifyRemaining;
            ++verified;
            if (verifyRemaining == 0) {
                System.out.println("V33_MATERIALIZE_VERIFY_OK checked=" + verified);
            }
        }

        // Phase 3: Parallel Connection rnode bind + 64-shard user bucket scatter across disjoint Connections.
        @SuppressWarnings("unchecked")
        final java.util.ArrayList<RouteNode>[][] userNodeBuckets = new java.util.ArrayList[numBuckets][SHARDS];
        @SuppressWarnings("unchecked")
        final java.util.ArrayList<NetWrapper>[][] userNetBuckets = new java.util.ArrayList[numBuckets][SHARDS];

        var bindJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int f = bi * chunk;
            final int t = Math.min(n, f + chunk);
            bindJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                @SuppressWarnings("unchecked")
                java.util.ArrayList<RouteNode>[] localNodes = new java.util.ArrayList[SHARDS];
                @SuppressWarnings("unchecked")
                java.util.ArrayList<NetWrapper>[] localNets = new java.util.ArrayList[SHARDS];
                for (int s = 0; s < SHARDS; ++s) {
                    localNodes[s] = new java.util.ArrayList<>();
                    localNets[s] = new java.util.ArrayList<>();
                }
                for (int i = f; i < t; ++i) {
                    Connection c = all.get(i);
                    c.resetRoute();
                    int[] path = exported[i];
                    if (path == null) {
                        c.setRouted(false);
                        continue;
                    }
                    int len = path.length;
                    RouteNode sinkRnode = c.getSinkRnode();
                    RouteNode srcRnode = c.getSourceRnode();
                    c.addRnode(sinkRnode);
                    for (int k = 1; k <= len - 2; ++k) {
                        c.addRnode(nodeCache[path[k]]);
                    }
                    c.addRnode(srcRnode);
                    c.setRouted(true);

                    int lo = 1, hi = len - 1;
                    if (c.hasAltSinks()) {
                        lo = 0;
                        hi = len;
                    }
                    if (hi > lo) {
                        NetWrapper nw = c.getNetWrapper();
                        if (lo == 0) {
                            int s0 = System.identityHashCode(sinkRnode) & (SHARDS - 1);
                            localNodes[s0].add(sinkRnode);
                            localNets[s0].add(nw);
                        }
                        for (int k = 1; k <= len - 2; ++k) {
                            RouteNode rn = nodeCache[path[k]];
                            int s = System.identityHashCode(rn) & (SHARDS - 1);
                            localNodes[s].add(rn);
                            localNets[s].add(nw);
                        }
                        if (hi == len) {
                            int sL = System.identityHashCode(srcRnode) & (SHARDS - 1);
                            localNodes[sL].add(srcRnode);
                            localNets[sL].add(nw);
                        }
                    }
                    exported[i] = null;
                }
                userNodeBuckets[b] = localNodes;
                userNetBuckets[b] = localNets;
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                bindJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        long t3 = System.nanoTime();

        // Phase 4: 64-shard parallel RouteNode.incrementUser (disjoint RouteNode ownership per shard).
        var userJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
        for (int si = 0; si < SHARDS; ++si) {
            final int s = si;
            userJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int b = 0; b < numBuckets; ++b) {
                    java.util.ArrayList<RouteNode> rns = userNodeBuckets[b][s];
                    java.util.ArrayList<NetWrapper> nws = userNetBuckets[b][s];
                    int sz = rns.size();
                    for (int k = 0; k < sz; ++k) {
                        rns.get(k).incrementUser(nws.get(k));
                    }
                    userNodeBuckets[b][s] = null;
                    userNetBuckets[b][s] = null;
                }
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                userJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

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
        long t4 = System.nanoTime();
        System.out.printf(
                "FIDES_NATIVE_CPU v33 materialized=%d overused=%d seq_fix=%d time_s=%.2f (export=%.2f cache=%.2f bind=%.2f users=%.2f)%n",
                n, overusedCount, seqFixes, (t4 - t0) / 1e9, (t1 - t0) / 1e9, (t2 - t1) / 1e9, (t3 - t2) / 1e9, (t4 - t3) / 1e9);
    }""",
    )
    return src


def router(src):
    src = v27.router(src)
    # Keep F1Corridor active across all rounds; C++ widens incrementally per-connection on congestion.
    src = once(
        src,
        "        if (routeIteration <= 5 && config.isUseBoundingBox()) {",
        "        if (config.isUseBoundingBox()) {",
    )
    src = once(
        src,
        """                        for (int k = f; k < t; ++k) {
                            Connection c = activeList.get(k);
                            if (routeIteration >= 4 && c.getHpwl() < 16) continue;
                            F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);""",
        """                        for (int k = f; k < t; ++k) {
                            Connection c = activeList.get(k);
                            F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);""",
    )
    src = once(
        src,
        """                for (int k = 0; k < activeCount; ++k) {
                    Connection c = activeList.get(k);
                    if (routeIteration >= 4 && c.getHpwl() < 16) continue;
                    F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);""",
        """                for (int k = 0; k < activeCount; ++k) {
                    Connection c = activeList.get(k);
                    F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);""",
    )
    src = once(
        src,
        """        final long[] stats = new long[11];

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
        long elapsedNs = stats[10];""",
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
    )
    src = once(
        src,
        "    private final java.util.concurrent.ForkJoinPool f4SharedPool =",
        "    final java.util.concurrent.ForkJoinPool f4SharedPool =",
    )
    src = once(
        src,
        """        System.out.printf(
                "F4_V27_ROUND iter=%d active=%d routed=%d par=%d ser=%d retries=%d heavy=%d fb=%d unrout=%d over=%d excess=%d pops=%d corridor_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, parCommits, serCommits, retries, heavy,
                fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess, roundPops,
                corridorSec, elapsedNs / 1e9);""",
        """        System.out.printf(
                "F4_V33_ROUND iter=%d active=%d routed=%d skip=%d par=%d ser=%d retries=%d same_ok=%d base_h=%d rel_h=%d fb=%d unrout=%d over=%d excess=%d pops=%d corridor_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, skippedLive, parCommits, serCommits, retries, sameNetOk,
                baseHeavy, relaxedHeavy, fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess, roundPops,
                corridorSec, elapsedNs / 1e9);""",
    )
    src = once(
        src,
        """            v27NextActiveCount = getFidesLaneGate().f4NativeUpdateHistAndFeedback(
                    historicalCongestionFactor, config.isEnlargeBoundingBox(),
                    config.getExtensionXIncrement(), config.getExtensionYIncrement(),
                    v27NextActive, null);""",
        """            v27NextActiveCount = getFidesLaneGate().f4NativeUpdateHistAndFeedback(
                    routeIteration, historicalCongestionFactor, config.isEnlargeBoundingBox(),
                    config.getExtensionXIncrement(), config.getExtensionYIncrement(),
                    v27NextActive, null);""",
    )
    src = once(
        src,
        """    private void computesNodeUsageAndTotalWirelength() {
        totalWL = 0;
        totalINTNodes = 0;
        nodeTypeUsage = new EnumMap<>(IntentCode.class);
        nodeTypeLength = new EnumMap<>(IntentCode.class);

        Set<Node> netNodes = new HashSet<>();
        for (Entry<Net,NetWrapper> e : nets.entrySet()) {
            NetWrapper netWrapper = e.getValue();
            for (Connection connection : netWrapper.getConnections()) {
                if (connection.getNodes() == null) {
                    continue;
                }
                    
                netNodes.addAll(connection.getNodes());
            }
            for (Node node : netNodes) {
                if (RouteNodeGraph.isExcludedTile(node)) {
                    continue;
                }
                totalINTNodes++;
                int wl = RouteNode.getLength(node, routingGraph);
                totalWL += wl;

                RouterHelper.addNodeTypeLengthToMap(node, wl, nodeTypeUsage, nodeTypeLength);
            }
            netNodes.clear();
        }
    }""",
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
        System.out.printf("F4_V33_STATS_WL nets=%d time_s=%.2f%n", nNets, (System.nanoTime() - t0) / 1e9);
    }""",
    )
    src = once(
        src,
        """    protected void setPIPsOfNets() {
        for (Entry<Net,NetWrapper> e : nets.entrySet()) {
            NetWrapper netWrapper = e.getValue();
            Net net = netWrapper.getNet();
            assert(net.getType() == NetType.WIRE && !NetTools.isGlobalClock(net));

            Set<PIP> newPIPs = new HashSet<>();
            // Start by carrying over all fixed PIPs (even those that didn't get used)
            for (PIP pip : net.getPIPs()) {
                if (pip.isPIPFixed()) {
                    newPIPs.add(pip);
                }
            }
            for (Connection connection:netWrapper.getConnections()) {
                List<PIP> pips = RouterHelper.getConnectionPIPs(connection);
                newPIPs.addAll(pips);
            }

            net.setPIPs(newPIPs);

            // When multiple sources are used (e.g. A_O and AMUX) then
            // mark the first PIP driven by either source as a logical driver
            SitePinInst source = net.getSource();
            SitePinInst altSource = net.getAlternateSource();
            if (altSource != null && source.isRouted() && altSource.isRouted()) {
                Tile sourceTile = altSource.getTile();
                for (PIP pip : net.getPIPs()) {
                    if (pip.getTile() != sourceTile) {
                        continue;
                    }
                    if (pip.isRouteThru()) {
                        continue;
                    }
                    Node startNode = pip.getStartNode();
                    IntentCode startIntent = startNode.getIntentCode();
                    if (startIntent != IntentCode.NODE_CLE_OUTPUT &&  // US+ and Versal
                            startIntent != IntentCode.NODE_OUTPUT) {  // US
                        continue;
                    }
                    SitePin sp = startNode.getSitePin();
                    if (sp.getPinName().equals(source.getName())) {
                        pip.setIsLogicalDriver(true);
                        break;
                    }
                }
            }
        }

        checkPIPsUsage();
    }

    /**
     * Checks if there are PIP overlaps among routed nets.
     */
    protected void checkPIPsUsage() {
        Map<PIP, Set<Net>> pipsUsage = new HashMap<>();
        for (Net net : design.getNets()) {
            for (PIP pip:net.getPIPs()) {
                pipsUsage.computeIfAbsent(pip, (k) -> new HashSet<>()).add(net);
            }
        }
        int pipsError = 0;
        for (Entry<PIP, Set<Net>> pipNets : pipsUsage.entrySet()) {
            if (pipNets.getValue().size() > 1) {
                if (pipsError < 10) {
                    System.out.println("pip " + pipNets.getKey() + " users = " + pipsUsage.get(pipNets.getKey()));
                }
                pipsError++;
            }
        }
        if (pipsError > 0)
            System.err.println("ERROR: PIPs overused error: " + pipsError);
        else
            System.out.println("\\nINFO: No PIP overlaps\\n");
    }""",
        """    protected void setPIPsOfNets() {
        long t0 = System.nanoTime();
        // Pre-warm TileType PIP lookup maps sequentially before concurrent reads in RouterHelper.getConnectionPIPs
        // (com.xilinx.rapidwright.device.i.a(int,int) lazily populates an unsynchronized HashMap on first touch).
        Set<Object> warmedTileTypes = new HashSet<>();
        for (Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
            }
        }
        final List<NetWrapper> netList = new ArrayList<>(nets.values());
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
        }
        System.out.printf("F4_V33_SETPIPS nets=%d tileTypes=%d time_s=%.2f%n", nNets, warmedTileTypes.size(), (System.nanoTime() - t0) / 1e9);

        checkPIPsUsage();
    }

    /**
     * Checks if there are PIP overlaps among routed nets (64-shard parallel, 0 HashSet allocations on clean routes).
     */
    protected void checkPIPsUsage() {
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
    )
    return src
