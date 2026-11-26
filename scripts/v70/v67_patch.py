"""Patch v66 sources to v67 (Stage-2 Fixed-Stage End-to-End Pipelining):
1. PartialRouter.main: overlap FidesRrgLoader + FidesRrgIdMap + C++ nativePreinitStatic
   concurrently with Design.readCheckpoint().
2. PartialRouter.preprocess + getUnroutedPins: 36-core parallel Tile PIP warmup +
   DesignTools.updatePinsIsRouted(net) + deterministic chunked getUnroutedPins().
3. RWRoute.initialize() + F2Prewarm: launch F2Prewarm.initializeAsync right after
   autoTuneRoutingProfileByDesignSize() (before F1Corridor + static nets), awaiting in
   RWRoute.route() before f4PrecomputeDirect().
4. FidesLaneGate: consume preloaded RRG/IdMap cleanly (fail-closed path match) and
   call FidesNativeCpu.nativeAttachDynamicState() in f4NativeInit() with fallback.
"""

import v66_patch as v66


def once(source, anchor, updated):
    return v66.once(source, anchor, updated)


def cuda(src):
    return v66.cuda(src)


def node(src):
    return v66.node(src)


def broker(src):
    return v66.broker(src)


def diversion(src):
    return v66.diversion(src)


def corridor(src):
    return v66.corridor(src)


def idmap(src):
    return v66.idmap(src)


def handoff(src):
    return v66.handoff(src)


def worker(src):
    return v66.worker(src)


