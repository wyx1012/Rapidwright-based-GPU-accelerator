"""Install weighted multi-SLR corridor selection over the existing directed RRG portals."""

_MULTI = r'''
    private final boolean multiSlrCorridor = Boolean.getBoolean("fides.ppt.f4.multiSlrCorridor");
    private boolean multiSlrWitness;

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

'''


def patch_planner(source: str) -> str:
    anchor = "    private Candidate planCross(Connection connection"
    original = """        if (from < 0 || to < 0 || Math.abs(from - to) != 1
                || connection.isCrossSLRnorth() != (ty > sy)
                || connection.isCrossSLRsouth() != (ty < sy)) return null;
        var direction = portals.get(cellKey(from, to));"""
    changed = """        if (from < 0 || to < 0 || from == to
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
        var direction = portals.get(cellKey(from, to));"""
    if source.count(anchor) != 1 or source.count(original) != 1:
        raise ValueError("Expected the pinned v6c cross-SLR planner")
    return source.replace(anchor, _MULTI + anchor, 1).replace(original, changed, 1)
