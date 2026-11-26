from pathlib import Path
import difflib

def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('Expected one source anchor: ' + old.splitlines()[0])
    return source.replace(old, new, 1)

def patch_node(source, scope):
    source = replace_once(source, '    private RouteNode prev;\n', '    private RouteNode prev;\n' + scope + '\n')
    source = replace_once(source,
        '        return (int) Math.signum(this.lowerBoundTotalPathCost - that.lowerBoundTotalPathCost);',
        '        return (int) Math.signum(getLowerBoundTotalPathCost() - that.getLowerBoundTotalPathCost());')
    source = replace_once(source, '        return isTarget;', '        ScratchValues values = currentScratchValues();\n        return values == null ? isTarget : values.target;')
    source = replace_once(source, '        isTarget = true;\n        state.targets.add(this);',
        '        setScratchTarget(true);\n        state.targets.add(this);')
    source = replace_once(source, '        assert(isTarget);\n        isTarget = false;', '        assert(isTarget());\n        setScratchTarget(false);')
    if source.count('&& visited == 0)') != 2:
        raise ValueError('Expected both assertions to read scratch-aware visited')
    source = source.replace('&& visited == 0)', '&& getVisited() == 0)')
    source = replace_once(source,
        '        boolean reverseSLL = (getType() == RouteNodeType.SUPER_LONG_LINE &&\n'
        '                prev != null &&\n'
        '                prev.endTileYCoordinate == endTileYCoordinate);',
        '        RouteNode searchPrev = getType() == RouteNodeType.SUPER_LONG_LINE ? getPrev() : null;\n'
        '        boolean reverseSLL = searchPrev != null && searchPrev.endTileYCoordinate == endTileYCoordinate;')
    pairs = (
        ('        lowerBoundTotalPathCost = totalPathCost;', 'lowerBoundTotalPathCost', 'totalPathCost'),
        ('        this.upstreamPathCost = newPartialPathCost;', 'upstreamPathCost', 'newPartialPathCost'),
        ('        this.prev = prev;', 'prev', 'prev'),
        ('        this.prev = null;', 'prev', 'null'),
        ('        visited = seq;', 'visited', 'seq'),
    )
    for anchor, field, value in pairs:
        source = replace_once(source, anchor,
            '        ScratchValues values = scratchValuesForWrite();\n'
            f'        if (values == null) this.{field} = {value};\n'
            f'        else values.{field} = {value};')
    for field in ('lowerBoundTotalPathCost', 'upstreamPathCost', 'prev', 'visited'):
        source = replace_once(source, f'        return {field};',
            f'        ScratchValues values = currentScratchValues();\n        return values == null ? {field} : values.{field};')
    return replace_once(source, '        return visited == seq;', '        return getVisited() == seq;')

