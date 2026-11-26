"""Install bounded dependency-ready region scheduling and measured GPU admission."""
from v11_window import replace_once as once


def replace_block(source, first, following, replacement):
    begin = source.index(first)
    end = source.index(following, begin + len(first))
    return source[:begin] + replacement + source[end:]


def router(source, work):
    source = replace_block(source, "    // This cohort is the serial dependency head.",
        "    private static final int F4_REGION_SHIFT", (work / "F4CriticalTrials.inc").read_text())
    source = once(source, "    private static final int F4_REGION_NEAR = 8192;",
                          "    private static final int F4_REGION_NEAR = 256;")
    source = replace_block(source, "        final int[] releaseHead, next, home, head, tail;",
        "        int indexed, occupied, regions, ripFootprints;", """        final int[] releaseHead, next, home, blocker;
        final java.util.TreeSet<Integer> runnable = new java.util.TreeSet<>();
        final List<PriorityQueue<Integer>> byRegion = new ArrayList<>();
""")
    source = replace_block(source, "            head = new int[regions];",
        "            var cursorByNet", """            for (int region = 0; region < regions; ++region) byRegion.add(new PriorityQueue<>());
            java.util.Arrays.fill(releaseHead, -1);
""")
    source = once(source, "                int waitFor = net == null ? -1 : net;",
                          "                int waitFor = -1;")
    source = once(source, """                blocker[i] = waitFor;
                if (!f4FirstShape(c) || !shouldRoute(c)) continue;""", """                blocker[i] = waitFor;
                if (net != null) waitFor = Math.max(waitFor, net);
                if (!f4FirstShape(c) || !shouldRoute(c)) continue;""")
    source = replace_block(source, "                if (spatialHead[at] < 0) {", "                ++indexed;", "")
    source = replace_block(source, "        void publish(int cursor) {",
        "    private void f4FirstBoxTally(", (work / "F4ReadyRegions.inc").read_text())
    source = once(source, """            f4StartCriticalRound(all);
            int retryFillAt = 0;""", """            f4StartCriticalRound();
            f4First.gpuTrials = f4GpuTrials;
            f4First.gpuGainBatches = f4GpuGained;
            int refillAfter = 0;""")
    source = once(source, """                if (f4SharedPool.getParallelism() >= 2 && i >= retryFillAt
                        && (!ready.containsKey(i) || ready.size() < 12)) {
                    long beforeGroup = f4First.groups;
                    f4RegionFill(queue, all, i, gate, ready);
                    if (beforeGroup == f4First.groups) retryFillAt = i + 16;
                }""", """                if (f4SharedPool.getParallelism() >= 2 && i >= refillAfter && ready.size() < 12) {
                    long beforeGroup = f4First.groups;
                    f4RegionFill(queue, all, i, gate, ready);
                    if (beforeGroup == f4First.groups) refillAfter = i + 4;
                }""")
    source = once(source, """                    boolean offer = f4MayOfferCritical(c, state.f4Boundary, tier, chain);
                    var search = f4SearchWithBoundary(state, tier, offer);""", """                    var cost = f4CriticalCost(c, state.f4Boundary, tier, chain);
                    int gpuMode = f4CriticalMode(cost);
                    var search = f4SearchWithBoundary(state, tier, gpuMode != 0);""")
    source = once(source, "                    boolean handoff = offer && search.target == null && search.crossingNs >= 0",
                          "                    boolean handoff = gpuMode != 0 && search.target == null && search.crossingNs >= 0")
    source = once(source, """                        f4CriticalProofPops += state.nodesPopped;
                        f4ObserveCpu(state, tier, search, true);""", """                        int proofPops = state.nodesPopped;
                        f4CriticalProofPops += proofPops;
                        f4ObserveCpu(state, tier, search, true);""")
    source = once(source, "                        f4RecordCriticalGpu(System.nanoTime() - start);",
                          "                        f4RecordCriticalGpu(cost, gpuMode, tier, sequence, proofPops, System.nanoTime() - start);")
    source = once(source, "                        if (chain && tier >= 0) f4RecordCriticalTail(c, search);",
        """                        if (!narrowed || state.f4Boundary != null) f4RecordCriticalTail(cost, search);""")
    # A narrow search exhausted without a target falls back to the original
    # boundary, with a different cost cohort; finish that retry on the host.
    source = once(source, "return f4CpuSearch(state, tier, allowHandoff && !state.connection.isCrossSLR());",
                          "return f4CpuSearch(state, tier, false);")
    source = once(source, "        long regionSpecSelected, regionLeaves, ripFootprints, regionWaitNs, regionCommitNs, hotWidened, chainSerial, gpuBatches;",
        """        long regionSpecSelected, regionLeaves, ripFootprints, regionWaitNs, regionCommitNs, hotWidened, chainSerial, gpuBatches;
        long gpuTrials, gpuGainBatches, gpuCommitNs, cpuTailSamples;""")
    source = once(source, '               .put("hot_widened",hotWidened).put("serial_chain",chainSerial);',
        '''               .put("hot_widened",hotWidened).put("serial_chain",chainSerial)
               .put("gpu_trials_total",gpuTrials).put("gpu_net_gain_batches_total",gpuGainBatches)
               .put("gpu_proof_to_commit_ns",gpuCommitNs).put("critical_cpu_tail_samples",cpuTailSamples);''')
    source = once(source, '''                    .put("critical_cold_start_surplus_ns",f4CriticalPotential)
                    .put("critical_gpu_actual_batches",f4CriticalStarts)''',
        '''                    .put("critical_startup_trial_balance_ns",f4GpuLearningBalance)
                    .put("critical_gpu_trials_total",f4GpuTrials)
                    .put("critical_gpu_net_gain_batches_total",f4GpuGained)
                    .put("critical_gpu_actual_batches",f4CriticalStarts)''')
    return once(source, """            f4CriticalPrior.clear();
            f4CriticalPrior.putAll(f4CriticalNext);
            f4CriticalNext.clear();
""", "")
