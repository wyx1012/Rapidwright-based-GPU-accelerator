from pathlib import Path
import difflib

from scratch_patch import patch_node, patch_replay_node, patch_router, patch_replay_router, replace_once


def patch_pair_node(source):
    source = replace_once(source, '        private boolean readOnlyReplay;',
        '        private boolean readOnlyReplay;\n'
        '        private boolean warmProbe;\n'
        '        private boolean capturePairFootprint;\n'
        '        private final java.util.Set<RouteNode> readNodes =\n'
        '                java.util.Collections.newSetFromMap(new IdentityHashMap<>());')
    source = replace_once(source, '            readOnlyReplay = false;\n            ACTIVE_SCRATCH.set(this);',
        '            readOnlyReplay = false;\n'
        '            warmProbe = false;\n'
        '            capturePairFootprint = false;\n'
        '            ACTIVE_SCRATCH.set(this);')
    source = replace_once(source, '        int writtenNodes() {',
        '        void openWarmProbe() {\n'
        '            openReadOnlyReplay();\n'
        '            warmProbe = true;\n'
        '        }\n\n'
        '        void openFrozenPair() {\n'
        '            openReadOnlyReplay();\n'
        '            capturePairFootprint = true;\n'
        '        }\n\n'
        '        void detachPair() {\n'
        '            if (ACTIVE_SCRATCH.get() != this || !readOnlyReplay || published) {\n'
        '                throw new IllegalStateException("Cannot detach pair scratch");\n'
        '            }\n'
        '            ACTIVE_SCRATCH.remove();\n'
        '        }\n\n'
        '        void attachPairForCommit() {\n'
        '            if (ACTIVE_SCRATCH.get() != null || !readOnlyReplay || warmProbe\n'
        '                    || !capturePairFootprint || published || values.isEmpty()) {\n'
        '                throw new IllegalStateException("Cannot attach pair scratch for commit");\n'
        '            }\n'
        '            readOnlyReplay = false;\n'
        '            capturePairFootprint = false;\n'
        '            ACTIVE_SCRATCH.set(this);\n'
        '        }\n\n'
        '        java.util.Set<RouteNode> readNodes() { return readNodes; }\n'
        '        java.util.Set<RouteNode> writeNodes() { return values.keySet(); }\n\n'
        '        int writtenNodes() {')
    source = replace_once(source, '            values.clear();',
        '            values.clear();\n            readNodes.clear();')
    source = replace_once(source, '    private ScratchValues currentScratchValues() {',
        '    static void requireNoFrozenPairGraphWrite(String operation) {\n'
        '        SearchScratch scratch = ACTIVE_SCRATCH.get();\n'
        '        if (scratch != null && scratch.readOnlyReplay && !scratch.warmProbe) {\n'
        '            throw new ReplayWriteException(operation);\n'
        '        }\n'
        '    }\n\n'
        '    private void recordPairRead() {\n'
        '        SearchScratch scratch = ACTIVE_SCRATCH.get();\n'
        '        if (scratch != null && scratch.capturePairFootprint) scratch.readNodes.add(this);\n'
        '    }\n\n'
        '    private ScratchValues currentScratchValues() {\n'
        '        recordPairRead();')
    source = replace_once(source, '    private ScratchValues scratchValuesForWrite() {',
        '    private ScratchValues scratchValuesForWrite() {\n        recordPairRead();')
    source = replace_once(source, '            requireNoReadOnlyReplay("uncached adjacency");',
        '            requireNoFrozenPairGraphWrite("uncached adjacency");')
    for declaration in ('public int uniqueUserCount()', 'public int countConnectionsOfUser(NetWrapper user)',
                        'public float getHistoricalCongestionCost()', 'public RouteNodeType getType()',
                        'public RouteNode[] getChildren(RouteNodeGraph routingGraph)'):
        anchor = '    ' + declaration + ' {'
        source = replace_once(source, anchor, anchor + '\n        recordPairRead();')
    return source


def patch_pair_router(source, methods):
    source = replace_once(source, '    private int f5LastReplayRound;',
        '    private int f5LastReplayRound;\n'
        '    private final Map<Integer, F5PairSpec> f5DistinctPairs = f5LoadPairs();')
    source = replace_once(source, '    protected void routeIndirectConnections(Collection<Connection> connections) {',
        '    protected void routeIndirectConnections(Collection<Connection> connections) {\n'
        '        if (!f5DistinctPairs.isEmpty() && routeIteration == 1) {\n'
        '            f5RouteFirstRound(connections);\n'
        '            return;\n'
        '        }')
    start = source.index('    private void routeIndirectConnectionSerial(Connection connection) {')
    body_start = source.index('        ConnectionState state = getConnectionState();', start)
    init_start = source.index('        state.connection = connection;', body_start)
    init_end = source.index('\n\n        PriorityQueue<RouteNode> queue', init_start)
    init = source[init_start:init_end]
    init = replace_once(init,
        '        state.sequence = connectionsRouted.incrementAndGet();\n'
        '        connectionsRoutedThisIteration.incrementAndGet();',
        '        state.sequence = sequence;')
    finish_start = source.index('        final int pushedThisConnection = state.nodesPopped + queue.size();', init_end)
    finish_end = source.index('\n    }', finish_start)
    finish = source[finish_start:finish_end]
    body = source[init_end:finish_start]
    body = replace_once(body,
        '\n\n        PriorityQueue<RouteNode> queue = state.queue;\n        assert(queue.isEmpty());',
        '\n        assert(state.queue.isEmpty());')
    replacement = (
        '        ConnectionState state = getConnectionState();\n'
        '        int sequence = connectionsRouted.incrementAndGet();\n'
        '        connectionsRoutedThisIteration.incrementAndGet();\n'
        '        f5InitCpuSearch(state, connection, sequence);'
        + body +
        '        f5FinalizeCpuSearch(state, rnode);\n'
        '    }\n\n'
        '    private void f5InitCpuSearch(ConnectionState state, Connection connection, int sequence) {\n'
        + init + '\n    }\n\n'
        '    private void f5FinalizeCpuSearch(ConnectionState state, RouteNode rnode) {\n'
        '        Connection connection = state.connection;\n'
        '        PriorityQueue<RouteNode> queue = state.queue;\n'
        + finish + '\n    }\n'
        + methods)
    return source[:body_start] + replacement + source[finish_end + len('\n    }'):]


def prepare_pairs(source_dir, work):
    scope = (work / 'route_node_scope.inc').read_text()
    replay = (work / 'replay_methods.inc').read_text()
    pair_methods = (work / 'pair_methods.inc').read_text()
    diffs = []
    for name in ('RouteNode.java', 'RWRoute.java', 'RouteNodeGraph.java'):
        original = (source_dir / name).read_text()
        if name == 'RouteNode.java':
            modified = patch_pair_node(patch_replay_node(patch_node(original, scope)))
        elif name == 'RWRoute.java':
            modified = patch_pair_router(patch_replay_router(patch_router(original), replay), pair_methods)
        else:
            anchor = '    public RouteNode getOrCreate(Node node, RouteNodeType type) {'
            modified = replace_once(original, anchor,
                anchor + '\n        RouteNode.requireNoFrozenPairGraphWrite("create graph node");')
        (work / name).write_text(modified)
        diffs.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                     'formal/' + name, 'isolated/' + name))
    (work / 'distinct-pairs.diff').write_text(''.join(diffs))