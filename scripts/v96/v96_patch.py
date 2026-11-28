"""v96 on v95/v94 Java stack + LUT Pin Swapping (altSinks Goal Set + lazy post-route LUTTools swap)."""
import sys
from pathlib import Path

_V95_DIR = str(Path(__file__).resolve().parent.parent / "v95")
if _V95_DIR not in sys.path:
    sys.path.insert(0, _V95_DIR)

import v95_patch as v95  # noqa: E402


def once(source, anchor, updated):
    if source.count(anchor) != 1:
        raise RuntimeError(f"v96_patch anchor missing/ambiguous: {anchor.splitlines()[0][:80]}")
    return source.replace(anchor, updated, 1)


def cuda(src):
    return v95.cuda(src)


def node(src):
    return v95.node(src)


def broker(src):
    return v95.broker(src)


def diversion(src):
    return v95.diversion(src)


def f2prewarm(src):
    return v95.f2prewarm(src)


def handoff(src):
    return v95.handoff(src)


def worker(src):
    return v95.worker(src)


def partial_router(src):
    src = v95.partial_router(src)
    src = once(
        src,
        """    @Override
    protected void addNetConnectionToRoutingTargets(Net net) {
        List<SitePinInst> pinsToRoute = netToPins.get(net);
        if (pinsToRoute != null) {""",
        """    @Override
    protected void preserveNetEarlyForPinSwap(Net net) {
        if (net.hasPIPs()) {
            preserveNet(net, true);
            numPreservedWire++;
            numPreservedRoutableNets++;
        }
    }

    @Override
    protected boolean needsDeferredRoutingTargetForPinSwap(Net net) {
        return netToPins.get(net) != null;
    }

    @Override
    protected void addDeferredNetConnectionToRoutingTargets(Net net) {
        addNetConnectionToRoutingTargetsBody(net, false);
    }

    @Override
    protected void addNetConnectionToRoutingTargets(Net net) {
        addNetConnectionToRoutingTargetsBody(net, true);
    }

    private void addNetConnectionToRoutingTargetsBody(Net net, boolean doPreserve) {
        List<SitePinInst> pinsToRoute = netToPins.get(net);
        if (pinsToRoute != null) {""",
    )
    src = once(
        src,
        """        if (net.hasPIPs()) {
            preserveNet(net, true);
            numPreservedWire++;
            numPreservedRoutableNets++;
        }
    }

    protected boolean isExcludedPip(Node start, Node end) {""",
        """        if (doPreserve && net.hasPIPs()) {
            preserveNet(net, true);
            numPreservedWire++;
            numPreservedRoutableNets++;
        }
    }

    protected boolean isExcludedPip(Node start, Node end) {""",
    )
    return src


def corridor(src):
    return v95.corridor(src)


def rrg_loader(src):
    return v95.rrg_loader(src)


def idmap(src):
    return v95.idmap(src)


