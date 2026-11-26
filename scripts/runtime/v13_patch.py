"""Patch the frozen v12 Java overlay with the F5 spatial schedule and hot state."""
from v11_window import replace_once as once


def patch_node(source, work):
    source = once(source, "    int f4JournalId = -1;",
                  "    int f4JournalId = -1;\n    long f4DirtyEpoch;\n\n"
                  "    static boolean f4PrivateRipActive() {\n"
                  "        SearchScratch scratch = f4ActiveScratch();\n"
                  "        return scratch != null && scratch.collectingRipUp;\n    }")
    source = once(source, """        ScratchValues(RouteNode node) {
            target = node.isTarget;""",
                  """        ScratchValues(RouteNode node) { resetFrom(node); }

        void resetFrom(RouteNode node) {
            target = node.isTarget;""")
    source = once(source, "    static final class SearchScratch {",
                  (work / "F4ScratchPages.inc").read_text() + "    static final class SearchScratch {")
    source = once(source, "        private final IdentityHashMap<RouteNode, ScratchValues> values = new IdentityHashMap<>();",
                  "        private final F4ScratchPages values = new F4ScratchPages();")
    source = once(source, """            for (Map.Entry<RouteNode, ScratchValues> entry : values.entrySet()) {
                entry.getValue().publish(entry.getKey());
            }""",
                  """            for (RouteNode node : values.touchedNodes()) values.get(node).publish(node);""")
    return once(source, "        void detachPair() {",
                """        void f4DiscardDetached() {
            values.clear();
            readNodes.clear();
            ripRemovals.clear();
            ripNet = null;
        }

        void detachPair() {""")


def patch_gate(source):
    source = once(source, "        mirror.connections++;\n        if (f4Live != null) {\n            f4Live.markPath(c);\n            return;\n        }",
                  "        mirror.connections++;\n        if (f4Live != null) return;")
    block = "        if (f4Live != null) {\n            f4Live.markPath(c);\n            return;\n        }"
    if source.count(block) != 2:
        raise RuntimeError("F4 delta hook scope changed")
    source = source.replace(block, "        if (f4Live != null) return;")
    source = once(source, "    void mirrorBeforeAttempt(Connection c) {",
                  """    F4HostJournal f4Journal() { return f4Live == null ? null : f4Live.journal(); }

    void mirrorBeforeAttempt(Connection c) {""")
    history = """        if (f4Live != null) {
            f4Live.markHistory(overUsed);
            return;
        }
"""
    source = once(source, history, "")
    return once(source,
        """                                 float historicalCongestionFactor, long routedThisIteration) {
        try""",
        """                                 float historicalCongestionFactor, long routedThisIteration) {
""" + history + """        try""")


def patch_capture(source):
    start = source.index("    void markPath(Connection connection)")
    end = source.index("    void markHistory(", start)
    return source[:start] + "    F4HostJournal journal() { return journal; }\n\n" + source[end:]


