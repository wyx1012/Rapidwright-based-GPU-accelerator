package com.xilinx.rapidwright.rwroute;

import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.TileTypeEnum;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.Map;
import java.util.PriorityQueue;

final class F1Corridor {
    static final int CELL = 4;
    private static final int WINDOW = 32;

    static final class Box {
        final int left;
        final int bottom;
        final int right;
        final int top;

        Box(int left, int bottom, int right, int top) {
            this.left = left;
            this.bottom = bottom;
            this.right = right;
            this.top = top;
        }

        boolean same(Box other) {
            return left == other.left && bottom == other.bottom && right == other.right && top == other.top;
        }

        static Box original(Connection c) {
            return new Box(c.getXMinBB(), c.getYMinBB(), c.getXMaxBB(), c.getYMaxBB());
        }

        boolean contains(int x, int y) {
            return left < x && x < right && bottom < y && y < top;
        }

        boolean contains(RouteNode node) {
            return contains(node.getEndTileXCoordinate(), node.getEndTileYCoordinate());
        }

        long area() {
            return (long) (right - left - 1) * (top - bottom - 1);
        }

        String fields() {
            return left + "\t" + bottom + "\t" + right + "\t" + top;
        }
    }

    static final class Candidate {
        final Box base;
        final Box tight;
        final RouteNode source;
        final RouteNode sink;

        Candidate(Box base, Box tight, RouteNode source, RouteNode sink) {
            this.base = base;
            this.tight = tight;
            this.source = source;
            this.sink = sink;
        }

        Box tight() { return tight; }

        boolean matches(Connection connection) {
            return source == connection.getSourceRnode() && sink == connection.getSinkRnode()
                    && base.same(Box.original(connection)) && !connection.isCrossSLR() && !connection.hasAltSinks();
        }
    }

    static final class Choice {
        final Box box;
        final int cost;

        Choice(Box box, int cost) {
            this.box = box;
            this.cost = cost;
        }
    }
    private final Box fabric;
    private final Map<Long, Integer> occupancy = new HashMap<>();
    private static final int CAND_SHARDS = 64;
    @SuppressWarnings("unchecked")
    private final Map<Connection, Candidate>[] shardCand = createCandShards();

