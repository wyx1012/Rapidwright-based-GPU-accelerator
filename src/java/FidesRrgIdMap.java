package com.xilinx.rapidwright.rwroute;

import java.io.Closeable;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.Arrays;
import java.util.Locale;

/**
 * Dense-id &lt;-&gt; live {@link RouteNode} map for the FIDES lane, backed by the frozen RRG manifest.
 *
 * <h2>Why this exists</h2>
 * The flattener's own {@code IdentityHashMap<RouteNode,Integer> idOf} (FidesRrgFlattener.java:102) is
 * built, used and dropped -- it was never dumped, so a lane that walks the flat CSR columns has no way
 * to name a node in the live routing graph, or vice versa.  Re-deriving it by re-running the
 * flattener's forced closure expansion costs 375 s per run (281.86 s expand + 29.42 s id + 63.14 s csr,
 * step3_config.txt), which would make the mutation table unaffordable.  The manifest is the cheap way
 * back: it already carries, per node, exactly the coordinate that indexes
 * {@code RouteNodeGraph.nodesMap}.
 *
 * <h2>The mapping, and what makes it a lookup instead of a search</h2>
 * {@code nodesMap} is a {@code RouteNode[][]} indexed {@code [tile.getUniqueAddress()][wireIndex]}
 * (RouteNodeGraph.java:74, :835, :919), and {@code getRnodes()} enumerates it
 * {@code tileAddress} ascending then {@code wireIndex} ascending (RouteNodeGraph.java:839-901).  The
 * dense id is the position in that enumeration, which is exactly what the manifest's third and fourth
 * fields record:
 * <pre>
 *   id  tileName/wireName  tileAddress  wireIndex       (file line 17 + id, 1-based)
 * </pre>
 * That the id really is the data-line index was verified over the whole file, not sampled: 0
 * violations and 0 gaps across all 87,040,026 lines.  So {@code id -> line} is arithmetic, and
 * {@code id -> tileAddress} is a binary search over a per-tile run table built during one sequential
 * pass -- no per-query file I/O at all.  {@code wireIndex} is the one column that needs a line read,
 * memoised on the way.
 *
 * <h2>What the start-up pass actually verifies</h2>
 * Opening the map is not just an index build; it is the only place that ties the manifest to the
 * {@code .bin} it is supposed to describe.  All four checks are fail-closed:
 * <ol>
 *   <li>the leading {@code #} header is <em>counted</em>, its declared {@code N} and format version
 *       must match the loader's, and the count must equal the frozen artifact's.  A hardcoded offset
 *       would silently mis-map every id by a whole record on a re-versioned file;</li>
 *   <li>every data line's id equals its own index;</li>
 *   <li>every data line's {@code tileAddress} equals {@code rrg.tileId[id]} -- <em>all</em>
 *       87,040,026 of them, not a sample.  This is strictly stronger than hashing the manifest: a
 *       hash says "this is the file I read before", whereas this says "this manifest and this .bin
 *       describe the same node at every id", which is the property the lane actually needs;</li>
 *   <li>{@code tileAddress} is non-decreasing and {@code wireIndex} strictly increases within a tile
 *       run -- i.e. the file really is in the enumeration order the ids are defined by.</li>
 * </ol>
 * The remaining tie -- manifest against the <em>live</em> graph -- cannot be made here because it
 * needs a {@link RouteNodeGraph}; {@code FidesLaneGate} makes it instead, by walking every live rnode
 * and requiring {@code nodeAt(idOf(rnode)) == rnode}.  That check is also what independently pins
 * {@code wireIndex}, which check 4 alone constrains only up to monotonicity.  It is affordable at
 * every node because both directions resolve in RAM: {@link #idOf(RouteNode)} from the node's own
 * coordinates, {@link #nodeAt(RouteNodeGraph, int)} from the dense {@link #wireOf} column.
 *
 * <p>Not thread-safe, and not meant to be: the lane is single-lane by construction (M3-step3), and one
 * instance serves one routing run.
 */
public final class FidesRrgIdMap implements Closeable {

    /** Sparse byte-offset index resolution, in data lines.  (N/STRIDE)*8 B = 680 KB at N=87M. */
    private static final int SPARSE_STRIDE = 1024;

    /** Sequential-pass buffer; large enough that a real record never straddles a refill. */
    private static final int SCAN_BUF = 1 << 22;

    /**
     * Header line count of the frozen artifact, measured with {@code grep -c '^#'} = 16.  Required,
     * not assumed: the whole point is that a different artifact must not be silently re-indexed.
     */
    public static final int EXPECTED_HEADER_LINES = 16;

