/*
 *
 * Copyright (c) 2021 Ghent University.
 * Copyright (c) 2022-2025, Advanced Micro Devices, Inc.
 * All rights reserved.
 *
 * Author: Yun Zhou, Ghent University.
 *
 * This file is part of RapidWright.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

package com.xilinx.rapidwright.rwroute;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Map.Entry;
import java.util.PriorityQueue;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Function;

import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.DesignTools;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.NetTools;
import com.xilinx.rapidwright.design.NetType;
import com.xilinx.rapidwright.design.SiteInst;
import com.xilinx.rapidwright.design.SitePinInst;
import com.xilinx.rapidwright.design.tools.LUTTools;
import com.xilinx.rapidwright.device.BEL;
import com.xilinx.rapidwright.device.ClockRegion;
import com.xilinx.rapidwright.device.IntentCode;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.PIP;
import com.xilinx.rapidwright.device.Part;
import com.xilinx.rapidwright.device.Series;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.SitePin;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.TileTypeEnum;
import com.xilinx.rapidwright.edif.EDIFHierNet;
import com.xilinx.rapidwright.interchange.Interchange;
import com.xilinx.rapidwright.router.RouteThruHelper;
import com.xilinx.rapidwright.tests.CodePerfTracker;
import com.xilinx.rapidwright.timing.ClkRouteTiming;
import com.xilinx.rapidwright.timing.TimingManager;
import com.xilinx.rapidwright.timing.TimingVertex;
import com.xilinx.rapidwright.timing.delayestimator.DelayEstimatorBase;
import com.xilinx.rapidwright.timing.delayestimator.InterconnectInfo;
import com.xilinx.rapidwright.util.MessageGenerator;
import com.xilinx.rapidwright.util.Pair;
import com.xilinx.rapidwright.util.RuntimeTracker;
import com.xilinx.rapidwright.util.RuntimeTrackerTree;
import com.xilinx.rapidwright.util.Utils;

/**
 * RWRoute class provides the main methods for routing a design.
 * Creating a RWRoute Object needs a {@link Design} Object and a {@link RWRouteConfig} Object.
 */
public class RWRoute {
    /** The design to route */
    protected Design design;
    /** Created NetWrappers */
    protected Map<Net,NetWrapper> nets;
    /** A list of indirect connections that will go through iterative routing */
    protected List<Connection> indirectConnections;
    /** A list of direct connections that are easily routed through dedicated resources */
    private List<Connection> directConnections;
    /** Sorted indirect connections */
    protected List<Connection> sortedIndirectConnections;
    /** A list of global clock nets */
    protected List<Net> clkNets;
    /** Static nets */
    protected Map<Net, List<SitePinInst>> staticNetAndRoutingTargets;
    /** Several integers to indicate the netlist info */
    protected int numPreservedRoutableNets;
    protected int numPreservedClks;
    protected int numPreservedStaticNets;
    protected int numPreservedWire;
    private int numWireNetsToRoute;
    private int numConnectionsToRoute;
    protected int numNotNeedingRoutingNets;
    private int numUnrecognizedNets;

    /** A {@link RWRouteConfig} instance consisting of a list of routing parameters */
    protected RWRouteConfig config;
    /** The present congestion cost factor */
    protected float presentCongestionFactor;
    /** The historical congestion cost factor */
    private float historicalCongestionFactor;
    /** Wirelength-driven weighting factor */
    private float wlWeight;
    /** 1 - wlWeight */
    private float oneMinusWlWeight;
    /** Timing-driven weighting factor */
    private float timingWeight;
    /** 1 - timingWeight */
    private float oneMinusTimingWeight;
    /** Flag for whether LUT pin swaps are to be considered */
    private boolean lutPinSwapping;

    /** Flag for use of Hybrid Updating Strategy (HUS) */
    private boolean hus;
    /** Flag (computed at end of iteration 1) to indicate design is congested enough to consider HUS */
    private boolean husInitialCongested;

    /** The current routing iteration */
    protected int routeIteration;
    /** Timers to store runtime of different phases */
    protected RuntimeTrackerTree routerTimer;
    protected RuntimeTracker rnodesTimer;
    private RuntimeTracker updateTimingTimer;
    private RuntimeTracker updateCongestionCosts;
    /** An instantiation of RouteThruHelper to avoid route-thrus in the routing resource graph */
    protected RouteThruHelper routethruHelper;

    /** A set of indices of overused rondes */
    private Set<RouteNode> overUsedRnodes;
    /** Class encapsulating the routing resource graph */
    protected RouteNodeGraph routingGraph;
    /** FIDES M3-step3 lane gate; built on the first lane connection, null until then. */
    private FidesLaneGate fidesLaneGate;
    /** Count of rnodes created in the current routing iteration */
    protected long rnodesCreatedThisIteration;
    /** State necessary to route the included connection */
    private ConnectionState connectionState;

    /** Total wirelength of the routed design */
    private int totalWL;
    /** Total used INT tile nodes */
    private long totalINTNodes;
    /** A map from node types to the node usage of the types */
    private Map<IntentCode, Long> nodeTypeUsage;
    /** A map from node types to the total wirelength of used nodes of the types */
    private Map<IntentCode, Long> nodeTypeLength;
    /** The total number of connections that are routed */
    private final AtomicInteger connectionsRouted;
    /** The total number of connections routed in an iteration */
    private final AtomicInteger connectionsRoutedThisIteration;
    /** Total number of nodes pushed/popped from the queue */
    private final AtomicLong nodesPushed;
    private final AtomicLong nodesPopped;
    /** Writer for the M2 golden reference dump (per-connection path + summary), null when disabled */
    private java.io.PrintWriter goldenWriter = null;
    /**
     * Connections this run handed to the serial router instead of searching itself, i.e. the
     * fail-closed decline of the GPU path.
     *
     * <p>Counted here and not in {@link FidesLaneGate} because the decline happens at the dispatch
     * below, which in single-connection mode runs for every connection of a whole round -- and the gate
     * is built lazily (it maps 6.5 GiB of artifact and 4.13 GiB of manifest index), so a round whose
     * lane takes one connection must not pay for building it merely to count.  Every increment is on
     * the path that routes the connection with the untouched reference implementation, so the counter
     * and the degradation are the same event.
     *
     * <p>It is the number the Phase-2 fail-closed switch is about: <em>zero</em> is what makes
     * "the lane took the whole round" true in the strong sense, and anything else means some
     * connection's line was written by the serial router.  What it counts is exactly the
     * dispatch-level decline; it is NOT a count of mid-search abandonments, because there is no such
     * path (a refusal inside the search stops the run).  A counter that claimed to include them would
     * be a number with no writer.
     *
     * <p>Deliberately not part of the golden file: the SUMMARY line is byte-frozen against the M2
     * golden trace, so this is a stdout line of its own, printed unconditionally -- see
     * {@link #printRoutingStatistics()}.
     */
    private long fallbackAttempts;

    /**
     * M5: the reference's backtracking, counted instead of assumed.
     *
     * <p>{@code saveRouting} has two faces and both are counted here.  The <em>loud</em> one is the
     * guard that throws {@code "Unexpected rnode to backtrack from"}; the <em>silent</em> one is the
     * line immediately past that guard, {@code rnode = prevRouting.get(0)}, which with a prior routing
     * present quietly re-points at the OLD path's sink and carries on -- so a route that is not the one
     * the search just found is committed and nothing in the run says so.  That branch had no counter
     * and no verdict before M5.
     *
     * <p>The denominator is part of the claim, not decoration.  A zero from a method that ran zero
     * times and a zero from a method that ran a million times and never took the branch are different
     * facts, which is why {@code backtrackCalls} is counted alongside the two numerators and why the
     * reader declares the leg VACUOUS when the denominator is zero rather than letting a reader assume
     * coverage.  See {@link #printRoutingStatistics()} for the line these three are printed on, and the
     * {@code FIDES_M5_FORCE_SILENT_BACKTRACK} knob for the witness run that makes the silent numerator
     * non-zero on purpose.
     *
     * <p>Not part of the golden file, for the same reason {@link #fallbackAttempts} is not: the SUMMARY
     * line is byte-frozen against the M2 golden trace, so these are printed as their own stdout line.
     */
    private long backtrackCalls;
    private long unexpectedBacktracks;
    private long silentBacktracks;

    /**
     * One-shot latch for {@code FIDES_M5_FORCE_SILENT_BACKTRACK}: true once the knob has fired, so a
     * witness run plants the condition exactly once and the rest of the round is the round it would
     * otherwise have been.  Never set by the gate -- see the knob in {@link #saveRouting}.
     */
    private boolean silentBacktrackForced;

    /** The maximum criticality constraint of connection */
    private static final float MAX_CRITICALITY = 0.99f;
    /** Threshold to select auto-tuned routing profile */
    private static final long AUTO_PROFILE_SIZE_THRESHOLD = 400_000L;
    /** Small-design profile values */
    private static final short SMALL_PROFILE_BB_EXT_X = 2;
    private static final short SMALL_PROFILE_BB_EXT_Y = 10;
    private static final float SMALL_PROFILE_INITIAL_PRESENT_CF = 1f;
    /** Large-design profile values */
    private static final short LARGE_PROFILE_BB_EXT_X = 12;
    private static final short LARGE_PROFILE_BB_EXT_Y = 60;
    private static final float LARGE_PROFILE_INITIAL_PRESENT_CF = 4f;
    /** The minimum criticality of connections that should be re-routed, updated after each iteration */
    protected float minRerouteCriticality;
    /** The list of critical connections */
    private List<Connection> criticalConnections;
    /** A {@link TimingManager} instance to use that handles timing related tasks */
    protected TimingManager timingManager;
    /** A map from nodes to delay values, used for timing update after fixing routes */
    private Map<Node, Float> nodesDelays;
    /** The maximum delay and associated timing vertex */
    private Pair<Float, TimingVertex> maxDelayAndTimingVertex;

    /** A map storing routes from CLK_OUT to different INT tiles that connect to sink pins of a global clock net */
    protected Map<String, List<String>> routesToSinkINTTiles;

    public static final EnumSet<Series> SUPPORTED_SERIES = EnumSet.of(
                Series.UltraScale,
                Series.UltraScalePlus,
                Series.Versal);

    /** For connections that require SLR crossing(s), snap back to the previous Laguna column if the (horizontal) detour
     *  is no more than this number of tiles */
    protected int maxDetourToSnapBackToPrevLagunaColumn = 4;

    public RWRoute(Design design, RWRouteConfig config) {
        this.design = design;
        this.config = config;
        connectionsRouted = new AtomicInteger();
        connectionsRoutedThisIteration = new AtomicInteger();
        nodesPushed = new AtomicLong();
        nodesPopped = new AtomicLong();

        if (design.getSeries() == Series.Versal) {
            if (config.isLutPinSwapping()) {
                throw new RuntimeException("ERROR: '--lutPinSwapping' not yet supported on Versal.");
            }
            if (config.isLutRoutethru()) {
                throw new RuntimeException("ERROR: '--lutRoutethru' not yet supported on Versal.");
            }
        }
    }

    protected static String getUnsupportedSeriesMessage(Part part) {
        return "ERROR: RWRoute does not support routing the " + part.getName() + " from the " 
                + part.getSeries() + " series. Please re-target the design to a part from a "
                + "supported series: " + SUPPORTED_SERIES;
    }

    /**
     * Pre-process the design to ensure that only the physical {@link Net}-s corresponding to
     * the parent logical {@link EDIFHierNet} exists, and that such {@link Net}-s contain
     * all necessary {@link SitePinInst} objects.
     * @param design Design to preprocess
     */
    public static void preprocess(Design design) {
        Series series = design.getPart().getSeries();
        if (!SUPPORTED_SERIES.contains(series)) {
            throw new RuntimeException(getUnsupportedSeriesMessage(design.getPart()));
        }

        long tPre0 = System.nanoTime();
        DesignTools.makePhysNetNamesConsistent(design);
        long tPre1 = System.nanoTime();
        DesignTools.createPossiblePinsToStaticNets(design);
        long tPre2 = System.nanoTime();
        DesignTools.createMissingSitePinInsts(design);
        long tPre3 = System.nanoTime();
        System.out.printf("F4_V71_PREPROC makePhys=%.2fs staticPins=%.2fs missingPins=%.2fs%n",
                (tPre1 - tPre0) / 1e9, (tPre2 - tPre1) / 1e9, (tPre3 - tPre2) / 1e9);
        if (series == Series.Versal) {
            DesignTools.updateVersalXPHYPinsForDMC(design);
        }
    }

    protected void preprocess() {
        preprocess(design);
    }

    protected void initialize() {
        routerTimer = new RuntimeTrackerTree("Route design", config.isVerbose());
        rnodesTimer = routerTimer.createStandAloneRuntimeTracker("rnodes creation");
        updateTimingTimer = routerTimer.createStandAloneRuntimeTracker("update timing");
        updateCongestionCosts = routerTimer.createStandAloneRuntimeTracker("update congestion costs");
        routerTimer.createRuntimeTracker("Initialization", routerTimer.getRootRuntimeTracker()).start();

        minRerouteCriticality = config.getMinRerouteCriticality();
        criticalConnections = new ArrayList<>();

        connectionState = new ConnectionState();
        routingGraph = createRouteNodeGraph();
        if (config.isTimingDriven()) {
            nodesDelays = new HashMap<>();
        }
        routethruHelper = new RouteThruHelper(design.getDevice());
        presentCongestionFactor = config.getInitialPresentCongestionFactor();
        lutPinSwapping = config.isLutPinSwapping();

        routerTimer.createRuntimeTracker("determine route targets", "Initialization").start();
        determineRoutingTargets();
        ensureSinkRoutability();
        autoTuneRoutingProfileByDesignSize();
        F2Prewarm.initializeAsync(indirectConnections, directConnections);
        if (System.getProperty("fides.ppt.f4.features") != null) {
            if (!config.isUseBoundingBox()) {
                throw new IllegalStateException("GPU-only F1 requires bounding boxes");
            }
            long started = System.nanoTime();
            f4Corridor = new F1Corridor(design);
            System.out.printf("F1_PPT_INITIAL_OCCUPANCY ns=%d%n", System.nanoTime() - started);
        }
        routerTimer.getRuntimeTracker("determine route targets").stop();

        if (config.isTimingDriven()) {
            ClkRouteTiming clkTiming = createClkTimingData(config);
            routesToSinkINTTiles = clkTiming == null? null : clkTiming.getRoutesToSinkINTTiles();
            Collection<Net> timingNets = getTimingNets();
            timingManager = createTimingManager(clkTiming, timingNets);
            timingManager.setTimingEdgesOfConnections(indirectConnections);
        }

        sortedIndirectConnections = new ArrayList<>(indirectConnections.size());
        connectionsRouted.set(0);
        connectionsRoutedThisIteration.set(0);
        nodesPushed.set(0);
        nodesPopped.set(0);
        overUsedRnodes = new HashSet<>();

        hus = config.isHus();
        husInitialCongested = false;

        routerTimer.getRuntimeTracker("Initialization").stop();
    }

    /**
     * Auto-tunes a subset of routing parameters based on input design size.
     * The small profile is used only if both node count and connection count are below threshold.
     * User-provided CLI/API values take precedence and are never overridden.
     */
    private void autoTuneRoutingProfileByDesignSize() {
        long nodeCount = routingGraph.numNodes();
        long connectionCount = indirectConnections.size();
        boolean smallDesign = nodeCount < AUTO_PROFILE_SIZE_THRESHOLD
                && connectionCount < AUTO_PROFILE_SIZE_THRESHOLD;

        short bbExtX = smallDesign ? SMALL_PROFILE_BB_EXT_X : LARGE_PROFILE_BB_EXT_X;
        short bbExtY = smallDesign ? SMALL_PROFILE_BB_EXT_Y : LARGE_PROFILE_BB_EXT_Y;
        float initialPresentCf = smallDesign
                ? SMALL_PROFILE_INITIAL_PRESENT_CF
                : LARGE_PROFILE_INITIAL_PRESENT_CF;

        boolean changed = config.applyAutoRoutingProfile(bbExtX, bbExtY, initialPresentCf);
        if (!changed) {
            return;
        }

        if (config.isUseBoundingBox()) {
            for (Connection connection : indirectConnections) {
                connection.computeConnectionBoundingBox(config.getBoundingBoxExtensionX(),
                        config.getBoundingBoxExtensionY(),
                        routingGraph);
            }
        }

        presentCongestionFactor = config.getInitialPresentCongestionFactor();
        System.out.println("INFO: Auto-tuned routing profile by design size"
                + " (nodes=" + nodeCount
                + ", connections=" + connectionCount
                + ", bbox=" + config.getBoundingBoxExtensionX() + "/" + config.getBoundingBoxExtensionY()
                + ", initialPresentCongestionFactor=" + config.getInitialPresentCongestionFactor() + ")");
    }

    /**
     * Creates clock routing related inputs based on the {@link RWRouteConfig} instance.
     * @param config The {@link RWRouteConfig} instance to use.
     */
    public static ClkRouteTiming createClkTimingData(RWRouteConfig config) {
        String clkRouteTimingFile = config.getClkRouteTiming();
        if (clkRouteTimingFile != null) {
            return new ClkRouteTiming(clkRouteTimingFile);
        }
        return null;
    }

    protected RouteNodeGraph createRouteNodeGraph() {
        if (config.isTimingDriven()) {
            /* An instantiated delay estimator that is used to calculate delay of routing resources */
            DelayEstimatorBase<InterconnectInfo> estimator = new DelayEstimatorBase<InterconnectInfo>(
                    design.getDevice(), new InterconnectInfo(), config.isUseUTurnNodes(), 0);
            return new RouteNodeGraphTimingDriven(design, config, estimator);
        } else {
            return new RouteNodeGraph(design, config);
        }
    }

    protected Collection<Net> getTimingNets() {
        return design.getNets();
    }

    protected TimingManager createTimingManager(ClkRouteTiming clkTiming, Collection<Net> timingNets) {
        final boolean isPartialRouting = false;
        return new TimingManager(design, routerTimer, config, clkTiming, timingNets, isPartialRouting);
    }

    /**
     * Classifies {@link Net} Objects into different categories: clocks, static nets,
     * and regular signal nets (i.e. {@link NetType}.WIRE) and determines routing targets.
     */
    protected void determineRoutingTargets() {
        categorizeNets();

        // Since createNetWrapperAndConnections() both creates the primary sink node and
        // computes alternate sinks (e.g. for LUT pin swaps), it is possible that
        // an alternate sink for one net later becomes an exclusive sink for another net.
        // Examine for all connections for this case and remove such alternate sinks.
        for (Connection connection : indirectConnections) {
            connection.getAltSinkRnodes().removeIf(RouteNode::isUsed);
        }

        // Wait for all outstanding RouteNodeGraph.preserveAsync() calls to complete
        routingGraph.awaitPreserve();
    }

    protected Set<Net> ensureSinkRoutability() {
        // RWRoute routes designs from scratch -- all sinks must be reachable
        return Collections.emptySet();
    }

    private void categorizeNets() {
        numWireNetsToRoute = 0;
        numConnectionsToRoute = 0;
        numPreservedRoutableNets = 0;
        numNotNeedingRoutingNets = 0;
        numUnrecognizedNets = 0;

        nets = new IdentityHashMap<>();
        indirectConnections = new ArrayList<>();
        directConnections = new ArrayList<>();
        clkNets = new ArrayList<>();
        staticNetAndRoutingTargets = new HashMap<>();

        for (Net net : design.getNets()) {
            if (NetTools.isGlobalClock(net)) {
                addGlobalClkRoutingTargets(net);

            } else if (net.isStaticNet()) {
                addStaticNetRoutingTargets(net);

            } else if (net.getType().equals(NetType.WIRE)) {
                if (RouterHelper.isDriverLessOrLoadLessNet(net) ||
                        RouterHelper.isInternallyRoutedNet(net) ||
                        net.getName().equals(Net.Z_NET)) {
                    preserveNet(net, true);
                    numNotNeedingRoutingNets++;
                } else if (RouterHelper.isRoutableNetWithSourceSinks(net)) {
                    addNetConnectionToRoutingTargets(net);
                } else {
                    numNotNeedingRoutingNets++;
                }
            } else {
                numUnrecognizedNets++;
                System.err.println("ERROR: Unknown net " + net);
            }
        }
    }

    /**
     * A helper method for profiling the routing runtime v.s. average span of connections.
     */
    protected void printConnectionSpanStatistics() {
        System.out.println("Connection Span Info:");
        if (config.isPrintConnectionSpan()) System.out.println(" Span" + "\t" + "# Connections" + "\t" + "Percent");

        long sumSpan = 0;
        short max = 0;
        for (Entry<Short, Integer> spanCount : connectionSpan.entrySet()) {
            Short span = spanCount.getKey();
            Integer count = spanCount.getValue();
            if (config.isPrintConnectionSpan()) {
                System.out.printf("%5d \t%12d \t%7.2f\n", span, count, (float)count / indirectConnections.size() * 100);
            }
            sumSpan += span * count;
            if (span > max) max = span;
        }

        if (config.isPrintConnectionSpan()) System.out.println();
        long avg = (long) (sumSpan / ((float) indirectConnections.size()));
        System.out.println("INFO: Max span of connections: " + max);
        System.out.println("INFO: Avg span of connections: " + avg);
        int numConnectionsLongerThanAvg = 0;
        for (Entry<Short, Integer> spanCount : connectionSpan.entrySet()) {
            if (spanCount.getKey() >= avg) numConnectionsLongerThanAvg += spanCount.getValue();
        }

        System.out.printf("INFO: # connections longer than avg span: %d\n", numConnectionsLongerThanAvg);
        System.out.printf("(%5.2f%%)\n", (float)numConnectionsLongerThanAvg / indirectConnections.size() * 100);
        System.out.println("------------------------------------------------------------------------------");
    }

    /**
     * Adds the clock net to the list of clock routing targets, if the clock has source and sink {@link SitePinInst} instances.
     * Any existing routing on such nets will be unrouted.
     * @param clk The clock net in question.
     */
    protected void addGlobalClkRoutingTargets(Net clk) {
        if (RouterHelper.isRoutableNetWithSourceSinks(clk)) {
            clk.unroute();
            // Preserve all pins (e.g. in case of BOUNCE nodes that may serve as a site pin)
            preserveNet(clk, true);
            clkNets.add(clk);
        } else {
            numNotNeedingRoutingNets++;
            System.err.println("ERROR: Incomplete clock net " + clk);
        }
    }

    /**
     * Returns whether a node is (a) available for use,
     * (b) already in used by this net, (c) unavailable
     * @return NodeStatus result.
     */
    protected NodeStatus getGlobalRoutingNodeStatus(Net net, Node node) {
        if (!routingGraph.isAllowedTile(node)) {
            // Outside of PBlock
            return NodeStatus.UNAVAILABLE;
        }

        Net preservedNet = routingGraph.getPreservedNet(node);
        if (preservedNet == net) {
            return NodeStatus.INUSE;
        }
        if (preservedNet != null) {
            return NodeStatus.UNAVAILABLE;
        }

        RouteNode rnode = routingGraph.getNode(node);
        if (rnode != null) {
            // A RouteNode will only be created if the net is necessary for
            // a to-be-routed connection
            return NodeStatus.UNAVAILABLE;
        }
        return NodeStatus.AVAILABLE;
    }

    /**
     * Routes clock nets by default or in a different way when corresponding timing info supplied.
     * NOTE: For an unrouted design, its clock nets must not contain any PIPs or nodes, i.e, completely unrouted.
     * Otherwise, there could be a critical warning of clock routing results, when loading the routed design into Vivado.
     * Vivado will unroute the global clock nets immediately when there is such warning.
     * TODO: fix the potential issue.
     */
    protected void routeGlobalClkNets() {
        Map<Integer, Set<ClockRegion>> usedRoutingTracks = new HashMap<>();
        for (Net clk : clkNets) {
            routeGlobalClkNet(clk, usedRoutingTracks);
        }
    }

    protected void routeGlobalClkNet(Net clk, Map<Integer, Set<ClockRegion>> usedRoutingTracks) {
        // Since we preserved all pins in addGlobalClkRoutingTargets(), unpreserve them here
        for (SitePinInst spi : clk.getPins()) {
            routingGraph.unpreserve(spi.getConnectedNode());
        }
        Function<Node, NodeStatus> gns = (node) -> getGlobalRoutingNodeStatus(clk, node);
        if (routesToSinkINTTiles != null) {
            // routes clock nets with references of partial routes
            System.out.println("INFO: Routing " + clk.getPins().size() + " pins of clock " + clk + " (timing-driven)");
            GlobalSignalRouting.routeClkWithPartialRoutes(clk, routesToSinkINTTiles, design.getDevice(), gns);
        } else {
            // routes clock nets from scratch
            System.out.println("INFO: Routing " + clk.getPins().size() + " pins of clock " + clk + " (non timing-driven)");
            GlobalSignalRouting.symmetricClkRouting(clk, design.getDevice(), gns, usedRoutingTracks);
        }
        preserveNet(clk, false);

        if (clk.hasPIPs()) {
            clk.getSource().setRouted(true);
            assert(clk.getAlternateSource() == null);
        }
    }

    /**
     * Adds and initialize a regular signal net to the list of routing targets.
     * @param net The net to be added for routing.
     */
    protected void addNetConnectionToRoutingTargets(Net net) {
        net.unroute();
        createNetWrapperAndConnections(net);
    }

    /**
     * Adds a static net to the static net routing target list, unrouting it
     * if any routing exists.
     * @param staticNet The static net in question, i.e. VCC or GND.
     */
    protected void addStaticNetRoutingTargets(Net staticNet) {
        List<SitePinInst> sinks = staticNet.getSinkPins();
        if (!sinks.isEmpty()) {
            staticNet.unroute();
            // Remove all output pins from unrouted net as those used will be repopulated
            staticNet.setPins(sinks);

            staticNetAndRoutingTargets.put(staticNet, new ArrayList<>(sinks));
        } else {
            numNotNeedingRoutingNets++;
        }
    }

    /**
     * Routes static nets.
     */
    protected void routeStaticNets() {
        Net vccNet = design.getVccNet();
        Net gndNet = design.getGndNet();

        boolean noStaticRouting = staticNetAndRoutingTargets.isEmpty();
        if (!noStaticRouting) {
            List<SitePinInst> gndPins = staticNetAndRoutingTargets.get(gndNet);
            if (gndPins != null) {
                boolean invertGndToVccForLutInputs = config.isInvertGndToVccForLutInputs();
                Set<SitePinInst> newVccPins = RouterHelper.invertPossibleGndPinsToVccPins(design, gndPins, invertGndToVccForLutInputs);
                if (!newVccPins.isEmpty()) {
                    gndPins.removeAll(newVccPins);
                    staticNetAndRoutingTargets.computeIfAbsent(vccNet, (net) -> new ArrayList<>())
                            .addAll(newVccPins);
                }
            }

            Iterator<Map.Entry<Net,List<SitePinInst>>> it = staticNetAndRoutingTargets.entrySet().iterator();
            while (it.hasNext()) {
                Map.Entry<Net,List<SitePinInst>> e = it.next();
                Net staticNet = e.getKey();
                List<SitePinInst> pins = e.getValue();
                // For some encrypted designs, it's possible that RapidWright cannot infer all SitePinInst-s leading to
                // some site pins (e.g. CKEN) defaulting those to static nets. Detect such cases -- when signal nets are
                // already routed to and preserved at those uninferrable SitePinInst-s -- and remove them from being a
                // static net sink
                pins.removeIf(spi -> {
                    Node node = spi.getConnectedNode();
                    if (!routingGraph.isAllowedTile(node)) {
                        // If sink is not in an allowed tile (e.g. outside routing PBlock) drop it silently
                        return true;
                    }

                    Net preservedNet = routingGraph.getPreservedNet(node);
                    if (preservedNet == null) {
                        // This sink is not preserved by any net, allow
                        return false;
                    }
                    // Sink preserved by another net, abandon; check that it cannot have been preserved by this static net
                    assert(preservedNet != staticNet);
                    return true;
                });

                // Remove from map if empty
                if (pins.isEmpty()) {
                    it.remove();
                }
            }
        }

        // Preserve all static nets' sink pins regardless of whether any routing is necessary
        for (Net staticNet : Arrays.asList(vccNet, gndNet)) {
            for (SitePinInst spi : staticNet.getPins()) {
                if (spi.isOutPin()) {
                    continue;
                }
                routingGraph.preserve(spi.getConnectedNode(), staticNet);
            }
        }

        if (noStaticRouting) {
            // Now that all static nets have been fully preserved, return if no work to be done
            return;
        }

        for (Map.Entry<Net,List<SitePinInst>> e : staticNetAndRoutingTargets.entrySet()) {
            Net staticNet = e.getKey();
            List<SitePinInst> pins = e.getValue();

            System.out.println("INFO: Routing " + pins.size() + " pins of " + staticNet);

            Function<Node, NodeStatus> gns = (node) -> getGlobalRoutingNodeStatus(staticNet, node);
            GlobalSignalRouting.routeStaticNet(pins, gns, design, routethruHelper);

            preserveNet(staticNet, false);
        }
    }

    /**
     * Preserves a net by preserving all nodes use by the net.
     * @param net The net to be preserved.
     */
    protected void preserveNet(Net net, boolean async) {
        if (async) {
            routingGraph.preserveAsync(net);
        } else {
            routingGraph.preserve(net);
        }
    }