def patch_router(source, work):
    source = once(source, "    private NetWrapper f4LastRejectedSharedNet;\n", "")
    source = once(source, "        f4LastRejectedSharedNet = null;\n", "")
    start = source.index("    private boolean f4CanSpeculatePair(")
    end = source.index("    private F4HostSearch f4SearchHostReadOnly", start)
    source = source[:start] + (work / "F4SpatialRouter.inc").read_text() + source[end:]
    start = source.index("    private F4HostSearch[] f4SearchHostPair(")
    end = source.index("    private void f4CommitHostSearch(", start)
    source = source[:start] + source[end:]
    source = once(source, "        RouteNode.SearchScratch scratch = new RouteNode.SearchScratch();",
                  """        RouteNode.SearchScratch scratch = f4HostScratchFree.poll();
        if (scratch == null) scratch = new RouteNode.SearchScratch();""")
    source = once(source, """            if (!detached) scratch.close();""",
                  """            if (!detached) {
                scratch.close();
                f4HostScratchFree.add(scratch);
            }""")
    source = once(source, "            result.scratch.close();",
                  "            result.scratch.close();\n            f4HostScratchFree.add(result.scratch);")
    source = once(source, '.put("host_cold_or_miss", f4HostColdOrMisses);',
                  """.put("host_cold_or_miss", f4HostColdOrMisses)
                .put("spatial_members", f4SpatialMembers)
                .put("spatial_unsplittable", f4SpatialUnsplittable)
                .put("spatial_accepted", f4SpatialAccepted);""")
    start = source.index("        Iterator<Connection> iterator = connections.iterator();",
                         source.index("    protected void routeIndirectConnectionsInBatches("))
    end = source.index("            /* The flush", start)
    source = source[:start] + """        List<Connection> ordered = connections instanceof List
                ? (List<Connection>) connections : new ArrayList<>(connections);
        F4HostSearch[] pending = null;
        int pendingStart = 0;
        int nextSpatialBlock = 0;
        for (int position = 0; position < ordered.size(); ++position) {
            Connection connection = ordered.get(position);
            F4HostSearch hostResult = null;
            if (pending != null) {
                hostResult = pending[position - pendingStart];
                pending[position - pendingStart] = null;
                if (position + 1 == pendingStart + pending.length) pending = null;
            }
""" + source[end:]
    start = source.index("                    int cpuWindow = f4ChooseCpuWindow(connection, batch.isEmpty());")
    end = source.index("                } else {", start)
    source = source[:start] + """                    int cpuWindow = f4ChooseCpuWindow(connection, batch.isEmpty());
                    if (hostResult == null && pending != null) {
                        for (F4HostSearch stale : pending) f4DiscardHostSearch(stale);
                        pending = null;
                    }
                    if (cpuWindow >= 0 && hostResult == null && position >= nextSpatialBlock) {
                        int width = f4SpatialWindow(ordered, position);
                        if (width > 1) {
                            F4SpatialSchedule spatial = new F4SpatialSchedule(ordered, position, width);
                            if (spatial.unsplittable()) {
                                ++f4SpatialUnsplittable;
                                nextSpatialBlock = position + width;
                            } else {
                                int independent = spatial.prefix();
                                if (independent > 1) {
                                    pending = f4SpatialSearch(ordered, position, independent);
                                    pendingStart = position;
                                    hostResult = pending[0];
                                    pending[0] = null;
                                }
                            }
                        }
                    }
                    if (cpuWindow >= 0 && hostResult != null) {
                        f4CommitHostSearch(hostResult);
                        continue;
                    }
                    f4DiscardHostSearch(hostResult);
                    hostResult = null;
                    collectLaneAttempt(connection, batch, slotStates, cpuWindow >= 0);
""" + source[end:]
    end = source.index("        if (!batch.isEmpty()) {", source.index("                    int cpuWindow = f4ChooseCpuWindow"))
    before = source[:end]
    before = before[:before.rindex("        }\n")] + """            // Admission changed or the first read-only result was unavailable.
            // None of the later private results can cross that serial action.
            f4DiscardHostSearch(hostResult);
            if (pending != null) {
                for (F4HostSearch stale : pending) f4DiscardHostSearch(stale);
                pending = null;
            }
        }
"""
    source = before + source[end:]
    for operation in ("decrementUser", "incrementUser"):
        anchor = f"""        NetWrapper netWrapper = connection.getNetWrapper();
        for (RouteNode rnode : rnodes) {{
            rnode.{operation}(netWrapper);
        }}"""
        source = once(source, anchor, f"""        NetWrapper netWrapper = connection.getNetWrapper();
        F4HostJournal journal = f4Direct == null || fidesLaneGate == null || RouteNode.f4PrivateRipActive()
                ? null : fidesLaneGate.f4Journal();
        F4HostJournal.Scope scope = journal == null ? null : journal.hotScope(netWrapper);
        for (RouteNode rnode : rnodes) {{
            rnode.{operation}(netWrapper);
            if (journal != null) journal.noteUsage(rnode, netWrapper, scope);
        }}""")
    return source