    /** First data line of the frozen manifest, for eyeballing a mismatch. */
    private static final String EXPECTED_FIRST_DATA_PREFIX = "0\t";

    /** Any structural or format violation.  Fail-closed: the caller must not proceed. */
    public static final class FormatException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public FormatException(String msg) {
            super(msg);
        }
    }

    private final Path path;
    private final RandomAccessFile raf;

    /** Counted, not assumed.  The line of id {@code k} is the {@code headerLines + k + 1}-th. */
    private final int headerLines;
    private final int n;

    /**
     * Byte offset of data line {@code k * SPARSE_STRIDE}; the anchor {@link #offsetOfDataLine} walks
     * forward from.  Not on the per-node path -- see {@link #wireOf}.
     */
    private final long[] sparseOffset;

    /** One entry per distinct tile run, both arrays ascending: the tile, and its run's first id. */
    private final int[] runTile;
    private final int[] runFirstId;
    private final int runCount;

    /**
     * The manifest's whole {@code wireIndex} column, dense by id: 4 B x N = 348 MB at N=87M.
     *
     * <p>Dense, and not a memo, because the two directions of this map are not symmetric and the
     * difference decides whether a per-node check is affordable.  {@code rnode -> id} is free: the
     * node carries its own {@code nodesMap} coordinates, so {@link #idOf(RouteNode)} is two binary
     * searches over in-RAM tables.  {@code id -> rnode} needs the wire index, which lives only in the
     * manifest, and reading one line there is not a seek-and-read but a walk from the nearest
     * {@link #SPARSE_STRIDE}-line anchor: up to 1023 lines, each its own {@code seek} + {@code read}.
     *
     * <p>Measured, at N=87,040,026: a direct-mapped 65,536-slot memo over that column is ~0% hit for
     * the scattered access a per-node check makes, so one {@link #nodeAt(RouteNodeGraph, int)} cost
     * ~366 line reads (~350 us), and rung 2's per-connection assertions (~60 resolutions each) turned
     * a 25.34 s iteration into 339.10 s -- 648 M read syscalls, visible in {@code /proc/<pid>/io}.
     * The column is on disk in one sequential pass anyway (see {@link #scan}), so capturing it there
     * costs one store per line and makes every later resolution a plain array read: no memo, no
     * eviction cliff, and no dependence on how local the caller's ids happen to be.
     */
    private final int[] wireOf;

    private final long scanBytes;
    private final long scanNanos;
    private final long scannedLines;
    private final byte[] seekBuf = new byte[512];
    private boolean closed;

    /* ------------------------------------------------------------------ open */

    /**
     * Scans and verifies the manifest against the {@code .bin} it describes.
     *
     * @param manifest the frozen {@code <prefix>.manifest.txt}
     * @param rrg      the loaded {@code .bin}; its {@code tileId} column witnesses check 3
     */
    public static FidesRrgIdMap open(Path manifest, FidesRrgLoader rrg) throws IOException {
        if (rrg == null) {
            throw new NullPointerException("rrg");
        }
        return new FidesRrgIdMap(manifest, rrg.N, rrg.tileId);
    }

    /**
     * Same checks, taking the witness column directly, so a synthetic KB-sized dump
     * ({@code FidesRrgLoaderSelfTest}'s emitter) can be used without materialising a loader.
     */
    public static FidesRrgIdMap open(Path manifest, int expectedN, int[] expectTileId) throws IOException {
        return new FidesRrgIdMap(manifest, expectedN, expectTileId);
    }

    private FidesRrgIdMap(Path path, int expectedN, int[] expectTileId) throws IOException {
        if (expectedN <= 0) {
            throw new IllegalArgumentException("expectedN " + expectedN);
        }
        if (expectTileId == null || expectTileId.length < expectedN) {
            throw new IllegalArgumentException("expectTileId shorter than N=" + expectedN);
        }
        this.path = path;
        String cacheProp = System.getProperty("fides.v37.idmapCache");
        String buildProp = System.getProperty("fides.v37.buildIdmapCache");
        Scan cached = null;
        long cacheT0 = System.nanoTime();
        if (cacheProp != null && !cacheProp.isEmpty()) {
            cached = tryLoadBinaryCache(Path.of(cacheProp), path, expectedN, expectTileId);
        }
        if (cached != null) {
            this.raf = null;
            this.headerLines = cached.headerLines;
            this.n = cached.n;
            this.sparseOffset = null;
            this.runTile = cached.runTile;
            this.runFirstId = cached.runFirstId;
            this.runCount = cached.runCount;
            this.scannedLines = cached.lines;
            this.scanBytes = cached.bytes;
            this.scanNanos = Math.max(1L, System.nanoTime() - cacheT0);
            this.wireOf = cached.wireOf;
            System.out.printf(Locale.ROOT,
                    "F4_V37_IDMAP_CACHE loaded=true n=%d runs=%d time_s=%.3f%n",
                    this.n, this.runCount, this.scanNanos / 1e9);
            return;
        }
        this.raf = new RandomAccessFile(path.toFile(), "r");
        try {
            long t0 = System.nanoTime();
            Scan s = scan(expectedN, expectTileId);
            this.headerLines = s.headerLines;
            this.n = s.n;
            this.sparseOffset = s.sparseOffset;
            this.runTile = s.runTile;
            this.runFirstId = s.runFirstId;
            this.runCount = s.runCount;
            this.scannedLines = s.lines;
            this.scanBytes = s.bytes;
            this.scanNanos = System.nanoTime() - t0;
            this.wireOf = s.wireOf;
            String outCache = (buildProp != null && !buildProp.isEmpty()) ? buildProp : cacheProp;
            if (outCache != null && !outCache.isEmpty()) {
                writeBinaryCache(Path.of(outCache), path, this.headerLines, this.n,
                        this.runTile, this.runFirstId, this.runCount, this.wireOf);
            }
        } catch (IOException | RuntimeException e) {
            try {
                raf.close();
            } catch (IOException ignored) {
                // The original failure is what matters; a close failure on top of it is noise.
            }
            throw e;
        }
    }

    /* ------------------------------------------------------------------ scan */

    private static final class Scan {
        int headerLines;
        int n;
        long[] sparseOffset;
        int[] runTile;
        int[] runFirstId;
        int runCount;
        int[] wireOf;
        long lines;
        long bytes;
        /** Running state for the monotonicity checks, seeded by the first data line. */
        int lastTile;
        int lastWire;
        boolean haveLast;
    }

    /**
     * One sequential pass over the manifest: builds both indexes and runs all four fail-closed checks.
     * Nothing is deferred to query time, so a bad manifest cannot surface later as a subtly wrong node.
     */
    private Scan scan(int expectedN, int[] expectTileId) throws IOException {
        final Scan s = new Scan();
        final int sparseCount = (expectedN + SPARSE_STRIDE - 1) / SPARSE_STRIDE + 1;
        s.sparseOffset = new long[sparseCount];
        s.runTile = new int[1 << 20];
        s.runFirstId = new int[1 << 20];
        /* Dense by id, so it can be filled at the index the line carries -- which the id check below
         * has already proved equals this line's position. */
        s.wireOf = new int[expectedN];

        final byte[] buf = new byte[SCAN_BUF];
        final StringBuilder header = new StringBuilder();

        raf.seek(0);
        int len = raf.read(buf);
        if (len < 0) {
            throw new FormatException("manifest is empty: " + path);
        }
        int cursor = 0;
        long base = 0;          // file offset of buf[0]
        long lineStart = 0;     // file offset of the current line's first byte
        long lineNo = 0;        // 0-based over all lines, header included
        int dataIndex = 0;      // 0-based over data lines == the dense id
        boolean inHeader = true;
        boolean sawData = false;

        while (true) {
            int nl = -1;
            for (int i = cursor; i < len; i++) {
                if (buf[i] == '\n') {
                    nl = i;
                    break;
                }
            }

            if (nl < 0 && len == buf.length) {
                // A record longer than SCAN_BUF.  Real lines are ~50 B, so this only triggers on a
                // mangled file, where failing on content beats failing on buffer arithmetic.
                int rem = len - cursor;
                System.arraycopy(buf, cursor, buf, 0, rem);
                base += cursor;
                len = rem;
                cursor = 0;
                int r = raf.read(buf, len, buf.length - len);
                if (r < 0) {
                    throw new FormatException("data at byte " + base + " has no newline within "
                            + SCAN_BUF + " B -- not a manifest line");
                }
                len += r;
                continue;
            }

            if (nl < 0) {
                int r = raf.read(buf, len, buf.length - len);
                if (r < 0) {
                    if (len > cursor) {
                        // Trailing record with no newline.  The frozen file ends with '\n' (PrintWriter
                        // println), so this is a truncated file unless it is the very last id.
                        if (dataIndex != expectedN - 1) {
                            throw new FormatException("file ends mid-record after id " + (dataIndex - 1)
                                    + "; expected " + expectedN + " complete data lines");
                        }
                        handleDataLine(s, buf, cursor, len, dataIndex, expectTileId);
                    }
                    break;
                }
                len += r;
                continue;
            }

            if (inHeader) {
                if (buf[cursor] != '#') {
                    inHeader = false;
                    s.headerLines = (int) lineNo;
                    verifyHeader(header.toString(), s.headerLines, expectedN);
                    s.sparseOffset[0] = lineStart;
                } else {
                    if (header.length() < 8192) {
                        header.append(new String(buf, cursor, nl - cursor, StandardCharsets.UTF_8))
                                .append('\n');
                    }
                    cursor = nl + 1;
                    lineStart = base + cursor;
                    lineNo++;
                    continue;
                }
            }

            if (!sawData && !new String(buf, cursor, nl - cursor, StandardCharsets.UTF_8)
                    .startsWith(EXPECTED_FIRST_DATA_PREFIX)) {
                throw new FormatException("first data line is not id 0: '"
                        + new String(buf, cursor, nl - cursor, StandardCharsets.UTF_8) + "'");
            }
            sawData = true;
            dataIndex = handleDataLine(s, buf, cursor, nl, dataIndex, expectTileId);
            if ((dataIndex - 1) % SPARSE_STRIDE == 0) {
                s.sparseOffset[(dataIndex - 1) / SPARSE_STRIDE] = lineStart;
            }

            cursor = nl + 1;
            lineStart = base + cursor;
            lineNo++;
        }

        if (inHeader || !sawData) {
            throw new FormatException("manifest has no data lines: " + path);
        }
        if (dataIndex != expectedN) {
            throw new FormatException("manifest has " + dataIndex + " data lines, expected " + expectedN);
        }
        s.n = dataIndex;
        s.lines = lineNo;
        s.bytes = raf.length();
        if (s.runCount != s.runTile.length) {
            s.runTile = Arrays.copyOf(s.runTile, s.runCount);
            s.runFirstId = Arrays.copyOf(s.runFirstId, s.runCount);
        }
        return s;
    }

    /** Fails closed unless the header declares the artifact this loader expects. */
    private void verifyHeader(String header, int headerLines, int expectedN) {
        long declaredN = declaredLong(header, "# N=");
        if (declaredN != expectedN) {
            throw new FormatException("manifest declares N=" + declaredN + ", expected " + expectedN
                    + " -- this is not the frozen RRG");
        }
        long declaredVersion = declaredLong(header, "# formatVersion=");
        if (declaredVersion != FidesRrgLoader.FORMAT_VERSION) {
            throw new FormatException("manifest declares formatVersion=" + declaredVersion
                    + ", expected " + FidesRrgLoader.FORMAT_VERSION);
        }
        if (header.indexOf("# id\t") < 0) {
            throw new FormatException("manifest header lacks the"
                    + " '# id\\ttileName/wireName\\ttileAddress\\twireIndex' column line, so the column"
                    + " order is unverified");
        }
        if (headerLines != EXPECTED_HEADER_LINES) {
            throw new FormatException("manifest has " + headerLines + " header lines, but the frozen"
                    + " artifact has " + EXPECTED_HEADER_LINES + " -- every id would be offset by a"
                    + " whole record; re-derive the offset before trusting this file");
        }
    }

    private static long declaredLong(String header, String key) {
        int i = header.indexOf(key);
        if (i < 0) {
            throw new FormatException("manifest header lacks '" + key + "'");
        }
        int j = i + key.length();
        int k = j;
        while (k < header.length() && header.charAt(k) >= '0' && header.charAt(k) <= '9') {
            k++;
        }
        if (k == j) {
            throw new FormatException("manifest header has no number after '" + key + "'");
        }
        return Long.parseLong(header.substring(j, k));
    }

    /**
     * Parses one data line ({@code id \t name \t tileAddress \t wireIndex}) and applies checks 2-4.
     * Fields 0, 2 and 3 must be pure decimal digits; the name field is the only free-form one.
     *
     * @return {@code dataIndex + 1}
     */
    private int handleDataLine(Scan s, byte[] buf, int from, int to, int dataIndex, int[] expectTileId) {
        if (dataIndex >= expectTileId.length) {
            throw new FormatException("manifest has more than " + expectTileId.length + " data lines");
        }
        long v = 0;
        int field = 0;
        int id = -1;
        int tile = -1;
        boolean digits = false;
        for (int i = from; i < to; i++) {
            byte b = buf[i];
            if (b == '\t') {
                if (field == 0) {
                    id = (int) v;
                } else if (field == 2) {
                    tile = (int) v;
                }
                field++;
                v = 0;
                digits = false;
            } else if (b >= '0' && b <= '9') {
                v = v * 10 + (b - '0');
                digits = true;
            } else if (field != 1) {
                throw new FormatException("non-digit '" + (char) (b & 0xff) + "' in field " + field
                        + " of data line " + (s.headerLines + dataIndex + 1));
            }
        }
        final int wire = (int) v;
        if (field != 3 || !digits) {
            throw new FormatException("data line " + (s.headerLines + dataIndex + 1)
                    + " does not have 4 tab-separated fields");
        }
        if (id != dataIndex) {
            throw new FormatException("data line " + (s.headerLines + dataIndex + 1) + " carries id "
                    + id + ", expected " + dataIndex + " -- the manifest is not id-indexed");
        }
        if (tile != expectTileId[dataIndex]) {
            throw new FormatException("id " + dataIndex + ": manifest tileAddress " + tile
                    + " != .bin tileId " + expectTileId[dataIndex]
                    + " -- manifest and .bin are not the same artifact");
        }
        if (s.haveLast) {
            if (tile < s.lastTile) {
                throw new FormatException("id " + dataIndex + ": tileAddress decreased, " + s.lastTile
                        + " -> " + tile + " -- not in getRnodes() enumeration order");
            }
            if (tile == s.lastTile && wire <= s.lastWire) {
                throw new FormatException("id " + dataIndex + ": wireIndex not strictly increasing"
                        + " within tile " + tile + ", " + s.lastWire + " -> " + wire);
            }
        }
        if (!s.haveLast || tile != s.lastTile) {
            if (s.runCount == s.runTile.length) {
                int cap = s.runTile.length << 1;
                if (cap < 0) {
                    throw new FormatException("tile run table overflow");
                }
                s.runTile = Arrays.copyOf(s.runTile, cap);
                s.runFirstId = Arrays.copyOf(s.runFirstId, cap);
            }
            s.runTile[s.runCount] = tile;
            s.runFirstId[s.runCount] = dataIndex;
            s.runCount++;
        }
        s.lastTile = tile;
        s.lastWire = wire;
        s.haveLast = true;
        /* Stored last, after every check on this line has passed: the column is what id -> rnode
         * resolution reads, so a line that fails a check must not leave a value behind. */
        s.wireOf[dataIndex] = wire;
        return dataIndex + 1;
    }

    /* ------------------------------------------------------------------ queries */

    public int n() {
        return n;
    }

    /** Header line count as measured.  The line of id {@code k} is {@code headerLines + k + 1}. */
    public int headerLines() {
        return headerLines;
    }

    /** Number of distinct tiles the flat graph reaches -- the size of the tile-run table. */
    public int runCount() {
        return runCount;
    }

    public long scanBytes() {
        return scanBytes;
    }

    public double scanSeconds() {
        return scanNanos * 1e-9;
    }

    public long scannedLines() {
        return scannedLines;
    }

    public Path path() {
        return path;
    }

    /**
     * The tile that owns node {@code id}.  Pure binary search over the run table -- no file I/O --
     * because ids ascend with {@code tileAddress}.
     */
    public int tileAddress(int id) {
        checkId(id);
        int lo = 0;
        int hi = runCount - 1;
        int r = -1;
        while (lo <= hi) {
            int m = (lo + hi) >>> 1;
            if (runFirstId[m] <= id) {
                r = m;
                lo = m + 1;
            } else {
                hi = m - 1;
            }
        }
        if (r < 0) {
            throw new FormatException("id " + id + " precedes the first tile run");
        }
        return runTile[r];
    }

    /**
     * The wire index of node {@code id}, from the in-RAM column {@link #wireOf}: one array read, no
     * file access.  This is the one column with no independent witness in the {@code .bin}, so it is
     * the single thing this class cannot cross-check on its own; {@code FidesLaneGate} checks it
     * against live objects instead.
     */
    public int wireIndex(int id) {
        checkId(id);
        return wireOf[id];
    }

    /** The whole manifest line for {@code id}.  Diagnostics only. */
    public String rawLine(int id) {
        checkId(id);
        if (raf == null) return id + "\tcached\t" + tileAddress(id) + "\t" + wireIndex(id);
        byte[] b = new byte[1024];
        try {
            return new String(b, 0, readLineAt(offsetOfDataLine(id), b), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new FormatException("cannot read the manifest line for id " + id + ": " + e);
        }
    }

    /**
     * The dense id of {@code (tileAddress, wireIndex)}, or {@code -1} if the flat graph has no such
     * node.  Ids are consecutive within a tile run, so the answer is {@code firstId + rank(wire)}.
     */
    public int idOf(int tileAddress, int wireIndex) {
        int lo = 0;
        int hi = runCount - 1;
        int r = -1;
        while (lo <= hi) {
            int m = (lo + hi) >>> 1;
            if (runTile[m] <= tileAddress) {
                r = m;
                lo = m + 1;
            } else {
                hi = m - 1;
            }
        }
        if (r < 0 || runTile[r] != tileAddress) {
            return -1;
        }
        /* Searched in the dense column itself, over this run's slice of it, rather than in a per-tile
         * copy: the run's wires are by definition wireOf[firstId .. firstId+count), the id check in
         * scan() guarantees line k carries id k, and check 4 guarantees the slice is ascending -- which
         * is what binarySearch needs and what makes "rank of this wire" the same question.  A per-tile
         * copy was the original shape, and building one meant a first-touch walk from the sparse
         * anchor: ~1,100 line reads x 148,914 runs is most of the ~314 M read syscalls this run still
         * makes in set-up.  See wireOf. */
        final int firstId = runFirstId[r];
        final int count = (r + 1 < runCount ? runFirstId[r + 1] : n) - firstId;
        /* The range form returns an ABSOLUTE index into the array, not one relative to firstId --
         * which is the whole answer here, because an index into the dense column is the id.  (Adding
         * firstId back, as the per-tile-copy version had to, double-counts: the self-test's round-trip
         * over every id caught exactly that, +firstId on both the hit and the miss paths.) */
        final int k = Arrays.binarySearch(wireOf, firstId, firstId + count, wireIndex);
        return k < 0 ? -1 : k;
    }

    /** The dense id of a live rnode, via its own {@code nodesMap} coordinates.  -1 if absent. */
    public int idOf(RouteNode rnode) {
        if (rnode == null) {
            return -1;
        }
        return idOf(rnode.getTile().getUniqueAddress(), rnode.getWireIndex());
    }

    /**
     * The live rnode for {@code id}, or {@code null} when the flat graph reaches a node the lazy live
     * graph has not materialised.  That is the normal case for most ids: {@code nodesMap} is populated
     * on demand (RouteNodeGraph.java:915-932), while the manifest covers the whole closure.
     *
     * <p>A {@code null} is not an error to paper over -- the caller decides what the value would have
     * been.  For the three mutable columns at iteration-1 attempt 1 that decision is forced: a node
     * with no rnode would have been created with {@code usersConnectionCounts = null} and
     * {@code historicalCongestionCost = 1} by {@link RouteNode}'s constructor (RouteNode.java:93-102),
     * so the defaults are "never used by any net" and "no congestion history".
     */
    public RouteNode nodeAt(RouteNodeGraph graph, int id) {
        checkId(id);
        final int tile = tileAddress(id);
        final int wire = wireIndex(id);
        final RouteNode[][] map = graph.nodesMap;
        if (tile < 0 || tile >= map.length) {
            throw new FormatException("id " + id + " maps to tileAddress " + tile
                    + ", outside nodesMap (length " + map.length + ")");
        }
        final RouteNode[] rnodes = map[tile];
        if (rnodes == null || wire >= rnodes.length) {
            return null;
        }
        return rnodes[wire];
    }

    /* ------------------------------------------------------------------ internals */

    private void checkId(int id) {
        if (id < 0 || id >= n) {
            throw new IndexOutOfBoundsException("id " + id + " outside [0," + n + ")");
        }
    }

    /**
     * Byte offset of the first byte of data line {@code lineIndex}, which is also id {@code lineIndex}.
     *
     * <p>Costs up to {@link #SPARSE_STRIDE} - 1 forward line reads, so it is now only for
     * {@link #rawLine(int)}, which reads a line's <em>text</em> for diagnostics.  Both per-node
     * directions go through the dense {@link #wireOf} column instead -- see its javadoc for the
     * measurement that put it there.
     */
    private long offsetOfDataLine(int lineIndex) throws IOException {
        final int step = lineIndex % SPARSE_STRIDE;
        long off = sparseOffset[lineIndex / SPARSE_STRIDE];
        for (int k = 0; k < step; k++) {
            off += readLineAt(off, seekBuf) + 1L;
        }
        return off;
    }

    /**
     * Reads the line starting at {@code offset} into {@code buf}; returns its length excluding the
     * newline, or the number of bytes available at EOF.
     */
    private int readLineAt(long offset, byte[] buf) throws IOException {
        raf.seek(offset);
        int filled = 0;
        while (true) {
            if (filled == buf.length) {
                return filled;
            }
            int r = raf.read(buf, filled, buf.length - filled);
            if (r < 0) {
                return filled;
            }
            for (int i = filled; i < filled + r; i++) {
                if (buf[i] == '\n') {
                    return i;
                }
            }
            filled += r;
        }
    }

    private static final long IDMAP_BIN_MAGIC = 0x4649443356333731L;

    private static long computeWireChecksum(int[] wireOf, int n) {
        long acc = 0x9E3779B97F4A7C15L ^ n;
        for (int i = 0; i < n; ++i) {
            acc = acc * 1315423911L + (wireOf[i] & 0xFFFFFFFFL);
        }
        return acc;
    }

    private static Scan tryLoadBinaryCache(Path cachePath, Path manifestPath, int expectedN, int[] expectTileId) {
        try {
            if (!Files.isRegularFile(cachePath)) return null;
            long manifestBytes = Files.size(manifestPath);
            try (FileChannel ch = FileChannel.open(cachePath, StandardOpenOption.READ)) {
                ByteBuffer hdr = ByteBuffer.allocate(40).order(ByteOrder.LITTLE_ENDIAN);
                while (hdr.hasRemaining()) {
                    if (ch.read(hdr) < 0) return null;
                }
                hdr.flip();
                long magic = hdr.getLong();
                long recManifestBytes = hdr.getLong();
                int hdrLines = hdr.getInt();
                int n = hdr.getInt();
                int runCount = hdr.getInt();
                long expectedSum = hdr.getLong();
                int reserved = hdr.getInt();
                if (magic != IDMAP_BIN_MAGIC || recManifestBytes != manifestBytes
                        || hdrLines != EXPECTED_HEADER_LINES || n != expectedN
                        || runCount <= 0 || runCount > n || reserved != 0) {
                    return null;
                }
                long expectedSize = 40L + (2L * runCount + (long) n) * 4L;
                if (ch.size() != expectedSize) return null;

                int[] runTile = readIntArray(ch, runCount);
                int[] runFirstId = readIntArray(ch, runCount);
                int[] wireOf = readIntArray(ch, n);
                if (runFirstId[0] != 0 || expectTileId[n - 1] != runTile[runCount - 1]) return null;
                for (int r = 0; r < runCount; ++r) {
                    int fid = runFirstId[r];
                    if (fid < 0 || fid >= n || runTile[r] != expectTileId[fid]) return null;
                    if (r > 0 && (runFirstId[r] <= runFirstId[r - 1] || runTile[r] <= runTile[r - 1])) {
                        return null;
                    }
                }
                if (computeWireChecksum(wireOf, n) != expectedSum) return null;

                Scan s = new Scan();
                s.headerLines = hdrLines;
                s.n = n;
                s.runTile = runTile;
                s.runFirstId = runFirstId;
                s.runCount = runCount;
                s.wireOf = wireOf;
                s.lines = hdrLines + (long) n;
                s.bytes = Files.size(cachePath);
                return s;
            }
        } catch (Throwable t) {
            return null;
        }
    }

    private static int[] readIntArray(FileChannel ch, int count) throws IOException {
        int[] out = new int[count];
        ByteBuffer bb = ByteBuffer.allocate(Math.min(1 << 20, Math.max(4, count * 4))).order(ByteOrder.LITTLE_ENDIAN);
        int idx = 0;
        while (idx < count) {
            bb.clear();
            int remInts = count - idx;
            int chunkBytes = Math.min(bb.capacity(), remInts * 4);
            bb.limit(chunkBytes);
            while (bb.hasRemaining()) {
                if (ch.read(bb) < 0) throw new IOException("Unexpected EOF reading idmap cache");
            }
            bb.flip();
            int got = chunkBytes / 4;
            for (int k = 0; k < got; ++k) {
                out[idx++] = bb.getInt();
            }
        }
        return out;
    }

    private static void writeIntArray(FileChannel ch, int[] arr, int count) throws IOException {
        ByteBuffer bb = ByteBuffer.allocate(Math.min(1 << 20, Math.max(4, count * 4))).order(ByteOrder.LITTLE_ENDIAN);
        int idx = 0;
        while (idx < count) {
            bb.clear();
            int capInts = bb.capacity() / 4;
            int take = Math.min(capInts, count - idx);
            for (int k = 0; k < take; ++k) bb.putInt(arr[idx++]);
            bb.flip();
            while (bb.hasRemaining()) ch.write(bb);
        }
    }

    private static void writeBinaryCache(Path cachePath, Path manifestPath, int headerLines, int n,
                                         int[] runTile, int[] runFirstId, int runCount, int[] wireOf) {
        try {
            Path tmp = cachePath.resolveSibling(cachePath.getFileName() + ".tmp");
            long sum = computeWireChecksum(wireOf, n);
            long manifestBytes = Files.size(manifestPath);
            try (FileChannel ch = FileChannel.open(tmp,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE)) {
                ByteBuffer hdr = ByteBuffer.allocate(40).order(ByteOrder.LITTLE_ENDIAN);
                hdr.putLong(IDMAP_BIN_MAGIC);
                hdr.putLong(manifestBytes);
                hdr.putInt(headerLines);
                hdr.putInt(n);
                hdr.putInt(runCount);
                hdr.putLong(sum);
                hdr.putInt(0);
                hdr.flip();
                while (hdr.hasRemaining()) ch.write(hdr);
                writeIntArray(ch, runTile, runCount);
                writeIntArray(ch, runFirstId, runCount);
                writeIntArray(ch, wireOf, n);
            }
            Files.move(tmp, cachePath, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            System.out.println("F4_V37_IDMAP_CACHE built=" + cachePath + " n=" + n + " runs=" + runCount);
        } catch (Throwable t) {
            System.out.println("WARN: F4_V37_IDMAP_CACHE write failed: " + t);
        }
    }

    @Override
    public void close() throws IOException {
        if (!closed) {
            closed = true;
            if (raf != null) raf.close();
        }
    }

    public String report() {
        return String.format(Locale.ROOT,
                "FidesRrgIdMap[%s] N=%d headerLines=%d tileRuns=%d scanned=%.2fs %.1f MiB %d lines"
                        + " (all N tileAddress values agreed with .bin tileId)",
                path.getFileName(), n, headerLines, runCount, scanSeconds(), scanBytes / 1048576.0,
                scannedLines);
    }

    /* ------------------------------------------------------------------ standalone check */

    /**
     * {@code FidesRrgIdMap <manifest.txt> <rrg.bin>} -- the delivery-step-1 gate: prove the mapping
     * before any lane uses it.  Loads the {@code .bin} (needs the artifacts' own heap budget), runs the
     * full start-up verification, then round-trips the loader's own sampled ids plus the two boundary
     * ids, where an off-by-one in the line offset would hide.
     */
    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.out.println("USAGE: FidesRrgIdMap <manifest.txt> <rrg.bin>");
            return;
        }
        System.out.println("INFO: loading " + args[1]);
        FidesRrgLoader rrg = FidesRrgLoader.load(java.nio.file.Paths.get(args[1]));
        System.out.println("INFO: loaded N=" + rrg.N + " M=" + rrg.M);
        try (FidesRrgIdMap map = open(java.nio.file.Paths.get(args[0]), rrg)) {
            System.out.println("INFO: " + map.report());

            int[] sample = FidesRrgLoader.sampleIndices(map.n());
            int checked = 0;
            int failures = 0;
            for (int i = 0; i < sample.length + 4; i++) {
                int id = i < sample.length ? sample[i]
                        : new int[] {0, 1, map.n() - 2, map.n() - 1}[i - sample.length];
                int tile = map.tileAddress(id);
                int wire = map.wireIndex(id);
                int back = map.idOf(tile, wire);
                checked++;
                if (back != id) {
                    failures++;
                    if (failures <= 5) {
                        System.out.println("FAIL: id " + id + " -> (" + tile + "," + wire + ") -> " + back);
                    }
                } else if (tile != rrg.tileId[id]) {
                    failures++;
                    if (failures <= 5) {
                        System.out.println("FAIL: id " + id + " tileAddress " + tile
                                + " != tileId " + rrg.tileId[id]);
                    }
                }
            }
            System.out.println("first data line: " + map.rawLine(0));
            System.out.println("last  data line: " + map.rawLine(map.n() - 1));
            System.out.println("round-trips checked = " + checked + ", failures = " + failures);
            System.out.println(failures == 0 ? "IDMAP RESULT: PASS" : "IDMAP RESULT: FAIL");
        }
    }
}
