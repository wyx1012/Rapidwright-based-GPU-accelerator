"""Connect the authoritative Java user maps to the existing GPU delta ABI."""
from v11_window import replace_once as once


def patch_gate(source):
    source = once(source, "    private final java.util.IdentityHashMap<Connection, int[]> f4OldPaths = new java.util.IdentityHashMap<>();\n",
                  "")
    source = once(source, "            F4FeatureDirect features) {\n        long[] scores",
                  "            F4FeatureDirect features) {\n        long[] scores")
    start = source.index("    void mirrorBeforeAttempt(Connection c) {")
    end = source.index("    /** The delta of the attempt", start)
    old = source[start:end]
    old = once(old, "        mirror.connections++;\n", """        mirror.connections++;
        if (f4Live != null) {
            f4Live.markPath(c);
            return;
        }
""")
    old = once(old, """            if (f4Live != null && f4Live.tracking()) {
                f4Live.track(members);
                if (mirror.currentNet()!=null) f4Live.track(membersOf((NetWrapper)mirror.currentNet()));
            }
""", "")
    old = once(old, """        if (f4Live != null && f4OldPaths.put(c, delta) != null) {
            throw new IllegalStateException("Duplicate GPU batch preparation");
        }
""", "")
    for anchor in ("            if (f4Live != null) f4Live.track(delta);\n",
                   "                if (f4Live != null) f4Live.track(now);\n"):
        old = once(old, anchor, "")
    old = once(old, """        if (c.isRouted()) {
            final int[] now = deltaIds(c);""",
        """        if (f4Live != null) {
            f4Live.markPath(c);
            return;
        }
        if (c.isRouted()) {
            final int[] now = deltaIds(c);""")
    old = once(old, """        if (f4Live != null) {
            attemptDelta = f4OldPaths.remove(c);
            if (attemptDelta == null) throw new IllegalStateException("Missing GPU batch rip-up");
        }
        if (f4Live == null) {
            mirror.assertNodes(attemptDelta);
            mirror.assertNodes(deltaIds(c));
        }""", """        mirror.assertNodes(attemptDelta);
        mirror.assertNodes(deltaIds(c));""")
    source = source[:start] + old + source[end:]
    source = once(source, "        if (f4Live != null) f4Live.history(iteration,overUsedIds);",
                  """        if (f4Live != null) {
            f4Live.markHistory(overUsed);
            return;
        }""")
    source = once(source, "        mirrorFixRips++;\n        final int[] members",
                  """        mirrorFixRips++;
        if (f4Live != null) {
            f4Live.markPath(c);
            return;
        }
        final int[] members""")
    return source


def patch_capture(source):
    start = source.index("    private final java.util.Map<Integer,int[]> dirty")
    end = source.index("    void before(", start)
    region = source[start:end]
    first = region.index("    boolean tracking()")
    # Keep unrelated fields and helpers above the old tracker, including its directory/JSON helpers.
    prefix = region[:first]
    prefix = once(prefix, "    private final java.util.Map<Integer,int[]> dirty = new java.util.TreeMap<>();",
                  "    private final F4HostJournal journal = new F4HostJournal();")
    prefix = once(prefix, "    private final java.util.HashSet<Integer> historyIds = new java.util.HashSet<>();\n", "")
    prefix = prefix.replace("    private boolean tracking;\n", "")
    source = source[:start] + prefix + """    void markPath(Connection connection) {
        if (!connection.isRouted()) return;
        var path = connection.getRnodes();
        if (path.isEmpty()) return;
        boolean exclusive = path.get(0) == connection.getSinkRnode() && !connection.hasAltSinks();
        int start = FidesMutableMirror.userDeltaStart(path.size(), exclusive);
        int end = FidesMutableMirror.userRangeEnd(path.size(), exclusive);
        for (int i = start; i < end; ++i) journal.mark(path.get(i));
    }

    void markHistory(Iterable<RouteNode> overUsed) {
        overUsed.forEach(journal::mark);
    }

    private static void word(java.io.DataOutputStream out, int value) throws IOException {
        out.writeInt(Integer.reverseBytes(value));
    }

""" + source[end:]
    source = once(source, "float[] pcc,int[] slr,Connection c, long score)",
                  "float[] pcc,int[] slr,Connection c, long score, FidesRrgIdMap idMap)")
    source = once(source, "            if (phase==1) {\n                mutableMirror=mirror;",
                  """            if (phase==1) {
                mutableMirror=mirror;
                journal.bind((int[]) mirrorColumn(mirror, "occ"), (int[]) mirrorColumn(mirror, "same"),
                        (float[]) mirrorColumn(mirror, "hist"));
                journal.seal(c.getNetWrapper(), idMap, null);""")
    source = once(source, "                tracking=true;\n", "")
    source = once(source, """                require(mutableMirror == mirror && (roundChanged || historyIds.isEmpty()),
                        "Unexpected history update inside a round");""",
                  """                require(mutableMirror == mirror, "Unexpected mirror change");""")
    start = source.index("                try (var out=new java.io.DataOutputStream")
    end = source.index("                if (refreshPcc)", start)
    source = source[:start] + """                F4HostJournal.Rows rows;
                try (var out = new java.io.BufferedOutputStream(
                        Files.newOutputStream(step.resolve("between.bin"), StandardOpenOption.CREATE_NEW))) {
                    rows = journal.seal(c.getNetWrapper(), idMap, out);
                }
                deltaRows = rows.changed;
                occRows = rows.occupancy;
                sameRows = rows.sameNet;
                historyRows = rows.history;
""" + source[end:]
    source = source.replace("                dirty.clear();\n                historyIds.clear();\n", "")
    source = once(source, ".put(\"refresh_pcc\", refreshPcc).put(\"feature_score\", featureScore));",
                  ".put(\"refresh_pcc\", refreshPcc).put(\"feature_score\", featureScore)\n"
                  "                    .put(\"journal\", journal.receipt()));")
    return once(source, """        before(attempts.get(0), iteration, rrg, mirror, pcc, slr, states.get(0).connection, scores[0]);""",
                """        before(attempts.get(0), iteration, rrg, mirror, pcc, slr, states.get(0).connection, scores[0], ids);""")
