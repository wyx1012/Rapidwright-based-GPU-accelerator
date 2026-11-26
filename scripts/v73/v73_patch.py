"""Patch v67/v70 sources to v73:
1. F2Prewarm: remove hardcoded THEIA_50per counts (4_909_155 / 4_952_719) -> native all-13 support.
2. PartialRouter + RWRoute:
   - Skip duplicate warmTilePIPs(Device) before writeCheckpoint.
   - Skip redundant checkPIPsUsage() when in-process 64-shard write/check overlap is active.
   - Cap checkPool workers to <= 34 (leaving 2 cores for writeCheckpoint) and run deferred STATS_WL
     sequentially inside checkFuture right after chk finishes (single pool, no thread storm!).
"""

import v67_patch as v67


def once(source, anchor, updated):
    return v67.once(source, anchor, updated)


def cuda(src):
    return v67.cuda(src)


def node(src):
    return v67.node(src)


def broker(src):
    return v67.broker(src)


def diversion(src):
    return v67.diversion(src)


def corridor(src):
    return v67.corridor(src)


def idmap(src):
    return v67.idmap(src)


def f2prewarm(src):
    src = v67.f2prewarm(src)
    src = once(
        src,
        "        if (indirectCount != 4_909_155 || nAll != 4_952_719) {",
        "        if (indirectCount < 0 || nAll <= 0) {",
    )
    return src


def gate(src):
    return v67.gate(src)


def handoff(src):
    return v67.handoff(src)


def worker(src):
    return v67.worker(src)


def partial_router(src):
    src = v67.partial_router(src)
    src = once(
        src,
        "    private static volatile java.util.concurrent.CompletableFuture<Void> v67PreloadFuture;",
        """    static volatile boolean fidesOverlapPostActive = false;
    static volatile RWRoute fidesLastRouter = null;
    static volatile com.xilinx.rapidwright.device.Device fidesWarmedDevice = null;
    private static volatile java.util.concurrent.CompletableFuture<Void> v67PreloadFuture;""",
    )
    src = once(
        src,
        """    private static void warmTilePIPs(Design design) {
        var warmedTiles = new HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTiles.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
                if (tile.getWireCount() > 0) {
                    tile.getForwardTilePIPs(0);
                }
            }
        }
    }""",
        """    private static void warmTilePIPs(Design design) {
        com.xilinx.rapidwright.device.Device dev = design.getDevice();
        if (fidesWarmedDevice == dev) return;
        var warmedTiles = new HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : dev.getAllTiles()) {
            if (warmedTiles.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
                if (tile.getWireCount() > 0) {
                    tile.getForwardTilePIPs(0);
                }
            }
        }
        fidesWarmedDevice = dev;
    }""",
    )
    src = once(
        src,
        """        F2Prewarm.start();
        startV67Preload(rwrouteArgs);
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);""",
        """        F2Prewarm.start();
        startV67Preload(rwrouteArgs);
        fidesOverlapPostActive = !Boolean.getBoolean("fides.v66.skipInprocDcpCheck");
        fidesLastRouter = null;
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);""",
    )
    src = once(
        src,
        """                long tPin0 = System.nanoTime();
                final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));""",
        """                long tPin0 = System.nanoTime();
                final int workers = Math.max(1, Math.min(34, Runtime.getRuntime().availableProcessors() - 2));""",
    )
    src = once(
        src,
        """                        long chkNs = System.nanoTime() - tChk0;
                        return new long[] { unroutedPins, sharedCount, totalPips, chkNs };
                    }, checkPool);

                    long tWr0 = System.nanoTime();
                    routed.writeCheckpoint(routedDCPfileName, t);
                    long tWr1 = System.nanoTime();
                    long[] chk = checkFuture.get();
                    long tEnd = System.nanoTime();
                    System.out.printf(
                            "F4_V66_INPROC_DCP_CHECK pins=%.2fs overlap_wall=%.2fs write_s=%.2fs check_s=%.2fs%n",
                            (tPin1 - tPin0) / 1e9, (tEnd - tWr0) / 1e9, (tWr1 - tWr0) / 1e9, chk[3] / 1e9);
                    System.out.printf("F4_BASE_DCP unroutedPins=%d sharedPipNodes=%d pips=%d%n",
                            chk[0], chk[1], chk[2]);""",
        """                        long chkNs = System.nanoTime() - tChk0;
                        final RWRoute deferredRouter = fidesLastRouter;
                        long statsNs = 0L;
                        if (deferredRouter != null) {
                            long tS0 = System.nanoTime();
                            deferredRouter.fidesRunDeferredStats();
                            statsNs = System.nanoTime() - tS0;
                        }
                        return new long[] { unroutedPins, sharedCount, totalPips, chkNs, statsNs };
                    }, checkPool);

                    long tWr0 = System.nanoTime();
                    routed.writeCheckpoint(routedDCPfileName, t);
                    long tWr1 = System.nanoTime();
                    long[] chk = checkFuture.get();
                    long tEnd = System.nanoTime();
                    if (chk[0] != 0L || chk[1] != 0L || chk[2] <= 0L) {
                        throw new IllegalStateException(String.format(
                                "In-process DCP check failed: unrouted=%d shared=%d pips=%d",
                                chk[0], chk[1], chk[2]));
                    }
                    System.out.printf(
                            "F4_V66_INPROC_DCP_CHECK pins=%.2fs overlap_wall=%.2fs write_s=%.2fs check_s=%.2fs stats_s=%.2fs%n",
                            (tPin1 - tPin0) / 1e9, (tEnd - tWr0) / 1e9, (tWr1 - tWr0) / 1e9, chk[3] / 1e9, chk[4] / 1e9);
                    System.out.printf("F4_BASE_DCP unroutedPins=%d sharedPipNodes=%d pips=%d%n",
                            chk[0], chk[1], chk[2]);""",
    )
    return src