def partial_router(src):
    src = v66.partial_router(src)
    src = once(
        src,
        """    public static List<SitePinInst> getUnroutedPins(Design design) {
        List<SitePinInst> pinsToRoute = new ArrayList<>();
        for (Net net : design.getNets()) {
            if (net.getSource() == null && !net.isStaticNet()) {
                // Source-less nets may exist since this is an out-of-context design
                continue;
            }
            for (SitePinInst spi : net.getPins()) {
                if (spi.isRouted() || spi.isOutPin()) {
                    continue;
                }
                pinsToRoute.add(spi);
            }
        }
        return pinsToRoute;
    }

    /**
     * Calls {@link RWRoute#preprocess(Design)} to preprocess the design, and furthermore
     * update the SitePinInst.isRouted() result for all pins in the design.
     * @param design Design to preprocess
     */
    public static void preprocess(Design design) {
        RWRoute.preprocess(design);
        DesignTools.updatePinsIsRouted(design);
    }""",
        """    private static volatile java.util.concurrent.CompletableFuture<Void> v67PreloadFuture;

    private static String extractFlagValue(String[] args, String flag) {
        for (int i = 0; i + 1 < args.length; ++i) {
            if (flag.equals(args[i])) return args[i + 1];
        }
        return null;
    }

    static void startV67Preload(String[] rwrouteArgs) {
        final String rrgPath = extractFlagValue(rwrouteArgs, "--fidesLaneRrg");
        final String manifestPath = extractFlagValue(rwrouteArgs, "--fidesLaneManifest");
        final String workDir = System.getProperty("fides.ppt.f4.work");
        if (rrgPath == null && workDir == null) return;
        v67PreloadFuture = java.util.concurrent.CompletableFuture.runAsync(() -> {
            long tWall0 = System.nanoTime();
            final double[] times = new double[2];
            java.util.concurrent.CompletableFuture<Void> javaJob = java.util.concurrent.CompletableFuture.runAsync(() -> {
                if (rrgPath != null && manifestPath != null) {
                    long t0 = System.nanoTime();
                    try {
                        FidesLaneGate.preloadStatic(rrgPath, manifestPath);
                    } catch (Throwable t) {
                        System.out.println("WARN: F4_V67_JAVA_PRELOAD failed (fallback): " + t);
                    }
                    times[0] = (System.nanoTime() - t0) / 1e9;
                }
            }, runnable -> {
                Thread th = new Thread(runnable, "fides-java-preload");
                th.setDaemon(true);
                th.start();
            });
            java.util.concurrent.CompletableFuture<Void> cppJob = java.util.concurrent.CompletableFuture.runAsync(() -> {
                if (workDir != null && rrgPath != null) {
                    String libPath = workDir + "/libfides_cpu.so";
                    String layoutPath = workDir + "/preflight-access-layout.txt";
                    if (java.nio.file.Files.isRegularFile(java.nio.file.Path.of(libPath))
                            && java.nio.file.Files.isRegularFile(java.nio.file.Path.of(rrgPath))
                            && java.nio.file.Files.isRegularFile(java.nio.file.Path.of(layoutPath))) {
                        long t0 = System.nanoTime();
                        try {
                            FidesNativeCpu.load(libPath);
                            FidesNativeCpu.nativePreinitStatic(rrgPath, layoutPath);
                        } catch (Throwable t) {
                            System.out.println("WARN: F4_V67_CPP_PREINIT failed (fallback): " + t);
                        }
                        times[1] = (System.nanoTime() - t0) / 1e9;
                    }
                }
            }, runnable -> {
                Thread th = new Thread(runnable, "fides-cpp-preinit");
                th.setDaemon(true);
                th.start();
            });
            java.util.concurrent.CompletableFuture.allOf(javaJob, cppJob).join();
            System.out.printf("F4_V67_PRELOAD java_s=%.2f cpp_s=%.2f wall_s=%.2f%n",
                    times[0], times[1], (System.nanoTime() - tWall0) / 1e9);
        }, runnable -> {
            Thread th = new Thread(runnable, "fides-v67-preload-coord");
            th.setDaemon(true);
            th.start();
        });
    }

    static void awaitV67Preload() {
        java.util.concurrent.CompletableFuture<Void> f = v67PreloadFuture;
        if (f != null) f.join();
    }

    private static void warmTilePIPs(Design design) {
        var warmedTiles = new HashSet<Object>();
        for (com.xilinx.rapidwright.device.Tile tile : design.getDevice().getAllTiles()) {
            if (warmedTiles.add(tile.getTileTypeEnum())) {
                tile.getPIP(0, 0);
                if (tile.getWireCount() > 0) {
                    tile.getForwardTilePIPs(0);
                }
            }
        }
    }

    private static List<Net> updatePinsIsRoutedParallel(Design design, java.util.concurrent.ForkJoinPool pool, int workers) {
        warmTilePIPs(design);
        final List<Net> allNets = new ArrayList<>(design.getNets());
        final int nNets = allNets.size();
        final int chunk = Math.max(2048, (nNets + workers * 4 - 1) / (workers * 4));
        var pinJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
        for (int from = 0; from < nNets; from += chunk) {
            final int f = from;
            final int end = Math.min(nNets, from + chunk);
            pinJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                var visited = Collections.newSetFromMap(
                        new java.util.IdentityHashMap<com.xilinx.rapidwright.design.NetTools.NodeTree, Boolean>());
                for (int i = f; i < end; ++i) {
                    DesignTools.updatePinsIsRouted(allNets.get(i), visited);
                    visited.clear();
                }
            }, pool));
        }
        java.util.concurrent.CompletableFuture.allOf(
                pinJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        return allNets;
    }

    /**
     * Return all SitePinInst objects belonging to fully unrouted nets (containing no routing PIPs).
     * @param design The {@link Design} instance to be examined.
     * @return A list of unrouted SitePinInst objects.
     */
    public static List<SitePinInst> getUnroutedPins(Design design) {
        long t0 = System.nanoTime();
        final List<Net> allNets = new ArrayList<>(design.getNets());
        final int nNets = allNets.size();
        final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
        final int chunk = Math.max(2048, (nNets + workers * 4 - 1) / (workers * 4));
        final int numChunks = Math.max(1, (nNets + chunk - 1) / chunk);
        @SuppressWarnings("unchecked")
        final List<SitePinInst>[] chunkPins = new List[numChunks];
        final var pool = new java.util.concurrent.ForkJoinPool(workers);
        try {
            var jobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(numChunks);
            for (int ci = 0; ci < numChunks; ++ci) {
                final int idx = ci;
                final int f = ci * chunk;
                final int end = Math.min(nNets, f + chunk);
                jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                    List<SitePinInst> local = new ArrayList<>();
                    for (int i = f; i < end; ++i) {
                        Net net = allNets.get(i);
                        if (net.getSource() == null && !net.isStaticNet()) {
                            continue;
                        }
                        for (SitePinInst spi : net.getPins()) {
                            if (spi.isRouted() || spi.isOutPin()) {
                                continue;
                            }
                            local.add(spi);
                        }
                    }
                    chunkPins[idx] = local;
                }, pool));
            }
            java.util.concurrent.CompletableFuture.allOf(
                    jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        } finally {
            pool.shutdown();
        }
        List<SitePinInst> pinsToRoute = new ArrayList<>();
        for (int ci = 0; ci < numChunks; ++ci) {
            if (chunkPins[ci] != null) pinsToRoute.addAll(chunkPins[ci]);
        }
        System.out.printf("F4_V67_UNROUTED count=%d time_s=%.2f%n", pinsToRoute.size(), (System.nanoTime() - t0) / 1e9);
        return pinsToRoute;
    }

    /**
     * Calls {@link RWRoute#preprocess(Design)} to preprocess the design, and furthermore
     * update the SitePinInst.isRouted() result for all pins in the design.
     * @param design Design to preprocess
     */
    public static void preprocess(Design design) {
        long t0 = System.nanoTime();
        RWRoute.preprocess(design);
        long t1 = System.nanoTime();
        final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
        final var pool = new java.util.concurrent.ForkJoinPool(workers);
        try {
            updatePinsIsRoutedParallel(design, pool, workers);
        } finally {
            pool.shutdown();
        }
        long t2 = System.nanoTime();
        System.out.printf("F4_V67_PREPROC rwPre=%.2fs updatePins=%.2fs%n", (t1 - t0) / 1e9, (t2 - t1) / 1e9);
    }""",
    )
    src = once(
        src,
        """        String[] rwrouteArgs = Arrays.copyOfRange(args, 2, args.length);
        F2Prewarm.start();
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);""",
        """        String[] rwrouteArgs = Arrays.copyOfRange(args, 2, args.length);
        F2Prewarm.start();
        startV67Preload(rwrouteArgs);
        try {
            Design routed = routeDesignWithUserDefinedArguments(Design.readCheckpoint(args[0]), rwrouteArgs);""",
    )
    src = once(
        src,
        """                long tPin0 = System.nanoTime();
                var warmedTiles = new HashSet<Object>();
                for (com.xilinx.rapidwright.device.Tile tile : routed.getDevice().getAllTiles()) {
                    if (warmedTiles.add(tile.getTileTypeEnum())) {
                        tile.getPIP(0, 0);
                        if (tile.getWireCount() > 0) {
                            tile.getForwardTilePIPs(0);
                        }
                    }
                }
                final List<Net> allNets = new ArrayList<>(routed.getNets());
                final int nNets = allNets.size();
                final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
                final java.util.concurrent.ForkJoinPool checkPool = new java.util.concurrent.ForkJoinPool(workers);
                try {
                    final int chunk = Math.max(2048, (nNets + workers * 4 - 1) / (workers * 4));
                    var pinJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>();
                    for (int from = 0; from < nNets; from += chunk) {
                        final int f = from;
                        final int end = Math.min(nNets, from + chunk);
                        pinJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                            for (int i = f; i < end; ++i) {
                                DesignTools.updatePinsIsRouted(allNets.get(i));
                            }
                        }, checkPool));
                    }
                    java.util.concurrent.CompletableFuture.allOf(
                            pinJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
                    long tPin1 = System.nanoTime();""",
        """                long tPin0 = System.nanoTime();
                final int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
                final java.util.concurrent.ForkJoinPool checkPool = new java.util.concurrent.ForkJoinPool(workers);
                try {
                    final List<Net> allNets = updatePinsIsRoutedParallel(routed, checkPool, workers);
                    final int nNets = allNets.size();
                    final int chunk = Math.max(2048, (nNets + workers * 4 - 1) / (workers * 4));
                    long tPin1 = System.nanoTime();""",
    )
    return src


