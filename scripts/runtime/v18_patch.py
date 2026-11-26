"""Extend the isolated v17-r2 sources to every ordinary routing round."""
from v11_window import replace_once as once


def router(s, work):
    s = once(s, "    private static final int F4_REGION_SHIFT = 4;",
        (work / "F4RerouteControl.inc").read_text() + "    private static final int F4_REGION_SHIFT = 4;")
    s = once(s, "if (routeIteration == 1 && !armed && !routingGraph.isVersal",
        "if (!routingGraph.isVersal")
    s = once(s, "&& c.getSinkRnode().getType().isAnyExclusiveSink() && c.getRnodes().isEmpty();",
        "&& c.getSinkRnode().getType().isAnyExclusiveSink();")
    s = once(s, "state.f4PrivatePopCeiling = c.connection.getHpwl() >= 16 ? 262144 : 32768;",
        "state.f4PrivatePopCeiling = c.connection.getHpwl() >= 16 ? 65536 : 16384;")
    s = once(s, """        push(state, source, 0f, 0f, false);
        return f4CpuSearch(state, tier, allowHandoff);""",
        """        push(state, source, 0f, 0f, false);
        return f4CpuSearch(state, tier, allowHandoff && !state.connection.isCrossSLR());""")
    for old in (
        "f4RegionBound(c, gate), p",
        "state.f4Boundary = f4RegionBound(c, gate);",
    ):
        s = s.replace(old, old.replace("f4RegionBound", "f4AdaptiveBound"))
    s = once(s, "    private static final class F4RegionQueue {", "    private final class F4RegionQueue {")
    s = once(s, "        final boolean[] claimed;", "        final boolean[] claimed;\n        final int[] blocker;")
    s = once(s, "        int indexed, occupied;", "        int indexed, occupied, regions, ripFootprints;")
    s = once(s, """                x1 = Math.max(x1, c.getXMaxBB() >> F4_REGION_SHIFT);
                y1 = Math.max(y1, c.getYMaxBB() >> F4_REGION_SHIFT);
            }""",
        """                x1 = Math.max(x1, c.getXMaxBB() >> F4_REGION_SHIFT);
                y1 = Math.max(y1, c.getYMaxBB() >> F4_REGION_SHIFT);
                for (RouteNode node : c.getRnodes()) {
                    int x = node.getEndTileXCoordinate() >> F4_REGION_SHIFT;
                    int y = node.getEndTileYCoordinate() >> F4_REGION_SHIFT;
                    x0 = Math.min(x0, x); x1 = Math.max(x1, x);
                    y0 = Math.min(y0, y); y1 = Math.max(y1, y);
                }
            }""")
    s = once(s, """            int[] last = new int[cells];
            java.util.Arrays.fill(last, -1);""",
        """            int[] last = new int[cells];
            long[] weight = new long[cells], conflict = new long[cells];
            for (Connection c : all) {
                if (!shouldRoute(c)) continue;
                F4HotPath hot = f4RecordHotPath(c);
                RouteNode sink = c.getSinkRnode();
                int x = (sink.getEndTileXCoordinate() >> F4_REGION_SHIFT) - x0;
                int y = (sink.getEndTileYCoordinate() >> F4_REGION_SHIFT) - y0;
                if (x >= 0 && x < w && y >= 0 && y < h) weight[y*w+x] += 1L + c.getRnodes().size();
                for (RouteNode n : c.getRnodes()) {
                    int a = (n.getEndTileXCoordinate() >> F4_REGION_SHIFT) - x0;
                    int b = (n.getEndTileYCoordinate() >> F4_REGION_SHIFT) - y0;
                    int cell = f4HotCell(n.getEndTileXCoordinate(), n.getEndTileYCoordinate());
                    conflict[b*w+a] += f4Pressure(cell) > 0 && hot != null ? 2 : 1;
                }
            }
            int[] ids = {0};
            F4RegionSplit partition = new F4RegionSplit(0, w-1, 0, h-1, weight, conflict, w, ids);
            regions = ids[0];
            java.util.Arrays.fill(last, -1);""")
    s = once(s, """            head = new int[cells];
            tail = new int[cells];""",
        """            blocker = new int[all.size()];
            head = new int[regions];
            tail = new int[regions];""")
    s = once(s, """            spatialHead = new int[cells];
            claimed = new boolean[all.size()];
            int[] spatialTail = new int[cells];""",
        """            spatialHead = new int[regions];
            claimed = new boolean[all.size()];
            int[] spatialTail = new int[regions];""")
    s = once(s, """                F1Corridor.Box b = c.isCrossSLR() ? F1Corridor.Box.original(c) : f4RegionEnvelope(c);""",
        """                F1Corridor.Box b = F1Corridor.Box.original(c);""")
    s = once(s, """                        waitFor = Math.max(waitFor, last[key]);
                        last[key] = i;""",
        """                        if (last[key] != i) waitFor = Math.max(waitFor, last[key]);
                        last[key] = i;""")
    s = once(s, """                if (c.hasAltSinks() || c.getSourceRnode() == c.getSinkRnode()
                        || !c.getSinkRnode().getType().isAnyExclusiveSink() || !c.getRnodes().isEmpty()) continue;
                int at = Math.min(u, Math.max(d, (d + u) / 2)) * w + Math.min(r, Math.max(l, (l + r) / 2));""",
        """                // Rip-up can affect cells outside the current search box.
                // Register those exact old-path cells before releasing later readers.
                int wl=l, wr=r, wd=d, wu=u;
                for (RouteNode n : c.getRnodes()) {
                    int a = (n.getEndTileXCoordinate() >> F4_REGION_SHIFT) - x0;
                    int b0 = (n.getEndTileYCoordinate() >> F4_REGION_SHIFT) - y0;
                    int key = b0*w + a;
                    if (last[key] == -1) ++occupied;
                    if (last[key] != i) waitFor = Math.max(waitFor, last[key]);
                    last[key] = i;
                    wl=Math.min(wl,a); wr=Math.max(wr,a); wd=Math.min(wd,b0); wu=Math.max(wu,b0);
                }
                if (!c.getRnodes().isEmpty()) ++ripFootprints;
                blocker[i] = waitFor;
                if (!f4FirstShape(c) || !shouldRoute(c)) continue;
                // Prefer the narrower of the simple same-SLR envelopes only
                // for assigning a region; strict dependencies still use base.
                F1Corridor.Box approx = f4RegionEnvelope(c);
                if (c.getRnodes().isEmpty() && !c.isCrossSLR()) {
                    wl=(approx.left >> F4_REGION_SHIFT)-x0; wr=(approx.right >> F4_REGION_SHIFT)-x0;
                    wd=(approx.bottom >> F4_REGION_SHIFT)-y0; wu=(approx.top >> F4_REGION_SHIFT)-y0;
                } else {
                    // Route endpoints anchor the queue; a long search crossing a cut goes to
                    // its mid-band rather than forcing the entire cut to be serialized.
                    RouteNode origin=c.getSourceRnode(), dest=c.getSinkRnode();
                    wl=(Math.min(origin.getEndTileXCoordinate(),dest.getEndTileXCoordinate()) >> F4_REGION_SHIFT)-x0;
                    wr=(Math.max(origin.getEndTileXCoordinate(),dest.getEndTileXCoordinate()) >> F4_REGION_SHIFT)-x0;
                    wd=(Math.min(origin.getEndTileYCoordinate(),dest.getEndTileYCoordinate()) >> F4_REGION_SHIFT)-y0;
                    wu=(Math.max(origin.getEndTileYCoordinate(),dest.getEndTileYCoordinate()) >> F4_REGION_SHIFT)-y0;
                    wl=Math.max(0,wl); wr=Math.min(w-1,wr); wd=Math.max(0,wd); wu=Math.min(h-1,wu);
                }
                int at = partition.bucket(wl,wr,wd,wu);""")
    s = once(s, "    private static final int F4_REGION_NEAR = 32768;",
        "    private static final int F4_REGION_NEAR = 8192;")
    s = once(s, "        long regionSpecSelected;",
        "        long regionSpecSelected, regionLeaves, ripFootprints, regionWaitNs, regionCommitNs, hotWidened, chainSerial, gpuBatches;")
    s = once(s, '.put("indirect_gpu_batches", 0)', '.put("indirect_gpu_batches", gpuBatches)')
    s = once(s, '               .put("region_spec_selected", regionSpecSelected);',
        '''               .put("region_spec_selected", regionSpecSelected)
               .put("dependency_buckets",regionLeaves).put("old_path_footprints",ripFootprints)
               .put("group_wait_ms",regionWaitNs / 1_000_000L).put("ordered_commit_ms",regionCommitNs / 1_000_000L)
               .put("hot_widened",hotWidened).put("serial_chain",chainSerial);''')
    s = once(s, """        // Workers own temporary labels. Only the coordinator writes occupancy,""",
        """        long waitStarted = System.nanoTime();
        // Workers own temporary labels. Only the coordinator writes occupancy,""")
    s = once(s, """                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        for (int k""",
        """                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        f4First.regionWaitNs += System.nanoTime() - waitStarted;
        for (int k""")
    s = once(s, """            long prep = System.nanoTime();
            F4RegionQueue queue""",
        """            long prep = System.nanoTime();
            f4ReadCongestion();
            f4StartCriticalRound(all);
            int retryFillAt = 0;
            F4RegionQueue queue""")
    s = once(s, "            f4First.regionCells = queue.occupied;",
        """            f4First.regionCells = queue.occupied;
            f4First.regionLeaves = queue.regions;
            f4First.ripFootprints = queue.ripFootprints;""")
    s = once(s, """                if (f4SharedPool.getParallelism() >= 2
                        && (!ready.containsKey(i) || ready.size() < 16)) {
                    f4RegionFill(queue, all, i, gate, ready);
                }""",
        """                if (f4SharedPool.getParallelism() >= 2 && i >= retryFillAt
                        && (!ready.containsKey(i) || ready.size() < 12)) {
                    long beforeGroup = f4First.groups;
                    f4RegionFill(queue, all, i, gate, ready);
                    if (beforeGroup == f4First.groups) retryFillAt = i + 16;
                }""")
    s = once(s, """                if (r != null) {
                    r.state.sequence""",
        """                if (r != null) {
                    long commitStarted = System.nanoTime();
                    r.state.sequence""")
    s = once(s, """                    ++f4First.parallelCommits;
                    ++f4SpatialCommits;""",
        """                    ++f4First.parallelCommits;
                    ++f4SpatialCommits;
                    f4First.regionCommitNs += System.nanoTime() - commitStarted;""")
    s = once(s, """                    state.f4Boundary = f4AdaptiveBound(c, gate);
                    f4FirstBoxTally""",
        """                    state.f4Boundary = f4AdaptiveBound(c, gate);
                    var hot = f4HotPaths.get(c);
                    if (hot != null && hot.round == routeIteration && hot.streak >= 2) {
                        var previous = gate.f4Boundary(c);
                        if (previous != null && (state.f4Boundary == null || !previous.same(state.f4Boundary)))
                            ++f4First.hotWidened;
                    }
                    f4FirstBoxTally""")
    s = once(s, """                    var search = f4SearchWithBoundary(state, tier, false);""",
        """                    boolean chain = queue.blocker[i] >= 0 && f4FirstShape(c);
                    if (chain) ++f4First.chainSerial;
                    boolean offer = f4MayOfferCritical(c, state.f4Boundary, tier, chain);
                    var search = f4SearchWithBoundary(state, tier, offer);""")
    s = once(s, """                    f4ObserveCpu(state, tier, search, false);
                    f4FinishCpu(state, search.target, gate);""",
        """                    boolean handoff = offer && search.target == null && search.crossingNs >= 0
                            && F4RoundDiversion.crossed(state.nodesPopped, F4RoundDiversion.budget(tier),
                                                       !state.queue.isEmpty());
                    if (handoff) {
                        long start = System.nanoTime();
                        f4CriticalProofPops += state.nodesPopped;
                        f4ObserveCpu(state, tier, search, true);
                        var batch = new ArrayList<LaneBatchSlot>(1);
                        f4CollectProven(state, F4RoundDiversion.budget(tier), state.nodesPopped, batch);
                        runAndCommitF4Batch(batch);
                        f4RecordCriticalGpu(System.nanoTime() - start);
                        ++f4First.gpuBatches;
                    } else {
                        if (chain && tier >= 0) f4RecordCriticalTail(c, search);
                        f4ObserveCpu(state, tier, search, false);
                        f4FinishCpu(state, search.target, gate);
                    }""")
    return once(s, '        System.out.println("F4_PPT11_ROUND " + decision);',
        '''        if (f4CurrentRegionRound == routeIteration) {
            decision.put("critical_observed_cpu_tails",f4CriticalTailSamples)
                    .put("critical_cold_start_surplus_ns",f4CriticalPotential)
                    .put("critical_gpu_actual_batches",f4CriticalStarts)
                    .put("critical_gpu_wall_ns",f4CriticalGpuNs)
                    .put("critical_gpu_width",f4CriticalStarts == 0 ? 0 : 1)
                    .put("critical_gpu_first_complete_ns",f4GpuColdActual)
                    .put("critical_proof_pops",f4CriticalProofPops);
            f4CriticalPrior.clear();
            f4CriticalPrior.putAll(f4CriticalNext);
            f4CriticalNext.clear();
        }
        System.out.println("F4_PPT11_ROUND " + decision);''')


def policy(s):
    return once(s, "    private long batchQuote(int members) {",
        """    long f4SingletonQuote() { return batchQuote(1); }
    long f4StartupQuote() { return resident ? 0L : coldNs; }

    private long batchQuote(int members) {""")

