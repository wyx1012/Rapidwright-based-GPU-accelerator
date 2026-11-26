"""Replace v10's per-lane quote with bounded CPU windows and whole GPU batch costs."""

from hybrid_v10_java import once


POLICY = r"""
    // Quotes are keyed by same-SLR, cross-SLR, or mixed and by actual GPU width.
    // Only homogeneous committed CPU windows receive credit for a whole request.
    private static final int F4_BENEFIT_WIDTHS = 9;
    private static final int F4_BENEFIT_QUOTES = 3 * F4_BENEFIT_WIDTHS;
    private static final int F4_BENEFIT_REVIEW_BATCHES = 32;
    private static final double F4_BENEFIT_MARGIN = 1.10;
    private boolean f4GpuWarm;
    private final double[] f4GpuWholeBatchNs = new double[F4_BENEFIT_QUOTES];
    private final long[] f4GpuQuotes = new long[F4_BENEFIT_QUOTES];
    private final long[] f4GpuQuotesAtCpuReview = new long[F4_BENEFIT_QUOTES];
    private final long[] f4CpuQuotesSinceGpuReview = new long[F4_BENEFIT_QUOTES];
    private final long[] f4CpuSamples = new long[F4_BENEFIT_QUOTES];
    private final double[] f4CpuWindowCostNs = new double[F4_BENEFIT_QUOTES];
    private final double[] f4CpuWindowSuccessRate = new double[F4_BENEFIT_QUOTES];
    private int f4CpuWindow = -1;
    private NetWrapper f4CpuWindowNet;
    private int f4CpuWindowRemaining;
    private long f4CpuWindowNs;
    private long f4CpuWindowsTried, f4CpuWindowsCompleted, f4CpuBudgetMisses;
    private long f4CpuIncompleteWindows, f4CpuWindowEffortNs, f4GpuWarmBatchNs, f4GpuWarmBatches;
    private long f4CpuWindowQuoteCreditNs, f4GpuRefreshBatches;
    private long f4CpuSameSlrPops, f4CpuCrossSlrPops;

    private static int f4BenefitKey(int cohort, int width) {
        return cohort * F4_BENEFIT_WIDTHS + width;
    }

    private static double f4BenefitMean(double previous, double observed, long samples) {
        return samples == 1 ? observed : (previous * 7 + observed) / 8;
    }

    private int f4ChooseCpuWindow(Connection connection, boolean batchEmpty) {
        // There cannot be a local search while an uncommitted GPU attempt owns target marks.
        if (f4CpuProbeLimit == 0 || !batchEmpty) return -1;
        int cohort = connection.isCrossSLR() ? 1 : 0;
        NetWrapper net = connection.getNetWrapper();
        if (config.isTimingDriven() || connection.hasAltSinks()
                || connection.getSourceRnode() == connection.getSinkRnode()
                || !connection.getSinkRnode().getType().isAnyExclusiveSink()) {
            f4FinishCpuWindow(false);
            return -1;
        }
        if (f4CpuWindow >= 0) {
            if (net == f4CpuWindowNet && f4CpuWindow / F4_BENEFIT_WIDTHS == cohort)
                return f4CpuWindow;
            f4FinishCpuWindow(false);
        }
        int width = Math.min(f4Width, net.getConnections().size());
        int quote = f4BenefitKey(cohort, width);
        if (f4GpuQuotes[quote] == 0) return -1;

        // Refresh the GPU quote with a naturally formed request. Do not flush a partial batch.
        if (f4CpuQuotesSinceGpuReview[quote] >= F4_BENEFIT_REVIEW_BATCHES) {
            f4CpuQuotesSinceGpuReview[quote] = 0;
            ++f4GpuRefreshBatches;
            return -1;
        }
        // A partial window does not establish that a batch was avoided. Include every miss
        // and interrupted window in the measured CPU cost, and only credit complete windows.
        double expectedAvoidedNs = f4GpuWholeBatchNs[quote] * f4CpuWindowSuccessRate[quote];
        boolean needsSample = f4CpuSamples[quote] < 2
                || f4GpuQuotes[quote] - f4GpuQuotesAtCpuReview[quote] >= F4_BENEFIT_REVIEW_BATCHES;
        if (!needsSample && expectedAvoidedNs <= F4_BENEFIT_MARGIN * f4CpuWindowCostNs[quote])
            return -1;
        f4CpuWindow = quote;
        f4CpuWindowNet = net;
        f4CpuWindowRemaining = width;
        f4CpuWindowNs = 0;
        f4GpuQuotesAtCpuReview[quote] = f4GpuQuotes[quote];
        return quote;
    }

    private void f4FinishCpuWindow(boolean completed) {
        int quote = f4CpuWindow;
        if (quote < 0) return;
        long samples = ++f4CpuSamples[quote];
        ++f4CpuWindowsTried;
        ++f4CpuQuotesSinceGpuReview[quote];
        f4CpuWindowEffortNs += f4CpuWindowNs;
        f4CpuWindowCostNs[quote] = f4BenefitMean(f4CpuWindowCostNs[quote], f4CpuWindowNs, samples);
        f4CpuWindowSuccessRate[quote] =
                f4BenefitMean(f4CpuWindowSuccessRate[quote], completed ? 1 : 0, samples);
        if (completed) {
            ++f4CpuWindowsCompleted;
            f4CpuWindowQuoteCreditNs += (long) f4GpuWholeBatchNs[quote];
        } else {
            ++f4CpuIncompleteWindows;
        }
        f4CpuWindow = -1;
        f4CpuWindowNet = null;
    }

    private void f4ObserveCpuWork(ConnectionState state, int popped, long nanos, boolean completed) {
        if (state.connection.isCrossSLR()) f4CpuCrossSlrPops += popped;
        else f4CpuSameSlrPops += popped;
        f4CpuWindowNs += nanos;
        if (!completed) {
            if (popped >= f4CpuProbeLimit) ++f4CpuBudgetMisses;
            f4FinishCpuWindow(false);
        } else if (--f4CpuWindowRemaining == 0) {
            f4FinishCpuWindow(true);
        }
    }

    private void f4ObserveGpuBatch(List<LaneBatchSlot> batch, long nanos) {
        if (f4CpuProbeLimit == 0) return;
        if (!f4GpuWarm) {
            f4GpuWarm = true;
            return;
        }
        int cohorts = 0;
        for (LaneBatchSlot slot : batch)
            cohorts |= slot.connection.isCrossSLR() ? 2 : 1;
        int quote = f4BenefitKey(cohorts - 1, batch.size());
        long samples = ++f4GpuQuotes[quote];
        f4GpuWholeBatchNs[quote] = f4BenefitMean(f4GpuWholeBatchNs[quote], nanos, samples);
        ++f4GpuWarmBatches;
        f4GpuWarmBatchNs += nanos;
    }

    private void f4EndBenefitRound() {
        f4FinishCpuWindow(false);
        // Congestion changes the work between rounds. Require new warm quotes and CPU evidence.
        java.util.Arrays.fill(f4GpuQuotes, 0);
        java.util.Arrays.fill(f4GpuQuotesAtCpuReview, 0);
        java.util.Arrays.fill(f4CpuQuotesSinceGpuReview, 0);
        java.util.Arrays.fill(f4CpuSamples, 0);
        java.util.Arrays.fill(f4CpuWindowCostNs, 0);
        java.util.Arrays.fill(f4CpuWindowSuccessRate, 0);
    }

    private org.json.JSONObject f4BenefitReceipt() {
        return new org.json.JSONObject()
                .put("cpu_windows_tried", f4CpuWindowsTried)
                .put("cpu_windows_completed", f4CpuWindowsCompleted)
                .put("cpu_windows_incomplete", f4CpuIncompleteWindows)
                .put("cpu_window_effort_ns", f4CpuWindowEffortNs)
                .put("cpu_window_estimated_gpu_credit_ns", f4CpuWindowQuoteCreditNs)
                .put("cpu_budget_misses", f4CpuBudgetMisses)
                .put("cpu_same_slr_pops", f4CpuSameSlrPops)
                .put("cpu_cross_slr_pops", f4CpuCrossSlrPops)
                .put("gpu_refresh_admissions", f4GpuRefreshBatches)
                .put("gpu_warm_batches", f4GpuWarmBatches)
                .put("gpu_warm_batch_ns", f4GpuWarmBatchNs);
    }

"""


