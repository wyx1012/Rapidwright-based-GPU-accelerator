import difflib

from pair_patch import patch_pair_node, patch_pair_router
from reroute_patch import patch_occupancy, reroute_methods
from scratch_patch import patch_node, patch_replay_node, patch_replay_router, patch_router, replace_once


def patch_direct_node(source, scope, usage):
    node = patch_pair_node(patch_replay_node(patch_node(source, scope)))
    occupancy_helpers = usage[usage.index('    private int f5PrivateRemoval('):]
    node = patch_occupancy(node, occupancy_helpers)
    node = replace_once(node, '        private boolean warmProbe;\n', '')
    node = replace_once(node, '            warmProbe = false;\n', '')
    begin = node.index('        void openWarmProbe() {')
    end = node.index('        void openFrozenPair() {', begin)
    node = node[:begin] + node[end:]
    if node.count(' || warmProbe\n') != 2:
        raise ValueError('Warm-probe guard anchors changed')
    node = node.replace(' || warmProbe\n', '\n')
    node = replace_once(node, ' && !scratch.warmProbe', '')
    node = replace_once(node, '    private void recordPairRead() {',
        '    static final class F5ColdAdjacency extends IllegalStateException {\n'
        '        final long nodeKey;\n\n'
        '        F5ColdAdjacency(long nodeKey) {\n'
        '            super("Uncached adjacency " + nodeKey);\n'
        '            this.nodeKey = nodeKey;\n'
        '        }\n'
        '    }\n\n'
        '    private void recordPairRead() {')
    node = replace_once(node, '            requireNoFrozenPairGraphWrite("uncached adjacency");',
        '            SearchScratch scratch = ACTIVE_SCRATCH.get();\n'
        '            if (scratch != null && scratch.readOnlyReplay) {\n'
        '                long key = ((long) getTile().getUniqueAddress() << 32)\n'
        '                        | Integer.toUnsignedLong(getWireIndex());\n'
        '                throw new F5ColdAdjacency(key);\n'
        '            }')
    return node


def direct_methods(source, helpers):
    source = replace_once(source, '        final int seq;', '        final int round;\n        final int seq;')
    source = replace_once(source,
        '            if (parts.length != 4) throw new IllegalArgumentException("Invalid F5 pair");\n'
        '            seq = Integer.parseInt(parts[0]);\n'
        '            firstId = Integer.parseInt(parts[1]);\n'
        '            secondId = Integer.parseInt(parts[2]);\n'
        '            kind = parts[3];\n'
        '            if (!("positive".equals(kind) || "negative".equals(kind))) {',
        '            if (parts.length != 5) throw new IllegalArgumentException("Invalid F5 pair");\n'
        '            round = Integer.parseInt(parts[0]);\n'
        '            seq = Integer.parseInt(parts[1]);\n'
        '            firstId = Integer.parseInt(parts[2]);\n'
        '            secondId = Integer.parseInt(parts[3]);\n'
        '            kind = parts[4];\n'
        '            if ((round != 1 && round != 4)\n'
        '                    || !("positive".equals(kind) || "negative".equals(kind) || "sample".equals(kind))) {')
    start = source.index('    private F5PairSearch f5SearchPairMember(')
    end = source.index('    private static String f5NodeKeys(', start)
    source = source[:start] + source[end:]
    start = source.index('    private void f5RunPair(')
    end = source.index('    private void f5PublishPairMember(', start)
    source = source[:start] + helpers + '\n' + source[end:]
    start = source.index('    private void f5RouteRerouteRound(')
    end = source.index('    private void f5RouteRegular(', start)
    new_dispatch = '''    private void f5RouteDirectRound(Collection<Connection> connections) {
        if (!f5UseNodeScratch || getClass() != PartialRouter.class || config.isTimingDriven()
                || config.isFidesLaneMirror() || fidesLaneGate != null) {
            throw new IllegalStateException("Direct F5 pairs require isolated CPU partial routing");
        }
        int expected = (int) f5DistinctPairs.values().stream().filter(pair -> pair.round == routeIteration).count();
        int observed = 0;
        var pool = java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            Iterator<Connection> remaining = connections.iterator();
            while (remaining.hasNext()) {
                Connection first = remaining.next();
                if (!shouldRoute(first)) continue;
                int seq = connectionsRouted.get() + 1;
                F5PairSpec pair = f5DistinctPairs.get(seq);
                if (pair == null) {
                    f5RouteRegular(first);
                    continue;
                }
                if (pair.round != routeIteration || !remaining.hasNext()) {
                    throw new IllegalStateException("Missing planned adjacent connection");
                }
                Connection second = remaining.next();
                boolean reroute = routeIteration != 1;
                if (first.hashCode() != pair.firstId || second.hashCode() != pair.secondId
                        || first.getNetWrapper() == second.getNetWrapper()
                        || (first.getRnodes().isEmpty() == reroute)
                        || (second.getRnodes().isEmpty() == reroute)) {
                    throw new IllegalStateException("Direct pair precondition failed at " + seq);
                }
                if (config.takesConnectionOnLane(seq) || config.takesConnectionOnLane(seq + 1)) {
                    throw new IllegalStateException("Direct F5 pair cannot enter lane routing");
                }
                f5RunPair(first, second, pair, pool);
                observed++;
            }
        } finally {
            pool.shutdownNow();
        }
        if (observed != expected) throw new IllegalStateException("Missed a planned direct pair");
    }

'''
    source = source[:start] + new_dispatch + source[end:]
    return replace_once(source, '            f5LogUsage("PUBLISH", state.sequence, actual);',
        '            if (!actual.isEmpty()) f5LogUsage("PUBLISH", state.sequence, actual);')


def prepare_direct(source_dir, work):
    scope = (work / 'route_node_scope.inc').read_text(encoding='utf-8')
    replay = (work / 'replay_methods.inc').read_text(encoding='utf-8')
    methods = reroute_methods((work / 'pair_methods.inc').read_text(encoding='utf-8'),
                              (work / 'reroute_methods.inc').read_text(encoding='utf-8'))
    methods = direct_methods(methods, (work / 'direct_methods.inc').read_text(encoding='utf-8'))
    usage = (work / 'private_usage.inc').read_text(encoding='utf-8')
    diff = []
    for name in ('RouteNode.java', 'RWRoute.java', 'RouteNodeGraph.java'):
        original = (source_dir / name).read_text(encoding='utf-8')
        if name == 'RouteNode.java':
            modified = patch_direct_node(original, scope, usage)
        elif name == 'RWRoute.java':
            modified = patch_pair_router(patch_replay_router(patch_router(original), replay), methods)
            modified = replace_once(modified,
                '        if (!f5DistinctPairs.isEmpty() && routeIteration == 1) {\n            f5RouteFirstRound(connections);',
                '        if (!f5DistinctPairs.isEmpty() && (routeIteration == 1 || routeIteration == 4)) {\n'
                '            f5RouteDirectRound(connections);')
        else:
            anchor = '    public RouteNode getOrCreate(Node node, RouteNodeType type) {'
            modified = replace_once(original, anchor,
                anchor + '\n        RouteNode.requireNoFrozenPairGraphWrite("create graph node");')
        (work / name).write_text(modified, encoding='utf-8')
        diff.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
                                         'formal/' + name, 'isolated/' + name))
    (work / 'direct-frozen.diff').write_text(''.join(diff), encoding='utf-8')