def patch_router(source):
    source = replace_once(source, '    private ConnectionState connectionState;',
        '    private ConnectionState connectionState;\n'
        '    private final boolean f5UseNodeScratch = Boolean.getBoolean("fides.f5.nodeScratch");\n'
        '    private final ThreadLocal<ConnectionState> f5PrivateState = ThreadLocal.withInitial(ConnectionState::new);\n'
        '    private final RouteNode.SearchScratch f5Scratch = new RouteNode.SearchScratch();\n'
        '    private int f5ScratchAttempts;\n'
        '    private long f5ScratchWrittenNodes;\n'
        '    private int f5ScratchPeakNodes;')
    source = replace_once(source, '    protected ConnectionState getConnectionState() {\n        return connectionState;\n    }',
        '    protected ConnectionState getConnectionState() {\n'
        '        return f5UseNodeScratch ? f5PrivateState.get() : connectionState;\n    }')
    source = replace_once(source, '    protected void routeIndirectConnection(Connection connection) {\n        ConnectionState state = getConnectionState();',
        '    protected void routeIndirectConnection(Connection connection) {\n'
        '        if (!f5UseNodeScratch) {\n'
        '            routeIndirectConnectionSerial(connection);\n'
        '            return;\n'
        '        }\n'
        '        if (getClass() != PartialRouter.class || config.isTimingDriven() || fidesLaneGate != null) {\n'
        '            throw new IllegalStateException("Node scratch fixture requires the CPU partial router");\n'
        '        }\n'
        '        if (f5ScratchAttempts == 0) F5NodeScratchFixture.verify(connection);\n'
        '        f5Scratch.open();\n'
        '        try {\n'
        '            routeIndirectConnectionSerial(connection);\n'
        '            int writtenNodes = f5Scratch.publish();\n'
        '            f5ScratchAttempts++;\n'
        '            f5ScratchWrittenNodes += writtenNodes;\n'
        '            f5ScratchPeakNodes = Math.max(f5ScratchPeakNodes, writtenNodes);\n'
        '        } finally {\n'
        '            f5Scratch.close();\n'
        '        }\n'
        '    }\n\n'
        '    private void routeIndirectConnectionSerial(Connection connection) {\n'
        '        ConnectionState state = getConnectionState();')
    source = replace_once(source, '        routeIndirectConnectionsIteratively();\n        // NOTE:',
        '        routeIndirectConnectionsIteratively();\n'
        '        if (f5UseNodeScratch) {\n'
        '            if (f5ScratchAttempts != connectionsRouted.get() || fidesLaneGate != null) {\n'
        '                throw new IllegalStateException("Incomplete CPU scratch coverage");\n'
        '            }\n'
        '            System.out.printf("F5_NODE_SCRATCH attempts=%d writtenNodes=%d peakNodes=%d%n",\n'
        '                    f5ScratchAttempts, f5ScratchWrittenNodes, f5ScratchPeakNodes);\n'
        '        }\n'
        '        // NOTE:')
    return source

def prepare(source_dir, work):
    sources = ('RouteNode.java', 'RWRoute.java')
    diffs = []
    for name in sources:
        original = (source_dir / name).read_text()
        if name == 'RouteNode.java':
            modified = patch_node(original, (work / 'route_node_scope.inc').read_text())
        else:
            modified = patch_router(original)
        (work / name).write_text(modified)
        diffs.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                     'formal/' + name, 'isolated/' + name))
    (work / 'node-scratch.diff').write_text(''.join(diffs))

def patch_replay_node(source):
    source = replace_once(source, '        private boolean published;',
        '        private boolean published;\n        private boolean readOnlyReplay;')
    source = replace_once(source, '            published = false;\n            ACTIVE_SCRATCH.set(this);',
        '            published = false;\n            readOnlyReplay = false;\n            ACTIVE_SCRATCH.set(this);')
    source = replace_once(source, '        int publish() {',
        '        void openReadOnlyReplay() {\n'
        '            open();\n'
        '            readOnlyReplay = true;\n'
        '        }\n\n'
        '        int writtenNodes() {\n'
        '            return values.size();\n'
        '        }\n\n'
        '        boolean hasSameValues(SearchScratch reference) {\n'
        '            if (values.size() != reference.values.size()) return false;\n'
        '            for (Map.Entry<RouteNode, ScratchValues> entry : values.entrySet()) {\n'
        '                ScratchValues actual = entry.getValue();\n'
        '                ScratchValues expected = reference.values.get(entry.getKey());\n'
        '                if (expected == null || actual.target != expected.target\n'
        '                        || actual.visited != expected.visited || actual.prev != expected.prev\n'
        '                        || Float.floatToRawIntBits(actual.upstreamPathCost)\n'
        '                                != Float.floatToRawIntBits(expected.upstreamPathCost)\n'
        '                        || Float.floatToRawIntBits(actual.lowerBoundTotalPathCost)\n'
        '                                != Float.floatToRawIntBits(expected.lowerBoundTotalPathCost)) return false;\n'
        '            }\n'
        '            return true;\n'
        '        }\n\n'
        '        int publish() {\n'
        '            requireNoReadOnlyReplay("publish scratch");')
    source = replace_once(source, '    private ScratchValues currentScratchValues() {',
        '    static void requireNoReadOnlyReplay(String operation) {\n'
        '        SearchScratch scratch = ACTIVE_SCRATCH.get();\n'
        '        if (scratch != null && scratch.readOnlyReplay) {\n'
        '            throw new ReplayWriteException(operation);\n'
        '        }\n'
        '    }\n\n'
        '    static final class ReplayWriteException extends IllegalStateException {\n'
        '        ReplayWriteException(String operation) { super(operation); }\n'
        '    }\n\n'
        '    private ScratchValues currentScratchValues() {')
    source = replace_once(source,
        '    public RouteNode[] getChildren(RouteNodeGraph routingGraph) {\n        if (children == null) {',
        '    public RouteNode[] getChildren(RouteNodeGraph routingGraph) {\n        if (children == null) {\n'
        '            requireNoReadOnlyReplay("uncached adjacency");')
    for signature, operation in (
            ('public void resetChildren()', 'reset adjacency'),
            ('public void setType(RouteNodeType type)', 'change route node type'),
            ('public void incrementUser(NetWrapper user)', 'add congestion user'),
            ('public void decrementUser(NetWrapper user)', 'remove congestion user'),
            ('public void setHistoricalCongestionCost(float historicalCongestionCost)', 'update historical congestion')):
        anchor = '    ' + signature + ' {'
        source = replace_once(source, anchor, anchor + '\n        requireNoReadOnlyReplay("' + operation + '");')
    return source

