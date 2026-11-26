"""Write a small server-side Java fixture for the exact patched F1 class.

Use an empty isolated directory. Compile the emitted .java files together with
the server's javac using -d classes, then run
com.xilinx.rapidwright.rwroute.MultiSlrV7Fixture.
"""

from pathlib import Path
import sys

from multi_slr_v7 import patch_planner


_SOURCES = {
    "device/TileTypeEnum.java": """package com.xilinx.rapidwright.device;
public enum TileTypeEnum { INT, LAGUNA }
""",
    "device/Tile.java": """package com.xilinx.rapidwright.device;
public final class Tile {
    private final int x, y;
    public Tile(int x, int y) { this.x = x; this.y = y; }
    public int getTileXCoordinate() { return x; }
    public int getTileYCoordinate() { return y; }
    public TileTypeEnum getTileTypeEnum() { return TileTypeEnum.INT; }
    public Slr getSLR() { return new Slr(y / 16); }
    public static final class Slr {
        private final int id;
        Slr(int id) { this.id = id; }
        public int getId() { return id; }
    }
}
""",
    "device/Node.java": """package com.xilinx.rapidwright.device;
public final class Node {
    public Tile getTile() { return null; }
}
""",
    "design/Design.java": """package com.xilinx.rapidwright.design;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.Tile;
import java.util.List;
public final class Design {
    public Device getDevice() { return new Device(); }
    public List<Net> getNets() { return List.of(); }
    public static final class Device {
        public Tile[] getAllTiles() {
            Tile[] tiles = new Tile[96];
            for (int y = 0; y < 48; ++y) {
                tiles[y * 2] = new Tile(0, y);
                tiles[y * 2 + 1] = new Tile(63, y);
            }
            return tiles;
        }
    }
    public static final class Net {
        public List<Pip> getPIPs() { return List.of(); }
    }
    public static final class Pip {
        public Node getStartNode() { return null; }
        public Node getEndNode() { return null; }
    }
}
""",
    "util/Utils.java": """package com.xilinx.rapidwright.util;
import com.xilinx.rapidwright.device.TileTypeEnum;
public final class Utils {
    public static boolean isLaguna(TileTypeEnum type) { return type == TileTypeEnum.LAGUNA; }
}
""",
    "rwroute/MultiSlrV7Fixture.java": """package com.xilinx.rapidwright.rwroute;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.device.TileTypeEnum;

enum RouteNodeType { NON_LOCAL, SUPER_LONG_LINE }

final class RouteNodeGraph {
    final int[] intYToSLRIndex = new int[48];
    final int[] wireIndicesLeadingToLaguna = new int[1];
    RouteNodeGraph() {
        for (int y = 0; y < 48; ++y) intYToSLRIndex[y] = y / 16;
    }
}

final class FidesRrgLoader {
    final int N;
    final int[] endTileX, endTileYRaw, tileY, nodeType, tileType, tileId, offset, edge;

    FidesRrgLoader(boolean withSecondNorthPortal) {
        int[][] routes = {
            {8,  14, 17},
            {20, 14, 17},
            {24, 30, 33},
            {30, 33, 30},
            {24, 17, 14}
        };
        N = 3 * routes.length;
        endTileX = new int[N];
        endTileYRaw = new int[N];
        tileY = new int[N];
        nodeType = new int[N];
        tileType = new int[N];
        tileId = new int[N];
        offset = new int[N + 1];
        edge = new int[routes.length * 2];
        int count = 0;
        for (int i = 0; i < routes.length; ++i) {
            int p = 3 * i;
            int[] route = routes[i];
            for (int j = 0; j < 3; ++j) {
                int node = p + j;
                endTileX[node] = route[0];
                endTileYRaw[node] = j == 0 ? route[1] : route[2];
                tileY[node] = j == 1 ? route[1] : endTileYRaw[node];
                tileId[node] = node;
                nodeType[node] = j == 1 ? RouteNodeType.SUPER_LONG_LINE.ordinal() : RouteNodeType.NON_LOCAL.ordinal();
                tileType[node] = j == 1 ? TileTypeEnum.INT.ordinal() : TileTypeEnum.LAGUNA.ordinal();
                offset[node] = count;
                if (j == 0 || (j == 1 && (i != 2 || withSecondNorthPortal))) edge[count++] = node + 1;
            }
        }
        offset[N] = count;
    }
}

final class RouteNode {
    final Tile tile;
    RouteNode(int x, int y) { tile = new Tile(x, y); }
    Tile getTile() { return tile; }
    int getEndTileXCoordinate() { return tile.getTileXCoordinate(); }
    int getEndTileYCoordinate() { return tile.getTileYCoordinate(); }
}

final class Connection {
    final RouteNode source, sink;
    final boolean reversedDirection;
    Connection(int sx, int sy, int tx, int ty) { this(sx, sy, tx, ty, false); }
    Connection(int sx, int sy, int tx, int ty, boolean reversedDirection) {
        source = new RouteNode(sx, sy);
        sink = new RouteNode(tx, ty);
        this.reversedDirection = reversedDirection;
    }
    RouteNode getSourceRnode() { return source; }
    RouteNode getSinkRnode() { return sink; }
    boolean isCrossSLR() { return source.getTile().getSLR().getId() != sink.getTile().getSLR().getId(); }
    boolean isCrossSLRnorth() {
        return (source.getEndTileYCoordinate() < sink.getEndTileYCoordinate()) != reversedDirection;
    }
    boolean isCrossSLRsouth() { return !isCrossSLRnorth(); }
    boolean hasAltSinks() { return false; }
    int getHpwl() {
        return Math.abs(source.getEndTileXCoordinate() - sink.getEndTileXCoordinate())
                + Math.abs(source.getEndTileYCoordinate() - sink.getEndTileYCoordinate());
    }
    int getXMinBB() { return -1; }
    int getYMinBB() { return -1; }
    int getXMaxBB() { return 64; }
    int getYMaxBB() { return 48; }
}

public final class MultiSlrV7Fixture {
    private static void require(boolean ok, String description) {
        if (!ok) throw new AssertionError(description);
    }

    public static void main(String[] args) {
        System.setProperty("fides.ppt.f4.crossSlrCorridor", "true");
        RouteNodeGraph graph = new RouteNodeGraph();
        FidesRrgLoader rrg = new FidesRrgLoader(true);
        Connection north = new Connection(15, 2, 26, 42);
        Connection adjacent = new Connection(17, 2, 20, 22);
        System.clearProperty("fides.ppt.f4.multiSlrCorridor");
        F1Corridor control = new F1Corridor(new Design());
        require(control.box(north, rrg, graph) == null, "disabled multi-SLR should use the original box");
        F1Corridor.Box oldAdjacent = control.box(adjacent, rrg, graph);

        System.setProperty("fides.ppt.f4.multiSlrCorridor", "true");
        F1Corridor planner = new F1Corridor(new Design());
        F1Corridor.Box first = planner.box(north, rrg, graph);
        // The portal at x=8 is closer to the source, but x=20 followed by x=24
        // has lower total cost. Its computed corridor preserves both crossings.
        require(first != null && first.left == 7 && first.right < north.getXMaxBB(),
                "layered weighted choice and strict narrowing");
        require(first.contains(20,14) && first.contains(20,17)
                && first.contains(24,30) && first.contains(24,33), "northbound crossings missing");
        require(first.contains(north.source) && first.contains(north.sink), "northbound endpoints missing");
        require(first.same(planner.box(north, rrg, graph)), "same connection's cached geometry changed");
        require(new F1Corridor(new Design()).box(north, new FidesRrgLoader(false), graph) == null,
                "a portal missing the real SLL exit must not narrow");
        require(planner.box(new Connection(15,2,26,42,true),rrg,graph) == null,
                "source/sink direction mismatch must not narrow");

        Connection south = new Connection(29, 42, 23, 3);
        F1Corridor.Box reverse = planner.box(south, rrg, graph);
        require(reverse != null && reverse.contains(30,33) && reverse.contains(30,30)
                && reverse.contains(24,17) && reverse.contains(24,14), "southbound CSR crossings missing");
        require(oldAdjacent != null && oldAdjacent.same(planner.box(adjacent, rrg, graph)),
                "adjacent algorithm changed");
        System.out.println("F4_MULTI_SLR_FIXTURE_PASS");
    }
}
""",
}


def write_fixture(pinned_source: Path, root: Path) -> None:
    root.mkdir()
    sources = dict(_SOURCES)
    sources["rwroute/F1Corridor.java"] = patch_planner(pinned_source.read_text(encoding="utf-8"))
    for relative, data in sources.items():
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(data, encoding="utf-8")


if __name__ == "__main__":
    write_fixture(Path(sys.argv[1]), Path(sys.argv[2]))
