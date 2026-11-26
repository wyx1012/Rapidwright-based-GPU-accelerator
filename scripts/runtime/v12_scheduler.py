"""Reuse F5 private search with exact dependency checks and ordered F4 publication."""
from v11_window import replace_once as once


def patch_node(source, folder):
    from direct_patch import patch_direct_node
    source = patch_direct_node(source, (folder / "route_node_scope.inc").read_text(),
                               (folder / "private_usage.inc").read_text())
    source = once(source, "    protected RouteNode[] children;\n",
                  """    protected RouteNode[] children;
    int f4JournalId = -1;
    final boolean f4CachedChildren() { return children != null; }
""")
    return once(source, "        void attachPairForCommit() {",
                """        void f4DropPrivateRip() {
            ripNet = null;
            ripRemovals.clear();
        }

        void attachPairForCommit() {""")


def patch_graph(source):
    return once(source, "    public RouteNode getOrCreate(Node node, RouteNodeType type) {",
                """    public RouteNode getOrCreate(Node node, RouteNodeType type) {
        RouteNode.requireNoReadOnlyReplay("create graph node");""")


def patch_router(source, folder):
    start = source.index("    private boolean f4ProbeAndCommit(ConnectionState state) {")
    nxt = source.index("    org.json.JSONObject f4CpuReceipt()", start)
    probe = source[start:nxt]
    middle = probe.index("        long probeNs = System.nanoTime() - started;")
    find = probe[:middle]
    find = once(find, """    private boolean f4ProbeAndCommit(ConnectionState state) {
        final long started = System.nanoTime();""",
                  """    private RouteNode f4FindLocalTarget(ConnectionState state) {""")
    body = probe[middle:]
    body = once(body, "        long probeNs = System.nanoTime() - started;\n", "")
    body = once(body, "        int pushed = state.nodesPopped + queue.size();",
                """        final PriorityQueue<RouteNode> queue = state.queue;
        int pushed = state.nodesPopped + queue.size();""")
    # Speculative CPU work runs in parallel. Do not compare summed CPU service
    # time against serial GPU wall; the paired elapsed counter is independent.
    body = once(body, "f4ObserveCpuWork(state, popped, System.nanoTime() - started, target != null);",
                "f4ObserveCpuWork(state, popped, System.nanoTime() - started, target != null);")
    source = source[:start] + (folder / "F4HostSchedule.inc").read_text() + find + """        return target;
    }

    private boolean f4ProbeAndCommit(ConnectionState state) {
        long started = System.nanoTime();
        RouteNode target = f4FindLocalTarget(state);
        return f4CompleteLocalAttempt(state, target, System.nanoTime() - started, started);
    }

    private boolean f4CompleteLocalAttempt(ConnectionState state, RouteNode target, long probeNs, long started) {
""" + body + source[nxt:]
    source = once(source, '.put("batch_benefit", f4BenefitReceipt());',
                  """.put("batch_benefit", f4BenefitReceipt())
                .put("host_pairs", f4HostPairs).put("host_reused", f4HostReused)
                .put("host_pair_ns", f4HostPairNs).put("host_conflicts", f4HostConflicts)
                .put("host_cold_or_miss", f4HostColdOrMisses);""")
    loop = source.index("    protected void routeIndirectConnectionsInBatches(")
    end = source.index("    private void collectLaneAttempt(", loop)
    method = source[loop:end]
    method = once(method, "        for (Connection connection : connections) {",
                  """        Iterator<Connection> iterator = connections.iterator();
        Connection next = iterator.hasNext() ? iterator.next() : null;
        F4HostSearch pendingHostResult = null;
        while (next != null) {
            Connection connection = next;
            next = iterator.hasNext() ? iterator.next() : null;
            F4HostSearch hostResult = pendingHostResult;
            pendingHostResult = null;""")
    method = once(method, """                    int cpuWindow = f4ChooseCpuWindow(connection, batch.isEmpty());
                    collectLaneAttempt(connection, batch, slotStates, cpuWindow >= 0);""",
                  """                    int cpuWindow = f4ChooseCpuWindow(connection, batch.isEmpty());
                    if (cpuWindow >= 0 && hostResult == null && f4CanSpeculatePair(connection, next)) {
                        F4HostSearch[] pair = f4SearchHostPair(connection, next);
                        hostResult = pair[0];
                        pendingHostResult = pair[0] == null ? null : pair[1];
                    }
                    if (cpuWindow >= 0 && hostResult != null) f4CommitHostSearch(hostResult);
                    else collectLaneAttempt(connection, batch, slotStates, cpuWindow >= 0);""")
    # The F4 host pair must run before the old route is removed. Gate-before
    # in this protocol only journals old references and is read-only.
    source = source[:loop] + method + source[end:]
    init = source.index("        state.connection = connection;", source.index("    private void collectLaneAttempt("))
    init_end = source.index("\n\n        PriorityQueue<RouteNode> queue", init)
    fields = source[init:init_end]
    fields = once(fields, """        state.sequence = connectionsRouted.incrementAndGet();
        connectionsRoutedThisIteration.incrementAndGet();""", "        state.sequence = sequence;")
    source = source[:init] + """        f4FillHostState(state, connection, connectionsRouted.incrementAndGet());
        connectionsRoutedThisIteration.incrementAndGet();""" + source[init_end:]
    source = once(source, "    private void collectLaneAttempt(Connection connection,",
                  """    private void f4FillHostState(ConnectionState state, Connection connection, int sequence) {
""" + fields + """
    }

    private void collectLaneAttempt(Connection connection,""")
    source = once(source, "        int f4ProbeRemainingLimit;", "        int f4ProbeRemainingLimit;\n        boolean f4PrivateSearch;")
    return once(source, "                    nodesPushed.addAndGet(queue.size());",
                "                    if (!state.f4PrivateSearch) nodesPushed.addAndGet(queue.size());")