def patch_rwroute(source):
    """Apply after hybrid_v10_java.patch_router, retaining its attempt and commit path."""
    start = source.index("    private boolean f4GpuWarm;\n")
    end = source.index("    private boolean f4ProbeAndCommit(", start)
    source = source[:start] + POLICY + source[end:]
    source = once(source, "    private boolean f4ProbeAndCommit(ConnectionState state, int cohort) {",
                  "    private boolean f4ProbeAndCommit(ConnectionState state) {")
    source = once(source, "        ++f4CpuCohortTrials[cohort];\n", "")
    source = once(source, "            ++f4CpuCohortHits[cohort];\n", "")
    source = once(source, """        f4CpuProbePops += state.nodesPopped;
        f4CpuProbeNs += probeNs;""", """        int popped = state.nodesPopped;
        f4CpuProbePops += popped;
        f4CpuProbeNs += probeNs;""")
    source = once(source, """        f4CpuCohortNs[cohort] += System.nanoTime() - started;
        return target != null;""", """        f4ObserveCpuWork(state, popped, System.nanoTime() - started, target != null);
        return target != null;""")
    start = source.index("    private void f4ObserveGpuBatch(List<LaneBatchSlot> batch, long nanos) {",
                         source.index("    private boolean f4ProbeAndCommit("))
    end = source.index("    org.json.JSONObject f4CpuReceipt()", start)
    source = source[:start] + source[end:]
    source = once(source, '.put("cpu_probe_ns", f4CpuProbeNs);',
                  '.put("cpu_probe_ns", f4CpuProbeNs).put("batch_benefit", f4BenefitReceipt());')
    source = once(source, """        java.util.Arrays.fill(f4CpuCohortTrials, 0);
        java.util.Arrays.fill(f4CpuCohortHits, 0);
        java.util.Arrays.fill(f4CpuCohortNs, 0);""", "        f4EndBenefitRound();")
    source = once(source, """            int cpuCohort = f4CpuCohort(connection);
            boolean probeCpu = f4CpuWorthProbing(cpuCohort);
            if (probeCpu && !batch.isEmpty()) {
                // A* reads the live target flags and negotiated state of a committed prefix.
                runAndCommitBatch(batch);
                probeCpu = f4CpuWorthProbing(cpuCohort);
            }
""", "")
    source = once(source,
                  "                    collectLaneAttempt(connection, batch, slotStates, probeCpu ? cpuCohort : -1);",
                  """                    int cpuWindow = f4ChooseCpuWindow(connection, batch.isEmpty());
                    collectLaneAttempt(connection, batch, slotStates, cpuWindow >= 0);""")
    source = once(source, "List<ConnectionState> slotStates, int cpuCohort) {",
                  "List<ConnectionState> slotStates, boolean tryLocal) {")
    source = once(source, "        if (cpuCohort >= 0 && f4ProbeAndCommit(state, cpuCohort)) return;",
                  "        if (tryLocal && f4ProbeAndCommit(state)) return;")
    return source
