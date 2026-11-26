package com.xilinx.rapidwright.rwroute;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/** The coordinator records native mutations and owns the device before-image. */
final class F4HostJournal {
    private static final int HOT_SCOPES = 8;
    private final ArrayList<RouteNode> dirty = new ArrayList<>();
    private final IdentityHashMap<RouteNode, Boolean> deviceScope = new IdentityHashMap<>();
    private final LinkedHashMap<NetWrapper, Scope> hot = new LinkedHashMap<NetWrapper, Scope>(16, .75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<NetWrapper, Scope> eldest) {
            return size() > HOT_SCOPES;
        }
    };
    private final ByteBuffer buffer = ByteBuffer.allocate(28 * 1024).order(ByteOrder.LITTLE_ENDIAN);
    private long[] keys = new long[1024];
    private NetWrapper currentNet;
    private long dirtyEpoch = 1;
    private int[] occupancy;
    private int[] sameNet;
    private float[] history;
    private long marks;
    private long uniqueMarks;
    private long touchedRows;
    private long writtenRows;
    private long scopeChanges;
    private long scopesBuilt;
    private long scopeNodesWalked;
    private long sealNanos;

    static final class Scope {
        final IdentityHashMap<RouteNode, Boolean> members = new IdentityHashMap<>();
    }

    Scope hotScope(NetWrapper net) { return hot.get(net); }

    void noteUsage(RouteNode node, NetWrapper net, Scope scope) {
        mark(node);
        if (scope != null) {
            if (node.countConnectionsOfUser(net) == 0) scope.members.remove(node);
            else scope.members.put(node, Boolean.TRUE);
        }
    }

    void mark(RouteNode node) {
        ++marks;
        if (node.f4DirtyEpoch != dirtyEpoch) {
            node.f4DirtyEpoch = dirtyEpoch;
            dirty.add(node);
            ++uniqueMarks;
        }
    }

    void bind(int[] occupancy, int[] sameNet, float[] history) {
        this.occupancy = occupancy;
        this.sameNet = sameNet;
        this.history = history;
    }

    static final class Rows {
        int changed;
        int occupancy;
        int sameNet;
        int history;
    }

    private void addMember(Scope scope, NetWrapper net, RouteNode node) {
        if (node == null) return;
        ++scopeNodesWalked;
        if (!scope.members.containsKey(node) && node.countConnectionsOfUser(net) > 0)
            scope.members.put(node, Boolean.TRUE);
    }

    private Scope scopeFor(NetWrapper net) {
        Scope existing = hot.get(net);
        if (existing != null) return existing;
        Scope scope = new Scope();
        for (Connection connection : net.getConnections()) {
            if (connection.isDirect()) continue;
            if (!connection.hasAltSinks()) addMember(scope, net, connection.getSinkRnode());
            if (connection.isRouted())
                for (RouteNode node : connection.getRnodes()) addMember(scope, net, node);
        }
        hot.put(net, scope);
        ++scopesBuilt;
        return scope;
    }

    Rows seal(NetWrapper net, FidesRrgIdMap idMap, OutputStream output) throws IOException {
        long started = System.nanoTime();
        if (currentNet != net) {
            deviceScope.keySet().forEach(this::mark);
            deviceScope.clear();
            scopeFor(net).members.keySet().forEach(this::mark);
            currentNet = net;
            ++scopeChanges;
        }
        Scope activeScope = hot.get(net);
        int count = dirty.size();
        if (count > keys.length) keys = new long[Math.max(count, keys.length * 2)];
        for (int index = 0; index < count; ++index) {
            RouteNode node = dirty.get(index);
            int id = node.f4JournalId;
            if (id < 0) {
                id = idMap.idOf(node);
                if (id < 0) throw new IllegalStateException("F4 journal node outside the RRG: " + node);
                node.f4JournalId = id;
            }
            keys[index] = ((long) id << 32) | index;
        }
        Arrays.sort(keys, 0, count);
        Rows result = new Rows();
        int previous = -1;
        for (int index = 0; index < count; ++index) {
            long key = keys[index];
            int id = (int) (key >>> 32);
            RouteNode node = dirty.get((int) key);
            if (id == previous) throw new IllegalStateException("Duplicate live RRG id " + id);
            previous = id;
            int occ = node.getOccupancy();
            int same = node.countConnectionsOfUser(net);
            float hist = node.getHistoricalCongestionCost();
            int oldHist = Float.floatToRawIntBits(history[id]);
            int newHist = Float.floatToRawIntBits(hist);
            if (same == 0) {
                deviceScope.remove(node);
                activeScope.members.remove(node);
            } else {
                deviceScope.put(node, Boolean.TRUE);
                activeScope.members.put(node, Boolean.TRUE);
            }
            boolean occChanged = occ != occupancy[id];
            boolean sameChanged = same != sameNet[id];
            boolean histChanged = oldHist != newHist;
            if (!(occChanged || sameChanged || histChanged)) continue;
            if (output != null) {
                if (buffer.remaining() < 28) flush(output);
                buffer.putInt(id).putInt(occupancy[id]).putInt(sameNet[id]).putInt(oldHist)
                      .putInt(occ).putInt(same).putInt(newHist);
            }
            occupancy[id] = occ;
            sameNet[id] = same;
            history[id] = hist;
            ++result.changed;
            if (occChanged) ++result.occupancy;
            if (sameChanged) ++result.sameNet;
            if (histChanged) ++result.history;
        }
        if (output != null) flush(output);
        dirty.clear();
        ++dirtyEpoch;
        touchedRows += count;
        if (output != null) writtenRows += result.changed;
        sealNanos += System.nanoTime() - started;
        return result;
    }

    private void flush(OutputStream output) throws IOException {
        output.write(buffer.array(), 0, buffer.position());
        buffer.clear();
    }

    org.json.JSONObject receipt() {
        return new org.json.JSONObject().put("marks", marks).put("unique_marks", uniqueMarks)
                .put("rows_examined", touchedRows).put("rows_sent", writtenRows)
                .put("gpu_scope_changes", scopeChanges).put("scopes_built", scopesBuilt)
                .put("scope_nodes_walked", scopeNodesWalked).put("seal_ns", sealNanos);
    }
}