def patch_replay_router(source, methods):
    source = replace_once(source, '    private int f5ScratchAttempts;',
        '    private final boolean f5ReplaySearch = Boolean.getBoolean("fides.f5.replaySearch");\n'
        '    private int f5LastReplayRound;\n'
        '    private int f5ScratchAttempts;')
    source = replace_once(source, '        protected boolean earlyTermination;',
        '        protected boolean earlyTermination;\n        private boolean f5ReadOnlyReplay;')
    start = source.index('    private void routeIndirectConnectionSerial(Connection connection) {')
    end = source.index('\n    }', start) + len('\n    }')
    body = source[start:end]
    original_search = ('        RouteNode rnode;\n'
        '        while ((rnode = queue.poll()) != null) {\n'
        '            state.nodesPopped++;\n'
        '            if (rnode.isTarget()) {\n'
        '                break;\n'
        '            }\n'
        '            exploreAndExpand(state, rnode);\n'
        '        }')
    body = replace_once(body, original_search,
        '        RouteNode rnode = f5SearchUntilTarget(state);\n'
        '        if (f5ReplaySearch && routeIteration != f5LastReplayRound) {\n'
        '            f5ReplayWorkers(state, rnode);\n'
        '            f5LastReplayRound = routeIteration;\n'
        '        }')
    source = source[:start] + body + '\n' + methods + source[end:]
    source = replace_once(source,
        '                    state.nodesPushed += queue.size();\n                    nodesPushed.addAndGet(queue.size());',
        '                    state.nodesPushed += queue.size();\n'
        '                    if (!state.f5ReadOnlyReplay) nodesPushed.addAndGet(queue.size());')
    source = replace_once(source,
        '        // Rips up the connection\n        ripUp(connection);\n        assert(state.queue.isEmpty());',
        '        // Rips up the connection\n        ripUp(connection);\n        f5SeedRouteConnection(state);\n    }\n\n'
        '    private void f5SeedRouteConnection(ConnectionState state) {\n'
        '        final Connection connection = state.connection;\n        assert(state.queue.isEmpty());')
    return source

def prepare_replay(source_dir, work):
    scope = (work / 'route_node_scope.inc').read_text()
    methods = (work / 'replay_methods.inc').read_text()
    diffs = []
    for name in ('RouteNode.java', 'RWRoute.java', 'RouteNodeGraph.java'):
        original = (source_dir / name).read_text()
        if name == 'RouteNode.java':
            modified = patch_replay_node(patch_node(original, scope))
        elif name == 'RWRoute.java':
            modified = patch_replay_router(patch_router(original), methods)
        else:
            anchor = '    public RouteNode getOrCreate(Node node, RouteNodeType type) {'
            modified = replace_once(original, anchor,
                anchor + '\n        RouteNode.requireNoReadOnlyReplay("create graph node");')
        (work / name).write_text(modified)
        diffs.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                     'formal/' + name, 'isolated/' + name))
    (work / 'node-scratch.diff').write_text(''.join(diffs))