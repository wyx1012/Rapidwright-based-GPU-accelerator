"""Patch v73 sources cleanly to v76:
1. PartialRouter: keep only lightweight 64-shard check + unroutedPins inside checkFuture,
   moving deferred STATS_WL right after writeCheckpoint completes to eliminate Write EDIF contention.
2. F1Corridor: pre-size 64 IdentityHashMap shards (1<<17) & skip retired/empty map checks in R1.
3. FidesLaneGate & RWRoute: trim per-bucket temporary ArrayList sizes in materialize & pre-size assignNodes lists.
"""

import v73_patch as v73


def once(source, anchor, updated):
    return v73.once(source, anchor, updated)


def cuda(src):
    return v73.cuda(src)


def node(src):
    return v73.node(src)


def broker(src):
    return v73.broker(src)


def diversion(src):
    return v73.diversion(src)


def f2prewarm(src):
    return v73.f2prewarm(src)


def idmap(src):
    return v73.idmap(src)


def handoff(src):
    return v73.handoff(src)


def worker(src):
    return v73.worker(src)


def corridor(src):
    src = v73.corridor(src)
    src = once(
        src,
        "        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 14);",
        "        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 17);",
    )
    src = once(
        src,
        """            if (connection.getHpwl() >= 16) {
                Map<Connection, Candidate> cmap = candMap(connection);
                if (cmap.containsKey(connection)) {
                    Candidate existing = cmap.get(connection);
                    if (existing != null && existing.matches(connection)) return existing.tight();
                    return null;
                }
                Candidate planned = plan(connection);
                out[index] = new ParallelEntry(planned, null);
                return planned != null ? planned.tight() : null;
            }
            if (candMap(connection).containsKey(connection)) return null;""",
        """            if (connection.getHpwl() >= 16) {
                Map<Connection, Candidate> cmap = candMap(connection);
                if (!cmap.isEmpty()) {
                    Candidate existing = cmap.get(connection);
                    if (existing != null && existing.matches(connection)) return existing.tight();
                    if (cmap.containsKey(connection)) return null;
                }
                Candidate planned = plan(connection);
                out[index] = new ParallelEntry(planned, null);
                return planned != null ? planned.tight() : null;
            }
            if (!f4Retired.isEmpty() && candMap(connection).containsKey(connection)) return null;""",
    )
    src = once(
        src,
        """        Map<Connection, Candidate> cmap = candMap(connection);
        if (cmap.containsKey(connection)) {
            Candidate existing = cmap.get(connection);
            if (existing == null || existing.source != source || existing.sink != sink || !existing.base.same(base)) {
                return null;
            }
            return crossSlrCompactX ? compactMap(connection).getOrDefault(connection, existing.tight()) : existing.tight();
        }""",
        """        Map<Connection, Candidate> cmap = candMap(connection);
        if (!cmap.isEmpty()) {
            Candidate existing = cmap.get(connection);
            if (existing != null) {
                if (existing.source != source || existing.sink != sink || !existing.base.same(base)) {
                    return null;
                }
                return crossSlrCompactX ? compactMap(connection).getOrDefault(connection, existing.tight()) : existing.tight();
            }
            if (cmap.containsKey(connection)) {
                return null;
            }
        }""",
    )
    return src


def gate(src):
    src = v73.gate(src)
    src = once(
        src,
        """                for (int s = 0; s < SHARDS; ++s) {
                    localNodes[s] = new java.util.ArrayList<>(2048);
                    localNets[s] = new java.util.ArrayList<>(2048);
                }""",
        """                for (int s = 0; s < SHARDS; ++s) {
                    localNodes[s] = new java.util.ArrayList<>(256);
                    localNets[s] = new java.util.ArrayList<>(256);
                }""",
    )
    return src


def partial_router(src):
    src = v73.partial_router(src)
    src = once(
        src,
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
                    long tEnd = System.nanoTime();""",
        """                        long chkNs = System.nanoTime() - tChk0;
                        return new long[] { unroutedPins, sharedCount, totalPips, chkNs, 0L };
                    }, checkPool);

                    long tWr0 = System.nanoTime();
                    routed.writeCheckpoint(routedDCPfileName, t);
                    long tWr1 = System.nanoTime();
                    long[] chk = checkFuture.get();
                    final RWRoute deferredRouter = fidesLastRouter;
                    if (deferredRouter != null) {
                        long tS0 = System.nanoTime();
                        deferredRouter.fidesRunDeferredStats();
                        chk[4] = System.nanoTime() - tS0;
                    }
                    long tEnd = System.nanoTime();""",
    )
    return src


def router(src):
    src = v73.router(src)
    src = once(
        src,
        """                    Connection connection = indirectConnections.get(i);
                    List<Node> nodes = new ArrayList<>();
                    connection.setNodes(nodes);""",
        """                    Connection connection = indirectConnections.get(i);
                    List<RouteNode> rnodes = connection.getRnodes();
                    if (rnodes.isEmpty()) {
                        connection.setNodes(java.util.Collections.emptyList());
                        continue;
                    }
                    List<Node> nodes = new ArrayList<>(rnodes.size() + 4);
                    connection.setNodes(nodes);""",
    )
    src = once(
        src,
        """                    RouteNode sinkRnode = connection.getSinkRnode();
                    List<RouteNode> rnodes = connection.getRnodes();
                    if (rnodes.isEmpty()) {
                        continue;
                    }""",
        """                    RouteNode sinkRnode = connection.getSinkRnode();""",
    )
    return src
