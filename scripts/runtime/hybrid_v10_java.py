"""Install bounded native A* and ordered GPU handoffs onto the v9d Java sources."""


def once(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError("Unexpected v9d Java anchor: " + before.strip().splitlines()[0])
    return source.replace(before, after, 1)


CPU_ROUTING = r"""
    static final int F4_CPU_PROBE_LIMIT = 8000;
    private int f4CpuProbeLimit;
    private long f4CpuLocalCommits, f4CpuProbeAttempts, f4CpuProbePops, f4CpuProbeNs;
    private boolean f4GpuWarm;
    private final long[] f4CpuCohortTrials = new long[2];
    private final long[] f4CpuCohortHits = new long[2];
    private final long[] f4CpuCohortNs = new long[2];
    private final double[] f4GpuCohortNs = new double[2];

    private int f4CpuCohort(Connection connection) {
        if (f4CpuProbeLimit == 0 || config.isTimingDriven() || connection.isCrossSLR()
                || connection.hasAltSinks() || connection.getSourceRnode() == connection.getSinkRnode()
                || !connection.getSinkRnode().getType().isAnyExclusiveSink()) return -1;
        int span = connection.getHpwl();
        return span < 16 ? (span < 8 ? 0 : 1) : -1;
    }

    private boolean f4CpuWorthProbing(int cohort) {
        if (cohort < 0 || f4GpuCohortNs[cohort] == 0) return false;
        // If A* exhausts its budget, the existing GPU solver starts at the source.
        // Compare the CPU work on all probes against GPU service saved by local hits.
        return f4CpuCohortTrials[cohort] < 4
                || f4CpuCohortNs[cohort] < f4CpuCohortHits[cohort] * f4GpuCohortNs[cohort];
    }

    private boolean f4ProbeAndCommit(ConnectionState state, int cohort) {
        final long started = System.nanoTime();
        final PriorityQueue<RouteNode> queue = state.queue;
        state.f4ProbeRemainingLimit = f4CpuProbeLimit;
        RouteNode target = null;
        try {
            while (state.nodesPopped < f4CpuProbeLimit && !queue.isEmpty()) {
                RouteNode candidate = queue.poll();
                ++state.nodesPopped;
                if (candidate.isTarget()) {
                    target = candidate;
                    break;
                }
                exploreAndExpand(state, candidate);
            }
        } finally {
            state.f4ProbeRemainingLimit = 0;
        }
        long probeNs = System.nanoTime() - started;
        ++f4CpuProbeAttempts;
        f4CpuProbePops += state.nodesPopped;
        f4CpuProbeNs += probeNs;
        ++f4CpuCohortTrials[cohort];
        int pushed = state.nodesPopped + queue.size();
        state.nodesPushed += pushed;
        nodesPopped.addAndGet(state.nodesPopped);
        nodesPushed.addAndGet(pushed);
        queue.clear();
        if (target == null) {
            state.f4CpuPopsBeforeHandoff = state.nodesPopped;
            state.nodesPopped = 0;
            state.nodesPushed = 0;
            state.earlyTermination = false;
            // The same attempt remains ripped up and retains its own target marks.
            // Rebuild only the descriptor seed; there is no second preparation or commit.
            RouteNode source = state.connection.getSourceRnode();
            source.clearPrev();
            push(state, source, 0f, 0f, false);
        } else {
            finishRouteConnection(state.connection, target);
            if (!state.connection.isRouted()) {
                throw new IllegalStateException("F4 CPU local commit failed sequence=" + state.sequence);
            }
            if (goldenWriter != null) dumpGoldenPath(state.connection, state);
            for (RouteNode mark : state.targets) mark.clearTarget();
            state.targets.clear();
            getFidesLaneGate().mirrorAfterAttempt(state.connection);
            ++f4CpuLocalCommits;
            ++f4CpuCohortHits[cohort];
        }
        f4CpuCohortNs[cohort] += System.nanoTime() - started;
        return target != null;
    }

    private void f4ObserveGpuBatch(List<LaneBatchSlot> batch, long nanos) {
        if (f4CpuProbeLimit == 0) return;
        if (!f4GpuWarm) {
            // The opening batch also pays device residency, so keep it out of warm quotes.
            f4GpuWarm = true;
            return;
        }
        int cohorts = 0;
        for (LaneBatchSlot slot : batch) {
            int cohort = f4CpuCohort(slot.connection);
            if (cohort >= 0) cohorts |= 1 << cohort;
        }
        double perCompletion = (double) nanos / batch.size();
        for (int i = 0; i < f4GpuCohortNs.length; ++i) {
            if ((cohorts & (1 << i)) == 0) continue;
            double old = f4GpuCohortNs[i];
            f4GpuCohortNs[i] = old == 0 ? perCompletion : (old * 7 + perCompletion) / 8;
        }
    }

    org.json.JSONObject f4CpuReceipt() {
        return new org.json.JSONObject().put("cpu_local_commits", f4CpuLocalCommits)
                .put("cpu_probe_attempts", f4CpuProbeAttempts).put("cpu_probe_pops", f4CpuProbePops)
                .put("cpu_probe_ns", f4CpuProbeNs);
    }

    private void f4WriteCpuFinal() {
        if (f4CpuProbeLimit == 0) return;
        java.nio.file.Path work = java.nio.file.Path.of(System.getProperty("fides.ppt.f4.work"));
        java.nio.file.Path pending = work.resolve("f4_hybrid_cpu_final.pending");
        try {
            java.nio.file.Files.writeString(pending, f4CpuReceipt()
                    .put("attempts", connectionsRouted.get()).toString() + "\n",
                    java.nio.file.StandardOpenOption.CREATE_NEW);
            java.nio.file.Files.move(pending, work.resolve("f4_hybrid_cpu_final.json"),
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (java.io.IOException error) {
            throw new java.io.UncheckedIOException("Could not publish F4 CPU routing counters", error);
        }
    }

"""


def patch_router(source):
    source = once(source, "    private int f4Width;\n", "    private int f4Width;\n" + CPU_ROUTING)
    source = once(source, '            f4Width = Integer.getInteger("fides.ppt.f4.lanes", 2);\n',
        '''            f4Width = Integer.getInteger("fides.ppt.f4.lanes", 2);
            f4CpuProbeLimit = Integer.getInteger("fides.ppt.f4.cpuProbeBudget", 0);
            if (f4CpuProbeLimit < 0 || f4CpuProbeLimit > F4_CPU_PROBE_LIMIT)
                throw new IllegalArgumentException("F4 CPU proof budget exceeds 8000 pops");
''')
    source = once(source, '        if (f4Direct != null) f4Direct.report("F4_DIRECT_FINAL");',
        '''        if (f4Direct != null) {
            f4Direct.report("F4_DIRECT_FINAL");
            f4WriteCpuFinal();
        }''')
    source = once(source, '    private void f4RoundBoundary(int overused, int unroutable) {\n',
        '''    private void f4RoundBoundary(int overused, int unroutable) {
        java.util.Arrays.fill(f4CpuCohortTrials, 0);
        java.util.Arrays.fill(f4CpuCohortHits, 0);
        java.util.Arrays.fill(f4CpuCohortNs, 0);
''')
    source = once(source, '''                runAndCommitBatch(batch);
            }
            if (shouldRoute(connection)) {
                // Which arm this connection goes to''', '''                runAndCommitBatch(batch);
            }
            int cpuCohort = f4CpuCohort(connection);
            boolean probeCpu = f4CpuWorthProbing(cpuCohort);
            if (probeCpu && !batch.isEmpty()) {
                // A* reads the live target flags and negotiated state of a committed prefix.
                runAndCommitBatch(batch);
                probeCpu = f4CpuWorthProbing(cpuCohort);
            }
            if (shouldRoute(connection)) {
                // Which arm this connection goes to''')
    source = once(source, '                    collectLaneAttempt(connection, batch, slotStates);',
        '                    collectLaneAttempt(connection, batch, slotStates, probeCpu ? cpuCohort : -1);')
    source = once(source, '''    private void collectLaneAttempt(Connection connection, List<LaneBatchSlot> batch,
                                    List<ConnectionState> slotStates) {''', '''    private void collectLaneAttempt(Connection connection, List<LaneBatchSlot> batch,
                                    List<ConnectionState> slotStates, int cpuCohort) {''')
    source = once(source, '''        prepareRouteConnection(state);

        FidesLane.Attempt attempt;
        try {
            attempt = getFidesLaneGate().begin(connection, state, queue);''', '''        prepareRouteConnection(state);
        state.f4CpuPopsBeforeHandoff = 0;
        if (cpuCohort >= 0 && f4ProbeAndCommit(state, cpuCohort)) return;

        FidesLane.Attempt attempt;
        try {
            attempt = getFidesLaneGate().begin(connection, state, queue);''')
    source = once(source, '    private void runAndCommitF4Batch(List<LaneBatchSlot> batch) {\n',
        '''    private void runAndCommitF4Batch(List<LaneBatchSlot> batch) {
        final long started = System.nanoTime();
''')
    source = once(source, '            gate.f4RecordCommit(slot.connection, slot.state.sequence, path);',
        '''            gate.f4RecordCommit(slot.connection, slot.state.sequence, path,
                    slot.state.f4CpuPopsBeforeHandoff);''')
    source = once(source, '''        gate.f4FinishBatch();
        batch.clear();''', '''        gate.f4FinishBatch();
        f4ObserveGpuBatch(batch, System.nanoTime() - started);
        batch.clear();''')
    source = once(source, '        protected boolean earlyTermination;\n',
        '''        protected boolean earlyTermination;
        int f4ProbeRemainingLimit;
        int f4CpuPopsBeforeHandoff;
''')
    return once(source, '''        if (lookahead) {
            state.nodesPopped++;
            exploreAndExpand(state, childRnode);''',
        '''        if (lookahead && (state.f4ProbeRemainingLimit == 0
                || state.nodesPopped < state.f4ProbeRemainingLimit)) {
            state.nodesPopped++;
            exploreAndExpand(state, childRnode);''')


def patch_gate(source):
    source = once(source, '            if (scores[slot] < 0 || state.nodesPopped != 0) {',
        '''            if (scores[slot] < 0 || state.nodesPopped != 0 || state.f4CpuPopsBeforeHandoff < 0
                    || state.f4CpuPopsBeforeHandoff > RWRoute.F4_CPU_PROBE_LIMIT) {''')
    return once(source, '''    void f4RecordCommit(Connection connection, int sequence, F4Theia50perCapture.BatchPath path) {
        f4Live.recordBatchCommit(connection, sequence, path, this, idMap);''',
        '''    void f4RecordCommit(Connection connection, int sequence, F4Theia50perCapture.BatchPath path, int cpuPops) {
        f4Live.recordBatchCommit(connection, sequence, path, this, idMap, cpuPops);''')


def patch_capture(source):
    source = once(source, '                        .put("feature_score", scores[index]);',
        '''                        .put("feature_score", scores[index])
                        .put("cpu_pops_before_handoff", states.get(index).f4CpuPopsBeforeHandoff);''')
    source = once(source,
        '            json("members.json", new JSONObject().put("members", members).put("f1_plan_ns", corridorPlanNs));',
        '            json("members.json", router.f4CpuReceipt().put("members", members).put("f1_plan_ns", corridorPlanNs));')
    source = once(source, '''                require(state.nodesPopped == 0, "CPU searched a GPU batch member");
                JSONObject reply = replies.getJSONObject(index);''',
        '''                JSONObject reply = replies.getJSONObject(index);
                require(state.nodesPopped == 0
                        && reply.getInt("cpu_pops_before_handoff") == state.f4CpuPopsBeforeHandoff,
                        "GPU reply does not match this attempt's bounded CPU handoff");''')
    source = once(source, '''    void recordBatchCommit(Connection connection, int sequence, BatchPath path, FidesLaneGate gate,
            FidesRrgIdMap idMap) {''',
        '''    void recordBatchCommit(Connection connection, int sequence, BatchPath path, FidesLaneGate gate,
            FidesRrgIdMap idMap, int cpuPops) {''')
    return once(source, '                .put("cpu_pops_before_handoff", 0));',
        '                .put("cpu_pops_before_handoff", cpuPops));')