def f2prewarm(src):
    src = v66.f2prewarm(src)
    src = once(
        src,
        """    private final ExecutorService worker = Executors.newSingleThreadExecutor(task -> new Thread(task, "f2-context"));
    private Future<?> ready;
    private CUcontext context;
    private long contextWorker, contextSchedule, contextStart, contextReady;
    private boolean completed;""",
        """    private final ExecutorService worker = Executors.newSingleThreadExecutor(task -> new Thread(task, "f2-context"));
    private Future<?> ready;
    private Future<?> pendingCompute;
    private CUcontext context;
    private long contextWorker, contextSchedule, contextStart, contextReady;
    private boolean completed;""",
    )
    src = once(
        src,
        """    static void initialize(List<Connection> indirect, List<Connection> direct) {
        if (session != null) session.compute(indirect, direct);
    }""",
        """    static void initializeAsync(List<Connection> indirect, List<Connection> direct) {
        if (session != null) session.computeAsync(indirect, direct);
    }

    static void initialize(List<Connection> indirect, List<Connection> direct) {
        if (session != null) session.awaitOrCompute(indirect, direct);
    }""",
    )
    src = once(
        src,
        """    private void compute(List<Connection> indirect, List<Connection> direct) {
        if (completed || Thread.currentThread().getId() != routerThread) throw new IllegalStateException("F2 host thread");
        try {
            long geometryStart = stamp();
            int[] input = geometry(indirect, direct);""",
        """    private void computeAsync(List<Connection> indirect, List<Connection> direct) {
        if (completed || Thread.currentThread().getId() != routerThread) throw new IllegalStateException("F2 host thread");
        if (pendingCompute != null) return;
        final List<Connection> indirectSnap = new ArrayList<>(indirect);
        final List<Connection> directSnap = new ArrayList<>(direct);
        ExecutorService exec = Executors.newSingleThreadExecutor(r -> {
            Thread th = new Thread(r, "f2-async-prep");
            th.setDaemon(true);
            return th;
        });
        pendingCompute = exec.submit(() -> {
            try {
                runComputeBody(indirectSnap, directSnap);
            } finally {
                exec.shutdown();
            }
        });
    }

    private void awaitOrCompute(List<Connection> indirect, List<Connection> direct) {
        if (Thread.currentThread().getId() != routerThread) throw new IllegalStateException("F2 host thread");
        if (pendingCompute != null) {
            try {
                pendingCompute.get();
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("Interrupted waiting for async F2 feature preparation", error);
            } catch (Exception error) {
                throw new IllegalStateException("Async F2 prewarm feature preparation failed", error);
            } finally {
                pendingCompute = null;
            }
            return;
        }
        runComputeBody(indirect, direct);
    }

    private void runComputeBody(List<Connection> indirect, List<Connection> direct) {
        if (completed) return;
        try {
            long geometryStart = stamp();
            int[] input = geometry(indirect, direct);""",
    )
    return src


