import difflib

from pair_patch import patch_pair_node, patch_pair_router
from scratch_patch import patch_node, patch_replay_node, patch_replay_router, patch_router, replace_once


def patch_occupancy(source, helpers):
    source = replace_once(source, '        private boolean capturePairFootprint;',
        '        private boolean capturePairFootprint;\n'
        '        private NetWrapper ripNet;\n'
        '        private boolean collectingRipUp;\n'
        '        private final IdentityHashMap<RouteNode, Integer> ripRemovals = new IdentityHashMap<>();')
    source = replace_once(source, '        void detachPair() {',
        '        void beginPrivateRipUp(NetWrapper net) {\n'
        '            if (ACTIVE_SCRATCH.get() != this || !readOnlyReplay || warmProbe\n'
        '                    || net == null || ripNet != null || collectingRipUp) {\n'
        '                throw new IllegalStateException("Cannot start private rip-up");\n'
        '            }\n'
        '            ripNet = net;\n'
        '            collectingRipUp = true;\n'
        '        }\n\n'
        '        void endPrivateRipUp() {\n'
        '            if (!collectingRipUp) throw new IllegalStateException("Private rip-up not active");\n'
        '            collectingRipUp = false;\n'
        '        }\n\n'
        '        void detachPair() {\n'
        '            if (collectingRipUp) throw new IllegalStateException("Cannot detach during rip-up");')
    source = replace_once(source, '            readOnlyReplay = false;\n            capturePairFootprint = false;\n            ACTIVE_SCRATCH.set(this);',
        '            readOnlyReplay = false;\n'
        '            capturePairFootprint = false;\n'
        '            ripNet = null;\n'
        '            ripRemovals.clear();\n'
        '            ACTIVE_SCRATCH.set(this);')
    source = replace_once(source, '            readNodes.clear();',
        '            readNodes.clear();\n'
        '            ripNet = null;\n'
        '            collectingRipUp = false;\n'
        '            ripRemovals.clear();')
    source = replace_once(source, '    private ScratchValues currentScratchValues() {',
        helpers + '\n    private ScratchValues currentScratchValues() {')
    source = replace_once(source,
        '    public void decrementUser(NetWrapper user) {\n        requireNoReadOnlyReplay("remove congestion user");',
        '    public void decrementUser(NetWrapper user) {\n'
        '        if (f5RecordPrivateDecrement(user)) return;\n'
        '        requireNoReadOnlyReplay("remove congestion user");')
    source = replace_once(source, '        return usersConnectionCounts.size();',
        '        return usersConnectionCounts.size() - f5RemovedUserFromOccupancy();')
    source = replace_once(source, '        return usersConnectionCounts.getOrDefault(user, 0);',
        '        return usersConnectionCounts.getOrDefault(user, 0) - f5PrivateRemoval(user);')
    source = replace_once(source,
        '    public Map<NetWrapper, Integer> getUsersConnectionCounts() {',
        '    public Map<NetWrapper, Integer> getUsersConnectionCounts() {\n'
        '        requireNoReadOnlyReplay("read mutable occupancy map");')
    return source


