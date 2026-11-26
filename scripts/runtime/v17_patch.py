"""Use the whole-round region dependency queue instead of a sequential scan window."""
from v11_window import replace_once as once


def router(src, work):
    begin = src.index("    private int f4FirstPlan(List<Connection>")
    end = src.index("    private void f4FirstBoxTally(", begin)
    src = src[:begin] + (work / 'F4RegionQueue.inc').read_text() + src[end:]
    src = once(src, "    private static final int F4_FIRST_LOOKAHEAD = 256;\n", "")
    src = once(src, "        long privateCap, privateMiss, noIndependent, shape, routeNotNeeded;",
        """        long privateCap, privateMiss, noIndependent, shape, routeNotNeeded;
        long regionIndexed, regionCells, regionPlanMs, regionEvicted, regionMaxWidth, regionMaxStaged;
        long regionSpecSelected;""")
    src = once(src, '               .put("already_routed_skips", routeNotNeeded).put("indirect_gpu_batches", 0);',
        '''               .put("already_routed_skips", routeNotNeeded).put("indirect_gpu_batches", 0)
               .put("region_indexed", regionIndexed).put("region_cells", regionCells)
               .put("region_index_ms", regionPlanMs).put("region_evicted", regionEvicted)
               .put("region_max_search_width", regionMaxWidth).put("region_peak_staged", regionMaxStaged)
               .put("region_spec_selected", regionSpecSelected);''')
    src = once(src, """        int nextPlan = 0;
        Map<Integer, F4SpatialResult> ready = new java.util.TreeMap<>();
        try {""",
        """        java.util.NavigableMap<Integer, F4SpatialResult> ready = new java.util.TreeMap<>();
        f4RegionRetired = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
        try {
            f4First.report(routeIteration, 0, all.size(), 0, true);
            long prep = System.nanoTime();
            F4RegionQueue queue = new F4RegionQueue(all);
            f4First.regionPlanMs = (System.nanoTime() - prep) / 1_000_000L;
            f4First.regionIndexed = queue.indexed;
            f4First.regionCells = queue.occupied;
            f4First.report(routeIteration, 0, all.size(), 0, true);""")
    src = once(src, "                f4FirstPosition = i;\n                if ((i & 63)",
        "                f4FirstPosition = i;\n                queue.publish(i);\n                if ((i & 63)")
    src = once(src, """                if (ready.isEmpty() && i >= nextPlan && f4SharedPool.getParallelism() >= 2) {
                    nextPlan = f4FirstPlan(all, i, gate, ready);
                }""",
        """                if (f4SharedPool.getParallelism() >= 2
                        && (!ready.containsKey(i) || ready.size() < 16)) {
                    f4RegionFill(queue, all, i, gate, ready);
                }""")
    src = once(src, "                    state.f4Boundary = gate.f4Boundary(c);",
        "                    state.f4Boundary = f4RegionBound(c, gate);")
    src = once(src, """                    if (narrowed && state.f4Boundary == null) {
                        ++f4First.retries;""",
        """                    if (narrowed && state.f4Boundary == null) {
                        f4RegionRetired.add(c);
                        ++f4First.retries;""")
    return once(src, """            for (var r : ready.values()) f4FirstDiscard(r);
            f4First = null;""",
        """            for (var r : ready.values()) f4FirstDiscard(r);
            f4RegionRetired = null;
            f4First = null;""")