def router(src):
    src = v67.router(src)
    src = once(
        src,
        """        Set<Object> warmedTileTypes = new HashSet<>();
        for (Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
            }
        }""",
        """        Set<Object> warmedTileTypes = new HashSet<>();
        if (PartialRouter.fidesWarmedDevice != design.getDevice()) {
            for (Tile tile : design.getDevice().getAllTiles()) {
                if (warmedTileTypes.add(tile.getTileTypeEnum())) {
                    tile.getPIP(0, 0);
                }
            }
        }""",
    )
    src = once(
        src,
        """        // Pre-processing of the design regarding physical net names pins
        DesignTools.makePhysNetNamesConsistent(design);
        DesignTools.createPossiblePinsToStaticNets(design);
        DesignTools.createMissingSitePinInsts(design);""",
        """        long tPre0 = System.nanoTime();
        DesignTools.makePhysNetNamesConsistent(design);
        long tPre1 = System.nanoTime();
        DesignTools.createPossiblePinsToStaticNets(design);
        long tPre2 = System.nanoTime();
        DesignTools.createMissingSitePinInsts(design);
        long tPre3 = System.nanoTime();
        System.out.printf("F4_V71_PREPROC makePhys=%.2fs staticPins=%.2fs missingPins=%.2fs%n",
                (tPre1 - tPre0) / 1e9, (tPre2 - tPre1) / 1e9, (tPre3 - tPre2) / 1e9);""",
    )
    src = once(
        src,
        """        System.out.printf("F4_V37_SETPIPS nets=%d tileTypes=%d time_s=%.2f%n", nNets, warmedTileTypes.size(), (System.nanoTime() - t0) / 1e9);

        checkPIPsUsage();""",
        """        System.out.printf("F4_V37_SETPIPS nets=%d tileTypes=%d trackChanges=%b time_s=%.2f%n",
                nNets, warmedTileTypes.size(), trackChanges, (System.nanoTime() - t0) / 1e9);

        if (!PartialRouter.fidesOverlapPostActive) {
            checkPIPsUsage();
        }""",
    )
    src = once(
        src,
        """    protected void printRoutingStatistics() {
        // The exclusive loop upper bound leaves routeIteration one past the last
        // completed iteration when routing reaches its limit without converging.
        final int iterationsCompleted = Math.min(routeIteration, config.getMaxIterations() - 1);
        MessageGenerator.printHeader("Statistics");
        computesNodeUsageAndTotalWirelength();
        printNodeTypeUsageAndWirelength(config.isVerbose(), nodeTypeUsage, nodeTypeLength, design.getSeries());
        printFormattedString("Total wirelength:", totalWL);""",
        """    void fidesRunDeferredStats() {
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
        }""",
    )
    return src