    private final Map<Short, Integer> connectionSpan = new HashMap<>();

    /**
     * Creates a unique {@link NetWrapper} instance and {@link Connection} instances based on a {@link Net} instance.
     * @param net The net to be initialized.
     * @return A {@link NetWrapper} instance.
     */
    protected NetWrapper createNetWrapperAndConnections(Net net) {
        List<SitePinInst> sinkPins = net.getSinkPins();
        assert(!sinkPins.isEmpty());

        NetWrapper netWrapper = new NetWrapper(numWireNetsToRoute++, net);
        NetWrapper existingNetWrapper = nets.put(net, netWrapper);
        assert(existingNetWrapper == null);

        SitePinInst source = net.getSource();
        Node sourceINTNode = RouterHelper.projectOutputPinToINTNode(source);

        RouteNode sourceINTRnode = null;
        int indirect = 0;
        for (SitePinInst sink : sinkPins) {
            Connection connection = new Connection(numConnectionsToRoute++, source, sink, netWrapper);
            Node sinkINTNode = RouterHelper.projectInputPinToINTNode(sink);
            if (sourceINTNode == null && sinkINTNode != null) {
                // Sink can be projected to an INT tile, but primary source (e.g. COUT)
                // cannot be; try alternate source
                Pair<SitePinInst,RouteNode> altSourceAndRnode = connection.getOrCreateAlternateSource(routingGraph);
                if (altSourceAndRnode != null) {
                    SitePinInst altSource = altSourceAndRnode.getFirst();
                    RouteNode altSourceINTRnode = altSourceAndRnode.getSecond();
                    connection.setSource(altSource);
                    connection.setSourceRnode(altSourceINTRnode);
                }
            }

            if ((sourceINTNode == null && connection.getSourceRnode() == null) || sinkINTNode == null) {
                // Direct connection if either source or sink pin cannot be projected to INT tile
                directConnections.add(connection);
                connection.setDirect(true);
            } else {
                if (connection.getSourceRnode() == null) {
                    assert(sourceINTNode != null);
                    if (sourceINTRnode == null) {
                        sourceINTRnode = routingGraph.getOrCreate(sourceINTNode, RouteNodeType.EXCLUSIVE_SOURCE);
                        // Where only a single primary source exists, always preserve
                        // its projected-to-INT source node, since it could
                        // be a projection from LAGUNA/RXQ* -> RXD* (node for INT/LOGIC_OUTS_*)
                        assert(sourceINTRnode != null);
                        routingGraph.preserve(sourceINTNode, net);
                        netWrapper.setSourceRnode(sourceINTRnode);
                    }
                    connection.setSourceRnode(sourceINTRnode);
                }

                indirectConnections.add(connection);

                RouteNode sinkRnode = routingGraph.getOrCreate(sinkINTNode);
                RouteNodeType sinkType = sinkRnode.getType();
                assert(sinkType.isAnyLocal());
                connection.setSinkRnode(sinkRnode);

                if (sinkINTNode.getTile() != sink.getTile()) {
                    TileTypeEnum sinkTileType = sink.getTile().getTileTypeEnum();
                    if (Utils.isLaguna(sinkTileType)) {
                        // Sinks in Laguna tiles must be Laguna registers (but will be projected into the INT tile)
                        // however, it's possible for another net to use the sink node as a bounce -- prevent that here
                        assert(sinkINTNode.getTile().getTileTypeEnum() == TileTypeEnum.INT);
                        routingGraph.preserve(sink.getConnectedNode(), net);
                    }
                }

                // Where appropriate, allow all 6 LUT pins to be swapped to begin with
                char lutLetter = sink.getName().charAt(0);
                int numberOfSwappablePins = (lutPinSwapping && sink.isLUTInputPin())
                        ? LUTTools.MAX_LUT_SIZE : 0;
                if (numberOfSwappablePins > 0) {
                    for (Cell cell : DesignTools.getConnectedCells(sink)) {
                        BEL bel = cell.getBEL();
                        assert(bel.isLUT());
                        String belName = bel.getName();
                        String cellType = cell.getType();
                        if (belName.charAt(0) != lutLetter) {
                            assert(cellType.startsWith("RAM"));
                            // This pin connects to other LUTs! (e.g. SLICEM.H[1-6] also serves
                            // as the WA for A-G LUTs used as distributed RAM) -- do not allow any swapping
                            // TODO: Relax this when https://github.com/Xilinx/RapidWright/issues/901 is fixed
                            numberOfSwappablePins = 0;
                            break;
                        }
                        if (bel.getName().startsWith("H") && cellType.startsWith("RAM")) {
                            // Similarly, disallow swapping of any RAMs on the "H" BELs since their
                            // "A" and "WA" inputs are shared and require extra care to keep in sync
                            numberOfSwappablePins = 0;
                            break;
                        }
                        if (cellType.startsWith("SRL")) {
                            // SRL* cells cannot support any pin swaps
                            numberOfSwappablePins = 0;
                            break;
                        }
                        if (belName.charAt(1) == '5') {
                            // Since a 5LUT cell exists, only allow bottom 5 pins to be swapped
                            numberOfSwappablePins = 5;
                        }
                    }
                }

                Site site = sink.getSite();
                for (int i = 1; i <= numberOfSwappablePins; i++) {
                    Node node = site.getConnectedNode(lutLetter + Integer.toString(i));
                    assert(node.getTile().getTileTypeEnum() == TileTypeEnum.INT);
                    if (node.equals(sinkINTNode)) {
                        continue;
                    }
                    if (routingGraph.isPreserved(node)) {
                        continue;
                    }
                    RouteNode altSinkRnode = routingGraph.getOrCreate(node, sinkType);
                    assert(altSinkRnode.getType() == sinkType);
                    connection.addAltSinkRnode(altSinkRnode);
                }

                if (!connection.hasAltSinks()) {
                    // Since this connection only has a single sink target, make it exclusive
                    sinkType = sinkType.isEastLocal() ? RouteNodeType.EXCLUSIVE_SINK_EAST :
                               sinkType.isWestLocal() ? RouteNodeType.EXCLUSIVE_SINK_WEST :
                               sinkType == RouteNodeType.LOCAL_BOTH ? RouteNodeType.EXCLUSIVE_SINK_BOTH :
                               null;
                    assert(sinkType != null);
                    sinkRnode.setType(sinkType);

                    // And increment its usage here immediately
                    sinkRnode.incrementUser(netWrapper);
                }

                connection.setDirect(false);
                indirect++;
                connection.computeHpwl();
                addConnectionSpanInfo(connection);
            }
        }

        if (indirect > 0) {
            netWrapper.computeHPWLAndCenterCoordinates(routingGraph);
            if (config.isUseBoundingBox()) {
                for (Connection connection : netWrapper.getConnections()) {
                    if (connection.isDirect()) continue;
                    connection.computeConnectionBoundingBox(config.getBoundingBoxExtensionX(),
                            config.getBoundingBoxExtensionY(),
                            routingGraph);
                }
            }
        }
        return netWrapper;
    }

    /**
     * Adds span info of a connection.
     * @param connection A connection of which span info is to be added.
     */
    private void addConnectionSpanInfo(Connection connection) {
        connectionSpan.merge(connection.getHpwl(), 1, Integer::sum);
    }

    /**
     * @return ConnectionState object to be used for routing.
     */
    protected ConnectionState getConnectionState() {
        return connectionState;
    }

    /**
     * Initializes routing.
     */
    private void initializeRouting() {
        routingGraph.initialize();
        routeIteration = 1;
        historicalCongestionFactor = config.getHistoricalCongestionFactor();
        presentCongestionFactor = config.getInitialPresentCongestionFactor();
        timingWeight = config.getTimingWeight();
        wlWeight = config.getWirelengthWeight();
        oneMinusTimingWeight = 1 - timingWeight;
        oneMinusWlWeight = 1 - wlWeight;
        printIterationHeader(config.isTimingDriven());

        // On Versal only, reserve all uphills of NODE_(CLE|INTF)_CTRL sinks since
        // their [BC]NODEs can also be used to reach NODE_INODEs --- not applying this
        // heuristic can lead to avoidable congestion
        if (routingGraph.isVersal) {
            for (Connection connection : indirectConnections) {
                RouteNode sinkRnode = connection.getSinkRnode();
                if (sinkRnode.getType() == RouteNodeType.EXCLUSIVE_SINK_BOTH) {
                    IntentCode sinkIntent = sinkRnode.getIntentCode();
                    for (Node uphill : sinkRnode.getAllUphillNodes()) {
                        if (uphill.isTiedToVcc()) {
                            continue;
                        }
                        Net preservedNet = routingGraph.getPreservedNet(uphill);
                        if (preservedNet != null && preservedNet != connection.getNet()) {
                            continue;
                        }
                        if (sinkIntent == IntentCode.NODE_SLL_INPUT && uphill.getIntentCode() == IntentCode.NODE_SLL_OUTPUT) {
                            // No need to reserve NODE_SLL_OUTPUT nodes of a NODE_SLL_INPUT
                            continue;
                        }
                        assert((sinkIntent == IntentCode.NODE_CLE_CTRL &&
                                        (uphill.getIntentCode() == IntentCode.NODE_CLE_CNODE || uphill.getIntentCode() == IntentCode.NODE_CLE_BNODE)) ||
                                (sinkIntent == IntentCode.NODE_INTF_CTRL &&
                                        (uphill.getIntentCode() == IntentCode.NODE_INTF_CNODE || uphill.getIntentCode() == IntentCode.NODE_INTF_BNODE)) ||
                                (sinkIntent == IntentCode.NODE_SLL_INPUT &&
                                        uphill.getIntentCode() == IntentCode.NODE_CLE_BNODE)
                        );
                        RouteNode rnode = routingGraph.getOrCreate(uphill, RouteNodeType.LOCAL_RESERVED);
                        rnode.setType(RouteNodeType.LOCAL_RESERVED);
                    }
                }
            }
        }
    }

    /**
     * Routes the design in a few routing phases and times those phases.
     */
    public void route() {
        // Prints the design and configuration info, if "--verbose" is configured
        printDesignNetsAndConfigurationInfo(config.isVerbose());

        routerTimer.createRuntimeTracker("Routing", routerTimer.getRootRuntimeTracker()).start();
        MessageGenerator.printHeader("Route Design");

        final long tPre0 = System.nanoTime();
        routerTimer.createRuntimeTracker("route clock", "Routing").start();
        routeGlobalClkNets();
        routerTimer.getRuntimeTracker("route clock").stop();
        final long tPreClk = System.nanoTime();

        routerTimer.createRuntimeTracker("route static nets", "Routing").start();
        // Routes static nets (VCC and GND) before signals for now.
        // All the used nodes by other nets should be marked as unavailable, if static nets are routed after signals.
        routeStaticNets();
        // Connection-based router for indirectly connected pairs of output pin and input pin */
        routerTimer.getRuntimeTracker("route static nets").stop();
        final long tPreStatic = System.nanoTime();

        RuntimeTracker routeWireNets = routerTimer.createRuntimeTracker("route wire nets", "Routing");
        routeWireNets.start();
        preRoutingEstimation();
        final long tPreEst = System.nanoTime();
        F2Prewarm.initialize(indirectConnections, directConnections);
        final long tPreF2 = System.nanoTime();
        if (F4DirectGpu.enabled()) f4PrecomputeDirect();
        final long tPreDir = System.nanoTime();
        System.out.printf("F4_V66_PRE clk=%.2f static=%.2f preEst=%.2f f2=%.2f directPre=%.2f%n",
                (tPreClk - tPre0) / 1e9, (tPreStatic - tPreClk) / 1e9,
                (tPreEst - tPreStatic) / 1e9, (tPreF2 - tPreEst) / 1e9,
                (tPreDir - tPreF2) / 1e9);
        routeIndirectConnectionsIteratively();
        // NOTE: route direct connections after indirect connection.
        // The reason is that there maybe additional direct connections in the soft preserve mode for partial routing,
        // and those direct connections should be included to be routed
        if (F4DirectGpu.enabled()) f4ReleaseFlood();
        try (F4DirectGpu direct = F4DirectGpu.enabled() ? new F4DirectGpu("direct_finalize") : null) {
        f4RawPaths = direct;
        if (direct != null) f4CommitDirect();
        else routeDirectConnections();
        routeWireNets.stop();
        // Adds child timers to "route wire nets" timer
        routeWireNets.addChild(rnodesTimer);
        // Do not time the cost evaluation method for routing connections, the timer itself takes time
        routerTimer.createRuntimeTracker("route connections", "route wire nets").setTime(routeWireNets.getTime() - rnodesTimer.getTime() - updateTimingTimer.getTime() - updateCongestionCosts.getTime());
        if (config.isTimingDriven()) {
            routeWireNets.addChild(updateTimingTimer);
        }
        routeWireNets.addChild(updateCongestionCosts);

        if (direct != null) direct.report("direct_finalize", directConnections.size());
        f4RawPaths = null;
        } finally {
            f4RawPaths = null;
        }
        routerTimer.createRuntimeTracker("finalize routes", "Routing").start();
        // Assigns a list of nodes to each direct and indirect connection that has been routed and fix illegal routes if any
        postRouteProcess();
        if (f4Direct != null) {
            f4Direct.report("F4_DIRECT_FINAL");
            f4WriteCpuFinal();
        }
        if (config.isFidesLaneMirror() && !Boolean.getBoolean("fides.v37.skipClosingMirrorScan")) {
            /* The closing full scan, before setPIPsOfNets() writes anything back: the last moment the
             * mirror can be compared against the objects it shadows, and the only place fixRoutes()'s
             * out-of-band rip is answerable for.  It also turns "the flat graph is the live graph" from
             * a construction-time claim into a number, since a materialised node the manifest does not
             * cover would have been silently absent from every scan this run reported. */
            try {
                getFidesLaneGate().mirrorClosingScan(connectionsRouted.get());
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES mirror failed at the close of the run\n"
                        + "       " + e.getMessage(), e);
            }
        }
        /* The lane's own close of a whole round: the cumulative census and whether the branches a
         * small sample cannot reach were reached.  Guarded on the field rather than on
         * getFidesLaneGate() so that a run whose lane never got as far as its first connection does
         * not build the gate purely in order to print nothing. */
        if (fidesLaneGate != null) {
            fidesLaneGate.closeRound();
        }
        // Assigns net PIPs based on lists of connections
        setPIPsOfNets();
        routerTimer.getRuntimeTracker("finalize routes").stop();

        routerTimer.getRuntimeTracker("Routing").stop();

        if (config.getExportOutOfContext()) {
            getDesign().setAutoIOBuffers(false);
            getDesign().setDesignOutOfContext(true);
        }

