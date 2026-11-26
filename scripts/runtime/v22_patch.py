"""Patch v21 sources to v22-A: preserve bounded corridors, unpoison GPU pricing, and skip chronic private-cap tails."""


def once(source, anchor, updated):
    count = source.count(anchor)
    if count != 1:
        raise RuntimeError(f"Expected 1 occurrence (got {count}) for anchor: {anchor.strip().splitlines()[0]}")
    return source.replace(anchor, updated, 1)


def capture(src):
    src = once(
        src,
        "    private int phase;\n",
        "    private int phase;\n    private long lastSealNs;\n    long lastSealNs() { return lastSealNs; }\n",
    )
    src = once(
        src,
        """        try {
            step=dir.resolve("step"+phase);
            int deltaRows=0,historyRows=0,occRows=0,sameRows=0;
            if (phase==1) {""",
        """        try {
            step=dir.resolve("step"+phase);
            int deltaRows=0,historyRows=0,occRows=0,sameRows=0;
            long sealStarted = System.nanoTime();
            if (phase==1) {""",
    )
    src = once(
        src,
        """            }
            sendBatchFrame("snapshot", new JSONObject().put("N",rrg.N).put("M",rrg.M)""",
        """            }
            lastSealNs = System.nanoTime() - sealStarted;
            sendBatchFrame("snapshot", new JSONObject().put("N",rrg.N).put("M",rrg.M)""",
    )
    return src


def gate(src):
    return once(
        src,
        """    void f4FinishBatch() {
        f4Live.finishBatch();
    }""",
        """    void f4FinishBatch() {
        f4Live.finishBatch();
    }

    long f4LastSealNs() {
        return f4Live == null ? 0L : f4Live.lastSealNs();
    }""",
    )


