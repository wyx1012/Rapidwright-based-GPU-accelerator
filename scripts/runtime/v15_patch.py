"""Adapt the compiled v14-r2 source; preserve original bounds and result semantics."""
from v11_window import replace_once as once


def router(src, work):
    src = once(src, "        boolean f4PrivateSearch;", """        boolean f4PrivateSearch;
        F1Corridor.Box f4Boundary;
        int f4RetryStamp;
        int f4PrivatePopCeiling;""")
    src = once(src, "        state.connection = connection;\n        state.sequence = sequence;", """        state.connection = connection;
        state.sequence = sequence;
        state.f4Boundary = null;
        state.f4RetryStamp = 0;
        state.f4PrivatePopCeiling = 0;""")
    src = once(src, "        final int sequence = state.sequence;\n        final PriorityQueue<RouteNode> queue",
        "        final int sequence = state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence;\n"
        "        final PriorityQueue<RouteNode> queue")
    src = once(src, "        childRnode.setVisited(state.sequence);",
        "        childRnode.setVisited(state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence);")
    src = once(src, "                if (!isAccessible(childRNode, connection)) {",
        """                if (!isAccessible(childRNode, connection)
                        || (state.f4Boundary != null && !state.f4Boundary.contains(childRNode))) {""")
    src = once(src, "    private void f4RouteDiverted(Collection<Connection> connections) {",
               (work / "F4SharedSpatial.inc").read_text() +
               "    private void f4RouteDiverted(Collection<Connection> connections) {")
    src = once(src, "        for (Connection connection : connections) {\n            if (!batch.isEmpty() && (batch.size() == f4Width",
        """        List<Connection> ordered = connections instanceof List
                ? (List<Connection>) connections : new ArrayList<>(connections);
        f4NextSpatial = 0;
        for (int position = 0; position < ordered.size(); ++position) {
            Connection connection = ordered.get(position);
            if (!batch.isEmpty() && (batch.size() == f4Width""")
    src = once(src, "            // A terminal phase cannot re-admit the device. Skip service accounting there.",
        """            if (batch.isEmpty()) {
                int reused = f4SpatialRoute(ordered, position, armed, gate);
                if (reused != 0) {
                    position += reused - 1;
                    continue;
                }
            }
            F1Corridor.Box boundary = gate.f4Boundary(connection);
            // A terminal phase cannot re-admit the device. Skip service accounting there.""")
    src = once(src, "                if (tier >= 0) privatePops = f4PrivatePendingProof(connection, tier, batch);",
        """                if (tier >= 0 && f4Diversion.worth(tier, batch.size() + 1))
                    privatePops = f4PrivatePendingProof(connection, tier, batch, boundary);""")
    src = once(src, "private int f4PrivatePendingProof(Connection connection, int tier, List<LaneBatchSlot> batch)",
        "private int f4PrivatePendingProof(Connection connection, int tier, List<LaneBatchSlot> batch, F1Corridor.Box boundary)")
    src = once(src, "        f4FillHostState(proof, connection, connectionsRouted.get() + 1);",
        """        f4FillHostState(proof, connection, connectionsRouted.get() + 1);
        proof.f4Boundary = boundary;""")
    src = once(src, "            f4FillHostState(state, connection, connectionsRouted.incrementAndGet());",
        """            f4FillHostState(state, connection, connectionsRouted.incrementAndGet());
            state.f4Boundary = boundary;
            if (boundary != null) ++f4Narrow;""")
    src = once(src, "            F4CpuSearch result = f4CpuSearch(state, tier, armed && tier >= 0);\n"
                    "            boolean handoff = armed && tier >= 0",
        """            boolean offer = armed && tier >= 0 && f4Diversion.worth(tier, 1);
            F4CpuSearch result = f4SearchWithBoundary(state, tier, offer);
            boolean handoff = offer""")
    src = once(src, "        state.f4HandoffBudget = budget;\n        state.nodesPopped",
        "        state.f4HandoffBudget = budget;\n        state.f4RetryStamp = 0;\n        state.nodesPopped")
    src = once(src, "            exploreAndExpand(state, target);\n            // Lookahead remains indivisible.",
        """            exploreAndExpand(state, target);
            if (state.f4PrivatePopCeiling != 0 && state.nodesPopped >= state.f4PrivatePopCeiling)
                return new F4CpuSearch(null, System.nanoTime() - started, -1, 0);
            // Lookahead remains indivisible.""")
    src = once(src, "        boolean detached = false;\n        try {\n            scratch.beginPrivateRipUp(c.getNetWrapper());",
        """        state.f4PrivatePopCeiling = 32768;
        boolean detached = false;
        try {
            scratch.beginPrivateRipUp(c.getNetWrapper());""")
    src = once(src, '        System.out.println("F4_PPT11_ROUND " + decision);',
        """        decision.put("corridor_searches", f4Narrow).put("original_box_retries", f4Widen)
                .put("spatial_groups", f4SpatialGroups).put("spatial_commits", f4SpatialCommits)
                .put("spatial_partial_group_ns", f4SpatialWasteNs);
        f4Narrow = f4Widen = f4SpatialGroups = f4SpatialCommits = f4SpatialWasteNs = 0;
        System.out.println("F4_PPT11_ROUND " + decision);""")
    return once(src, "        f4Diversion.gpu(batch.size(), System.nanoTime() - started);",
        "        f4Diversion.gpu(batch.size(), gate.f4DeviceWidth(), System.nanoTime() - started);")


