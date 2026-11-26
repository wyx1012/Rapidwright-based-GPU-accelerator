"""Patch v22 sources to v24: P0 (36-core NUMA unlock, -ea off, parallel region init,
safe speculative scheduling, flat F4ScratchPages) + P1 (8-lane resident GPU, multi-slot
same-net batching in f4RouteFirstRound, 16k tier-1 proof budget, amortized GPU pricing)."""


def once(source, anchor, updated):
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(f"Expected 1 occurrence (got {count}) for anchor: {anchor.strip().splitlines()[0]}")
    return source.replace(anchor, updated, 1)


def cuda(src):
    return once(
        src,
        "    growMaps(preparation ? 1 : channels);",
        """    U32 residentLanes = 1;
    if (const char* envLanes = std::getenv("FIDES_V23_RESIDENT_LANES")) {
        residentLanes = std::min<U32>(channels, std::max<U32>(1u, static_cast<U32>(std::stoul(envLanes))));
    }
    growMaps(preparation ? residentLanes : channels);""",
    )


def handoff(src):
    # Pre-allocate up to 8 resident GPU lanes (or 4 if VRAM is tight) and restore full-node CPU affinity.
    src = once(
        src,
        """    lane = 4 * (n + m)
    minimum = shared + lane + RESERVE""",
        """    lane = 4 * (n + m)
    target_lanes = min(8, int(ctx["width"]))
    minimum = shared + lane + RESERVE""",
    )
    src = once(
        src,
        """        folder.mkdir()
        cmd = [str(ctx["binary"]), str(folder), str(n), str(m), str(pcc), str(folder),
               str(rrg), str(layout), str(folder / "unused_slr"), "15", str(ctx["width"])]
        device = None
        try:
            save("resident_preflight_starting", gpu_uuid=uuid, resident_attempt=attempt,
                 resident_minimum_bytes=minimum, resident_shared_bytes=shared,
                 resident_per_lane_bytes=lane, resident_reserve_bytes=RESERVE)
            started = time.perf_counter_ns()
            device, log = launch(f"resident_cuda_{attempt}", cmd, env | {
                "CUDA_VISIBLE_DEVICES": uuid,
                "FIDES_V20_RRG_OFFSETS": " ".join(map(str, offsets)),
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
                    or int(ready[-1][0]) < shared + lane or f"STATE_MAP 1 {lane}\\n" not in log_text):
                raise RuntimeError("V20 ready receipt does not show the one-GiB reserve")
            owners = ctx["gpu"]("compute-apps", "gpu_uuid,pid").splitlines()
            if f"{uuid}, {device.pid}" not in owners:
                raise RuntimeError("Prepared CUDA PID absent from the selected device")
            receipt = dict(pid=device.pid, gpu_uuid=uuid, attempt=attempt, startup_ns=startup_ns,
                           pcc=pcc, nodes=n, edges=m, slr_words=slr_words, resident_lanes=1,
                           shared_bytes=shared, per_lane_bytes=lane,
                           actual_bytes=int(ready[-1][0]), free_bytes=int(ready[-1][1]),
                           reserved_headroom_bytes=RESERVE, no_route_issued=True, log=str(log))
            publish(work / "resident_preflight.json", receipt)
            save("resident_preflight_ready", gpu_pid=device.pid, gpu_uuid=uuid,
                 resident_preflight=receipt)
            return locks, receipt | {"process": device}""",
        """        folder.mkdir()
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
    )
    return src


def worker(src):
    # Drop -ea (assertions) for production routing speed and unlock all 36 CPU cores across NUMA.
    src = once(
        src,
        'script = swap(script, \'    args = [str(java/"java"), "-ea", "-Xmx256G", "-XX:ActiveProcessorCount=4",\', \'    args = [str(java/"java"), "-ea", "-Xmx256G", "-XX:ActiveProcessorCount=4", "-Dfides.ppt.f4.crossSlrCorridor=true", "-Dfides.ppt.f4.crossSlrCompactX=true", "-Dfides.ppt.f4.multiSlrCorridor=true",\')',
        'script = swap(script, \'    args = [str(java/"java"), "-ea", "-Xmx256G", "-XX:ActiveProcessorCount=4",\', \'    args = [str(java/"java"), "-Xmx256G", f"-XX:ActiveProcessorCount={max(2, len(os.sched_getaffinity(0)))}", "-Dfides.ppt.f4.crossSlrCorridor=true", "-Dfides.ppt.f4.crossSlrCompactX=true", "-Dfides.ppt.f4.multiSlrCorridor=true",\')',
    )
    src = once(
        src,
        "    return v20_drive(window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source)))))))",
        "    out = v20_drive(window_broker(bridge_broker(hybrid_broker(active_broker(patch_host_drive(score_broker(source)))))))\n"
        "    out = out.replace('args.insert(4, f\"-XX:ActiveProcessorCount={max(2, min(16, len(os.sched_getaffinity(0))))}\")', 'pass')\n"
        "    return out",
    )
    return src


def broker(src):
    return once(
        src,
        'and member["cpu_budget"] in (8000, 262144, 1048576) and pops > member["cpu_budget"],',
        'and member["cpu_budget"] in (8000, 16384, 32768, 262144, 1048576) and pops > member["cpu_budget"],',
    )


def diversion(src):
    return once(
        src,
        "private static final int[] BUDGETS = {8_000, 262_144, 1_048_576};",
        "private static final int[] BUDGETS = {8_000, 16_384, 262_144};",
    )


def corridor(src):
    src = once(
        src,
        "    private boolean multiSlrWitness;\n",
        "    private volatile boolean multiSlrWitness;\n",
    )
    src = once(
        src,
        """    void disable(Connection connection) {
        f4Retired.add(connection);
        candidates.put(connection, null);
    }""",
        """    void ensurePortalsIndexed(FidesRrgLoader rrg, RouteNodeGraph graph) {
        if (crossSlrCorridor && graph.wireIndicesLeadingToLaguna != null && !portalsIndexed) {
            indexPortals(rrg, graph);
        }
    }

    static final class ParallelEntry {
        final Candidate candidate;
        final Box compact;
        ParallelEntry(Candidate candidate, Box compact) {
            this.candidate = candidate;
            this.compact = compact;
        }
    }

    Box boxParallel(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph,
                    ParallelEntry[] out, int index) {
        if (!connection.isCrossSLR()) {
            RouteNode source = connection.getSourceRnode();
            RouteNode sink = connection.getSinkRnode();
            if (source == null || sink == null || connection.hasAltSinks()
                    || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) {
                return f4LongEnvelope(connection);
            }
            if (connection.getHpwl() >= 16) {
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
            return chosen != null ? chosen : f4LongEnvelope(connection);
        }
        if (!crossSlrCorridor || connection.hasAltSinks() || connection.getHpwl() < 16
                || graph.wireIndicesLeadingToLaguna == null) return null;
        RouteNode source = connection.getSourceRnode(), sink = connection.getSinkRnode();
        if (source == null || sink == null) return null;
        int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
        int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
        Box base = Box.original(connection);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        if (candidates.containsKey(connection)) {
            Candidate existing = candidates.get(connection);
            if (existing == null || existing.source != source || existing.sink != sink || !existing.base.same(base)) {
                return null;
            }
            return crossSlrCompactX ? compactCrossSlr.getOrDefault(connection, existing.tight()) : existing.tight();
        }
        Box[] compactBox = new Box[1];
        Candidate planned = planCrossParallel(connection, source, sink, base, sx, sy, tx, ty, graph, compactBox);
        out[index] = new ParallelEntry(planned, compactBox[0]);
        if (planned == null) return null;
        return crossSlrCompactX && compactBox[0] != null ? compactBox[0] : planned.tight();
    }

    private Candidate planCrossParallel(Connection connection, RouteNode source, RouteNode sink, Box base,
                                        int sx, int sy, int tx, int ty, RouteNodeGraph graph, Box[] compactBox) {
        int from = rawSlr(graph, sy), to = rawSlr(graph, ty);
        if (from < 0 || to < 0 || from == to
                || connection.isCrossSLRnorth() != (ty > sy)
                || connection.isCrossSLRsouth() != (ty < sy)) return null;
        if (Math.abs(from - to) > 1) {
            if (!multiSlrCorridor) return null;
            Box tight = planMultiSlrBox(base, from, to, sx, sy, tx, ty);
            if (tight == null) return null;
            if (!multiSlrWitness) {
                synchronized (this) {
                    if (!multiSlrWitness) {
                        multiSlrWitness = true;
                        System.out.printf("F4_MULTI_SLR_CORRIDOR from=%d to=%d boundaries=%d source=%d,%d sink=%d,%d base=%s tight=%s%n",
                                from, to, Math.abs(from - to), sx, sy, tx, ty, base.fields(), tight.fields());
                    }
                }
            }
            return new Candidate(base, tight, source, sink);
        }
        var direction = portals.get(cellKey(from, to));
        if (direction == null) return null;
        Box sourceBox = slrBox(base, from), sinkBox = slrBox(base, to);
        Portal best = null;
        int bestDetour = Integer.MAX_VALUE;
        for (Portal portal : direction) {
            if (!portal.inside(base) || !sourceBox.contains(portal.ingressX, portal.ingressY)
                    || !sinkBox.contains(portal.exitX, portal.exitY)
                    || !fabric.contains(portal.ingressX, portal.ingressY)
                    || !fabric.contains(portal.exitX, portal.exitY)) continue;
            int detour = portal.detour(sx, sy, tx, ty);
            if (detour < bestDetour) {
                best = portal;
                bestDetour = detour;
            }
        }
        if (best == null) return null;
        Choice first = choose(sourceBox, sx, sy, best.ingressX, best.ingressY);
        Choice last = choose(sinkBox, best.exitX, best.exitY, tx, ty);
        if (first == null || last == null) return null;
        Box tight = new Box(Math.min(Math.min(first.box.left, last.box.left), best.sllX - 1),
                Math.min(Math.min(first.box.bottom, last.box.bottom), best.sllRawY - 1),
                Math.max(Math.max(first.box.right, last.box.right), best.sllX + 1),
                Math.max(Math.max(first.box.top, last.box.top), best.sllRawY + 1));
        if (!tight.contains(sx, sy) || !tight.contains(tx, ty) || !best.inside(tight)
                || tight.same(base)) return null;
        if (crossSlrCompactX) {
            compactBox[0] = compactCrossSlrX(tight, best, sx, tx);
        }
        return new Candidate(base, tight, source, sink);
    }

    void commitParallel(java.util.List<Connection> all, ParallelEntry[] entries) {
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
    )
    return src


def gate(src):
    return once(
        src,
        """    F1Corridor.Box f4Boundary(Connection c) {
        return config.isUseBoundingBox() && !config.isTimingDriven()
                ? router.f4CorridorBox(c, rrg, graph) : null;
    }""",
        """    F1Corridor.Box f4Boundary(Connection c) {
        return config.isUseBoundingBox() && !config.isTimingDriven()
                ? router.f4CorridorBox(c, rrg, graph) : null;
    }

    void ensureF4PortalsIndexed() {
        if (config.isUseBoundingBox() && !config.isTimingDriven()) {
            router.f4EnsureCorridorPortals(rrg, graph);
        }
    }

    F1Corridor.Box f4BoundaryParallel(Connection c, F1Corridor.ParallelEntry[] out, int index) {
        return config.isUseBoundingBox() && !config.isTimingDriven()
                ? router.f4CorridorBoxParallel(c, rrg, graph, out, index) : null;
    }

    void commitF4BoundaryParallel(java.util.List<Connection> all, F1Corridor.ParallelEntry[] out) {
        if (config.isUseBoundingBox() && !config.isTimingDriven()) {
            router.f4CommitCorridorParallel(all, out);
        }
    }""",
    )


def node(src):
    src = once(
        src,
        """        private static final class Page {
            final long[] stamps = new long[1 << SHIFT];
            final ScratchValues[] slots = new ScratchValues[1 << SHIFT];
        }

        private final java.util.HashMap<Long, Page> pages = new java.util.HashMap<>();
        private final ArrayList<RouteNode> touched = new ArrayList<>();
        private long stamp = 1;
        private long lastKey = -1;
        private Page lastPage;

        private Page page(RouteNode node, boolean create) {
            int wire = node.getWireIndex();
            long address = ((long) node.getTile().getUniqueAddress() << 32) | (wire >>> SHIFT);
            if (lastKey == address && lastPage != null) return lastPage;
            Page page = pages.get(address);
            if (page == null && create) {
                page = new Page();
                pages.put(address, page);
            }
            lastKey = address;
            lastPage = page;
            return page;
        }""",
        """        private static final class Page {
            final long[] stamps = new long[1 << SHIFT];
            final long[] readStamps = new long[1 << SHIFT];
            final ScratchValues[] slots = new ScratchValues[1 << SHIFT];
        }

        private static final long EMPTY_PAGE = Long.MIN_VALUE;
        private long[] pageKeys = new long[512];
        private Page[] pageVals = new Page[512];
        private int pageCount = 0;
        { java.util.Arrays.fill(pageKeys, EMPTY_PAGE); }
        private final ArrayList<RouteNode> touched = new ArrayList<>();
        private final ArrayList<RouteNode> readList = new ArrayList<>();
        private long stamp = 1;
        private long lastKey = EMPTY_PAGE;
        private Page lastPage;

        private static int mixKey(long k) {
            long z = k * 0x9E3779B97F4A7C15L;
            return (int) (z ^ (z >>> 32));
        }

        private void growPages() {
            int newCap = pageKeys.length << 1;
            int mask = newCap - 1;
            long[] nk = new long[newCap];
            Page[] nv = new Page[newCap];
            java.util.Arrays.fill(nk, EMPTY_PAGE);
            for (int i = 0; i < pageKeys.length; ++i) {
                long k = pageKeys[i];
                if (k == EMPTY_PAGE) continue;
                int idx = mixKey(k) & mask;
                while (nk[idx] != EMPTY_PAGE) idx = (idx + 1) & mask;
                nk[idx] = k;
                nv[idx] = pageVals[i];
            }
            pageKeys = nk;
            pageVals = nv;
        }

        private Page page(RouteNode node, boolean create) {
            int wire = node.getWireIndex();
            long address = ((long) node.getTile().getUniqueAddress() << 32) | (wire >>> SHIFT);
            if (lastKey == address && lastPage != null) return lastPage;
            int mask = pageKeys.length - 1;
            int idx = mixKey(address) & mask;
            Page page = null;
            while (true) {
                long k = pageKeys[idx];
                if (k == address) {
                    page = pageVals[idx];
                    break;
                }
                if (k == EMPTY_PAGE) {
                    if (create) {
                        if ((pageCount + 1) * 10 >= pageKeys.length * 7) {
                            growPages();
                            mask = pageKeys.length - 1;
                            idx = mixKey(address) & mask;
                            while (pageKeys[idx] != EMPTY_PAGE) idx = (idx + 1) & mask;
                        }
                        page = new Page();
                        pageKeys[idx] = address;
                        pageVals[idx] = page;
                        ++pageCount;
                    }
                    break;
                }
                idx = (idx + 1) & mask;
            }
            if (page != null) {
                lastKey = address;
                lastPage = page;
            }
            return page;
        }

        void markRead(RouteNode node) {
            Page p = page(node, true);
            int slot = node.getWireIndex() & MASK;
            if (p.readStamps[slot] != stamp) {
                p.readStamps[slot] = stamp;
                readList.add(node);
            }
        }

        boolean isRead(RouteNode node) {
            Page p = page(node, false);
            return p != null && p.readStamps[node.getWireIndex() & MASK] == stamp;
        }""",
    )
    src = once(
        src,
        """        @Override
        public void clear() {
            touched.clear();
            ++stamp;
        }""",
        """        @Override
        public void clear() {
            touched.clear();
            readList.clear();
            ++stamp;
        }""",
    )
    src = once(
        src,
        """        private final java.util.Set<RouteNode> readNodes =
                java.util.Collections.newSetFromMap(new IdentityHashMap<>());""",
        """        private final java.util.Set<RouteNode> readNodes = new java.util.AbstractSet<RouteNode>() {
            @Override public int size() { return values.readList.size(); }
            @Override public boolean contains(Object o) {
                return (o instanceof RouteNode) && values.isRead((RouteNode) o);
            }
            @Override public boolean add(RouteNode node) {
                values.markRead(node);
                return true;
            }
            @Override public void clear() { values.readList.clear(); }
            @Override public java.util.Iterator<RouteNode> iterator() { return values.readList.iterator(); }
        };""",
    )
    return src


def router(src):
    # 1. Corridor parallel helpers
    src = once(
        src,
        """    F1Corridor.Box f4CorridorBox(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph) {
        return f4Corridor.box(connection, rrg, graph);
    }""",
        """    F1Corridor.Box f4CorridorBox(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph) {
        return f4Corridor.box(connection, rrg, graph);
    }

    void f4EnsureCorridorPortals(FidesRrgLoader rrg, RouteNodeGraph graph) {
        f4Corridor.ensurePortalsIndexed(rrg, graph);
    }

    F1Corridor.Box f4CorridorBoxParallel(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph,
                                          F1Corridor.ParallelEntry[] out, int index) {
        return f4Corridor.boxParallel(connection, rrg, graph, out, index);
    }

    void f4CommitCorridorParallel(List<Connection> all, F1Corridor.ParallelEntry[] out) {
        f4Corridor.commitParallel(all, out);
    }""",
    )

    # 2. F4SpatialResult footprint
    src = once(
        src,
        """    private static final class F4SpatialResult {
        final ConnectionState state;
        final RouteNode.SearchScratch scratch;
        final F4CpuSearch search;
        final List<RouteNode> path;
        final int tier;
        F4SpatialResult(ConnectionState state, RouteNode.SearchScratch scratch,
                        F4CpuSearch search, List<RouteNode> path, int tier) {""",
        """    private static final class F4SpatialResult {
        final ConnectionState state;
        final RouteNode.SearchScratch scratch;
        final F4CpuSearch search;
        final List<RouteNode> path;
        final int tier;
        F1Corridor.Box footprint;
        F4SpatialResult(ConnectionState state, RouteNode.SearchScratch scratch,
                        F4CpuSearch search, List<RouteNode> path, int tier) {""",
    )

    # 3. Amortized multi-lane GPU pricing & lower tier=0 floor (4 ms per member instead of 25 ms)
    src = once(
        src,
        """        if (cost.gpuSamples < 2) {
            if (cost.gpuTrials >= 3 || f4GpuTrials >= F4_MAX_GPU_TRIALS) return 0;
            long warmFloor = f21MinimumWarmNs == Long.MAX_VALUE
                    ? 25_000_000L : Math.max(25_000_000L, f21MinimumWarmNs);
            if (tier == 0 && cost.cpuMean() <= warmFloor + warmFloor / 4) return 0;
            // Use the phase forecast with live proofs after a phase transition.
            if (f21EstimatedProofs[tier] == 0 && f21Proofs[tier] < 2) return 0;
            return F4_GPU_TRIAL;
        }""",
        """        if (cost.gpuSamples < 2) {
            if (cost.gpuTrials >= 3 || f4GpuTrials >= F4_MAX_GPU_TRIALS) return 0;
            long warmFloor = f21MinimumWarmNs == Long.MAX_VALUE
                    ? 4_000_000L : Math.max(3_000_000L, Math.min(8_000_000L, f21MinimumWarmNs));
            if (tier == 0 && cost.cpuMean() <= warmFloor + warmFloor / 4) return 0;
            if (tier > 0 && f21EstimatedProofs[tier] == 0 && f21Proofs[tier] < 2) return 0;
            return F4_GPU_TRIAL;
        }""",
    )

    # 4. Region granularity 16x16 (SHIFT=4), wide lookahead (65536), pending=128
    src = once(
        src,
        """    private static final int F4_REGION_SHIFT = 3;
    private static final int F4_REGION_PENDING = 64;
    private static final int F4_REGION_NEAR = 256;""",
        """    private static final int F4_REGION_SHIFT = 4;
    private static final int F4_REGION_PENDING = 128;
    private static final int F4_REGION_NEAR = 65536;""",
    )

    # 5. Parallel corridor bound helper
    src = once(
        src,
        """    private F1Corridor.Box f4AdaptiveBound(Connection c, FidesLaneGate gate) {""",
        """    private F1Corridor.Box f4AdaptiveBoundParallel(Connection c, FidesLaneGate gate,
                                                   F1Corridor.ParallelEntry[] out, int index) {
        if (f4RegionRetired != null && f4RegionRetired.contains(c)) return null;
        F1Corridor.Box preferred = gate.f4BoundaryParallel(c, out, index);
        if (preferred == null) return null;
        F1Corridor.Box base = F1Corridor.Box.original(c);
        var hot = f4HotPaths.get(c);
        if (hot == null || hot.round != routeIteration || hot.streak < 2) return preferred;
        int x = ((hot.cell >> 8) << 4) - 248;
        int y = ((hot.cell & 255) << 4) - 248;
        if (x + 8 <= preferred.left || x - 8 >= preferred.right
                || y + 8 <= preferred.bottom || y - 8 >= preferred.top) return preferred;
        int step = 8 * Math.min(hot.streak - 1, 4);
        int edge = 0;
        int[] score = {
            preferred.left > base.left ? f4Pressure(f4HotCell(preferred.left - step / 2, y)) * 32
                    + (x - preferred.left) : Integer.MAX_VALUE,
            preferred.right < base.right ? f4Pressure(f4HotCell(preferred.right + step / 2, y)) * 32
                    + (preferred.right - x) : Integer.MAX_VALUE,
            preferred.bottom > base.bottom ? f4Pressure(f4HotCell(x, preferred.bottom - step / 2)) * 32
                    + (y - preferred.bottom) : Integer.MAX_VALUE,
            preferred.top < base.top ? f4Pressure(f4HotCell(x, preferred.top + step / 2)) * 32
                    + (preferred.top - y) : Integer.MAX_VALUE};
        for (int i = 1; i < score.length; ++i) if (score[i] < score[edge]) edge = i;
        F1Corridor.Box wider = new F1Corridor.Box(
            edge == 0 ? Math.max(base.left, preferred.left - step) : preferred.left,
            edge == 2 ? Math.max(base.bottom, preferred.bottom - step) : preferred.bottom,
            edge == 1 ? Math.min(base.right, preferred.right + step) : preferred.right,
            edge == 3 ? Math.min(base.top, preferred.top + step) : preferred.top);
        return wider.same(base) ? null : wider;
    }

    private F1Corridor.Box f4AdaptiveBound(Connection c, FidesLaneGate gate) {""",
    )

    # 6. Parallel F4RegionQueue init + speculative regionHeads
    src = once(
        src,
        """    private final class F4RegionQueue {
        final int[] releaseHead, next, home, blocker, rank;
        final F1Corridor.Box[] bounds;
        final byte[] service, geometry;
        final boolean[] narrowed;
        final java.util.TreeSet<Integer> runnable = new java.util.TreeSet<>();
        final List<PriorityQueue<Integer>> byRegion = new ArrayList<>();
        final long[] supply = new long[3];
        int indexed, occupied, regions, ripFootprints, active, featured;

        F4RegionQueue(List<Connection> all, FidesLaneGate gate) {
            int n = all.size();
            releaseHead = new int[n];
            next = new int[n];
            home = new int[n];
            blocker = new int[n];
            rank = new int[n];
            bounds = new F1Corridor.Box[n];
            service = new byte[n];
            geometry = new byte[n];
            narrowed = new boolean[n];
            int[] histogram = new int[32];
            java.util.Arrays.fill(blocker, -1);
            java.util.Arrays.fill(releaseHead, -1);
            java.util.Arrays.fill(home, -1);
            java.util.Arrays.fill(service, (byte) -1);
            int[] eligible = new int[n];
            int[] left = new int[n], right = new int[n], down = new int[n], up = new int[n];
            int x0 = Integer.MAX_VALUE, y0 = Integer.MAX_VALUE;
            int x1 = Integer.MIN_VALUE, y1 = Integer.MIN_VALUE;
            for (int i = 0; i < n; ++i) {
                Connection c = all.get(i);
                rank[i] = active;
                if (!shouldRoute(c)) continue;
                ++active;
                f4RecordHotPath(c);
                F1Corridor.Box chosen = f4AdaptiveBound(c, gate);
                narrowed[i] = chosen != null;
                bounds[i] = chosen == null ? F1Corridor.Box.original(c) : chosen;
                F1Corridor.Box b = bounds[i];
                int l = b.left >> F4_REGION_SHIFT, r = b.right >> F4_REGION_SHIFT;
                int d = b.bottom >> F4_REGION_SHIFT, u = b.top >> F4_REGION_SHIFT;
                for (RouteNode old : c.getRnodes()) {
                    int x = old.getEndTileXCoordinate() >> F4_REGION_SHIFT;
                    int y = old.getEndTileYCoordinate() >> F4_REGION_SHIFT;
                    l = Math.min(l, x);
                    r = Math.max(r, x);
                    d = Math.min(d, y);
                    u = Math.max(u, y);
                }
                if (!c.getRnodes().isEmpty()) ++ripFootprints;
                left[i] = l;
                right[i] = r;
                down[i] = d;
                up[i] = u;
                x0 = Math.min(x0, l);
                x1 = Math.max(x1, r);
                y0 = Math.min(y0, d);
                y1 = Math.max(y1, u);
                if (f4FirstShape(c) && !f22SkipPrivatePrev.contains(c)) eligible[indexed++] = i;
                int bucket = f21Geometry(c, b);
                geometry[i] = (byte) bucket;
                if (bucket >= 0) {
                    ++featured;
                    ++histogram[bucket];
                }
            }
            if (active == 0) return;
            f21HeavyGeometry = f21Quantile(histogram, featured, 90);
            f21RareGeometry = f21Quantile(histogram, featured, 99);

            F21Partition partition = new F21Partition(left, right, down, up, eligible, home);
            if (indexed != 0) partition.divide(0, indexed);
            regions = partition.count;
            for (int region = 0; region < regions; ++region) byRegion.add(new PriorityQueue<>());

            int w = x1 - x0 + 1;
            int[] last = new int[Math.multiplyExact(w, y1 - y0 + 1)];
            java.util.Arrays.fill(last, -1);
            var cursorByNet = new java.util.IdentityHashMap<NetWrapper, Integer>();
            for (int i = 0; i < n; ++i) {
                Connection c = all.get(i);
                Integer net = cursorByNet.put(c.getNetWrapper(), i);
                F1Corridor.Box b = bounds[i];
                if (b == null) continue;
                int tier = f21Service(c, gate, geometry[i]);
                service[i] = (byte) tier;
                if (tier >= 0) ++supply[tier];
                int waitFor = -1;
                // Effective search cells, not the wider retry box.
                int l = (b.left >> F4_REGION_SHIFT) - x0;
                int r = (b.right >> F4_REGION_SHIFT) - x0;
                int d = (b.bottom >> F4_REGION_SHIFT) - y0;
                int u = (b.top >> F4_REGION_SHIFT) - y0;
                for (int y = d; y <= u; ++y) {
                    for (int key = y * w + l, end = y * w + r; key <= end; ++key) {
                        waitFor = mark(last, key, i, waitFor);
                    }
                }
                for (RouteNode old : c.getRnodes()) {
                    int x = (old.getEndTileXCoordinate() >> F4_REGION_SHIFT) - x0;
                    int y = (old.getEndTileYCoordinate() >> F4_REGION_SHIFT) - y0;
                    waitFor = mark(last, y * w + x, i, waitFor);
                }
                blocker[i] = waitFor;
                if (home[i] < 0) continue;
                if (net != null) waitFor = Math.max(waitFor, net);
                next[i] = releaseHead[waitFor + 1];
                releaseHead[waitFor + 1] = i;
            }
        }

        F1Corridor.Box bound(int i, Connection c, FidesLaneGate gate) {
            F1Corridor.Box b = bounds[i];
            if (b == null) return f4AdaptiveBound(c, gate);
            return narrowed[i] ? b : null;
        }

        int tier(int i, Connection c, FidesLaneGate gate, F1Corridor.Box bound) {
            if (bounds[i] != null) return service[i];
            return f21Service(c, gate, f21Geometry(c, bound));
        }

        private int mark(int[] last, int key, int i, int previous) {
            int earlier = last[key];
            if (earlier == -1) ++occupied;
            last[key] = i;
            return earlier == i ? previous : Math.max(earlier, previous);
        }

        void publish(int cursor) {
            for (int p = releaseHead[cursor]; p >= 0; p = next[p]) {
                var pending = byRegion.get(home[p]);
                if (!pending.isEmpty()) runnable.remove(pending.peek());
                pending.add(p);
                runnable.add(pending.peek());
            }
        }

        void take(int p) {
            var pending = byRegion.get(home[p]);
            runnable.remove(p);
            pending.remove();
            if (!pending.isEmpty()) runnable.add(pending.peek());
        }

        void prune(int cursor) {
            while (!runnable.isEmpty() && runnable.first() < cursor) take(runnable.first());
        }
    }""",
        """    private final class F4RegionQueue {
        final int[] releaseHead, next, home, blocker, rank, left, right, down, up;
        final int[] regionNext, regionHead;
        final boolean[] claimed;
        final F1Corridor.Box[] bounds, footprints;
        final byte[] service, geometry;
        final boolean[] narrowed;
        final java.util.TreeSet<Integer> runnable = new java.util.TreeSet<>();
        final java.util.TreeSet<Integer> regionHeads = new java.util.TreeSet<>();
        final List<PriorityQueue<Integer>> byRegion = new ArrayList<>();
        final long[] supply = new long[3];
        int indexed, occupied, regions, ripFootprints, active, featured;

        F4RegionQueue(List<Connection> all, FidesLaneGate gate) {
            int n = all.size();
            releaseHead = new int[n];
            next = new int[n];
            home = new int[n];
            blocker = new int[n];
            rank = new int[n];
            left = new int[n];
            right = new int[n];
            down = new int[n];
            up = new int[n];
            regionNext = new int[n];
            claimed = new boolean[n];
            bounds = new F1Corridor.Box[n];
            footprints = new F1Corridor.Box[n];
            service = new byte[n];
            geometry = new byte[n];
            narrowed = new boolean[n];
            int[] histogram = new int[32];
            java.util.Arrays.fill(blocker, -1);
            java.util.Arrays.fill(releaseHead, -1);
            java.util.Arrays.fill(home, -1);
            java.util.Arrays.fill(regionNext, -1);
            java.util.Arrays.fill(service, (byte) -1);
            int[] activeIdx = new int[n];
            int activeCount = 0;
            for (int i = 0; i < n; ++i) {
                Connection c = all.get(i);
                rank[i] = activeCount;
                if (!shouldRoute(c)) continue;
                activeIdx[activeCount++] = i;
                f4RecordHotPath(c);
            }
            active = activeCount;
            if (active == 0) {
                regionHead = new int[0];
                return;
            }
            gate.ensureF4PortalsIndexed();
            var parallelEntries = new F1Corridor.ParallelEntry[n];
            int workers = Math.max(1, f4SharedPool.getParallelism());
            if (workers >= 2 && activeCount >= 4096) {
                int chunk = Math.max(2048, (activeCount + workers * 4 - 1) / (workers * 4));
                var prepJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                for (int from = 0; from < activeCount; from += chunk) {
                    final int f = from, t = Math.min(activeCount, from + chunk);
                    prepJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                        for (int k = f; k < t; ++k) {
                            int idx = activeIdx[k];
                            Connection c = all.get(idx);
                            F1Corridor.Box chosen = f4AdaptiveBoundParallel(c, gate, parallelEntries, idx);
                            narrowed[idx] = chosen != null;
                            F1Corridor.Box b = chosen == null ? F1Corridor.Box.original(c) : chosen;
                            bounds[idx] = b;
                            geometry[idx] = (byte) f21Geometry(c, b);
                        }
                    }, f4SharedPool));
                }
                java.util.concurrent.CompletableFuture.allOf(
                        prepJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
                gate.commitF4BoundaryParallel(all, parallelEntries);
            } else {
                for (int k = 0; k < activeCount; ++k) {
                    int idx = activeIdx[k];
                    Connection c = all.get(idx);
                    F1Corridor.Box chosen = f4AdaptiveBound(c, gate);
                    narrowed[idx] = chosen != null;
                    F1Corridor.Box b = chosen == null ? F1Corridor.Box.original(c) : chosen;
                    bounds[idx] = b;
                    geometry[idx] = (byte) f21Geometry(c, b);
                }
            }

            int[] eligible = new int[n];
            int x0 = Integer.MAX_VALUE, y0 = Integer.MAX_VALUE;
            int x1 = Integer.MIN_VALUE, y1 = Integer.MIN_VALUE;
            for (int k = 0; k < activeCount; ++k) {
                int i = activeIdx[k];
                Connection c = all.get(i);
                F1Corridor.Box b = bounds[i];
                int fl = b.left, fr = b.right, fd = b.bottom, fu = b.top;
                int l = b.left >> F4_REGION_SHIFT, r = b.right >> F4_REGION_SHIFT;
                int d = b.bottom >> F4_REGION_SHIFT, u = b.top >> F4_REGION_SHIFT;
                if (!c.getRnodes().isEmpty()) {
                    ++ripFootprints;
                    for (RouteNode old : c.getRnodes()) {
                        int tx = old.getEndTileXCoordinate(), ty = old.getEndTileYCoordinate();
                        fl = Math.min(fl, tx - 1);
                        fr = Math.max(fr, tx + 1);
                        fd = Math.min(fd, ty - 1);
                        fu = Math.max(fu, ty + 1);
                        int x = tx >> F4_REGION_SHIFT, y = ty >> F4_REGION_SHIFT;
                        l = Math.min(l, x);
                        r = Math.max(r, x);
                        d = Math.min(d, y);
                        u = Math.max(u, y);
                    }
                    footprints[i] = new F1Corridor.Box(fl, fd, fr, fu);
                } else {
                    footprints[i] = b;
                }
                left[i] = l;
                right[i] = r;
                down[i] = d;
                up[i] = u;
                x0 = Math.min(x0, l);
                x1 = Math.max(x1, r);
                y0 = Math.min(y0, d);
                y1 = Math.max(y1, u);
                if (f4FirstShape(c) && !f22SkipPrivatePrev.contains(c)) eligible[indexed++] = i;
                int bucket = geometry[i];
                if (bucket >= 0) {
                    ++featured;
                    ++histogram[bucket];
                }
            }
            f21HeavyGeometry = f21Quantile(histogram, featured, 90);
            f21RareGeometry = f21Quantile(histogram, featured, 99);

            F21Partition partition = new F21Partition(left, right, down, up, eligible, home);
            if (indexed != 0) partition.divide(0, indexed);
            regions = partition.count;
            regionHead = new int[regions];
            int[] regionTail = new int[regions];
            java.util.Arrays.fill(regionHead, -1);
            java.util.Arrays.fill(regionTail, -1);
            for (int region = 0; region < regions; ++region) byRegion.add(new PriorityQueue<>());

            int w = x1 - x0 + 1;
            int[] last = new int[Math.multiplyExact(w, y1 - y0 + 1)];
            java.util.Arrays.fill(last, -1);
            var cursorByNet = new java.util.IdentityHashMap<NetWrapper, Integer>();
            for (int i = 0; i < n; ++i) {
                Connection c = all.get(i);
                Integer net = cursorByNet.put(c.getNetWrapper(), i);
                F1Corridor.Box b = bounds[i];
                if (b == null) continue;
                int tier = f21Service(c, gate, geometry[i]);
                service[i] = (byte) tier;
                if (tier >= 0) ++supply[tier];
                int waitFor = -1;
                int l = (b.left >> F4_REGION_SHIFT) - x0;
                int r = (b.right >> F4_REGION_SHIFT) - x0;
                int d = (b.bottom >> F4_REGION_SHIFT) - y0;
                int u = (b.top >> F4_REGION_SHIFT) - y0;
                for (int y = d; y <= u; ++y) {
                    for (int key = y * w + l, end = y * w + r; key <= end; ++key) {
                        waitFor = mark(last, key, i, waitFor);
                    }
                }
                for (RouteNode old : c.getRnodes()) {
                    int x = (old.getEndTileXCoordinate() >> F4_REGION_SHIFT) - x0;
                    int y = (old.getEndTileYCoordinate() >> F4_REGION_SHIFT) - y0;
                    waitFor = mark(last, y * w + x, i, waitFor);
                }
                blocker[i] = waitFor;
                int reg = home[i];
                if (reg < 0) continue;
                if (regionHead[reg] < 0) {
                    regionHead[reg] = i;
                    regionHeads.add(i);
                } else {
                    regionNext[regionTail[reg]] = i;
                }
                regionTail[reg] = i;
                if (net != null) waitFor = Math.max(waitFor, net);
                next[i] = releaseHead[waitFor + 1];
                releaseHead[waitFor + 1] = i;
            }
        }

        F1Corridor.Box bound(int i, Connection c, FidesLaneGate gate) {
            F1Corridor.Box b = bounds[i];
            if (b == null) return f4AdaptiveBound(c, gate);
            return narrowed[i] ? b : null;
        }

        int tier(int i, Connection c, FidesLaneGate gate, F1Corridor.Box bound) {
            if (bounds[i] != null) return service[i];
            return f21Service(c, gate, f21Geometry(c, bound));
        }

        private int mark(int[] last, int key, int i, int previous) {
            int earlier = last[key];
            if (earlier == -1) ++occupied;
            last[key] = i;
            return earlier == i ? previous : Math.max(earlier, previous);
        }

        void publish(int cursor) {
            for (int p = releaseHead[cursor]; p >= 0; p = next[p]) {
                if (claimed[p]) continue;
                var pending = byRegion.get(home[p]);
                if (!pending.isEmpty()) runnable.remove(pending.peek());
                pending.add(p);
                runnable.add(pending.peek());
            }
        }

        private void advanceRegion(int reg, int cursor) {
            int head = regionHead[reg];
            if (head >= 0) regionHeads.remove(head);
            int q = head;
            while (q >= 0 && (q < cursor || claimed[q])) q = regionNext[q];
            regionHead[reg] = q;
            if (q >= 0) regionHeads.add(q);
        }

        void claim(int p, int cursor) {
            claimed[p] = true;
            int reg = home[p];
            if (reg >= 0) {
                var pending = byRegion.get(reg);
                if (!pending.isEmpty() && pending.peek() == p) {
                    runnable.remove(p);
                    pending.remove();
                    while (!pending.isEmpty() && (pending.peek() < cursor || claimed[pending.peek()])) pending.remove();
                    if (!pending.isEmpty()) runnable.add(pending.peek());
                }
                if (regionHead[reg] == p) advanceRegion(reg, cursor);
            }
        }

        void take(int p) {
            claim(p, p);
        }

        void prune(int cursor) {
            while (!runnable.isEmpty()) {
                int p = runnable.first();
                if (p >= cursor && !claimed[p]) break;
                var pending = byRegion.get(home[p]);
                runnable.remove(p);
                if (!pending.isEmpty() && pending.peek() == p) pending.remove();
                while (!pending.isEmpty() && (pending.peek() < cursor || claimed[pending.peek()])) pending.remove();
                if (!pending.isEmpty()) runnable.add(pending.peek());
            }
            while (!regionHeads.isEmpty()) {
                int p = regionHeads.first();
                if (p >= cursor && !claimed[p]) break;
                advanceRegion(home[p], cursor);
            }
        }
    }""",
    )

    # 7. Safe speculative region filling + 48 lanes
    src = once(
        src,
        """    private void f4RegionFill(F4RegionQueue queue, List<Connection> all, int i, FidesLaneGate gate,
                              java.util.NavigableMap<Integer, F4SpatialResult> results) {
        queue.prune(i);
        int lanes = Math.min(F4_REGION_PENDING - results.size(), Math.min(16, f4SharedPool.getParallelism()));
        if (lanes < 2 || queue.runnable.size() < 2) return;
        var chosen = new ArrayList<F4FirstCandidate>(lanes);
        // Each head is released by all earlier cell and same-net writes. Take
        // at most one head per region and never widen the horizon to fill slots.
        for (int p : queue.runnable) {
            if (queue.rank[p] - queue.rank[i] > F4_REGION_NEAR || chosen.size() == lanes) break;
            Connection c = all.get(p);
            if (!shouldRoute(c)) continue;
            chosen.add(new F4FirstCandidate(c, queue.bound(p, c, gate), p));
        }
        if (chosen.size() < 2) return;
        ++f4First.groups;
        f4First.selected += chosen.size();
        f4First.regionMaxWidth = Math.max(f4First.regionMaxWidth, chosen.size());
        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<F4FirstPrivate>>(chosen.size());
        long waitStarted = System.nanoTime();
        for (int k = 0; k < chosen.size(); ++k) {
            final int slot = k;
            var c = chosen.get(k);
            queue.take(c.position);
            int tier = queue.service[c.position];
            jobs.add(java.util.concurrent.CompletableFuture.supplyAsync(
                    () -> f4FirstProbe(c, slot, tier), f4SharedPool));
        }
        // Scratch is private, but probing reads shared graph and occupancy.
        // Drain the independent reads before the coordinator publishes writes.
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        f4First.regionWaitNs += System.nanoTime() - waitStarted;
        for (int k = 0; k < jobs.size(); ++k) {
            var value = jobs.get(k).join();
            f4First.coldBuilt += value.coldBuilt;
            f4First.coldNs += value.coldNs;
            if (value.result == null) {
                f4First.droppedPops += value.pops;
                if (value.capped) { ++f4First.privateCap; f22SkipPrivateNext.add(chosen.get(k).connection); } else ++f4First.privateMiss;
            } else {
                results.put(chosen.get(k).position, value.result);
            }
        }
        f4First.regionMaxStaged = Math.max(f4First.regionMaxStaged, results.size());
    }""",
        """    private void f4RegionFill(F4RegionQueue queue, List<Connection> all, int i, FidesLaneGate gate,
                              java.util.NavigableMap<Integer, F4SpatialResult> results) {
        queue.prune(i);
        int lanes = Math.min(F4_REGION_PENDING - results.size(), Math.min(48, f4SharedPool.getParallelism()));
        if (lanes < 2 || (queue.runnable.size() < 2 && queue.regionHeads.size() < 2)) return;
        var chosen = new ArrayList<F4FirstCandidate>(lanes);
        var chosenPos = new java.util.HashSet<Integer>(lanes * 2);
        for (int p : queue.runnable) {
            if (queue.rank[p] - queue.rank[i] > F4_REGION_NEAR || chosen.size() == lanes) break;
            if (queue.claimed[p] || results.containsKey(p)) continue;
            Connection c = all.get(p);
            if (!shouldRoute(c)) continue;
            var cand = new F4FirstCandidate(c, queue.bound(p, c, gate), p);
            F1Corridor.Box fp = queue.footprints[p] != null ? queue.footprints[p] : cand.footprint;
            boolean conflict = false;
            for (var prev : chosen) {
                F1Corridor.Box prevFp = queue.footprints[prev.position] != null ? queue.footprints[prev.position] : prev.footprint;
                if (prev.connection.getNetWrapper() == c.getNetWrapper() || f4FirstOverlap(fp, prevFp)) {
                    conflict = true;
                    break;
                }
            }
            if (!conflict && !results.isEmpty()) {
                for (var r : results.values()) {
                    if (r.state.connection.getNetWrapper() == c.getNetWrapper()
                            || (r.footprint != null && f4FirstOverlap(fp, r.footprint))) {
                        conflict = true;
                        break;
                    }
                }
            }
            if (conflict) continue;
            chosen.add(cand);
            chosenPos.add(p);
        }
        int specStart = chosen.size();
        if (chosen.size() < lanes) {
            int nearEnd = Math.min(all.size(), i + 16);
            for (int p : queue.regionHeads) {
                if (queue.rank[p] - queue.rank[i] > F4_REGION_NEAR || chosen.size() == lanes) break;
                if (queue.claimed[p] || chosenPos.contains(p) || results.containsKey(p)) continue;
                Connection c = all.get(p);
                if (!shouldRoute(c)) continue;
                F1Corridor.Box fp = queue.footprints[p] != null ? queue.footprints[p] : F1Corridor.Box.original(c);
                boolean conflict = false;
                // Avoid guaranteed invalidation by checking upcoming serial cursor items [i .. min(p, i+16)).
                int checkEnd = Math.min(p, nearEnd);
                for (int m = i; m < checkEnd; ++m) {
                    if (queue.claimed[m] || results.containsKey(m) || queue.footprints[m] == null) continue;
                    Connection mc = all.get(m);
                    if (mc.getNetWrapper() == c.getNetWrapper() || f4FirstOverlap(fp, queue.footprints[m])) {
                        conflict = true;
                        break;
                    }
                }
                if (conflict) continue;
                for (var prev : chosen) {
                    F1Corridor.Box prevFp = queue.footprints[prev.position] != null ? queue.footprints[prev.position] : prev.footprint;
                    if (prev.connection.getNetWrapper() == c.getNetWrapper() || f4FirstOverlap(fp, prevFp)) {
                        conflict = true;
                        break;
                    }
                }
                if (!conflict && !results.isEmpty()) {
                    for (var r : results.values()) {
                        if (r.state.connection.getNetWrapper() == c.getNetWrapper()
                                || (r.footprint != null && f4FirstOverlap(fp, r.footprint))) {
                            conflict = true;
                            break;
                        }
                    }
                }
                if (conflict) continue;
                chosen.add(new F4FirstCandidate(c, queue.bound(p, c, gate), p));
                chosenPos.add(p);
            }
        }
        if (chosen.size() < 2) return;
        ++f4First.groups;
        f4First.selected += chosen.size();
        f4First.regionSpecSelected += (chosen.size() - specStart);
        f4First.regionMaxWidth = Math.max(f4First.regionMaxWidth, chosen.size());
        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<F4FirstPrivate>>(chosen.size());
        long waitStarted = System.nanoTime();
        for (int k = 0; k < chosen.size(); ++k) {
            final int slot = k;
            var c = chosen.get(k);
            queue.claim(c.position, i);
            int tier = queue.service[c.position];
            f4First.farthest = Math.max(f4First.farthest, c.position - i);
            jobs.add(java.util.concurrent.CompletableFuture.supplyAsync(
                    () -> f4FirstProbe(c, slot, tier), f4SharedPool));
        }
        // Scratch is private, but probing reads shared graph and occupancy.
        // Drain the independent reads before the coordinator publishes writes.
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        f4First.regionWaitNs += System.nanoTime() - waitStarted;
        for (int k = 0; k < jobs.size(); ++k) {
            var value = jobs.get(k).join();
            f4First.coldBuilt += value.coldBuilt;
            f4First.coldNs += value.coldNs;
            if (value.result == null) {
                f4First.droppedPops += value.pops;
                if (value.capped) { ++f4First.privateCap; f22SkipPrivateNext.add(chosen.get(k).connection); } else ++f4First.privateMiss;
            } else {
                int pos = chosen.get(k).position;
                value.result.footprint = queue.footprints[pos];
                results.put(pos, value.result);
            }
        }
        f4First.regionMaxStaged = Math.max(f4First.regionMaxStaged, results.size());
    }""",
    )

    # 8. Multi-slot same-net GPU batching in f4RouteFirstRound + amortized per-member GPU pricing
    src = once(
        src,
        """    private void f4RouteFirstRound(List<Connection> all, FidesLaneGate gate) {
        f4First = new F4FirstProgress();
        f4FirstTotal = all.size();
        java.util.NavigableMap<Integer, F4SpatialResult> ready = new java.util.TreeMap<>();
        f4RegionRetired = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
        f22SkipPrivatePrev = f22SkipPrivateNext;
        f22SkipPrivateNext = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
        try {
            f4First.report(routeIteration, 0, all.size(), 0, true);
            long prep = System.nanoTime();
            f4ReadCongestion();
            f4StartCriticalRound();
            f4First.gpuTrials = f4GpuTrials;
            f4First.gpuGainBatches = f4GpuGained;
            int refillAfter = 0;
            F4RegionQueue queue = new F4RegionQueue(all, gate);
            f21Stage(queue);
            var serialState = new ConnectionState();
            serialState.f21BoundFirst = true;
            f4First.regionPlanMs = (System.nanoTime() - prep) / 1_000_000L;
            f4First.regionIndexed = queue.indexed;
            f4First.regionCells = queue.occupied;
            f4First.regionLeaves = queue.regions;
            f4First.ripFootprints = queue.ripFootprints;
            f4First.report(routeIteration, 0, all.size(), 0, true);
            for (int i = 0; i < all.size(); ++i) {
                f4FirstPosition = i;
                queue.publish(i);
                if ((i & 63) == 0) f4First.report(routeIteration, i, all.size(), 0, false);
                Connection c = all.get(i);
                if (!shouldRoute(c)) {
                    ++f4First.routeNotNeeded;
                    var dropped = ready.remove(i);
                    if (dropped != null) f4FirstDiscard(dropped);
                    continue;
                }
                if (f4SharedPool.getParallelism() >= 2 && i >= refillAfter && ready.size() < 12) {
                    long beforeGroup = f4First.groups;
                    f4RegionFill(queue, all, i, gate, ready);
                    if (beforeGroup == f4First.groups) refillAfter = i + 4;
                }
                var r = ready.remove(i);
                int sequence = connectionsRouted.incrementAndGet();
                if (sequence >= 1 << 29) throw new IllegalStateException("F4 first-round stamp range exhausted");
                connectionsRoutedThisIteration.incrementAndGet();
                ++f4First.attempts;
                gate.mirrorBeforeAttempt(c);
                f4FirstInvalidate(ready, c.getRnodes());
                if (r != null) {
                    long commitStarted = System.nanoTime();
                    r.state.sequence = sequence;
                    f4FirstBoxTally(c, r.state.f4Boundary);
                    ripUp(c);
                    r.scratch.f4DropPrivateRip();
                    r.scratch.attachPairForCommit();
                    try {
                        f4ObserveCpu(r.state, r.tier, r.search, false);
                        f4FinishCpu(r.state, r.search.target, gate);
                        // Occupancy and the connection route are authoritative.
                        // Keep temporary visited, target and parent labels private.
                    } finally {
                        r.scratch.close();
                        f4SpatialScratch.add(r.scratch);
                    }
                    if (r.state.f4Boundary != null) f4First.narrowedPops += r.state.nodesPopped;
                    else f4First.fullPops += r.state.nodesPopped;
                    f4First.outside += r.state.f4OutsideChecks;
                    ++f4First.parallelCommits;
                    ++f4SpatialCommits;
                    f4First.regionCommitNs += System.nanoTime() - commitStarted;
                } else {
                    ++f4First.serialAttempts;
                    if (!f4FirstShape(c)) ++f4First.shape;
                    var state = serialState;
                    assert(state.queue.isEmpty() && state.targets.isEmpty());
                    f4FillHostState(state, c, sequence);
                    state.f4HandoffBudget = state.f4CpuPopsBeforeHandoff = 0;
                    state.f4Boundary = queue.bound(i, c, gate);
                    var hot = f4HotPaths.get(c);
                    if (hot != null && hot.round == routeIteration && hot.streak >= 2) {
                        var previous = gate.f4Boundary(c);
                        if (previous != null && (state.f4Boundary == null || !previous.same(state.f4Boundary)))
                            ++f4First.hotWidened;
                    }
                    f4FirstBoxTally(c, state.f4Boundary);
                    prepareRouteConnection(state);
                    int tier = queue.tier(i, c, gate, state.f4Boundary);
                    if (tier >= 0 && !gate.f4Supported(c, state)) tier = -1;
                    boolean narrowed = state.f4Boundary != null;
                    boolean chain = f4FirstShape(c);
                    if (chain) ++f4First.chainSerial;
                    var cost = f4CriticalCost(c, state.f4Boundary, tier, chain);
                    int gpuMode = f21CriticalMode(cost, tier);
                    var search = f4SearchWithBoundary(state, tier, gpuMode != 0);
                    if (tier >= 0 && search.crossingNs >= 0) ++f21Proofs[tier];
                    if (narrowed && state.f4Boundary == null) {
                        f4RegionRetired.add(c);
                        ++f4First.retries;
                        f4First.narrowedPops += state.f4RetriedPops;
                    }
                    if (state.f4Boundary == null) f4First.fullPops += state.nodesPopped;
                    else f4First.narrowedPops += state.nodesPopped;
                    if (state.nodesPopped > 65536 || (narrowed && state.f4Boundary == null)) f22SkipPrivateNext.add(c);
                    f4First.outside += state.f4OutsideChecks;
                    boolean handoff = gpuMode != 0 && search.target == null && search.crossingNs >= 0
                            && F4RoundDiversion.crossed(state.nodesPopped, F4RoundDiversion.budget(tier),
                                                       !state.queue.isEmpty());
                    if (handoff) {
                        long start = System.nanoTime();
                        int proofPops = state.nodesPopped;
                        f4CriticalProofPops += proofPops;
                        f4ObserveCpu(state, tier, search, true);
                        var batch = new ArrayList<LaneBatchSlot>(1);
                        f4CollectProven(state, F4RoundDiversion.budget(tier), state.nodesPopped, batch);
                        runAndCommitF4Batch(batch);
                        f4RecordCriticalGpu(cost, gpuMode, tier, sequence, proofPops, System.nanoTime() - start, gate.f4LastSealNs());
                        ++f4First.gpuBatches;
                    } else {
                        if (!narrowed || state.f4Boundary != null) f4RecordCriticalTail(cost, search);
                        f4ObserveCpu(state, tier, search, false);
                        f4FinishCpu(state, search.target, gate);
                    }
                }
                f4FirstInvalidate(ready, c.getRnodes());
            }
            f21FinishStage();
            f4First.report(routeIteration, all.size(), all.size(), 0, true);
            f4SpatialGroups += f4First.groups;
        } finally {
            for (var r : ready.values()) f4FirstDiscard(r);
            f4RegionRetired = null;
            f4First = null;
        }
    }""",
        """    private void f4FlushFirstGpuBatch(List<LaneBatchSlot> f4Batch, List<ConnectionState> f4BatchStates,
                                      F4CriticalCost[] bCosts, int[] bModes, int[] bTiers, int[] bSeqs, int[] bPops,
                                      java.util.NavigableMap<Integer, F4SpatialResult> ready, FidesLaneGate gate) {
        if (f4Batch.isEmpty()) return;
        int n = f4Batch.size();
        for (int k = 0; k < n; ++k) f4FirstInvalidate(ready, f4Batch.get(k).connection.getRnodes());
        long started = System.nanoTime();
        runAndCommitF4Batch(f4Batch);
        long totalElapsed = System.nanoTime() - started;
        long sealNs = gate.f4LastSealNs();
        long perMember = Math.max(1L, totalElapsed - sealNs) / n;
        for (int k = 0; k < n; ++k) {
            f4RecordCriticalGpu(bCosts[k], bModes[k], bTiers[k], bSeqs[k], bPops[k],
                    k == 0 ? sealNs + perMember : perMember, k == 0 ? sealNs : 0L);
            f4FirstInvalidate(ready, f4BatchStates.get(k).connection.getRnodes());
        }
        ++f4First.gpuBatches;
    }

    private void f4RouteFirstRound(List<Connection> all, FidesLaneGate gate) {
        f4First = new F4FirstProgress();
        f4FirstTotal = all.size();
        java.util.NavigableMap<Integer, F4SpatialResult> ready = new java.util.TreeMap<>();
        f4RegionRetired = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
        f22SkipPrivatePrev = f22SkipPrivateNext;
        f22SkipPrivateNext = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
        final List<LaneBatchSlot> f4Batch = new ArrayList<>(f4Width);
        final List<ConnectionState> f4BatchStates = new ArrayList<>(f4Width);
        final F4CriticalCost[] bCosts = new F4CriticalCost[f4Width];
        final int[] bModes = new int[f4Width];
        final int[] bTiers = new int[f4Width];
        final int[] bSeqs = new int[f4Width];
        final int[] bPops = new int[f4Width];
        try {
            f4First.report(routeIteration, 0, all.size(), 0, true);
            long prep = System.nanoTime();
            f4ReadCongestion();
            f4StartCriticalRound();
            f4First.gpuTrials = f4GpuTrials;
            f4First.gpuGainBatches = f4GpuGained;
            int refillAfter = 0;
            F4RegionQueue queue = new F4RegionQueue(all, gate);
            f21Stage(queue);
            var serialState = new ConnectionState();
            serialState.f21BoundFirst = true;
            f4First.regionPlanMs = (System.nanoTime() - prep) / 1_000_000L;
            f4First.regionIndexed = queue.indexed;
            f4First.regionCells = queue.occupied;
            f4First.regionLeaves = queue.regions;
            f4First.ripFootprints = queue.ripFootprints;
            f4First.report(routeIteration, 0, all.size(), 0, true);
            for (int i = 0; i < all.size(); ++i) {
                f4FirstPosition = i;
                queue.publish(i);
                if ((i & 63) == 0) f4First.report(routeIteration, i, all.size(), 0, false);
                Connection c = all.get(i);
                if (!f4Batch.isEmpty() && (f4Batch.size() == f4Width
                        || f4Batch.get(0).connection.getNetWrapper() != c.getNetWrapper())) {
                    f4FlushFirstGpuBatch(f4Batch, f4BatchStates, bCosts, bModes, bTiers, bSeqs, bPops, ready, gate);
                }
                if (!shouldRoute(c)) {
                    ++f4First.routeNotNeeded;
                    var dropped = ready.remove(i);
                    if (dropped != null) f4FirstDiscard(dropped);
                    continue;
                }
                if (f4Batch.isEmpty() && f4SharedPool.getParallelism() >= 2 && i >= refillAfter && ready.size() < 48) {
                    long beforeGroup = f4First.groups;
                    f4RegionFill(queue, all, i, gate, ready);
                    if (beforeGroup == f4First.groups) refillAfter = i + 2;
                }
                var r = ready.remove(i);
                if (r != null && !f4Batch.isEmpty()) {
                    f4FlushFirstGpuBatch(f4Batch, f4BatchStates, bCosts, bModes, bTiers, bSeqs, bPops, ready, gate);
                }
                if (r != null) {
                    int sequence = connectionsRouted.incrementAndGet();
                    if (sequence >= 1 << 29) throw new IllegalStateException("F4 first-round stamp range exhausted");
                    connectionsRoutedThisIteration.incrementAndGet();
                    ++f4First.attempts;
                    gate.mirrorBeforeAttempt(c);
                    f4FirstInvalidate(ready, c.getRnodes());
                    long commitStarted = System.nanoTime();
                    r.state.sequence = sequence;
                    f4FirstBoxTally(c, r.state.f4Boundary);
                    ripUp(c);
                    r.scratch.f4DropPrivateRip();
                    r.scratch.attachPairForCommit();
                    try {
                        f4ObserveCpu(r.state, r.tier, r.search, false);
                        f4FinishCpu(r.state, r.search.target, gate);
                    } finally {
                        r.scratch.close();
                        f4SpatialScratch.add(r.scratch);
                    }
                    if (r.state.f4Boundary != null) f4First.narrowedPops += r.state.nodesPopped;
                    else f4First.fullPops += r.state.nodesPopped;
                    f4First.outside += r.state.f4OutsideChecks;
                    ++f4First.parallelCommits;
                    ++f4SpatialCommits;
                    f4First.regionCommitNs += System.nanoTime() - commitStarted;
                    f4FirstInvalidate(ready, c.getRnodes());
                    continue;
                }

                // Try adding consecutive same-net heavy connections into the pending multi-lane GPU batch.
                if (!f4Batch.isEmpty()) {
                    F1Corridor.Box bBound = queue.bound(i, c, gate);
                    int bTier = queue.tier(i, c, gate, bBound);
                    boolean bChain = f4FirstShape(c);
                    var bCost = f4CriticalCost(c, bBound, bTier, bChain);
                    int bMode = bTier >= 0 ? f21CriticalMode(bCost, bTier) : 0;
                    int privatePops = bMode != 0 ? f4PrivatePendingProof(c, bTier, f4Batch, bBound) : 0;
                    if (privatePops > 0) {
                        int sequence = connectionsRouted.incrementAndGet();
                        if (sequence >= 1 << 29) throw new IllegalStateException("F4 first-round stamp range exhausted");
                        connectionsRoutedThisIteration.incrementAndGet();
                        ++f4First.attempts;
                        ++f4First.serialAttempts;
                        if (!bChain) ++f4First.shape;
                        int slotIdx = f4Batch.size();
                        ConnectionState slotSt = slotState(f4BatchStates, slotIdx);
                        f4FillHostState(slotSt, c, sequence);
                        slotSt.f4Boundary = bBound;
                        f4FirstBoxTally(c, bBound);
                        gate.mirrorBeforeAttempt(c);
                        prepareRouteConnection(slotSt);
                        if (!gate.f4Supported(c, slotSt)) {
                            throw new IllegalStateException("F4 prepared target changed after private proof");
                        }
                        ++f21Proofs[bTier];
                        f4CriticalProofPops += privatePops;
                        bCosts[slotIdx] = bCost;
                        bModes[slotIdx] = bMode;
                        bTiers[slotIdx] = bTier;
                        bSeqs[slotIdx] = sequence;
                        bPops[slotIdx] = privatePops;
                        f4CollectProven(slotSt, F4RoundDiversion.budget(bTier), privatePops, f4Batch);
                        continue;
                    }
                    f4FlushFirstGpuBatch(f4Batch, f4BatchStates, bCosts, bModes, bTiers, bSeqs, bPops, ready, gate);
                    if (!shouldRoute(c)) continue;
                }

                int sequence = connectionsRouted.incrementAndGet();
                if (sequence >= 1 << 29) throw new IllegalStateException("F4 first-round stamp range exhausted");
                connectionsRoutedThisIteration.incrementAndGet();
                ++f4First.attempts;
                ++f4First.serialAttempts;
                gate.mirrorBeforeAttempt(c);
                f4FirstInvalidate(ready, c.getRnodes());
                if (!f4FirstShape(c)) ++f4First.shape;
                var state = serialState;
                f4FillHostState(state, c, sequence);
                state.f4HandoffBudget = state.f4CpuPopsBeforeHandoff = 0;
                state.f4Boundary = queue.bound(i, c, gate);
                var hot = f4HotPaths.get(c);
                if (hot != null && hot.round == routeIteration && hot.streak >= 2) {
                    var previous = gate.f4Boundary(c);
                    if (previous != null && (state.f4Boundary == null || !previous.same(state.f4Boundary)))
                        ++f4First.hotWidened;
                }
                f4FirstBoxTally(c, state.f4Boundary);
                prepareRouteConnection(state);
                int tier = queue.tier(i, c, gate, state.f4Boundary);
                if (tier >= 0 && !gate.f4Supported(c, state)) tier = -1;
                boolean narrowed = state.f4Boundary != null;
                boolean chain = f4FirstShape(c);
                if (chain) ++f4First.chainSerial;
                var cost = f4CriticalCost(c, state.f4Boundary, tier, chain);
                int gpuMode = f21CriticalMode(cost, tier);
                var search = f4SearchWithBoundary(state, tier, gpuMode != 0);
                if (tier >= 0 && search.crossingNs >= 0) ++f21Proofs[tier];
                if (narrowed && state.f4Boundary == null) {
                    f4RegionRetired.add(c);
                    ++f4First.retries;
                    f4First.narrowedPops += state.f4RetriedPops;
                }
                if (state.f4Boundary == null) f4First.fullPops += state.nodesPopped;
                else f4First.narrowedPops += state.nodesPopped;
                if (state.nodesPopped > 65536 || (narrowed && state.f4Boundary == null)) f22SkipPrivateNext.add(c);
                f4First.outside += state.f4OutsideChecks;
                boolean handoff = gpuMode != 0 && search.target == null && search.crossingNs >= 0
                        && F4RoundDiversion.crossed(state.nodesPopped, F4RoundDiversion.budget(tier),
                                                   !state.queue.isEmpty());
                if (handoff) {
                    int proofPops = state.nodesPopped;
                    f4CriticalProofPops += proofPops;
                    f4ObserveCpu(state, tier, search, true);
                    ConnectionState slotSt = slotState(f4BatchStates, 0);
                    f4FillHostState(slotSt, c, sequence);
                    slotSt.f4Boundary = state.f4Boundary;
                    slotSt.targets.addAll(state.targets);
                    state.targets.clear();
                    bCosts[0] = cost;
                    bModes[0] = gpuMode;
                    bTiers[0] = tier;
                    bSeqs[0] = sequence;
                    bPops[0] = proofPops;
                    f4CollectProven(slotSt, F4RoundDiversion.budget(tier), proofPops, f4Batch);
                    if (f4Batch.size() == f4Width) {
                        f4FlushFirstGpuBatch(f4Batch, f4BatchStates, bCosts, bModes, bTiers, bSeqs, bPops, ready, gate);
                    }
                } else {
                    if (!narrowed || state.f4Boundary != null) f4RecordCriticalTail(cost, search);
                    f4ObserveCpu(state, tier, search, false);
                    f4FinishCpu(state, search.target, gate);
                    f4FirstInvalidate(ready, c.getRnodes());
                }
            }
            if (!f4Batch.isEmpty()) {
                f4FlushFirstGpuBatch(f4Batch, f4BatchStates, bCosts, bModes, bTiers, bSeqs, bPops, ready, gate);
            }
            f21FinishStage();
            f4First.report(routeIteration, all.size(), all.size(), 0, true);
            f4SpatialGroups += f4First.groups;
        } finally {
            for (var r : ready.values()) f4FirstDiscard(r);
            f4RegionRetired = null;
            f4First = null;
        }
    }""",
    )
    return src
