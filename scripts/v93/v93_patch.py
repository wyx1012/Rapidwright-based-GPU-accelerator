"""v93 on v92 Java stack + pre-sized setPIPsOfNets HashSet & zero-iterator PIP add."""
import v92_patch as v92


def once(source, anchor, updated):
    return v92.once(source, anchor, updated)


def cuda(src):
    return v92.cuda(src)


def node(src):
    return v92.node(src)


def broker(src):
    return v92.broker(src)


def diversion(src):
    return v92.diversion(src)


def f2prewarm(src):
    return v92.f2prewarm(src)


def handoff(src):
    return v92.handoff(src)


def worker(src):
    return v92.worker(src)


def rrg_loader(src):
    return v92.rrg_loader(src)


def idmap(src):
    return v92.idmap(src)


def partial_router(src):
    return v92.partial_router(src)


def corridor(src):
    return v92.corridor(src)


def gate(src):
    return v92.gate(src)


def router(src):
    src = v92.router(src)
    src = once(
        src,
        """                    Set<PIP> newPIPs = new HashSet<>();
                    for (PIP pip : net.getPIPs()) {
                        if (pip.isPIPFixed()) {
                            newPIPs.add(pip);
                        }
                    }
                    for (Connection connection : netWrapper.getConnections()) {
                        List<Node> connNodes = connection.getNodes();
                        List<PIP> pips = RouterHelper.getConnectionPIPs(connection);
                        if (connNodes != null && connNodes.size() > 1 && pips.size() != connNodes.size() - 1) {
                            throw new IllegalStateException("V32 setPIPsOfNets Null PIP detected on net="
                                    + net.getName() + " expected=" + (connNodes.size() - 1) + " got=" + pips.size());
                        }
                        newPIPs.addAll(pips);
                    }""",
        """                    Set<PIP> newPIPs = new HashSet<>(Math.max(16, netWrapper.getConnections().size() * 4));
                    for (PIP pip : net.getPIPs()) {
                        if (pip.isPIPFixed()) {
                            newPIPs.add(pip);
                        }
                    }
                    for (Connection connection : netWrapper.getConnections()) {
                        List<Node> connNodes = connection.getNodes();
                        List<PIP> pips = RouterHelper.getConnectionPIPs(connection);
                        int pn = pips.size();
                        if (connNodes != null && connNodes.size() > 1 && pn != connNodes.size() - 1) {
                            throw new IllegalStateException("V32 setPIPsOfNets Null PIP detected on net="
                                    + net.getName() + " expected=" + (connNodes.size() - 1) + " got=" + pn);
                        }
                        for (int pi = 0; pi < pn; ++pi) {
                            newPIPs.add(pips.get(pi));
                        }
                    }""",
    )
    return src
