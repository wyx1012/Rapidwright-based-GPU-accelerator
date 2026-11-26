"""Remove redundant diagnostics from the isolated GPU corridor Java overlay."""


def replace_once(source: str, old: str, new: str = "") -> str:
    if source.count(old) != 1:
        raise ValueError("Unexpected GPU corridor source anchor: " + old.strip().splitlines()[0])
    return source.replace(old, new, 1)


def patch_gate(source: str) -> str:
    attempt_log = r'''        System.out.println("=== FIDES LANE ATTEMPT === sequence=" + a.sequence
                + " connection=\"" + connection + "\"");
        System.out.println("  sourceId=" + a.sourceId + " sinkId=" + a.sinkId
                + " targets=" + targetIds.length
                + " weights(cost=" + a.rnodeCostWeight + " share=" + a.shareWeight
                + " wl=" + a.rnodeWLWeight + " est=" + a.estWlWeight + " crit=" + a.criticality + ")");
        System.out.println("  net(x=" + a.xCenter + " y=" + a.yCenter + " hpwl=" + a.doubleHpwl
                + " size=" + a.netSize + ") sinkBegin=(" + a.sinkBeginX + "," + a.sinkBeginY + ")"
                + " bbox=[" + a.xMinBB + "," + a.xMaxBB + "]x[" + a.yMinBB + "," + a.yMaxBB + "]"
                + " useBB=" + a.useBoundingBox + " uTurn=" + a.useUTurnNodes
                + " crossSLR=" + a.crossSLR + " altSinks=" + a.hasAltSinks
                + " sinkType=" + a.sinkTypeOrdinal + " sinkIntent=" + a.sinkIntentOrdinal
                + " sinkTile=" + a.sinkTileId + " sinkCtrl=" + a.sinkWireIsCtrl);
'''
    source = replace_once(source, attempt_log)
    source = replace_once(source,
        """        mirror.assertNodes(attemptDelta);
        mirror.assertNodes(deltaIds(c));
        attemptDelta = EMPTY_IDS;""",
        """        if (f4Live == null) {
            mirror.assertNodes(attemptDelta);
            mirror.assertNodes(deltaIds(c));
        }
        attemptDelta = EMPTY_IDS;""")
    source = replace_once(source,
        """        lane.mirrorSync(FidesLane.MIRROR_HIST, overUsedIds);
        mirror.fullScan();""",
        """        lane.mirrorSync(FidesLane.MIRROR_HIST, overUsedIds);
        if (f4Live == null) mirror.fullScan();""")
    return replace_once(source,
        """    void mirrorClosingScan(long connectionsRouted) throws FidesLane.Failure {
        mirror.fullScan();""",
        """    void mirrorClosingScan(long connectionsRouted) throws FidesLane.Failure {
        if (f4Live == null) mirror.fullScan();""")


def patch_capture(source: str) -> str:
    source = replace_once(
        source,
        '        System.out.println("F4_ITERATIVE_READY round="+iteration+" seq="+attempt.sequence+" "+attempt.name);\n',
    )
    source = replace_once(
        source,
        """                int parent = path.getInt(i + 1), child = path.getInt(i);
                require(0 <= child && child < rrg.N && seen.add(child), "GPU path has an invalid or repeated node");
                boolean found = false;
                for (int k = rrg.offset[parent]; k < rrg.offset[parent + 1]; ++k) {
                    if (rrg.edge[k] == child) {
                        found = true;
                        break;
                    }
                }
                require(found, "GPU path hop is outside the pinned RRG");
                for (RouteNode candidate : chain[i + 1].getChildren(graph)) {
""",
        """                int child = path.getInt(i);
                require(0 <= child && child < rrg.N && seen.add(child), "GPU path has an invalid or repeated node");
                for (RouteNode candidate : chain[i + 1].getChildren(graph)) {
""",
    )
    source = replace_once(
        source,
        """        for (int node : live) {
            require(gate.liveOccupancy(node) == mutableMirror.occupancy(node)
                    && gate.liveSameNetCount(node, connection.getNetWrapper()) == mutableMirror.sameNetCount(node)
                    && Float.floatToRawIntBits(gate.liveHistoricalCongestionCost(node))
                        == Float.floatToRawIntBits(mutableMirror.historicalCongestionCost(node)),
                    "Original batch commit mirror differs");
        }
""",
    )


    return replace_once(source,
        '        commits.put(new JSONObject().put("sequence", sequence).put("path", new JSONArray(live))',
        '        commits.put(new JSONObject().put("sequence", sequence)')


def patch_broker(source: str) -> str:
    source = replace_once(source,
        '            check(confirmed["members"] == [dict(sequence=row["sequence"], path=row["path"],',
        '            check(confirmed["members"] == [dict(sequence=row["sequence"],')
    return replace_once(source,
        '                                    channel=channel, f1_narrowed="corridor" in member,',
        '                                    channel=channel, cross_slr=attempt["crossSLR"], f1_narrowed="corridor" in member,')