def router(src):
    src = v95.router(src)
    src = once(
        src,
        """        for (Connection connection : indirectConnections) {
            connection.getAltSinkRnodes().removeIf(RouteNode::isUsed);
        }

        // Wait for all outstanding RouteNodeGraph.preserveAsync() calls to complete
        routingGraph.awaitPreserve();
    }

    protected Set<Net> ensureSinkRoutability() {
        // RWRoute routes designs from scratch -- all sinks must be reachable
        return Collections.emptySet();
    }

    private void categorizeNets() {
        numWireNetsToRoute = 0;
        numConnectionsToRoute = 0;
        numPreservedRoutableNets = 0;
        numNotNeedingRoutingNets = 0;
        numUnrecognizedNets = 0;

        nets = new IdentityHashMap<>();
        indirectConnections = new ArrayList<>();
        directConnections = new ArrayList<>();
        clkNets = new ArrayList<>();
        staticNetAndRoutingTargets = new HashMap<>();

        for (Net net : design.getNets()) {
            if (NetTools.isGlobalClock(net)) {
                addGlobalClkRoutingTargets(net);

            } else if (net.isStaticNet()) {
                addStaticNetRoutingTargets(net);

            } else if (net.getType().equals(NetType.WIRE)) {
                if (RouterHelper.isDriverLessOrLoadLessNet(net) ||
                        RouterHelper.isInternallyRoutedNet(net) ||
                        net.getName().equals(Net.Z_NET)) {
                    preserveNet(net, true);
                    numNotNeedingRoutingNets++;
                } else if (RouterHelper.isRoutableNetWithSourceSinks(net)) {
                    addNetConnectionToRoutingTargets(net);
                } else {
                    numNotNeedingRoutingNets++;
                }
            } else {
                numUnrecognizedNets++;
                System.err.println("ERROR: Unknown net " + net);
            }
        }
    }""",
        """        for (Connection connection : indirectConnections) {
            connection.getAltSinkRnodes().removeIf(RouteNode::isUsed);
        }
        for (Connection connection : indirectConnections) {
            if (!connection.hasAltSinks()) {
                RouteNode sinkRnode = connection.getSinkRnode();
                RouteNodeType sinkType = sinkRnode.getType();
                if (!sinkType.isAnyExclusiveSink()) {
                    RouteNodeType exclType = sinkType.isEastLocal() ? RouteNodeType.EXCLUSIVE_SINK_EAST :
                                             sinkType.isWestLocal() ? RouteNodeType.EXCLUSIVE_SINK_WEST :
                                             sinkType == RouteNodeType.LOCAL_BOTH ? RouteNodeType.EXCLUSIVE_SINK_BOTH :
                                             null;
                    assert(exclType != null);
                    sinkRnode.setType(exclType);
                    sinkRnode.incrementUser(connection.getNetWrapper());
                }
            }
        }

        // Wait for all outstanding RouteNodeGraph.preserveAsync() calls to complete
        routingGraph.awaitPreserve();
    }

    protected Set<Net> ensureSinkRoutability() {
        // RWRoute routes designs from scratch -- all sinks must be reachable
        return Collections.emptySet();
    }

    protected void preserveNetEarlyForPinSwap(Net net) {
    }

    protected boolean needsDeferredRoutingTargetForPinSwap(Net net) {
        return true;
    }

    protected void addDeferredNetConnectionToRoutingTargets(Net net) {
        addNetConnectionToRoutingTargets(net);
    }

    private void categorizeNets() {
        numWireNetsToRoute = 0;
        numConnectionsToRoute = 0;
        numPreservedRoutableNets = 0;
        numNotNeedingRoutingNets = 0;
        numUnrecognizedNets = 0;

        nets = new IdentityHashMap<>();
        indirectConnections = new ArrayList<>();
        directConnections = new ArrayList<>();
        clkNets = new ArrayList<>();
        staticNetAndRoutingTargets = new HashMap<>();

        List<Net> deferredWireNets = lutPinSwapping ? new ArrayList<>() : null;
        for (Net net : design.getNets()) {
            if (NetTools.isGlobalClock(net)) {
                addGlobalClkRoutingTargets(net);

            } else if (net.isStaticNet()) {
                addStaticNetRoutingTargets(net);

            } else if (net.getType().equals(NetType.WIRE)) {
                if (RouterHelper.isDriverLessOrLoadLessNet(net) ||
                        RouterHelper.isInternallyRoutedNet(net) ||
                        net.getName().equals(Net.Z_NET)) {
                    preserveNet(net, true);
                    numNotNeedingRoutingNets++;
                } else if (RouterHelper.isRoutableNetWithSourceSinks(net)) {
                    if (deferredWireNets != null) {
                        preserveNetEarlyForPinSwap(net);
                        if (needsDeferredRoutingTargetForPinSwap(net)) {
                            deferredWireNets.add(net);
                        }
                    } else {
                        addNetConnectionToRoutingTargets(net);
                    }
                } else {
                    numNotNeedingRoutingNets++;
                }
            } else {
                numUnrecognizedNets++;
                System.err.println("ERROR: Unknown net " + net);
            }
        }
        if (deferredWireNets != null) {
            routingGraph.awaitPreserve();
            for (Net net : deferredWireNets) {
                addDeferredNetConnectionToRoutingTargets(net);
            }
        }
    }""",
    )
    return src