def reroute_methods(source, helpers):
    source = replace_once(source, '    private static final class F5PairSearch {',
        helpers + '\n    private static final class F5PairSearch {')
    source = replace_once(source, '        final List<RouteNode> path;',
        '        final List<RouteNode> path;\n        final List<F5UsageRow> usage;')
    source = replace_once(source, '                     List<RouteNode> path) {',
        '                     List<RouteNode> path, List<F5UsageRow> usage) {')
    source = replace_once(source, '            this.path = path;',
        '            this.path = path;\n            this.usage = usage;')
    source = replace_once(source, '            writes.addAll(path);',
        '            writes.addAll(path);\n'
        '            for (F5UsageRow row : usage) {\n'
        '                if (row.removed()) writes.add(row.node);\n'
        '            }')
    source = replace_once(source,
        '                    || first.isRouted() || second.isRouted()\n'
        '                    || !first.getRnodes().isEmpty() || !second.getRnodes().isEmpty()\n'
        '                    || !shouldRoute(second)) {\n'
        '                throw new IllegalStateException("First-round pair precondition failed at " + seq);',
        '                    || !first.isRouted() || !second.isRouted()\n'
        '                    || first.getRnodes().isEmpty() || second.getRnodes().isEmpty()) {\n'
        '                throw new IllegalStateException("Existing-route pair precondition failed at " + seq);')
    source = source.replace('f5RouteFirstRound', 'f5RouteRerouteRound')
    source = replace_once(source, 'Missed a planned first-round pair', 'Missed a planned reroute pair')
    source = replace_once(source,
        '        ConnectionState state = new ConnectionState();\n'
        '        f5InitCpuSearch(state, connection, seq);\n'
        '        state.f5ReadOnlyReplay = true;\n'
        '        RouteNode.SearchScratch scratch = new RouteNode.SearchScratch();',
        '        List<RouteNode> oldPath = List.copyOf(connection.getRnodes());\n'
        '        NetWrapper net = connection.getNetWrapper();\n'
        '        List<F5UsageRow> before = f5ReadUsage(oldPath, net, null);\n'
        '        if (!warm) return f5RunRerouteSearch(connection, seq, oldPath, before, false);\n'
        '        try (RouteNode.F5UsageOracle oracle = new RouteNode.F5UsageOracle(oldPath)) {\n'
        '            ripUp(connection);\n'
        '            return f5RunRerouteSearch(connection, seq, oldPath, before, true);\n'
        '        }\n'
        '    }\n\n'
        '    private F5PairSearch f5RunRerouteSearch(Connection connection, int seq, List<RouteNode> oldPath,\n'
        '                                           List<F5UsageRow> before, boolean warm) {\n'
        '        ConnectionState state = new ConnectionState();\n'
        '        f5InitCpuSearch(state, connection, seq);\n'
        '        state.f5ReadOnlyReplay = true;\n'
        '        RouteNode.SearchScratch scratch = new RouteNode.SearchScratch();')
    source = replace_once(source, '        try {\n            f5SeedRouteConnection(state);',
        '        try {\n'
        '            if (!warm) {\n'
        '                scratch.beginPrivateRipUp(connection.getNetWrapper());\n'
        '                try { ripUp(connection); } finally { scratch.endPrivateRipUp(); }\n'
        '            }\n'
        '            List<F5UsageRow> usage = f5ReadUsage(oldPath, connection.getNetWrapper(), before);\n'
        '            if (usage.stream().noneMatch(F5UsageRow::removed)) {\n'
        '                throw new IllegalStateException("Reroute sample made no rip-up changes");\n'
        '            }\n'
        '            f5SeedRouteConnection(state);')
    source = replace_once(source, 'new F5PairSearch(state, scratch, target, path);',
        'new F5PairSearch(state, scratch, target, path, usage);')
    source = replace_once(source, '        if (warm.target != frozen.target',
        '        f5CheckSameUsage(warm.usage, frozen.usage);\n        if (warm.target != frozen.target')
    source = replace_once(source, '        long pushed = nodesPushed.get();',
        '        boolean secondAdmitted = shouldRoute(second);\n        long pushed = nodesPushed.get();')
    source = replace_once(source,
        '                || first.isRouted() || second.isRouted()\n'
        '                || !first.getRnodes().isEmpty() || !second.getRnodes().isEmpty()) {',
        '                || !first.isRouted() || !second.isRouted()) {')
    source = replace_once(source, '        f5MatchWarm(warmFirst, a);',
        '        f5CheckOriginalUsage(first, warmFirst.usage);\n'
        '        f5CheckOriginalUsage(second, warmSecond.usage);\n'
        '        f5MatchWarm(warmFirst, a);')
    source = replace_once(source, '        f5LogPairSearch(a);\n        f5LogPairSearch(b);',
        '        f5LogPairSearch(a);\n'
        '        f5LogPairSearch(b);\n'
        '        f5LogUsage("ORACLE", a.state.sequence, warmFirst.usage);\n'
        '        f5LogUsage("PRIVATE", a.state.sequence, a.usage);\n'
        '        f5LogUsage("ORACLE", b.state.sequence, warmSecond.usage);\n'
        '        f5LogUsage("PRIVATE", b.state.sequence, b.usage);')
    source = replace_once(source,
        '        boolean conflict = wr || rw || ww;\n'
        '        System.out.printf("F5_PAIR_DECISION pair=%d kind=%s wr=%s rw=%s ww=%s warmNodes=%d action=%s%n",\n'
        '                spec.seq, spec.kind, wr, rw, ww, frozenNodes - beforeWarm, conflict ? "serial" : "ordered");',
        '        boolean conflict = wr || rw || ww || !secondAdmitted;\n'
        '        System.out.printf("F5_PAIR_DECISION pair=%d kind=%s wr=%s rw=%s ww=%s admitted=%s warmNodes=%d action=%s%n",\n'
        '                spec.seq, spec.kind, wr, rw, ww, secondAdmitted, frozenNodes - beforeWarm,\n'
        '                conflict ? "serial" : "ordered");')
    source = replace_once(source, '        result.scratch.attachPairForCommit();\n        try {',
        '        f5CheckOriginalUsage(state.connection, result.usage);\n'
        '        result.scratch.attachPairForCommit();\n'
        '        try {\n'
        '            ripUp(state.connection);\n'
        '            List<F5UsageRow> actual = f5ReadUsage(state.connection.getRnodes(),\n'
        '                    state.connection.getNetWrapper(), result.usage);\n'
        '            f5CheckSameUsage(result.usage, actual);\n'
        '            f5LogUsage("PUBLISH", state.sequence, actual);')
    return source


def prepare_reroute(source_dir, work):
    scope = (work / 'route_node_scope.inc').read_text(encoding='utf-8')
    replay = (work / 'replay_methods.inc').read_text(encoding='utf-8')
    methods = reroute_methods((work / 'pair_methods.inc').read_text(encoding='utf-8'), (work / 'reroute_methods.inc').read_text(encoding='utf-8'))
    occ = (work / 'private_usage.inc').read_text(encoding='utf-8')
    diffs = []
    for name in ('RouteNode.java', 'RWRoute.java', 'RouteNodeGraph.java'):
        original = (source_dir / name).read_text(encoding='utf-8')
        if name == 'RouteNode.java':
            modified = patch_occupancy(patch_pair_node(patch_replay_node(patch_node(original, scope))), occ)
        elif name == 'RWRoute.java':
            modified = patch_pair_router(patch_replay_router(patch_router(original), replay), methods)
            modified = replace_once(modified,
                '        if (!f5DistinctPairs.isEmpty() && routeIteration == 1) {\n            f5RouteFirstRound(connections);',
                '        if (!f5DistinctPairs.isEmpty() && routeIteration == 4) {\n            f5RouteRerouteRound(connections);')
        else:
            anchor = '    public RouteNode getOrCreate(Node node, RouteNodeType type) {'
            modified = replace_once(original, anchor,
                anchor + '\n        RouteNode.requireNoFrozenPairGraphWrite("create graph node");')
        (work / name).write_text(modified, encoding='utf-8')
        diffs.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                     'formal/' + name, 'isolated/' + name))
    (work / 'reroute-occupancy.diff').write_text(''.join(diffs), encoding='utf-8')