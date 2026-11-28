package com.xilinx.rapidwright.rwroute;

import com.xilinx.rapidwright.device.IntentCode;

import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.PriorityQueue;

/**
 * The in-JVM driver for M3-step3 Phase 1 item 3: it hands <em>one</em> connection to
 * {@link FidesLaneHost} and checks what comes back.
 *
 * <p>Three jobs, in the order they matter:
 *
 * <ol>
 *   <li><b>Flatten the live world into an {@link FidesLane.Attempt}</b>, using the reference's own
 *       expressions for every weight.  The lane never sees a {@code Connection}; if a number is not
 *       in the attempt, the lane cannot use it, which is what keeps the CUDA port honest.</li>
 *   <li><b>Be the three live interfaces the lane can talk to</b> -- {@link FidesLane.MutableColumns}
 *       (read the three mutable columns off the real {@code RouteNode}s),
 *       {@link FidesLane.LiveOracle} (re-derive each decision through the reference's own code and
 *       compare), and {@link FidesLane.LiveMirror} (write the lane's four push mutations back onto
 *       the objects, so the run can continue exactly as if the reference had routed it).</li>
 *   <li><b>Compare the result against the golden line</b>, field by field, and refuse to continue on
 *       any mismatch.  A gate that reports and then keeps going is not a gate.</li>
 * </ol>
 *
 * <p>This class is deliberately the only place where "the lane" and "the live router" are both in
 * scope.  Every claim of the form "the lane's answer is the reference's answer" is made here, and
 * every one of them is either measured on this run's real data or reported as un-measurable.
 */
