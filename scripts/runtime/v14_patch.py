"""Attach PPT 11 admission to the existing CUDA worker and ordered router."""
from v11_window import replace_once as once


def router(source, work):
    start = source.index("    protected void routeIndirectConnectionsInBatches(")
    end = source.index("    /**\n     * The {@code ConnectionState} for slot", start)
    loop = source[start:end]
    loop = once(loop, "        final int batchSize = f4Direct != null ? f4Width : config.getFidesLaneBatch();",
                "        final int batchSize = config.getFidesLaneBatch();")
    left = loop.index("        List<Connection> ordered")
    right = loop.index("            /* The flush", left)
    loop = loop[:left] + "        for (Connection connection : connections) {\n" + loop[right:]
    left = loop.index("                    int cpuWindow")
    right = loop.index("\n                } else {\n                    /* The fail-closed decline", left) + 1
    loop = loop[:left] + """                    collectLaneAttempt(connection, batch, slotStates);
""" + loop[right:]
    left = loop.index("            // Admission changed")
    right = loop.index("        if (!batch.isEmpty()) {", left)
    loop = loop[:left] + "        }\n" + loop[right:]
    loop = once(loop, "final boolean onLane = f4Direct != null || config.takesConnectionOnLane",
                "final boolean onLane = config.takesConnectionOnLane")
    source = source[:start] + loop + source[end:]
    source = once(source, """        if (f4Direct != null || config.getFidesLaneBatch() > RWRouteConfig.FIDES_LANE_BATCH_OFF) {
""", """        if (f4Diversion != null) {
            f4RouteDiverted(connections);
        } else if (config.getFidesLaneBatch() > RWRouteConfig.FIDES_LANE_BATCH_OFF) {
""")
    source = once(source, "List<ConnectionState> slotStates, boolean tryLocal)",
                "List<ConnectionState> slotStates)")
    source = once(source, """        state.f4CpuPopsBeforeHandoff = 0;
        if (tryLocal && f4ProbeAndCommit(state)) return;
""", "")
    source = once(source, "        f4ObserveGpuBatch(batch, System.nanoTime() - started);",
                "        f4Diversion.gpu(batch.size(), System.nanoTime() - started);")
    source = once(source, "        if (F4DirectGpu.enabled()) F4DirectGpu.releaseIndirectDevice();",
                "        if (F4DirectGpu.enabled()) f4ReleaseFlood();")
    start = source.index("    static final int F4_CPU_PROBE_LIMIT")
    close = source.index("    private void f4WriteCpuFinal()", start)
    source = source[:start] + (work / "F4DiversionRouter.inc").read_text() + source[close:]
    source = once(source, "        if (f4CpuProbeLimit == 0) return;\n", "")
    start = source.index("    private int f4BestOveruse")
    end = source.index("    public void routeIndirectConnectionsIteratively()", start)
    source = source[:start] + source[end:]
    start = source.index('            f4CpuProbeLimit = Integer.getInteger("fides.ppt.f4.cpuProbeBudget"')
    end = source.index("        }\n", start)
    source = source[:start] + """            if (f4Width < 2 || f4Width > 8) throw new IllegalArgumentException("F4 GPU channels must be 2 to 8");
            var quote = java.nio.file.Path.of(System.getProperty("fides.ppt.f4.work"), "gpu_quote.json");
            try {
                f4Diversion = new F4RoundDiversion(f4Width,
                        Long.getLong("fides.ppt.f4.inputBytes"),
                        new org.json.JSONObject(java.nio.file.Files.readString(quote)));
            } catch (java.io.IOException error) {
                throw new java.io.UncheckedIOException("Cannot read the GPU round quote", error);
            }
""" + source[end:]
    source = once(source, "if (f4Direct != null) f4RoundBoundary(overUsedRnodes.size(), unroutableConnections.size());",
                "if (f4Diversion != null) f4RoundBoundary(unroutableConnections.size());")
    source = once(source, "        int f4ProbeRemainingLimit;", "        int f4HandoffBudget;")
    return once(source, """        if (lookahead && (state.f4ProbeRemainingLimit == 0
                || state.nodesPopped < state.f4ProbeRemainingLimit)) {""", "        if (lookahead) {")


def gate(source):
    source = once(source, "    boolean f4Supported(Connection c, RWRoute.ConnectionState state) {",
                  """    boolean f4CanOffer(Connection c) {
        return f4Live != null && c.getSourceRnode() != c.getSinkRnode() && f4Live.want(c, router.routeIteration);
    }

    boolean f4Supported(Connection c, RWRoute.ConnectionState state) {""")
    source = once(source, """            if (scores[slot] < 0 || state.nodesPopped != 0 || state.f4CpuPopsBeforeHandoff < 0
                    || state.f4CpuPopsBeforeHandoff > RWRoute.F4_CPU_PROBE_LIMIT) {""",
                """            if (scores[slot] < 0 || state.nodesPopped != 0
                    || !F4RoundDiversion.isBudget(state.f4HandoffBudget)
                    || state.f4CpuPopsBeforeHandoff <= state.f4HandoffBudget) {""")
    source = once(source, "F4HostJournal f4Journal() { return f4Live == null ? null : f4Live.journal(); }",
        """F4HostJournal f4Journal() {
        return f4Live == null || router.f4FloodReleased() ? null : f4Live.journal();
    }""")
    return once(source, "            f4Live.markHistory(overUsed);",
                "            if (!router.f4FloodReleased()) f4Live.markHistory(overUsed);")


def capture(source):
    return once(source, '                        .put("cpu_pops_before_handoff", states.get(index).f4CpuPopsBeforeHandoff);',
        '''                        .put("cpu_pops_before_handoff", states.get(index).f4CpuPopsBeforeHandoff)
                        .put("cpu_budget", states.get(index).f4HandoffBudget);''')


def bridge(source):
    source = once(source, """    def attach(self, app):
        self._readable(self.listener, (app,), None)""", """    def attach(self, app, release=None):
        if not self._readable(self.listener, (app,), release):
            return False""")
    return once(source, "        self.listener.close()\n", "        self.listener.close()\n        return True\n")