    private static Map<Connection, Candidate>[] createCandShards() {
        @SuppressWarnings("unchecked")
        Map<Connection, Candidate>[] arr = new Map[CAND_SHARDS];
        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 17);
        return arr;
    }

    private static int candShard(Connection c) {
        return (System.identityHashCode(c) >>> 4) & (CAND_SHARDS - 1);
    }

    private Map<Connection, Candidate> candMap(Connection c) {
        return shardCand[candShard(c)];
    }

    private static long cellKey(int x, int y) {
        return ((long) x << 32) | Integer.toUnsignedLong(y);
    }

    private static boolean isInt(Tile tile) {
        return tile != null && tile.getTileTypeEnum() == TileTypeEnum.INT;
    }

    F1Corridor(Design design) {
        int left = Integer.MAX_VALUE, bottom = Integer.MAX_VALUE;
        int right = Integer.MIN_VALUE, top = Integer.MIN_VALUE;
        for (Tile tile : design.getDevice().getAllTiles()) {
            if (!isInt(tile)) continue;
            if (crossSlrCorridor) {
                int y = tile.getTileYCoordinate();
                int[] rows = slrRows.computeIfAbsent(tile.getSLR().getId(), key -> new int[]{y, y});
                rows[0] = Math.min(rows[0], y);
                rows[1] = Math.max(rows[1], y);
            }
            left = Math.min(left, tile.getTileXCoordinate());
            bottom = Math.min(bottom, tile.getTileYCoordinate());
            right = Math.max(right, tile.getTileXCoordinate());
            top = Math.max(top, tile.getTileYCoordinate());
        }
        if (left < 0 || bottom < 0 || left > right || bottom > top) throw new IllegalStateException("F1 INT fabric");
        fabric = new Box(left - 1, bottom - 1, right + 1, top + 1);
        var used = new HashSet<Node>();
        for (var net : design.getNets()) {
            for (var pip : net.getPIPs()) {
                Node start = pip.getStartNode(), end = pip.getEndNode();
                if (start != null && isInt(start.getTile())) used.add(start);
                if (end != null && isInt(end.getTile())) used.add(end);
            }
        }
        for (Node node : used) {
            Tile tile = node.getTile();
            long cell = cellKey(tile.getTileXCoordinate() / CELL, tile.getTileYCoordinate() / CELL);
            occupancy.merge(cell, 1, Integer::sum);
        }
        freezeCoarseOccupancy();
    }

    private Candidate plan(Connection connection) {
        RouteNode source = connection.getSourceRnode();
        RouteNode sink = connection.getSinkRnode();
        if (connection.isCrossSLR() || connection.hasAltSinks() || connection.getHpwl() < 16
                || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) {
            return null;
        }

        int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
        int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
        Box base = Box.original(connection);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) {
            return null;
        }
        Choice choice = choose(base, sx, sy, tx, ty);
        if (choice == null || choice.box.same(base)) return null;
        return new Candidate(base, choice.box, source, sink);
    }

    private static final class CoarseScratch {
        private int[] tags = new int[0], distances = new int[0], previous = new int[0];
        private int[] heap = new int[0], heapPositions = new int[0];
        private int generation, size;

        void begin(int count) {
            if (count > tags.length) {
                int capacity = Math.max(count, Math.max(128, 2 * tags.length));
                tags = new int[capacity];
                distances = new int[capacity];
                previous = new int[capacity];
                heap = new int[capacity];
                heapPositions = new int[capacity];
            }
            if (++generation == 0) {
                Arrays.fill(tags, 0);
                generation = 1;
            }
            size = 0;
        }

        int distance(int cell) {
            return tags[cell] == generation ? distances[cell] : Integer.MAX_VALUE;
        }

        private boolean less(int a, int b) {
            int ca = distances[a], cb = distances[b];
            return ca < cb || (ca == cb && a < b);
        }

        void decrease(int cell, int cost, int from) {
            int index;
            if (tags[cell] != generation) {
                tags[cell] = generation;
                index = size++;
            } else {
                index = heapPositions[cell];
            }
            distances[cell] = cost;
            previous[cell] = from;
            while (index > 0) {
                int parent = (index - 1) >>> 1;
                int above = heap[parent];
                if (!less(cell, above)) break;
                heap[index] = above;
                heapPositions[above] = index;
                index = parent;
            }
            heap[index] = cell;
            heapPositions[cell] = index;
        }

        int removeFirst() {
            int result = heap[0];
            heapPositions[result] = -1;
            int last = heap[--size];
            if (size == 0) return result;
            int index = 0;
            for (int child = 1; child < size; child = 2 * index + 1) {
                if (child + 1 < size && less(heap[child + 1], heap[child])) ++child;
                if (!less(heap[child], last)) break;
                heap[index] = heap[child];
                heapPositions[heap[index]] = index;
                index = child;
            }
            heap[index] = last;
            heapPositions[last] = index;
            return result;
        }

        void relax(int from, int next, int cost, int occupancy) {
            int improved = cost + 1 + occupancy;
            if (improved < distance(next)) decrease(next, improved, from);
        }
    }

    private final ThreadLocal<CoarseScratch> scratch = ThreadLocal.withInitial(CoarseScratch::new);
    private int coarseBottom, coarseLeft, coarseHeight;
    private int[] coarseOccupancy;

    private void freezeCoarseOccupancy() {
        coarseLeft = (fabric.left + 1) / CELL;
        coarseBottom = (fabric.bottom + 1) / CELL;
        int width = (fabric.right - 1) / CELL - coarseLeft + 1;
        coarseHeight = (fabric.top - 1) / CELL - coarseBottom + 1;
        coarseOccupancy = new int[width * coarseHeight];
        for (var entry : occupancy.entrySet()) {
            long key = entry.getKey();
            int x = (int) (key >>> 32), y = (int) key;
            coarseOccupancy[(x - coarseLeft) * coarseHeight + y - coarseBottom] = entry.getValue();
        }
    }

    private int weight(int x, int y) {
        return coarseOccupancy[(x - coarseLeft) * coarseHeight + y - coarseBottom];
    }

    private Choice choose(Box base, int sx, int sy, int tx, int ty) {
        int ax = sx / CELL, ay = sy / CELL, zx = tx / CELL, zy = ty / CELL;
        int left = Math.max(Math.floorDiv(Math.max(base.left, fabric.left) + 1, CELL), Math.min(ax, zx) - WINDOW);
        int bottom = Math.max(Math.floorDiv(Math.max(base.bottom, fabric.bottom) + 1, CELL), Math.min(ay, zy) - WINDOW);
        int right = Math.min(Math.floorDiv(Math.min(base.right, fabric.right) - 1, CELL), Math.max(ax, zx) + WINDOW);
        int top = Math.min(Math.floorDiv(Math.min(base.top, fabric.top) - 1, CELL), Math.max(ay, zy) + WINDOW);
        int w = right - left + 1, h = top - bottom + 1;
        if (w <= 0 || h <= 0 || (long) w * h > 200000) throw new IllegalStateException("F1 coarse window");
        int start = (ax - left) * h + ay - bottom, end = (zx - left) * h + zy - bottom;
        CoarseScratch queue = scratch.get();
        queue.begin(w * h);
        queue.decrease(start, 0, -1);
        while (queue.size != 0) {
            int at = queue.removeFirst();
            if (at == end) break;
            int x = at / h, y = at % h, cost = queue.distances[at];
            int col = left + x, row = bottom + y;
            // Same neighbor order and cell-ID tie break as the coarse Dijkstra.
            if (x != 0) queue.relax(at, at - h, cost, weight(col - 1, row));
            if (y != 0) queue.relax(at, at - 1, cost, weight(col, row - 1));
            if (y + 1 != h) queue.relax(at, at + 1, cost, weight(col, row + 1));
            if (x + 1 != w) queue.relax(at, at + h, cost, weight(col + 1, row));
        }
        if (queue.distance(end) == Integer.MAX_VALUE) return null;
        int x0 = zx, y0 = zy, x1 = zx, y1 = zy;
        for (int at = end;; at = queue.previous[at]) {
            if (at < 0) throw new IllegalStateException("F1 coarse backtrack");
            int x = left + at / h, y = bottom + at % h;
            x0 = Math.min(x0, x); y0 = Math.min(y0, y);
            x1 = Math.max(x1, x); y1 = Math.max(y1, y);
            if (at == start) break;
        }
        Box tight = new Box(Math.max(base.left, (x0 - 1) * CELL - 1),
                Math.max(base.bottom, (y0 - 1) * CELL - 1),
                Math.min(base.right, (x1 + 2) * CELL), Math.min(base.top, (y1 + 2) * CELL));
        if (!tight.contains(sx, sy) || !tight.contains(tx, ty) || tight.area() > base.area()) {
            throw new IllegalStateException("F1 candidate box");
        }
        return new Choice(tight, queue.distance(end));
    }

    Box box(Connection connection) {
        RouteNode source = connection.getSourceRnode();
        RouteNode sink = connection.getSinkRnode();
        if (source == null || sink == null || connection.isCrossSLR() || connection.hasAltSinks()
                || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) return null;

        if (connection.getHpwl() >= 16) {
            Candidate candidate = get(connection);
            return candidate == null ? null : candidate.tight();
        }
        // A failed short box is retired like a failed weighted corridor.
        if (candMap(connection).containsKey(connection)) return null;
        int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
        int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
        if (!fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        Box base = Box.original(connection);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)) return null;
        // Box.contains uses strict bounds; include the source and sink tiles.
        Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 4),
                Math.max(base.bottom, Math.min(sy, ty) - 4),
                Math.min(base.right, Math.max(sx, tx) + 4),
                Math.min(base.top, Math.max(sy, ty) + 4));
        return shortBox.same(base) ? null : shortBox;
    }


    // A portal records real CSR arcs ingress -> SLL -> exit. Its geometry
    // does not pre-authorize those arcs on the current congestion snapshot.
    private static final class Portal {
        final int ingressX, ingressY, sllX, sllRawY, exitX, exitY;

        Portal(FidesRrgLoader rrg, int ingress, int sll, int exit) {
            ingressX = rrg.endTileX[ingress];
            ingressY = rrg.endTileYRaw[ingress];
            sllX = rrg.endTileX[sll];
            sllRawY = rrg.endTileYRaw[sll];
            exitX = rrg.endTileX[exit];
            exitY = rrg.endTileYRaw[exit];
        }

        boolean inside(Box box) {
            return box.contains(ingressX, ingressY) && box.contains(sllX, sllRawY)
                    && box.contains(exitX, exitY);
        }

        int detour(int sx, int sy, int tx, int ty) {
            return Math.abs(sx - ingressX) + Math.abs(sy - ingressY)
                    + Math.abs(tx - exitX) + Math.abs(ty - exitY);
        }
    }

    private final Map<Integer, int[]> slrRows = new HashMap<>();
    private final Map<Long, java.util.List<Portal>> portals = new HashMap<>();
    private boolean portalsIndexed;
    private final boolean crossSlrCorridor = Boolean.getBoolean("fides.ppt.f4.crossSlrCorridor");

    private int rawSlr(RouteNodeGraph graph, int y) {
        if (y < 0 || y >= graph.intYToSLRIndex.length) return -1;
        int slr = graph.intYToSLRIndex[y];
        int[] rows = slrRows.get(slr);
        return rows != null && y >= rows[0] && y <= rows[1] ? slr : -1;
    }

    private void indexPortals(FidesRrgLoader rrg, RouteNodeGraph graph) {
        long t0 = System.nanoTime();
        final int longLine = RouteNodeType.SUPER_LONG_LINE.ordinal();
        final int nonlocal = RouteNodeType.NON_LOCAL.ordinal();
        TileTypeEnum[] tileTypes = TileTypeEnum.values();
        final boolean[] laguna = new boolean[tileTypes.length];
        for (int i = 0; i < tileTypes.length; ++i) {
            laguna[i] = com.xilinx.rapidwright.util.Utils.isLaguna(tileTypes[i]);
        }
        int maxSlr = -1;
        for (Integer slr : slrRows.keySet()) {
            if (slr != null && slr > maxSlr) maxSlr = slr;
        }
        final int slrCap = maxSlr + 1;
        final boolean[] slrHas = new boolean[Math.max(1, slrCap)];
        final int[] slrMin = new int[Math.max(1, slrCap)];
        final int[] slrMax = new int[Math.max(1, slrCap)];
        for (Map.Entry<Integer, int[]> e : slrRows.entrySet()) {
            int s = e.getKey();
            if (s >= 0 && s < slrCap) {
                slrHas[s] = true;
                slrMin[s] = e.getValue()[0];
                slrMax[s] = e.getValue()[1];
            }
        }
        final int[] yToSlr = graph.intYToSLRIndex;
        final int yLen = yToSlr.length;
        final int N = rrg.N;
        final int chunk = Math.max(65536, 1 << 20);
        final int numChunks = Math.max(1, (N + chunk - 1) / chunk);
        final long[][] chunkDirs = new long[numChunks][];
        final long[][] chunkCells = new long[numChunks][];
        final Portal[][] chunkPortals = new Portal[numChunks][];
        final int[] chunkCounts = new int[numChunks];

        var jobs = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numChunks);
        for (int ci = 0; ci < numChunks; ++ci) {
            final int cIdx = ci;
            final int fromP = ci * chunk;
            final int toP = Math.min(N, fromP + chunk);
            jobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<Long, java.util.Set<Long>> localCells = new HashMap<>();
                int cap = 64, sz = 0;
                long[] dirs = new long[cap];
                long[] cells = new long[cap];
                Portal[] ps = new Portal[cap];
                for (int parent = fromP; parent < toP; ++parent) {
                    if (rrg.nodeType[parent] != nonlocal || !laguna[rrg.tileType[parent]]) continue;
                    int py = rrg.endTileYRaw[parent];
                    int from = (py >= 0 && py < yLen) ? yToSlr[py] : -1;
                    if (from < 0 || from >= slrCap || !slrHas[from] || py < slrMin[from] || py > slrMax[from]) continue;
                    for (int edge = rrg.offset[parent]; edge < rrg.offset[parent + 1]; ++edge) {
                        int sll = rrg.edge[edge];
                        if (rrg.nodeType[sll] != longLine) continue;
                        int sy = py == rrg.endTileYRaw[sll] ? rrg.tileY[sll] : rrg.endTileYRaw[sll];
                        int to = (sy >= 0 && sy < yLen) ? yToSlr[sy] : -1;
                        if (to < 0 || to >= slrCap || !slrHas[to] || sy < slrMin[to] || sy > slrMax[to] || Math.abs(from - to) != 1) continue;
                        for (int out = rrg.offset[sll]; out < rrg.offset[sll + 1]; ++out) {
                            int child = rrg.edge[out];
                            if (rrg.nodeType[child] != nonlocal || !laguna[rrg.tileType[child]]
                                    || rrg.tileId[parent] == rrg.tileId[child]) continue;
                            int cy = rrg.endTileYRaw[child];
                            int cSlr = (cy >= 0 && cy < yLen) ? yToSlr[cy] : -1;
                            if (cSlr != to || cy < slrMin[to] || cy > slrMax[to]) continue;
                            long direction = cellKey(from, to);
                            int column = rrg.endTileX[sll];
                            int rows = ((py / CELL) << 16) | (cy / CELL);
                            long cell = cellKey(column, rows);
                            if (localCells.computeIfAbsent(direction, key -> new HashSet<>()).add(cell)) {
                                if (sz == cap) {
                                    cap *= 2;
                                    dirs = java.util.Arrays.copyOf(dirs, cap);
                                    cells = java.util.Arrays.copyOf(cells, cap);
                                    ps = java.util.Arrays.copyOf(ps, cap);
                                }
                                dirs[sz] = direction;
                                cells[sz] = cell;
                                ps[sz] = new Portal(rrg, parent, sll, child);
                                ++sz;
                            }
                        }
                    }
                }
                chunkDirs[cIdx] = dirs;
                chunkCells[cIdx] = cells;
                chunkPortals[cIdx] = ps;
                chunkCounts[cIdx] = sz;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(jobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        Map<Long, java.util.Set<Long>> portalCells = new HashMap<>();
        int totalPortals = 0;
        for (int ci = 0; ci < numChunks; ++ci) {
            int sz = chunkCounts[ci];
            long[] dirs = chunkDirs[ci];
            long[] cells = chunkCells[ci];
            Portal[] ps = chunkPortals[ci];
            for (int k = 0; k < sz; ++k) {
                if (portalCells.computeIfAbsent(dirs[k], key -> new HashSet<>()).add(cells[k])) {
                    portals.computeIfAbsent(dirs[k], key -> new java.util.ArrayList<>()).add(ps[k]);
                    ++totalPortals;
                }
            }
        }
        portalsIndexed = true;
        System.out.printf("F4_V66_PORTALS portals=%d time_s=%.2f%n", totalPortals, (System.nanoTime() - t0) / 1e9);
    }

    private Box slrBox(Box base, int slr) {
        int[] rows = slrRows.get(slr);
        return new Box(base.left, Math.max(base.bottom, rows[0] - 1),
                base.right, Math.min(base.top, rows[1] + 1));
    }

    @SuppressWarnings("unchecked")
    private final Map<Connection, Box>[] shardCompact = createCompactShards();

    private static Map<Connection, Box>[] createCompactShards() {
        @SuppressWarnings("unchecked")
        Map<Connection, Box>[] arr = new Map[CAND_SHARDS];
        for (int s = 0; s < CAND_SHARDS; ++s) arr[s] = new IdentityHashMap<>(1 << 10);
        return arr;
    }

    private Map<Connection, Box> compactMap(Connection c) {
        return shardCompact[candShard(c)];
    }
    private boolean crossSlrCompactX = Boolean.getBoolean("fides.ppt.f4.crossSlrCompactX");

    void stopCompactCrossSlr() {
        crossSlrCompactX = false;
        for (int s = 0; s < CAND_SHARDS; ++s) shardCompact[s].clear();
    }

    private static Box compactCrossSlrX(Box planned, Portal portal, int sx, int tx) {
        int portalLeft = Math.min(portal.sllX, Math.min(portal.ingressX, portal.exitX));
        int portalRight = Math.max(portal.sllX, Math.max(portal.ingressX, portal.exitX));
        // Box bounds are open. Preserve the endpoint and portal columns, plus one coarse cell.
        int left = Math.max(planned.left, Math.min(Math.min(sx, tx), portalLeft) - CELL - 1);
        int right = Math.min(planned.right, Math.max(Math.max(sx, tx), portalRight) + CELL + 1);
        return left == planned.left && right == planned.right
                ? planned : new Box(left, planned.bottom, right, planned.top);
    }


    private final boolean multiSlrCorridor = Boolean.getBoolean("fides.ppt.f4.multiSlrCorridor");
    private volatile boolean multiSlrWitness;

    // A label carries the geometry of all settled slabs and real SLL crossings so far.
    private static final class MultiSlrSeed {
        final int x, y, cost;
        final Box envelope;

        MultiSlrSeed(int x, int y, int cost, Box envelope) {
            this.x = x;
            this.y = y;
            this.cost = cost;
            this.envelope = envelope;
        }
    }

    private static final class MultiSlrScratch {
        final CoarseScratch queue = new CoarseScratch();
        private int[] pathLeft = new int[0], pathBottom = new int[0];
        private int[] pathRight = new int[0], pathTop = new int[0];
        private boolean[] targets = new boolean[0];
        private int left, bottom, width, height, targetCount;

        boolean begin(Box slab) {
            // As in choose(), bounding-box coordinates are open tile bounds.
            left = Math.floorDiv(slab.left + 1, CELL);
            bottom = Math.floorDiv(slab.bottom + 1, CELL);
            width = Math.floorDiv(slab.right - 1, CELL) - left + 1;
            height = Math.floorDiv(slab.top - 1, CELL) - bottom + 1;
            long area = (long) width * height;
            if (slab.right - slab.left <= 1 || slab.top - slab.bottom <= 1
                    || width <= 0 || height <= 0 || area > 200000) return false;
            int size = (int) area;
            if (size > targets.length) {
                int capacity = Math.max(size, Math.max(128, 2 * targets.length));
                pathLeft = new int[capacity];
                pathBottom = new int[capacity];
                pathRight = new int[capacity];
                pathTop = new int[capacity];
                targets = new boolean[capacity];
            }
            Arrays.fill(targets, 0, size, false);
            queue.begin(size);
            targetCount = 0;
            return true;
        }

        int cell(int x, int y) {
            int column = Math.floorDiv(x, CELL) - left;
            int row = Math.floorDiv(y, CELL) - bottom;
            if (column < 0 || column >= width || row < 0 || row >= height) return -1;
            return column * height + row;
        }

        void addTarget(int cell) {
            if (cell >= 0 && !targets[cell]) {
                targets[cell] = true;
                ++targetCount;
            }
        }

        private void includeCell(int at, int x0, int y0, int x1, int y1) {
            int x = (left + at / height) * CELL;
            int y = (bottom + at % height) * CELL;
            pathLeft[at] = Math.min(x0, x - 1);
            pathBottom[at] = Math.min(y0, y - 1);
            pathRight[at] = Math.max(x1, x + CELL);
            pathTop[at] = Math.max(y1, y + CELL);
        }

        void seed(MultiSlrSeed seed) {
            int at = cell(seed.x, seed.y);
            if (at < 0 || seed.cost >= queue.distance(at)) return;
            queue.decrease(at, seed.cost, -1);
            Box box = seed.envelope;
            includeCell(at, box.left, box.bottom, box.right, box.top);
        }

        private void relax(int from, int to, int weight) {
            long improved = (long) queue.distances[from] + 1 + weight;
            if (improved >= queue.distance(to)) return;
            queue.decrease(to, (int) improved, from);
            includeCell(to, pathLeft[from], pathBottom[from], pathRight[from], pathTop[from]);
        }

        void settle(F1Corridor planner) {
            while (targetCount != 0 && queue.size != 0) {
                int at = queue.removeFirst();
                if (targets[at] && --targetCount == 0) break;
                int x = at / height, y = at % height;
                int column = left + x, row = bottom + y;
                if (x != 0) relax(at, at - height, planner.weight(column - 1, row));
                if (y != 0) relax(at, at - 1, planner.weight(column, row - 1));
                if (y + 1 != height) relax(at, at + 1, planner.weight(column, row + 1));
                if (x + 1 != width) relax(at, at + height, planner.weight(column + 1, row));
            }
        }

        Box envelope(int cell) {
            return new Box(pathLeft[cell], pathBottom[cell], pathRight[cell], pathTop[cell]);
        }
    }

    private final ThreadLocal<MultiSlrScratch> multiScratch = ThreadLocal.withInitial(MultiSlrScratch::new);

    private Box planMultiSlrBox(Box base, int from, int to, int sx, int sy, int tx, int ty) {
        int step = Integer.compare(to, from);
        MultiSlrScratch search = multiScratch.get();
        java.util.List<MultiSlrSeed> incoming = new java.util.ArrayList<>();
        incoming.add(new MultiSlrSeed(sx, sy, 0, new Box(sx - 1, sy - 1, sx + 1, sy + 1)));
        // Clip every slab to both the original box and the INT fabric. The crossing
        // itself uses the actual directed CSR portal and is not inferred from the grid.
        Box bounds = new Box(Math.max(base.left, fabric.left), Math.max(base.bottom, fabric.bottom),
                Math.min(base.right, fabric.right), Math.min(base.top, fabric.top));
        for (int slr = from;; slr += step) {
            if (!slrRows.containsKey(slr) || !search.begin(slrBox(bounds, slr))) return null;
            Box slab = slrBox(bounds, slr);
            for (MultiSlrSeed seed : incoming) {
                if (slab.contains(seed.x, seed.y)) search.seed(seed);
            }
            if (slr == to) {
                if (!slab.contains(tx, ty)) return null;
                int target = search.cell(tx, ty);
                search.addTarget(target);
                search.settle(this);
                if (target < 0 || search.queue.distance(target) == Integer.MAX_VALUE) return null;
                Box envelope = search.envelope(target);
                Box tight = new Box(Math.max(base.left, envelope.left - CELL),
                        Math.max(base.bottom, envelope.bottom - CELL),
                        Math.min(base.right, envelope.right + CELL),
                        Math.min(base.top, envelope.top + CELL));
                return tight.contains(sx, sy) && tight.contains(tx, ty) && !tight.same(base) ? tight : null;
            }
            int next = slr + step;
            var direction = portals.get(cellKey(slr, next));
            if (direction == null || !slrRows.containsKey(next)) return null;
            Box nextSlab = slrBox(bounds, next);
            java.util.List<Portal> eligible = new java.util.ArrayList<>();
            for (Portal portal : direction) {
                if (!portal.inside(base) || !slab.contains(portal.ingressX, portal.ingressY)
                        || !nextSlab.contains(portal.exitX, portal.exitY)) continue;
                eligible.add(portal);
                search.addTarget(search.cell(portal.ingressX, portal.ingressY));
            }
            if (eligible.isEmpty()) return null;
            search.settle(this);
            incoming.clear();
            // Maintain the pinned CSR enumeration order for equal-cost seeds. Grid
            // cells retain CoarseScratch's deterministic distance/cell-ID order.
            for (Portal portal : eligible) {
                int at = search.cell(portal.ingressX, portal.ingressY);
                int cost = search.queue.distance(at);
                if (cost == Integer.MAX_VALUE) continue;
                Box envelope = search.envelope(at);
                Box crossed = new Box(Math.min(envelope.left, Math.min(portal.sllX, portal.exitX) - 1),
                        Math.min(envelope.bottom, Math.min(portal.sllRawY, portal.exitY) - 1),
                        Math.max(envelope.right, Math.max(portal.sllX, portal.exitX) + 1),
                        Math.max(envelope.top, Math.max(portal.sllRawY, portal.exitY) + 1));
                incoming.add(new MultiSlrSeed(portal.exitX, portal.exitY, cost, crossed));
            }
            if (incoming.isEmpty()) return null;
        }
    }

    private Candidate planCross(Connection connection, RouteNode source, RouteNode sink, Box base,
                                 int sx, int sy, int tx, int ty, RouteNodeGraph graph) {
        int from = rawSlr(graph, sy), to = rawSlr(graph, ty);
        if (from < 0 || to < 0 || from == to
                || connection.isCrossSLRnorth() != (ty > sy)
                || connection.isCrossSLRsouth() != (ty < sy)) return null;
        if (Math.abs(from - to) > 1) {
            if (!multiSlrCorridor) return null;
            Box tight = planMultiSlrBox(base, from, to, sx, sy, tx, ty);
            if (tight == null) return null;
            if (!multiSlrWitness) {
                multiSlrWitness = true;
                System.out.printf("F4_MULTI_SLR_CORRIDOR from=%d to=%d boundaries=%d source=%d,%d sink=%d,%d base=%s tight=%s%n",
                        from, to, Math.abs(from - to), sx, sy, tx, ty, base.fields(), tight.fields());
            }
            return new Candidate(base, tight, source, sink);
        }
        var direction = portals.get(cellKey(from, to));
        if (direction == null) return null;
        Box sourceBox = slrBox(base, from), sinkBox = slrBox(base, to);
        Portal best = null;
        int bestDetour = Integer.MAX_VALUE;
        for (Portal portal : direction) {
            if (!portal.inside(base) || !sourceBox.contains(portal.ingressX, portal.ingressY)
                    || !sinkBox.contains(portal.exitX, portal.exitY)
                    || !fabric.contains(portal.ingressX, portal.ingressY)
                    || !fabric.contains(portal.exitX, portal.exitY)) continue;
            int detour = portal.detour(sx, sy, tx, ty);
            if (detour < bestDetour) {
                best = portal;
                bestDetour = detour;
            }
        }
        if (best == null) return null;
        Choice first = choose(sourceBox, sx, sy, best.ingressX, best.ingressY);
        Choice last = choose(sinkBox, best.exitX, best.exitY, tx, ty);
        if (first == null || last == null) return null;
        Box tight = new Box(Math.min(Math.min(first.box.left, last.box.left), best.sllX - 1),
                Math.min(Math.min(first.box.bottom, last.box.bottom), best.sllRawY - 1),
                Math.max(Math.max(first.box.right, last.box.right), best.sllX + 1),
                Math.max(Math.max(first.box.top, last.box.top), best.sllRawY + 1));
        if (!tight.contains(sx, sy) || !tight.contains(tx, ty) || !best.inside(tight)
                || tight.same(base)) return null;
        if (crossSlrCompactX) {
            compactMap(connection).put(connection, compactCrossSlrX(tight, best, sx, tx));
        }
        return new Candidate(base, tight, source, sink);
    }

    Box box(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph) {
        if (!connection.isCrossSLR()) return box(connection);
        if (!crossSlrCorridor
                || connection.hasAltSinks() || connection.getHpwl() < 16
                || graph.wireIndicesLeadingToLaguna == null) return null;
        RouteNode source = connection.getSourceRnode(), sink = connection.getSinkRnode();
        if (source == null || sink == null) return null;
        int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
        int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
        Box base = Box.original(connection);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        if (!portalsIndexed) indexPortals(rrg, graph);
        Map<Connection, Candidate> cmap = candMap(connection);
        if (!cmap.containsKey(connection)) {
            cmap.put(connection, planCross(connection, source, sink, base, sx, sy, tx, ty, graph));
        }
        Candidate candidate = cmap.get(connection);
        if (candidate == null) return null;
        if (candidate.source != source || candidate.sink != sink || !candidate.base.same(base)) {
            disable(connection);
            return null;
        }
        return crossSlrCompactX ? compactMap(connection).getOrDefault(connection, candidate.tight()) : candidate.tight();
    }

    Candidate get(Connection connection) {
        Map<Connection, Candidate> cmap = candMap(connection);
        if (!cmap.containsKey(connection)) cmap.put(connection, plan(connection));
        Candidate candidate = cmap.get(connection);
        if (candidate != null && !candidate.matches(connection)) {
            disable(connection);
            return null;
        }
        return candidate;
    }

    private final java.util.Set<Connection> f4Retired =
            java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());

    private Box f4LongEnvelope(Connection c) {
        if (c.getHpwl() < 16 || c.hasAltSinks() || f4Retired.contains(c)) return null;
        RouteNode s = c.getSourceRnode(), t = c.getSinkRnode();
        if (s == null || t == null || s.getTile().getSLR().getId() != t.getTile().getSLR().getId())
            return null;
        int sx = s.getEndTileXCoordinate(), sy = s.getEndTileYCoordinate();
        int tx = t.getEndTileXCoordinate(), ty = t.getEndTileYCoordinate();
        Box base = Box.original(c);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        int margin = Math.min(16, Math.max(4, c.getHpwl() / 8));
        Box effective = new Box(Math.max(base.left, Math.min(sx, tx) - margin - 1),
                Math.max(base.bottom, Math.min(sy, ty) - margin - 1),
                Math.min(base.right, Math.max(sx, tx) + margin + 1),
                Math.min(base.top, Math.max(sy, ty) + margin + 1));
        return (effective.area() * 8 <= base.area() * 7) ? effective : null;
    }

    void ensurePortalsIndexed(FidesRrgLoader rrg, RouteNodeGraph graph) {
        if (crossSlrCorridor && graph.wireIndicesLeadingToLaguna != null && !portalsIndexed) {
            indexPortals(rrg, graph);
        }
    }

    static final class ParallelEntry {
        final Candidate candidate;
        final Box compact;
        ParallelEntry(Candidate candidate, Box compact) {
            this.candidate = candidate;
            this.compact = compact;
        }
    }

    Box boxParallel(Connection connection, FidesRrgLoader rrg, RouteNodeGraph graph,
                    ParallelEntry[] out, int index) {
        if (!connection.isCrossSLR()) {
            RouteNode source = connection.getSourceRnode();
            RouteNode sink = connection.getSinkRnode();
            if (source == null || sink == null || connection.hasAltSinks()
                    || source.getTile().getSLR().getId() != sink.getTile().getSLR().getId()) {
                return null;
            }
            if (connection.getHpwl() >= 16) {
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
            if (!f4Retired.isEmpty() && candMap(connection).containsKey(connection)) return null;
            int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
            int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
            if (!fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
            Box base = Box.original(connection);
            if (!base.contains(sx, sy) || !base.contains(tx, ty)) return null;
            Box shortBox = new Box(Math.max(base.left, Math.min(sx, tx) - 4),
                    Math.max(base.bottom, Math.min(sy, ty) - 4),
                    Math.min(base.right, Math.max(sx, tx) + 4),
                    Math.min(base.top, Math.max(sy, ty) + 4));
            return shortBox.same(base) ? null : shortBox;
        }
        if (!crossSlrCorridor || connection.hasAltSinks() || connection.getHpwl() < 16
                || graph.wireIndicesLeadingToLaguna == null) return null;
        RouteNode source = connection.getSourceRnode(), sink = connection.getSinkRnode();
        if (source == null || sink == null) return null;
        int sx = source.getEndTileXCoordinate(), sy = source.getEndTileYCoordinate();
        int tx = sink.getEndTileXCoordinate(), ty = sink.getEndTileYCoordinate();
        Box base = Box.original(connection);
        if (!base.contains(sx, sy) || !base.contains(tx, ty)
                || !fabric.contains(sx, sy) || !fabric.contains(tx, ty)) return null;
        Map<Connection, Candidate> cmap = candMap(connection);
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
        }
        Box[] compactBox = new Box[1];
        Candidate planned = planCrossParallel(connection, source, sink, base, sx, sy, tx, ty, graph, compactBox);
        out[index] = new ParallelEntry(planned, compactBox[0]);
        if (planned == null) return null;
        return crossSlrCompactX && compactBox[0] != null ? compactBox[0] : planned.tight();
    }

    private Candidate planCrossParallel(Connection connection, RouteNode source, RouteNode sink, Box base,
                                        int sx, int sy, int tx, int ty, RouteNodeGraph graph, Box[] compactBox) {
        int from = rawSlr(graph, sy), to = rawSlr(graph, ty);
        if (from < 0 || to < 0 || from == to
                || connection.isCrossSLRnorth() != (ty > sy)
                || connection.isCrossSLRsouth() != (ty < sy)) return null;
        if (Math.abs(from - to) > 1) {
            if (!multiSlrCorridor) return null;
            Box tight = planMultiSlrBox(base, from, to, sx, sy, tx, ty);
            if (tight == null) return null;
            if (!multiSlrWitness) {
                synchronized (this) {
                    if (!multiSlrWitness) {
                        multiSlrWitness = true;
                        System.out.printf("F4_MULTI_SLR_CORRIDOR from=%d to=%d boundaries=%d source=%d,%d sink=%d,%d base=%s tight=%s%n",
                                from, to, Math.abs(from - to), sx, sy, tx, ty, base.fields(), tight.fields());
                    }
                }
            }
            return new Candidate(base, tight, source, sink);
        }
        var direction = portals.get(cellKey(from, to));
        if (direction == null) return null;
        Box sourceBox = slrBox(base, from), sinkBox = slrBox(base, to);
        Portal best = null;
        int bestDetour = Integer.MAX_VALUE;
        for (Portal portal : direction) {
            if (!portal.inside(base) || !sourceBox.contains(portal.ingressX, portal.ingressY)
                    || !sinkBox.contains(portal.exitX, portal.exitY)
                    || !fabric.contains(portal.ingressX, portal.ingressY)
                    || !fabric.contains(portal.exitX, portal.exitY)) continue;
            int detour = portal.detour(sx, sy, tx, ty);
            if (detour < bestDetour) {
                best = portal;
                bestDetour = detour;
            }
        }
        if (best == null) return null;
        Choice first = choose(sourceBox, sx, sy, best.ingressX, best.ingressY);
        Choice last = choose(sinkBox, best.exitX, best.exitY, tx, ty);
        if (first == null || last == null) return null;
        Box tight = new Box(Math.min(Math.min(first.box.left, last.box.left), best.sllX - 1),
                Math.min(Math.min(first.box.bottom, last.box.bottom), best.sllRawY - 1),
                Math.max(Math.max(first.box.right, last.box.right), best.sllX + 1),
                Math.max(Math.max(first.box.top, last.box.top), best.sllRawY + 1));
        if (!tight.contains(sx, sy) || !tight.contains(tx, ty) || !best.inside(tight)
                || tight.same(base)) return null;
        if (crossSlrCompactX) {
            compactBox[0] = compactCrossSlrX(tight, best, sx, tx);
        }
        return new Candidate(base, tight, source, sink);
    }

    void commitParallel(java.util.List<Connection> all, ParallelEntry[] entries) {
        final int n = entries.length;
        if (n < 4096) {
            for (int i = 0; i < n; ++i) {
                ParallelEntry e = entries[i];
                if (e == null) continue;
                Connection c = all.get(i);
                candMap(c).put(c, e.candidate);
                if (e.compact != null) compactMap(c).put(c, e.compact);
            }
            return;
        }
        final int workers = Math.min(CAND_SHARDS, Math.max(1, Runtime.getRuntime().availableProcessors()));
        final int chunk = Math.max(4096, (n + workers * 4 - 1) / (workers * 4));
        final int numBuckets = (n + chunk - 1) / chunk;
        final int[][][] bIdx = new int[numBuckets][CAND_SHARDS][];
        final int[][] bLen = new int[numBuckets][CAND_SHARDS];
        var scatter = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(numBuckets);
        for (int bi = 0; bi < numBuckets; ++bi) {
            final int b = bi, from = bi * chunk, to = Math.min(n, from + chunk);
            scatter.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int[][] lIdx = new int[CAND_SHARDS][];
                int[] lLen = new int[CAND_SHARDS];
                for (int i = from; i < to; ++i) {
                    if (entries[i] == null) continue;
                    int s = candShard(all.get(i));
                    int[] arr = lIdx[s];
                    int pos = lLen[s];
                    if (arr == null) {
                        arr = new int[128];
                        lIdx[s] = arr;
                    } else if (pos == arr.length) {
                        arr = java.util.Arrays.copyOf(arr, pos * 2);
                        lIdx[s] = arr;
                    }
                    arr[pos] = i;
                    lLen[s] = pos + 1;
                }
                bIdx[b] = lIdx;
                bLen[b] = lLen;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(scatter.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        var puts = new java.util.ArrayList<java.util.concurrent.CompletableFuture<Void>>(CAND_SHARDS);
        for (int si = 0; si < CAND_SHARDS; ++si) {
            final int s = si;
            puts.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                Map<Connection, Candidate> cmap = shardCand[s];
                Map<Connection, Box> kmap = shardCompact[s];
                for (int b = 0; b < numBuckets; ++b) {
                    int[] ids = bIdx[b][s];
                    int len = bLen[b][s];
                    for (int k = 0; k < len; ++k) {
                        int i = ids[k];
                        ParallelEntry e = entries[i];
                        Connection c = all.get(i);
                        cmap.put(c, e.candidate);
                        if (e.compact != null) kmap.put(c, e.compact);
                    }
                    bIdx[b][s] = null;
                }
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(puts.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
    }

    void disable(Connection connection) {
        f4Retired.add(connection);
        candMap(connection).put(connection, null);
    }
}