def gate(src):
    return once(src, "    boolean f4CanOffer(Connection c) {",
        """    F1Corridor.Box f4Boundary(Connection c) {
        return config.isUseBoundingBox() && !config.isTimingDriven()
                ? router.f4CorridorBox(c, rrg, graph) : null;
    }

    int f4DeviceWidth() { return f4Live.f4DeviceWidth(); }

    boolean f4CanOffer(Connection c) {""")


def capture(src):
    src = once(src, """                    long started = System.nanoTime();
                    F1Corridor.Box box = router.f4CorridorBox(states.get(index).connection, rrg, graph);
                    corridorPlanNs += System.nanoTime() - started;""",
        """                    F1Corridor.Box box = states.get(index).f4Boundary;""")
    src = once(src, "    private JSONArray commits;",
        "    private JSONArray commits;\n    private int deviceWidth;\n    int f4DeviceWidth() { return deviceWidth; }")
    return once(src, '        return frame.getJSONObject("payload").getJSONArray("members");',
        '''        JSONObject payload = frame.getJSONObject("payload");
        deviceWidth = payload.getInt("device_width");
        return payload.getJSONArray("members");''')


def spatial(src):
    src = once(src, "    private final List<Connection> connections;\n    private final int begin;",
        "    private final F1Corridor.Box[] boxes;")
    src = once(src, """    F4SpatialSchedule(List<Connection> connections, int begin, int count) {
        this.connections = connections;
        this.begin = begin;
        leaf = new int[count];""",
        """    F4SpatialSchedule(List<Connection> connections, List<F1Corridor.Box> proposed) {
        int count = connections.size();
        boxes = new F1Corridor.Box[count];
        for (int i = 0; i < count; ++i)
            boxes[i] = proposed.get(i) == null ? F1Corridor.Box.original(connections.get(i)) : proposed.get(i);
        leaf = new int[count];""")
    begin = src.index("    private Connection box(")
    end = src.index("    private void partition(", begin)
    src = src[:begin] + """    private F1Corridor.Box box(int index) { return boxes[index]; }

    private static boolean overlaps(F1Corridor.Box a, F1Corridor.Box b) {
        return a.left <= b.right && b.left <= a.right && a.bottom <= b.top && b.bottom <= a.top;
    }

""" + src[end:]
    return (src.replace("Connection connection = box(item);", "F1Corridor.Box connection = box(item);")
        .replace("int min(Connection connection, boolean x)", "int min(F1Corridor.Box connection, boolean x)")
        .replace("int max(Connection connection, boolean x)", "int max(F1Corridor.Box connection, boolean x)")
        .replace("connection.getXMinBB()", "connection.left").replace("connection.getXMaxBB()", "connection.right")
        .replace("connection.getYMinBB()", "connection.bottom").replace("connection.getYMaxBB()", "connection.top"))


