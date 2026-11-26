package com.xilinx.rapidwright.rwroute;

import java.util.Arrays;
import org.json.JSONObject;

final class F4RoundDiversion {
    private static final int[] BUDGETS = {32_768, 65_536, 262_144};

    private final int width;
    private final F3RoundAdmission congestion;
    private final long coldNs;
    private final long[] archivedBatchNs;
    private final long[] tailMeanNs = new long[BUDGETS.length];
    private final long[] liveBatchNs, liveBatches;
    private final long[] valid, pops, overBudget, budgetStops, exactTailsNs, exactTails, exactTailPops, estimatedTailsNs;
    private final long[] groups;
    private final long[] previousBatchNs, previousBatches, deviceWidths;
    private final int[] lastTailRound = {-1, -1, -1};
    private final boolean[] keptTail = new boolean[BUDGETS.length];
    private int lastRound;
    private NetWrapper pendingNet;
    private int pendingWidth;
    private int lastExcess = -1;
    private boolean resident;
    private String decision = "DEFER_NO_PREVIOUS_ROUND";

    F4RoundDiversion(int width, long bytes, JSONObject quote) {
        this.width = width;
        coldNs = quote.getLong("cold_upper_ns");
        var warm = quote.getJSONArray("warm_ns_by_width");
        archivedBatchNs = new long[width + 1];
        liveBatchNs = new long[width + 1];
        liveBatches = new long[width + 1];
        groups = new long[width + 1];
        previousBatchNs = new long[width + 1];
        previousBatches = new long[width + 1];
        deviceWidths = new long[width + 1];
        for (int w = 1; w <= width; w++) archivedBatchNs[w] = warm.getLong(w);
        if (coldNs <= 0 || archivedBatchNs[1] <= 0) throw new IllegalArgumentException("Incomplete GPU quote");
        congestion = new F3RoundAdmission(bytes, coldNs);
        valid = new long[BUDGETS.length];
        pops = new long[BUDGETS.length];
        overBudget = new long[BUDGETS.length];
        budgetStops = new long[BUDGETS.length];
        exactTailsNs = new long[BUDGETS.length];
        exactTails = new long[BUDGETS.length];
        exactTailPops = new long[BUDGETS.length];
        estimatedTailsNs = new long[BUDGETS.length];
    }

    static int budget(int tier) {
        return BUDGETS[tier];
    }

    static boolean isBudget(int value) {
        return value == BUDGETS[0] || value == BUDGETS[1] || value == BUDGETS[2];
    }

    static boolean crossed(int popped, int budget, boolean hasWork) {
        return popped > budget && hasWork;
    }

    boolean admits() {
        return "ADMIT".equals(decision);
    }

    private void closeGroup() {
        if (pendingWidth != 0) ++groups[pendingWidth];
        pendingWidth = 0;
        pendingNet = null;
    }

    void cpu(NetWrapper net, int tier, int popped, long elapsedNs,
            long crossingNs, int crossingPops, boolean handedOff) {
        if (tier < 0) {
            closeGroup();
            return;
        }
        ++valid[tier];
        pops[tier] += popped;
        if (crossingNs < 0) {
            closeGroup();
            return;
        }
        ++overBudget[tier];
        if (net != pendingNet || pendingWidth == width) closeGroup();
        pendingNet = net;
        ++pendingWidth;
        if (handedOff) {
            ++budgetStops[tier];
            estimatedTailsNs[tier] += freshTail(tier);
        } else {
            ++exactTails[tier];
            keptTail[tier] = true;
            exactTailsNs[tier] += elapsedNs - crossingNs;
            exactTailPops[tier] += popped - crossingPops;
        }
    }

    void gpu(int members, int deviceWidth, long wallNs) {
        if (deviceWidth < 1 || deviceWidth > members)
            throw new IllegalArgumentException("Invalid completed device batch width");
        ++deviceWidths[deviceWidth];
        // First call includes the cold resident startup. Do not price warm batches with it.
        if (resident) {
            liveBatchNs[members] += wallNs;
            ++liveBatches[members];
        }
        resident = true;
    }