        // Prints routing statistics, e.g. total wirelength, runtime and timing report
        printRoutingStatistics();
    }

    /**
     * Calculates initial criticality for each connection based on a simple estimation.
     */
    private void preRoutingEstimation() {
        if (config.isTimingDriven()) {
            estimateDelayOfConnections();
            maxDelayAndTimingVertex = timingManager.calculateArrivalRequiredTimes();
            timingManager.calculateCriticality(indirectConnections, MAX_CRITICALITY, config.getCriticalityExponent());
            System.out.printf("INFO: Estimated pre-routing max delay: %4d\n", (short) maxDelayAndTimingVertex.getFirst().floatValue());
        }
    }

    /**
     * A simple approach to estimate delay of each connection and update route delay of its timing edges.
     */
    private void estimateDelayOfConnections() {
        for (Connection connection : indirectConnections) {
            RouteNode source = connection.getSourceRnode();
            RouteNode[] children = source.getChildren(routingGraph);
            if (children.length == 0) {
                // output pin is blocked
                swapOutputPin(connection);
                source = connection.getSourceRnode();
            }
            short estDelay = (short) 10000;
            for (RouteNode child : children) {
                short tmpDelay = 113;
                tmpDelay += child.getDelay();
                if (tmpDelay < estDelay) {
                    estDelay = tmpDelay;
                }

            }
            estDelay += source.getDelay();
            connection.setTimingEdgesDelay(estDelay);
        }
    }

    /**
     * Routes direct connections.
     */
    private void routeDirectConnections() {
        System.out.println("\nINFO: Route " + directConnections.size() + " direct connections ");
        for (Connection connection : directConnections) {
            boolean success = RouterHelper.routeDirectConnection(connection);
            connection.setRouted(success);
            // no need to update route delay of direct connection, because it would not be changed
            if (!success) System.err.println("ERROR: Failed to route direct connection " + connection);
        }
    }

    /**
     * The dispatch itself, which is now a choice of driver rather than a loop.
     *
     * <p>M4-2alpha adds a second driver and leaves the first one alone.  The serial driver is the loop
     * this method has always been, moved into {@link #routeIndirectConnectionsOneAtATime} with its body
     * unchanged; the batch driver is {@link #routeIndirectConnectionsInBatches}.  Which one runs is
     * {@code --fidesLaneBatch}, and the default is <b>one</b>, so the batch driver is the one that runs
     * unless a caller asks for zero.  That default is the point of this tier rather than an accident:
     * a batch of one must reproduce the serial trajectory byte for byte, and a proof of that which
     * could be had by leaving the old code in place would prove nothing.  Zero stays available, and is
     * what the gate uses as its control arm -- same binary, same round, one number different.
     *
     * <p>The split is deliberately a <em>copy</em> and not a refactor into shared helpers.  If the two
     * arms shared the collect and commit steps, then "the batch driver is inert at a batch of one"
     * would be a statement about a single code path compared with itself, and an edit that broke the
     * serial arm would break both arms in the same way and stay invisible.  Keeping the serial loop
     * byte-identical is what makes the gate's A/B an experiment: two code paths, one binary, one
     * answer.
     */
    protected void routeIndirectConnections(Collection<Connection> connections) {
        if (f4Diversion != null) {
            f4RouteDiverted(connections);
        } else if (config.getFidesLaneBatch() > RWRouteConfig.FIDES_LANE_BATCH_OFF) {
            routeIndirectConnectionsInBatches(connections);
        } else {
            routeIndirectConnectionsOneAtATime(connections);
        }
    }

    /**
     * The serial driver: one connection at a time, collected and committed in the same breath.
     *
     * <p>This is the loop {@code routeIndirectConnections} was before M4-2alpha, verbatim, and it is
     * kept verbatim on purpose -- see that method's javadoc.  It is the control arm of the batch
     * tier's A/B, so its bytes are load-bearing and it must not be tidied.
     */
    protected void routeIndirectConnectionsOneAtATime(Collection<Connection> connections) {
        for (Connection connection : connections) {
            if (shouldRoute(connection)) {
                // FIDES M3-step3 (item 4, rung 2): the flat mirror of the three mutable columns is
                // driven from here, by the event stream rather than by reading the objects.  It goes
                // around the dispatch rather than inside either branch because it has to hold for
                // every connection of every iteration -- the protocol a CUDA lane would use -- and
                // "before" has to precede the reference's own ripUp, which is the first thing
                // prepareRouteConnection does.  Both hooks are no-ops without --fidesLaneMirror.
                final FidesLaneGate mirrorGate = config.isFidesLaneMirror() ? getFidesLaneGate() : null;
                if (mirrorGate != null) {
                    mirrorGate.mirrorBeforeAttempt(connection);
                }
                // FIDES M3-step3: connections are handed to the flat-CSR lane instead.  Which ones is
                // decided in RWRouteConfig#takesConnectionOnLane -- one named connection under
                // --fidesLaneAttempt (item 3), or the whole round under --fidesLaneAll (item 5).  The
                // counter is only advanced by the two methods below, so `get() + 1` is the sequence
                // this call is about to take; every other connection goes through the untouched
                // reference path.
                if (config.takesConnectionOnLane(connectionsRouted.get() + 1)) {
                    routeIndirectConnectionOnLane(connection);
                } else {
                    /* The fail-closed decline: this connection is routed by the untouched serial
                     * implementation.  Counted because "how often did the GPU path step aside" is the
                     * one number that decides whether a lane verdict is about the whole round -- the
                     * driver's G5d reads it against FROZEN_CON minus the attempts the lane took, which
                     * is 0 on a full round and the connections above the limit on a limited one (the
                     * same takesConnectionOnLane predicate above is what declines them, so a limit is
                     * not a second mechanism: it is this one, with a threshold).  The increment sits
                     * on this side of the dispatch, so it cannot be reached by a lane that answered. */
                    fallbackAttempts++;
                    routeIndirectConnection(connection);
                }
                if (mirrorGate != null) {
                    mirrorGate.mirrorAfterAttempt(connection);
                }
            }
        }
    }

    /* ==================================================================== M4-2alpha: the batch driver */

    /**
     * One connection's place in a batch: what it takes to search it later, and what it takes to commit
     * it later.
     *
     * <p>Four fields, and the two halves are the two phases.  {@code attempt} is what the device is
     * handed; {@code connection}, {@code state} and {@code result} are what the commit needs, and the
     * commit is where every one of them is finally read.  The split exists because the search and the
     * commit stop happening in the same breath -- but at a batch of one the batch is always empty by
     * the time the next connection is collected, so the breath is the serial one.
     *
     * <p><b>{@code state} is not yet per-slot, and that is the whole of why a batch above one is
     * refused.</b>  {@link #getConnectionState} hands out one {@code ConnectionState} for the whole
     * run, holding the live priority queue and the target list; a batch of W attempts in flight at once
     * would have W of them searching through one queue.  It is a field here rather than looked up at
     * commit time so that the shape beta needs is the shape written down: give this slot its own state
     * and the queue stops being shared, which is the first of the three things {@code --fidesLaneBatch}
     * names when it refuses a wider batch.
     */
    private static final class LaneBatchSlot {
        final Connection connection;
        final ConnectionState state;
        final FidesLane.Attempt attempt;
        FidesLane.Result result;

        LaneBatchSlot(Connection connection, ConnectionState state, FidesLane.Attempt attempt) {
            this.connection = connection;
            this.state = state;
            this.attempt = attempt;
        }
    }

    /**
     * The batch driver: collect a group of connections, hand the group to the lane as a group, commit
     * the answers in the order the connections came in.
     *
     * <p>The one thing this driver changes about the run is <em>when</em> a connection is committed
     * relative to when it is searched.  Everything the commit does -- {@code finishRouteConnection},
     * the criterion-A render, the hand-back check, the golden line, the event stream, the global
     * counters, the mirror updates -- still happens once per connection, in sequence order, on the
     * host, exactly as the serial arm does it.  What is deferred is the search, and with it the device
     * upload of the three mutable columns that the search reads; and at a batch of one the deferred
     * window is empty, so there is nothing to observe.
     *
     * <p>Above one there would be something to observe and it is not a bug: with W attempts in flight,
     * connection k+1's search reads the columns as they were at the batch boundary rather than after
     * connection k's commit.  {@link #shouldRoute} below is the same staleness seen from the host side.
     * That divergence is M4-2beta's, deliberately: it is what the batch boundary snapshot exists to
     * make well-defined, and it cannot be made well-defined before the device can hold W attempts.
     */
    protected void routeIndirectConnectionsInBatches(Collection<Connection> connections) {
        final int batchSize = config.getFidesLaneBatch();
        final List<LaneBatchSlot> batch = new ArrayList<>(batchSize);
        /* One ConnectionState per slot, and null at a batch of at most one -- see slotState, which is
         * where the two arms and why the null one is not a per-slot state that happens to be the same
         * object are argued.  Allocated here rather than per batch so that the W queues are allocated
         * once for the round rather than 84,107/W times. */
        final List<ConnectionState> slotStates = batchSize > 1 ? new ArrayList<>(batchSize) : null;
        for (Connection connection : connections) {
            /* The flush is here, at the top of the body and ahead of shouldRoute, and both halves of
             * that placement carry weight.
             *
             * Ahead of shouldRoute because shouldRoute reads the live graph -- isCongested, isOverUsed,
             * isRouted -- and a commit is what changes it.  Testing first would admit connection k+1 on
             * the strength of a state that connection k has already stopped agreeing with; at a batch of
             * one, where the flush always fires, that difference is the entire gap between this driver
             * and the serial one, which is to say it is exactly the bug this tier exists to show absent.
             *
             * At the top of the body rather than at the end because the tail of the body has two exits
             * -- a connection that shouldRoute refuses, and one the lane declines -- and only the top of
             * the body is reached on every path.  The condition is `full`, not `non-empty`, so that at a
             * batch of one it fires after every single collect and the batch is never deeper than one
             * connection at the moment a decision is taken. */
            /* Commit the old net before inspecting ANY connection of the next net.
             * Collect has already ripped up the pending routes. Testing shouldRoute
             * before this flush can hide another net's conflict in that transient
             * graph, skip its reroute, and recreate the conflict at the tail commit. */
            if (batch.size() >= batchSize || (!batch.isEmpty()
                    && batch.get(0).connection.getNetWrapper() != connection.getNetWrapper())) {
                runAndCommitBatch(batch);
            }
            if (shouldRoute(connection)) {
                // Which arm this connection goes to, decided once here and read by the branch at the
                // bottom rather than asked twice: the two arms must not be able to disagree about it.
                //
                // The same predicate as the serial arm's, and the same `get() + 1`: the counter is
                // advanced by the collect step, so a connection already collected is already counted
                // and `get() + 1` is this connection's sequence whether or not a batch is open.  The
                // one place the two arms must not drift is here -- a predicate that could not agree
                // with itself between arms would move connections between the lane and the fallback
                // and change the round, and M10's witness is what would catch it.
                final boolean onLane = config.takesConnectionOnLane(connectionsRouted.get() + 1);
                /* The deferral window ends before any host-side routing activity, and that is
                 * load-bearing rather than tidy.  A connection the lane does not carry is routed by the
                 * reference on the spot, and the reference routes it with state that is global and
                 * single-connection by construction: RouteNode.isTarget is one boolean per node, set by
                 * prepareRouteConnection through Connection#setAllTargets and cleared by the commit, and
                 * the reference's search breaks on the first node it pops that carries it (:2328).  A
                 * partial batch's marks are therefore visible to that search, are not its sink, and the
                 * break lands on a foreign target -- saveRouting then throws "Unexpected rnode to
                 * backtrack from" (:2639).  Measured, not argued: limit=20 W=8 dies there on the first
                 * declined connection, which is connection 21, with the four unlaunched slots of the
                 * last batch still marked.  On an already-routed connection the same break takes the
                 * `rnode = prevRouting.get(0)` branch instead and the throw does not happen, which is a
                 * silently different path rather than a loud one.
                 *
                 * The sentence that used to stand here claimed "the batch is empty here by construction
                 * -- the flush above fires whenever it is full and this is the only other way out of the
                 * body -- so the decline never has an open batch to order itself against."  *Full* is
                 * not *empty*: the flush above fires at batchSize, so a partial batch survives to this
                 * branch.  At W=1 the two coincide and those are the bytes M9 verifies; at limit=20 W=2
                 * they coincide by arithmetic (20 % 2 == 0), which is why the first walk of this path
                 * looked clean; at limit=20 W=8 the last batch is 4 slots (20 % 8 == 4) and the first
                 * declined connection runs with them open.  The invariant the old sentence asserted is
                 * what the flush below makes true.
                 *
                 * The flush goes ahead of the mirror's before-hook rather than after it so that the hook
                 * runs in a committed world, the way the serial arm has it.  That is free -- nothing has
                 * changed for this connection yet, so the push is value-neutral -- but the rule being
                 * stated is the one worth holding to: no host-side routing activity inside an open
                 * window. */
                /* M4-2beta (A): the deferral window also ends at a NET BOUNDARY, and it has to.
                 *
                 * The batch's snapshot is one image for all W slots, and one of the three columns in it
                 * -- sameNetCount -- is not a per-node quantity but a per-NET one.  The mirror scopes it
                 * to the current net and rebuilds it whole whenever the net changes, which is why its
                 * notification is MIRROR_SCOPE rather than a delta: every id of the net being left goes
                 * to zero and every id of the one being entered is raised to that net's count.  With
                 * members of two nets in one batch, the scope change mirrorBeforeAttempt is about to make
                 * for this connection lands in the bag the batch pushes at its own boundary, so the image
                 * every slot reads is scoped to the LAST net collected, and the earlier net's slots read
                 * zero exactly where their own net has one.
                 *
                 * Measured, and it is the whole of what that reading predicts: W=8 at limit=185 dies at
                 * sequence 177 with exactly the three sites that read sameNetCount failing five times
                 * each -- five being the number of the earlier net's members in the batch -- while the
                 * two sites that do not read it (isOverUsed, which reads occupancy, and the exclusive
                 * sink identity, which reads the sink id and intentCode) do not fire at all; and the same
                 * run at limit=181, whose last batch is one net, is clean (censusFail=0).  So a batch may
                 * not straddle, and this is where that is enforced: ahead of the hook, because the hook
                 * is what changes the scope, and after the two exits above, so the decision is taken in a
                 * committed world like every other decision in this driver.
                 *
                 * The cost is width, and it is not small: a batch cannot be wider than the net run it
                 * sits in, and this design's nets average 2.13 connections with 63% of them exactly one,
                 * so the effective width on this workload is about two whatever W is.  That is a property
                 * of the workload and not of this condition, and it is recorded rather than tuned around.
                 *
                 * Inert at a batch of one, by construction rather than by arithmetic: the flush at the
                 * top of the body fires whenever the batch is full, so at W=1 the batch is empty by the
                 * time this line is reached and neither disjunct can be true.  The bytes the W=1 arm is
                 * verified against are therefore untouched by this condition, and so is any W>1 run whose
                 * lane prefix happens to sit inside one net. */
                final boolean netBoundary = !batch.isEmpty()
                        && batch.get(0).connection.getNetWrapper() != connection.getNetWrapper();
                if (!batch.isEmpty() && (!onLane || netBoundary)) {
                    runAndCommitBatch(batch);
                }
                // FIDES M3-step3 (item 4, rung 2): the flat mirror of the three mutable columns is
                // driven from here, by the event stream rather than by reading the objects.  It goes
                // around the dispatch rather than inside either branch because it has to hold for
                // every connection of every iteration -- the protocol a CUDA lane would use -- and
                // "before" has to precede the reference's own ripUp, which is the first thing
                // prepareRouteConnection does.  Both hooks are no-ops without --fidesLaneMirror.
                final FidesLaneGate mirrorGate = config.isFidesLaneMirror() ? getFidesLaneGate() : null;
                if (mirrorGate != null) {
                    mirrorGate.mirrorBeforeAttempt(connection);
                }
                if (onLane) {
                    collectLaneAttempt(connection, batch, slotStates);
                } else {
                    /* The fail-closed decline, in the serial arm's shape but not deferred: a declined
                     * connection is routed by the untouched reference path on the spot.  It cannot
                     * defer, because the reference path commits as it goes, and it cannot be collected,
                     * because that is the other arm's job.  The batch is empty by the time it runs --
                     * see the flush above, which is where the old claim that it was empty *without* one
                     * is corrected. */
                    fallbackAttempts++;
                    routeIndirectConnection(connection);
                    if (mirrorGate != null) {
                        /* The mirror's second hook stays with its first one on this path, which is where
                         * the serial arm has it too.  On the lane path it moves to the commit, because
                         * that is when the connection has actually been routed. */
                        mirrorGate.mirrorAfterAttempt(connection);
                    }
                }
            }
        }
        if (!batch.isEmpty()) {
            runAndCommitBatch(batch);
        }
    }

    /**
     * The {@code ConnectionState} for slot {@code slot} of the batch being collected.
     *
     * <p><b>Why a batch of W cannot use the router's own state.</b>  {@link #getConnectionState} hands
     * out one {@code ConnectionState} for the whole router ({@code RWRoute.java:288}), and the reference
     * path gets away with that because it fills the state, searches, and commits before the next
     * connection is looked at.  The commit reads back three things that are per-connection: the two
     * counters {@code dumpGoldenPath} prints ({@code POP=}, {@code PUSH=}), and -- the one that would
     * corrupt the graph rather than a number -- {@code state.targets}, which the commit walks to call
     * {@code clearTarget()} on every target node.  With W states collapsed into one, slot 0's commit
     * would clear slot W-1's targets and leave slots 0..W-2's nodes marked as targets for the rest of
     * the round.  So the batch needs W of them, and this is where it gets them.
     *
     * <p><b>Why the batch-of-one arm is the shared object and not a new one.</b>  A fresh
     * {@code ConnectionState} would very probably behave identically at W=1 -- and "very probably" is
     * the standard this milestone cannot be held to, because W=1's whole job is to be the serial
     * trajectory itself, byte for byte, and it is the arm every existing criterion was measured on.
     * {@code null} therefore means "at most one slot in flight, use the router's own", and it is the
     * batch size that decides, not the batch's current depth.
     *
     * <p>The states are reused across batches, which is safe because the commit clears
     * {@code targets} and {@code begin} empties {@code queue} -- the same two invariants the serial
     * arm relies on to reuse its single state for 84,107 connections.
     */
    private ConnectionState slotState(List<ConnectionState> slotStates, int slot) {
        if (slotStates == null) {
            return getConnectionState();
        }
        while (slotStates.size() <= slot) {
            slotStates.add(new ConnectionState());
        }
        return slotStates.get(slot);
    }

    /**
     * The collect phase: everything the serial arm does before it hands the connection to the lane,
     * and nothing after it.
     *
     * <p>The division is exact and worth stating, because it is the only thing keeping the batch driver
     * honest: this method ends at {@code gate.begin}, and {@code gate.begin} is the last host-side act
     * that reads the live objects as they are.  {@code prepareRouteConnection} has already done the
     * reference's own ripUp (which is why {@code mirrorBeforeAttempt} precedes it here exactly as it
     * does in the serial arm), the state has been assembled with the reference's own weight
     * expressions, and the attempt is folded.  Nothing that writes to the live graph may happen in this
     * method after that point, and nothing does.
     */
    private void f4FillHostState(ConnectionState state, Connection connection, int sequence) {
        state.connection = connection;
        state.sequence = sequence;
        state.f4Boundary = null;
        state.f4RetryStamp = 0;
        state.f4PrivatePopCeiling = 0;
        state.f4OutsideChecks = 0;
        state.f4RetriedPops = 0;
        state.rnodeCostWeight = 1 - connection.getCriticality();
        state.shareWeight = (float) (Math.pow(state.rnodeCostWeight, config.getShareExponent()));
        state.rnodeWLWeight = state.rnodeCostWeight * oneMinusWlWeight;
        state.estWlWeight = state.rnodeCostWeight * wlWeight;
        state.dlyWeight = connection.getCriticality() * oneMinusTimingWeight / 100f;
        state.estDlyWeight = connection.getCriticality() * timingWeight;
        state.nodesPopped = 0;
        state.nodesPushed = 0;
        state.earlyTermination = false;
    }

    private void collectLaneAttempt(Connection connection, List<LaneBatchSlot> batch,
                                    List<ConnectionState> slotStates) {
        /* The driver's invariant, checked where the batch is written and not only where the flush that
         * establishes it is written: a batch is one net's (see the net-boundary flush in the driver).
         * Checked before prepareRouteConnection, which is the first thing below that writes to the live
         * graph, because a straddle is a bug in the driver and must stop the round here rather than reach
         * the lane and come back as a census failure that reads like a defect in the lane. */
        if (!batch.isEmpty()
                && batch.get(0).connection.getNetWrapper() != connection.getNetWrapper()) {
            throw new IllegalStateException("ERROR: FIDES batch straddles a net boundary: "
                    + batch.get(0).connection + " -> " + connection);
        }
        ConnectionState state = slotState(slotStates, batch.size());
        f4FillHostState(state, connection, connectionsRouted.incrementAndGet());
        connectionsRoutedThisIteration.incrementAndGet();

        PriorityQueue<RouteNode> queue = state.queue;
        assert(queue.isEmpty());

        prepareRouteConnection(state);

        FidesLane.Attempt attempt;
        try {
            attempt = getFidesLaneGate().begin(connection, state, queue);
        } catch (FidesLane.Failure e) {
            throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                    + "       " + e.getMessage(), e);
        }
        batch.add(new LaneBatchSlot(connection, state, attempt));
    }

    /**
     * The device phase: hand the whole batch over as a batch, and refuse to carry on if what comes back
     * is not one answer per attempt.
     *
     * <p>The size check is not defensive padding.  Everything downstream indexes results against slots,
     * so a lane that returned a short list would silently pair an answer with the wrong connection --
     * and at a batch of one a short list means an empty one, which is a lane that answered nothing at
     * all.  Failing closed here is what keeps "one result per attempt" an invariant of the driver rather
     * than a hope about the lane.
     */
    private void runAndCommitF4Batch(List<LaneBatchSlot> batch) {
        final long started = System.nanoTime();
        final FidesLaneGate gate = getFidesLaneGate();
        final List<FidesLane.Attempt> attempts = new ArrayList<>(batch.size());
        final List<ConnectionState> states = new ArrayList<>(batch.size());
        for (LaneBatchSlot slot : batch) {
            attempts.add(slot.attempt);
            states.add(slot.state);
        }
        final var paths = gate.f4SearchBatch(attempts, states, f4Direct);
        for (int index = 0; index < batch.size(); ++index) {
            LaneBatchSlot slot = batch.get(index);
            var path = paths.get(index);
            gate.enterSlot(slot.connection, slot.state.sequence);
            slot.state.queue.clear();
            path.apply(slot.state.sequence);
            finishRouteConnection(slot.connection, path.chain()[0]);
            if (!slot.connection.isRouted()) {
                throw new IllegalStateException("GPU_ONLY_COMMIT_FAILED sequence=" + slot.state.sequence);
            }
            if (goldenWriter != null) dumpGoldenPath(slot.connection, slot.state);
            for (RouteNode target : slot.state.targets) target.clearTarget();
            slot.state.targets.clear();
            gate.mirrorAfterAttempt(slot.connection);
            gate.f4RecordCommit(slot.connection, slot.state.sequence, path,
                    slot.state.f4CpuPopsBeforeHandoff);
            f4Direct.finish(true);
        }
        gate.f4FinishBatch();
        long batchElapsed = System.nanoTime() - started;
        f4Diversion.gpu(batch.size(), gate.f4DeviceWidth(), Math.max(1L, batchElapsed - gate.f4LastSealNs()));
        batch.clear();
    }

    private void runAndCommitBatch(List<LaneBatchSlot> batch) {
        if (f4Direct != null) {
            runAndCommitF4Batch(batch);
            return;
        }
        final FidesLaneGate gate = getFidesLaneGate();
        final List<FidesLane.Attempt> attempts = new ArrayList<>(batch.size());
        for (LaneBatchSlot slot : batch) {
            attempts.add(slot.attempt);
        }

        final List<FidesLane.Result> results;
        try {
            results = gate.runBatch(attempts);
        } catch (FidesLane.Failure e) {
            /* The batch is refused whole.  One attempt's failure stops the round, which is what the
             * serial arm does too; naming the connection is exact when the batch is one, and a wider
             * batch would have to name the slot that failed, which the lane's Failure does not carry
             * today (beta's to add alongside the W descriptors). */
            throw new RuntimeException("ERROR: FIDES lane refused a batch of " + batch.size()
                    + " starting at " + batch.get(0).connection + "\n"
                    + "       " + e.getMessage(), e);
        }
        if (results.size() != batch.size()) {
            throw new RuntimeException("ERROR: FIDES lane returned " + results.size() + " results for a"
                    + " batch of " + batch.size() + " attempts; refusing to commit a partial batch");
        }

        for (int k = 0; k < batch.size(); k++) {
            final LaneBatchSlot slot = batch.get(k);
            slot.result = results.get(k);
            commitLaneAttempt(slot);
        }
        batch.clear();
    }

    /**
     * The commit phase: everything the serial arm does after the lane has answered, in the serial
     * arm's order, and unchanged in substance.
     *
     * <p>This is the half that must not become parallel, and the reader should be able to see why
     * without leaving the method.  {@code finishRouteConnection} writes the live graph and the mirror
     * follows it; {@code reportGolden} renders criterion A and therefore has to follow the commit,
     * because {@code dumpGoldenPath} reads the connection's bounding box, which the commit grows; the
     * global counters are added one connection at a time so that the round's totals are the sum of the
     * same terms in the same order.  Every one of those is a host-side act on shared state, and the
     * order they happen in is the connection order.  Handing W of them to the device is what M4-2beta
     * does; committing W of them at once is not on anyone's list.
     *
     * <p>The mirror's {@code mirrorAfterAttempt} belongs here rather than in the collect phase for the
     * same reason: on the serial path it runs after the connection has been routed, and "after" means
     * after the commit, not after the request.
     */
    private void commitLaneAttempt(LaneBatchSlot slot) {
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.COMMIT)) {
        final Connection connection = slot.connection;
        final ConnectionState state = slot.state;
        final FidesLane.Result laneResult = slot.result;
        final FidesLaneGate gate = getFidesLaneGate();
        final PriorityQueue<RouteNode> queue = state.queue;

        /* The gate's per-connection cursor is pointed at the slot being committed, before anything
         * reads it.  At a batch of one this restates what the collect phase's begin() left behind and
         * changes nothing; at a batch of W it is the whole difference, because begin() ran W times
         * before the first commit and the last of those is what the cursor would otherwise hold --
         * which would render every slot's criterion A against the last slot's connection and ask the
         * accessibility oracle about the wrong net.  The sequence goes with them: the gate indexes the
         * frozen golden by it, so a stale one makes criterion A compare this slot's line against
         * another connection's and refuse with HARNESS FAULT -- a loud refusal that names the wrong
         * party.  See FidesLaneGate.enterSlot. */
        gate.enterSlot(connection, state.sequence);

        state.nodesPopped = laneResult.nodesPopped;
        state.nodesPushed = laneResult.nodesPushed;
        state.earlyTermination = laneResult.earlyTermination;

        /* The global's share IS the per-connection total: the reference increments both, with the same
         * values, at :2163-2164 and :1830-1831.  See routeIndirectConnectionOnLane's javadoc for why
         * subtracting the abandoned queue here is the mistake that looks right. */
        nodesPushed.addAndGet(laneResult.nodesPushed);
        nodesPopped.addAndGet(state.nodesPopped);

        /* `rnode` is declared outside the try and assigned inside it, which is the serial arm's shape at
         * :2370-2378 and not a style choice: both calls are on the lane's `Failure` path, and the
         * RuntimeException below is what the dispatch loop already knows how to carry.  Copying the two
         * call sites without the try that surrounds them in the serial arm is a compile error and was
         * one -- this milestone's first build caught exactly this and nothing else, which is also the
         * argument for building before running: neither `bash -n` nor a table selftest can see it. */
        final RouteNode rnode;
        try {
            rnode = gate.resultNode(laneResult);
            /* Pre-commit on purpose, and the two halves must not be merged back into one call: the
             * census delta and the mirror drift check are about the state the lane READ, and the commit
             * below writes exactly those columns (updateUsersAndPresentCongestionCost).  A drift check
             * moved past it would compare the mirror against the commit's own writes and report a drift
             * the lane never saw.  Criterion A is the other half and it MUST wait for the commit -- that
             * is reportGolden, called below. */
            gate.report(laneResult);
        } catch (FidesLane.Failure e) {
            throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                    + "       " + e.getMessage(), e);
        }

        if (rnode != null) {
            queue.clear();
            /* The chain the reference is about to walk, written from the lane's own answer.  It has to
             * be here -- after every replay of the batch, before the first read of `prev` -- and not in
             * the collect phase, where the later slots had not run yet.  At a batch of one this writes
             * what the single attempt's own pushes already wrote, so it is not a behaviour change for
             * the arm that already worked; see FidesLaneGate#rewritePrevFromPath for the measurement
             * that made it necessary at W>1 and for what it does not prove. */
            try {
                gate.rewritePrevFromPath(laneResult, rnode);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
            finishRouteConnection(connection, rnode);
            /* Criterion A, rendered here and not above: the golden line this is compared against is
             * written by dumpGoldenPath from connection.toString() AFTER finishRouteConnection, and
             * finishRouteConnection -> saveRouting -> Connection.addRnode grows the connection's
             * bounding box monotonically (resetRoute clears the nodes but not the box).  Comparing
             * before the commit reads the wrong instant; Con 5261, whose own detour extends its own
             * box, is the one connection in 84,107 where that shows.  See FidesLaneGate#reportGolden. */
            try {
                gate.reportGolden(laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
            try {
                gate.checkHandback(connection, laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane hand-back failed for connection "
                        + connection + "\n       " + e.getMessage(), e);
            }
	    if (connection.isRouted() && goldenWriter != null) {
                dumpGoldenPath(connection, state);
            }
            if (!connection.isRouted()) {
                List<RouteNode> rnodes = connection.getRnodes();
                throw new RuntimeException("ERROR: Unable to save routing for connection " + connection + "\n" +
                                           "       Backtracking terminated at " + rnodes.get(rnodes.size() -1));
            }
            if (config.isTimingDriven()) {
                connection.updateRouteDelay();
            }
            assert(connection.isRouted());
        } else {
            assert(queue.isEmpty());
            // Clears previous route of the connection
            connection.resetRoute();
            connection.setRouted(false);
            assert(connection.getRnodes().isEmpty());
            /* Criterion A runs here too: an attempt that produces no line is exactly what would shift
             * the sequence-to-line mapping, so refusing is the point.  (See
             * routeIndirectConnectionOnLane for the whole-round assumption this rests on.) */
            try {
                gate.reportGolden(laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
        }

        // Reset the nodes marked as this connection's target(s)
        List<RouteNode> targets = state.targets;
        for (RouteNode target : targets) {
            target.clearTarget();
        }
        targets.clear();

        /* Last, because on the serial path it is last: the dispatch loop calls it after
         * routeIndirectConnectionOnLane has returned, which is after the target list has been cleared. */
        if (config.isFidesLaneMirror()) {
            getFidesLaneGate().mirrorAfterAttempt(connection);
        }
            }
    }

    /**
     * Routes indirect connections iteratively.
     */
    private F4FeatureDirect f4Direct;
    private F1Corridor f4Corridor;

    F1Corridor.Box f4CorridorBox(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph) {
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
    }

    void f4RetireCorridor(Connection connection) {
        f4Corridor.disable(connection);
    }
    private int f4Width;

    private F4RoundDiversion f4Diversion;
    private boolean f4FloodReleased;
    private long f4CpuLocalCompleted, f4CpuLocalCommits, f4CpuProbeAttempts, f4CpuProbePops, f4CpuProbeNs;
    private long f4PreviousRoundCpuPops;
    private RouteNode.SearchScratch f4PendingProofScratch;
    private ConnectionState f4PendingProofState;

    boolean f4FloodReleased() { return f4FloodReleased; }

    private void f4ReleaseFlood() {
        if (!f4FloodReleased) {
            F4DirectGpu.releaseIndirectDevice();
            f4FloodReleased = true;
        }
    }

    private int f4Service(Connection connection, FidesLaneGate gate) {
        if (config.isTimingDriven() || !gate.f4CanOffer(connection)) return -1;
        if (!connection.isCrossSLR()) return 0;
        int span = Math.abs(connection.getSinkRnode().getSLRIndex(routingGraph)
                - connection.getSourceRnode().getSLRIndex(routingGraph));
        return span > 1 ? 2 : 1;
    }

    private static final class F4CpuSearch {
        final RouteNode target;
        final long elapsedNs, crossingNs;
        final int crossingPops;

        F4CpuSearch(RouteNode target, long elapsedNs, long crossingNs, int crossingPops) {
            this.target = target;
            this.elapsedNs = elapsedNs;
            this.crossingNs = crossingNs;
            this.crossingPops = crossingPops;
        }
    }

    private F4CpuSearch f4CpuSearch(ConnectionState state, int tier, boolean allowHandoff) {
        final PriorityQueue<RouteNode> queue = state.queue;
        final int budget = tier < 0 ? Integer.MAX_VALUE : F4RoundDiversion.budget(tier);
        long started = System.nanoTime();
        long crossing = -1;
        int crossingPops = 0;
        RouteNode target;
        while ((target = queue.poll()) != null) {
            ++state.nodesPopped;
            if (!state.f4PrivateSearch && f4First != null && (state.nodesPopped & 8191) == 0)
                f4First.report(routeIteration, f4FirstPosition, f4FirstTotal, state.nodesPopped, false);
            if (target.isTarget()) break;
            exploreAndExpand(state, target);
            if (state.f4PrivatePopCeiling != 0 && state.nodesPopped >= state.f4PrivatePopCeiling)
                return new F4CpuSearch(null, System.nanoTime() - started, -1, 0);
            // Lookahead remains indivisible. Check at the outer boundary without reordering A*.
            if (crossing < 0 && F4RoundDiversion.crossed(state.nodesPopped, budget, !queue.isEmpty())) {
                crossing = System.nanoTime() - started;
                crossingPops = state.nodesPopped;
                if (allowHandoff) return new F4CpuSearch(null, crossing, crossing, crossingPops);
            }
        }
        return new F4CpuSearch(target, System.nanoTime() - started, crossing, crossingPops);
    }

    private void f4ObserveCpu(ConnectionState state, int tier, F4CpuSearch result, boolean handedOff) {
        f4CpuProbeAttempts++;
        f4CpuProbePops += state.nodesPopped;
        f4CpuProbeNs += result.elapsedNs;
        f4Diversion.cpu(state.connection.getNetWrapper(), tier, state.nodesPopped,
                result.elapsedNs, result.crossingNs, result.crossingPops, handedOff);
        nodesPopped.addAndGet(state.nodesPopped);
        int pushed = state.nodesPopped + state.queue.size();
        nodesPushed.addAndGet(pushed);
        if (state.f4PrivateSearch) nodesPushed.addAndGet(state.nodesPushed);
        state.nodesPushed += pushed;
        state.queue.clear();
    }

    private void f4FinishCpu(ConnectionState state, RouteNode target, FidesLaneGate gate) {
        Connection connection = state.connection;
        if (target == null) {
            connection.resetRoute();
            connection.setRouted(false);
        } else {
            finishRouteConnection(connection, target);
            if (!connection.isRouted())
                throw new IllegalStateException("F4 CPU commit failed sequence=" + state.sequence);
            if (config.isTimingDriven()) connection.updateRouteDelay();
            if (goldenWriter != null) dumpGoldenPath(connection, state);
            ++f4CpuLocalCommits;
        }
        for (RouteNode mark : state.targets) mark.clearTarget();
        state.targets.clear();
        gate.mirrorAfterAttempt(connection);
        ++f4CpuLocalCompleted;
    }

    private int f4PrivatePendingProof(Connection connection, int tier, List<LaneBatchSlot> batch, F1Corridor.Box boundary) {
        for (LaneBatchSlot pending : batch)
            if (pending.connection.getSinkRnode() == connection.getSinkRnode()) return 0;
        if (f4PendingProofScratch == null) {
            f4PendingProofScratch = new RouteNode.SearchScratch();
            f4PendingProofState = new ConnectionState();
            f4PendingProofState.f4PrivateSearch = true;
        }
        RouteNode.SearchScratch scratch = f4PendingProofScratch;
        ConnectionState proof = f4PendingProofState;
        f4FillHostState(proof, connection, connectionsRouted.get() + 1);
        proof.f4Boundary = boundary;
        scratch.openReadOnlyReplay();
        try {
            for (LaneBatchSlot pending : batch)
                for (RouteNode target : pending.state.targets)
                    if (target.isTarget()) target.clearTarget();
            scratch.beginPrivateRipUp(connection.getNetWrapper());
            try {
                prepareRouteConnection(proof);
            } finally {
                scratch.endPrivateRipUp();
            }
            if (!getFidesLaneGate().f4Supported(connection, proof)) return 0;
            F4CpuSearch result = f4CpuSearch(proof, tier, true);
            if (result.target != null || result.crossingNs < 0) return 0;
            int popped = proof.nodesPopped;
            f4ObserveCpu(proof, tier, result, true);
            return popped;
        } catch (RouteNode.F5ColdAdjacency cold) {
            // The pending GPU batch must commit before the live search can follow a new edge.
            return 0;
        } finally {
            proof.queue.clear();
            proof.targets.clear();
            scratch.close();
        }
    }

    private void f4CollectProven(ConnectionState state, int budget, int popped, List<LaneBatchSlot> batch) {
        state.f4CpuPopsBeforeHandoff = popped;
        state.f4HandoffBudget = budget;
        state.f4RetryStamp = 0;
        state.nodesPopped = 0;
        state.nodesPushed = 0;
        state.earlyTermination = false;
        state.queue.clear();
        RouteNode source = state.connection.getSourceRnode();
        source.clearPrev();
        push(state, source, 0f, 0f, false);
        try {
            var attempt = getFidesLaneGate().begin(state.connection, state, state.queue);
            batch.add(new LaneBatchSlot(state.connection, state, attempt));
        } catch (FidesLane.Failure error) {
            throw new IllegalStateException("F4 lane refused proven attempt " + state.sequence, error);
        }
    }

    private long f4Narrow, f4Widen, f4SpatialGroups, f4SpatialCommits, f4SpatialWasteNs;
    private int f4NextSpatial;
    final java.util.concurrent.ForkJoinPool f4SharedPool =
            java.util.concurrent.ForkJoinPool.commonPool();
    private final java.util.concurrent.ConcurrentLinkedQueue<RouteNode.SearchScratch> f4SpatialScratch =
            new java.util.concurrent.ConcurrentLinkedQueue<>();

    private static final class F4SpatialResult {
        final ConnectionState state;
        final RouteNode.SearchScratch scratch;
        final F4CpuSearch search;
        final List<RouteNode> path;
        final int tier;
        F1Corridor.Box footprint;
        F4SpatialResult(ConnectionState state, RouteNode.SearchScratch scratch,
                        F4CpuSearch search, List<RouteNode> path, int tier) {
            this.state = state;
            this.scratch = scratch;
            this.search = search;
            this.path = path;
            this.tier = tier;
        }
    }

    private F4SpatialResult f4ParallelSearch(Connection c, F1Corridor.Box box, int sequence, int tier) {
        var state = new ConnectionState();
        f4FillHostState(state, c, sequence);
        state.f4Boundary = box;
        state.f4PrivateSearch = true;
        var cached = f4SpatialScratch.poll();
        final var scratch = cached == null ? new RouteNode.SearchScratch() : cached;
        scratch.openFrozenPair();
        state.f4PrivatePopCeiling = 32768;
        boolean detached = false;
        try {
            scratch.beginPrivateRipUp(c.getNetWrapper());
            try {
                prepareRouteConnection(state);
            } finally {
                scratch.endPrivateRipUp();
            }
            // Private work stays bounded. Hard connections take the common live path.
            F4CpuSearch search = f4CpuSearch(state, tier, true);
            if (search.target == null) return null;
            var path = new ArrayList<RouteNode>();
            for (RouteNode node = search.target; node != null; node = node.getPrev()) {
                path.add(node);
                if (node == c.getSourceRnode()) break;
                if (path.size() > state.nodesPopped + 1) return null;
            }
            if (path.get(path.size() - 1) != c.getSourceRnode()) return null;
            scratch.detachPair();
            detached = true;
            return new F4SpatialResult(state, scratch, search, path, tier);
        } catch (RouteNode.F5ColdAdjacency cold) {
            return null;
        } finally {
            if (!detached) {
                scratch.close();
                f4SpatialScratch.add(scratch);
            }
        }
    }

    private boolean f4SpatialConflict(F4SpatialResult first, F4SpatialResult second) {
        var reads = second.scratch.readNodes();
        var oldPath = first.state.connection.getRnodes();
        return !Collections.disjoint(first.scratch.writeNodes(), reads)
                || !Collections.disjoint(oldPath, reads)
                || !Collections.disjoint(first.path, reads)
                || !Collections.disjoint(oldPath, second.path)
                || !Collections.disjoint(first.path, second.path)
                || !Collections.disjoint(first.path, second.state.connection.getRnodes())
                || !Collections.disjoint(oldPath, second.state.connection.getRnodes());
    }

    private int f4SpatialRoute(List<Connection> ordered, int position, boolean armed, FidesLaneGate gate) {
        if (!config.isUseBoundingBox() || config.isTimingDriven() || position < f4NextSpatial
                || f4SharedPool.getParallelism() < 2) return 0;
        Connection first = ordered.get(position);
        if ((armed && first.getHpwl() >= 16) || first.hasAltSinks()
                || !first.getSourceRnode().f4CachedChildren()) return 0;
        final int limit = Math.min(ordered.size(), position + Math.min(32, 2 * f4SharedPool.getParallelism()));
        var candidates = new ArrayList<Connection>(limit - position);
        var boxes = new ArrayList<F1Corridor.Box>(limit - position);
        for (int i = position; i < limit; ++i) {
            Connection c = ordered.get(i);
            if (!shouldRoute(c) || (armed && c.getHpwl() >= 16) || c.hasAltSinks()
                    || !c.getSinkRnode().getType().isAnyExclusiveSink()
                    || c.getSourceRnode() == c.getSinkRnode()
                    || !c.getSourceRnode().f4CachedChildren()) break;
            candidates.add(c);
            boxes.add(gate.f4Boundary(c));
        }
        if (candidates.size() < 2) return 0;
        int n = new F4SpatialSchedule(candidates, boxes).prefix();
        // A giant overlapping leaf is routed once, serially. Do not re-plan it per connection.
        if (n < 2) {
            f4NextSpatial = position + candidates.size();
            return 0;
        }
        final int sequence = connectionsRouted.get() + 1;
        final var futures = new ArrayList<java.util.concurrent.CompletableFuture<F4SpatialResult>>(n);
        long started = System.nanoTime();
        for (int i = 0; i < n; ++i) {
            Connection c = candidates.get(i);
            F1Corridor.Box box = boxes.get(i);
            int number = sequence + i;
            int tier = f4FloodReleased ? -1 : f4Service(c, gate);
            futures.add(java.util.concurrent.CompletableFuture.supplyAsync(
                    () -> f4ParallelSearch(c, box, number, tier), f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                futures.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        final var accepted = new ArrayList<F4SpatialResult>(n);
        for (int i = 0; i < n; ++i) {
            F4SpatialResult r = futures.get(i).join();
            if (r == null) break;
            boolean conflict = false;
            for (var earlier : accepted) {
                if (f4SpatialConflict(earlier, r)) { conflict = true; break; }
            }
            if (conflict) break;
            accepted.add(r);
        }
        if (accepted.size() != n) {
            f4SpatialWasteNs += System.nanoTime() - started;
            f4NextSpatial = position + candidates.size();
            for (int i = accepted.size(); i < n; ++i) {
                var r = futures.get(i).join();
                if (r != null) {
                    r.scratch.f4DiscardDetached();
                    f4SpatialScratch.add(r.scratch);
                }
            }
        }
        ++f4SpatialGroups;
        for (var r : accepted) {
            if (connectionsRouted.incrementAndGet() != r.state.sequence)
                throw new IllegalStateException("Spatial CPU commit out of order");
            connectionsRoutedThisIteration.incrementAndGet();
            if (r.state.f4Boundary != null) ++f4Narrow;
            gate.mirrorBeforeAttempt(r.state.connection);
            ripUp(r.state.connection);
            r.scratch.f4DropPrivateRip();
            r.scratch.attachPairForCommit();
            try {
                f4ObserveCpu(r.state, r.tier, r.search, false);
                f4FinishCpu(r.state, r.search.target, gate);
                r.scratch.publish();
            } finally {
                r.scratch.close();
                f4SpatialScratch.add(r.scratch);
            }
            ++f4SpatialCommits;
        }
        return accepted.size();
    }

    private F4CpuSearch f4SearchWithBoundary(ConnectionState state, int tier, boolean allowHandoff) {
        F4CpuSearch search = f4CpuSearch(state, tier, allowHandoff);
        if (state.f4Boundary == null || search.target != null || !state.queue.isEmpty()) return search;
        ++f4Widen;
        state.f4RetriedPops = state.nodesPopped;
        f4RetireCorridor(state.connection);
        state.f4Boundary = null;
        // Sparse visited stamps isolate the retry; keep the public attempt sequence and existing rip-up.
        if (state.sequence >= (1 << 30)) throw new IllegalStateException("F4 search stamp range exhausted");
        state.f4RetryStamp = Integer.MAX_VALUE - state.sequence;
        f4CpuProbePops += state.nodesPopped;
        f4CpuProbeNs += search.elapsedNs;
        nodesPopped.addAndGet(state.nodesPopped);
        state.nodesPopped = state.nodesPushed = 0;
        state.earlyTermination = false;
        RouteNode source = state.connection.getSourceRnode();
        source.clearPrev();
        push(state, source, 0f, 0f, false);
        return f4CpuSearch(state, tier, false);
    }


    private boolean v27RoundActive;
    private int[] v27NextActive;
    private int v27NextActiveCount = -1;
    private int v27OverusedCount;
    private int v27Excess;
    private int v27UnroutableCount;
    private long f4V37GpuCommits;
    private long f4V37GpuPops;
    private long f4V37GpuNs;
    private long f4V37GpuWaves;
    private F4FirstProgress f4First;

    private static final class F4FirstProgress {
        final long started = System.nanoTime();
        long nextReport = started;
        long attempts, parallelCommits, serialAttempts, groups, selected, farthest;
        long narrowed, narrowedPops, fullPops, droppedPops, outside, retries;
        long areaOriginal, areaEffective, coldBuilt, coldNs, conflict, invalidated;
        long privateCap, privateMiss, noIndependent, shape, routeNotNeeded;
        long regionIndexed, regionCells, regionPlanMs, regionEvicted, regionMaxWidth, regionMaxStaged;
        long regionSpecSelected, regionLeaves, ripFootprints, regionWaitNs, regionCommitNs, hotWidened, chainSerial, gpuBatches;
        long gpuTrials, gpuGainBatches, gpuCommitNs, cpuTailSamples;

        void report(int round, int position, int total, int inFlightPops, boolean force) {
            long now = System.nanoTime();
            if (!force && now < nextReport) return;
            nextReport = now + 15_000_000_000L;
            var row = new org.json.JSONObject();
            row.put("round", round).put("position", position).put("listed", total)
               .put("elapsed_ms", (now - started) / 1_000_000L)
               .put("attempts", attempts).put("parallel_commits", parallelCommits)
               .put("serial_attempts", serialAttempts).put("groups", groups)
               .put("selected", selected).put("farthest_lookahead", farthest)
               .put("narrowed", narrowed).put("narrow_pops", narrowedPops)
               .put("original_pops", fullPops).put("live_search_pops", inFlightPops)
               .put("dropped_private_pops", droppedPops).put("corridor_rejected_child_checks", outside)
               .put("original_retries", retries).put("original_box_tiles", areaOriginal)
               .put("effective_box_tiles", areaEffective).put("cold_adjacency_built", coldBuilt)
               .put("cold_adjacency_worker_ms", coldNs / 1_000_000L)
               .put("serial_no_independent_group", noIndependent)
               .put("serial_shape", shape).put("private_cap", privateCap).put("private_miss", privateMiss)
               .put("invalidate_after_ordered_write", invalidated).put("path_footprint_conflicts", conflict)
               .put("already_routed_skips", routeNotNeeded).put("indirect_gpu_batches", gpuBatches)
               .put("region_indexed", regionIndexed).put("region_cells", regionCells)
               .put("region_index_ms", regionPlanMs).put("region_evicted", regionEvicted)
               .put("region_max_search_width", regionMaxWidth).put("region_peak_staged", regionMaxStaged)
               .put("region_spec_selected", regionSpecSelected)
               .put("dependency_buckets",regionLeaves).put("old_path_footprints",ripFootprints)
               .put("group_wait_ms",regionWaitNs / 1_000_000L).put("ordered_commit_ms",regionCommitNs / 1_000_000L)
               .put("hot_widened",hotWidened).put("serial_chain",chainSerial)
               .put("gpu_trials_total",gpuTrials).put("gpu_net_gain_batches_total",gpuGainBatches)
               .put("gpu_proof_to_commit_ns",gpuCommitNs).put("critical_cpu_tail_samples",cpuTailSamples);
            System.out.println("F4_FIRST_PROGRESS " + row);
        }
    }

    private static final class F4FirstCandidate {
        final Connection connection;
        final F1Corridor.Box boundary;
        final F1Corridor.Box footprint;
        final int position;
        F4FirstCandidate(Connection connection, F1Corridor.Box boundary, int position) {
            this.connection = connection;
            this.boundary = boundary;
            this.footprint = boundary == null ? F1Corridor.Box.original(connection) : boundary;
            this.position = position;
        }
    }

    private static final class F4FirstPrivate {
        final F4SpatialResult result;
        final int pops;
        final long outside, coldBuilt, coldNs;
        final boolean capped;
        F4FirstPrivate(F4SpatialResult result, ConnectionState state, RouteNode.SearchScratch scratch) {
            this.result = result;
            this.pops = state.nodesPopped;
            this.outside = state.f4OutsideChecks;
            this.coldBuilt = scratch.f4ColdBuilt;
            this.coldNs = scratch.f4ColdNs;
            this.capped = state.nodesPopped >= state.f4PrivatePopCeiling;
        }
    }

    private int f4FirstPosition, f4FirstTotal;

    private boolean f4FirstShape(Connection c) {
        return !c.hasAltSinks() && c.getSourceRnode() != c.getSinkRnode()
                && c.getSinkRnode().getType().isAnyExclusiveSink();
    }

    private static boolean f4FirstOverlap(F1Corridor.Box a, F1Corridor.Box b) {
        return a.left < b.right && b.left < a.right && a.bottom < b.top && b.bottom < a.top;
    }

    private F4FirstPrivate f4FirstProbe(F4FirstCandidate c, int slot, int tier) {
        var state = new ConnectionState();
        f4FillHostState(state, c.connection, 1);
        // These disposable labels are never published; no connection can inherit them.
        state.f4RetryStamp = (1 << 30) + slot;
        state.f4Boundary = c.boundary;
        state.f4PrivateSearch = true;
        state.f4PrivatePopCeiling = routeIteration >= 3 ? 16384 : (c.connection.getHpwl() >= 16 ? (routeIteration >= 2 ? 32768 : 65536) : 16384);
        var cached = f4SpatialScratch.poll();
        final var scratch = cached == null ? new RouteNode.SearchScratch() : cached;
        scratch.f4OpenWarmPair();
        boolean detached = false;
        try {
            scratch.beginPrivateRipUp(c.connection.getNetWrapper());
            try {
                prepareRouteConnection(state);
            } finally {
                scratch.endPrivateRipUp();
            }
            F4CpuSearch search = f4CpuSearch(state, tier, false);
            if (search.target == null) return new F4FirstPrivate(null, state, scratch);
            var path = new ArrayList<RouteNode>();
            for (RouteNode node = search.target; node != null; node = node.getPrev()) {
                path.add(node);
                if (node == c.connection.getSourceRnode()) break;
                if (path.size() > state.nodesPopped + 1) return new F4FirstPrivate(null, state, scratch);
            }
            if (path.get(path.size() - 1) != c.connection.getSourceRnode())
                return new F4FirstPrivate(null, state, scratch);
            scratch.detachPair();
            detached = true;
            return new F4FirstPrivate(new F4SpatialResult(state, scratch, search, path, tier), state, scratch);
        } finally {
            if (!detached) {
                scratch.close();
                f4SpatialScratch.add(scratch);
            }
        }
    }

    private void f4FirstDiscard(F4SpatialResult r) {
        f4First.droppedPops += r.state.nodesPopped;
        r.scratch.f4DiscardDetached();
        f4SpatialScratch.add(r.scratch);
    }

    private void f4FirstInvalidate(Map<Integer, F4SpatialResult> ready, List<RouteNode> path) {
        if (path.isEmpty() || ready.isEmpty()) return;
        var it = ready.values().iterator();
        while (it.hasNext()) {
            var pending = it.next();
            if (!Collections.disjoint(path, pending.scratch.readNodes())
                    || !Collections.disjoint(path, pending.scratch.writeNodes())
                    || !Collections.disjoint(path, pending.path)) {
                ++f4First.invalidated;
                ++f4First.conflict;
                f4FirstDiscard(pending);
                it.remove();
            }
        }
    }

    private static final class F4HotPath {
        final int cell;
        final int streak;
        final int round;
        F4HotPath(int cell, int streak, int round) {
            this.cell = cell; this.streak = streak; this.round = round;
        }
    }

    private final java.util.IdentityHashMap<Connection, F4HotPath> f4HotPaths = new java.util.IdentityHashMap<>();
    private int f4HotGeneration;
    private int[] f4HotPressure = new int[1 << 16];
    private int[] f4HotStamp = new int[1 << 16];

    private static int f4HotCell(int x, int y) {
        int a = (x + 256) >> 4, b = (y + 256) >> 4;
        return (a | b) < 0 || a >= 256 || b >= 256 ? -1 : (a << 8) | b;
    }

    private void f4ReadCongestion() {
        ++f4HotGeneration;
        for (RouteNode n : overUsedRnodes) {
            int cell = f4HotCell(n.getEndTileXCoordinate(), n.getEndTileYCoordinate());
            if (cell < 0) continue;
            if (f4HotStamp[cell] != f4HotGeneration) {
                f4HotStamp[cell] = f4HotGeneration;
                f4HotPressure[cell] = 0;
            }
            f4HotPressure[cell] += Math.max(0, n.getOccupancy() - RouteNode.capacity);
        }
    }

    private int f4Pressure(int cell) {
        return cell >= 0 && f4HotStamp[cell] == f4HotGeneration ? f4HotPressure[cell] : 0;
    }

    private F4HotPath f4RecordHotPath(Connection c) {
        if (routeIteration < 2 || c.getRnodes().isEmpty()) return null;
        int hot = -1, pressure = 0;
        for (RouteNode node : c.getRnodes()) {
            if (!overUsedRnodes.contains(node)) continue;
            int cell = f4HotCell(node.getEndTileXCoordinate(), node.getEndTileYCoordinate());
            int inCell = f4Pressure(cell);
            if (inCell > pressure) { hot = cell; pressure = inCell; }
        }
        var prior = f4HotPaths.get(c);
        if (hot < 0) {
            if (prior != null) f4HotPaths.remove(c);
            return null;
        }
        int streak = prior != null && prior.round == routeIteration - 1 && prior.cell == hot
                ? Math.min(prior.streak + 1, 4) : 1;
        F4HotPath now = new F4HotPath(hot, streak, routeIteration);
        f4HotPaths.put(c, now);
        return now;
    }

    private F1Corridor.Box f4AdaptiveBoundParallel(Connection c, FidesLaneGate gate,
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

    private F1Corridor.Box f4AdaptiveBound(Connection c, FidesLaneGate gate) {
        if (f4RegionRetired != null && f4RegionRetired.contains(c)) return null;
        F1Corridor.Box preferred = gate.f4Boundary(c);
        if (preferred == null) return null;
        F1Corridor.Box base = F1Corridor.Box.original(c);
        var hot = f4HotPaths.get(c);
        if (hot == null || hot.round != routeIteration || hot.streak < 2) return preferred;
        // Keep bounded widening instead of collapsing to the full original box at streak >= 4;
        // genuine corridor exhaustion still falls back to the original box in f4SearchWithBoundary.
        int x = ((hot.cell >> 8) << 4) - 248;
        int y = ((hot.cell & 255) << 4) - 248;
        // A corridor that avoids the old hot tile is already doing its job.
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

    private static final int F4_MAX_GPU_TRIALS = 24;
    private static final int F4_GPU_TRIAL = 1;
    private static final int F4_GPU_NET_GAIN = 2;

    private static final class F4CriticalCost {
        long cpuNs, gpuNs, roundCrossings;
        int cpuSamples, gpuSamples, offers, gpuTrials;

        long cpuMean() { return cpuNs / Math.max(cpuSamples, 1); }
        long gpuMean() { return gpuNs / Math.max(gpuSamples, 1); }

        void recordCpu(long elapsed) {
            if (cpuSamples == 32) cpuNs -= cpuMean();
            else ++cpuSamples;
            cpuNs += elapsed;
        }

        void recordGpu(long elapsed) {
            if (gpuSamples == 16) gpuNs -= gpuMean();
            else ++gpuSamples;
            gpuNs += elapsed;
        }
    }

    // CPU tails are from this round, indexed by service, HPWL and boundary kind.
    private final F4CriticalCost[] f4CriticalCosts = new F4CriticalCost[18];
    private int f4CurrentRegionRound, f4GpuTrials;
    private long f4CriticalTailSamples, f4CriticalStarts, f4CriticalGpuNs, f4CriticalProofPops;
    private long f4GpuColdActual, f4GpuLearningBalance, f4GpuGained;
    private boolean f4GpuCriticalResident;
    private boolean f4LatestWarmGain;

    boolean f4ResidentGrowthEarned() {
        return f4GpuGained > 0 && f4LatestWarmGain;
    }

    private void f4StartCriticalRound() {
        f4CurrentRegionRound = routeIteration;
        f4CriticalTailSamples = f4CriticalStarts = f4CriticalGpuNs = f4CriticalProofPops = 0;
        for (var cost : f4CriticalCosts) {
            if (cost == null) continue;
            cost.cpuNs = cost.roundCrossings = 0;
            cost.cpuSamples = cost.offers = 0;
        }
    }

    private F4CriticalCost f4CriticalCost(Connection c, F1Corridor.Box boundary, int tier, boolean chain) {
        if (!chain || tier < 0 || f4FloodReleased || (c.isCrossSLR() && boundary == null)) return null;
        int hpwl = c.getHpwl();
        int size = hpwl < 64 ? 0 : hpwl < 256 ? 1 : 2;
        int key = (tier * 3 + size) * 2 + (boundary == null ? 0 : 1);
        var cost = f4CriticalCosts[key];
        if (cost == null) f4CriticalCosts[key] = cost = new F4CriticalCost();
        return cost;
    }

    private int f21HeavyGeometry, f21RareGeometry;
    private final long[] f21PreviousProofs = new long[3];
    private final long[] f21PreviousSupply = new long[3];
    private final long[] f21Proofs = new long[3];
    private final long[] f21EstimatedProofs = new long[3];
    private final long[] f21GeometrySupply = new long[3];
    private int f21PreviousOverused = -1;
    private long f21MinimumWarmNs = Long.MAX_VALUE;

    private int f21Geometry(Connection c, F1Corridor.Box bound) {
        if (f4Direct == null) return -1;
        F1Corridor.Box box = bound == null ? F1Corridor.Box.original(c) : bound;
        long work = f4Direct.f21EffectiveWork(c, box.right - box.left, box.top - box.bottom);
        return work <= 0 ? -1 : 63 - Long.numberOfLeadingZeros(work);
    }

    private static int f21Quantile(int[] histogram, int count, int percent) {
        if (count == 0) return Integer.MAX_VALUE;
        long rank = ((long) count * percent + 99) / 100;
        for (int bucket = 0; bucket < histogram.length; ++bucket) {
            rank -= histogram[bucket];
            if (rank <= 0) return bucket;
        }
        return Integer.MAX_VALUE;
    }

    private int f21Service(Connection c, FidesLaneGate gate, int geometry) {
        int original = f4Service(c, gate);
        if (original < 0 || geometry < 0) return original;
        if (original == 2 && geometry >= f21RareGeometry) return 2;
        if (c.isCrossSLR() || geometry >= f21HeavyGeometry) return 1;
        return 0;
    }

    private void f21Stage(F4RegionQueue queue) {
        for (int tier = 0; tier < f21Proofs.length; ++tier) {
            f21Proofs[tier] = 0;
            f21GeometrySupply[tier] = queue.supply[tier];
            f21EstimatedProofs[tier] = f21PreviousSupply[tier] == 0 ? 0
                    : Math.round((double) f21PreviousProofs[tier] * queue.supply[tier] / f21PreviousSupply[tier]);
        }
        var row = new org.json.JSONObject().put("round", routeIteration)
                .put("active", queue.active).put("gpu_feature_rows", queue.featured)
                .put("geometry_supply_by_tier", new org.json.JSONArray(f21GeometrySupply))
                .put("previous_proofs_by_tier", new org.json.JSONArray(f21PreviousProofs))
                .put("forecast_proofs_by_tier", new org.json.JSONArray(f21EstimatedProofs))
                .put("previous_overused", f21PreviousOverused)
                .put("round_start_overused", overUsedRnodes.size())
                .put("heavy_geometry_bin", f21HeavyGeometry).put("rare_geometry_bin", f21RareGeometry);
        System.out.println("F21_STAGE " + row);
    }

    private void f21FinishStage() {
        for (int tier = 0; tier < f21Proofs.length; ++tier) {
            f21PreviousProofs[tier] = f21Proofs[tier];
            f21PreviousSupply[tier] = f21GeometrySupply[tier];
        }
        f21PreviousOverused = overUsedRnodes.size();
        System.out.println("F21_STAGE_RESULT " + new org.json.JSONObject()
                .put("round", routeIteration).put("overused_before_round_update", f21PreviousOverused)
                .put("proofs_by_tier", new org.json.JSONArray(f21Proofs)));
    }

    private int f21CriticalMode(F4CriticalCost cost, int tier) {
        if (cost == null) return 0;
        // Price a plausible heavy member first. Fast proof traffic contributes
        // normal CPU controls until its measured tail can beat a real warm cost.
        if (!f4GpuCriticalResident) {
            boolean heavySupply = f21EstimatedProofs[tier] > 0 || f21GeometrySupply[tier] > 0;
            return tier > 0 && heavySupply && f4GpuTrials < F4_MAX_GPU_TRIALS ? F4_GPU_TRIAL : 0;
        }
        if (cost.cpuSamples < 2 || (++cost.offers & 7) == 0) return 0;
        if (cost.gpuSamples < 2) {
            if (cost.gpuTrials >= 3 || f4GpuTrials >= F4_MAX_GPU_TRIALS) return 0;
            long warmFloor = f21MinimumWarmNs == Long.MAX_VALUE
                    ? 4_000_000L : Math.max(3_000_000L, Math.min(8_000_000L, f21MinimumWarmNs));
            if (tier == 0 && cost.cpuMean() <= warmFloor + warmFloor / 4) return 0;
            if (tier > 0 && f21EstimatedProofs[tier] == 0 && f21Proofs[tier] < 2) return 0;
            return F4_GPU_TRIAL;
        }
        // The graph is already resident. Historical trials stay on the report;
        // they do not change the cost of submitting the next proven member.
        return cost.cpuMean() - cost.gpuMean() > cost.gpuMean() / 8 ? F4_GPU_NET_GAIN : 0;
    }

    private void f4RecordCriticalTail(F4CriticalCost cost, F4CpuSearch search) {
        if (cost == null || search.crossingNs < 0 || search.target == null) return;
        long tail = search.elapsedNs - search.crossingNs;
        if (tail <= 0) return;
        cost.recordCpu(tail);
        ++cost.roundCrossings;
        ++f4CriticalTailSamples;
        f4First.cpuTailSamples = f4CriticalTailSamples;
    }

    private void f4RecordCriticalGpu(F4CriticalCost cost, int mode, int tier, int sequence,
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
    }

    private static final int F4_REGION_SHIFT = 4;
    private static final int F4_REGION_PENDING = 128;
    private static final int F4_REGION_NEAR = 65536;

    // All coordinates are on the same raw tile system as the authoritative CPU bound.
    private static F1Corridor.Box f4RegionEnvelope(Connection c) {
        F1Corridor.Box base = F1Corridor.Box.original(c);
        if (!c.getRnodes().isEmpty() || c.hasAltSinks() || c.getSourceRnode() == c.getSinkRnode()
                || !c.getSinkRnode().getType().isAnyExclusiveSink()) return base;
        RouteNode s = c.getSourceRnode(), t = c.getSinkRnode();
        int sx = s.getEndTileXCoordinate(), sy = s.getEndTileYCoordinate();
        int tx = t.getEndTileXCoordinate(), ty = t.getEndTileYCoordinate();
        if (!base.contains(sx, sy) || !base.contains(tx, ty)) return base;
        int hp = c.getHpwl();
        int margin = hp < 16 ? 0 : Math.max(3, Math.min(12, hp / 12));
        boolean cross = c.isCrossSLR();
        int left = cross ? base.left : Math.max(base.left, Math.min(sx, tx) - margin - 1);
        int right = cross ? base.right : Math.min(base.right, Math.max(sx, tx) + margin + 1);
        int bottom = Math.max(base.bottom, Math.min(sy, ty) - margin - 1);
        int top = Math.min(base.top, Math.max(sy, ty) + margin + 1);
        return new F1Corridor.Box(left, bottom, right, top);
    }

    private F1Corridor.Box f4RegionBound(Connection c, FidesLaneGate gate) {
        if (f4RegionRetired != null && f4RegionRetired.contains(c)) return null;
        F1Corridor.Box preferred = gate.f4Boundary(c);
        if (!f4FirstShape(c)) return preferred;
        F1Corridor.Box base = F1Corridor.Box.original(c);
        F1Corridor.Box envelope = f4RegionEnvelope(c);
        // A cross-SLR corridor has portal bands; do not remove them.
        if (c.isCrossSLR()) return preferred;
        F1Corridor.Box effective = preferred == null ? envelope : new F1Corridor.Box(
                Math.max(preferred.left, envelope.left), Math.max(preferred.bottom, envelope.bottom),
                Math.min(preferred.right, envelope.right), Math.min(preferred.top, envelope.top));
        RouteNode s = c.getSourceRnode(), t = c.getSinkRnode();
        return !effective.same(base) && effective.contains(s) && effective.contains(t) ? effective : preferred;
    }

    private java.util.Set<Connection> f4RegionRetired;
    private java.util.Set<Connection> f22SkipPrivatePrev = java.util.Collections.emptySet();
    private java.util.Set<Connection> f22SkipPrivateNext = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());

    /**
     * Split the actual search/read and rip-up footprints. A cut yields a left
     * region, a right region and a set of jobs touching the boundary.
     */
    private static final class F21Partition {
        final int[] left, right, down, up, ids, scratch, home;
        int count;

        F21Partition(int[] left, int[] right, int[] down, int[] up, int[] ids, int[] home) {
            this.left = left;
            this.right = right;
            this.down = down;
            this.up = up;
            this.ids = ids;
            this.home = home;
            this.scratch = new int[ids.length];
        }

        void divide(int from, int to) {
            int n = to - from;
            int bestCost = Integer.MAX_VALUE, bestCut = 0, nLow = 0, nHigh = 0;
            boolean bestX = true;
            if (n > 16) {
                for (int axis = 0; axis != 2; ++axis) {
                    int[] first = axis == 0 ? left : down;
                    int[] last = axis == 0 ? right : up;
                    int min = Integer.MAX_VALUE, max = Integer.MIN_VALUE;
                    for (int j = from; j < to; ++j) {
                        int id = ids[j];
                        min = Math.min(min, first[id]);
                        max = Math.max(max, last[id]);
                    }
                    if (min == max) continue;
                    int[] starts = new int[max - min + 1];
                    int[] ends = new int[starts.length];
                    for (int j = from; j < to; ++j) {
                        ++starts[first[ids[j]] - min];
                        ++ends[last[ids[j]] - min];
                    }
                    for (int x = min, lo = 0, hi = n; x < max; ++x) {
                        lo += ends[x - min];
                        hi -= starts[x - min];
                        if (lo == 0 || hi == 0) continue;
                        int cost = 4 * (n - lo - hi) + Math.abs(lo - hi);
                        if (cost < bestCost) {
                            bestX = axis == 0;
                            bestCut = x;
                            bestCost = cost;
                            nLow = lo;
                            nHigh = hi;
                        }
                    }
                }
            }
            if (nLow == 0) {
                int leaf = count++;
                for (int i = from; i < to; ++i) home[ids[i]] = leaf;
                return;
            }
            int middle = from + nLow, upper = to - nHigh;
            int lo = from, cross = middle, hi = upper;
            int[] first = bestX ? left : down;
            int[] last = bestX ? right : up;
            for (int j = from; j < to; ++j) {
                int id = ids[j];
                scratch[last[id] <= bestCut ? lo++ : first[id] > bestCut ? hi++ : cross++] = id;
            }
            System.arraycopy(scratch, from, ids, from, n);
            divide(from, middle);
            if (middle < upper) divide(middle, upper);
            divide(upper, to);
        }
    }

    private final class F4RegionQueue {
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
    }

    private void f4RegionFill(F4RegionQueue queue, List<Connection> all, int i, FidesLaneGate gate,
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
    }

    private void f4FirstBoxTally(Connection c, F1Corridor.Box boundary) {
        long original = F1Corridor.Box.original(c).area();
        if (original < 0) return;
        f4First.areaOriginal += original;
        f4First.areaEffective += boundary == null ? original : boundary.area();
        if (boundary != null) {
            ++f4First.narrowed;
            ++f4Narrow;
        }
    }

    private void f4FlushFirstGpuBatch(List<LaneBatchSlot> f4Batch, List<ConnectionState> f4BatchStates,
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

    private void f4RouteV27Round(List<Connection> all, FidesLaneGate gate) {
        final int nAll = all.size();
        final int activeCount = (routeIteration == 1 || v27NextActiveCount < 0) ? nAll : v27NextActiveCount;
        if (activeCount <= 0) {
            v27OverusedCount = 0;
            v27Excess = 0;
            v27UnroutableCount = 0;
            return;
        }
        final int[] activeIdx = new int[activeCount];
        if (routeIteration == 1 || v27NextActiveCount < 0) {
            for (int k = 0; k < activeCount; ++k) activeIdx[k] = k;
        } else {
            System.arraycopy(v27NextActive, 0, activeIdx, 0, activeCount);
        }

        final int[] bx0 = new int[activeCount];
        final int[] by0 = new int[activeCount];
        final int[] bx1 = new int[activeCount];
        final int[] by1 = new int[activeCount];
        final byte[] buse = new byte[activeCount];

        long prepStarted = System.nanoTime();
        if (config.isUseBoundingBox()) {
            gate.ensureF4PortalsIndexed();
            final List<Connection> activeList = (activeCount == nAll) ? all : new ArrayList<>(activeCount);
            if (activeCount != nAll) {
                for (int k = 0; k < activeCount; ++k) activeList.add(all.get(activeIdx[k]));
            }
            final F1Corridor.ParallelEntry[] entries = new F1Corridor.ParallelEntry[activeCount];
            final int workers = Math.max(1, f4SharedPool.getParallelism());
            if (workers >= 2 && activeCount >= 4096) {
                int chunk = Math.max(2048, (activeCount + workers * 4 - 1) / (workers * 4));
                var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                for (int from = 0; from < activeCount; from += chunk) {
                    final int f = from, t = Math.min(activeCount, from + chunk);
                    jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                        for (int k = f; k < t; ++k) {
                            Connection c = activeList.get(k);
                            F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);
                            if (b != null) {
                                bx0[k] = b.left;
                                by0[k] = b.bottom;
                                bx1[k] = b.right;
                                by1[k] = b.top;
                                buse[k] = 1;
                            }
                        }
                    }, f4SharedPool));
                }
                java.util.concurrent.CompletableFuture.allOf(
                        jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
            } else {
                for (int k = 0; k < activeCount; ++k) {
                    Connection c = activeList.get(k);
                    F1Corridor.Box b = gate.f4BoundaryParallel(c, entries, k);
                    if (b != null) {
                        bx0[k] = b.left;
                        by0[k] = b.bottom;
                        bx1[k] = b.right;
                        by1[k] = b.top;
                        buse[k] = 1;
                    }
                }
            }
            gate.commitF4BoundaryParallel(activeList, entries);
        }
        double corridorSec = (System.nanoTime() - prepStarted) / 1e9;

        gate.f4NativeUpdatePcc(routingGraph.presentCongestionCosts);
        final float rnodeCostWeight = 1.0f;
        final float shareWeight = (float) Math.pow(rnodeCostWeight, config.getShareExponent());
        final float rnodeWLWeight = oneMinusWlWeight;
        final float estWlWeight = wlWeight;
        final int threads = Math.max(1, f4SharedPool.getParallelism() + 1);
        final long[] stats = new long[18];

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
        long sameNetOk = stats[13];
        long gpuCommits = stats[14];
        long gpuPops = stats[15];
        long gpuNs = stats[16];
        long gpuWaves = stats[17];
        f4V37GpuCommits += gpuCommits;
        f4V37GpuPops += gpuPops;
        f4V37GpuNs += gpuNs;
        f4V37GpuWaves += gpuWaves;

        connectionsRouted.addAndGet(routed);
        connectionsRoutedThisIteration.addAndGet(routed);
        nodesPopped.addAndGet(roundPops);
        f4CpuProbeAttempts += routed;
        f4CpuLocalCompleted += routed;
        f4CpuLocalCommits += routed;
        f4CpuProbePops += roundPops;
        f4CpuProbeNs += elapsedNs;

        System.out.printf(
                "F4_V37_ROUND iter=%d active=%d routed=%d skip=%d gpu_ok=%d cpu_ok=%d par=%d ser=%d retries=%d same_ok=%d base_h=%d rel_h=%d fb=%d unrout=%d over=%d excess=%d pops=%d gpu_pops=%d waves=%d corridor_s=%.2f gpu_s=%.2f cpp_s=%.2f%n",
                routeIteration, activeCount, routed, skippedLive, gpuCommits, routed - gpuCommits,
                parCommits, serCommits, retries, sameNetOk,
                baseHeavy, relaxedHeavy, fallbacks, v27UnroutableCount, v27OverusedCount, v27Excess,
                roundPops, gpuPops, gpuWaves,
                corridorSec, gpuNs / 1e9, elapsedNs / 1e9);
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
    }


    private void f4RouteDiverted(Collection<Connection> connections) {
        final List<LaneBatchSlot> batch = new ArrayList<>(f4Width);
        final List<ConnectionState> slotStates = new ArrayList<>(f4Width);
        final FidesLaneGate gate = getFidesLaneGate();
        // The decision is frozen for the round; a device starts only on the first proven member.
        final boolean armed = f4Diversion.admits();
        List<Connection> ordered = connections instanceof List
                ? (List<Connection>) connections : new ArrayList<>(connections);
        if (v27RoundActive) {
            f4RouteV27Round(ordered, gate);
            return;
        }
        if (!routingGraph.isVersal
                && config.isUseBoundingBox() && !config.isTimingDriven()) {
            f4RouteFirstRound(ordered, gate);
            return;
        }
        f4NextSpatial = 0;
        for (int position = 0; position < ordered.size(); ++position) {
            Connection connection = ordered.get(position);
            if (!batch.isEmpty() && (batch.size() == f4Width
                    || batch.get(0).connection.getNetWrapper() != connection.getNetWrapper())) {
                runAndCommitF4Batch(batch);
            }
            if (!shouldRoute(connection)) continue;
            if (batch.isEmpty()) {
                int reused = f4SpatialRoute(ordered, position, armed, gate);
                if (reused != 0) {
                    position += reused - 1;
                    continue;
                }
            }
            F1Corridor.Box boundary = gate.f4Boundary(connection);
            // A terminal phase cannot re-admit the device. Skip service accounting there.
            int tier = f4FloodReleased ? -1 : f4Service(connection, gate);
            int privatePops = 0;
            if (!batch.isEmpty()) {
                if (tier >= 0 && f4Diversion.worth(tier, batch.size() + 1))
                    privatePops = f4PrivatePendingProof(connection, tier, batch, boundary);
                if (privatePops == 0) {
                    runAndCommitF4Batch(batch);
                    if (!shouldRoute(connection)) continue;
                }
            }
            ConnectionState state = slotState(slotStates, batch.size());
            f4FillHostState(state, connection, connectionsRouted.incrementAndGet());
            state.f4Boundary = boundary;
            if (boundary != null) ++f4Narrow;
            connectionsRoutedThisIteration.incrementAndGet();
            gate.mirrorBeforeAttempt(connection);
            prepareRouteConnection(state);
            if (tier >= 0 && !gate.f4Supported(connection, state)) tier = -1;
            if (privatePops > 0) {
                if (tier < 0) throw new IllegalStateException("F4 prepared target changed after private budget proof");
                f4CollectProven(state, F4RoundDiversion.budget(tier), privatePops, batch);
                continue;
            }
            boolean offer = armed && tier >= 0 && f4Diversion.worth(tier, 1);
            F4CpuSearch result = f4SearchWithBoundary(state, tier, offer);
            boolean handoff = offer && result.target == null && result.crossingNs >= 0
                    && F4RoundDiversion.crossed(state.nodesPopped, F4RoundDiversion.budget(tier),
                            !state.queue.isEmpty());
            int popped = state.nodesPopped;
            f4ObserveCpu(state, tier, result, handoff);
            if (handoff) f4CollectProven(state, F4RoundDiversion.budget(tier), popped, batch);
            else f4FinishCpu(state, result.target, gate);
        }
        if (!batch.isEmpty()) runAndCommitF4Batch(batch);
    }

    org.json.JSONObject f4CpuReceipt() {
        return new org.json.JSONObject().put("cpu_local_completed", f4CpuLocalCompleted)
                .put("cpu_local_commits", f4CpuLocalCommits)
                .put("cpu_probe_attempts", f4CpuProbeAttempts).put("cpu_probe_pops", f4CpuProbePops)
                .put("cpu_probe_ns", f4CpuProbeNs)
                .put("v37_gpu_commits", f4V37GpuCommits)
                .put("v37_gpu_pops", f4V37GpuPops)
                .put("v37_gpu_ns", f4V37GpuNs)
                .put("v37_gpu_waves", f4V37GpuWaves);
    }

    private void f4RoundBoundary(int unroutable) {
        int excess = 0;
        int overCount = v27RoundActive ? v27OverusedCount : overUsedRnodes.size();
        if (v27RoundActive) {
            excess = v27Excess;
        } else {
            for (RouteNode node : overUsedRnodes) excess += node.getOccupancy() - RouteNode.capacity;
        }
        var decision = f4Diversion.endRound(routeIteration, excess, overCount, unroutable,
                connectionsRoutedThisIteration.get(), f4CpuProbePops - f4PreviousRoundCpuPops);
        f4PreviousRoundCpuPops = f4CpuProbePops;
        decision.put("corridor_searches", f4Narrow).put("original_box_retries", f4Widen)
                .put("spatial_groups", f4SpatialGroups).put("spatial_commits", f4SpatialCommits)
                .put("spatial_partial_group_ns", f4SpatialWasteNs);
        f4Narrow = f4Widen = f4SpatialGroups = f4SpatialCommits = f4SpatialWasteNs = 0;
        if (f4CurrentRegionRound == routeIteration) {
            decision.put("critical_observed_cpu_tails",f4CriticalTailSamples)
                    .put("critical_startup_trial_balance_ns",f4GpuLearningBalance)
                    .put("critical_gpu_trials_total",f4GpuTrials)
                    .put("critical_gpu_net_gain_batches_total",f4GpuGained)
                    .put("critical_gpu_actual_batches",f4CriticalStarts)
                    .put("critical_gpu_wall_ns",f4CriticalGpuNs)
                    .put("critical_gpu_width",f4CriticalStarts == 0 ? 0 : 1)
                    .put("critical_gpu_first_complete_ns",f4GpuColdActual)
                    .put("critical_proof_pops",f4CriticalProofPops);
        }
        System.out.println("F4_PPT11_ROUND " + decision);
        String next = decision.getString("next");
        if (next.equals("STOP") || next.equals("DRAIN")) f4ReleaseFlood();
    }

    private void f4WriteCpuFinal() {
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


    public void routeIndirectConnectionsIteratively() {
        f4Direct = F4FeatureDirect.open(config);
        if (f4Direct != null) {
            f4Width = Integer.getInteger("fides.ppt.f4.lanes", 2);
            if (f4Width < 2 || f4Width > 8) throw new IllegalArgumentException("F4 GPU channels must be 2 to 8");
            var quote = java.nio.file.Path.of(System.getProperty("fides.ppt.f4.work"), "gpu_quote.json");
            try {
                f4Diversion = new F4RoundDiversion(f4Width,
                        Long.getLong("fides.ppt.f4.inputBytes"),
                        new org.json.JSONObject(java.nio.file.Files.readString(quote)));
            } catch (java.io.IOException error) {
                throw new java.io.UncheckedIOException("Cannot read the GPU round quote", error);
            }
        }
        //String goldenPath = System.getProperty("rapidwright.rwroute.goldenFile");
        String goldenPath = config.getGoldenFile();
	if (goldenPath != null) {
            try {
                goldenWriter = new java.io.PrintWriter(new java.io.FileWriter(goldenPath));
            } catch (java.io.IOException e) {
                throw new RuntimeException(e);
            }
        }

	// print config info
        System.out.println("INFO: Routing indirect connections iteratively");
        System.out.println("      Max iterations: " + config.getMaxIterations());
        System.out.println("      Timing driven: " + config.isTimingDriven());
        System.out.println("      LUT pin swapping: " + lutPinSwapping);
        // Bounding box extension
        float boundingBoxExtensionX = config.getBoundingBoxExtensionX();
        float boundingBoxExtensionY = config.getBoundingBoxExtensionY();
        System.out.println("      Bounding box extension: " + boundingBoxExtensionX + " " + boundingBoxExtensionY);
        // Present congestion factor
        float initialPresentCongestionFactor = config.getInitialPresentCongestionFactor();
        System.out.println("      Initial present congestion factor: " + initialPresentCongestionFactor);
        
        sortConnections();
        initializeRouting();
        if (config.getFidesRrgDumpPrefix() != null) {
            FidesRrgFlattener.flattenAndDump(routingGraph, config.getFidesRrgDumpPrefix(), presentCongestionFactor);
        }
        if (config.isFidesLaneMirror()) {
            /* The mirror's initial fill, and the reason it is HERE rather than at the top of the run:
             * connection setup has already called incrementUser on the sink of every connection
             * without alternative sinks (RWRoute.java:800), and that batch has no event behind it.
             * Filling here makes it part of the initial state; filling earlier would make the mirror
             * low by one on those sinks with nothing left to tell it so.  It is also the instant the
             * flattener above dumps the artifact, which is what makes the three-way reconciliation in
             * mirrorInit() a comparison of one state rather than of two.
             *
             * Note this is a SECOND trigger point: --fidesLaneAttempt still decides which connection
             * the lane searches, but the mirror is maintained for the whole run either way. */
            getFidesLaneGate().mirrorInit();
            getFidesLaneGate().f4NativeInit(System.getProperty("fides.ppt.f4.work"), routingGraph, config);
            v27RoundActive = getFidesLaneGate().f4NativeRegisterAll(sortedIndirectConnections, config);
        }
        long lastIterationRnodeCount = routingGraph.numNodes();
        long lastIterationRnodeTime = 0;

        boolean initialHus = this.hus;
        final FidesPhaseTimer phaseTimer = config.isFidesLane() ? FidesPhaseTimer.open() : null;
        try {
        while (routeIteration < config.getMaxIterations()) {
            if (f4Direct != null) f4Direct.report("F4_DIRECT_ROUND_" + routeIteration);
            if (phaseTimer != null) phaseTimer.beginIteration(routeIteration);
            long start = RuntimeTracker.now();
            connectionsRoutedThisIteration.set(0);
            if (config.isTimingDriven()) {
                setRerouteCriticality();
            }
            try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.NEGOTIATE)) {
                routingGraph.updatePresentCongestionCosts(presentCongestionFactor);
            }
            routeIndirectConnections(sortedIndirectConnections);
            rnodesTimer.setTime(routingGraph.getCreateRnodeTime());

            try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.NEGOTIATE)) {
                updateCostFactors();
            }

            if (config.isFidesLaneMirror()) {
                /* The per-iteration half of the mirror.  updateCost() has just cleared and refilled
                 * overUsedRnodes and written historicalCongestionCost on exactly those nodes with
                 * exactly this factor, so handing both in makes the mirror's copy a reproduction of
                 * the reference's rule -- (occupancy - capacity) * factor, computed from the mirror's
                 * own occupancy -- rather than a second opinion about which nodes to touch.  The full
                 * scan that follows is where a delta that was never applied at all would show up.
                 *
                 * M6-A hands in the iteration number and the connection count as well, so that the
                 * witness line the gate reads from the other side of this call can be checked against
                 * THIS loop's own printed iteration table instead of taken on this file's word.  The
                 * two are written by different code, which is the whole point of passing them in. */
                getFidesLaneGate().mirrorIterationBoundary(routeIteration, overUsedRnodes,
                        historicalCongestionFactor, connectionsRoutedThisIteration.get());
            }
            rnodesCreatedThisIteration = routingGraph.numNodes() - lastIterationRnodeCount;
            List<Connection> unroutableConnections = v27RoundActive ? java.util.Collections.emptyList() : getUnroutableConnections();
            if (f4Diversion != null) f4RoundBoundary(v27RoundActive ? v27UnroutableCount : unroutableConnections.size());
            boolean needsResorting = false;
            if (!v27RoundActive) {
                for (Connection connection : unroutableConnections) {
                    System.out.printf("CRITICAL WARNING: Unroutable connection in iteration #%d\n", routeIteration);
                    System.out.println("                 " + connection);
                    needsResorting = handleUnroutableConnection(connection) || needsResorting;
                }
                for (Connection connection : getCongestedConnections()) {
                    needsResorting = handleCongestedConnection(connection) || needsResorting;
                }
            }
            if (needsResorting) {
                sortConnections();
            }

            if (config.isTimingDriven()) {
                updateTiming();
            }

            long elapsed = RuntimeTracker.elapsed(start);
            printRoutingIterationStatisticsInfo(elapsed, (float) ((rnodesTimer.getTime() - lastIterationRnodeTime) * 1e-9));
            if (phaseTimer != null) phaseTimer.endIteration(connectionsRoutedThisIteration.get());

            boolean v27Done = v27RoundActive ? (v27OverusedCount == 0 && v27UnroutableCount == 0) : (overUsedRnodes.isEmpty() && unroutableConnections.isEmpty());
            if (v27Done) {
                break;
            }
            if (!v27RoundActive && overUsedRnodes.isEmpty()) {
                if (unroutableConnections.isEmpty()) {
                    break;
                } else {
                    if (routeIteration == config.getMaxIterations() - 1) {
                        System.err.println("ERROR: Unroutable connections: " + unroutableConnections.size());
                    }
                }
            }

            if (initialHus && !hus) {
                System.out.println("INFO: Hybrid Updating Strategy (HUS) activated");
                initialHus = false;
            }

            routeIteration++;
            lastIterationRnodeCount = routingGraph.numNodes();
            lastIterationRnodeTime = rnodesTimer.getTime();
        }
        } finally {
            if (v27RoundActive) {
                getFidesLaneGate().f4NativeMaterializeAll(sortedIndirectConnections, this, v27OverusedCount, overUsedRnodes);
                getFidesLaneGate().f4SyncMirrorConnections(connectionsRouted.get());
                v27RoundActive = false;
            }
            if (phaseTimer != null) phaseTimer.close();
        }
        if (routeIteration == config.getMaxIterations()) {
            System.out.println("\nERROR: Routing terminated after " + (routeIteration -1 ) + " iterations.");
            System.out.println("       Unroutable connections: " + getUnroutableConnections().size());
            System.out.println("       Conflicting nodes: " + overUsedRnodes.size());
            for (RouteNode rnode : overUsedRnodes) {
                System.out.println("              " + rnode);
            }
        }

        if (config.getFidesRrgDumpPrefix() != null) {
            // M3-step2 S0-b: if this equals the post-expansion count, the graph stayed frozen
            // for the whole run (no rnode was created after Phase 1).
            System.out.println("INFO: [FIDES RRG] rnodes at end of routing = " + routingGraph.numNodes());
        }
    }

    /**
     * Gets unrouted connections.
     * @return A list of unrouted connections.
     */
    private List<Connection> getUnroutableConnections() {
        List<Connection> unroutedConnections = new ArrayList<>();
        for (Connection connection : sortedIndirectConnections) {
            if (!connection.isRouted()) {
                unroutedConnections.add(connection);
            }
        }
        return unroutedConnections;
    }

    private List<Connection> getCongestedConnections() {
        List<Connection> congestedConnections = new ArrayList<>();
        for (Connection connection : sortedIndirectConnections) {
            if (connection.isCongested()) {
                congestedConnections.add(connection);
            }
        }
        return congestedConnections;
    }

    /**
     * Assigns a list of nodes to each connection and fix net routes if there are cycles and / or multi-driver nodes.
     */
    protected void postRouteProcess() {
        if (routeIteration > config.getMaxIterations()) {
            return;
        }
        long tPost0 = System.nanoTime();

        // Perform LUT pin mapping updates
        if (lutPinSwapping &&
                !Boolean.getBoolean("rapidwright.rwroute.lutPinSwapping.deferIntraSiteRoutingUpdates")) {
            Map<SitePinInst, String> pinSwaps = new HashMap<>();
            for (Connection connection: indirectConnections) {
                SitePinInst oldSinkSpi = connection.getSink();
                if (!oldSinkSpi.isLUTInputPin() || !oldSinkSpi.isRouted()) {
                    continue;
                }

                List<RouteNode> rnodes = connection.getRnodes();
                RouteNode newSinkRnode = rnodes.get(0);
                if (newSinkRnode == connection.getSinkRnode()) {
                    continue;
                }
                connection.setSinkRnode(newSinkRnode);

                SitePin newSitePin = newSinkRnode.getSitePin();
                String existing = pinSwaps.put(oldSinkSpi, newSitePin.getPinName());
                assert(existing == null);
            }
            LUTTools.swapMultipleLutPins(pinSwaps);
        }
        long tLut1 = System.nanoTime();

        assignNodesToConnections();
        long tAssign1 = System.nanoTime();

        // fix routes with cycles and / or multi-driver nodes
        List<NetWrapper> fixedRoutes = fixRoutes();
        if (config.isTimingDriven()) {
            updateTimingAfterFixingRoutes(fixedRoutes);
        }
        long tFix1 = System.nanoTime();

        // Unset the routed state of all source pins (avoid Arrays.asList churn across 901k nets / 4.9M conns)
        for (Map.Entry<Net, NetWrapper> e : nets.entrySet()) {
            Net net = e.getKey();
            SitePinInst source = net.getSource();
            SitePinInst altSource = net.getAlternateSource();
            SiteInst si = source.getSiteInst();
            boolean altSourcePreviouslyRouted = altSource != null && altSource.isRouted();
            assert(source != null);
            boolean sourcePreviouslyRouted = source.isRouted();
            source.setRouted(false);
            assert(source.getSiteInst() == si);
            if (altSource != null) {
                altSource.setRouted(false);
                assert(altSource.getSiteInst() == si);
            }
            Node srcConnNode = source.getConnectedNode();
            Node altConnNode = (altSource != null) ? altSource.getConnectedNode() : null;

            // Set the routed state on those source pins that were actually used
            NetWrapper netWrapper = e.getValue();
            for (Connection connection : netWrapper.getConnections()) {
                List<Node> nodes = connection.getNodes();
                if (nodes == null || nodes.isEmpty()) {
                    continue;
                }

                Node sourceNode = nodes.get(nodes.size() - 1);
                SitePinInst usedSpi = null;
                if (sourceNode.equals(srcConnNode)) {
                    usedSpi = source;
                }
                if (altConnNode != null && sourceNode.equals(altConnNode)) {
                    usedSpi = altSource;
                }
                if (usedSpi == null) {
                    throw new RuntimeException("ERROR: Unknown source node " + sourceNode + " on net " + net.getName());
                }

                usedSpi.setRouted(true);
                if (si.getSitePinInst(usedSpi.getName()) == null) {
                    si.addPin(usedSpi);
                }

                if (source.isRouted() && (altSource == null || altSource.isRouted())) {
                    break;
                }
            }

            // If the alt source was previously routed, and is no longer, let's remove it
            if (altSource != null && altSourcePreviouslyRouted && !altSource.isRouted()) {
                // If altSource is not routed, then source must be routed
                assert(source.isRouted());
                altSource.getSiteInst().removePin(altSource);
                net.removePin(altSource, true);
                assert(source.isRouted());
                if (source.getName().endsWith("MUX")) {
                    assert(altSource.getName().endsWith("_O"));
                    // Add site routing back if we are keeping the MUX pin
                    si.routeIntraSiteNet(net, altSource.getBELPin(), altSource.getBELPin());
                }
            }
            // If the original source was routed and is now no longer used, it must
            // mean that the alternate source is used instead. Let's remove the main
            // source which will promote the alternate to main.
            if (sourcePreviouslyRouted && !source.isRouted()) {
                assert(altSource != null && altSource.isRouted());
                net.removePin(source, true);
                assert(net.getSource() == altSource);
                assert(net.getAlternateSource() == null);
                assert(altSource.isRouted());
                if (source.getName().endsWith("_O")) {
                    assert(altSource.getName().endsWith("MUX"));
                    // Add site routing back if we are keeping the MUX pin
                    si.routeIntraSiteNet(net, source.getBELPin(), source.getBELPin());
                }
            }
        }
        System.out.printf("F4_V66_POST lut=%.2f assign=%.2f fix=%.2f srcPin=%.2f%n",
                (tLut1 - tPost0) / 1e9, (tAssign1 - tLut1) / 1e9,
                (tFix1 - tAssign1) / 1e9, (System.nanoTime() - tFix1) / 1e9);
    }

    /**
     * Checks if a connection should be routed.
     * @param connection The connection in question.
     * @return true, if the connection should be routed.
     */
    protected boolean shouldRoute(Connection connection) {
        if (routeIteration > 1) {
            if (connection.getCriticality() > minRerouteCriticality) {
                return true;
            }
        }

        if (!connection.hasAltSinks()) {
            // Check that this connection's exclusive sink node is used but never overused
            RouteNode sinkRnode = connection.getSinkRnode();
            assert(sinkRnode.countConnectionsOfUser(connection.getNetWrapper()) > 0);
            assert(!sinkRnode.isOverUsed());
        }

        return !connection.isRouted() || connection.isCongested();
    }

    /**
     * Computes and sets the minimum reroute criticality for re-routing critical connections.
     */
    private void setRerouteCriticality() {
        // Limit the number of critical connections to be routed based on minRerouteCriticality and reroutePercentage
        minRerouteCriticality = config.getMinRerouteCriticality();
        criticalConnections.clear();

        int maxNumberOfCriticalConnections = (int) (indirectConnections.size() * 0.01 * config.getReroutePercentage());
        for (Connection connection : indirectConnections) {
            if (connection.getCriticality() > minRerouteCriticality) {
                criticalConnections.add(connection);
            }
        }

        if (criticalConnections.size() > maxNumberOfCriticalConnections) {
            criticalConnections.sort((connection1, connection2) -> Float.compare(connection2.getCriticality(),connection1.getCriticality()));
            minRerouteCriticality = criticalConnections.get(maxNumberOfCriticalConnections).getCriticality();
        }
    }

    /**
     * Updates timing through static timing analysis and calculates connections' criticalities.
     */
    private void updateTiming() {
        updateTimingTimer.start();
        timingWeight = Math.min(timingWeight * config.getTimingMultiplier(), 1f);
        oneMinusTimingWeight = 1 - timingWeight;
        maxDelayAndTimingVertex = timingManager.calculateArrivalRequiredTimes();
        timingManager.calculateCriticality(sortedIndirectConnections,
                MAX_CRITICALITY, config.getCriticalityExponent());
        updateTimingTimer.stop();
    }

    /**
     * Updates timing after fixing routes of nets.
     * @param fixedRoutes A set of nets whose routes have been fixed.
     */
    private void updateTimingAfterFixingRoutes(List<NetWrapper> fixedRoutes) {
        timingManager.updateIllegalNetsDelays(fixedRoutes, nodesDelays);
        timingManager.patchUpDelayOfConnections(sortedIndirectConnections);
        updateTiming();
    }

    /**
     * Assigns a list nodes to each connection to complete the route path of it.
     */
    private F4DirectGpu f4RawPaths;
    private final Map<F4DirectGpu.Query, List<Node>> f4PreparedDirect = new HashMap<>();

    private static F4DirectGpu.Query f4DirectQuery(Connection connection) {
        return new F4DirectGpu.Query(connection.getSource().getConnectedNode(), connection.getSink().getConnectedNode());
    }

    private void f4PrecomputeDirect() {
        try (F4DirectGpu direct = new F4DirectGpu("direct_precompute")) {
            List<F4DirectGpu.Query> batch = new ArrayList<>(F4DirectGpu.MAX_QUERIES);
            for (Connection connection : directConnections) {
                batch.add(f4DirectQuery(connection));
                if (batch.size() == F4DirectGpu.MAX_QUERIES) f4CacheDirect(direct, batch);
            }
            if (!batch.isEmpty()) f4CacheDirect(direct, batch);
            direct.report("direct_precompute", directConnections.size());
        }
    }

    private void f4CacheDirect(F4DirectGpu direct, List<F4DirectGpu.Query> batch) {
        List<List<Node>> paths = direct.resolve(batch);
        for (int i = 0; i < batch.size(); ++i) {
            if (paths.get(i).isEmpty()) {
                throw new IllegalStateException("GPU_ONLY_DIRECT_HAS_NO_PATH " + batch.get(i).source
                        + " -> " + batch.get(i).sink);
            }
            f4PreparedDirect.put(batch.get(i), paths.get(i));
        }
        batch.clear();
    }

    private void f4CommitDirect() {
        List<F4DirectGpu.Query> batch = new ArrayList<>(F4DirectGpu.MAX_QUERIES);
        for (Connection connection : directConnections) {
            F4DirectGpu.Query query = f4DirectQuery(connection);
            if (!f4PreparedDirect.containsKey(query)) {
                batch.add(query);
                if (batch.size() == F4DirectGpu.MAX_QUERIES) f4CacheDirect(f4RawPaths, batch);
            }
        }
        if (!batch.isEmpty()) f4CacheDirect(f4RawPaths, batch);
        for (Connection connection : directConnections) {
            List<Node> path = f4PreparedDirect.get(f4DirectQuery(connection));
            connection.setNodes(new ArrayList<>(path));
            connection.setRouted(true);
        }
        f4PreparedDirect.clear();
        System.out.println("F4_GPU_DIRECT_COMMITTED " + directConnections.size());
    }

    private void f4AssignProjectionPaths() {
        List<Connection> batch = new ArrayList<>(F4DirectGpu.MAX_QUERIES / 2);
        for (Connection connection : indirectConnections) {
            if (connection.getRnodes().isEmpty()) {
                connection.setNodes(new ArrayList<>());
                continue;
            }
            batch.add(connection);
            if (batch.size() == F4DirectGpu.MAX_QUERIES / 2) f4AssignProjectionBatch(batch);
        }
        if (!batch.isEmpty()) f4AssignProjectionBatch(batch);
    }

    private void f4AssignProjectionBatch(List<Connection> batch) {
        List<F4DirectGpu.Query> queries = new ArrayList<>(batch.size() * 2);
        for (Connection connection : batch) {
            RouteNode sink = connection.getSinkRnode();
            RouteNode routedSink = connection.getRnodes().get(0);
            if (sink == routedSink) {
                queries.add(new F4DirectGpu.Query(sink, connection.getSink().getConnectedNode()));
            } else {
                assert(isValidSink(connection, sink));
                assert(routedSink.getSitePin() != null);
            }
            queries.add(new F4DirectGpu.Query(connection.getSource().getConnectedNode(), connection.getSourceRnode()));
        }
        List<List<Node>> answers = f4RawPaths.resolve(queries);
        int at = 0;
        for (Connection connection : batch) {
            List<Node> nodes = new ArrayList<>();
            if (connection.getSinkRnode() == connection.getRnodes().get(0)) {
                List<Node> tail = answers.get(at++);
                if (tail.size() >= 2) nodes.addAll(tail.subList(0, tail.size() - 1));
            }
            nodes.addAll(connection.getRnodes());
            List<Node> head = answers.get(at++);
            if (head.size() >= 2) nodes.addAll(head.subList(1, head.size()));
            connection.setNodes(nodes);
        }
        if (at != answers.size()) throw new IllegalStateException("GPU pin-path result count");
        batch.clear();
    }

    protected void assignNodesToConnections() {
        if (f4RawPaths != null) {
            f4AssignProjectionPaths();
            return;
        }
        long t0 = System.nanoTime();
        var warmedTileTypes = new java.util.HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
                if (tile.getWireCount() > 0) {
                    tile.getForwardTilePIPs(0);
                }
            }
        }
        final int total = indirectConnections.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(4096, (total + workers * 4 - 1) / (workers * 4));
        var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < total; from += chunk) {
            final int f = from;
            final int t = Math.min(total, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    Connection connection = indirectConnections.get(i);
                    List<RouteNode> rnodes = connection.getRnodes();
                    if (rnodes.isEmpty()) {
                        connection.setNodes(java.util.Collections.emptyList());
                        continue;
                    }
                    List<Node> nodes = new ArrayList<>(rnodes.size() + 4);
                    connection.setNodes(nodes);

                    RouteNode sinkRnode = connection.getSinkRnode();

                    if (sinkRnode == rnodes.get(0)) {
                        List<Node> switchBoxToSink = RouterHelper.findPathBetweenNodes(sinkRnode, connection.getSink().getConnectedNode());
                        if (switchBoxToSink.size() >= 2) {
                            nodes.addAll(switchBoxToSink.subList(0, switchBoxToSink.size() - 1));
                        }
                    } else {
                        assert(isValidSink(connection, sinkRnode));
                        assert(rnodes.get(0).getSitePin() != null);
                    }

                    nodes.addAll(rnodes);

                    List<Node> sourceToSwitchBox = RouterHelper.findPathBetweenNodes(connection.getSource().getConnectedNode(), connection.getSourceRnode());
                    if (sourceToSwitchBox.size() >= 2) {
                        nodes.addAll(sourceToSwitchBox.subList(1, sourceToSwitchBox.size()));
                    }
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        System.out.printf("F4_V37_ASSIGN_NODES conns=%d time_s=%.2f%n", total, (System.nanoTime() - t0) / 1e9);
    }

    /**
     * Sorts indirect connections for routing.
     */
    private void sortConnections() {
        long t0 = System.nanoTime();
        Connection[] arr = indirectConnections.toArray(new Connection[0]);
        ConnectionSortStrategy strategy = config.getConnectionSortStrategy();
        java.util.Arrays.parallelSort(arr, strategy.comparator());
        sortedIndirectConnections.clear();
        java.util.Collections.addAll(sortedIndirectConnections, arr);
        System.out.printf("F4_V66_SORT conns=%d time_s=%.2f%n", arr.length, (System.nanoTime() - t0) / 1e9);
        System.out.println("INFO: Sorting connections with strategy: " + strategy);
    }


    private void printIterationHeader(boolean timingDriven) {
        System.out.printf("------------------------------------------------------------------------------\n");
        if (timingDriven) {
            System.out.printf("%9s  %12s  %8s   %11s  %10s   %5s  %9s\n",
                    "         ", "Generated", "  RRG",    "  Routed",   "Nodes With", "CPD", "Total Run");
            System.out.printf("%9s  %12s  %8s   %11s  %10s   %5s  %9s\n",
                    "Iteration", "RRG Nodes", "Time (s)", "Connections", "Overlaps", "(ps)", "Time (s)");
            System.out.printf("---------  ----------------------   -----------  ----------   -----  ---------\n");
        } else {
            System.out.printf("%9s  %12s  %8s   %11s  %10s   %5s  %9s\n",
                    "         ", "Generated", "  RRG",    "  Routed",   "Nodes With", "    ", "Total Run");
            System.out.printf("%9s  %12s  %8s   %11s  %10s   %5s  %9s\n",
                    "Iteration", "RRG Nodes", "Time (s)", "Connections", "Overlaps", "    ", "Time (s)");
            System.out.printf("---------  ----------------------   -----------  ----------   ----------------\n");
        }
    }

    /**
     * Prints routing iteration statistics, including the iteration, number of connections routed in the iteration,
     * total runtime of the iteration, number of created rnodes, time spent in creating rnodes that is included in the
     * @param iterationRuntime Total runtime of this iteration.
     * @param rnodesCreationTime The runtime of generating routing resource graph nodes.
     */
    private void printRoutingIterationStatisticsInfo(float iterationRuntime, float rnodesCreationTime) {
        long overUsed = v27RoundActive ? v27OverusedCount : overUsedRnodes.size();
        if (config.isTimingDriven()) {
            System.out.printf("%4d       %12d  %8.2f   %11d  %10d   %5d  %9.2f\n",
                    routeIteration,
                    rnodesCreatedThisIteration,
                    rnodesCreationTime,
                    connectionsRoutedThisIteration.get(),
                    overUsed,
                    (short)(maxDelayAndTimingVertex == null? 0 : maxDelayAndTimingVertex.getFirst()),
                    iterationRuntime * 1e-9);
        } else {
            System.out.printf("%4d       %12d  %8.2f   %11d  %10d   %5s  %9.2f\n",
                    routeIteration,
                    rnodesCreatedThisIteration,
                    rnodesCreationTime,
                    connectionsRoutedThisIteration.get(),
                    overUsed,
                    "",
                    iterationRuntime * 1e-9);
        }
        if (overUsed == 0) System.out.printf("------------------------------------------------------------------------------\n");
    }

    /**
     * Updates the congestion cost factors.
     */
    private void updateCostFactors() {
        updateCongestionCosts.start();

        checkHus();

        // Inflate the present congestion factor
        presentCongestionFactor *= config.getPresentCongestionMultiplier();
        presentCongestionFactor = Math.min(presentCongestionFactor, config.getMaxPresentCongestionFactor());

        if (v27RoundActive) {
            if (v27NextActive == null) v27NextActive = new int[sortedIndirectConnections.size()];
            v27NextActiveCount = getFidesLaneGate().f4NativeUpdateHistAndFeedback(
                    routeIteration, historicalCongestionFactor, config.isEnlargeBoundingBox(),
                    config.getExtensionXIncrement(), config.getExtensionYIncrement(),
                    v27NextActive, null);
        } else {
            updateCost();
        }

        updateCongestionCosts.stop();
    }

    /**
     * Updates present congestion cost and historical congestion cost of rnodes.
     */
    private void updateCost() {
        overUsedRnodes.clear();
        for (RouteNode rnode : routingGraph.getRnodes()) {
            int overuse = rnode.getOccupancy() - RouteNode.capacity;
            if (overuse > 0) {
                overUsedRnodes.add(rnode);
                rnode.setHistoricalCongestionCost(rnode.getHistoricalCongestionCost() + overuse * historicalCongestionFactor);
            }
        }
    }

    /**
     * Check whether to activate Hybrid Updating Strategy (HUS)
     */
    private void checkHus() {
        if (!hus) {
            return;
        }

        if (routeIteration == 1) {
            long overUseCnt = 0;
            if (v27RoundActive) {
                overUseCnt = v27OverusedCount;
            } else {
                for (RouteNode rnode : routingGraph.getRnodes()) {
                    if (rnode.isOverUsed()) {
                        overUseCnt++;
                    }
                }
            }
            husInitialCongested = (float) overUseCnt / sortedIndirectConnections.size() > config.getHusInitialCongestedThreshold();
        }

        if (husInitialCongested) {
            float congestedConnRatio = (float) connectionsRoutedThisIteration.get() / sortedIndirectConnections.size();
            if (congestedConnRatio < config.getHusActivateThreshold()) {
                // Activate HUS: slow down the present cost growth and increase historical cost growth instead
                float husAlpha = config.getHusAlpha();
                if (husAlpha >= config.getPresentCongestionMultiplier()) {
                    System.out.println("WARNING: HUS alpha is not less than the current present congestion multiplier.");
                }
                config.setPresentCongestionMultiplier(husAlpha);

                float husBeta = config.getHusBeta();
                if (husBeta <= historicalCongestionFactor) {
                    System.out.println("WARNING: HUS beta is not greater than the current historical congestion factor.");
                }
                historicalCongestionFactor = husBeta;

                // Disable HUS from being activated again
                hus = false;
            }
        }
    }

    /**
     * Computes node usage of each type and the total wirelength of the design.
     */
    private void computesNodeUsageAndTotalWirelength() {
        long t0 = System.nanoTime();
        totalWL = 0;
        totalINTNodes = 0;
        nodeTypeUsage = new EnumMap<>(IntentCode.class);
        nodeTypeLength = new EnumMap<>(IntentCode.class);

        final IntentCode[] allIC = IntentCode.values();
        final int nIC = allIC.length;
        final int lagunaOrd = IntentCode.NODE_LAGUNA_DATA.ordinal();
        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        final int workers = Math.max(1, f4SharedPool.getParallelism());
        final int chunk = Math.max(1024, (nNets + workers * 4 - 1) / (workers * 4));
        final int numTasks = (nNets + chunk - 1) / chunk;
        final long[] taskWl = new long[numTasks];
        final long[] taskInt = new long[numTasks];
        final long[][] taskUsage = new long[numTasks][];
        final long[][] taskLength = new long[numTasks][];

        var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numTasks);
        for (int ti = 0; ti < numTasks; ++ti) {
            final int idx = ti;
            final int from = ti * chunk;
            final int to = Math.min(nNets, from + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Set<Node> netNodes = null;
                final long[] localUsage = new long[nIC];
                final long[] localLength = new long[nIC];
                long localWL = 0;
                long localINT = 0;
                for (int i = from; i < to; ++i) {
                    NetWrapper netWrapper = netList.get(i);
                    List<Connection> conns = netWrapper.getConnections();
                    int cSz = conns.size();
                    if (cSz == 1) {
                        List<Node> cn = conns.get(0).getNodes();
                        if (cn != null) {
                            for (int j = 0, cnSz = cn.size(); j < cnSz; ++j) {
                                Node node = cn.get(j);
                                if (RouteNodeGraph.isExcludedTile(node)) continue;
                                localINT++;
                                RouteNode rn = routingGraph.getNode(node);
                                int wl = (rn != null) ? rn.getLength() : RouteNode.getLength(node, routingGraph);
                                localWL += wl;
                                int ord = (node.getTile().getTileTypeEnum() == com.xilinx.rapidwright.device.TileTypeEnum.LAGUNA_TILE
                                        && node.getWireName().startsWith("UBUMP"))
                                        ? lagunaOrd : node.getIntentCode().ordinal();
                                localUsage[ord]++;
                                localLength[ord] += wl;
                            }
                        }
                    } else if (cSz > 1) {
                        if (netNodes == null) netNodes = new HashSet<>();
                        else netNodes.clear();
                        for (int ci = 0; ci < cSz; ++ci) {
                            List<Node> cn = conns.get(ci).getNodes();
                            if (cn != null) netNodes.addAll(cn);
                        }
                        for (Node node : netNodes) {
                            if (RouteNodeGraph.isExcludedTile(node)) continue;
                            localINT++;
                            RouteNode rn = routingGraph.getNode(node);
                            int wl = (rn != null) ? rn.getLength() : RouteNode.getLength(node, routingGraph);
                            localWL += wl;
                            int ord = (node.getTile().getTileTypeEnum() == com.xilinx.rapidwright.device.TileTypeEnum.LAGUNA_TILE
                                    && node.getWireName().startsWith("UBUMP"))
                                    ? lagunaOrd : node.getIntentCode().ordinal();
                            localUsage[ord]++;
                            localLength[ord] += wl;
                        }
                    }
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
        final long[] totUsage = new long[nIC];
        final long[] totLength = new long[nIC];
        for (int ti = 0; ti < numTasks; ++ti) {
            wlSum += taskWl[ti];
            totalINTNodes += taskInt[ti];
            long[] u = taskUsage[ti];
            long[] l = taskLength[ti];
            for (int o = 0; o < nIC; ++o) {
                totUsage[o] += u[o];
                totLength[o] += l[o];
            }
        }
        for (int o = 0; o < nIC; ++o) {
            if (totUsage[o] > 0L) nodeTypeUsage.put(allIC[o], totUsage[o]);
            if (totLength[o] > 0L) nodeTypeLength.put(allIC[o], totLength[o]);
        }
        totalWL = (int) wlSum;
        System.out.printf("F4_V37_STATS_WL nets=%d time_s=%.2f%n", nNets, (System.nanoTime() - t0) / 1e9);
    }

    static List<IntentCode> nodeUsageForUltraScale = new ArrayList<>();
    static {
        nodeUsageForUltraScale.add(IntentCode.NODE_SINGLE);
        nodeUsageForUltraScale.add(IntentCode.NODE_DOUBLE);
        nodeUsageForUltraScale.add(IntentCode.NODE_VQUAD);
        nodeUsageForUltraScale.add(IntentCode.NODE_HQUAD);
        nodeUsageForUltraScale.add(IntentCode.NODE_VLONG);
        nodeUsageForUltraScale.add(IntentCode.NODE_HLONG);
        nodeUsageForUltraScale.add(IntentCode.NODE_LOCAL);
        nodeUsageForUltraScale.add(IntentCode.NODE_PINBOUNCE);
        nodeUsageForUltraScale.add(IntentCode.NODE_PINFEED);
        nodeUsageForUltraScale.add(IntentCode.NODE_LAGUNA_DATA); // UltraScale+ only intent code,
                                                                 // but super long lines from UltraScale (which have
                                                                 // IntentCode.INTENT_DEFAULT are mapped to this)
    }

    static List<IntentCode> nodeUsageForVersal = new ArrayList<>();
    static {
        nodeUsageForVersal.add(IntentCode.NODE_VSINGLE);
        nodeUsageForVersal.add(IntentCode.NODE_HSINGLE);
        nodeUsageForVersal.add(IntentCode.NODE_VDOUBLE);
        nodeUsageForVersal.add(IntentCode.NODE_HDOUBLE);
        nodeUsageForVersal.add(IntentCode.NODE_VQUAD);
        nodeUsageForVersal.add(IntentCode.NODE_HQUAD);
        nodeUsageForVersal.add(IntentCode.NODE_VLONG7);
        nodeUsageForVersal.add(IntentCode.NODE_VLONG12);
        nodeUsageForVersal.add(IntentCode.NODE_HLONG6);
        nodeUsageForVersal.add(IntentCode.NODE_HLONG10);
        nodeUsageForVersal.add(IntentCode.NODE_CLE_BNODE);
        nodeUsageForVersal.add(IntentCode.NODE_INTF_BNODE);
        nodeUsageForVersal.add(IntentCode.NODE_CLE_CNODE);
        nodeUsageForVersal.add(IntentCode.NODE_INTF_CNODE);
        nodeUsageForVersal.add(IntentCode.NODE_PINBOUNCE);
        nodeUsageForVersal.add(IntentCode.NODE_IMUX);
        // NODE_PINFEED exists on Versal but is behind a NODE_IMUX
        // and gets projectInputPinToINTNode() -ed away
        nodeUsageForVersal.add(IntentCode.NODE_SLL_DATA);
    }

    /**
     * Fixes routes of nets with routing path cycles and multi-driver nodes.
     */
    private void v37RipUpNoJournal(Connection connection) {
        List<RouteNode> rnodes = connection.getRnodes();
        if (rnodes.isEmpty()) return;
        RouteNode sinkRnode = rnodes.get(0);
        if (sinkRnode == connection.getSinkRnode()) {
            if (!connection.hasAltSinks()) {
                rnodes = rnodes.subList(1, rnodes.size() - 1);
            }
        }
        NetWrapper netWrapper = connection.getNetWrapper();
        for (RouteNode rnode : rnodes) {
            rnode.decrementUser(netWrapper);
        }
    }

    private List<NetWrapper> fixRoutes() {
        long t0 = System.nanoTime();
        List<NetWrapper> fixedRoutes = new ArrayList<>();
        int sequence = connectionsRouted.get() + 1;
        for (Entry<Net,NetWrapper> e : nets.entrySet()) {
            NetWrapper netWrapper = e.getValue();
            if (netWrapper.hasMultipleDrivers(sequence)) {
                fixedRoutes.add(netWrapper);
            }
            sequence++;
        }
        final boolean skipMirror = Boolean.getBoolean("fides.v37.skipClosingMirrorScan");
        if (!fixedRoutes.isEmpty() && !config.isTimingDriven() && v27OverusedCount == 0 && overUsedRnodes.isEmpty()
                && (!config.isFidesLaneMirror() || skipMirror)) {
            final int total = fixedRoutes.size();
            final int workers = Math.max(1, f4SharedPool.getParallelism());
            final int chunk = Math.max(256, (total + workers * 4 - 1) / (workers * 4));
            var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>();
            for (int from = 0; from < total; from += chunk) {
                final int f = from;
                final int t = Math.min(total, from + chunk);
                jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                    for (int i = f; i < t; ++i) {
                        NetWrapper nw = fixedRoutes.get(i);
                        for (Connection connection : nw.getConnections()) {
                            if (!connection.isDirect()) v37RipUpNoJournal(connection);
                        }
                        new RouteFixer(nw, routingGraph).finalizeRoutesOfConnections();
                    }
                }, f4SharedPool));
            }
            java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
            System.out.printf("F4_V37_FIX_ROUTES fixed=%d parallel=true time_s=%.2f%n", total, (System.nanoTime() - t0) / 1e9);
            return fixedRoutes;
        }
        for (NetWrapper netWrapper : fixedRoutes) {
            if (config.isFidesLaneMirror() && !skipMirror) {
                getFidesLaneGate().mirrorFixedNet();
            }
            if (config.isTimingDriven()) {
                addNodesDelays(netWrapper);
            }
            for (Connection connection : netWrapper.getConnections()) {
                if (connection.isDirect()) {
                    continue;
                }
                if (config.isFidesLaneMirror() && !skipMirror) {
                    getFidesLaneGate().mirrorOutOfBandRip(netWrapper, connection);
                }
                ripUp(connection);
            }
            RouteFixer graphHelper = new RouteFixer(netWrapper, routingGraph);
            graphHelper.finalizeRoutesOfConnections();
        }
        System.out.printf("F4_V37_FIX_ROUTES fixed=%d parallel=false time_s=%.2f%n", fixedRoutes.size(), (System.nanoTime() - t0) / 1e9);
        return fixedRoutes;
    }

    /**
     * Adds nodes and delay values of a routed to the map.
     * @param net The routed net.
     */
    private void addNodesDelays(NetWrapper net) {
        for (Connection connection:net.getConnections()) {
            for (RouteNode rnode : connection.getRnodes()) {
                nodesDelays.put(rnode, rnode.getDelay());
            }
        }
    }

    /**
     * Rips up a connection.
     * @param connection The connection to be ripped up.
     */
    protected void ripUp(Connection connection) {
        List<RouteNode> rnodes = connection.getRnodes();
        if (rnodes.isEmpty()) {
            assert(!connection.isRouted());
            return;
        }

        RouteNode sinkRnode = rnodes.get(0);
        if (sinkRnode == connection.getSinkRnode()) {
            if (!connection.hasAltSinks()) {
                // Sink is exclusive -- do not rip up
                rnodes = rnodes.subList(1, rnodes.size() - 1);
            }
        } else {
            // sinkRnode could be an alternate sink
            assert(isValidSink(connection, sinkRnode));
            // In which case it cannot be exclusive -- rip up all used nodes
        }

        NetWrapper netWrapper = connection.getNetWrapper();
        F4HostJournal journal = f4Direct == null || fidesLaneGate == null || RouteNode.f4PrivateRipActive()
                ? null : fidesLaneGate.f4Journal();
        F4HostJournal.Scope scope = journal == null ? null : journal.hotScope(netWrapper);
        for (RouteNode rnode : rnodes) {
            rnode.decrementUser(netWrapper);
            if (journal != null) journal.noteUsage(rnode, netWrapper, scope);
        }

        assert(sinkRnode.countConnectionsOfUser(netWrapper) > 0 ||
               (sinkRnode.countConnectionsOfUser(netWrapper) == 0 && connection.hasAltSinks()));
    }

    /**
     * Updates the users and present congestion cost of rnodes used by a routed connection.
     * @param connection The routed connection.
     */
    void updateUsersAndPresentCongestionCost(Connection connection) {
        List<RouteNode> rnodes = connection.getRnodes();
        if (rnodes.isEmpty()) {
            assert(!connection.isRouted());
            return;
        }

        RouteNode sinkRnode = rnodes.get(0);
        if (sinkRnode == connection.getSinkRnode()) {
            if (!connection.hasAltSinks()) {
                // Sink is exclusive -- do not increment
                rnodes = rnodes.subList(1, rnodes.size() - 1);
            }
        } else {
            // sinkRnode could be an alternate sink
            assert(isValidSink(connection, sinkRnode));
            // In which case it cannot be exclusive -- increment all used nodes
        }

        NetWrapper netWrapper = connection.getNetWrapper();
        F4HostJournal journal = f4Direct == null || fidesLaneGate == null || RouteNode.f4PrivateRipActive()
                ? null : fidesLaneGate.f4Journal();
        F4HostJournal.Scope scope = journal == null ? null : journal.hotScope(netWrapper);
        for (RouteNode rnode : rnodes) {
            rnode.incrementUser(netWrapper);
            if (journal != null) journal.noteUsage(rnode, netWrapper, scope);
        }
        assert(sinkRnode.countConnectionsOfUser(netWrapper) == 1 ||
               (sinkRnode.countConnectionsOfUser(netWrapper) > 1 &&
                       // An alternate sink (e.g. LUT routethru to FF) that serves more than one FF
                       (sinkRnode != connection.getSinkRnode() ||
                        // A bounce node that is this sink and also used to serve a different sink
                        sinkRnode.getIntentCode() == IntentCode.NODE_PINBOUNCE)
               )
        );
    }

    /**
     * Sets a list of {@link PIP} instances of each {@link Net} instance and checks if there is any PIP overlaps.
     */
    protected void setPIPsOfNets() {
        long t0 = System.nanoTime();
        // Pre-warm TileType PIP lookup maps sequentially before concurrent reads in RouterHelper.getConnectionPIPs
        // (com.xilinx.rapidwright.device.i.a(int,int) lazily populates an unsynchronized HashMap on first touch).
        Set<Object> warmedTileTypes = new HashSet<>();
        if (PartialRouter.fidesWarmedDevice != design.getDevice()) {
            for (Tile tile : design.getDevice().getAllTiles()) {
                if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                    tile.getPIP(0, 0);
                }
            }
        }
        final List<NetWrapper> netList = new ArrayList<>(nets.values());
        final int nNets = netList.size();
        final boolean trackChanges = design.isTrackingNetChanges();
        @SuppressWarnings("unchecked")
        final Set<PIP>[] builtPips = trackChanges ? new Set[nNets] : null;
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
                    if (trackChanges) {
                        builtPips[i] = newPIPs;
                    } else {
                        net.setPIPs(newPIPs);
                    }
                }
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        if (trackChanges) {
            for (int i = 0; i < nNets; ++i) {
                netList.get(i).getNet().setPIPs(builtPips[i]);
                builtPips[i] = null;
            }
        }
        System.out.printf("F4_V37_SETPIPS nets=%d tileTypes=%d trackChanges=%b time_s=%.2f%n",
                nNets, warmedTileTypes.size(), trackChanges, (System.nanoTime() - t0) / 1e9);

        if (!PartialRouter.fidesOverlapPostActive) {
            checkPIPsUsage();
        }
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
        final int chunk = Math.max(4096, (nAll + workers - 1) / workers);
        final int numBuckets = Math.max(1, (nAll + chunk - 1) / chunk);
        final long[][][] bKeys = new long[numBuckets][SHARDS][];
        final int[][][] bNets = new int[numBuckets][SHARDS][];
        final int[][] bLens = new int[numBuckets][SHARDS];
        final java.util.concurrent.atomic.AtomicBoolean needSlow = new java.util.concurrent.atomic.AtomicBoolean(false);

        var bJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi;
            final int from = bi * chunk;
            final int to = Math.min(nAll, from + chunk);
            bJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                long[][] lk = new long[SHARDS][8192];
                int[][] ln = new int[SHARDS][8192];
                int[] ll = new int[SHARDS];
                for (int i = from; i < to; ++i) {
                    Net net = allNets.get(i);
                    for (PIP pip : net.getPIPs()) {
                        Tile tile = pip.getTile();
                        if (tile == null) { needSlow.set(true); return; }
                        int ta = tile.getUniqueAddress();
                        int sw = pip.getStartWireIndex();
                        int ew = pip.getEndWireIndex();
                        if (ta < 0 || ta >= (1 << 27) || (sw & ~262143) != 0 || (ew & ~262143) != 0) {
                            needSlow.set(true);
                            return;
                        }
                        long key = (((long) (ta + 1)) << 36) | (((long) sw) << 18) | (long) ew;
                        long hm = (key ^ (key >>> 32)) * 0x9E3779B97F4A7C15L;
                        int s = ((int) (hm >>> 32)) & (SHARDS - 1);
                        int pos = ll[s];
                        long[] ka = lk[s];
                        if (pos == ka.length) {
                            int nc = ka.length << 1;
                            ka = java.util.Arrays.copyOf(ka, nc);
                            lk[s] = ka;
                            ln[s] = java.util.Arrays.copyOf(ln[s], nc);
                        }
                        ka[pos] = key;
                        ln[s][pos] = i;
                        ll[s] = pos + 1;
                    }
                }
                bKeys[b] = lk;
                bNets[b] = ln;
                bLens[b] = ll;
            }, f4SharedPool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                bJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        if (!needSlow.get()) {
            var sJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(SHARDS);
            for (int si = 0; si < SHARDS; ++si) {
                final int s = si;
                sJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                    int total = 0;
                    for (int b = 0; b < numBuckets; ++b) total += bLens[b][s];
                    int cap = 1024;
                    while (cap < total * 2 + 64) cap <<= 1;
                    long[] tKeys = new long[cap];
                    int[] tNets = new int[cap];
                    int mask = cap - 1;
                    for (int b = 0; b < numBuckets; ++b) {
                        long[] ks = bKeys[b][s];
                        int[] ns = bNets[b][s];
                        int sz = bLens[b][s];
                        for (int k = 0; k < sz; ++k) {
                            long key = ks[k];
                            int netIdx = ns[k];
                            long hm = (key ^ (key >>> 32)) * 0x9E3779B97F4A7C15L;
                            int slot = ((int) (hm >>> 32)) & mask;
                            while (true) {
                                long cur = tKeys[slot];
                                if (cur == 0L) {
                                    tKeys[slot] = key;
                                    tNets[slot] = netIdx;
                                    break;
                                }
                                if (cur == key) {
                                    if (tNets[slot] != netIdx) {
                                        needSlow.set(true);
                                        bKeys[b][s] = null;
                                        bNets[b][s] = null;
                                        return;
                                    }
                                    break;
                                }
                                slot = (slot + 1) & mask;
                            }
                        }
                        bKeys[b][s] = null;
                        bNets[b][s] = null;
                    }
                }, f4SharedPool));
            }
            java.util.concurrent.CompletableFuture.allOf(
                    sJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        }
        if (!needSlow.get()) {
            System.out.printf("F4_V37_CHECKPIPS nets=%d fast=true time_s=%.2f%n", nAll, (System.nanoTime() - t0) / 1e9);
            System.out.println("\nINFO: No PIP overlaps\n");
            return;
        }
        checkPIPsUsageSlowFallback(allNets, t0);
    }

    private void checkPIPsUsageSlowFallback(List<Net> allNets, long t0) {
        final int SHARDS = 64;
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
        System.out.printf("F4_V37_CHECKPIPS nets=%d fast=false time_s=%.2f%n", nAll, (System.nanoTime() - t0) / 1e9);
        if (pipsError > 0)
            System.err.println("ERROR: PIPs overused error: " + pipsError);
        else
            System.out.println("\nINFO: No PIP overlaps\n");
    }

    /**
     * Class encapsulating all state necessary to route the included connection
     */
    protected static class ConnectionState {
        /** Priority queue of all candidate nodes to be considered for routing this connection */
        protected final PriorityQueue<RouteNode> queue;
        /** The list of nodes marked as a target for this connection */
        protected final List<RouteNode> targets;

        /** Connection to be routed */
        protected Connection connection;
        /** Unique sequence count for this routing attempt for this connection */
        protected int sequence;

        /** Pre-computed weights specific to this connection for one routing iteration */
        protected float rnodeCostWeight;
        protected float shareWeight;
        protected float rnodeWLWeight;
        protected float estWlWeight;
        protected float dlyWeight;
        protected float estDlyWeight;

        /** Number of nodes popped during the routing of this connection */
        protected int nodesPopped;

        /** Number of nodes pushed during the routing of this connection */
        protected int nodesPushed;

        protected boolean earlyTermination;
        int f4HandoffBudget;
        boolean f4PrivateSearch;
        boolean f21BoundFirst;
        F1Corridor.Box f4Boundary;
        int f4RetryStamp;
        int f4PrivatePopCeiling;
        long f4OutsideChecks;
        int f4RetriedPops;
        int f4CpuPopsBeforeHandoff;

        protected ConnectionState() {
            this.queue = new PriorityQueue<>();
            this.targets = new ArrayList<>();
        }
    }

    /**
     * Routes a connection.
     * @param connection The connection to route.
     */
    int f4CheckLiveArc(ConnectionState state, RouteNode rnode, RouteNode childRNode) {
        final Connection connection = state.connection;
        final NetWrapper netWrapper = connection.getNetWrapper();
        final RouteNodeType rnodeType = rnode.getType();
        final boolean rnodeIsLaguna = Utils.isLaguna(rnode.getTile().getTileTypeEnum());
        final boolean longParent = config.isTimingDriven() && DelayEstimatorBase.isLong(rnode);
        assert !config.isTimingDriven() && !connection.hasAltSinks();
        // If childRnode is preserved, then it must be preserved for the current net we're routing
        Net preservedNet;
        assert((preservedNet = routingGraph.getPreservedNet(childRNode)) == null ||
                preservedNet == connection.getNet());

        boolean lookahead = false;
        if ((childRNode == connection.getSinkRnode())) {
            if (childRNode.getType().isAnyExclusiveSink()) {
                // This sink must be exclusively reserved for this connection already
                assert((childRNode == connection.getSinkRnode() && !connection.hasAltSinks()) ||
                       // Or be an exclusive BOUNCE sink for a different connection on the same net
                       childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
                assert(!childRNode.isOverUsed());
                assert(!childRNode.willOverUse(netWrapper));
                assert(childRNode.countConnectionsOfUser(netWrapper) == 1 ||
                       childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
            } else {
                throw new IllegalStateException("Expected an exclusive target");
            }
        } else {
            if (!isAccessible(childRNode, connection)) {
                throw new IllegalStateException("Live native access rejected " + childRNode);
            }
            RouteNodeType childType = childRNode.getType();
            switch (childType) {
                case LOCAL_EAST_LEADING_TO_NORTHBOUND_LAGUNA:
                case LOCAL_EAST_LEADING_TO_SOUTHBOUND_LAGUNA:
                case LOCAL_WEST_LEADING_TO_NORTHBOUND_LAGUNA:
                case LOCAL_WEST_LEADING_TO_SOUTHBOUND_LAGUNA:
                    // Lookahead beyond child nodes leading to a Laguna if it won't get overused
                    lookahead = !childRNode.willOverUse(netWrapper);
                    // Fall-through
                case LOCAL_BOTH:
                case LOCAL_EAST:
                case LOCAL_WEST:
                case LOCAL_RESERVED:
                    if (!routingGraph.isAccessible(childRNode, rnode, connection)) {
                        throw new IllegalStateException("Live native access rejected " + childRNode);
                    }
                    // Verify invariant that east/west wires stay east/west ...
                    assert(!rnodeType.isEastLocal() || childType.isEastLocal() ||
                            // ... unless it's an exclusive sink using a LOCAL_RESERVED node
                            (childType == RouteNodeType.LOCAL_RESERVED && connection.getSinkRnode().getType() == RouteNodeType.EXCLUSIVE_SINK_BOTH) ||
                            // ... or unless it's a Versal SLL input
                            childRNode.getIntentCode() == IntentCode.NODE_SLL_INPUT);
                    assert(!rnodeType.isWestLocal() || childType.isWestLocal() ||
                            (childType == RouteNodeType.LOCAL_RESERVED && connection.getSinkRnode().getType() == RouteNodeType.EXCLUSIVE_SINK_BOTH) ||
                            // ... or unless it's a Versal SLL input
                            childRNode.getIntentCode() == IntentCode.NODE_SLL_INPUT);
                    break;
                case NON_LOCAL_LEADING_TO_NORTHBOUND_LAGUNA:
                case NON_LOCAL_LEADING_TO_SOUTHBOUND_LAGUNA:
                    if (connection.isCrossSLR() && connection.getSinkRnode().getSLRIndex(routingGraph) != childRNode.getSLRIndex(routingGraph) &&
                            ((connection.isCrossSLRnorth() && childType == RouteNodeType.NON_LOCAL_LEADING_TO_NORTHBOUND_LAGUNA) ||
                             (connection.isCrossSLRsouth() && childType == RouteNodeType.NON_LOCAL_LEADING_TO_SOUTHBOUND_LAGUNA))) {
                        // Only lookahead beyond child nodes leading to a Laguna if we require an SLR crossing in that direction,
                        // and it won't get overused
                        lookahead = !childRNode.willOverUse(netWrapper);
                    }
                    // Fall-through
                case NON_LOCAL:
                    // LOCALs cannot connect to NON_LOCALs except
                    //   (a) IMUX (LOCAL_*_LEADING_TO_*_LAGUNA) -> LAG_MUX_ATOM_\\d+_TXOUT
                    //   (b) via a LUT routethru: IMUX (LOCAL*) -> CLE_CLE_*_SITE_0_[A-H]_O
                    assert(!rnodeType.isAnyLocal() || rnodeType.isLocalLeadingToLaguna() ||
                           (routingGraph.lutRoutethru && rnode.getIntentCode() == IntentCode.NODE_PINFEED) ||
                            // Allow CLE/*LAG*_PIN -> CLE/*[A-H]Q2?_PIN
                            (routingGraph.isVersal && connection.isCrossSLR() &&
                                   routingGraph.isVersalLagOutRoutethru(rnode, childRNode))
                    );

                    if (!routingGraph.isAccessible(childRNode, rnode, connection)) {
                        throw new IllegalStateException("Live native access rejected " + childRNode);
                    }
                    if (!config.isUseUTurnNodes() && childRNode.getDelay() > 10000) {
                        // To filter out those nodes that are considered to be excluded with the masking resource approach,
                        // such as U-turn shape nodes near the boundary
                        throw new IllegalStateException("Live native access rejected " + childRNode);
                    }

                    // Lookahead if parent or child is in a Laguna tile
                    // (e.g. LAG_MUX_ATOM_\\d+_TXOUT -> UBUMP\\d+
                    //       LAG_LAGUNA_SITE_[0-3]_RXD[0-5] -> RXD\\d+
                    //       RXD\\d+ -> INT_NODE_SDQ_\\d+_INT_OUT[01]
                    // )
                    // NOTE: UBUMP\\d+ wires have RouteNodeType.SUPER_LONG_LINE
                    lookahead |= (rnodeIsLaguna || Utils.isLaguna(childRNode.getTile().getTileTypeEnum()));
                    break;
                case EXCLUSIVE_SINK_BOTH:
                case EXCLUSIVE_SINK_EAST:
                case EXCLUSIVE_SINK_WEST:
                case EXCLUSIVE_SINK_NON_LOCAL:
                    assert(childType != RouteNodeType.EXCLUSIVE_SINK_EAST || rnodeType == RouteNodeType.LOCAL_EAST ||
                            // Must be an INODE that services Laguna but also feedsthrough above/below to a SLICE sink
                            rnodeType.isLocalLeadingToLaguna());
                    assert(childType != RouteNodeType.EXCLUSIVE_SINK_WEST || rnodeType == RouteNodeType.LOCAL_WEST ||
                            // Must be an INODE that services Laguna but also feedsthrough above/below to a SLICE sink
                            rnodeType.isLocalLeadingToLaguna());
                    assert(childType != RouteNodeType.EXCLUSIVE_SINK_BOTH || rnodeType == RouteNodeType.LOCAL_BOTH ||
                           // [BC]NODEs are LOCAL_{EAST,WEST} since they connect to INODEs, but also service CTRL sinks
                           (routingGraph.isVersal && EnumSet.of(IntentCode.NODE_CLE_BNODE, IntentCode.NODE_CLE_CNODE,
                                                                IntentCode.NODE_INTF_BNODE, IntentCode.NODE_INTF_CNODE)
                                   .contains(rnode.getIntentCode())));
                    if (!isAccessibleSink(childRNode, connection)) {
                        throw new IllegalStateException("Live native access rejected " + childRNode);
                    }
                    assert(childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
                    assert(childRNode.countConnectionsOfUser(netWrapper) > 0);
                    assert(!childRNode.willOverUse(netWrapper));
                    break;
                case SUPER_LONG_LINE:
                    assert(connection.isCrossSLR() &&
                           connection.getSinkRnode().getSLRIndex(routingGraph) != rnode.getSLRIndex(routingGraph));
                    // Do not lookahead beyond the SLL, since looking-ahead may push many SLLs onto the queue,
                    // and we want to pick the best (least expensive/congested) one
                    assert(!lookahead);
                    break;
                default:
                    throw new RuntimeException("Unexpected rnode type: " + childType);
            }
        }

        final int countSourceUses = childRNode.countConnectionsOfUser(connection.getNetWrapper());
        final float sharingFactor = 1 + state.shareWeight * countSourceUses;
        // Set the prev pointer, as RouteNode.getEndTileYCoordinate() and
        // RouteNode.getSLRIndex() require this
        childRNode.setPrev(rnode);

        float newPartialPathCost = rnode.getUpstreamPathCost();
        newPartialPathCost += state.rnodeCostWeight * getNodeCost(childRNode, connection, countSourceUses, sharingFactor);
        newPartialPathCost += state.rnodeWLWeight * childRNode.getLength() / sharingFactor;
        if (config.isTimingDriven()) {
            newPartialPathCost += state.dlyWeight * (childRNode.getDelay() + DelayEstimatorBase.getExtraDelay(childRNode, longParent));
        }

        int childX = childRNode.getEndTileXCoordinate();
        int childY = childRNode.getEndTileYCoordinate();
        RouteNode sinkRnode = connection.getSinkRnode();
        int sinkX = sinkRnode.getBeginTileXCoordinate();
        int sinkY = sinkRnode.getBeginTileYCoordinate();
        int deltaX = Math.abs(childX - sinkX);
        int deltaY = Math.abs(childY - sinkY);
        if (connection.isCrossSLR()) {
            assert(!childRNode.getType().isLocalLeadingToLaguna() || (
                    (connection.isCrossSLRnorth() && childRNode.getType().leadsToNorthboundLaguna()) ||
                    (connection.isCrossSLRsouth() && childRNode.getType().leadsToSouthboundLaguna()) ||
                    (deltaX == 0 && deltaY <= 1)
            ));

        }
        return Float.floatToRawIntBits(newPartialPathCost);
    }

    protected void routeIndirectConnection(Connection connection) {
        if (f4Direct != null) throw new IllegalStateException("GPU-only batch dispatch was bypassed");
        ConnectionState state = getConnectionState();
        state.connection = connection;
        state.sequence = connectionsRouted.incrementAndGet();
        connectionsRoutedThisIteration.incrementAndGet();
        state.rnodeCostWeight = 1 - connection.getCriticality();
        state.shareWeight = (float) (Math.pow(state.rnodeCostWeight, config.getShareExponent()));
        state.rnodeWLWeight = state.rnodeCostWeight * oneMinusWlWeight;
        state.estWlWeight = state.rnodeCostWeight * wlWeight;
        state.dlyWeight = connection.getCriticality() * oneMinusTimingWeight / 100f;
        state.estDlyWeight = connection.getCriticality() * timingWeight;
        state.nodesPopped = 0;
        state.nodesPushed = 0;
        state.earlyTermination = false;

        PriorityQueue<RouteNode> queue = state.queue;
        assert(queue.isEmpty());

        prepareRouteConnection(state);

        RouteNode rnode;
        while ((rnode = queue.poll()) != null) {
            state.nodesPopped++;
            if (rnode.isTarget()) {
                break;
            }
            exploreAndExpand(state, rnode);
        }
        final int pushedThisConnection = state.nodesPopped + queue.size();
        state.nodesPushed += pushedThisConnection;
        nodesPushed.addAndGet(pushedThisConnection);
        nodesPopped.addAndGet(state.nodesPopped);

        if (rnode != null) {
            queue.clear();
            finishRouteConnection(connection, rnode);
	    if (connection.isRouted() && goldenWriter != null) {
                dumpGoldenPath(connection, state);
            }
            if (!connection.isRouted()) {
                List<RouteNode> rnodes = connection.getRnodes();
                throw new RuntimeException("ERROR: Unable to save routing for connection " + connection + "\n" +
                                           "       Backtracking terminated at " + rnodes.get(rnodes.size() -1));
            }
            if (config.isTimingDriven()) {
                connection.updateRouteDelay();
            }
            assert(connection.isRouted());
        } else {
            assert(queue.isEmpty());
            // Clears previous route of the connection
            connection.resetRoute();
            connection.setRouted(false);
            assert(connection.getRnodes().isEmpty());
        }

        // Reset the nodes marked as this connection's target(s)
        List<RouteNode> targets = state.targets;
        for (RouteNode target : targets) {
            target.clearTarget();
        }
        targets.clear();
    }

    /**
     * Routes one connection with {@link FidesLaneHost} in place of {@link #exploreAndExpand}.
     *
     * <p>This is a sibling of {@link #routeIndirectConnection}, not a variant of it: the reference
     * method is a byte-for-byte piece of the frozen golden trace and is left alone.  What the two
     * share is everything except the search -- the same {@code ConnectionState}, the same
     * {@code prepareRouteConnection} and {@code finishRouteConnection}, the same
     * {@code dumpGoldenPath}, the same global counters -- so the only difference a run can observe
     * is which code walked the graph.
     *
     * <p><b>The accounting is a single identity, and getting it wrong is invisible until the
     * SUMMARY line.</b>  The reference splits its counting across three places -- the poll loop counts
     * pops, {@code push} counts lookahead re-entries, and {@code exploreAndExpand}'s early-termination
     * branch adds the queue it is about to throw away -- but the last two are each applied
     * <em>twice</em>, in the same two statements, to the per-connection {@code state.nodesPushed} and
     * to the global {@code nodesPushed} (RWRoute.java:2163-2164 and :1830-1831).  So for every
     * connection the global receives exactly what {@code state.nodesPushed} receives, and the two
     * fields are equal at the end of every attempt.  The lane's {@link FidesLane.Result#nodesPushed}
     * is the reference's {@code state.nodesPushed} whole, so it is also the global's share.
     *
     * <p>The abandoned queue is the trap: it belongs to {@code state.nodesPushed} (early termination
     * adds it at :2163), so subtracting it before adding to the global makes the global short by
     * exactly the queue that was thrown away -- 87 nodes on this workload's first connection, and
     * nothing but the SUMMARY line notices.
     *
     * @param connection The connection to route.
     */
    protected void routeIndirectConnectionOnLane(Connection connection) {
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.COMMIT)) {
        ConnectionState state = getConnectionState();
        state.connection = connection;
        state.sequence = connectionsRouted.incrementAndGet();
        connectionsRoutedThisIteration.incrementAndGet();
        state.rnodeCostWeight = 1 - connection.getCriticality();
        state.shareWeight = (float) (Math.pow(state.rnodeCostWeight, config.getShareExponent()));
        state.rnodeWLWeight = state.rnodeCostWeight * oneMinusWlWeight;
        state.estWlWeight = state.rnodeCostWeight * wlWeight;
        state.dlyWeight = connection.getCriticality() * oneMinusTimingWeight / 100f;
        state.estDlyWeight = connection.getCriticality() * timingWeight;
        state.nodesPopped = 0;
        state.nodesPushed = 0;
        state.earlyTermination = false;

        PriorityQueue<RouteNode> queue = state.queue;
        assert(queue.isEmpty());

        prepareRouteConnection(state);

        RouteNode rnode;
        FidesLane.Result laneResult;
        try {
            FidesLaneGate gate = getFidesLaneGate();
            FidesLane.Attempt attempt = gate.begin(connection, state, queue);
            laneResult = gate.run(attempt);

            state.nodesPopped = laneResult.nodesPopped;
            state.nodesPushed = laneResult.nodesPushed;
            state.earlyTermination = laneResult.earlyTermination;

            /* The global's share IS the per-connection total: the reference increments both, with the
             * same values, at :2163-2164 and :1830-1831.  See the method's javadoc for why subtracting
             * the abandoned queue here is the mistake that looks right. */
            nodesPushed.addAndGet(laneResult.nodesPushed);
            nodesPopped.addAndGet(state.nodesPopped);

            rnode = gate.resultNode(laneResult);
            /* Pre-commit on purpose, and the two halves must not be merged back into one call: the
             * census delta and the mirror drift check are about the state the lane READ, and the
             * commit below writes exactly those columns (updateUsersAndPresentCongestionCost).  A
             * drift check moved past it would compare the mirror against the commit's own writes and
             * report a drift the lane never saw.  Criterion A is the other half and it MUST wait for
             * the commit -- that is reportGolden, called below. */
            gate.report(laneResult);
        } catch (FidesLane.Failure e) {
            throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                    + "       " + e.getMessage(), e);
        }

        if (rnode != null) {
            queue.clear();
            /* The same write as the batch path's, and here for the same reason rather than a different
             * one: this path is a batch of one, and writing the chain from the lane's own answer is
             * what that single attempt's pushes would have left anyway.  Doing it on both paths keeps
             * one rule -- the commit's chain comes from the lane's path -- instead of two that happen
             * to agree today.  See FidesLaneGate#rewritePrevFromPath. */
            try {
                getFidesLaneGate().rewritePrevFromPath(laneResult, rnode);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
            finishRouteConnection(connection, rnode);
            /* Criterion A, rendered here and not above: the golden line this is compared against is
             * written by dumpGoldenPath from connection.toString() AFTER finishRouteConnection, and
             * finishRouteConnection -> saveRouting -> Connection.addRnode grows the connection's
             * bounding box monotonically (resetRoute clears the nodes but not the box).  Comparing
             * before the commit reads the wrong instant; Con 5261, whose own detour extends its own
             * box, is the one connection in 84,107 where that shows.  See FidesLaneGate#reportGolden. */
            try {
                getFidesLaneGate().reportGolden(laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
            try {
                getFidesLaneGate().checkHandback(connection, laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane hand-back failed for connection "
                        + connection + "\n       " + e.getMessage(), e);
            }
	    if (connection.isRouted() && goldenWriter != null) {
                dumpGoldenPath(connection, state);
            }
            if (!connection.isRouted()) {
                List<RouteNode> rnodes = connection.getRnodes();
                throw new RuntimeException("ERROR: Unable to save routing for connection " + connection + "\n" +
                                           "       Backtracking terminated at " + rnodes.get(rnodes.size() -1));
            }
            if (config.isTimingDriven()) {
                connection.updateRouteDelay();
            }
            assert(connection.isRouted());
        } else {
            assert(queue.isEmpty());
            // Clears previous route of the connection
            connection.resetRoute();
            connection.setRouted(false);
            assert(connection.getRnodes().isEmpty());
            /* Criterion A runs here too: an attempt that produces no line is exactly what would shift
             * the sequence-to-line mapping, so refusing is the point.
             *
             * ONE ASSUMPTION, stated rather than hidden: in whole-round mode the golden line index is
             * the attempt sequence, which is correct only while every attempt emits a line.  If this
             * workload ever has an unrouted attempt, checkGolden's identity check fires and its message
             * names this possibility -- a loud stop, not a silent misalignment.  Verified for this
             * workload's first 82,514 attempts (every one of them routed), and for the remainder by
             * the next whole-round run.  resetRoute() does not touch the bounding box, so the string
             * rendered here is the same one the pre-commit position would have rendered. */
            try {
                getFidesLaneGate().reportGolden(laneResult);
            } catch (FidesLane.Failure e) {
                throw new RuntimeException("ERROR: FIDES lane refused connection " + connection + "\n"
                        + "       " + e.getMessage(), e);
            }
        }

        // Reset the nodes marked as this connection's target(s)
        List<RouteNode> targets = state.targets;
        for (RouteNode target : targets) {
            target.clearTarget();
        }
        targets.clear();
            }
    }

    /**
     * The lane gate, built on first use.
     *
     * <p>Lazy on purpose: the gate loads the 6.5 GiB artifact, the 4.13 GiB manifest index and the
     * lane's four N-sized arrays, and a run that never reaches its named connection should not pay
     * for any of it.  Held in a field because a second construction would be a second copy of the
     * whole graph.
     */
    protected FidesLaneGate getFidesLaneGate() {
        if (fidesLaneGate == null) {
            try {
                fidesLaneGate = FidesLaneGate.open(this, config, routingGraph);
            } catch (java.io.IOException e) {
                throw new RuntimeException("ERROR: cannot open the FIDES lane gate: " + e.getMessage(), e);
            }
        }
        return fidesLaneGate;
    }

    /**
     * Dumps the final routing path of a routed connection to the golden reference file.
     * @param connection The routed connection.
     */
    private void dumpGoldenPath(Connection connection, ConnectionState state) {
        try (FidesPhaseTimer.Scope phaseScope = FidesPhaseTimer.enter(FidesPhaseTimer.LOG)) {
        StringBuilder sb = new StringBuilder(connection.toString()).append(" | ");
        int wirelength = 0;
        for (RouteNode r : connection.getRnodes()) {
            sb.append(r.getTile().getName()).append('/').append(r.getWireName()).append(" -> ");
            wirelength += r.getLength();
        }
        // Per-connection counters, matching exactly what is accumulated into the global
        // nodesPopped/nodesPushed counters for this routing attempt (see routeIndirectConnection
        // and the earlyTermination branch of exploreAndExpand). Appended after the path so the
        // path section remains the last-but-one " | "-delimited field.
        sb.append("| POP=").append(state.nodesPopped)
          .append(" PUSH=").append(state.nodesPushed)
          .append(" WL=").append(wirelength);
        goldenWriter.println(sb);
            }
    }


    protected void enlargeBoundingBox(Connection connection) {
        if (!config.isEnlargeBoundingBox()) {
            return;
        }

        connection.enlargeBoundingBox(config.getExtensionXIncrement(), config.getExtensionYIncrement());
    }

    protected void abandonConnectionIfUnroutable(Connection connection) {
        if (!config.isUseBoundingBox() || config.isEnlargeBoundingBox()) {
            return;
        }

        System.out.println("INFO: Abandoning\n");

        // Since bounding box is never enlarged there is no hope of routing this connection so abandon it
        indirectConnections.remove(connection);
        sortedIndirectConnections.remove(connection);
    }

    /**
     * Deals with a failed connection by possible output pin swapping and unrouting preserved nets if the router is in the soft preserve mode.
     * @param connection The failed connection.
     */
    protected boolean handleUnroutableConnection(Connection connection) {
        enlargeBoundingBox(connection);
        if (routeIteration == 1 && swapOutputPin(connection)) {
            return true;
        }
        abandonConnectionIfUnroutable(connection);
        return false;
    }

    protected boolean handleCongestedConnection(Connection connection) {
        enlargeBoundingBox(connection);
        return false;
    }

    /**
     * Swaps the output pin of a connection, if its net has an alternative output pin.
     * @param connection The connection in question.
     * @return true, if the output pin has been swapped.
     */
    protected boolean swapOutputPin(Connection connection) {
        SitePinInst source = connection.getSource();
        Pair<SitePinInst,RouteNode> altSourceAndRnode = connection.getOrCreateAlternateSource(routingGraph);
        if (altSourceAndRnode == null) {
            return false;
        }

        SitePinInst altSource = altSourceAndRnode.getFirst();
        System.out.println("INFO: Swap source from " + source + " to " + altSource + "\n");
        connection.setSource(altSource);

        RouteNode altSourceRnode = altSourceAndRnode.getSecond();
        connection.setSourceRnode(altSourceRnode);

        connection.setRouted(false);
        return true;
    }

    /**
     * Completes the routing process of a connection.
     * @param connection The routed target connection.
     */
    protected void finishRouteConnection(Connection connection, RouteNode rnode) {
        boolean routed = saveRouting(connection, rnode);
        connection.setRouted(routed);
        if (routed) {
            updateUsersAndPresentCongestionCost(connection);
        }
    }

    protected boolean isValidSink(Connection connection, RouteNode rnode) {
        return connection.getSinkRnode() == rnode || connection.getAltSinkRnodes().contains(rnode);
    }

    /**
     * Traces back for a connection from its sink rnode to its source, in order to build and store the routing path.
     * @param connection: The connection that is being routed.
     * @param rnode RouteNode to start backtracking from.
     * @return True if backtracking successful.
     */
    protected boolean saveRouting(Connection connection, RouteNode rnode) {
        backtrackCalls++;
        /* M5's witness knob.  The silent face below is reached only when a connection is handed a node
         * that is marked a TARGET but is not one of its own sinks -- and during one connection's
         * routing the only nodes marked target are that connection's own sink and alternates, every one
         * of which isValidSink accepts.  So no input this project has can plant the condition, and "a
         * leg that has never been seen red is not evidence" is the rule this milestone wrote for
         * itself; FidesLaneGate's tail-prev knob is the same shape for the same reason.
         *
         * The knob MANUFACTURES the condition rather than bypassing the guard.  It marks one foreign
         * node as a target (the connection's own source, which is never a sink of it) and hands that
         * over, so the guard below still decides, in its own words, and the silent line still executes
         * for real.  A knob that wrote silentBacktracks++ itself, or that relaxed the guard, would prove
         * only that a write placed above a guard runs.  The clauses the knob does NOT plant are not
         * accidents: a connection in its second iteration arrives here already routed with a non-empty
         * chain, because ripUp decrements users and clears neither.
         *
         * Never set by the gate; any run that sets it is a witness run and not a verdict run. */
        RouteNode m5ForcedTarget = null;
        if (System.getenv("FIDES_M5_FORCE_SILENT_BACKTRACK") != null && !silentBacktrackForced
                && connection.isRouted() && !connection.getRnodes().isEmpty()) {
            RouteNode foreign = connection.getSourceRnode();
            ConnectionState state = getConnectionState();
            if (foreign != null && state != null && !isValidSink(connection, foreign)) {
                foreign.markTarget(state);
                m5ForcedTarget = foreign;
                rnode = foreign;
                silentBacktrackForced = true;
            }
        }
        if (!isValidSink(connection, rnode)) {
            // M5: the family, both faces of it.  Counter first, before the guard can throw, so that the
            // loud face is counted even though it does not return -- otherwise a run whose every entry
            // here threw would report unexpected=0, which is the one reading this counter exists to
            // make impossible (see the field declarations).
            unexpectedBacktracks++;
            List<RouteNode> prevRouting = connection.getRnodes();
            // Check that this is the sink path marked by prepareRouteConnection()
            if (!connection.isRouted() || prevRouting.isEmpty() || !rnode.isTarget()) {
                throw new RuntimeException("Unexpected rnode to backtrack from: " + rnode);
            }
            // M5: the silent face -- a route is about to be committed from the PREVIOUS routing rather
            // than from the search that just finished.  Counted here, after the guard, so that the
            // numerator means "the corruption happened" and not "a sibling of it did".
            silentBacktracks++;
            // Backtrack from the sink used on that sink path
            rnode = prevRouting.get(0);
        }
        /* Undo the one thing the knob changed, so that the mark cannot outlive the branch it was planted
         * for.  Deliberately not a finally: the only way to reach the line after this with the mark still
         * set is a throw from the guard above, and that ends the run -- a node left marked in a run that
         * is already over is not a state any reader can observe. */
        if (m5ForcedTarget != null) {
            m5ForcedTarget.clearTarget();
        }

        connection.resetRoute();
        do {
            connection.addRnode(rnode);
        } while ((rnode = rnode.getPrev()) != null);

        List<RouteNode> rnodes = connection.getRnodes();
        RouteNode sourceRnode = rnodes.get(rnodes.size() - 1);
        // Only successfully routed if backtracked to this connection's source node
        return sourceRnode == connection.getSourceRnode();
    }

    /**
     * Explores children (downhill rnodes) of a rnode for routing a connection and pushes the child into the queue,
     * if it is the target or is an accessible routing resource.
     * @param state State from the connection that is being routed.
     * @param rnode The rnode popped out from the queue.
     */
    private void exploreAndExpand(ConnectionState state, RouteNode rnode) {
        final boolean longParent = config.isTimingDriven() && DelayEstimatorBase.isLong(rnode);
        final Connection connection = state.connection;
        final int sequence = state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence;
        final PriorityQueue<RouteNode> queue = state.queue;
        final NetWrapper netWrapper = connection.getNetWrapper();
        final RouteNodeType rnodeType = rnode.getType();
        final boolean rnodeIsLaguna = Utils.isLaguna(rnode.getTile().getTileTypeEnum());

        F1Corridor.Box earlyBox = state.f21BoundFirst ? state.f4Boundary : null;
        for (RouteNode childRNode : rnode.getChildren(routingGraph)) {
            if (earlyBox != null && !earlyBox.contains(childRNode) && !childRNode.isTarget()) {
                ++state.f4OutsideChecks;
                continue;
            }
            if (childRNode.isVisited(sequence)) {
                // Node must be in queue already

                // Targets that are visited more than once must be overused
                assert(!childRNode.isTarget() || childRNode.willOverUse(netWrapper));

                // Note: it is possible this is a cheaper path to childRNode; however, because the
                // PriorityQueue class does not support (efficiently) reducing the cost of nodes
                // already in the queue, this opportunity is discarded
                continue;
            }

            // If childRnode is preserved, then it must be preserved for the current net we're routing
            Net preservedNet;
            assert((preservedNet = routingGraph.getPreservedNet(childRNode)) == null ||
                    preservedNet == connection.getNet());

            boolean lookahead = false;
            if (childRNode.isTarget()) {
                if (childRNode.getType().isAnyExclusiveSink()) {
                    // This sink must be exclusively reserved for this connection already
                    assert((childRNode == connection.getSinkRnode() && !connection.hasAltSinks()) ||
                           // Or be an exclusive BOUNCE sink for a different connection on the same net
                           childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
                    assert(!childRNode.isOverUsed());
                    assert(!childRNode.willOverUse(netWrapper));
                    assert(childRNode.countConnectionsOfUser(netWrapper) == 1 ||
                           childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
                    state.earlyTermination = true;
                } else {
                    // Target is not an exclusive sink, only early terminate if this net will not
                    // (further) overuse this node
                    state.earlyTermination = !childRNode.willOverUse(netWrapper);
                }

                if (state.earlyTermination) {
                    assert(!childRNode.isVisited(sequence));
                    state.nodesPushed += queue.size();
                    if (!state.f4PrivateSearch) nodesPushed.addAndGet(queue.size());
                    queue.clear();
                }
            } else {
                if (!isAccessible(childRNode, connection)) continue;
                if (earlyBox == null && state.f4Boundary != null && !state.f4Boundary.contains(childRNode)) {
                    ++state.f4OutsideChecks;
                    continue;
                }
                RouteNodeType childType = childRNode.getType();
                switch (childType) {
                    case LOCAL_EAST_LEADING_TO_NORTHBOUND_LAGUNA:
                    case LOCAL_EAST_LEADING_TO_SOUTHBOUND_LAGUNA:
                    case LOCAL_WEST_LEADING_TO_NORTHBOUND_LAGUNA:
                    case LOCAL_WEST_LEADING_TO_SOUTHBOUND_LAGUNA:
                        // Lookahead beyond child nodes leading to a Laguna if it won't get overused
                        lookahead = !childRNode.willOverUse(netWrapper);
                        // Fall-through
                    case LOCAL_BOTH:
                    case LOCAL_EAST:
                    case LOCAL_WEST:
                    case LOCAL_RESERVED:
                        if (!routingGraph.isAccessible(childRNode, rnode, connection)) {
                            continue;
                        }
                        // Verify invariant that east/west wires stay east/west ...
                        assert(!rnodeType.isEastLocal() || childType.isEastLocal() ||
                                // ... unless it's an exclusive sink using a LOCAL_RESERVED node
                                (childType == RouteNodeType.LOCAL_RESERVED && connection.getSinkRnode().getType() == RouteNodeType.EXCLUSIVE_SINK_BOTH) ||
                                // ... or unless it's a Versal SLL input
                                childRNode.getIntentCode() == IntentCode.NODE_SLL_INPUT);
                        assert(!rnodeType.isWestLocal() || childType.isWestLocal() ||
                                (childType == RouteNodeType.LOCAL_RESERVED && connection.getSinkRnode().getType() == RouteNodeType.EXCLUSIVE_SINK_BOTH) ||
                                // ... or unless it's a Versal SLL input
                                childRNode.getIntentCode() == IntentCode.NODE_SLL_INPUT);
                        break;
                    case NON_LOCAL_LEADING_TO_NORTHBOUND_LAGUNA:
                    case NON_LOCAL_LEADING_TO_SOUTHBOUND_LAGUNA:
                        if (connection.isCrossSLR() && connection.getSinkRnode().getSLRIndex(routingGraph) != childRNode.getSLRIndex(routingGraph) &&
                                ((connection.isCrossSLRnorth() && childType == RouteNodeType.NON_LOCAL_LEADING_TO_NORTHBOUND_LAGUNA) ||
                                 (connection.isCrossSLRsouth() && childType == RouteNodeType.NON_LOCAL_LEADING_TO_SOUTHBOUND_LAGUNA))) {
                            // Only lookahead beyond child nodes leading to a Laguna if we require an SLR crossing in that direction,
                            // and it won't get overused
                            lookahead = !childRNode.willOverUse(netWrapper);
                        }
                        // Fall-through
                    case NON_LOCAL:
                        // LOCALs cannot connect to NON_LOCALs except
                        //   (a) IMUX (LOCAL_*_LEADING_TO_*_LAGUNA) -> LAG_MUX_ATOM_\\d+_TXOUT
                        //   (b) via a LUT routethru: IMUX (LOCAL*) -> CLE_CLE_*_SITE_0_[A-H]_O
                        assert(!rnodeType.isAnyLocal() || rnodeType.isLocalLeadingToLaguna() ||
                               (routingGraph.lutRoutethru && rnode.getIntentCode() == IntentCode.NODE_PINFEED) ||
                                // Allow CLE/*LAG*_PIN -> CLE/*[A-H]Q2?_PIN
                                (routingGraph.isVersal && connection.isCrossSLR() &&
                                       routingGraph.isVersalLagOutRoutethru(rnode, childRNode))
                        );

                        if (!routingGraph.isAccessible(childRNode, rnode, connection)) {
                            continue;
                        }
                        if (!config.isUseUTurnNodes() && childRNode.getDelay() > 10000) {
                            // To filter out those nodes that are considered to be excluded with the masking resource approach,
                            // such as U-turn shape nodes near the boundary
                            continue;
                        }

                        // Lookahead if parent or child is in a Laguna tile
                        // (e.g. LAG_MUX_ATOM_\\d+_TXOUT -> UBUMP\\d+
                        //       LAG_LAGUNA_SITE_[0-3]_RXD[0-5] -> RXD\\d+
                        //       RXD\\d+ -> INT_NODE_SDQ_\\d+_INT_OUT[01]
                        // )
                        // NOTE: UBUMP\\d+ wires have RouteNodeType.SUPER_LONG_LINE
                        lookahead |= (rnodeIsLaguna || Utils.isLaguna(childRNode.getTile().getTileTypeEnum()));
                        break;
                    case EXCLUSIVE_SINK_BOTH:
                    case EXCLUSIVE_SINK_EAST:
                    case EXCLUSIVE_SINK_WEST:
                    case EXCLUSIVE_SINK_NON_LOCAL:
                        assert(childType != RouteNodeType.EXCLUSIVE_SINK_EAST || rnodeType == RouteNodeType.LOCAL_EAST ||
                                // Must be an INODE that services Laguna but also feedsthrough above/below to a SLICE sink
                                rnodeType.isLocalLeadingToLaguna());
                        assert(childType != RouteNodeType.EXCLUSIVE_SINK_WEST || rnodeType == RouteNodeType.LOCAL_WEST ||
                                // Must be an INODE that services Laguna but also feedsthrough above/below to a SLICE sink
                                rnodeType.isLocalLeadingToLaguna());
                        assert(childType != RouteNodeType.EXCLUSIVE_SINK_BOTH || rnodeType == RouteNodeType.LOCAL_BOTH ||
                               // [BC]NODEs are LOCAL_{EAST,WEST} since they connect to INODEs, but also service CTRL sinks
                               (routingGraph.isVersal && EnumSet.of(IntentCode.NODE_CLE_BNODE, IntentCode.NODE_CLE_CNODE,
                                                                    IntentCode.NODE_INTF_BNODE, IntentCode.NODE_INTF_CNODE)
                                       .contains(rnode.getIntentCode())));
                        if (!isAccessibleSink(childRNode, connection)) {
                            continue;
                        }
                        assert(childRNode.getIntentCode() == IntentCode.NODE_PINBOUNCE);
                        assert(childRNode.countConnectionsOfUser(netWrapper) > 0);
                        assert(!childRNode.willOverUse(netWrapper));
                        break;
                    case SUPER_LONG_LINE:
                        assert(connection.isCrossSLR() &&
                               connection.getSinkRnode().getSLRIndex(routingGraph) != rnode.getSLRIndex(routingGraph));
                        // Do not lookahead beyond the SLL, since looking-ahead may push many SLLs onto the queue,
                        // and we want to pick the best (least expensive/congested) one
                        assert(!lookahead);
                        break;
                    default:
                        throw new RuntimeException("Unexpected rnode type: " + childType);
                }
            }

            evaluateCostAndPush(state, rnode, longParent, childRNode, lookahead);
            if (state.earlyTermination) {
                assert(queue.size() == 1 && queue.peek().isTarget() && !queue.peek().willOverUse(netWrapper));
                // Target is uncongested and the only thing in the (previously cleared) queue, abandon immediately
                break;
            }
        }
    }

    /**
     * Checks if a routing resource is accessible.
     * @param child The routing resource in question.
     * @param connection The connection to route.
     * @return true, if no bounding box constraints, or if the routing resource is within the connection's bounding box when use the bounding box constraint.
     */
    protected boolean isAccessible(RouteNode child, Connection connection) {
        return !config.isUseBoundingBox() || child.isInConnectionBoundingBox(connection);
    }

    protected boolean isAccessibleSink(RouteNode child, Connection connection) {
        assert(child.getType().isAnyExclusiveSink());
        assert(!child.isOverUsed());

        if (child.isTarget()) {
            return true;
        }

        if (child.countConnectionsOfUser(connection.getNetWrapper()) == 0 ||
            child.getIntentCode() != IntentCode.NODE_PINBOUNCE) {
            // Inaccessible if child is not a sink pin of another connection on the same
            // net, or it is not a PINBOUNCE node
            return false;
        }

        // Must be a NODE_PINBOUNCE that is an exclusive sink of some other connection on the same net
        return true;
    }

    /**
     * Evaluates the cost of a child of a rnode and pushes the child into the queue after cost evaluation.
     * @param state State from the connection that is being routed.
     * @param rnode The parent rnode of the child in question.
     * @param longParent A boolean value to indicate if the parent is a Long node
     * @param childRnode The child rnode in question.
     * @param lookahead A boolean for whether we should skipping pushing this child node on the queue and immediately explore it.
     */
    protected void evaluateCostAndPush(ConnectionState state,
                                       RouteNode rnode,
                                       boolean longParent,
                                       RouteNode childRnode,
                                       boolean lookahead) {
        final Connection connection = state.connection;
        final int countSourceUses = childRnode.countConnectionsOfUser(connection.getNetWrapper());
        final float sharingFactor = 1 + state.shareWeight * countSourceUses;
        // Set the prev pointer, as RouteNode.getEndTileYCoordinate() and
        // RouteNode.getSLRIndex() require this
        childRnode.setPrev(rnode);

        float newPartialPathCost = rnode.getUpstreamPathCost();
        newPartialPathCost += state.rnodeCostWeight * getNodeCost(childRnode, connection, countSourceUses, sharingFactor);
        newPartialPathCost += state.rnodeWLWeight * childRnode.getLength() / sharingFactor;
        if (config.isTimingDriven()) {
            newPartialPathCost += state.dlyWeight * (childRnode.getDelay() + DelayEstimatorBase.getExtraDelay(childRnode, longParent));
        }

        int childX = childRnode.getEndTileXCoordinate();
        int childY = childRnode.getEndTileYCoordinate();
        RouteNode sinkRnode = connection.getSinkRnode();
        int sinkX = sinkRnode.getBeginTileXCoordinate();
        int sinkY = sinkRnode.getBeginTileYCoordinate();
        int deltaX = Math.abs(childX - sinkX);
        int deltaY = Math.abs(childY - sinkY);
        if (connection.isCrossSLR()) {
            assert(!childRnode.getType().isLocalLeadingToLaguna() || (
                    (connection.isCrossSLRnorth() && childRnode.getType().leadsToNorthboundLaguna()) ||
                    (connection.isCrossSLRsouth() && childRnode.getType().leadsToSouthboundLaguna()) ||
                    (deltaX == 0 && deltaY <= 1)
            ));

            int deltaSLR = Math.abs(sinkRnode.getSLRIndex(routingGraph) - childRnode.getSLRIndex(routingGraph));
            if (deltaSLR != 0) {
                // Check for overshooting which occurs when child and sink node are in
                // adjacent SLRs and less than a SLL wire's length apart in the Y axis.
                if (deltaSLR == 1) {
                    int overshootByY = deltaY - routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES;
                    if (overshootByY < 0) {
                        assert(deltaY < routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES);
                        deltaY = routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES - overshootByY;
                    }
                }

                if (!routingGraph.isVersal) {
                    // For UltraScale/UltraScale+, account for any detours that must be taken
                    // to get to the closest Laguna column and from there onto the sink.
                    // This optimization is not currently performed on Versal due to the
                    // distributed nature of its SLLs.
                    int nextLagunaColumn = routingGraph.nextLagunaColumn[childX];
                    int prevLagunaColumn = routingGraph.prevLagunaColumn[childX];
                    if (nextLagunaColumn == prevLagunaColumn) {
                        // On top of the column
                        assert(deltaX == Math.abs(sinkX - nextLagunaColumn));
                    } else {
                        assert(rnode.getType() != RouteNodeType.SUPER_LONG_LINE);

                        final int deltaXToNextColumn;
                        final int deltaXToPrevColumn;
                        final int deltaXToAndFromNextColumn;
                        final int deltaXToAndFromPrevColumn;
                        if (nextLagunaColumn == Integer.MAX_VALUE  || nextLagunaColumn >= connection.getXMaxBB()) {
                            deltaXToNextColumn = Integer.MAX_VALUE;
                            deltaXToAndFromNextColumn = Integer.MAX_VALUE;
                        } else {
                            deltaXToNextColumn = Math.abs(nextLagunaColumn - childX);
                            deltaXToAndFromNextColumn = deltaXToNextColumn + Math.abs(sinkX - nextLagunaColumn);
                        }
                        if (prevLagunaColumn == Integer.MIN_VALUE || prevLagunaColumn <= connection.getXMinBB()) {
                            deltaXToPrevColumn = Integer.MAX_VALUE;
                            deltaXToAndFromPrevColumn = Integer.MAX_VALUE;
                        } else {
                            deltaXToPrevColumn = Math.abs(prevLagunaColumn - childX);
                            deltaXToAndFromPrevColumn = deltaXToPrevColumn + Math.abs(sinkX - prevLagunaColumn);
                        }
                        if (deltaXToNextColumn == deltaXToPrevColumn) {
                            // Equidistant from both columns, prefer the one closer when considering to/from the sink
                            deltaX = Math.min(deltaXToAndFromNextColumn, deltaXToAndFromPrevColumn);
                        } else if (deltaXToNextColumn < deltaXToPrevColumn &&
                                deltaXToAndFromNextColumn <= deltaXToAndFromPrevColumn + maxDetourToSnapBackToPrevLagunaColumn) {
                            // Closer to the next column and not detouring more than 4 tiles extra to/from using the prev column
                            assert(deltaX <= deltaXToAndFromNextColumn);
                            deltaX = deltaXToAndFromNextColumn;
                        } else if (deltaXToPrevColumn < deltaXToNextColumn &&
                                deltaXToAndFromPrevColumn <= deltaXToAndFromNextColumn + maxDetourToSnapBackToPrevLagunaColumn) {
                            // Closer to the next column and not detouring more than 4 tiles extra to/from using the prev column
                            assert(deltaX <= deltaXToAndFromPrevColumn);
                            deltaX = deltaXToAndFromPrevColumn;
                        } else {
                            // Pretty much same distance to/from both columns; prefer the closer to column
                            deltaX = (deltaXToNextColumn < deltaXToPrevColumn) ? deltaXToAndFromNextColumn
                                                                               : deltaXToAndFromPrevColumn;
                        }
                    }
                    assert(deltaX >= 0 && deltaX < Integer.MAX_VALUE);
                }
            }
        }

        int distanceToSink = deltaX + deltaY;
        float newTotalPathCost = newPartialPathCost + state.estWlWeight * distanceToSink / sharingFactor;
        if (config.isTimingDriven()) {
            newTotalPathCost += state.estDlyWeight * (deltaX * 0.32f + deltaY * 0.16f);
        }

        push(state, childRnode, newPartialPathCost, newTotalPathCost, lookahead);
    }

    /**
     * Gets the congestion cost and bias cost of a rnode.
     * @param rnode The rnode in question.
     * @param connection The connection being routed.
     * @param countSameSourceUsers The number of connections from the same net that are using rnode.
     * Note: a net is represented by its source {@link SitePinInst} Object.
     * @param sharingFactor The sharing factor.
     * @return The sum of the congestion cost and the bias cost of rnode.
     */
    private float getNodeCost(RouteNode rnode, Connection connection, int countSameSourceUsers, float sharingFactor) {
        boolean hasSameSourceUsers = (countSameSourceUsers != 0);
        float presentCongestionCost;

        if (hasSameSourceUsers) {// the rnode is used by other connection(s) from the same net
            int occupancyWithoutThisNet = rnode.getOccupancy() - 1;
            // make the congestion cost less for the current connection
            presentCongestionCost = routingGraph.getPresentCongestionCost(occupancyWithoutThisNet);
        } else {
            presentCongestionCost = rnode.getPresentCongestionCost(routingGraph);
        }

        float baseCost = rnode.getBaseCost();
        NetWrapper net = connection.getNetWrapper();
        float distToCenter = Math.abs(rnode.getEndTileXCoordinate() - net.getXCenter()) + Math.abs(rnode.getEndTileYCoordinate() - net.getYCenter());
        // CRoute paper states that the bias factor cannot be more than half of the wire cost
        // (it may exceed this here because we may not be using the minimum-sized bounding box)
        float biasCost = baseCost / net.getConnections().size() * distToCenter / net.getDoubleHpwl();
        float nodeCost = baseCost * rnode.getHistoricalCongestionCost() * presentCongestionCost / sharingFactor;
        return nodeCost + Math.min(biasCost, nodeCost / 2);
    }

    /**
     * Sets the costs of a rnode and pushes it to the queue.
     * @param state State from the connection that is being routed.
     * @param childRnode A child rnode.
     * @param newPartialPathCost The upstream path cost from childRnode to the source.
     * @param newTotalPathCost Total path cost of childRnode.
     * @param lookahead True to explore this node immediately, rather than to push it onto the queue.
     */
    protected void push(ConnectionState state,
                        RouteNode childRnode,
                        float newPartialPathCost,
                        float newTotalPathCost,
                        boolean lookahead) {
        // Pushed node must have a prev pointer, unless it is a source (with no upstream path cost)
        assert(childRnode.getPrev() != null || newPartialPathCost == 0);
        childRnode.f21LabelOnPush(newTotalPathCost, newPartialPathCost,
                state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence);
        if (lookahead) {
            state.nodesPopped++;
            exploreAndExpand(state, childRnode);
        } else {
            state.queue.add(childRnode);
        }
    }

    /**
     * Prepares for routing a connection, including seeding the routing queue with
     * known-uncongested downstream-from-source routing segments acquired from prior
     * iterations, as well as marking known-uncongested upstream-from-sink segments
     * as targets.
     * @param state State from the connection that is being routed.
     */
    protected void prepareRouteConnection(ConnectionState state) {
        final Connection connection = state.connection;

        // Rips up the connection
        ripUp(connection);
        assert(state.queue.isEmpty());

        // Sets the sink rnode(s) of the connection as the target(s)
        connection.setAllTargets(state);

        // Adds the source rnode to the queue
        RouteNode sourceRnode = connection.getSourceRnode();
        float newPartialPathCost = 0;
        float newTotalPathCost = 0;
        boolean lookahead = false;
        push(state, sourceRnode, newPartialPathCost, newTotalPathCost, lookahead);

        assert(routingGraph.isAllowedTile(connection.getSinkRnode()));
    }

    /**
     * Adds a clock net to the clock net routing targets.
     * @param clk The clock net to be added.
     */
    public void addClkNet(Net clk) {
        clkNets.add(clk);
    }

    public Design getDesign() {
        return design;
    }

    protected int getNumIndirectConnectionPins() {
        return indirectConnections.size();
    }

    protected int getNumConnectionsCrossingSLRs() {
        int numCrossingSLRs = 0;
        for (Connection c : indirectConnections) {
            numCrossingSLRs += c.isCrossSLR() ? 1 : 0;
        }
        return numCrossingSLRs;
    }

    private int getNumStaticNetPins() {
        int totalSitePins = 0;
        for (Entry<Net,List<SitePinInst>> e : staticNetAndRoutingTargets.entrySet()) {
            List<SitePinInst> pins = e.getValue();
            totalSitePins += pins.size();
        }
        return totalSitePins;
    }

    private void printDesignNetsAndConfigurationInfo(boolean verbose) {
        printDesignInfo(verbose);
        if (config.isVerbose()) printConnectionSpanStatistics();
        printConfiguration(verbose);
    }

    private void printConfiguration(boolean verbose) {
        if (verbose) System.out.println(config);
    }

    private void printTimingInfo() {
        if (!sortedIndirectConnections.isEmpty()) {
            timingManager.getCriticalPathInfo(maxDelayAndTimingVertex, false, routingGraph);
        }
    }

    public static void printNodeTypeUsageAndWirelength(boolean verbose, Map<IntentCode, Long> nodeTypeUsage, Map<IntentCode, Long> nodeTypeLength, Series series) {
        if (verbose) {
            System.out.println("Node Usage Per Type");
            System.out.printf(" %-16s  %13s  %12s\n", "Node Type", "Usage", "Length");
            List<IntentCode> nodeTypeList = (series == Series.Versal) ? nodeUsageForVersal : nodeUsageForUltraScale;
            for (IntentCode ic : nodeTypeList) {
                long usage = nodeTypeUsage.getOrDefault(ic, 0L);
                long length = nodeTypeLength.getOrDefault(ic, 0L);
                System.out.printf(" %-16s  %13d  %12d\n", ic, usage, length);
            }
            System.out.println();
        }
    }

    private void printDesignInfo(boolean verbose) {
        if (!verbose) return;
        System.out.println("------------------------------------------------------------------------------");
        printFormattedString("Total nets: ", design.getNets().size());
        printFormattedString("Routable nets: ", numPreservedRoutableNets + numPreservedClks + numPreservedStaticNets + nets.size() + staticNetAndRoutingTargets.size() + clkNets.size());
        printFormattedString("  Preserved routable nets: ", numPreservedRoutableNets);
        printFormattedString("    GLOBAL_CLOCK: ", numPreservedClks);
        printFormattedString("    Static nets: ", numPreservedStaticNets);
        printFormattedString("    WIRE: ", numPreservedWire);
        printFormattedString("  Nets to be routed: ", (nets.size() +  staticNetAndRoutingTargets.size() + clkNets.size()));
        printFormattedString("    GLOBAL_CLOCK: ", clkNets.size());
        printFormattedString("    Static nets: ", staticNetAndRoutingTargets.size());
        printFormattedString("    WIRE: ", nets.size());
        int clkPins = 0;
        for (Net clk : clkNets) {
            clkPins += clk.getSinkPins().size();
        }
        int indirectPins = getNumIndirectConnectionPins();
        int staticPins = getNumStaticNetPins();
        printFormattedString("  All site pins to be routed: ", (indirectPins + staticPins + clkPins));
        printFormattedString("    Connections to be routed: ", indirectPins);
        printFormattedString("      With SLR crossings: ", getNumConnectionsCrossingSLRs());
        printFormattedString("    Static net pins: ", staticPins);
        printFormattedString("    Clock pins: ", clkPins);
        printFormattedString("Nets not needing routing: ", numNotNeedingRoutingNets);
        if (numUnrecognizedNets != 0)
            printFormattedString("Nets unrecognized: ", numUnrecognizedNets);
        System.out.printf("------------------------------------------------------------------------------\n");
    }

    private static void printFormattedString(String s, int value) {
        System.out.print(MessageGenerator.formatString(s, value));
    }

    private static void printFormattedString(String s, long value) {
        System.out.print(MessageGenerator.formatString(s, value));
    }

    void fidesRunDeferredStats() {
        computesNodeUsageAndTotalWirelength();
        printNodeTypeUsageAndWirelength(config.isVerbose(), nodeTypeUsage, nodeTypeLength, design.getSeries());
        printFormattedString("Total wirelength:", totalWL);
    }

    protected void printRoutingStatistics() {
        // The exclusive loop upper bound leaves routeIteration one past the last
        // completed iteration when routing reaches its limit without converging.
        final int iterationsCompleted = Math.min(routeIteration, config.getMaxIterations() - 1);
        MessageGenerator.printHeader("Statistics");
        if (PartialRouter.fidesOverlapPostActive && goldenWriter == null) {
            PartialRouter.fidesLastRouter = this;
        } else {
            computesNodeUsageAndTotalWirelength();
            printNodeTypeUsageAndWirelength(config.isVerbose(), nodeTypeUsage, nodeTypeLength, design.getSeries());
            printFormattedString("Total wirelength:", totalWL);
        }
        if (config.isVerbose()) {
            printFormattedString("Total INT tile nodes:", totalINTNodes);
            printFormattedString("Total rnodes created:", routingGraph.numNodes());
            printFormattedString("Average #children per node:", routingGraph.averageChildren());
            System.out.printf("------------------------------------------------------------------------------\n");
            printFormattedString("Num iterations:", iterationsCompleted);
            printFormattedString("Connections routed:", connectionsRouted.get());
            printFormattedString("Nodes pushed:", nodesPushed.get());
        }
        printFormattedString("Nodes popped:", nodesPopped.get());
        if (config.isVerbose()) {
            System.out.printf("------------------------------------------------------------------------------\n");
        }

        System.out.print(routerTimer);
        if (config.isTimingDriven()) {
            MessageGenerator.printHeader("Timing Report");
            printTimingInfo();
        }
        System.out.printf("==============================================================================\n");

        // For testing
        System.setProperty("rapidwright.rwroute.sortStrategy", config.getConnectionSortStrategy().name());
	System.setProperty("rapidwright.rwroute.nodesPopped", String.valueOf(nodesPopped));
        System.setProperty("rapidwright.rwroute.numStaticNetPins", String.valueOf(getNumStaticNetPins()));
	if (goldenWriter != null) {
            // Global short count = number of rnodes shared by more than one net.
            // RouteNode.capacity == 1, so isOverUsed() <=> getOccupancy() (== uniqueUserCount())
            // >= 2. This is the fail-closed gate: any non-zero value must abort the GPU path and
            // fall back to the serial router.
            long shorts = 0;
            for (RouteNode rn : routingGraph.getRnodes()) {
                if (rn.isOverUsed()) {
                    shorts++;
                }
            }
            goldenWriter.println("SUMMARY nodesPopped=" + nodesPopped.get()
                    + " nodesPushed=" + nodesPushed.get()
                    + " wirelength=" + totalWL
                    + " iterations=" + iterationsCompleted
                    + " shorts=" + shorts);
            goldenWriter.close();
        }

        /* The fail-closed counter, on stdout rather than in the golden file: the SUMMARY line above is
         * frozen and byte-diffed against the M2 trace, so a new field there would break the very
         * comparison this line exists to protect.  Printed unconditionally -- including the pristine
         * zero -- so that a driver can require the line's presence: an absent line means the run never
         * reached here, and that must not be able to read as "no fallbacks". */
        System.out.println("FIDES fallbackAttempts=" + fallbackAttempts);

        /* M5's backtracking counters, a third unconditional stdout line for the same reason the two
         * around it are unconditional: the frozen SUMMARY has no field for them, and the whole point of
         * the denominator is that it prints when it is zero.  A reader that required this line and got
         * nothing must be able to tell "the method never ran" from "the method ran and behaved" -- an
         * absent line means the run never reached here, and that must not be able to read as "quiet". */
        System.out.println("FIDES backtracks calls=" + backtrackCalls
                + " unexpected=" + unexpectedBacktracks
                + " silent=" + silentBacktracks);

        /* Criterion 2c's witness, and it is a second unconditional stdout line for the same reason the
         * one above is: the frozen SUMMARY has no field for it, and every other place this number is
         * computed is on a path that only prints when it is non-empty.  {@code getUnroutableConnections}
         * is exactly "an indirect connection with no path" -- it filters sortedIndirectConnections on
         * isRouted -- so this one line is both halves of 2c's statement about paths: the count is the
         * number of connections that did not get one, and zero is the claim that every one of them did.
         *
         * The serial value is 0 and it is not a coincidence that it is zero here: the iterative loop
         * only breaks out of its last iteration when unroutableConnections is empty, and a round that
         * exhausts maxIterations prints "ERROR: Routing terminated after" instead of reaching this line.
         * So the number is a re-derivation rather than a second opinion -- which is the point.  The W>1
         * track is where the claim stops being free: batching changes WHICH path a connection takes, and
         * 2c asserts it must not change WHETHER one exists, so this counter has to be read on that track
         * and compared for equality (== 0 == the serial value), never as "<= the serial value". */
        System.out.println("FIDES unroutable=" + getUnroutableConnections().size());
    }

    /**
     * Routes a design in the full timing-driven routing mode.
     * @param design The {@link Design} instance to be routed.
     */
    public static Design routeDesignFullTimingDriven(Design design) {
        return routeDesignWithUserDefinedArguments(design, null);
    }

    /**
     * Routes a design in the full non-timing-driven routing mode.
     * @param design The {@link Design} instance to be routed.
     */
    public static Design routeDesignFullNonTimingDriven(Design design) {
        return routeDesignWithUserDefinedArguments(design, new String[] {"--nonTimingDriven"});
    }

    /**
     * Routes a {@link Design} instance.
     * @param design The {@link Design} instance to be routed.
     * @param args An array of string arguments, can be null.
     * If null, the design will be routed in the full timing-driven routing mode with default a {@link RWRouteConfig} instance.
     * For more options of the configuration, please refer to the {@link RWRouteConfig} class.
     * @return Routed design.
     */
    public static Design routeDesignWithUserDefinedArguments(Design design, String[] args) {
        // Instantiates a RWRouteConfig Object and parses the arguments.
        // Uses the default configuration if basic usage only.
        return routeDesign(design, new RWRouteConfig(args));
    }

    private static Design routeDesign(Design design, RWRouteConfig config) {
        if (config.isTimingDriven() && !config.isMaskNodesCrossRCLK()) {
            System.out.println("WARNING: Not masking nodes across RCLK could result in delay optimism.");
        }

        return routeDesign(new RWRoute(design, config));
    }

    /**
     * Routes a design after pre-processing.
     * @param router A {@link RWRoute} object to be used to route the design.
     */
    protected static Design routeDesign(RWRoute router) {
        router.preprocess();

        // Initialize router object
        router.initialize();

        // Routes the design
        router.route();

        return router.getDesign();
    }

    /**
     * The main interface of {@link RWRoute} that reads in a {@link Design} design
     * (DCP or FPGA Interchange), and parses the arguments for the
     * {@link RWRouteConfig} object of the router.
     * 
     * @param args An array of strings that are used to create a
     *             {@link RWRouteConfig} object for the router.
     */
    public static void main(String[] args) {
        if (args.length < 2) {
            System.out.println("USAGE: <input.dcp|input.phys> <output.dcp>");
            return;
        }
        // Reads the output directory and set the output design checkpoint file name
        String routedDCPfileName = args[1];

        CodePerfTracker t = new CodePerfTracker("RWRoute", true);

        // Reads in a design and routes it
        String[] rwrouteArgs = Arrays.copyOfRange(args, 2, args.length);
        Design input;
        if (Interchange.isInterchangeFile(args[0])) {
            input = Interchange.readInterchangeDesign(args[0]);
        } else {
            input = Design.readCheckpoint(args[0]);
        }
        Design routed = routeDesignWithUserDefinedArguments(input, rwrouteArgs);

        // Writes out the routed design checkpoint
        routed.writeCheckpoint(routedDCPfileName,t);
        System.out.println("\nINFO: Wrote routed design\n " + routedDCPfileName + "\n");
    }

}
