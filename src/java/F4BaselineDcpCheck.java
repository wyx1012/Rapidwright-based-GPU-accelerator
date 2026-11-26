import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.DesignTools;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.device.Node;
import com.xilinx.rapidwright.device.Tile;
import com.xilinx.rapidwright.rwroute.PartialRouter;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ForkJoinPool;
import java.util.concurrent.atomic.LongAdder;

public final class F4BaselineDcpCheck {
    private static final int SHARDS = 64;

    public static void main(String[] args) throws Exception {
        long t0 = System.nanoTime();
        Design design = Design.readCheckpoint(args[0]);
        long t1 = System.nanoTime();
        var warmedTiles = new java.util.HashSet<Object>();
        for (Tile t : design.getDevice().getAllTiles()) {
            if (warmedTiles.add(t.getTileTypeEnum())) {
                t.getPIP(0, 0);
                if (t.getWireCount() > 0) {
                    t.getForwardTilePIPs(0);
                }
            }
        }
        List<Net> nets = new ArrayList<>(design.getNets());
        final int n = nets.size();
        int workers = Math.max(1, Math.min(36, Runtime.getRuntime().availableProcessors()));
        ForkJoinPool pool = new ForkJoinPool(workers);

        @SuppressWarnings("unchecked")
        ConcurrentHashMap<Node, Net>[] ownerShards = new ConcurrentHashMap[SHARDS];
        for (int s = 0; s < SHARDS; ++s) {
            ownerShards[s] = new ConcurrentHashMap<>(1 << 20, 0.75f, workers);
        }
        Set<Node> shared = ConcurrentHashMap.newKeySet();
        LongAdder pips = new LongAdder();

        int chunk = Math.max(2048, (n + workers * 4 - 1) / (workers * 4));
        List<CompletableFuture<Void>> pinJobs = new ArrayList<>();
        for (int from = 0; from < n; from += chunk) {
            final int f = from;
            final int t = Math.min(n, from + chunk);
            pinJobs.add(CompletableFuture.runAsync(() -> {
                for (int i = f; i < t; ++i) {
                    DesignTools.updatePinsIsRouted(nets.get(i));
                }
            }, pool));
        }
        CompletableFuture.allOf(pinJobs.toArray(new CompletableFuture<?>[0])).join();
        long t2 = System.nanoTime();

        List<CompletableFuture<Void>> jobs = new ArrayList<>();
        for (int from = 0; from < n; from += chunk) {
            final int f = from;
            final int t = Math.min(n, from + chunk);
            jobs.add(CompletableFuture.runAsync(() -> {
                long localPips = 0;
                for (int i = f; i < t; ++i) {
                    Net net = nets.get(i);
                    for (var pip : net.getPIPs()) {
                        ++localPips;
                        add(ownerShards, shared, net, pip.getStartNode());
                        add(ownerShards, shared, net, pip.getEndNode());
                    }
                }
                pips.add(localPips);
            }, pool));
        }
        CompletableFuture.allOf(jobs.toArray(new CompletableFuture<?>[0])).join();
        pool.shutdown();

        long t3 = System.nanoTime();
        int unroutedPins = PartialRouter.getUnroutedPins(design).size();
        long t4 = System.nanoTime();

        System.out.printf("F4_V37_DCP_CHECK_TIMING read=%.2fs pins=%.2fs pip_shards=%.2fs unrouted_scan=%.2fs%n",
                (t1 - t0) / 1e9, (t2 - t1) / 1e9, (t3 - t2) / 1e9, (t4 - t3) / 1e9);
        System.out.printf("F4_BASE_DCP unroutedPins=%d sharedPipNodes=%d pips=%d%n",
                unroutedPins, shared.size(), pips.sum());
    }

    private static void add(ConcurrentHashMap<Node, Net>[] ownerShards, Set<Node> shared, Net net, Node node) {
        if (node == null) return;
        Tile tile = node.getTile();
        int s = tile == null ? 0 : (tile.getUniqueAddress() & (SHARDS - 1));
        Net owner = ownerShards[s].putIfAbsent(node, net);
        if (owner != null && owner != net) shared.add(node);
    }
}