def policy(src):
    src = once(src, "    private final long[] groups;",
        """    private final long[] groups;
    private final long[] previousBatchNs, previousBatches, deviceWidths;
    private final int[] lastTailRound = {-1, -1, -1};
    private final boolean[] keptTail = new boolean[BUDGETS.length];
    private int lastRound;""")
    src = once(src, "        groups = new long[width + 1];",
        """        groups = new long[width + 1];
        previousBatchNs = new long[width + 1];
        previousBatches = new long[width + 1];
        deviceWidths = new long[width + 1];""")
    src = once(src, "            estimatedTailsNs[tier] += tailMeanNs[tier];",
        "            estimatedTailsNs[tier] += freshTail(tier);")
    src = once(src, "            ++exactTails[tier];", "            ++exactTails[tier];\n            keptTail[tier] = true;")
    src = once(src, """    void gpu(int members, long wallNs) {
        if (resident) {
            liveBatchNs[members] += wallNs;
            ++liveBatches[members];
        }""",
        """    void gpu(int members, int deviceWidth, long wallNs) {
        if (deviceWidth < 1 || deviceWidth > members)
            throw new IllegalArgumentException("Invalid completed device batch width");
        ++deviceWidths[deviceWidth];
        // First call includes the cold resident startup. Do not price warm batches with it.
        if (resident) {
            liveBatchNs[members] += wallNs;
            ++liveBatches[members];
        }""")
    src = once(src, "        long archive = archivedBatchNs[members];",
        """        if (previousBatches[members] > 0) return previousBatchNs[members] / previousBatches[members];
        long archive = archivedBatchNs[members];""")
    src = once(src, "    JSONObject endRound(",
        """    private long freshTail(int tier) {
        if (exactTails[tier] > 0) return exactTailsNs[tier] / exactTails[tier];
        int age = lastRound - lastTailRound[tier];
        return age >= 2 ? 0 : tailMeanNs[tier] >> age;
    }

    boolean worth(int tier, int members) {
        // Keep one naturally encountered CPU tail per represented class each round
        // so that a run of admitted rounds never relies on a frozen early-round average.
        if (!keptTail[tier]) return false;
        long whole = batchQuote(members);
        long marginal = members == 1 ? whole : Math.max(whole / members, whole - batchQuote(members - 1));
        return freshTail(tier) > marginal;
    }

    JSONObject endRound(""")
    src = once(src, """        for (int w = 1; w <= width; ++w) dispatchNs += groups[w] * batchQuote(w);
        long setupNs""",
        """        long realizedNs = 0, realizedMembers = 0;
        for (int w = 1; w <= width; ++w) {
            dispatchNs += groups[w] * batchQuote(w);
            realizedNs += liveBatchNs[w];
            realizedMembers += liveBatches[w] * w;
        }
        long supply = Arrays.stream(overBudget).sum();
        if (realizedMembers > 0) {
            // Use the price per member at widths the device actually completed this round,
            // even when adjacent proofs suggested that wider batches were possible.
            dispatchNs = Math.max(dispatchNs, (long) ((double) realizedNs / realizedMembers * supply));
        }
        long setupNs""")
    src = once(src, '                .put("resident_started", resident);',
        """                .put("resident_started", resident).put("realized_members", realizedMembers)
                .put("realized_batch_ns", realizedNs).put("realized_batches_by_width", liveBatches.clone())
                .put("completed_device_widths", deviceWidths.clone())
                .put("cpu_tail_samples_by_service", exactTails.clone());""")
    src = once(src, "                tailMeanNs[tier] = (long) (remainingPops * nanosPerPop);",
        "                tailMeanNs[tier] = (long) (remainingPops * nanosPerPop);\n                lastTailRound[tier] = round;")
    return once(src, "        lastExcess = excess;",
        """        if (realizedMembers > 0) {
            System.arraycopy(liveBatchNs, 0, previousBatchNs, 0, liveBatchNs.length);
            System.arraycopy(liveBatches, 0, previousBatches, 0, liveBatches.length);
        }
        Arrays.fill(liveBatchNs, 0);
        Arrays.fill(liveBatches, 0);
        Arrays.fill(deviceWidths, 0);
        Arrays.fill(keptTail, false);
        lastRound = round;
        lastExcess = excess;""").replace("estimatedTailsNs, groups, liveBatchNs, liveBatches})",
                                               "estimatedTailsNs, groups})")


def broker(src):
    src = once(src, '"FIDES_GPU_ACTIVE_FRONTIER_V9": "1"}, pipe=True)',
        '"FIDES_GPU_ACTIVE_FRONTIER_V9": "1", "FIDES_GPU_SAME_SLR_FRONTIER_V11": "1"}, pipe=True)')
    return once(src, '    args.insert(4, f"-Dfides.ppt.f4.inputBytes={dcp.stat().st_size}")',
        '''    args.insert(4, f"-Dfides.ppt.f4.inputBytes={dcp.stat().st_size}")
    args.insert(4, f"-XX:ActiveProcessorCount={max(2, min(16, len(os.sched_getaffinity(0))))}")''')