    long f4SingletonQuote() { return batchQuote(1); }
    long f4StartupQuote() { return resident ? 0L : coldNs; }

    private long batchQuote(int members) {
        if (liveBatches[members] > 0) return liveBatchNs[members] / liveBatches[members];
        if (previousBatches[members] > 0) return previousBatchNs[members] / previousBatches[members];
        long archive = archivedBatchNs[members];
        return archive > 0 ? archive : members * archivedBatchNs[1];
    }

    private long freshTail(int tier) {
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

    JSONObject endRound(int round, int excess, int overused, int unroutable, long attempts, long allPops) {
        closeGroup();
        var boundary = congestion.boundary(excess, true, excess == 0 && unroutable == 0);
        long remainingNs = Arrays.stream(exactTailsNs).sum() + Arrays.stream(estimatedTailsNs).sum();
        double supplyRetention = lastExcess > 0 ? Math.min(1.0, (double) excess / lastExcess) : 1.0;
        long forecastNs = (long) (remainingNs * supplyRetention);
        long dispatchNs = 0;
        long realizedNs = 0, realizedMembers = 0;
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
        long setupNs = resident ? 0 : coldNs;
        String current = decision;
        String next;
        switch (boundary) {
            case DRAIN: next = "DRAIN"; break;
            case CONVERGED: next = "CONVERGED"; break;
            case STOP: next = "STOP"; break;
            default:
                next = F3RoundAdmission.evaluate((int) Arrays.stream(overBudget).sum(),
                        forecastNs, Math.max(1, dispatchNs + setupNs), true) == F3RoundAdmission.Gate.ADMIT
                        ? "ADMIT" : "HOLD";
        }
        JSONObject row = new JSONObject().put("round", round).put("current", current).put("next", next)
                .put("excess", excess).put("previous_excess", lastExcess).put("overused_nodes", overused)
                .put("unroutable", unroutable).put("attempts", attempts).put("cpu_pops", allPops)
                .put("eligible_by_service", valid.clone()).put("eligible_pops_by_service", pops.clone())
                .put("heavy_by_service", overBudget.clone()).put("budget_stops_by_service", budgetStops.clone())
                .put("estimated_batches_by_width", groups.clone()).put("observed_cpu_tail_ns", exactTailsNs.clone())
                .put("observed_cpu_tail_pops", exactTailPops.clone())
                .put("estimated_cpu_tail_ns_for_handoffs", estimatedTailsNs.clone())
                .put("forecast_cpu_tail_ns", forecastNs).put("dispatch_ns", dispatchNs)
                .put("setup_ns", setupNs).put("gain_ns", forecastNs - dispatchNs - setupNs)
                .put("resident_started", resident).put("realized_members", realizedMembers)
                .put("realized_batch_ns", realizedNs).put("realized_batches_by_width", liveBatches.clone())
                .put("completed_device_widths", deviceWidths.clone())
                .put("cpu_tail_samples_by_service", exactTails.clone());
        for (int tier = 0; tier < BUDGETS.length; ++tier) {
            if (exactTails[tier] > 0) {
                double remainingPops = (double) exactTailPops[tier] / exactTails[tier];
                double nanosPerPop = exactTailPops[tier] > 0
                        ? (double) exactTailsNs[tier] / exactTailPops[tier] : 0;
                tailMeanNs[tier] = (long) (remainingPops * nanosPerPop);
                lastTailRound[tier] = round;
            }
        }
        for (long[] data : new long[][]{valid, pops, overBudget, budgetStops, exactTailsNs,
                exactTails, exactTailPops, estimatedTailsNs, groups}) Arrays.fill(data, 0);
        if (realizedMembers > 0) {
            System.arraycopy(liveBatchNs, 0, previousBatchNs, 0, liveBatchNs.length);
            System.arraycopy(liveBatches, 0, previousBatches, 0, liveBatches.length);
        }
        Arrays.fill(liveBatchNs, 0);
        Arrays.fill(liveBatches, 0);
        Arrays.fill(deviceWidths, 0);
        Arrays.fill(keptTail, false);
        lastRound = round;
        lastExcess = excess;
        decision = next;
        return row;
    }
}