def router(src):
    # 1. Keep bounded widening instead of collapsing hot.streak >= 4 to null original box.
    src = once(
        src,
        "        if (hot.streak >= 4) return null; // Repeatedly routing a hot cell warrants the full original box.\n",
        "        // Keep bounded widening instead of collapsing to the full original box at streak >= 4;\n"
        "        // genuine corridor exhaustion still falls back to the original box in f4SearchWithBoundary.\n",
    )
    src = once(
        src,
        "        int step = 8 * (hot.streak - 1);",
        "        int step = 8 * Math.min(hot.streak - 1, 4);",
    )

    # 2. Exclude accumulated prior-CPU journal.seal() I/O from marginal GPU batch pricing.
    src = once(
        src,
        """        gate.f4FinishBatch();
        f4Diversion.gpu(batch.size(), gate.f4DeviceWidth(), System.nanoTime() - started);
        batch.clear();""",
        """        gate.f4FinishBatch();
        long batchElapsed = System.nanoTime() - started;
        f4Diversion.gpu(batch.size(), gate.f4DeviceWidth(), Math.max(1L, batchElapsed - gate.f4LastSealNs()));
        batch.clear();""",
    )

    # 3. Unblock per-bucket GPU trials and raise tier=0 trial floor above short CPU tails.
    src = once(
        src,
        "    private static final int F4_MAX_GPU_TRIALS = 8;",
        "    private static final int F4_MAX_GPU_TRIALS = 24;",
    )
    src = once(
        src,
        "        int cpuSamples, gpuSamples, offers;",
        "        int cpuSamples, gpuSamples, offers, gpuTrials;",
    )
    src = once(
        src,
        """        if (cost.gpuSamples < 2) {
            if (f4GpuTrials == F4_MAX_GPU_TRIALS) return 0;
            if (tier == 0 && cost.cpuMean() <= f21MinimumWarmNs) return 0;
            // Use the phase forecast with live proofs after a phase transition.
            if (f21EstimatedProofs[tier] == 0 && f21Proofs[tier] < 2) return 0;
            return F4_GPU_TRIAL;
        }""",
        """        if (cost.gpuSamples < 2) {
            if (cost.gpuTrials >= 3 || f4GpuTrials >= F4_MAX_GPU_TRIALS) return 0;
            long warmFloor = f21MinimumWarmNs == Long.MAX_VALUE
                    ? 25_000_000L : Math.max(25_000_000L, f21MinimumWarmNs);
            if (tier == 0 && cost.cpuMean() <= warmFloor + warmFloor / 4) return 0;
            // Use the phase forecast with live proofs after a phase transition.
            if (f21EstimatedProofs[tier] == 0 && f21Proofs[tier] < 2) return 0;
            return F4_GPU_TRIAL;
        }""",
    )
    src = once(
        src,
        """    private void f4RecordCriticalGpu(F4CriticalCost cost, int mode, int tier, int sequence,
                                     int pops, long elapsed) {
        ++f4CriticalStarts;
        ++cost.roundCrossings;
        f4CriticalGpuNs += elapsed;
        long cpuTail = cost.cpuMean();
        boolean cold = !f4GpuCriticalResident;
        if (cold) {
            f4GpuColdActual = elapsed;
            f4GpuLearningBalance += Long.getLong("fides.ppt.f4.prewarmNs", 0L);
            f4GpuCriticalResident = true;
        } else {
            cost.recordGpu(elapsed);
            f21MinimumWarmNs = Math.min(f21MinimumWarmNs, elapsed);
        }
        if (mode == F4_GPU_TRIAL) ++f4GpuTrials;
        else ++f4GpuGained;
        f4GpuLearningBalance = Math.max(0, f4GpuLearningBalance + elapsed - cpuTail);
        f4LatestWarmGain = !cold && cpuTail > elapsed;
        f4First.gpuTrials = f4GpuTrials;
        f4First.gpuGainBatches = f4GpuGained;
        f4First.gpuCommitNs = f4CriticalGpuNs;
        if (mode == F4_GPU_TRIAL || f4GpuGained == 1) {
            var sample = new org.json.JSONObject().put("round", routeIteration)
                    .put("sequence", sequence).put("tier", tier).put("proof_pops", pops)
                    .put("trial", mode == F4_GPU_TRIAL).put("cold", cold)
                    .put("cpu_tail_estimate_ns", cpuTail).put("cpu_tail_samples", cost.cpuSamples)
                    .put("proof_to_commit_ns", elapsed).put("warm_samples", cost.gpuSamples)
                    .put("startup_trial_balance_ns", f4GpuLearningBalance);
            System.out.println("F4_CRITICAL_GPU_SAMPLE " + sample);
        }
    }""",
        """    private void f4RecordCriticalGpu(F4CriticalCost cost, int mode, int tier, int sequence,
                                     int pops, long elapsed, long sealNs) {
        long marginal = Math.max(1L, elapsed - sealNs);
        ++f4CriticalStarts;
        ++cost.roundCrossings;
        f4CriticalGpuNs += elapsed;
        long cpuTail = cost.cpuMean();
        boolean cold = !f4GpuCriticalResident;
        if (cold) {
            f4GpuColdActual = elapsed;
            f4GpuLearningBalance += Long.getLong("fides.ppt.f4.prewarmNs", 0L);
            f4GpuCriticalResident = true;
        } else {
            cost.recordGpu(marginal);
            f21MinimumWarmNs = Math.min(f21MinimumWarmNs, marginal);
        }
        if (mode == F4_GPU_TRIAL) {
            ++f4GpuTrials;
            ++cost.gpuTrials;
        } else {
            ++f4GpuGained;
        }
        f4GpuLearningBalance = Math.max(0, f4GpuLearningBalance + marginal - cpuTail);
        f4LatestWarmGain = !cold && cpuTail > marginal;
        f4First.gpuTrials = f4GpuTrials;
        f4First.gpuGainBatches = f4GpuGained;
        f4First.gpuCommitNs = f4CriticalGpuNs;
        if (mode == F4_GPU_TRIAL || f4GpuGained == 1) {
            var sample = new org.json.JSONObject().put("round", routeIteration)
                    .put("sequence", sequence).put("tier", tier).put("proof_pops", pops)
                    .put("trial", mode == F4_GPU_TRIAL).put("cold", cold)
                    .put("cpu_tail_estimate_ns", cpuTail).put("cpu_tail_samples", cost.cpuSamples)
                    .put("proof_to_commit_ns", elapsed).put("seal_ns", sealNs).put("marginal_ns", marginal)
                    .put("warm_samples", cost.gpuSamples)
                    .put("startup_trial_balance_ns", f4GpuLearningBalance);
            System.out.println("F4_CRITICAL_GPU_SAMPLE " + sample);
        }
    }""",
    )
    src = once(
        src,
        "                        runAndCommitF4Batch(batch);\n"
        "                        f4RecordCriticalGpu(cost, gpuMode, tier, sequence, proofPops, System.nanoTime() - start);",
        "                        runAndCommitF4Batch(batch);\n"
        "                        f4RecordCriticalGpu(cost, gpuMode, tier, sequence, proofPops, System.nanoTime() - start, gate.f4LastSealNs());",
    )

    # 4. Skip chronic private_cap / heavy (>65k pops) connections in later parallel groups and tighten late-round probe cap.
    src = once(
        src,
        "    private java.util.Set<Connection> f4RegionRetired;\n",
        "    private java.util.Set<Connection> f4RegionRetired;\n"
        "    private java.util.Set<Connection> f22SkipPrivatePrev = java.util.Collections.emptySet();\n"
        "    private java.util.Set<Connection> f22SkipPrivateNext = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());\n",
    )
    src = once(
        src,
        "        state.f4PrivatePopCeiling = c.connection.getHpwl() >= 16 ? 65536 : 16384;",
        "        state.f4PrivatePopCeiling = routeIteration >= 3 ? 16384 : (c.connection.getHpwl() >= 16 ? (routeIteration >= 2 ? 32768 : 65536) : 16384);",
    )
    src = once(
        src,
        "                if (f4FirstShape(c)) eligible[indexed++] = i;",
        "                if (f4FirstShape(c) && !f22SkipPrivatePrev.contains(c)) eligible[indexed++] = i;",
    )
    src = once(
        src,
        "                if (value.capped) ++f4First.privateCap; else ++f4First.privateMiss;",
        "                if (value.capped) { ++f4First.privateCap; f22SkipPrivateNext.add(chosen.get(k).connection); } else ++f4First.privateMiss;",
    )
    src = once(
        src,
        "        f4RegionRetired = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());",
        "        f4RegionRetired = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());\n"
        "        f22SkipPrivatePrev = f22SkipPrivateNext;\n"
        "        f22SkipPrivateNext = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());",
    )
    src = once(
        src,
        """                    if (state.f4Boundary == null) f4First.fullPops += state.nodesPopped;
                    else f4First.narrowedPops += state.nodesPopped;""",
        """                    if (state.f4Boundary == null) f4First.fullPops += state.nodesPopped;
                    else f4First.narrowedPops += state.nodesPopped;
                    if (state.nodesPopped > 65536 || (narrowed && state.f4Boundary == null)) f22SkipPrivateNext.add(c);""",
    )
    return src
