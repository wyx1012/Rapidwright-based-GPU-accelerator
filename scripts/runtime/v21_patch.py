"""Rewrite v20-r2's round preparation and serial search hot paths."""
from v11_window import replace_once as once


def replace_block(src, first, following, replacement):
    start = src.index(first)
    end = src.index(following, start + len(first))
    return src[:start] + replacement + src[end:]


def feature(src):
    return once(src, "    long offer(Connection connection, boolean supported) {",
                '''    long f21EffectiveWork(Connection connection, int width, int height) {
        int offset = offsetOf(connection.hashCode());
        if (offset < 0) return -1;
        long area = rows.getLong(offset + 24), span = rows.getLong(offset + 32);
        long effectiveArea = Math.min(area, (long) width * height);
        long effectiveSpan = Math.min(span, (long) width + height);
        long score = rows.getLong(offset + 64) & 0x7fffffffL;
        return Math.max(1, score - area - 32 * span + effectiveArea + 32 * effectiveSpan);
    }

    long offer(Connection connection, boolean supported) {''')


def node(src):
    return once(src, "    public void setLowerBoundTotalPathCost(float totalPathCost) {",
                '''    void f21LabelOnPush(float total, float upstream, int sequence) {
        assert(sequence > 0);
        ScratchValues values = scratchValuesForWrite();
        if (values == null) {
            lowerBoundTotalPathCost = total;
            upstreamPathCost = upstream;
            visited = sequence;
        } else {
            values.lowerBoundTotalPathCost = total;
            values.upstreamPathCost = upstream;
            values.visited = sequence;
        }
    }

    public void setLowerBoundTotalPathCost(float totalPathCost) {''')


def router(src, work):
    src = replace_block(src, "    private static final class F4RegionSplit {",
        "    private static final int F4_MAX_GPU_TRIALS", "")
    src = replace_block(src, "    private final class F4RegionQueue {",
        "        void publish(int cursor)", (work / "F21Regions.inc").read_text())
    src = replace_block(src, "    private int f4CriticalMode(F4CriticalCost cost) {",
        "    private void f4RecordCriticalTail(", (work / "F21Policy.inc").read_text())
    src = once(src, "    private static final int F4_REGION_SHIFT = 4;",
                    "    private static final int F4_REGION_SHIFT = 3;")
    src = once(src, "            if (p - i > F4_REGION_NEAR || chosen.size() == lanes) break;",
                    "            if (queue.rank[p] - queue.rank[i] > F4_REGION_NEAR || chosen.size() == lanes) break;")
    src = once(src, "            chosen.add(new F4FirstCandidate(c, f4AdaptiveBound(c, gate), p));",
                    "            chosen.add(new F4FirstCandidate(c, queue.bound(p, c, gate), p));")
    src = once(src, """            queue.take(c.position);
            int tier = f4Service(c.connection, gate);""", """            queue.take(c.position);
            int tier = queue.service[c.position];""")
    src = once(src, "            F4RegionQueue queue = new F4RegionQueue(all);",
                    """            F4RegionQueue queue = new F4RegionQueue(all, gate);
            f21Stage(queue);
            var serialState = new ConnectionState();
            serialState.f21BoundFirst = true;""")
    src = once(src, """                    if (!f4FirstShape(c)) ++f4First.shape;
                    var state = new ConnectionState();
                    f4FillHostState(state, c, sequence);
                    state.f4Boundary = f4AdaptiveBound(c, gate);""", """                    if (!f4FirstShape(c)) ++f4First.shape;
                    var state = serialState;
                    assert(state.queue.isEmpty() && state.targets.isEmpty());
                    f4FillHostState(state, c, sequence);
                    state.f4HandoffBudget = state.f4CpuPopsBeforeHandoff = 0;
                    state.f4Boundary = queue.bound(i, c, gate);""")
    src = once(src, """                    int tier = f4Service(c, gate);
                    if (tier >= 0 && !gate.f4Supported(c, state)) tier = -1;
                    boolean narrowed = state.f4Boundary != null;
                    boolean chain = queue.blocker[i] >= 0 && f4FirstShape(c);""", """                    int tier = queue.tier(i, c, gate, state.f4Boundary);
                    if (tier >= 0 && !gate.f4Supported(c, state)) tier = -1;
                    boolean narrowed = state.f4Boundary != null;
                    boolean chain = f4FirstShape(c);""")
    src = once(src, """                    int gpuMode = f4CriticalMode(cost);
                    var search = f4SearchWithBoundary(state, tier, gpuMode != 0);""",
                    """                    int gpuMode = f21CriticalMode(cost, tier);
                    var search = f4SearchWithBoundary(state, tier, gpuMode != 0);
                    if (tier >= 0 && search.crossingNs >= 0) ++f21Proofs[tier];""")
    src = once(src, """            f4First.report(routeIteration, all.size(), all.size(), 0, true);
            f4SpatialGroups += f4First.groups;""",
                    """            f21FinishStage();
            f4First.report(routeIteration, all.size(), all.size(), 0, true);
            f4SpatialGroups += f4First.groups;""")
    src = once(src, "        return f4GpuGained > 0 && f4LatestWarmGain && f4GpuLearningBalance == 0;",
                    "        return f4GpuGained > 0 && f4LatestWarmGain;")
    src = once(src, """        } else {
            cost.recordGpu(elapsed);
        }
        if (mode == F4_GPU_TRIAL)""", """        } else {
            cost.recordGpu(elapsed);
            f21MinimumWarmNs = Math.min(f21MinimumWarmNs, elapsed);
        }
        if (mode == F4_GPU_TRIAL)""")
    src = once(src, """        childRnode.setLowerBoundTotalPathCost(newTotalPathCost);
        childRnode.setUpstreamPathCost(newPartialPathCost);
        // Use the number-of-connections-routed-so-far as the identifier for whether a rnode
        // has been visited by this connection before
        childRnode.setVisited(state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence);""",
                    """        childRnode.f21LabelOnPush(newTotalPathCost, newPartialPathCost,
                state.f4RetryStamp != 0 ? state.f4RetryStamp : state.sequence);""")
    src = once(src, "        boolean f4PrivateSearch;\n", "        boolean f4PrivateSearch;\n        boolean f21BoundFirst;\n")
    src = once(src, """        for (RouteNode childRNode : rnode.getChildren(routingGraph)) {
            if (childRNode.isVisited(sequence)) {""", """        F1Corridor.Box earlyBox = state.f21BoundFirst ? state.f4Boundary : null;
        for (RouteNode childRNode : rnode.getChildren(routingGraph)) {
            if (earlyBox != null && !earlyBox.contains(childRNode) && !childRNode.isTarget()) {
                ++state.f4OutsideChecks;
                continue;
            }
            if (childRNode.isVisited(sequence)) {""")
    src = once(src, """                if (!isAccessible(childRNode, connection)) continue;
                if (state.f4Boundary != null && !state.f4Boundary.contains(childRNode)) {""",
                    """                if (!isAccessible(childRNode, connection)) continue;
                if (earlyBox == null && state.f4Boundary != null && !state.f4Boundary.contains(childRNode)) {""")
    return src

