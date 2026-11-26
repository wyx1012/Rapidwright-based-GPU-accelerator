"""Patch the v5 adjacent-SLR planner without changing the GPU attempt format.

Set -Dfides.ppt.f4.crossSlrCompactX=true to try the compact rectangle.
The device still searches the current bounds and owns original-box retries.
"""


def patch_planner(source: str) -> str:
    anchor = "    private Candidate planCross(Connection connection"
    if source.count(anchor) != 1:
        raise ValueError("Expected one v5 adjacent-SLR planner")
    helper = """    private final boolean crossSlrCompactX = Boolean.getBoolean("fides.ppt.f4.crossSlrCompactX");

    private static Box compactCrossSlrX(Box planned, Portal portal, int sx, int tx) {
        int portalLeft = Math.min(portal.sllX, Math.min(portal.ingressX, portal.exitX));
        int portalRight = Math.max(portal.sllX, Math.max(portal.ingressX, portal.exitX));
        // Box bounds are open. Preserve the endpoint and portal columns, plus one coarse cell.
        int left = Math.max(planned.left, Math.min(Math.min(sx, tx), portalLeft) - CELL - 1);
        int right = Math.min(planned.right, Math.max(Math.max(sx, tx), portalRight) + CELL + 1);
        return left == planned.left && right == planned.right
                ? planned : new Box(left, planned.bottom, right, planned.top);
    }

"""
    source = source.replace(anchor, helper + anchor, 1)
    anchor = """        return new Candidate(base, tight, source, sink);
    }

    Box box(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph)"""
    if source.count(anchor) != 1:
        raise ValueError("Expected one v5 cross-SLR candidate return")
    replacement = """        if (crossSlrCompactX) tight = compactCrossSlrX(tight, best, sx, tx);
""" + anchor
    return source.replace(anchor, replacement, 1)