final class FidesLaneGate implements FidesLane.MutableColumns, FidesLane.LiveOracle, FidesLane.LiveMirror,
        FidesMutableMirror.LiveView {

    /* ------------------------------------------------------------------ construction */

    private final RWRoute router;
    private final RWRouteConfig config;
    private final RouteNodeGraph graph;
    private final int[] slrData;
    private final FidesLaneHost.Census census = new FidesLaneHost.Census();

    private static final class PreloadedStatic {
        final String rrgPath;
        final String manifestPath;
        final FidesRrgLoader rrg;
        final FidesRrgIdMap idMap;

        PreloadedStatic(String rrgPath, String manifestPath, FidesRrgLoader rrg, FidesRrgIdMap idMap) {
            this.rrgPath = Path.of(rrgPath).toAbsolutePath().normalize().toString();
            this.manifestPath = Path.of(manifestPath).toAbsolutePath().normalize().toString();
            this.rrg = rrg;
            this.idMap = idMap;
        }

        boolean matches(String otherRrg, String otherManifest) {
            if (otherRrg == null || otherManifest == null) return false;
            return rrgPath.equals(Path.of(otherRrg).toAbsolutePath().normalize().toString())
                    && manifestPath.equals(Path.of(otherManifest).toAbsolutePath().normalize().toString());
        }
    }

    private static volatile PreloadedStatic v67Preloaded;

    static void preloadStatic(String rrgPath, String manifestPath) throws IOException {
        if (rrgPath == null || manifestPath == null) return;
        PreloadedStatic cur = v67Preloaded;
        if (cur != null && cur.matches(rrgPath, manifestPath) && cur.rrg.nodeType != null) return;
        FidesRrgLoader loadedRrg = FidesRrgLoader.load(Paths.get(rrgPath));
        FidesRrgIdMap loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), loadedRrg);
        v67Preloaded = new PreloadedStatic(rrgPath, manifestPath, loadedRrg, loadedMap);
    }

    static void preloadStaticSlim(String rrgPath, String manifestPath, int pinfeedOrd) throws IOException {
        if (rrgPath == null || manifestPath == null) return;
        PreloadedStatic cur = v67Preloaded;
        if (cur != null && cur.matches(rrgPath, manifestPath)) return;
        FidesRrgLoader loadedRrg = null;
        FidesRrgIdMap loadedMap = null;
        String idCache = System.getProperty("fides.v37.idmapCache");
        if (idCache != null && !idCache.isEmpty() && Files.isRegularFile(Path.of(idCache))) {
            try {
                FidesRrgLoader hdrRrg = FidesRrgLoader.loadHeaderAndPcc(Paths.get(rrgPath));
                loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), hdrRrg);
                loadedRrg = hdrRrg;
            } catch (Throwable ignored) {
                loadedRrg = null;
                loadedMap = null;
            }
        }
        if (loadedRrg == null) {
            loadedRrg = FidesRrgLoader.loadSlim(Paths.get(rrgPath), -1);
            loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), loadedRrg);
        }
        v67Preloaded = new PreloadedStatic(rrgPath, manifestPath, loadedRrg, loadedMap);
    }

    private final FidesRrgLoader rrg;
    private final FidesRrgIdMap idMap;

    /**
     * The search, whichever implementation {@code --fidesLaneImpl} named.
     *
     * <p>Held as the interface rather than as {@link FidesLaneHost} so that the whole gate -- the
     * attempt flattening, the golden comparison, the census, the five mirror hooks, the event stream --
     * is shared between the Java lane and the CUDA lane instead of being written twice.  That is the
     * entire point of the interface existing.  Only two methods are ever called on it, {@link
     * FidesLane#setEventStream} and {@link FidesLane#route}, which is why widening the type cost nothing
     * else: {@link FidesLane#setEventStream} moved into the interface for this, and the CUDA lane is
     * reached by name so that this source set still compiles without JCuda.
     */
    private final FidesLane lane;

    /**
     * The per-attempt event stream, or {@link FidesEventStream#DISABLED}.
     *
     * <p>Held here and handed to the lane, because the drive points that matter are the lane's: the
     * five methods the reference's search is built from are frozen, so POP cannot be observed from
     * outside the lane at all -- and inside it, {@code push()}'s lookahead arm never touches the queue
     * yet counts as a pop in PUSH's identity, so no queue-level hook could see it either.  The gate's
     * only jobs with respect to this object are to name the attempt, to make sure the bytes reach the
     * file before the run can die, and to close it exactly once.
     */
    private final FidesEventStream events;

    /** The golden file, read once.  Never written. */
    private final List<String> goldenLines;
    private final FidesAttemptReference attemptReference;

    /** Where the lane's line is expected to be, 1-based; see the config option's javadoc. */
    private final int goldenLine;

    /* ------------------------------------------------------------------ per-attempt state */

    /**
     * The connection the gate is currently answering about, and its net -- the cursor.  Set by
     * {@link #enterSlot}, which {@link #begin} and each slot's commit both call.  It is a field rather
     * than a parameter because the {@code LiveOracle} methods the lane calls mid-search do not take
     * one; the obligation that comes with that is the field being right every time it is read, and
     * {@link #enterSlot} is where the two phases that can break it are dealt with.
     */
    private Connection connection;

    /** {@link #connection}'s net, cached because {@code getNetWrapper()} is what the oracle reads. */
    private NetWrapper net;

    /* ------------------------------------------------------------------ measurements */

    private long occQueries;
    private long occNonDefault;
    private long sameNetQueries;
    private long sameNetNonDefault;
    private long histQueries;
    private long histNonDefault;
    private long liveNodePresent;
    private long liveNodeAbsent;
    private long mirrorPushes;
    private long materialisedCalls;
    private long materialisedCreated;
    /**
     * The chain rewrites of {@link #rewritePrevFromPath}: calls, and the links they wrote.
     *
     * <p>Counted so the run can say the write happened rather than that it was supposed to.  The
     * expected shape is one call per committed connection and {@code links == total path length minus
     * the number of committed connections}; a run where the calls are zero is a run whose hand-back
     * rested on a field W attempts shared, which is exactly the state this replaced.
     */
    private long prevRewrites;
    private long prevLinks;
    /* Evaluations of the W<=1 chain check at the head of rewritePrevFromPath, and it
     * exists for the same reason the mirror counters above it do: a check whose count is
     * never printed is a check a reader has to take on the source's word, which is exactly
     * how the retraction this repair undoes stayed invisible for a milestone. */
    private long chainChecked;
    /** Successful independent chain reads at W>1, at each slot's own replay boundary. */
    private long slotChainChecked;
    /** Successful CPU hand-backs, counted after saveRouting's result was independently checked. */
    private long committedConnections;
    /* M6-A: the iteration-boundary witness (see mirrorIterationBoundary).  `iterWitnesses` should
     * equal the round's iteration count and `iterWitnessRouted` the round's connection count, and the
     * closing line prints both -- so that a boundary which stopped firing is visible as a SMALLER
     * first number rather than as a missing line.  A missing line is what the gate refuses on; these
     * two make the refusal and the count two answers instead of one. */
    private long iterWitnesses;
    private long iterWitnessRouted;
    /**
     * How many mirror calls to echo, from {@code -Dfides.lane.trace=N}; 0 disables it.
     *
     * <p>This exists to locate the FIRST divergence between two lanes on the same attempt.  The
     * counters in the census say <em>that</em> the two runs differ (attempt 1: the host lane reports
     * {@code visit-continue=22, childScans=276} and the CUDA lane {@code 31, 202}) but not where, and
     * a first-divergence line is what turns "the search went somewhere else" into a named node and a
     * pair of keys.
     *
     * <p>It is deliberately placed on the mirror rather than on either lane, because the mirror is the
     * one place both lanes pass through with the same four numbers: {@code FidesLaneHost.push} calls
     * {@code mirror.pushed} directly, and {@code FidesLaneCuda.replayRecords} calls it with
     * {@code records[at+1..at+4]} bit-reinterpreted.  A trace in the lane would be two traces that
     * have to agree about a format before they can be compared; a trace here is one print statement
     * and the comparison is by construction.
     *
     * <p>Read once into a static because the reply is the same for every gate in the JVM, and a
     * property read on the hot path would be a real cost in the full-round runs.
     */
    private static final int TRACE_LIMIT = Integer.getInteger("fides.lane.trace", 0);
    private int tracePushes;
    private int traceExpands;
    private long staticChecks;
    private long staticSllYUnverifiable;
    private long staticBaseCostSkipped;
    private long childBaseCostChecks;
    private long edgeChecks;
    private long edgeUnverifiable;
    private int edgeMismatchPrinted;
    /** Cap on the number of slice mismatches dumped in full; the census count stays exact. */
    private static final int EDGE_MISMATCH_LIMIT = 8;
    private long mutableChecks;
    private long oracleAgree;
    private long oracleDisagree;
    private long oracleUnverifiable;

    /**
     * {@code --fidesLaneVerify} absent turns the oracle family off, and the census then lists those
     * sites as NEVER EVALUATED rather than as passing.  Kept as a field rather than read from
     * {@code config} at each site so that the choice is visible in one place.
     */
    private final boolean oracleOff;

    /* ------------------------------------------------------- whole-round mode (M3-step3 item 5) */

    /**
     * {@code --fidesLaneAll}: the lane takes every connection of the round, not just one.
     *
     * <p>Read once so the choice is visible in one place, and so that the reporting methods can branch
     * on it without consulting {@code config} from inside a per-attempt path.
     */
    private final boolean allMode;

    /** Attempts begun this run.  In all-mode this is also how many attempts the census below covers. */
    private long laneAttempts;

    /* ---- M4-2alpha: the batch driver's witness ------------------------------------------------- */

    /**
     * How many times the batch driver was entered, how many attempts it carried, and the widest batch it
     * was handed.
     *
     * <p>These exist because criterion a ("a batch of one is the serial trajectory itself") is a claim
     * about a mechanism, and a mechanism that is never entered makes the claim vacuously -- the answer
     * would be the frozen golden whether the batch code ran or not.  The mutation that proves this is
     * not vacuous is to make a batch of one quietly take the serial path: every number the run produces
     * stays right, and only {@link #batchAttempts} falls to zero.  Without the counters that mutation is
     * not merely undetected, it is undetectable.
     *
     * <p>{@link #runCalls} is the denominator, and it is counted at {@link #run}/{@link #runBatch}
     * rather than at {@link #begin} on purpose: {@code begin} is where an attempt is described,
     * {@code run} is where it is searched, and the question these two numbers answer together is "of the
     * attempts this gate searched, how many went through the batch driver" -- a ratio of two counters
     * that must be 1:1 at a batch of one.  (Beta splits the counting across two methods because the batch
     * hands the whole group to the lane at once; the sum is the same one it was.)
     */
    private long batchCalls;
    private long batchAttempts;
    private int batchMax;

    /** Attempts searched by this gate, whichever driver asked for them. */
    private long runCalls;

    /** Criterion A verdicts, accumulated.  The round's headline number. */
    private long aPass;
    private long aFail;

    /** Where the first failure was, so a summary can name it without re-reading 84,107 lines. */
    private int firstFailSequence = -1;
    private String firstFailDetail;

    /**
     * {@code census.totalFails()} at the start of the attempt in flight.
     *
     * <p>The census is a field on this gate and is never reset, so it is cumulative and
     * {@code totalFails()} only ever grows.  Comparing the gate's total against zero would therefore
     * mean "has any attempt so far failed", which is right for a single attempt and wrong for a round:
     * after the first failure the run has already stopped.  The delta is what makes "this attempt had
     * a new failure" answerable.
     */
    private long censusFailsAtBegin;

    /**
     * {@code state.sequence} of the attempt in flight -- the all-mode golden line number.
     *
     * <p>Part of the cursor, and moved with it by {@link #enterSlot}.  It was set in {@code begin} alone
     * until M4-2beta, which was correct while exactly one attempt was in flight on the host: the
     * sequence a connection was collected with was still the sequence it was committed with.  At a
     * batch of W it is not, and this field is the one whose wrongness is loudest rather than quietest --
     * {@link #checkGolden} locates the golden line by this number, and the wrong line is a different
     * connection, so the name check there refuses with HARNESS FAULT.  Loud is not the same as safe: the
     * refusal would name the harness for a fault that is the driver's, which is the shape this project
     * has spent the most effort learning to tell apart.
     */
    private int allSequence;

    /**
     * Coverage of the branches that a small sample cannot reach: the highest lookahead count and the
     * deepest frame stack seen across the round.
     *
     * <p>Reported rather than assumed.  The lookahead branch is entered only near a Laguna tile, and
     * the first two diverted attempts were both {@code lka=0, depth=1}, so "the whole round is
     * covered by the diff" is only true to the extent these say so.  If they come out zero, the
     * summary has to say the branch was not exercised instead of letting the empty diff imply it was.
     */
    private int maxLka;
    private int maxFrameDepthSeen;

    /**
     * G4's decomposition of PUSH across the round, and G3's lookahead total.
     *
     * <p>PUSH has three terms in the reference and each is a different branch: the polls, the queue
     * left over when the poll loop broke ({@code pushedThisConnection = nodesPopped + queue.size()}),
     * and the queue that early termination cleared and counted separately.  The per-line number is
     * compared against the golden 84,107 times, but <em>which</em> of the three a line's PUSH came from
     * is not visible in the line, so a round in which one term is always zero would still show a
     * perfect diff.  These counters are that visibility: they are accumulated from the Result the line
     * is rendered from (see {@link #run}), so they describe the same numbers the diff compared.
     */
    private long sumLka;
    private long sumAbandoned;
    private long attemptsWithAbandoned;
    private long sumLeftover;
    private long attemptsWithLeftover;

    /** Set on the first attempt begun, so the close can report a rate over the round. */
    private long allStartedAt;

    /** How often all-mode prints a progress line, in attempts. */
    private static final int PROGRESS_EVERY = 500;

    /**
     * How wide a batch this round runs, read once.
     *
     * <p>Read from {@code config} into a field for the same reason {@link #allMode} is: the verdict
     * methods below branch on it, and a branch that consults {@code config} from inside a per-attempt
     * path is a branch whose reader has to go and find out what the config held at that instant.
     */
    private final int batchWidth;

    /**
     * Criterion A verdicts on the {@code W > 1} track, where a divergence is <b>expected</b> and is
     * therefore recorded rather than fatal.
     *
     * <p>Why it is expected: at a batch of W the search for slot k reads the three mutable columns as
     * they stood at the batch boundary, while the serial search for connection k reads them after
     * connections 0..k-1 have been committed.  Congestion cost is a function of those columns, so the
     * two searches can choose different paths -- and the plan says so in as many words ("W&gt;1 那趟的
     * golden 与冻结值不同. 别只看线长").  The M4-2c criteria are what carry correctness there; a golden
     * diff is not, and this field is how that is made structural rather than a special case that could
     * be quietly relaxed: at {@code W <= 1} the same comparison still refuses and stops the round
     * (mutation-covered), and at {@code W > 1} it counts into {@link #aDiverge} and prints the first
     * site, which is the diagnostic the plan calls 首异位点.
     *
     * <p>What this must NOT become: a way to make a red run green.  The count is reported, the first
     * divergence is named with its full line pair, and the W&gt;1 track has criteria of its own that can
     * go red (determinism across two runs, {@code UNROUTED}, {@code shorts}, the fallback contract).
     */
    private long aDiverge;

    /** Where the first divergence was, and how many were printed. */
    private int firstDivergeSequence = -1;
    private String firstDivergeDetail;
    private int divergePrints;

    /** How many divergences print their two full lines.  See {@link #aDiverge}. */
    private static final int DIVERGE_PRINT_CAP = 5;

    @Override
    public int[] slrData() { return slrData; }


    private final F4Theia50perCapture f4Live = System.getProperty("fides.f4.snapshot") == null ? null : new F4Theia50perCapture(
            java.nio.file.Path.of(System.getProperty("fides.f4.snapshot")));

    F1Corridor.Box f4Boundary(Connection c) {
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
    }

    int f4DeviceWidth() { return f4Live.f4DeviceWidth(); }

    boolean f4CanOffer(Connection c) {
        return f4Live != null && c.getSourceRnode() != c.getSinkRnode() && f4Live.want(c, router.routeIteration);
    }

    boolean f4Supported(Connection c, RWRoute.ConnectionState state) {
        return f4Live != null && c.getSourceRnode() != c.getSinkRnode() && state.targets.size() == 1 && state.targets.get(0) == c.getSinkRnode()
                && f4Live.want(c, router.routeIteration);
    }


    java.util.List<F4Theia50perCapture.BatchPath> f4SearchBatch(
            java.util.List<FidesLane.Attempt> attempts, java.util.List<RWRoute.ConnectionState> states,
            F4FeatureDirect features) {
        long[] scores = new long[states.size()];
        for (int slot = 0; slot < states.size(); ++slot) {
            var state = states.get(slot);
            scores[slot] = features.offer(state.connection, f4Supported(state.connection, state));
            if (scores[slot] < 0 || state.nodesPopped != 0
                    || !F4RoundDiversion.isBudget(state.f4HandoffBudget)
                    || state.f4CpuPopsBeforeHandoff <= state.f4HandoffBudget) {
                throw new IllegalStateException("GPU_ONLY_UNSUPPORTED sequence=" + state.sequence
                        + " connection=" + state.connection.hashCode() + " targets=" + state.targets.size());
            }
        }
        return f4Live.searchBatch(attempts, states, scores, router.routeIteration, rrg, mirror,
                graph.presentCongestionCosts, slrData, graph, idMap, router);
    }

    void f4RecordCommit(Connection connection, int sequence, F4Theia50perCapture.BatchPath path, int cpuPops) {
        f4Live.recordBatchCommit(connection, sequence, path, this, idMap, cpuPops);
    }

    void f4FinishBatch() {
        f4Live.finishBatch();
    }

    private boolean f4NativeReady;
    private boolean f4NativeRegistered;
    private byte[] f4CorrMode;
    private int[] f4CrossSlrIdx;
    private final java.util.IdentityHashMap<NetWrapper, Integer> f4NetIds =
            new java.util.IdentityHashMap<>(1 << 20);
    private RouteNode[] f4NodeCache;

    int f4FastId(RouteNode node) {
        if (node == null) return -1;
        int id = node.f4JournalId;
        if (id >= 0) {
            if (f4NodeCache != null && f4NodeCache[id] == null) f4NodeCache[id] = node;
            return id;
        }
        id = idMap.idOf(node);
        node.f4JournalId = id;
        if (id >= 0 && f4NodeCache != null && f4NodeCache[id] == null) f4NodeCache[id] = node;
        return id;
    }

    RouteNode f4FastNodeAt(int id) {
        RouteNode[] cache = f4NodeCache;
        if (cache != null) {
            RouteNode cached = cache[id];
            if (cached != null) return cached;
        }
        RouteNode resolved = idMap.nodeAt(graph, id);
        if (resolved != null && cache != null) cache[id] = resolved;
        return resolved;
    }

    private int[] f4FastMembersOf(NetWrapper net) {
        final Ids out = new Ids();
        for (Connection c : net.getConnections()) {
            if (c.isDirect()) continue;
            if (!c.hasAltSinks()) {
                int id = f4FastId(c.getSinkRnode());
                if (id >= 0) out.add(id);
            }
            if (c.isRouted()) {
                List<RouteNode> rnodes = c.getRnodes();
                int size = rnodes.size();
                if (size > 0) {
                    boolean exclusive = rnodes.get(0) == c.getSinkRnode() && !c.hasAltSinks();
                    int start = FidesMutableMirror.userDeltaStart(size, exclusive);
                    int end = FidesMutableMirror.userRangeEnd(size, exclusive);
                    for (int i = start; i < end; ++i) {
                        int id = f4FastId(rnodes.get(i));
                        if (id >= 0) out.add(id);
                    }
                }
            }
        }
        return out.take();
    }

    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg) {
        f4NativeInit(workDir, routingGraph, cfg, -1);
    }

    void f4NativeInit(String workDir, RouteNodeGraph routingGraph, RWRouteConfig cfg, int expectedConns) {
        if (f4NativeReady || workDir == null || mirror == null || cfg.isTimingDriven() || routingGraph.isVersal) return;
        try {
            String libPath = workDir + "/libfides_cpu.so";
            String rrgPath = cfg.getFidesLaneRrg();
            String layoutPath = workDir + "/preflight-access-layout.txt";
            if (!Files.isRegularFile(Path.of(libPath)) || rrgPath == null || !Files.isRegularFile(Path.of(layoutPath))) {
                System.out.println("FIDES_NATIVE_CPU skipped (missing artifact)");
                return;
            }
            FidesNativeCpu.load(libPath);
            FidesNativeCpu.nativeSetDesignConnCount(expectedConns);
            var occF = FidesMutableMirror.class.getDeclaredField("occ");
            var histF = FidesMutableMirror.class.getDeclaredField("hist");
            occF.setAccessible(true);
            histF.setAccessible(true);
            int[] occArr = (int[]) occF.get(mirror);
            float[] histArr = (float[]) histF.get(mirror);
            int[] nextLag = routingGraph.nextLagunaColumn != null ? routingGraph.nextLagunaColumn : new int[0];
            int[] prevLag = routingGraph.prevLagunaColumn != null ? routingGraph.prevLagunaColumn : new int[0];
            PartialRouter.awaitV67Preload();
            f4NativeReady = FidesNativeCpu.nativeAttachDynamicState(slrData,
                    routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                    occArr, histArr, routingGraph.presentCongestionCosts);
            if (!f4NativeReady) {
                f4NativeReady = FidesNativeCpu.nativeInit(rrgPath, layoutPath, slrData,
                        routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                        occArr, histArr, routingGraph.presentCongestionCosts);
            }
            if (f4NativeReady) f4NodeCache = new RouteNode[rrg.N];
            System.out.println("FIDES_NATIVE_CPU ready=" + f4NativeReady + " N=" + rrg.N + " M=" + rrg.M);
        } catch (Throwable t) {
            System.out.println("FIDES_NATIVE_CPU init failed (fallback to Java): " + t);
            f4NativeReady = false;
        }
    }

    boolean f4NativeEnabled() { return f4NativeReady && f4NativeRegistered; }

    boolean f4NativeRegisterAll(List<Connection> all, RWRouteConfig cfg) {
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
        final byte[] corrMode = new byte[n];
        final int[][] altPerConn = new int[n][];
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
                    boolean hasAlts = c.hasAltSinks();
                    int[] alts = null;
                    boolean altsOk = true;
                    if (hasAlts) {
                        List<RouteNode> altList = c.getAltSinkRnodes();
                        int aCnt = altList.size();
                        if (aCnt <= 0 || aCnt > 5 || sType == null || sType.isAnyExclusiveSink()
                                || !(sType == RouteNodeType.LOCAL_EAST || sType == RouteNodeType.LOCAL_WEST
                                     || sType == RouteNodeType.LOCAL_BOTH)) {
                            altsOk = false;
                        } else {
                            alts = new int[aCnt];
                            for (int ai = 0; ai < aCnt; ++ai) {
                                RouteNode ar = altList.get(ai);
                                int aid = f4FastId(ar);
                                if (aid < 0 || ar == src || ar == sink) {
                                    altsOk = false;
                                    break;
                                }
                                alts[ai] = aid;
                            }
                        }
                    }
                    altPerConn[i] = (hasAlts && altsOk) ? alts : null;
                    boolean shapeOk = altsOk && src != sink && sId >= 0 && tId >= 0
                            && sType != null
                            && ((!hasAlts && sType.isAnyExclusiveSink())
                                || (hasAlts && (sType == RouteNodeType.LOCAL_EAST
                                                || sType == RouteNodeType.LOCAL_WEST
                                                || sType == RouteNodeType.LOCAL_BOTH)))
                            && dHpwl > 0;
                    byte fl = 0;
                    if (shapeOk) fl |= 1;
                    if (useOrigBox) fl |= 2;
                    if (uturn) fl |= 4;
                    if (c.isCrossSLR()) fl |= 8;
                    if (c.isCrossSLRnorth()) fl |= 16;
                    if (c.isCrossSLRsouth()) fl |= 32;
                    if (sType == RouteNodeType.EXCLUSIVE_SINK_BOTH || sType == RouteNodeType.LOCAL_BOTH) fl |= 64;
                    if (hasAlts && altsOk) fl |= (byte) 128;

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
                    byte cm = 0;
                    if (useOrigBox && src != null && sink != null && !c.hasAltSinks()) {
                        if (!c.isCrossSLR()) {
                            if (src.getTile().getSLR().getId() == sink.getTile().getSLR().getId()) {
                                cm = (c.getHpwl() >= 16) ? (byte) 2 : (byte) 1;
                            }
                        } else {
                            cm = 3;
                        }
                    }
                    corrMode[i] = cm;
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
        int[] altOffsets = new int[n + 1];
        int totalAlts = 0;
        for (int i = 0; i < n; ++i) {
            altOffsets[i] = totalAlts;
            if (altPerConn[i] != null) totalAlts += altPerConn[i].length;
        }
        altOffsets[n] = totalAlts;
        int[] altIds = new int[totalAlts];
        if (totalAlts > 0) {
            int pos = 0;
            for (int i = 0; i < n; ++i) {
                int[] a = altPerConn[i];
                if (a != null) {
                    System.arraycopy(a, 0, altIds, pos, a.length);
                    pos += a.length;
                }
            }
        }
        FidesNativeCpu.nativeRegisterAltSinks(altOffsets, altIds);
        this.f4CorrMode = corrMode;
        int crossCnt = 0;
        for (int i = 0; i < n; ++i) {
            if (corrMode[i] == 3) ++crossCnt;
        }
        int[] crossIdx = new int[crossCnt];
        int ci = 0;
        for (int i = 0; i < n; ++i) {
            if (corrMode[i] == 3) crossIdx[ci++] = i;
        }
        this.f4CrossSlrIdx = crossIdx;
        f4NativeRegistered = true;
        double sec = (System.nanoTime() - t0) / 1e9;
        System.out.printf("FIDES_NATIVE_CPU v37 registered=%d nets=%d init_s=%.2f%n", n, numNets, sec);
        return true;
    }

    void f4NativeUpdatePcc(float[] pcc) {
        if (f4NativeReady) FidesNativeCpu.nativeUpdatePcc(pcc);
    }

    int[] f4CrossSlrIndices() {
        return f4CrossSlrIdx;
    }

    void f4NativeInitCorridors(F1Corridor corridor) {
        if (!f4NativeReady || f4CorrMode == null || corridor == null) return;
        FidesNativeCpu.nativeInitCorridorGrid(
                corridor.fabricLeft(), corridor.fabricBottom(), corridor.fabricRight(), corridor.fabricTop(),
                corridor.coarseLeft(), corridor.coarseBottom(), corridor.coarseHeight(),
                corridor.coarseOccupancy(), f4CorrMode);
        f4CorrMode = null;
    }

    void f4NativeSeedCrossSlrCorridors(int[] crossIdx, int count, int[] bx0, int[] by0, int[] bx1, int[] by1, byte[] buse) {
        if (!f4NativeReady || count <= 0) return;
        FidesNativeCpu.nativeSeedCrossSlrCorridors(crossIdx, count, bx0, by0, bx1, by1, buse);
    }

    void f4NativeRouteRound(int routeIter, int[] activeConns, int activeCount,
                            int[] bx0, int[] by0, int[] bx1, int[] by1, byte[] buse,
                            float shareWeight, float rnodeCostWeight, float rnodeWLWeight, float estWlWeight,
                            int wavePopCeiling, int numThreads, long[] outStats) {
        FidesNativeCpu.nativeRouteRound(routeIter, activeConns, activeCount, bx0, by0, bx1, by1, buse,
                shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                wavePopCeiling, numThreads, outStats);
    }

    int f4NativeUpdateHistAndFeedback(int routeIter, float histFactor, boolean enlargeBbox, int extX, int extY,
                                      int[] outNextActive, int[] outCongested) {
        return FidesNativeCpu.nativeUpdateHistAndFeedback(
                routeIter, histFactor, enlargeBbox, extX, extY, outNextActive, outCongested);
    }

    void f4NativeMaterializeAll(List<Connection> all, RWRoute router, int overusedCount,
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
                }
                missingIds[b] = localIds;
                missingLens[b] = localLens;
            }, router.f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                collectJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        // Phase 2b: 64-shard parallel RouteNode instantiation (disjoint tileAddress per shard).
        final java.util.concurrent.atomic.AtomicInteger unresolvedCount = new java.util.concurrent.atomic.AtomicInteger(0);
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
        if (unresolvedCount.get() > 0)
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
            RouteNode actualSink = c.getSinkRnode();
            if (c.hasAltSinks() && f4FastId(actualSink) != path[0]) {
                RouteNode foundSink = null;
                for (RouteNode cand : c.getAltSinkRnodes()) {
                    if (f4FastId(cand) == path[0]) { foundSink = cand; break; }
                }
                if (foundSink == null) {
                    foundSink = (path[0] >= 0 && path[0] < nodeCache.length && nodeCache[path[0]] != null)
                            ? nodeCache[path[0]] : f4FastNodeAt(path[0]);
                }
                if (foundSink == null) {
                    throw new IllegalStateException("V96 verify missing altSink id=" + path[0] + " conn=" + i);
                }
                actualSink = foundSink;
            }
            chain[0] = actualSink;
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
                System.out.println("V37_MATERIALIZE_VERIFY_OK checked=" + verified);
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
                    localNodes[s] = new java.util.ArrayList<>(256);
                    localNets[s] = new java.util.ArrayList<>(256);
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
                    if (c.hasAltSinks() && f4FastId(sinkRnode) != path[0]) {
                        RouteNode foundSink = null;
                        for (RouteNode cand : c.getAltSinkRnodes()) {
                            if (f4FastId(cand) == path[0]) { foundSink = cand; break; }
                        }
                        if (foundSink == null) {
                            foundSink = (path[0] >= 0 && path[0] < nodeCache.length && nodeCache[path[0]] != null)
                                    ? nodeCache[path[0]] : f4FastNodeAt(path[0]);
                        }
                        if (foundSink == null) {
                            throw new IllegalStateException("V96 materialize missing altSink id=" + path[0] + " conn=" + i);
                        }
                        sinkRnode = foundSink;
                    }
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
        FidesNativeCpu.nativeShutdownGpu();
        long t4 = System.nanoTime();
        System.out.printf(
                "FIDES_NATIVE_CPU v37 materialized=%d overused=%d seq_fix=%d time_s=%.2f (export=%.2f cache=%.2f bind=%.2f users=%.2f)%n",
                n, overusedCount, seqFixes, (t4 - t0) / 1e9, (t1 - t0) / 1e9, (t2 - t1) / 1e9, (t3 - t2) / 1e9, (t4 - t3) / 1e9);
    }

    void f4SyncMirrorConnections(long totalAttempts) {
        if (mirror != null) mirror.connections = totalAttempts;
    }

    long f4LastSealNs() {
        return f4Live == null ? 0L : f4Live.lastSealNs();
    }

    private FidesLaneGate(RWRoute router, RWRouteConfig config, RouteNodeGraph graph) throws IOException {
        this.router = router;
        this.config = config;
        this.graph = graph;
        long tGate0 = System.nanoTime();
        this.slrData = FidesLaneSlrData.fromGraph(graph);
        PartialRouter.awaitV67Preload();
        PreloadedStatic pre = v67Preloaded;
        if (pre != null && pre.matches(config.getFidesLaneRrg(), config.getFidesLaneManifest())) {
            this.rrg = pre.rrg;
            this.idMap = pre.idMap;
            v67Preloaded = null;
            System.out.printf("F4_V67_GATE_OPEN preloaded=true time_s=%.2f%n", (System.nanoTime() - tGate0) / 1e9);
        } else {
            this.rrg = FidesRrgLoader.load(Paths.get(config.getFidesLaneRrg()));
            this.idMap = FidesRrgIdMap.open(Paths.get(config.getFidesLaneManifest()), rrg);
            System.out.printf("F4_V67_GATE_OPEN preloaded=false time_s=%.2f%n", (System.nanoTime() - tGate0) / 1e9);
        }
        this.oracleOff = !config.isFidesLaneVerify();
        this.allMode = config.isFidesLaneAll();
        this.batchWidth = config.getFidesLaneBatch();

        this.lane = openLane();

        /* Opened here rather than at the first attempt so that an unwritable path fails at setup,
         * where the message can still say so, instead of a third of the way into an hours-long round.
         * The gate names each attempt; the lane writes the events.  With no --eventsFile this is
         * DISABLED, every method on it is a no-op, and the run is byte-for-byte the run it was. */
        this.events = FidesEventStream.open(config.getEventsFile(), config.getEventsConn(),
                config.getEventsFrom(), config.getEventsTo(), config.getEventsMax());
        lane.setEventStream(events);

        /* The trace compared against is named separately from the trace being written, and the two
         * being the same file is refused rather than tolerated: a run that read the golden file it was
         * itself producing would compare its output with its output, pass unconditionally, and say
         * nothing.  That failure mode is worth a hard stop, because it is silent. */
        final String against = config.getFidesLaneGolden();
        if (against != null && against.equals(config.getGoldenFile())) {
            throw new IllegalArgumentException("--fidesLaneGolden and --goldenFile both name " + against
                    + ": the lane would be comparing the run's own output with itself.  Name the frozen"
                    + " trace with --fidesLaneGolden and let --goldenFile be this run's output.");
        }
        this.goldenLines = against == null
                ? null : Files.readAllLines(Paths.get(against), StandardCharsets.UTF_8);
        if (goldenLines != null && goldenLines.isEmpty()) {
            throw new IllegalArgumentException("--fidesLaneGolden " + against + " is empty: an empty"
                    + " trace cannot be compared with, and criterion A would be recorded as a failure"
                    + " of the lane rather than of the invocation");
        }
        this.attemptReference = config.getFidesLaneAttempts() == null ? null
                : new FidesAttemptReference(Paths.get(config.getFidesLaneAttempts()), config.getFidesLaneAttemptsSha(),
                        Paths.get(against), config.getFidesLaneReferenceSha(), goldenLines);
        this.goldenLine = config.getFidesLaneGoldenLine() != 0
                ? config.getFidesLaneGoldenLine() : config.getFidesLaneAttempt();
    }

    /**
     * Opens the lane implementation {@code --fidesLaneImpl} names.
     *
     * <p>The host lane is constructed directly; the CUDA lane is looked up by name, because the main
     * source set is compiled without JCuda on its classpath (JCuda arrives through the injected {@code
     * fides_cuda.gradle}, which is what keeps {@code common.gradle} and {@code build.gradle}
     * byte-identical).  A direct reference here would simply not compile in the tree that ships.
     *
     * <p><b>Fail-closed on every path.</b>  An unknown name is refused by {@code RWRouteConfig} before
     * this runs.  A <em>missing</em> implementation -- {@code --fidesLaneImpl cuda} without {@code -I
     * fides_cuda.gradle} -- must abort here rather than fall back to the host lane: a fallback would
     * produce a completely green run that measured the wrong implementation, and the green would be
     * indistinguishable from the real thing.  The three reflective failures are therefore reported with
     * the command that fixes them, not with the {@code ClassNotFoundException} that would read like a
     * broken build.
     *
     * <p>The live present-congestion table is passed by identity, not copied: the skeleton refreshes
     * that array at every iteration boundary, and the copy inside the {@code .bin} is a snapshot from
     * one call inside the flattener.  Handing over the live array object is what makes "the lane read
     * the numbers the reference would have read" true rather than probable -- and on the CUDA side it is
     * the one column that has to be re-uploaded at each boundary, which is a fact the lane cannot
     * discover for itself because an array that is replaced in place looks identical from the inside.
     */
    private FidesLane openLane() throws IOException {
        final String impl = config.getFidesLaneImpl();
        final FidesLane.MutableColumns columns = this;
        final FidesLane.LiveOracle oracle = oracleOff ? null : this;
        final int capacity = 9 * (1 << 17);

        if (FidesLane.LANE_IMPL_HOST.equals(impl)) {
            return new FidesLaneHost(rrg, graph.presentCongestionCosts, columns, oracle, this, capacity,
                    census);
        }

        /* Past the two known names, and RWRouteConfig has already refused anything else; the branch is
         * written this way so that adding a third implementation is a compile error here rather than a
         * silent fall-through to the host lane. */
        if (!FidesLane.LANE_IMPL_CUDA.equals(impl)) {
            throw new IllegalArgumentException("--fidesLaneImpl " + impl + " reached the gate, which"
                    + " RWRouteConfig should have refused; this is a bug in the option's validation");
        }

        final String className = "com.xilinx.rapidwright.rwroute.FidesLaneCuda";
        final Object opened;
        try {
            final Class<?> cls = Class.forName(className);
            final Method open = cls.getMethod("open", FidesRrgLoader.class, float[].class,
                    FidesLane.MutableColumns.class, FidesLane.LiveOracle.class, FidesLane.LiveMirror.class,
                    int.class, FidesLaneHost.Census.class, RWRouteConfig.class);
            if (!FidesLane.class.isAssignableFrom(open.getReturnType())) {
                throw new IOException("--fidesLaneImpl cuda: " + className + ".open returns "
                        + open.getReturnType().getName() + ", which is not a FidesLane");
            }
            opened = open.invoke(null, rrg, graph.presentCongestionCosts, columns, oracle, this, capacity,
                    census, config);
        } catch (ClassNotFoundException e) {
            throw new IOException("--fidesLaneImpl cuda: " + className + " is not on the classpath."
                    + "  The CUDA lane is compiled into its own source set, which is added by an injected"
                    + " init script: run the build with -I fides_cuda.gradle (and see fides_cuda.gradle"
                    + " for why the injection is an init script rather than a build file edit).  This is"
                    + " refused rather than falling back to the host lane, because a fallback would"
                    + " produce a green run that measured the other implementation.", e);
        } catch (NoSuchMethodException e) {
            throw new IOException("--fidesLaneImpl cuda: " + className + " has no usable open("
                    + "FidesRrgLoader, float[], MutableColumns, LiveOracle, LiveMirror, int, Census,"
                    + " RWRouteConfig) factory: " + e.getMessage(), e);
        } catch (InvocationTargetException e) {
            /* The lane's own construction failure -- a missing cubin, no usable device, a refused
             * memory budget.  Unwrapped so the run dies with that message and not with a reflective
             * wrapper around it; the lane is fail-closed already, so this is not a hole. */
            final Throwable cause = e.getCause() == null ? e : e.getCause();
            if (cause instanceof IOException) {
                throw (IOException) cause;
            }
            if (cause instanceof RuntimeException) {
                throw (RuntimeException) cause;
            }
            throw new IOException("--fidesLaneImpl cuda: " + className + ".open failed: " + cause, cause);
        } catch (IllegalAccessException e) {
            throw new IOException("--fidesLaneImpl cuda: " + className + ".open is not accessible: "
                    + e.getMessage(), e);
        }

        if (!(opened instanceof FidesLane)) {
            throw new IOException("--fidesLaneImpl cuda: " + className + ".open returned "
                    + (opened == null ? "null" : opened.getClass().getName()) + ", not a FidesLane");
        }
        return (FidesLane) opened;
    }

    /**
     * Builds the gate, or explains why it cannot be built.
     *
     * <p>The only moment the 6.5 GiB artifact and the 4.13 GiB manifest are read, so the checks that
     * belong to "is this the artifact the trace was made from?" all live here and all print.
     */
    static FidesLaneGate open(RWRoute router, RWRouteConfig config, RouteNodeGraph graph) throws IOException {
        final FidesLaneGate gate = new FidesLaneGate(router, config, graph);
        gate.printArtifactFacts();
        return gate;
    }

    /* ------------------------------------------------------------------ startup facts */

    private void printArtifactFacts() {
        System.out.println("=== FIDES LANE GATE ===");
        /* Which lane answered, by name and by class.  The name comes from the invocation and the class
         * from the object actually constructed, so a run that named one implementation and got the
         * other cannot print this line and stay green.  Printed unconditionally: it is the one fact in
         * this block that is about the thing being tested rather than about its preconditions. */
        System.out.println("lane impl: " + config.getFidesLaneImpl() + " -> " + lane.getClass().getName()
                + " (interface " + FidesLane.class.getName() + ")");
        System.out.println("artifact: " + config.getFidesLaneRrg() + "  N=" + rrg.N + " M=" + rrg.M
                + " version=" + rrg.version + " sections=" + rrg.sections.size());
        System.out.println("manifest: " + config.getFidesLaneManifest() + "  header=" + idMap.headerLines()
                + " runs=" + idMap.runCount() + " lines=" + idMap.scannedLines());
        System.out.println("golden  : this run writes " + config.getGoldenFile() + "; compared against "
                + (config.getFidesLaneGolden() == null ? "(none given)" : config.getFidesLaneGolden())
                + " lines=" + (goldenLines == null ? "(none)" : String.valueOf(goldenLines.size()))
                + "  lane's line=" + goldenLine);

        /* The guards the lane's whole design rests on, measured rather than assumed.  Each of these
         * would make the lane model a different router if it were true. */
        System.out.println("guard isVersal      : " + graph.isVersal + " (must be false)");
        System.out.println("guard lutRoutethru  : " + graph.lutRoutethru + " (must be false)");
        System.out.println("guard timingDriven  : " + config.isTimingDriven() + " (must be false)");
        System.out.println("guard useBoundingBox: " + config.isUseBoundingBox());
        System.out.println("guard useUTurnNodes : " + config.isUseUTurnNodes());
        System.out.println("oracle              : " + (oracleOff ? "OFF (--fidesLaneVerify not given)"
                : "ON"));

        /* Printed only when the stream is on.  A run without --eventsFile therefore prints exactly what
         * it printed before this option existed, which is what makes the no-perturbation criterion (E1:
         * diff the two stdouts) a two-line diff instead of a judgement call. */
        if (events.isEnabled()) {
            System.out.println("events              : " + config.getEventsFile() + " conn="
                    + (config.getEventsConn() == null ? "*" : config.getEventsConn())
                    + " from=" + config.getEventsFrom() + " to=" + config.getEventsTo()
                    + " max=" + config.getEventsMax() + "  (written by the lane, flushed per attempt)");
        }

        /* The two mutable columns the lane can see must be the same objects the reference mutates.
         * Identity, not equality: a copy that happens to agree today is a different thing. */
        System.out.println("pcc array identity  : lane.table == graph.presentCongestionCosts -> "
                + (rrg.presentCongestionCosts == graph.presentCongestionCosts ? "same object"
                        : "DIFFERENT (the lane was handed the live array, this is the .bin copy)"));
        System.out.print("pcc live[0..7]      :");
        for (int i = 0; i < 8; i++) {
            System.out.print(" " + graph.presentCongestionCosts[i]);
        }
        System.out.println();
        System.out.print("pcc artifact[0..7]  :");
        for (int i = 0; i < 8; i++) {
            System.out.print(" " + rrg.presentCongestionCosts[i]);
        }
        System.out.println();
        System.out.println("pcc length          : live=" + graph.presentCongestionCosts.length
                + " artifact=" + rrg.presentCongestionCosts.length);

        /* The pre-condition of the PINFEED argument in the working record §2.5: every NODE_PINFEED
         * rnode that will ever exist must already exist, or the .bin's edge column (built by a forced
         * expansion) would not describe the lazy graph the reference actually walks.  The claim is
         * "connection setup created them all"; the count is what turns the claim into a measurement. */
        long livePinfeed = 0;
        long liveNodes = 0;
        for (RouteNode[] column : graph.nodesMap) {
            if (column == null) {
                continue;
            }
            for (RouteNode rn : column) {
                if (rn == null) {
                    continue;
                }
                liveNodes++;
                if (rn.getIntentCode() == IntentCode.NODE_PINFEED) {
                    livePinfeed++;
                }
            }
        }
        long artifactPinfeed = 0;
        if (rrg.slimPinfeedCount >= 0L) {
            artifactPinfeed = rrg.slimPinfeedCount;
        } else if (rrg.intentCode != null) {
            int pfOrd = IntentCode.NODE_PINFEED.ordinal();
            for (int i = 0; i < rrg.N; i++) {
                if (rrg.intentCode[i] == pfOrd) {
                    artifactPinfeed++;
                }
            }
        } else {
            artifactPinfeed = -1L;
        }
        /* Not a criterion and not comparable.  The two numbers count NODE_PINFEED at two different
         * moments in the graph's life: the .bin was measured after the forced full expansion, while
         * the live graph holds only what the design load has created so far.  So they are allowed to
         * differ, and reading a difference as a disagreement was exactly the mistake working record
         * 2.5 made.  The question that does matter -- whether the .bin edge column describes the live
         * graph this run actually walks -- is not answered here but per expansion, by
         * "oracle: live getChildren() matches the .bin slice", which reads the live list after the
         * reference's own getChildren() has run and compares it node for node. */
        System.out.println("PINFEED materialised: live=" + livePinfeed + " artifact=" + artifactPinfeed
                + "  (not comparable: live counts only what the design load created, the artifact was"
                + " measured after a forced full expansion -- see the per-expansion oracle line below)");
        System.out.println("live rnodes         : " + liveNodes + " (numNodes=" + graph.numNodes() + ")");
        System.out.println("=== END GATE SETUP ===");
    }

    /* ------------------------------------------------------------------ attempt assembly */

    /**
     * Re-points the gate's per-connection cursor, which {@link #begin} set and which every
     * commit-phase reader of {@link #connection} / {@link #net} resolves through.
     *
     * <p>The cursor is a single pair of fields rather than a parameter because two of its readers are
     * not called from here: the {@code LiveOracle} methods ({@link #willOverUse}, {@link #graphAccessible},
     * {@link #sinkAccessible}, {@link #mutableColumns}) are called by the lane while it searches, and
     * the interface they implement carries no connection argument -- the gate is the object that knows
     * which connection the run is on.  So the cursor is a field and the discipline is that it is
     * correct <em>whenever it is read</em>.
     *
     * <p>That discipline is free at a batch of one, where {@code begin} runs and the whole
     * search-then-commit happens before the next {@code begin}: the value {@code begin} left is still
     * the right one.  It is not free at a batch of W, and this method is what buys it back.  The
     * collect phase calls {@code begin} W times before any of them is committed, so the field is left
     * pointing at the last slot's connection; committing slot 0 would then render criterion A against
     * slot W-1's name ({@link #renderLine}, {@link #checkGolden}), read slot W-1's net for the mutable
     * columns, and ask the router's accessibility predicates about slot W-1's connection -- W wrong
     * answers that the batch driver would report as divergence, with a message naming the wrong
     * connection.  The driver therefore calls this at the top of each slot's commit.
     *
     * <p>Only the commit phase needs the correction, which is why a single cursor is enough rather
     * than one per slot.  The mid-search readers are the host lane's -- the CUDA lane never calls the
     * oracle -- and the host lane cannot be driven at a batch above one: {@code RWRouteConfig} refuses
     * {@code --fidesLaneBatch} other than 1 for any implementation but the CUDA one, on the ground
     * that the option is read by the CUDA lane.  So the arm that would break mid-search is the arm
     * that cannot reach W &gt; 1, and the arm that reaches it has no mid-search reader.
     *
     * <p>It sets the cursor and checks nothing, deliberately.  A cursor that is silently wrong is
     * caught immediately by the criterion-A comparison -- which is what makes the fix's effect
     * measurable rather than merely asserted: with this call in place the W&gt;1 commits are green,
     * and a mutation that drops the call turns every one of them red.  The check that the two are
     * describing the same connection is criterion A's, and it already exists.
     *
     * <p><b>The sequence is part of the cursor.</b>  {@link #allSequence} is the third field, and it is
     * set here for the same reason as the other two: {@code begin} used to be the only writer, which was
     * the same statement as "one attempt is in flight".  Its reader is {@link #checkGolden}, which
     * indexes the frozen golden by it -- so a stale one does not produce a subtly wrong verdict, it
     * produces a comparison against another connection's line and a HARNESS FAULT refusal that names
     * the wrong party.  Passing it in rather than deriving it from {@code connection} keeps this method
     * a plain field-setter: the sequence lives on {@code ConnectionState}, which the gate does not hold,
     * and re-deriving it here would be a second place that knows how a sequence is minted.
     */
    void enterSlot(Connection connection, int sequence) {
        this.connection = connection;
        this.net = connection.getNetWrapper();
        this.allSequence = sequence;
    }

    /**
     * Flattens the live connection into the lane's attempt.
     *
     * <p>Every weight is the reference's own expression evaluated on the reference's own fields
     * (copied from {@code routeIndirectConnection} and handed over already computed), because a
     * constant computed twice is a chance to compute it differently.  The lane therefore contains no
     * {@code Math.pow} and no config lookup.
     *
     * @param state the reference's own per-connection state, already filled by the skeleton.
     * @param queue the reference's queue, holding the source and nothing else.
     */
    FidesLane.Attempt begin(Connection connection, RWRoute.ConnectionState state,
                            PriorityQueue<RouteNode> queue) throws FidesLane.Failure {
        /* The cursor, through the one setter there is, so that "where it is set" and "where it is moved"
         * cannot drift apart.  At a batch of one this is the last word on it; at a batch of W the driver
         * moves it again per slot at commit -- see enterSlot, which is where that is argued. */
        enterSlot(connection, state.sequence);

        /* prepareRouteConnection seeded the source through the reference's own push().  The lane
         * seeds its own heap, so the queue has to be empty again before the attempt starts -- and the
         * way to be sure the two seeds describe the same node is to require that the source is the
         * only thing in there, rather than to assume it. */
        if (queue.size() != 1) {
            throw new FidesLane.Failure("state.queue holds " + queue.size() + " nodes after"
                    + " prepareRouteConnection; the lane's seeding assumes exactly the source's");
        }
        final RouteNode sourceRnode = queue.poll();
        if (sourceRnode != connection.getSourceRnode()) {
            throw new FidesLane.Failure("state.queue does not hold the source rnode");
        }

        /* The reference's saveRouting walks prev from the sink and stops at the first null.  It
         * therefore relies on the source's prev being null -- if it is not, the walk runs past the
         * source, saveRouting reports an unrouted connection, and routeIndirectConnection throws
         * "Unable to save routing".  The lane stops at the source by construction (it clears
         * prev[src]), so if this ever fires the two are no longer describing the same search and the
         * hand-back would be wrong in a way no path comparison would show.  Stated as a check
         * because 84,107 golden lines are the only evidence that it never happens. */
        if (sourceRnode.getPrev() != null) {
            throw new FidesLane.Failure("the source rnode " + sourceRnode + " already has prev="
                    + sourceRnode.getPrev() + ": the reference's saveRouting would backtrack past the"
                    + " source and fail, while the lane stops there. This is a real difference, not a"
                    + " guard to relax");
        }

        final FidesLane.Attempt.Builder b = new FidesLane.Attempt.Builder();
        b.sequence = state.sequence;
        /* The stream's CONN line names the attempt, and the name is the identity only -- the leading
         * "Con <id>" -- not connection.toString().  Two reasons, and the second is the one that
         * matters: the full rendering carries the connection's bounding box, which is the fourth class
         * of mutable state (item 5: finishRouteConnection -> saveRouting -> addRnode grows the box, and
         * Con 5261's own detour grows its own), so a name captured here and a name rendered by
         * reportGolden would differ on exactly the connections where the box moved.  What both lead
         * with is stable, and it is what a golden line can be matched against by the same extraction
         * the gate already uses. */
        b.name = conIdOf(connection.toString());
        b.sourceId = idOf(sourceRnode, "source");
        b.sinkId = idOf(connection.getSinkRnode(), "sink");

        final List<RouteNode> targets = state.targets;
        final int[] targetIds = new int[targets.size()];
        for (int i = 0; i < targets.size(); i++) {
            targetIds[i] = idOf(targets.get(i), "target[" + i + "]");
        }
        b.targetIds = targetIds;

        b.rnodeCostWeight = state.rnodeCostWeight;
        b.shareWeight = state.shareWeight;
        b.rnodeWLWeight = state.rnodeWLWeight;
        b.estWlWeight = state.estWlWeight;
        b.criticality = connection.getCriticality();

        b.xCenter = net.getXCenter();
        b.yCenter = net.getYCenter();
        b.doubleHpwl = net.getDoubleHpwl();
        b.netSize = net.getConnections().size();
        b.sinkBeginX = connection.getSinkRnode().getBeginTileXCoordinate();
        b.sinkBeginY = connection.getSinkRnode().getBeginTileYCoordinate();

        b.xMinBB = connection.getXMinBB();
        b.xMaxBB = connection.getXMaxBB();
        b.yMinBB = connection.getYMinBB();
        b.yMaxBB = connection.getYMaxBB();
        b.useBoundingBox = config.isUseBoundingBox();
        b.useUTurnNodes = config.isUseUTurnNodes();
        b.crossSLR = connection.isCrossSLR();
        b.crossSLRnorth = connection.isCrossSLRnorth();
        b.crossSLRsouth = connection.isCrossSLRsouth();
        b.hasAltSinks = connection.hasAltSinks();

        final RouteNode sink = connection.getSinkRnode();
        b.sinkTypeOrdinal = sink.getType().ordinal();
        b.sinkIntentOrdinal = sink.getIntentCode().ordinal();
        b.sinkTileId = sink.getTile().getUniqueAddress();
        b.sinkWireIsCtrl = sink.getWireName().startsWith("CTRL_");

        final FidesLane.Attempt a = b.build();
        if (allMode) {
            return beginAll(a, state);
        }
        return a;
    }

    /**
     * The all-mode replacement for the three-line {@code ATTEMPT} header: one line per attempt, and a
     * progress line every {@link #PROGRESS_EVERY}.
     *
     * <p>One line per attempt rather than nothing at all because a round is long enough that a refusal
     * deep inside it has to be locatable: the counter and the {@code sequence} printed here are what
     * turn "the lane threw somewhere" into "the lane threw on attempt 61,204".  The block being
     * suppressed is the census, the column coverage and the mirror report -- 84,107 x ~104 lines.
     *
     * <p>The progress line carries the two coverage numbers for the same reason the close does: they
     * are the only evidence about whether the lookahead branch was ever entered, and on a run that
     * takes hours the answer is worth having before the end rather than only after it.
     */
    private FidesLane.Attempt beginAll(FidesLane.Attempt a, RWRoute.ConnectionState state) {
        laneAttempts++;
        /* allSequence is NOT set here.  It used to be, and at a batch of one that was the same
         * statement as the line above -- but it is the cursor's third field, and the cursor has one
         * setter (enterSlot, called by begin).  A second writer that happens to agree today is the
         * thing that makes "where is it set" unanswerable tomorrow. */
        censusFailsAtBegin = census.totalFails();
        if (laneAttempts == 1) {
            allStartedAt = System.nanoTime();
            System.out.println("=== FIDES LANE TAKES THE WHOLE ROUND ==="
                    + " one line per attempt; the full census, column coverage and mirror report are"
                    + " printed on the first failure and once more at the close.");
        }
        try (FidesPhaseTimer.Scope logScope = FidesPhaseTimer.enter(FidesPhaseTimer.LOG)) {
        System.out.println("LANE ATTEMPT " + laneAttempts + " sequence=" + a.sequence + " "
                + conIdOf(connection.toString()) + " targets=" + a.targetIds.length
                + " crossSLR=" + a.crossSLR + " altSinks=" + a.hasAltSinks);
        }
        if (laneAttempts % PROGRESS_EVERY == 0) {
            final double secs = Math.max(1e-9, (System.nanoTime() - allStartedAt) / 1e9);
            System.out.println("LANE PROGRESS attempts=" + laneAttempts + " sequence=" + a.sequence
                    + String.format(" elapsed=%.1fs rate=%.1f/s", secs, laneAttempts / secs)
                    + " maxLka=" + maxLka + " maxFrameDepth=" + maxFrameDepthSeen);
        }
        return a;
    }

    private int idOf(RouteNode rnode, String what) throws FidesLane.Failure {
        if (rnode == null) {
            throw new FidesLane.Failure("the " + what + " rnode is null: it must exist before the lane"
                    + " can be handed an id for it");
        }
        final int id = idMap.idOf(rnode);
        if (id < 0) {
            throw new FidesLane.Failure("the " + what + " rnode " + rnode.getTile().getName() + "/"
                    + rnode.getWireName() + " is not in the manifest: the live graph has a node the"
                    + " flattened artifact does not, so the two are not the same graph");
        }
        return id;
    }

    /* ------------------------------------------------------------------ running and reporting */

    /**
     * Run a whole batch of attempts and hand back their results, in order.
     *
     * <p>This is the batch driver's only entry point.  M4-2alpha's body was a loop over {@link #run};
     * M4-2beta hands the whole group to the lane in one call, which is what lets the device lane run W
     * connections in one launch.  The gate's contribution to the batch is unchanged by that: it counts
     * the group, times it and folds the W results, and it still hands the answers back rather than
     * applying them, because committing is the router's business and must happen in sequence order --
     * the gate searching on the device is what I3 makes parallel, not the committing.
     *
     * <p>One flush for the batch and not one per attempt, which is a consequence of the single call and
     * not an optimisation: the lane writes the whole batch's stream before it returns, so a per-attempt
     * flush on this side would have nothing to flush for the lanes that had not been read back yet.
     *
     * <p>The results come back as a list rather than being applied here on purpose.  Committing means
     * {@code finishRouteConnection}, the criterion-A render, the hand-back check and the golden line,
     * and every one of those is the router's business and must happen in sequence order.  So the driver
     * returns the answers and the caller walks them in order, which is also what keeps the commit loop
     * identical in shape to the serial one.
     *
     * @param attempts the batch, in sequence order; empty is legal and returns an empty list.
     */
    List<FidesLane.Result> runBatch(List<FidesLane.Attempt> attempts) throws FidesLane.Failure {
        if (attempts.isEmpty()) {
            /* Counted as nothing at all, including not as a batch.  The witness's `batches` is meant to
             * be the number of launches the round took -- ceil(attempts / W) -- and a legal no-op call
             * that incremented it would make that arithmetic wrong by one for a reader checking it, with
             * nothing on the line to say which of the two numbers was the odd one.  The driver guards
             * this case too (RWRoute:1222); the guard is here because the counters are here. */
            return new ArrayList<>(0);
        }
        batchCalls++;
        batchAttempts += attempts.size();
        if (attempts.size() > batchMax) {
            batchMax = attempts.size();
        }
        runCalls += attempts.size();
        final long t0 = System.nanoTime();
        final List<FidesLane.Result> results;
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.LANE)) {
            results = lane.routeBatch(attempts);
        } finally {
            /* Flushed on both paths, and deliberately not closed on either.
             *
             * The lane fails closed: the whole point of it is to die at the divergence, and the
             * divergence is what is sitting in the 1 MiB buffer when it does.  Flushing here is what
             * puts it on disk -- for a single-attempt run that buffer holds the entire run, so without
             * this the failure would produce an empty file and explain nothing.
             *
             * The finally covers the lane's own refusals, which are thrown from inside route(); the
             * gate's later refusals (criterion A in reportGolden, the hand-back check) happen after
             * this returns, but by then the bytes are already out, because the attempt's last write --
             * its END line -- also happens inside route().  What is *not* written on a failure path is
             * the footer, and that is the point: the footer is the completeness bit, and a stream that
             * died at attempt 61,204 must not be closable as though it had finished.
             *
             * M4-2beta moved this from per-attempt to per-batch, and the argument above is unchanged by
             * that: the buffer is bigger than a whole round and the batch's writes are a superset of the
             * attempt's, so the flush lands after the same bytes in the same order.  A batch that
             * refuses mid-way has already written the CONN and the events of the lanes it finished, for
             * the same reason a refusing attempt has. */
            try (FidesPhaseTimer.Scope logScope = FidesPhaseTimer.enter(FidesPhaseTimer.LOG)) {
                events.flush();
            }
        }
        if (results.size() != attempts.size()) {
            throw new FidesLane.Failure("the lane returned " + results.size() + " results for a batch"
                    + " of " + attempts.size() + ".  The results are committed in slot order, so a"
                    + " short list would leave the last connection(s) of the batch uncommitted -- and the"
                    + " live graph would then hold a search that ran and was never handed back."
                    + "  Refused rather than padded: the missing slot has no answer to pad it with.");
        }
        final double secs = (System.nanoTime() - t0) / 1e9;
        final List<FidesLane.Result> out = new ArrayList<>(attempts.size());
        for (final FidesLane.Result r : results) {
            if (!allMode) {
                /* The time on these lines is the BATCH's, and the suffix says so exactly when the two
                 * are not the same thing.  At a batch of one the string is character for character what
                 * the per-attempt driver printed, which is what keeps a W=1 sample run's stdout
                 * comparable with the archived one. */
                System.out.println("  lane: " + r.toString() + String.format("  [%.3f s]", secs)
                        + (attempts.size() == 1 ? "" : " (batch of " + attempts.size() + ")"));
            }
            out.add(record(r));
        }
        return out;
    }

    FidesLane.Result run(FidesLane.Attempt attempt) throws FidesLane.Failure {
        runCalls++;
        final long t0 = System.nanoTime();
        final FidesLane.Result r;
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.LANE)) {
            r = lane.route(attempt);
        } finally {
            /* See runBatch: the flush, and why it is in a finally and never closes the stream.  This
             * path is the non-batch one, where an attempt is the whole of what there is to flush. */
            try (FidesPhaseTimer.Scope logScope = FidesPhaseTimer.enter(FidesPhaseTimer.LOG)) {
                events.flush();
            }
        }
        final double secs = (System.nanoTime() - t0) / 1e9;
        if (!allMode) {
            System.out.println("  lane: " + r.toString() + String.format("  [%.3f s]", secs));
            return r;
        }
        return record(r);
    }

    /**
     * The per-attempt tallies the round summary is built from, for one result.
     *
     * <p>Split out of {@link #run} when the batch arrived: the numbers are properties of an attempt, not
     * of the call that produced it, and a batch produces W of them from one call.  Keeping this inside
     * {@code run} would have meant the batch path either duplicating it or calling {@code run} per
     * attempt -- and the second of those is exactly the batch driver being bypassed.
     */
    private FidesLane.Result record(FidesLane.Result r) {
        /* All-mode: the per-attempt result line is gone, so the two numbers that say whether the
         * sample is representative are collected here instead.  maxFrameDepth > 1 or lka > 0 means the
         * lookahead frame stack -- the branch neither of the two single-connection runs ever entered --
         * was exercised after all. */
        if (r.lookaheads > maxLka) {
            maxLka = r.lookaheads;
        }
        if (r.maxFrameDepth > maxFrameDepthSeen) {
            maxFrameDepthSeen = r.maxFrameDepth;
        }
        /* G4's decomposition, reconstructed from the Result the line is rendered from rather than read
         * off the host, so that it is a statement about the numbers the diff compared:
         *
         *   PUSH = nodesPushed = (nodesPopped + heap at the measure) + abandonedQueueSize
         *        = polls + lookaheads + heap + abandoned          (the seed is in pushes, not here)
         *
         * so
         *
         *   leftover = nodesPushed - nodesPopped - abandonedQueueSize = heap.size() at RWRoute:1899's
         *              measure point, the queue the poll loop left behind.
         *
         * There is no correction for the node the loop broke on: the measure runs after the break, and
         * nodesPopped already counts that poll.  The abandoned term is the early-termination branch
         * (FidesLaneHost:926), which clears the queue and records what it cleared, so an attempt that
         * early-terminated has leftover 0 and abandoned > 0, and one that did not has the reverse.  All
         * three terms are counted here rather than asserted away: a term that is always zero on this
         * workload has to say so in the round summary. */
        sumLka += r.lookaheads;
        sumAbandoned += r.abandonedQueueSize;
        if (r.abandonedQueueSize > 0) {
            attemptsWithAbandoned++;
        }
        final int leftover = r.nodesPushed - r.nodesPopped - r.abandonedQueueSize;
        sumLeftover += leftover;
        if (leftover > 0) {
            attemptsWithLeftover++;
        }
        return r;
    }

    /**
     * The live node the lane's search stopped on -- the reference's {@code rnode} out of the poll
     * loop, and the argument {@code finishRouteConnection} is handed.
     *
     * <p>Which target the lane stopped on is part of the result, so this is not a formality: taking
     * the sink when the lane stopped on an alt sink would silently route a different path.
     */
    RouteNode resultNode(FidesLane.Result result) throws FidesLane.Failure {
        if (result.targetId < 0) {
            return null;
        }
        final RouteNode rn = idMap.nodeAt(graph, result.targetId);
        if (rn == null) {
            throw new FidesLane.Failure("the lane stopped on id " + result.targetId + ", which has no"
                    + " live rnode; finishRouteConnection could not be handed it");
        }
        return rn;
    }

    /** Read the replay's live chain, before the commit rewrites it from the decoded path. */
    private void checkReplayChain(int[] path, RouteNode sink) throws FidesLane.Failure {
        int at = 0;
        RouteNode walk = sink;
        while (at < path.length && walk != null && idMap.idOf(walk) == path[at]) {
            at++;
            walk = walk.getPrev();
        }
        if (at != path.length) {
            String got = "and then ends -- the sink itself carries no prev at all";
            if (walk != null) {
                final int gotId = idOf(walk, "node on the mirror's chain");
                got = "and then carries " + gotId + " (" + manifestPathName(gotId)
                        + ") where the lane's path has " + path[at] + " ("
                        + manifestPathName(path[at]) + ")";
            }
            throw new FidesLane.Failure("the mirror's write-back does not reproduce the lane's"
                    + " path, and the commit would have hidden it: the object graph's chain from"
                    + " the sink matches the lane's path for " + at + " of " + path.length
                    + " node(s) " + got + ".  Every link of that chain is written by the mirror's"
                    + " replay of the lane's own push events, so a chain that differs means the"
                    + " replay dropped or mis-parented a push -- and the commit would not have said"
                    + " so: it writes the chain from the lane's path, so by the time anything reads"
                    + " the objects the chain is the lane's path again whether or not this replay"
                    + " wrote it");
        }
    }

    @Override
    public void replayBoundary(int slot, int[] path, int sinkId) throws FidesLane.Failure {
        if (batchWidth <= 1) {
            return; // W<=1 retains its existing pre-commit reading, without double counting.
        }
        if (sinkId < 0 && path.length == 0) {
            return; // No found path will be handed to saveRouting.
        }
        final RouteNode sink = sinkId < 0 ? null : idMap.nodeAt(graph, sinkId);
        if (path.length == 0 || path[0] != sinkId || sink == null) {
            throw new FidesLane.Failure("the slot's replay has no valid sink-first path: slot="
                    + slot + " sinkId=" + sinkId + " pathLength=" + path.length);
        }
        checkReplayChain(path, sink);
        slotChainChecked++;
    }

    /**
     * Write {@code prev} along the lane's own path, on the objects, before the reference's
     * backtracking reads it.
     *
     * <p>{@code prev} is <em>one field per node</em> and the nodes are shared between a batch's
     * attempts: the mirror writes it from {@link #pushed}, once per PUSH and abandoned branches
     * included.  Serially that is exactly right, because one attempt is the only writer between
     * {@code begin} and {@code finishRouteConnection} and the field therefore ends the attempt holding
     * the chain the attempt found.  Under a batch of W the W replays all run before the caller commits
     * slot 0 -- {@code FidesLaneCuda.replayRecords} inside the lane's {@code route}, then
     * {@code RWRoute.runBatch}, which commits only after every slot has answered -- so a node on slot
     * 0's path can end up holding the parent slot j&gt;0 pushed for it, and {@code saveRouting}'s walk
     * leaves the path at that node.  Nothing about the <em>search</em> is wrong; what is wrong is
     * handing back W answers through a field that holds one.
     *
     * <p>Measured, on the frozen workload, before this method existed: W=1, 2 and 4 commit the whole
     * 501-attempt prefix, and W=5, 6, 8 and 16 each die at their first batch with five or more members
     * -- the batch holding sequence 362 and its successors -- all four with the <em>same</em> first
     * difference, {@code position 3} on {@code Con 16679}, the host giving
     * {@code INT_X78Y414/INT_NODE_IMUX_47_INT_OUT1} where the lane and the frozen golden both give
     * {@code INT_X78Y414/INT_NODE_IMUX_40_INT_OUT0}.  Four widths, four different batches, one
     * position: a field overwritten by a later writer, not a search that went somewhere else.  Which
     * slot is that later writer depends on the batch's membership, so the width at which this first
     * bites is a property of this workload and not a constant; the fix does not depend on knowing it.
     *
     * <p>The chain is taken from the lane's {@code path} rather than from a per-slot copy of every
     * push, because the lane's path <em>is</em> the answer and the reference's walk reads exactly it:
     * {@code path} is sink-to-source inclusive, {@code path[0] == result.targetId} (the node
     * {@link #resultNode} hands {@code finishRouteConnection}), and the walk reads {@code prev} off
     * {@code path[i]} expecting {@code path[i+1]}.  The last element is the source -- the lane's own
     * backtrack starts at the found target and walks {@code prev} until id {@code -1}, so the array is
     * sink-first and source-last -- and its {@code prev} is cleared: the reference's walk stops at the
     * first null and would otherwise run past the source.  Both ends of the array are therefore
     * checked against the connection's own rnodes and not against each other: the head against the
     * sink the caller passes in, the tail against {@code connection.getSourceRnode()}, which
     * {@link #begin} has already tied to the node the lane seeded (it refuses a queue that does not
     * hold the source).  The first version of the tail guard compared it against the <em>sink</em>, so
     * it refused the first attempt of the first spot-check run with the path's own source in its
     * message; the fix is this check, not a relaxed walk.  That one write is the only link stated differently from the rest -- the rest
     * carry a node and go through {@code setPrev}, while this one clears and goes through
     * {@code RouteNode.clearPrev}, because the setter refuses null by design and the answer's last
     * link is a clear rather than a link.  After the write the object graph holds, along the path, what
     * the serial arm's own pushes would have left there -- which is the point, since the commit is the
     * serial arm's.
     *
     * <p><b>What this does not buy,</b> written down because the next reader will assume it does:
     * after this, {@link #checkHandback}'s comparison is no longer evidence that the mirror's
     * write-back agreed with the lane -- the chain is that agreement, by construction.  It is evidence
     * that the reference's own backtracking, on the reference's own objects, turns the written chain
     * into the lane's array exactly.  The independent evidence that the routing itself is right is
     * where it always was: criterion A on the rendered golden line, and the byte-identity of the whole
     * file against the frozen one.
     *
     * <p><b>The one instant at which the mirror can still be read, and what was lost before
     * it was.</b>  Until 2026-10-05 the paragraph above stood alone and the retraction was
     * silent: with the chain written from the lane's path before anything read the objects,
     * the mirror's own write-back was overwritten at every commit, so dropping it -- the
     * mutation table's `gate-handback-prev-dropped` entry -- turned nothing red on any arm at
     * any batch width, while the comment on {@code pushed()}'s "cannot happen" throw went on
     * claiming the defense.  The comparison is made again at the head of this method, ahead
     * of the loop that overwrites the chain, which is the only point at which it can be made
     * at all; it is gated to a batch of at most one, which is the configuration in which the
     * chain is still this attempt's own.  (The write it reads is the gate's own
     * LiveMirror#pushed callback, which both lanes make in both rungs, so no
     * --fidesLaneMirror appears in the gate; the first draft of this repair had
     * `mirror != null` in it and would have left rung 1 unread.)  Above a batch of
     * one the independent reading is taken per slot, at that slot's own replay boundary,
     * before the next slot clobbers its links. W>1 still does not claim equality to the W=1
     * frozen trajectory; the replay-chain reading proves the write-back, not that equality.
     */
    void rewritePrevFromPath(FidesLane.Result result, RouteNode sink) throws FidesLane.Failure {
        final int[] path = result.path;
        prevRewrites++;
        if (path.length == 0) {
            throw new FidesLane.Failure("the lane handed back no path, so there is no chain to write;"
                    + " a connection with no path must not be committed");
        }
        for (int i = 0; i < path.length; i++) {
            if (idMap.nodeAt(graph, path[i]) == null) {
                throw new FidesLane.Failure("the lane's path names id " + path[i] + " at position " + i
                        + ", which has no live RouteNode; the chain cannot be written and saveRouting"
                        + " would walk stale pointers");
            }
        }
        /* path[0] is the node the walk starts from.  If it is not the node the caller is about to hand
         * finishRouteConnection, the walk would start somewhere the chain does not cover and the
         * hand-back would be wrong in a way no length check would show.  Both are the lane's
         * result.targetId by construction, so this is a statement that the two readings are still the
         * same reading, not a guard against the lane. */
        if (idMap.nodeAt(graph, path[0]) != sink) {
            throw new FidesLane.Failure("the lane's path starts at id " + path[0] + " ("
                    + manifestPathName(path[0]) + ") but the node handed to the commit is "
                    + sink.getTile().getName() + "/" + sink.getWireName() + "; the chain would be"
                    + " written from the wrong end");
        }
        /* The mirror's own write-back, read BEFORE the loop below overwrites it -- and this is the only
         * instant at which it can be read.  The commit's chain is the lane's path by construction, so
         * checkHandback, which is where this comparison used to be made, can no longer speak about the
         * mirror at all; that retraction is deliberate (see this method's javadoc) and it was silent,
         * because the bytes emitted are the same either way.  What it cost is stated there and is
         * worth restating where the repair is: the only reading under which a mirror that dropped a
         * push is a mirror that lied.  Restated ahead of the write, the comparison is about the
         * mirror's data again.
         *
         * Gated on a batch of at most one.  Above one the chain is legitimately clobbered by later
         * slots' pushes -- the declared semantics of the batch tier, and the reason this method
         * exists -- so a disagreement at commit is not a defect. Above one, replayBoundary
         * checks each slot before the next slot's pushes can clobber it.
         *
         * The gate is NOT `mirror != null`, and this repair's first draft had that conjunct and was
         * wrong.  What this check reads is the gate's own LiveMirror#pushed callback, not the flat
         * mirror's: openLane() hands the gate over as the LiveMirror in both branches (the host at
         * :379 and the CUDA lane at :403) with no --fidesLaneMirror anywhere in the chain,
         * FidesLaneHost makes the call at :565 and :859, and FidesLaneCuda.replayRecords makes it at
         * :4041 for every REC_PUSH.  --fidesLaneMirror builds the FLAT mirror, which is what the lane
         * READS from in rung 2; it does not decide who WRITES prev on the objects.  Gating on it left
         * rung 1 -- the rung the default table runs in; `gate-handback-prev-dropped` is mirror=False
         * -- reading nothing at all.
         *
         * Only the lane's own positions are walked, and the walk stops at the first difference rather
         * than running off the end.  A stale prev on the LAST node is the tail guard's case, below:
         * walking past the source would report it here, in this check's words, and take that guard's
         * witness (FIDES_LANE_DEBUG_FORCE_TAIL_PREV) away from it. */
        if (batchWidth <= 1) {
            chainChecked++;
            checkReplayChain(path, sink);
        }

        for (int i = 0; i + 1 < path.length; i++) {
            idMap.nodeAt(graph, path[i]).setPrev(idMap.nodeAt(graph, path[i + 1]));
            prevLinks++;
        }
        /* The last link is the clear.  Fetch the tail and state the two facts that make clearing it
         * the answer rather than a corruption, before writing:
         *
         *   - it is the node the caller is about to hand finishRouteConnection, i.e. the attempt's own
         *     source.  {@link #checkHandback} compares the reference's backtrack against the lane's
         *     array, so a tail that is some other node still produces an array of the right length --
         *     what it stops being is this attempt's chain, which no length check would show.
         *   - it has no predecessor now.  {@link #begin} already refuses an attempt whose source
         *     carries one ("a real difference, not a guard to relax"), because the reference's walk
         *     would run past the source; reaching here with one means something wrote it after that
         *     check, and this clear would overwrite it instead of reporting it.  The guard's own
         *     preconditions say the value is already null, so this is the second reading of the same
         *     fact, which is why it is a throw and not an assert: -ea is exactly where the check has to
         *     speak, and a bare assert here is the shape that cost this milestone one -ea round. */
        final RouteNode tail = idMap.nodeAt(graph, path[path.length - 1]);
        if (tail == null) {
            throw new FidesLane.Failure("the lane's path names id " + path[path.length - 1]
                    + " at its last position, which has no live RouteNode; the chain's last link"
                    + " cannot be cleared");
        }
        /* The tail is the attempt's source -- the path is sink-first, source-last -- and the oracle
         * for the source is the connection, whose source {@link #begin} already tied to the node the
         * lane seeded.  Stated separately from the head's check above rather than as "tail == head":
         * the two guards are about two different ends, and a path whose last element is some other
         * node would still write an array of the right length and only fail later, in the reference's
         * walk, where the message would name the walk. */
        final RouteNode sourceRnode = connection.getSourceRnode();
        if (sourceRnode == null) {
            throw new FidesLane.Failure("the connection this commit is for has no source rnode, so the"
                    + " chain's last link cannot be attributed to an attempt; committing it would"
                    + " write prev links for a search that was never seeded");
        }
        if (tail != sourceRnode) {
            throw new FidesLane.Failure("the lane's path ends at id " + path[path.length - 1]
                    + " (" + manifestPathName(path[path.length - 1]) + "), which is not this attempt's"
                    + " source (" + sourceRnode.getTile().getName() + "/" + sourceRnode.getWireName()
                    + "): the path's last element must be the source, so this chain is not this"
                    + " attempt's");
        }
        /* Red witness, and the reason this is a knob rather than an argument: the check below cannot
         * be shown to be able to fail by any run that does not plant a predecessor on the source
         * between collect and commit, and "a test that has never been seen red is not evidence" is
         * the rule this milestone wrote for itself.  The knob plants exactly what the guard exists to
         * catch -- and then falls through, so the refusal that follows is the guard's own, in its own
         * words.  A witness that threw here instead would only prove that a throw placed above a
         * guard runs.  Never set by the gate; any run that sets it is a witness run and not a
         * verdict run. */
        if (System.getenv("FIDES_LANE_DEBUG_FORCE_TAIL_PREV") != null) {
            tail.setPrev(tail);
        }
        if (tail.getPrev() != null) {
            throw new FidesLane.Failure("the source rnode " + tail + " carries prev=" + tail.getPrev()
                    + " at commit, having passed the collect-time check that refuses exactly this.  The"
                    + " predecessor was written between the two readings, and the hand-back cannot clear"
                    + " it silently: the reference's walk would run past the source.");
        }
        tail.clearPrev();
    }

    /**
     * After {@code finishRouteConnection} has run: does the reference's own backtracking, on the
     * objects, reproduce the lane's path?
     *
     * <p>This is the check the criterion-A comparison cannot make.  Rendering the golden line from
     * the lane's {@code int[]} proves the lane found the right <em>ids</em>; it says nothing about
     * whether {@code saveRouting} -- which reads {@code prev} off the objects -- finds the same
     * chain.  If the mirror wrote one pointer wrong, the golden line this run emits would differ
     * from the lane's own answer, and the whole-file diff (criterion B) would then be comparing the
     * CPU against a file the CPU wrote.
     *
     * <p><b>Narrowed by {@link #rewritePrevFromPath}, and the narrowing is deliberate.</b>  Since the
     * commit writes the chain from the lane's own path before this runs, a failure here says the
     * reference's backtracking did not turn that chain into that array -- a walk that left the chain
     * (a node whose {@code prev} the walk found non-null where the chain ends) or an object graph
     * missing a node the lane named.  It no longer says the mirror's write-back agreed with the lane,
     * because the chain no longer comes from the mirror.  Do not read a green line here as more than
     * it is; the evidence that the lane and the reference agree is criterion A and the frozen bytes.
     * At a batch of at most one the write-back is not left unread either: {@code
     * rewritePrevFromPath} compares it with the lane's path before it writes, so this method
     * is then about the reference's walk alone, and that check is about the write-back alone.
     * Above a batch of one there is no such reading, and none is claimed.
     */
    void checkHandback(Connection connection, FidesLane.Result result) throws FidesLane.Failure {
        final List<RouteNode> rnodes = connection.getRnodes();
        boolean ok = connection.isRouted() && rnodes.size() == result.path.length;
        /* All-mode prints this only when it is not ok: the success path is four lines x 84,107, and the
         * close already states the aggregate.  Deciding `ok` first and only then printing keeps the
         * quiet path quiet without moving any of the checks. */
        final boolean explain = !allMode;
        if (explain) {
            System.out.println("=== FIDES LANE HAND-BACK ===");
            System.out.println("routed=" + connection.isRouted() + " rnodes=" + rnodes.size()
                    + " lanePath=" + result.path.length);
        }
        if (ok) {
            for (int i = 0; i < rnodes.size(); i++) {
                if (idMap.idOf(rnodes.get(i)) != result.path[i]) {
                    ok = false;
                    System.out.println("  position " + i + ": saveRouting gives "
                            + rnodes.get(i).getTile().getName() + "/" + rnodes.get(i).getWireName()
                            + ", the lane's path has id " + result.path[i] + " ("
                            + manifestPathName(result.path[i]) + ")");
                    break;
                }
            }
        }
        if (explain || !ok) {
            System.out.println("hand-back: " + (ok ? "saveRouting reproduces the lane's path exactly"
                    : "*** DIFFERENT: the object graph and the lane's array disagree ***"));
        }
        if (!ok) {
            throw new FidesLane.Failure("the hand-back does not reproduce the lane's path");
        }
        committedConnections++;
    }

    /**
     * The PRE-COMMIT half of the verdict: the state the lane's answer came from.
     *
     * <p>Read here, before {@code finishRouteConnection}, deliberately.  The mirror check is about the
     * instant the lane read the flat arrays, and the commit itself writes those columns
     * ({@code updateUsersAndPresentCongestionCost}), so a drift check moved after it would compare the
     * mirror against the commit's own writes and report a drift the lane never saw.
     *
     * <p>Criterion A is deliberately NOT here.  See {@link #reportGolden} for why, and for what the
     * first whole-round run measured.
     */
    void report(FidesLane.Result result) throws FidesLane.Failure {
        if (allMode) {
            reportAll(result);
        } else {
            reportSingle(result);
        }
    }

    /**
     * The POST-COMMIT half: criterion A, compared at the instant the golden line is written.
     *
     * <p>This is a separate call rather than part of {@link #report} because the golden line is
     * produced by {@code RWRoute.dumpGoldenPath}, which renders {@code connection.toString()}
     * <em>after</em> {@code finishRouteConnection}.  That method calls {@code saveRouting}, which calls
     * {@link Connection#resetRoute} -- and {@code resetRoute} is only {@code getRnodes().clear()}: it
     * does not reset the bounding box, while every {@link Connection#addRnode} on the walk-back grows
     * it with {@code Math.min}/{@code Math.max}.  The box is therefore a monotone high-water mark over
     * the connection's whole history, and a connection whose own routing reaches outside its current
     * box prints a different {@code toString()} before the commit than after it.  Criterion A used to
     * compare the "before" rendering against a golden line that records the "after" one.
     *
     * <p>Measured, not reasoned -- this is what the first whole-round run found.  L2 (2026-09-21)
     * matched 82,513 lines byte for byte and then refused at line 82,514 on {@code Con 5261},
     * differing by exactly one character: {@code boundbox = [( 67, 366 ), ...]} in the golden against
     * {@code [( 67, 367 ), ...]} in the lane, with the path, POP, PUSH and WL all identical.  And
     * {@code Con 5261} is the only connection among all 84,107 golden lines whose bounding box is not
     * constant: read straight out of the frozen file, its five routings print 367, 367, 367, 367 and
     * then 366, and its last one jumps from WL=7 to WL=61.
     *
     * <p>Mind the direction, because it is the opposite of how it reads: the box GROWING is
     * {@code yMinBB} getting SMALLER.  {@code addRnode} takes {@code Math.min(yMinBB,
     * rn.getBeginTileYCoordinate() - 1)}, so the detour's node at Y=367 lowers 367 to 366 and the box
     * is bigger, not smaller.  That is why the five values above are consistent with a box that is
     * never reset and only ever expands -- and it is the same arithmetic that makes the lane's
     * pre-commit string the one from the PREVIOUS attempt (367, unchanged since line 80,465) while the
     * golden records this attempt's (366).
     *
     * <p>It is the single connection in the round where the two instants differ, which is exactly why
     * one attempt, five hundred attempts and every item-3 and item-4 mutation passed: the defect needs
     * a connection that grows its own box while being routed, and that happened once in 84,107.
     *
     * <p>What this does NOT change: the lane's own golden file was always right.  Every line it wrote
     * is byte-identical to the frozen one, because {@code dumpGoldenPath} already rendered at the
     * post-commit instant.  What was wrong was the verdict -- a reading of the right object at the
     * wrong instant, which is the temporal form of this project's recurring failure (a probe reading
     * the wrong process, a verdict reading the wrong symbol).
     */
    void reportGolden(FidesLane.Result result) throws FidesLane.Failure {
        final boolean a = checkGolden(result);
        if (allMode) {
            if (a) {
                aPass++;
                return;
            }
            if (batchWidth > 1) {
                /* The W>1 track: recorded, not fatal.  See aDiverge for why this is not a relaxation
                 * dressed up as one -- the same comparison, the same line pair, the same first-site
                 * detail; what changes is that a divergence here is the declared semantics rather than
                 * a lane error.  Note the HARNESS FAULT check inside checkGolden is NOT relaxed: a
                 * comparison against the wrong row still throws on this path. */
                aDiverge++;
                if (firstDivergeSequence < 0) {
                    firstDivergeSequence = allSequence;
                    firstDivergeDetail = "criterion A diverged (W=" + batchWidth + " track)";
                    failBlockAll();
                }
                return;
            }
            aFail++;
            if (firstFailSequence < 0) {
                firstFailSequence = allSequence;
                firstFailDetail = "criterion A FAILED";
            }
            failBlockAll();
            throw new FidesLane.Failure("criterion A failed at sequence " + allSequence + ": the"
                    + " lane's line is not golden line " + allSequence + ". See the LANDIFF lines"
                    + " above. The run is stopped rather than left to write the remaining "
                    + (goldenLines == null ? 0 : goldenLines.size() - laneAttempts)
                    + " lines of a golden file that would then be diffed against itself.");
        }
        System.out.println();
        /* No census term: the pre-commit half already refused if any site had failed, so reaching
         * here means the census is clean and this line is about criterion A alone. */
        System.out.println("LANE GATE RESULT: " + (a ? "PASS" : "FAIL"));
        if (!a) {
            throw new FidesLane.Failure("criterion A failed: the lane's line is not the golden line."
                    + " See the LANDIFF lines above. The run is stopped rather than left to produce a"
                    + " golden file that would then be diffed against itself.");
        }
    }

    /** The all-mode failure block, shared by both halves so the two cannot drift apart. */
    private void failBlockAll() {
        System.out.println();
        System.out.println("=== FIDES LANE FULL REPORT (whole-round failure at attempt " + laneAttempts
                + ", sequence " + allSequence + ") ===");
        printCensus();
        printColumnCoverage();
        if (mirror != null) {
            printMirror();
        }
        System.out.println("LANE GATE RESULT: FAIL");
    }

    /**
     * The whole-round state check: one quiet line per attempt when it passes, and the full block plus a
     * refusal when it does not.
     *
     * <p>Kept as a separate method from {@link #reportSingle} rather than folded into it so that the
     * single-connection path -- the one every item-3 and item-4 mutation and every previous verdict
     * was measured through -- is not restructured in order to add a second mode.  The duplicated
     * printers are the same printers.
     *
     * <p>Fail-closed is unchanged: a new census failure, a mirror drift and (in
     * {@link #reportGolden}) a criterion-A failure all stop the run.  What differs from single mode is
     * only how much is printed before the throw, and that the census is read as a delta rather than as
     * a total (see {@link #censusFailsAtBegin}).
     *
     * <p><b>At a batch of W the census delta is a statement about the batch, not about the slot.</b>
     * {@code censusFailsAtBegin} is sampled in {@code beginAll}, which the collect phase runs W times
     * before any commit, and the census is filled by the readback's replays -- which the lane runs for
     * all W slots before this method is reached for slot 0.  So every slot's delta is the whole batch's,
     * and the refusal (if there is one) lands on slot 0 with the batch's failures counted under its
     * sequence.  That is deliberately left as it is.  The alternative -- sampling the census between
     * slots -- needs a hook the lane does not offer, and the failure it would buy is a better *name* in
     * a message, not a different verdict: any census failure still stops the round, which is what
     * fail-closed means here.  Declared rather than silently relied on, because "the delta is per
     * attempt" is exactly the kind of sentence a later reader would otherwise inherit as true.
     */
    private void reportAll(FidesLane.Result result) throws FidesLane.Failure {
        final long newFails = census.totalFails() - censusFailsAtBegin;
        if (mirror != null) {
            /* Same reasoning as single mode: this is the instant the lane read the flat arrays, so a
             * drift found here is a drift the answer came from, and it is fatal rather than noted. */
            mirror.verifyNoDrift();
        }
        if (newFails == 0) {
            return;
        }
        aFail++;
        if (firstFailSequence < 0) {
            firstFailSequence = allSequence;
            /* Says what was actually read.  With the two halves split, a state failure refuses
             * before criterion A is compared, so this must not claim A passed -- the previous
             * wording reported A's result for attempts where A had not run. */
            firstFailDetail = "criterion A not compared (the state check refused first)"
                    + ", new census failures=" + newFails;
        }
        failBlockAll();
        throw new FidesLane.Failure("resident assertion(s) failed during sequence " + allSequence
                + ": " + census.failingSites() + " -- a lane that cannot follow the reference must not"
                + " report a path");
    }

    /** The single-connection state check; unchanged from item 4 apart from the census/CrA split. */
    private void reportSingle(FidesLane.Result result) throws FidesLane.Failure {
        printCensus();
        printColumnCoverage();
        if (mirror != null) {
            /* Printed here as well as at the close because this is the instant the lane read the flat
             * arrays: if the mirror had drifted, this is the state the lane's answer came from, and a
             * closing report alone could not tell a drift that was already there from one introduced
             * afterwards.  verifyClean() is what makes it fatal rather than informative -- a lane that
             * answered from a known-wrong mirror must not be allowed to report a path. */
            printMirror();
            /* Drift only, not artifactMismatches: the lane's answer is only as good as the shadow it
             * read, so a drifted shadow must stop it -- but a stale .bin does not make the shadow
             * wrong, and the closing scan is where that question gets its verdict. */
            mirror.verifyNoDrift();
        }
        /* Moved ahead of criterion A by the same split as above: this half runs before the commit, so
         * a site failure has to refuse here or it would never be read.  The printed lines are
         * unchanged, and a run whose only failure is this one produces the identical output it did
         * before the split.  What does change: a run with BOTH a site failure and a criterion-A
         * failure now reports the site failure, where it used to report A.  Both refuse. */
        if (census.totalFails() != 0) {
            System.out.println();
            System.out.println("LANE GATE RESULT: FAIL");
            throw new FidesLane.Failure("resident assertion(s) failed: " + census.failingSites()
                    + " -- a lane that cannot follow the reference must not report a path");
        }
    }

    private void printCensus() {
        /* The old header said "this attempt only", which was equivalent to the truth while exactly one
         * connection was diverted and false from the moment the lane took a second one: the Census is a
         * field on this gate and is never reset, so it is cumulative in both modes.  A header that
         * misdescribes its own table is worse than no header, because the next reader draws a
         * conclusion from it. */
        System.out.println("=== FIDES LANE CENSUS (assertion sites, "
                + (allMode ? "cumulative over " + laneAttempts + " attempt(s) of this round"
                           : "this attempt only") + ") ===");
        System.out.print(census.report());
    }

    private void printColumnCoverage() {
        /* Which side the lane read from is the difference between criterion C's mutable-column site
         * being a check and being a tautology, so it is stated in the header rather than inferred from
         * the flags.  The queries/nonDefault counters below count the reads either way: nonDefault is
         * what makes the acceptance run's attempt number a measurement (rung 1's historicalCost
         * nonDefault=0 at attempt 1 is why 29,452 is the number that matters). */
        System.out.println("=== FIDES LANE MUTABLE COLUMNS (read through "
                + (mirror == null ? "the live objects -- rung 1" : "the flat mirror -- rung 2") + ") ===");
        System.out.println("occupancy        queries=" + occQueries + " nonDefault=" + occNonDefault);
        System.out.println("sameNetCount     queries=" + sameNetQueries + " nonDefault=" + sameNetNonDefault);
        System.out.println("historicalCost   queries=" + histQueries + " nonDefault=" + histNonDefault);
        /* The two columns that are degenerate at iteration-1 attempt 1.  Printed rather than argued:
         * a run at a later attempt is the only thing that can change these numbers, and the record
         * has to be able to say which columns the gate actually exercised. */
        System.out.println("live node present=" + liveNodePresent + " absent=" + liveNodeAbsent
                + " (absent ones answer with a fresh node's defaults, which is what the reference"
                + " would have read from the node it had just created)");
        System.out.println("mirror pushes=" + mirrorPushes + "  materialisedChildren calls="
                + materialisedCalls + " (created " + materialisedCreated + " live rnodes)");
        System.out.println("prev chain rewrites=" + prevRewrites + " links=" + prevLinks);
        if (!oracleOff) {
            System.out.println("oracle agree=" + oracleAgree + " DISAGREE=" + oracleDisagree
                    + " unverifiable=" + oracleUnverifiable
                    + " staticChecks=" + staticChecks + " edgeChecks=" + edgeChecks
                    + " edgeUnverifiable=" + edgeUnverifiable
                    + " sllYUnverifiable=" + staticSllYUnverifiable
                    + " baseCostSkipped=" + staticBaseCostSkipped
                    + " childBaseCostChecks=" + childBaseCostChecks
                    + " mutableChecks=" + mutableChecks);
            if (oracleDisagree != 0) {
                System.out.println("*** " + oracleDisagree + " oracle disagreements: the lane and the"
                        + " reference router do not agree on this workload ***");
            }
        }
    }

    /* ------------------------------------------------------------------ criterion A */

    /**
     * Compares the lane's attempt with the golden line, field by field, and prints both on any
     * mismatch.
     *
     * <p>The path is rendered from the <em>manifest</em>, not from the live objects: the golden text
     * is {@code tileName/wireName}, and the manifest's own column is the same string produced by the
     * same flattening run the rest of the lane depends on.  So this comparison can only pass if the
     * id &harr; node mapping, the .bin's {@code length} column and the lane's path all agree with an
     * independently written artifact.
     */
    private boolean checkGolden(FidesLane.Result result) throws FidesLane.Failure {
        if (goldenLines == null) {
            System.out.println("=== FIDES LANE CRITERION A === no --fidesLaneGolden given; skipped, and"
                    + " recorded as skipped rather than passed");
            return false;
        }
        /* In all-mode the line is located by the attempt's own sequence number: the default mapping
         * (golden line == sequence) holds only while every attempt before this one emitted a line, and
         * an unrouted attempt consumes a sequence without writing one.  See conIdOf below for what
         * happens when that assumption is wrong. */
        final FidesAttemptReference.Row reference = attemptReference == null ? null : attemptReference.at(allSequence);
        if (reference != null && !reference.routed) {
            boolean identical = attemptReference.checkNoPath(allSequence, connection.toString(), result);
            System.out.println("FIDES_REFERENCE_NOPATH seq="+allSequence+" POP="+result.nodesPopped
                    +" PUSH="+result.nodesPushed+" found="+result.targetId+" path="+result.path.length
                    +" exact="+(identical?1:0));
            return identical;
        }
        final int line = reference != null ? reference.line : (allMode ? allSequence : goldenLine);
        if (line < 1 || line > goldenLines.size()) {
            System.out.println("=== FIDES LANE CRITERION A === golden line " + line
                    + " is outside the file (" + goldenLines.size() + " lines)");
            return false;
        }
        final String want = goldenLines.get(line - 1);
        final String got = renderLine(result);

        /* Before comparing anything else: is this even the right line?
         *
         * The plan for item 5 made this check mandatory, and the reason is that it is the one failure
         * the whole-file diff cannot distinguish from a lane error.  `renderLine` begins with the live
         * connection's own toString(), so a wrong line would show up as an ordinary criterion-A
         * mismatch -- and the reader would conclude the lane searches wrongly, when the truth is that
         * the harness compared against the wrong row.  That is this project's recurring shape: a
         * probe reading the wrong process, a verdict reading the wrong symbol.  With one attempt the
         * two cases were indistinguishable in principle but never arose; with 84,107 the mapping is
         * load-bearing, so it is checked by name rather than trusted.
         *
         * Note this cannot turn a passing run into a failing one: `Con <id>` is part of the string
         * that the comparison below already requires to be equal. */
        final String wantCon = conIdOf(want);
        final String gotCon = conIdOf(connection.toString());
        if (!wantCon.equals(gotCon)) {
            throw new FidesLane.Failure("HARNESS FAULT, not a lane fault: golden line " + line
                    + " belongs to \"" + wantCon + "\" but this attempt is \"" + gotCon + "\"."
                    + " The lane is comparing against the wrong row, so every verdict below would be"
                    + " about the wrong connection. Either the run and the frozen trace are not the"
                    + " same workload, or the line-per-sequence mapping is off (an unrouted attempt"
                    + " consumes a sequence number without emitting a line).");
        }

        /* Printing both lines is the whole of criterion A's value on a mismatch, and 84,107 x 4 lines
         * of "byte-identical" is not: the round summary already states the aggregate, and the point of
         * a quiet mode is that the log stays readable when something does go wrong.  The comparison
         * itself is unchanged -- only whether it announces itself when it agrees. */
        if (want.equals(got)) {
            if (!allMode) {
                System.out.println("=== FIDES LANE CRITERION A (golden line " + line + ") ===");
                System.out.println("GOLDEN: " + want);
                System.out.println("LANE  : " + got);
                System.out.println("criterion A: byte-identical");
            }
            return true;
        }
        /* Printing both lines is the whole of criterion A's value on a mismatch, and it is capped on the
         * W>1 track, where divergences are expected and can number in the thousands: an uncapped log
         * would be 84,107 x 4 lines of the same three-word verdict, and the round summary is where the
         * count belongs.  The cap is on the PRINTING only -- every divergence is still counted into
         * aDiverge, and the first one always prints, which is the one that says where it started. */
        if (batchWidth > 1 && divergePrints >= DIVERGE_PRINT_CAP) {
            if (divergePrints == DIVERGE_PRINT_CAP) {
                divergePrints++;
                System.out.println("=== FIDES LANE CRITERION A === " + DIVERGE_PRINT_CAP
                        + " divergences printed; further ones are counted, not printed (see the round"
                        + " summary's aDiverge).  This is the W=" + batchWidth + " track, where a golden"
                        + " divergence is the declared semantics and not a verdict.");
            }
            return false;
        }
        if (batchWidth > 1) {
            divergePrints++;
        }
        System.out.println("=== FIDES LANE CRITERION A (golden line " + line + ") ===");
        System.out.println("GOLDEN: " + want);
        System.out.println("LANE  : " + got);
        System.out.println("criterion A: *** DIFFERENT ***");
        firstDifference(want, got);
        return false;
    }

    /**
     * The connection identity of a golden line or a {@code connection.toString()}: the leading
     * {@code Con <id>}, which is everything before the first comma.
     *
     * <p>Parsed rather than pattern-matched on purpose.  The id is right-padded by
     * {@code String.format("%6s", id)}, so the two strings are compared after trimming rather than
     * with a regex that would have to know about the padding -- and a comparison that silently
     * matched nothing would report success for the wrong reason, which is the failure this whole
     * method exists to prevent.
     */
    private static String conIdOf(String line) {
        final int bar = line.indexOf(" | ");
        final String header = bar < 0 ? line : line.substring(0, bar);
        final int comma = header.indexOf(',');
        return (comma < 0 ? header : header.substring(0, comma)).trim();
    }

    /** The golden line as the lane would have written it: same fields, same order, same separators. */
    private String renderLine(FidesLane.Result result) {
        final StringBuilder sb = new StringBuilder(connection.toString()).append(" | ");
        int wirelength = 0;
        for (int id : result.path) {
            sb.append(manifestPathName(id)).append(" -> ");
            wirelength += rrg.length[id];
        }
        sb.append("| POP=").append(result.nodesPopped)
          .append(" PUSH=").append(result.nodesPushed)
          .append(" WL=").append(wirelength);
        return sb.toString();
    }

    private String manifestPathName(int id) {
        final String line = idMap.rawLine(id);
        final int a = line.indexOf('\t');
        final int b = line.indexOf('\t', a + 1);
        return (a < 0 || b < 0) ? ("?" + id) : line.substring(a + 1, b);
    }

    private static void firstDifference(String want, String got) {
        final int n = Math.min(want.length(), got.length());
        int i = 0;
        while (i < n && want.charAt(i) == got.charAt(i)) {
            i++;
        }
        System.out.println("  first difference at char " + i + " (want len " + want.length()
                + ", got len " + got.length() + ")");
        System.out.println("  want ..." + slice(want, i) + "...");
        System.out.println("  got  ..." + slice(got, i) + "...");
    }

    private static String slice(String s, int i) {
        final int from = Math.max(0, i - 60);
        final int to = Math.min(s.length(), i + 60);
        return s.substring(from, to);
    }

    /* ================================================================== the mutable-column mirror */

    /**
     * The shadow copy of the three mutable columns, driven by the event stream and pointed at the live
     * objects only to be checked.  Non-null exactly when {@code --fidesLaneMirror} is on.
     *
     * <p>Rung 1 read those three columns straight out of the {@code RouteNode}s, which made the oracle
     * site {@code S_XLIVE_MUTABLE} compare a value with itself: the lane's reader and the oracle's
     * reader were the same method on the same object.  From here the lane reads this mirror, and the
     * oracle reads the objects, so the site says something.
     */
    private FidesMutableMirror mirror;

    /**
     * The mirror's view of "the live graph's materialised nodes", as dense ids in ascending order.
     *
     * <p>Ids rather than nodes because the mirror only ever asks about ids, and because the reverse
     * lookup -- node to id -- is two binary searches with a cached wire table, while the forward one
     * seeks the manifest file on a memo miss.  Snapshotting in the cheap direction is what makes a
     * full scan affordable.
     */
    private int[] liveIds = new int[0];

    /** Materialised nodes the manifest does not cover.  Counted, printed, and fatal at the close. */
    private long unmappedLive;
    private String firstUnmapped;

    /** {@code fixRoutes()}: nets that turned out to have multiple drivers, and connections ripped. */
    private long mirrorFixUps;
    private long mirrorFixRips;

    /**
     * Builds the mirror and fills it from the live objects.
     *
     * <p><b>When this is called is the whole of the initialisation's correctness.</b>
     * {@code initializeRouting()} has already run, which means connection setup has already called
     * {@code incrementUser} on the sink of every connection without alternative sinks
     * ({@code RWRoute.java:800}).  That batch has no event behind it -- nothing will ever tell a mirror
     * built later about it -- so it has to be part of the initial state.  Built before setup, the mirror
     * would be low by one on those sinks with no way to find out; built here, at the same instant the
     * RRG is dumped, the flat arrays the lane reads, the {@code .bin} on disk and the live objects are
     * three views of one state, and {@link FidesMutableMirror#crossCheckArtifact} says so over all N ids.
     */
    void mirrorInit() {
        this.mirror = new FidesMutableMirror(rrg.N, RouteNode.capacity, this);
        mirror.initialise();
        System.out.println("=== FIDES LANE MIRROR (initialised; the lane now reads the flat arrays) ===");
        System.out.println("mirror: N=" + rrg.N + " capacity=" + RouteNode.capacity
                + " lines=" + liveIds.length + " unmapped=" + unmappedLive
                + "  (occupancy sameNetCount historicalCongestionCost)");

        if (unmappedLive != 0) {
            throw new IllegalStateException("Initial live nodes missing from the RRG manifest: " + unmappedLive);
        }
    }

    /**
     * The ids the reference's own {@code ripUp}/{@code updateUsers} would touch for this connection's
     * current path -- the connection's own node set, not a new one.
     *
     * <p>The rule is transcribed from the reference rather than paraphrased, including the branch it
     * takes when the path's first node is an alternate sink, because that branch decides whether the
     * exclusive sink is in the set.  A connection whose path is empty (never routed, or cleared by a
     * failed attempt) has no delta, exactly as the reference's leading {@code if (rnodes.isEmpty())}
     * says.
     */
    private int[] deltaIds(Connection c) {
        if (!c.isRouted()) {
            return EMPTY_IDS;
        }
        final List<RouteNode> rnodes = c.getRnodes();
        final int size = rnodes.size();
        if (size == 0) {
            return EMPTY_IDS;
        }
        final boolean exclusive = rnodes.get(0) == c.getSinkRnode() && !c.hasAltSinks();
        return idsOf(rnodes, FidesMutableMirror.userDeltaStart(size, exclusive),
                FidesMutableMirror.userRangeEnd(size, exclusive));
    }

    /**
     * Every node of {@code net} that is a user of it -- the multiset {@code scopeTo} rebuilds from, one
     * entry per connection using the node.
     *
     * <p>This is the <em>membership</em> range, not the delta range, and the two differ by exactly the
     * exclusive sink: setup counted it ({@code RWRoute.java:800}) and no rip will ever remove it, so a
     * connection that has never been routed still contributes its sink.  Using the delta range here
     * would rebuild {@code sameNetCount} one short on every unrouted exclusive connection's sink.
     *
     * <p>The input is paths, not user maps: the rebuild is derived from the event stream, which is what
     * makes the mirror a shadow rather than a copy.
     */
    private int[] membersOf(NetWrapper net) {
        final Ids out = new Ids();
        for (Connection c : net.getConnections()) {
            if (c.isDirect()) {
                continue;
            }
            if (!c.hasAltSinks()) {
                final RouteNode sink = c.getSinkRnode();
                final int id = sink == null ? -1 : idMap.idOf(sink);
                if (id >= 0) {
                    out.add(id);
                } else {
                    countUnmapped(sink);
                }
            }
            if (c.isRouted()) {
                final List<RouteNode> rnodes = c.getRnodes();
                if (!rnodes.isEmpty()) {
                    final boolean exclusive = rnodes.get(0) == c.getSinkRnode() && !c.hasAltSinks();
                    addIds(out, rnodes, FidesMutableMirror.userDeltaStart(rnodes.size(), exclusive),
                            FidesMutableMirror.userRangeEnd(rnodes.size(), exclusive));
                }
            }
        }
        return out.take();
    }

    /**
     * Before an attempt: point the mirror's {@code same} at this connection's net, then apply the
     * decrement the reference's {@code prepareRouteConnection} is about to apply.
     *
     * <p>Called before the dispatch, so the mirror has already undone the old path while the reference
     * is still walking it.  That is safe in this direction: nothing reads the mirror between here and
     * the reference's own {@code ripUp}, and by the time the search starts both states agree -- which is
     * what lets the lane read the mirror instead of the objects mid-search.
     *
     * <p>{@code newPathIds} is the connection's delta <em>at this instant</em>, i.e. the old path; the
     * reference's {@code ripUp} does not clear {@code rnodes} (only {@code saveRouting} overwrites it),
     * so the two are reading the same list.
     */
    F4HostJournal f4Journal() {
        return f4Live == null || router.f4FloodReleased() ? null : f4Live.journal();
    }

    void mirrorBeforeAttempt(Connection c) {
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.HANDBACK)) {
        mirror.connections++;
        if (f4Live != null) return;
        /* Checked here as well as inside scopeTo(), because the argument is evaluated before the call:
         * a rebuild walks every connection of the net and maps every node of every path, and
         * connections arrive in net blocks, so paying it per connection instead of per net would be
         * 84,107 rebuilds where a few thousand are needed.  The mirror keeps its own guard anyway --
         * it must be right when called by something other than this gate. */
        final NetWrapper n = c.getNetWrapper();
        if (mirror.currentNet() != n) {
            final int[] members = membersOf(n);
            mirror.scopeTo(n, members);
            /* Phase 2: the scope change is a change to the mutable columns, and a device lane keeping
             * its own copy of them has no way to find that out -- `sameNetCount` is scoped to the
             * current net, so `scopeTo` has just zeroed every id of the net we left and raised every
             * id of the one we entered, and the mirror's list of what it zeroed is private.  Forwarded
             * with the same array the mirror was given, which is what makes this a reference and not a
             * recomputation. */
            lane.mirrorSync(FidesLane.MIRROR_SCOPE, members);
        }
        final int[] delta = deltaIds(c);
        attemptDelta = delta;
        if (delta.length != 0) {
            mirror.ripUp(delta);
            lane.mirrorSync(FidesLane.MIRROR_RIP, delta);
        }
            }
    }

    /**
     * After an attempt: apply the increment for the new path, then assert the mirror against the live
     * objects over everything this connection touched.
     *
     * <p>Gated on {@code isRouted()} because the reference's failure path is
     * {@code connection.resetRoute(); connection.setRouted(false);} -- the rip already happened and
     * nothing re-adds it, so incrementing here would invent an increment the reference never made.
     *
     * <p>The assertion covers the union of the delta the attempt undid and the membership it now holds,
     * in <b>two</b> calls per attempt, so the log's {@code assertChecks} is {@code 2 x} the number of
     * attempts that reached here -- a relation the record can be checked against rather than a bare
     * count.  Asserting on the freed nodes as well as the held ones matters: a decrement that failed to
     * reach the live object leaves its mark on a node the connection has just left, which the new
     * membership would not mention.
     */
    void mirrorAfterAttempt(Connection c) {
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.HANDBACK)) {
        if (f4Live != null) return;
        if (c.isRouted()) {
            final int[] now = deltaIds(c);
            if (now.length != 0) {
                mirror.updateUsers(now);
                lane.mirrorSync(FidesLane.MIRROR_UPDATE, now);
            }
        }
        mirror.assertNodes(attemptDelta);
        mirror.assertNodes(deltaIds(c));
        attemptDelta = EMPTY_IDS;

            }
    }

    /** The delta of the attempt in progress, held between the two hooks. */
    private int[] attemptDelta = EMPTY_IDS;

    private static final int[] EMPTY_IDS = new int[0];

    /**
     * One iteration boundary: reproduce {@code updateCost()}'s history update from the mirror's own
     * occupancy, then re-check the whole graph.
     *
     * <p>The node set is the one {@code updateCost()} itself selected -- {@code overUsedRnodes}, cleared
     * and refilled on every call -- and the factor is the one it just used.  Passing them in rather than
     * recomputing the selection is what makes the mirror's history a reproduction instead of a second
     * opinion: the rule being tested is {@code (occ - capacity) * factor}, not the choice of nodes.
     *
     * <p>The full scan afterwards is where "no delta was missed" is actually established.  A
     * per-connection assertion cannot see a delta that was never applied -- the connection that should
     * have applied it looks exactly like one that had nothing to apply -- and drift that starts inside
     * an iteration is caught here rather than 29,451 connections later.
     */
    void mirrorIterationBoundary(int iteration, Iterable<RouteNode> overUsed,
                                 float historicalCongestionFactor, long routedThisIteration) {
        if (f4Live != null) {
            if (!router.f4FloodReleased()) f4Live.markHistory(overUsed);
            return;
        }
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.HANDBACK)) {
        /* `over` counts what the reference handed in; `ids` counts what the manifest could name.  On a
         * graph the manifest covers they are equal, and the difference is the reading that says so --
         * `addId` drops a node it cannot find, so a manifest with a hole shows up here as over > ids
         * rather than as a shorter list someone would have to notice. */
        int over = 0;
        final Ids out = new Ids();
        for (RouteNode rn : overUsed) {
            over++;
            addId(out, rn);
        }
        final int[] overUsedIds = out.take();
        mirror.iterationBoundary(overUsedIds, historicalCongestionFactor);
        /* Phase 2's other iteration-boundary duty rides on this same notification: the present
         * congestion cost table is a LIVE array handed to the lane by reference, and the reference
         * refills it every iteration.  A device lane holds a copy, so the boundary is the only instant
         * at which it can be brought up to date. */
        lane.mirrorSync(FidesLane.MIRROR_HIST, overUsedIds);
        if (f4Live == null) mirror.fullScan();
        /* M6-A's witness.  The fields come from FOUR different writers on purpose: iter/over/routed
         * from RWRoute's loop, ids from this method, hist/scans/scanNodes/scanFails from the mirror's
         * own arrays, handback from the lane-attempt counter.  Any single field is satisfied by "the
         * function was called"; the identities BETWEEN them are not, which is why the gate requires
         * the identities and not the fields.  No wall clock and no `Time (s)`: this line has to be
         * reproducible so it can be paired against the iteration table and the device's snapshot. */
        iterWitnesses++;
        iterWitnessRouted += routedThisIteration;
        System.out.println("FIDES_LANE_ITER_WITNESS iter=" + iteration
                + " over=" + over
                + " ids=" + overUsedIds.length
                + " hist=" + mirror.histUpdates
                + " scans=" + mirror.fullScans
                + " scanNodes=" + mirror.fullScanNodes
                + " scanFails=" + mirror.fullScanFails
                + " handback=" + laneAttempts
                + " routed=" + routedThisIteration);
            }
    }

    /**
     * The fifth mutation site: {@code fixRoutes()} ripping a connection after every iteration has run.
     *
     * <p>Scoped like an attempt, because it is a rip of a connection that belongs to a net -- and the
     * nets {@code fixRoutes} walks are not the net the main loop left current, so the mirror has to be
     * pointed at the right one before it can apply the decrement at all.
     */
    void mirrorOutOfBandRip(NetWrapper net, Connection c) {
        mirrorFixRips++;
        if (f4Live != null) return;
        final int[] members = membersOf(net);
        mirror.scopeTo(net, members);
        lane.mirrorSync(FidesLane.MIRROR_SCOPE, members);
        final int[] delta = deltaIds(c);
        if (delta.length != 0) {
            mirror.outOfBandRip(delta);
            lane.mirrorSync(FidesLane.MIRROR_RIP, delta);
        }
    }

    /**
     * A net {@code fixRoutes()} decided to fix, whether or not it ended up ripping anything.
     *
     * <p>Phase 2 note, recorded because the plan expected otherwise: this hook notifies the lane of
     * <b>nothing</b>.  Its whole body is a counter increment -- no mirror call, therefore no change to
     * any of the three columns, therefore nothing to push.  A {@code mirrorSync} here would be a
     * notification that says a value moved when it did not, and the lane's own "ids uploaded == ids
     * changed" completeness check would have to be weakened to accommodate it.  The upload for a fixed
     * net happens in the two hooks above, which are where the change actually is.
     */
    void mirrorFixedNet() {
        mirrorFixUps++;
    }

    /**
     * The closing full scan, after {@code postRouteProcess()}: the last moment at which the mirror can
     * be compared against the objects it shadows, and the only place the out-of-band rip is answerable
     * for.  It is also the only scan whose scope is whatever net {@code fixRoutes} left current.
     */
    /**
     * The close of a whole round: what the lane covered, and the census for every attempt of it.
     *
     * <p>This is the block §13.10 promised -- the census "要等 lane 接管整轮" -- so it prints the
     * cumulative census rather than a sample of it, and it prints the two coverage counters next to
     * it.  The counters are the point: an empty whole-file diff is evidence about the code paths the
     * round actually entered, and printing {@code maxLka} and {@code maxFrameDepth} is what lets the
     * record say which branches those were instead of implying the diff covered all of them.
     *
     * <p>No verdict is issued here and nothing is thrown: a failure has already stopped the run at the
     * attempt it happened on, so reaching this method at all means every attempt passed.  The numbers
     * are printed for the record, and the criterion verdicts belong to the driver that reads them.
     */
    void closeRound() {
        /* Closed before the all-mode guard, not after it.  The footer is what licenses a reader to
         * check the stream's identities, and a single-attempt run is exactly the run whose stream is
         * read line by line -- so a close placed under the guard would leave every single-attempt
         * stream footer-less, which is the shape this class uses for "the writer died".  Closing here
         * means "the round ended", which is true in both modes; a run that dies earlier never reaches
         * this method at all (the Failure propagates out of routeIndirectConnections) and so keeps its
         * footer-less stream. */
        events.close();
        if (events.isEnabled()) {
            System.out.println("events  : " + config.getEventsFile() + " events="
                    + events.eventsWritten() + " truncated=" + (events.isTruncated() ? 1 : 0)
                    + "  (filter conn=" + (config.getEventsConn() == null ? "*" : config.getEventsConn())
                    + " from=" + config.getEventsFrom() + " to=" + config.getEventsTo()
                    + " max=" + config.getEventsMax() + "; closed with #END-OF-STREAM)");
        }
        /* M4-2alpha: which driver searched this round, and whether every attempt went through it.
         * Printed here rather than under the all-mode guard for the same reason `events.close()` is:
         * the witness is about the round, and a round is a round in both modes.  It is an unqualified
         * line, not a verdict -- the verdict is the driver's, and the driver is what would notice it
         * missing.  `driver=` is a POSITIVE word read off the counters, never off the config: a
         * mutation that quietly short-circuits W=1 back onto the serial path leaves every other number
         * in this file correct and only `attempts` at zero, so the counters are the whole detection and
         * a word derived from the knob would say "batch" while nothing was batched. */
        System.out.println("FIDES_LANE_BATCH_WITNESS driver=" + (batchCalls > 0 ? "batch" : "serial")
                + " batches=" + batchCalls + " attempts=" + batchAttempts
                + " maxBatch=" + batchMax + " runCalls=" + runCalls);
        /* The W<=1 hand-back check's witness, here for the reason the line above is here:
         * a round is a round in both modes, and this one is a measurement rather than a
         * verdict -- the verdict is the mutation table's `gate-handback-prev-dropped`
         * entry, which is the run that showed this count can refuse.  `gated_on=` says
         * whether the count was supposed to be non-zero at all, so a zero is attributable
         * instead of ambiguous: above a batch of one the pre-commit check is off by design.
         * Its independent reading is slot_checked instead; committed is the CPU hand-back count.
         * Append fields so the M5 six-configuration reader still sees checked/gated_on unchanged. */
        System.out.println("FIDES_LANE_CHAIN_WITNESS checked=" + chainChecked + " gated_on="
                + (batchWidth <= 1 ? 1 : 0) + " slot_checked=" + slotChainChecked
                + " committed=" + committedConnections);
        if (chainChecked + slotChainChecked != committedConnections) {
            throw new IllegalStateException("criterion CHAIN failed at sequence " + allSequence
                    + ": the independent replay-chain readings do not cover the committed connections: "
                    + "checked=" + chainChecked + " slot_checked=" + slotChainChecked
                    + " committed=" + committedConnections);
        }
        if (!allMode) {
            return;
        }
        final double secs = allStartedAt == 0 ? 0.0 : (System.nanoTime() - allStartedAt) / 1e9;
        System.out.println();
        System.out.println("=== FIDES LANE ROUND SUMMARY ===");
        System.out.println("lane attempts=" + laneAttempts + " A pass=" + aPass + " A fail=" + aFail
                + " first failure=" + (firstFailSequence < 0
                        ? "(none)"
                        : "attempt sequence " + firstFailSequence + " (" + firstFailDetail + ")"));
        /* The W>1 track's own line, and it is printed unconditionally rather than only when non-zero:
         * at W<=1 aDiverge must be 0 and an absent line would be indistinguishable from a zero, while
         * at W>1 the zero is the interesting case (it says the batch's staleness window changed no
         * path, which is a claim about the workload and not about the code). */
        System.out.println("criterion A (W=" + batchWidth + " track): diverged=" + aDiverge
                + " printed=" + Math.min(divergePrints, DIVERGE_PRINT_CAP)
                + " first=" + (firstDivergeSequence < 0
                        ? "(none)"
                        : "attempt sequence " + firstDivergeSequence + " (" + firstDivergeDetail + ")"));
        System.out.println(String.format("elapsed=%.1fs  rate=%.1f attempts/s", secs,
                laneAttempts / Math.max(1e-9, secs)));
        System.out.println("coverage: maxLka=" + maxLka + " maxFrameDepth=" + maxFrameDepthSeen
                + (maxLka == 0 && maxFrameDepthSeen <= 1
                        ? "  -> the lookahead frame stack was NEVER ENTERED in this round: the Laguna"
                          + " branches are therefore not part of what the golden diff proves"
                        : "  -> the lookahead frame stack WAS entered, so the Laguna branches are"
                          + " covered by this round"));
        /* G3/G4's decomposition, printed even when every term is zero: the two questions the diff
         * cannot answer are "was the lookahead half of POP ever non-zero" and "which of PUSH's three
         * terms did this round's lines come from", and an unstated zero is indistinguishable from an
         * unfilled field.  The terms are tied to the frozen SUMMARY by the driver:
         *   SUMMARY nodesPushed - nodesPopped == sumAbandoned + sumLeftover,
         * which is the same identity the per-attempt census checks, aggregated over the whole round. */
        System.out.println("G3 lka total: sumLka=" + sumLka
                + "  (the lookahead half of POP; the other half is the polls)");
        System.out.println("G4 push split: sumAbandoned=" + sumAbandoned
                + " (attempts>0: " + attemptsWithAbandoned + ")"
                + " sumLeftover=" + sumLeftover + " (attempts>0: " + attemptsWithLeftover + ")"
                + "  [PUSH - POP = abandoned + leftover]");
        if (sumAbandoned == 0) {
            System.out.println("  -> the early-termination branch that adds the cleared queue to PUSH was"
                    + " NEVER ENTERED in this round: its term is not part of what the diff proves");
        }
        if (sumLeftover == 0) {
            System.out.println("  -> the queue left at the measure point (RWRoute:1899's queue.size()) was"
                    + " never non-zero in this round");
        }
        printColumnCoverage();
        printCensus();
        if (mirror != null) {
            printMirror();
            /* G5's second witness for shorts=0.  The SUMMARY's shorts is counted from the live rnodes
             * (RWRoute.printRoutingStatistics); this counts the same predicate over the mirror's own
             * occupancy column -- flat memory maintained by the event stream and drift-checked against
             * the objects at every attempt.  Two structures, one predicate: RouteNode.capacity is 1 for
             * every rnode of this device (the shorts comment states it), so isOverUsed() is occupancy>=2
             * on both sides.  It is a witness and not a second opinion about a different question. */
            long mirrorShorts = 0;
            for (int id = 0, n = rrg.occupancy.length; id < n; id++) {
                if (mirror.occupancy(id) >= 2) {
                    mirrorShorts++;
                }
            }
            System.out.println("shorts from mirror occupancy = " + mirrorShorts
                    + "  (" + rrg.occupancy.length + " ids; the SUMMARY's shorts= counts the same"
                    + " predicate over the live rnodes)");
        }
    }

    void mirrorClosingScan(long connectionsRouted) throws FidesLane.Failure {
        if (f4Live == null) mirror.fullScan();
        printMirror();
        System.out.println("mirror fixRoutes: fixUps=" + mirrorFixUps + " fixRips=" + mirrorFixRips
                + " (mutating sites 4 and 5 of the five that write the mutable columns; fixRips=0 is a"
                + " measurement about this workload, not a missing hook)");
        System.out.println("mirror vs live  : unmapped=" + unmappedLive + " materialised rnodes"
                + (unmappedLive == 0 ? "" : "  e.g. " + firstUnmapped));
        /* The router's counter is advanced by both dispatch paths, so the two numbers are independent
         * statements about the same event and the identity between them is checkable.  A mirror that
         * missed an attempt would show up here even if every assertion it did make passed. */
        System.out.println("mirror attempts : mirror=" + mirror.connections
                + " connectionsRouted=" + connectionsRouted + " -> "
                + (mirror.connections == connectionsRouted ? "same" : "*** DIFFERENT ***"));
        /* M6-A's closing reading.  `scans` here counts the closing fullScan() too, which is why the
         * gate requires `scans == iterations + 1` on a whole-round arm and not `== iterations`. */
        System.out.println("FIDES_LANE_ITER_WITNESS_END iters=" + iterWitnesses
                + " routed=" + iterWitnessRouted + " hist=" + mirror.histUpdates
                + " scans=" + mirror.fullScans + " scanFails=" + mirror.fullScanFails);
        mirror.verifyClean();
        if (unmappedLive != 0) {
            throw new FidesLane.Failure(unmappedLive + " materialised rnode(s) are not in the manifest"
                    + " (e.g. " + firstUnmapped + "): the flat graph is not the live graph, so every"
                    + " full scan this run reported was missing them");
        }
    }

    private void printMirror() {
        System.out.println("=== FIDES LANE MIRROR (three mutable columns, maintained by the event stream) ===");
        System.out.print(mirror.report());
    }

    /** One note per unmapped node, so a systematic hole names itself once instead of 700,000 times. */
    private void countUnmapped(RouteNode rn) {
        unmappedLive++;
        if (firstUnmapped == null && rn != null) {
            firstUnmapped = rn.getTile().getName() + "/" + rn.getWireName();
        }
    }

    private void addId(Ids out, RouteNode rn) {
        final int id = rn == null ? -1 : idMap.idOf(rn);
        if (id < 0) {
            countUnmapped(rn);
        } else {
            out.add(id);
        }
    }

    private void addIds(Ids out, List<RouteNode> nodes, int from, int to) {
        for (int i = from; i < to; i++) {
            addId(out, nodes.get(i));
        }
    }

    private int[] idsOf(List<RouteNode> nodes, int from, int to) {
        final Ids out = new Ids();
        addIds(out, nodes, from, to);
        return out.take();
    }

    /** A tiny growable int list: the id sets here are ≤ a path long, and allocating an ArrayList of
     * {@code Integer} 250,000 times is the kind of thing that turns a 43 s run into a 4 minute one. */
    private static final class Ids {
        private int[] a = new int[64];
        private int n;

        void add(int id) {
            if (n == a.length) {
                a = java.util.Arrays.copyOf(a, a.length * 2);
            }
            a[n++] = id;
        }

        int[] take() {
            final int[] out = java.util.Arrays.copyOf(a, n);
            n = 0;
            return out;
        }
    }

    /* ================================================================== the live view the mirror checks against */

    /**
     * {@link FidesMutableMirror.LiveView} over the real graph: the "live object" half of every mirror
     * comparison.
     *
     * <p>{@link #liveCount()} snapshots the materialised nodes into sorted parallel arrays, and
     * {@link #liveId(int)} then answers from that snapshot.  The snapshot exists for the walk's
     * <em>order</em>: {@code LiveView} promises {@code liveId} is strictly increasing, so the artifact
     * cross-check can walk the id space with a cursor instead of searching per id, while
     * {@code nodesMap} enumerates in tile-then-wire order, which is not id order.  Both directions of
     * {@code FidesRrgIdMap} are in-RAM ({@code rnode -> id} from the node's own coordinates,
     * {@code id -> rnode} from the dense wire column), so building the snapshot is cheap and resolving
     * an id is too -- the sort is for the contract, not to avoid I/O.
     *
     * <p>A materialised node the manifest does not cover is counted once and dropped from the snapshot
     * rather than thrown on: the run should reach its other numbers, and
     * {@code mirrorClosingScan} turns the count into a failure at the point where the claim is made.
     */
    @Override
    public int liveCount() {
        final int count = graph.numNodes();
        final int[] ids = new int[count];
        int n = 0;
        for (RouteNode[] column : graph.nodesMap) {
            if (column == null) {
                continue;
            }
            for (RouteNode rn : column) {
                if (rn == null) {
                    continue;
                }
                final int id = idMap.idOf(rn);
                if (id < 0) {
                    countUnmapped(rn);
                    continue;
                }
                ids[n++] = id;
            }
        }
        /* Sorted by id, because LiveView's contract says liveId is strictly increasing: the mirror's
         * artifact cross-check walks the id space with a cursor rather than searching per id.  No two
         * materialised nodes share an id -- nodesMap holds one node per (tile address, wire index) and
         * idOf() is injective over that pair. */
        liveIds = java.util.Arrays.copyOf(ids, n);
        java.util.Arrays.sort(liveIds);
        return n;
    }

    @Override
    public int liveId(int index) {
        return liveIds[index];
    }

    @Override
    public int liveOccupancy(int id) {
        final RouteNode rn = liveNodeFor(id);
        occQueries++;
        final int v = rn == null ? 0 : rn.getOccupancy();
        if (v != 0) {
            occNonDefault++;
        }
        return v;
    }

    @Override
    public int liveSameNetCount(int id, Object forNet) {
        final RouteNode rn = liveNodeFor(id);
        sameNetQueries++;
        final int v = rn == null ? 0 : rn.countConnectionsOfUser((NetWrapper) forNet);
        if (v != 0) {
            sameNetNonDefault++;
        }
        return v;
    }

    @Override
    public float liveHistoricalCongestionCost(int id) {
        final RouteNode rn = liveNodeFor(id);
        histQueries++;
        /* RouteNode's constructor sets historicalCongestionCost = initialHistoricalCongestionCost
         * (RouteNode.java:99), and NetWrapper init sets it to 1f -- so a node that does not exist yet
         * reads 1.0f, the same value the reference would read from the node it had just created. */
        final float v = rn == null ? 1.0f : rn.getHistoricalCongestionCost();
        if (v != 1.0f) {
            histNonDefault++;
        }
        return v;
    }

    /** Binary search of the snapshot: the whole reason {@link #liveCount()} exists. */
    private RouteNode liveNodeFor(int id) {
        final RouteNode rn = liveNodeOrNull(id);
        if (rn == null) {
            liveNodeAbsent++;
        } else {
            liveNodePresent++;
        }
        return rn;
    }

    /* ================================================================== MutableColumns */

    /**
     * What the lane reads in place of the objects.  Under {@code --fidesLaneMirror} these are the flat
     * arrays; without it they are still the live objects, because that is what rung 1 was and a run that
     * asks for neither should not be silently upgraded.
     *
     * <p>The three counters alongside are the criterion-F evidence: rung 1's {@code historicalCost
     * queries=667 nonDefault=0} on this workload's first connection is why the acceptance run is at
     * attempt 29,452, and they have to keep being printed for that to stay checkable.
     */
    @Override
    public int occupancy(int id) {
        if (mirror == null) {
            /* Delegated rather than re-spelled: the two paths are the rung-1 and rung-2 readings of
             * one column, and a copy of the live reading here would be a second place for the
             * rung-1 mutation controls to stop applying to (the harness's `old` strings must occur
             * exactly once, and a duplicate body would make the entry ambiguous). */
            return liveOccupancy(id);
        }
        occQueries++;
        final int v = mirror.occupancy(id);
        if (v != 0) {
            occNonDefault++;
        }
        return v;
    }

    @Override
    public int sameNetCount(int id) {
        if (mirror == null) {
            return liveSameNetCount(id, net);
        }
        sameNetQueries++;
        final int v = mirror.sameNetCount(id);
        if (v != 0) {
            sameNetNonDefault++;
        }
        return v;
    }

    @Override
    public float historicalCongestionCost(int id) {
        if (mirror == null) {
            return liveHistoricalCongestionCost(id);
        }
        histQueries++;
        final float v = mirror.historicalCongestionCost(id);
        if (v != 1.0f) {
            histNonDefault++;
        }
        return v;
    }

    /**
     * The live rnode for {@code id}, or null when the lazy live graph has not materialised it.
     *
     * <p>O(1) in RAM: the manifest's wire column is dense by id in {@link FidesRrgIdMap}, so this is an
     * array read plus a {@code nodesMap} index.  It is called three times per id per comparison
     * (once per column, in {@code FidesMutableMirror#compare}) and once per materialised node per full
     * scan, and both of those rates are only affordable because the column is dense -- see
     * {@code FidesRrgIdMap#wireOf} for the 648 M-read measurement that made it so.
     */
    @Override
    public RouteNode liveNodeOrNull(int id) {
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            liveNodeAbsent++;
        } else {
            liveNodePresent++;
        }
        return rn;
    }

    /* ================================================================== LiveMirror */

    @Override
    public void pushed(int id, int parentId, float newPartialPathCost, float newTotalPathCost, int seq) {
        mirrorPushes++;
        if (tracePushes < TRACE_LIMIT) {
            tracePushes++;
            /* Shape copied from FidesEventStream.event so that a trace line and an event-stream line
             * read the same way; `key` is the total path cost (what the heap orders by) and `up` the
             * parent, both of which FidesLaneCuda carries in records[at+2] and records[at+4]. */
            System.out.println("TRACE PUSH seq=" + seq + " node=" + id + " key="
                    + String.format("%08x", Float.floatToRawIntBits(newTotalPathCost))
                    + " up=" + parentId + " part="
                    + String.format("%08x", Float.floatToRawIntBits(newPartialPathCost)));
        }
        final RouteNode child = idMap.nodeAt(graph, id);
        if (child == null) {
            /* Cannot happen for a pushed node: every pushed node was examined as a child, and the
             * reference examines only children its own getChildren() has just created.  If it did
             * happen the hand-back would be silently incomplete -- and until 2026-10-05 this
             * comment was the whole of the claim: the commit rewrote the chain from the lane's
             * path, so a dropped push left no trace in any criterion.  It is refused now, at the
             * head of {@link #rewritePrevFromPath}, which reads this replay's chain against the
             * lane's path before the write; that check is what makes this sentence load-bearing
             * rather than aspirational. */
            throw new IllegalStateException("push(" + id + ") has no live RouteNode: the lane pushed a"
                    + " node the live graph does not have, so prev cannot be written back along the"
                    + " path and saveRouting would backtrack through stale pointers");
        }
        if (parentId >= 0) {
            final RouteNode parent = idMap.nodeAt(graph, parentId);
            if (parent == null) {
                throw new IllegalStateException("push(" + id + ") has parent " + parentId
                        + " with no live RouteNode");
            }
            child.setPrev(parent);
        }
        child.setUpstreamPathCost(newPartialPathCost);
        child.setLowerBoundTotalPathCost(newTotalPathCost);
        child.setVisited(seq);
    }

    @Override
    public void materialisedChildren(int id) {
        materialisedCalls++;
        if (traceExpands < TRACE_LIMIT) {
            traceExpands++;
            System.out.println("TRACE EXPAND node=" + id);
        }
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            /* A pushed node whose parent's expansion did not create it: either the mirror was wired
             * after the push, or the CSR slice listed a child the live isExcluded rejected.  Both are
             * bugs in this gate rather than in the lane, and both would make the hand-back silently
             * incomplete. */
            throw new IllegalStateException("expand(" + id + ") has no live RouteNode: the lane is"
                    + " expanding a node its parent's expansion did not create, so the live graph has"
                    + " diverged from the CSR the lane is reading");
        }
        final int before = graph.numNodes();
        /* The reference's own call, with the reference's own side effects: it decides isExcluded for
         * every downhill wire, creates the survivors, caches the array and bills the create time. */
        rn.getChildren(graph);
        materialisedCreated += graph.numNodes() - before;
    }

    /* ================================================================== LiveOracle */

    @Override
    public int target(int id, boolean laneAnswer) {
        final RouteNode rn = liveNodeOrNull(id);
        if (rn == null) {
            return tally(UNVERIFIABLE);
        }
        return tally(rn.isTarget() == laneAnswer ? AGREE : DISAGREE);
    }

    @Override
    public int willOverUse(int id, boolean laneAnswer) {
        final RouteNode rn = liveNodeOrNull(id);
        if (rn == null) {
            return tally(UNVERIFIABLE);
        }
        return tally(rn.willOverUse(net) == laneAnswer ? AGREE : DISAGREE);
    }

    @Override
    public int graphAccessible(int childId, int parentId, boolean laneAnswer) {
        final RouteNode child = liveNodeOrNull(childId);
        final RouteNode parent = liveNodeOrNull(parentId);
        if (child == null || parent == null) {
            return tally(UNVERIFIABLE);
        }
        /* The reference's own method, on the reference's own objects.  This is the check that makes
         * the accessibility reduction a measurement: it reads parent.getPrev(), so it also re-tests
         * the mirror's write-back at every call. */
        return tally(graph.isAccessible(child, parent, connection) == laneAnswer ? AGREE : DISAGREE);
    }

    @Override
    public int sinkAccessible(int childId, boolean laneAnswer) {
        final RouteNode child = liveNodeOrNull(childId);
        if (child == null) {
            return tally(UNVERIFIABLE);
        }
        return tally(router.isAccessibleSink(child, connection) == laneAnswer ? AGREE : DISAGREE);
    }

    @Override
    public int mutableColumns(int id, int laneOcc, int laneSameNet, float laneHist) {
        mutableChecks++;
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            /* No live object: the lane answered with a fresh node's defaults.  That is only correct
             * if it really did, and "the reference would have created the node first" is the argument
             * that makes the default the right answer -- so the check is that the answer is the
             * default, not that it is unchecked. */
            final boolean ok = laneOcc == 0 && laneSameNet == 0 && laneHist == 1.0f;
            return tally(ok ? AGREE : DISAGREE);
        }
        final boolean ok = laneOcc == rn.getOccupancy()
                && laneSameNet == rn.countConnectionsOfUser(net)
                && laneHist == rn.getHistoricalCongestionCost();
        return tally(ok ? AGREE : DISAGREE);
    }

    @Override
    public int staticColumns(int id) {
        staticChecks++;
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            return tally(UNVERIFIABLE);
        }
        /* RouteNode.getBaseCost() asserts the type is not EXCLUSIVE_SOURCE, and the node whose
         * expansion is starting on an attempt's first expansion is exactly that -- the source.  The
         * assert is a precondition about use, not about the value: the reference calls the accessor
         * on children, and a child is never an EXCLUSIVE_SOURCE.  So the column is compared on the
         * types the accessor admits, the refusals are counted rather than waved through, and the
         * comparison the lane actually rests on is made by childBaseCost(), on the nodes it reads
         * the column for.
         *
         * This is not hypothetical bookkeeping: calling the accessor anyway does not return a wrong
         * number, it throws, and with -ea on it takes the whole run down before a single golden line
         * is written.  Criterion D2 is what surfaced it. */
        final boolean baseCostAdmitted = rn.getType() != RouteNodeType.EXCLUSIVE_SOURCE;
        if (!baseCostAdmitted) {
            staticBaseCostSkipped++;
        }
        final boolean ok = rrg.nodeType[id] == rn.getType().ordinal()
                && rrg.intentCode[id] == rn.getIntentCode().ordinal()
                && rrg.tileId[id] == rn.getTile().getUniqueAddress()
                && rrg.endTileX[id] == rn.getEndTileXCoordinate()
                && rrg.length[id] == rn.getLength()
                && (!baseCostAdmitted || rrg.baseCost[id] == rn.getBaseCost());
        if (!ok) {
            return tally(DISAGREE);
        }
        /* endTileYRaw has no accessor: getEndTileYCoordinate() applies the reverse-SLL rule, which
         * needs prev.  So the column is compared exactly where the two must agree, and the exempt case
         * is counted rather than waved through. */
        if (rn.getType() != RouteNodeType.SUPER_LONG_LINE) {
            return tally(rrg.endTileYRaw[id] == rn.getEndTileYCoordinate() ? AGREE : DISAGREE);
        }
        staticSllYUnverifiable++;
        return tally(UNVERIFIABLE);
    }

    @Override
    public int childBaseCost(int id) {
        childBaseCostChecks++;
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            return tally(UNVERIFIABLE);
        }
        /* No laneValue parameter: for this column the lane's value IS the .bin column, copied with no
         * arithmetic in between, so comparing the two would be comparing the column with itself and
         * would inflate the check's apparent reach.  The claim worth testing is the one the oracle
         * exists for -- that the column still describes the live object. */
        return tally(rrg.baseCost[id] == rn.getBaseCost() ? AGREE : DISAGREE);
    }

    @Override
    public int childrenSlice(int id) {
        edgeChecks++;
        final RouteNode rn = idMap.nodeAt(graph, id);
        if (rn == null) {
            return tally(UNVERIFIABLE);
        }
        /* RouteNode.children is the cache the mirror's materialisedChildren() call -- which the lane
         * makes immediately before this one -- has just filled.  Reading the field rather than calling
         * getChildren() is what keeps this a read: a list this call had just created could not
         * disagree with the slice it is being compared against. */
        final RouteNode[] live = rn.children;
        if (live == null) {
            edgeUnverifiable++;
            return tally(UNVERIFIABLE);
        }
        final int from = rrg.offset[id];
        final int to = rrg.offset[id + 1];
        int mismatch = -1;
        boolean same = to - from == live.length;
        if (same) {
            for (int k = 0; k < live.length; k++) {
                if (rrg.edge[from + k] != idMap.idOf(live[k])) {
                    mismatch = k;
                    same = false;
                    break;
                }
            }
        } else {
            mismatch = Math.min(to - from, live.length);
        }
        if (same) {
            return tally(AGREE);
        }
        /* The one disagreement that is worth more than the count it contributes: it says the CSR was
         * dumped at a moment when the graph had a different set of created nodes than the moment the
         * lane is walking it, which is the single assumption the flat-CSR approach cannot do without.
         * So print what actually differs, with a cap -- a run where every node disagrees would drown
         * the log rather than inform it. */
        if (edgeMismatchPrinted < EDGE_MISMATCH_LIMIT) {
            edgeMismatchPrinted++;
            System.out.println("*** EDGE SLICE MISMATCH *** parent id=" + id + " ("
                    + manifestPathName(id) + ")  .bin=" + (to - from) + " children, live="
                    + live.length + " children; first difference at index " + mismatch);
            final int lo = Math.max(0, mismatch - 2);
            for (int k = lo; k < Math.min(Math.max(to - from, live.length), mismatch + 3); k++) {
                final String want = k < to - from ? (rrg.edge[from + k] + " " + manifestPathName(rrg.edge[from + k])) : "-";
                final String got = k < live.length ? (idMap.idOf(live[k]) + " " + liveName(live[k])) : "-";
                System.out.println("      [" + k + "] .bin " + want + "   |   live " + got);
            }
            if (edgeMismatchPrinted == EDGE_MISMATCH_LIMIT) {
                System.out.println("      (further mismatches not printed)");
            }
        }
        return tally(DISAGREE);
    }

    /** A live node's own name, for the mismatch dump.  No id: the point is to show what it is. */
    private static String liveName(RouteNode rn) {
        return rn == null ? "null" : (rn.getTile() + "/" + rn.getWireName());
    }

    private int tally(int verdict) {
        if (verdict == AGREE) {
            oracleAgree++;
        } else if (verdict == DISAGREE) {
            oracleDisagree++;
            System.out.println("*** ORACLE DISAGREE *** the lane and the reference router differ; the"
                    + " census site named in the next FAILED line is where it was caught");
        } else {
            /* Counted apart from a disagreement on purpose: a check that could not be performed is
             * not a check that passed, and it is also not evidence of a bug. */
            oracleUnverifiable++;
        }
        return verdict;
    }
}