def gate(src):
    src = v66.gate(src)
    src = once(
        src,
        """    private final FidesRrgLoader rrg;
    private final FidesRrgIdMap idMap;""",
        """    private static final class PreloadedStatic {
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
        if (cur != null && cur.matches(rrgPath, manifestPath)) return;
        FidesRrgLoader loadedRrg = FidesRrgLoader.load(Paths.get(rrgPath));
        FidesRrgIdMap loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), loadedRrg);
        v67Preloaded = new PreloadedStatic(rrgPath, manifestPath, loadedRrg, loadedMap);
    }

    private final FidesRrgLoader rrg;
    private final FidesRrgIdMap idMap;""",
    )
    src = once(
        src,
        """            f4NativeReady = FidesNativeCpu.nativeInit(rrgPath, layoutPath, slrData,
                    routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                    occArr, histArr, routingGraph.presentCongestionCosts);""",
        """            PartialRouter.awaitV67Preload();
            f4NativeReady = FidesNativeCpu.nativeAttachDynamicState(slrData,
                    routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                    occArr, histArr, routingGraph.presentCongestionCosts);
            if (!f4NativeReady) {
                f4NativeReady = FidesNativeCpu.nativeInit(rrgPath, layoutPath, slrData,
                        routingGraph.SUPER_LONG_LINE_LENGTH_IN_TILES, nextLag, prevLag,
                        occArr, histArr, routingGraph.presentCongestionCosts);
            }""",
    )
    src = once(
        src,
        """        this.slrData = FidesLaneSlrData.fromGraph(graph);
        this.rrg = FidesRrgLoader.load(Paths.get(config.getFidesLaneRrg()));
        this.idMap = FidesRrgIdMap.open(Paths.get(config.getFidesLaneManifest()), rrg);""",
        """        long tGate0 = System.nanoTime();
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
        }""",
    )
    return src


def router(src):
    src = v66.router(src)
    src = once(
        src,
        """        determineRoutingTargets();
        ensureSinkRoutability();
        autoTuneRoutingProfileByDesignSize();
        if (System.getProperty("fides.ppt.f4.features") != null) {""",
        """        determineRoutingTargets();
        ensureSinkRoutability();
        autoTuneRoutingProfileByDesignSize();
        F2Prewarm.initializeAsync(indirectConnections, directConnections);
        if (System.getProperty("fides.ppt.f4.features") != null) {""",
    )
    return src