def gate(src):
    src = v95.gate(src)
    src = once(
        src,
        """        final byte[] flags = new byte[n];
        final byte[] corrMode = new byte[n];
        final boolean useOrigBox = cfg.isUseBoundingBox();
        final boolean uturn = cfg.isUseUTurnNodes();
        final int cChunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        var connJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>((n + cChunk - 1) / cChunk);
        for (int from = 0; from < n; from += cChunk) {
            final int f = from, t = Math.min(n, from + cChunk);
            connJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    Connection c = all.get(i);
                    NetWrapper nw = uniqueNetsInit[netId[i]];
                    RouteNode src = c.getSourceRnode();
                    RouteNode sink = c.getSinkRnode();
                    int sId = f4FastId(src);
                    int tId = f4FastId(sink);
                    RouteNodeType sType = sink != null ? sink.getType() : null;
                    int dHpwl = nw.getDoubleHpwl();
                    boolean shapeOk = !c.hasAltSinks() && src != sink && sId >= 0 && tId >= 0
                            && sType != null && sType.isAnyExclusiveSink() && dHpwl > 0;
                    byte fl = 0;
                    if (shapeOk) fl |= 1;
                    if (useOrigBox) fl |= 2;
                    if (uturn) fl |= 4;
                    if (c.isCrossSLR()) fl |= 8;
                    if (c.isCrossSLRnorth()) fl |= 16;
                    if (c.isCrossSLRsouth()) fl |= 32;
                    if (sType == RouteNodeType.EXCLUSIVE_SINK_BOTH) fl |= 64;""",
        """        final byte[] flags = new byte[n];
        final byte[] corrMode = new byte[n];
        final int[][] altPerConn = new int[n][];
        final boolean useOrigBox = cfg.isUseBoundingBox();
        final boolean uturn = cfg.isUseUTurnNodes();
        final int cChunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        var connJobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>((n + cChunk - 1) / cChunk);
        for (int from = 0; from < n; from += cChunk) {
            final int f = from, t = Math.min(n, from + cChunk);
            connJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    Connection c = all.get(i);
                    NetWrapper nw = uniqueNetsInit[netId[i]];
                    RouteNode src = c.getSourceRnode();
                    RouteNode sink = c.getSinkRnode();
                    int sId = f4FastId(src);
                    int tId = f4FastId(sink);
                    RouteNodeType sType = sink != null ? sink.getType() : null;
                    int dHpwl = nw.getDoubleHpwl();
                    boolean hasAlts = c.hasAltSinks();
                    int[] alts = null;
                    boolean altsOk = true;
                    if (hasAlts) {
                        List<RouteNode> altList = c.getAltSinkRnodes();
                        int aCnt = altList.size();
                        if (aCnt <= 0 || aCnt > 5 || sType == null || sType.isAnyExclusiveSink()
                                || !(sType == RouteNodeType.LOCAL_EAST || sType == RouteNodeType.LOCAL_WEST
                                     || sType == RouteNodeType.LOCAL_BOTH)) {
                            altsOk = false;
                        } else {
                            alts = new int[aCnt];
                            for (int ai = 0; ai < aCnt; ++ai) {
                                RouteNode ar = altList.get(ai);
                                int aid = f4FastId(ar);
                                if (aid < 0 || ar == src || ar == sink) {
                                    altsOk = false;
                                    break;
                                }
                                alts[ai] = aid;
                            }
                        }
                    }
                    altPerConn[i] = (hasAlts && altsOk) ? alts : null;
                    boolean shapeOk = altsOk && src != sink && sId >= 0 && tId >= 0
                            && sType != null
                            && ((!hasAlts && sType.isAnyExclusiveSink())
                                || (hasAlts && (sType == RouteNodeType.LOCAL_EAST
                                                || sType == RouteNodeType.LOCAL_WEST
                                                || sType == RouteNodeType.LOCAL_BOTH)))
                            && dHpwl > 0;
                    byte fl = 0;
                    if (shapeOk) fl |= 1;
                    if (useOrigBox) fl |= 2;
                    if (uturn) fl |= 4;
                    if (c.isCrossSLR()) fl |= 8;
                    if (c.isCrossSLRnorth()) fl |= 16;
                    if (c.isCrossSLRsouth()) fl |= 32;
                    if (sType == RouteNodeType.EXCLUSIVE_SINK_BOTH || sType == RouteNodeType.LOCAL_BOTH) fl |= 64;
                    if (hasAlts && altsOk) fl |= (byte) 128;""",
    )
    src = once(
        src,
        """        FidesNativeCpu.nativeRegisterConnections(
                netId, srcId, sinkId, ox0, oy0, ox1, oy1,
                sbx, sby, stile, stype, nxc, nyc, nsize, dhpwl, flags);""",
        """        FidesNativeCpu.nativeRegisterConnections(
                netId, srcId, sinkId, ox0, oy0, ox1, oy1,
                sbx, sby, stile, stype, nxc, nyc, nsize, dhpwl, flags);
        int[] altOffsets = new int[n + 1];
        int totalAlts = 0;
        for (int i = 0; i < n; ++i) {
            altOffsets[i] = totalAlts;
            if (altPerConn[i] != null) totalAlts += altPerConn[i].length;
        }
        altOffsets[n] = totalAlts;
        int[] altIds = new int[totalAlts];
        if (totalAlts > 0) {
            int pos = 0;
            for (int i = 0; i < n; ++i) {
                int[] a = altPerConn[i];
                if (a != null) {
                    System.arraycopy(a, 0, altIds, pos, a.length);
                    pos += a.length;
                }
            }
        }
        FidesNativeCpu.nativeRegisterAltSinks(altOffsets, altIds);""",
    )
    src = once(
        src,
        """            Connection c = all.get(i);
            int len = path.length;
            RouteNode[] chain = new RouteNode[len];
            chain[0] = c.getSinkRnode();
            chain[len - 1] = c.getSourceRnode();""",
        """            Connection c = all.get(i);
            int len = path.length;
            RouteNode[] chain = new RouteNode[len];
            RouteNode actualSink = c.getSinkRnode();
            if (c.hasAltSinks() && f4FastId(actualSink) != path[0]) {
                RouteNode foundSink = null;
                for (RouteNode cand : c.getAltSinkRnodes()) {
                    if (f4FastId(cand) == path[0]) { foundSink = cand; break; }
                }
                if (foundSink == null) {
                    foundSink = (path[0] >= 0 && path[0] < nodeCache.length && nodeCache[path[0]] != null)
                            ? nodeCache[path[0]] : f4FastNodeAt(path[0]);
                }
                if (foundSink == null) {
                    throw new IllegalStateException("V96 verify missing altSink id=" + path[0] + " conn=" + i);
                }
                actualSink = foundSink;
            }
            chain[0] = actualSink;
            chain[len - 1] = c.getSourceRnode();""",
    )
    src = once(
        src,
        """                    int len = path.length;
                    RouteNode sinkRnode = c.getSinkRnode();
                    RouteNode srcRnode = c.getSourceRnode();""",
        """                    int len = path.length;
                    RouteNode sinkRnode = c.getSinkRnode();
                    if (c.hasAltSinks() && f4FastId(sinkRnode) != path[0]) {
                        RouteNode foundSink = null;
                        for (RouteNode cand : c.getAltSinkRnodes()) {
                            if (f4FastId(cand) == path[0]) { foundSink = cand; break; }
                        }
                        if (foundSink == null) {
                            foundSink = (path[0] >= 0 && path[0] < nodeCache.length && nodeCache[path[0]] != null)
                                    ? nodeCache[path[0]] : f4FastNodeAt(path[0]);
                        }
                        if (foundSink == null) {
                            throw new IllegalStateException("V96 materialize missing altSink id=" + path[0] + " conn=" + i);
                        }
                        sinkRnode = foundSink;
                    }
                    RouteNode srcRnode = c.getSourceRnode();""",
    )
    return src
