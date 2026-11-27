"""v94 on v93 Java stack + header-only idmap-cached preload + 64K tileAddress lookup hint."""
import sys
from pathlib import Path

_V93_DIR = str(Path(__file__).resolve().parent.parent / "v93")
if _V93_DIR not in sys.path:
    sys.path.insert(0, _V93_DIR)

import v93_patch as v93  # noqa: E402


def once(source, anchor, updated):
    return v93.once(source, anchor, updated)


def cuda(src):
    return v93.cuda(src)


def node(src):
    return v93.node(src)


def broker(src):
    return v93.broker(src)


def diversion(src):
    return v93.diversion(src)


def f2prewarm(src):
    return v93.f2prewarm(src)


def handoff(src):
    return v93.handoff(src)


def worker(src):
    return v93.worker(src)


def partial_router(src):
    return v93.partial_router(src)


def corridor(src):
    return v93.corridor(src)


def rrg_loader(src):
    src = v93.rrg_loader(src)
    src = once(
        src,
        """            final Map<String, int[]> ints = new LinkedHashMap<>();
            final Map<String, float[]> floats = new LinkedHashMap<>();
            ints.put("tileId", readInts(ch, byName.get("tileId")));
            floats.put("presentCongestionCosts", readFloats(ch, byName.get("presentCongestionCosts")));
            long pfCount = 0L;
            if (countIntentOrd >= 0) {
                pfCount = countIntMatches(ch, byName.get("intentCode"), countIntentOrd);
            }
            return new FidesRrgLoader(N, M, version, fileSize, sections, ints, floats, pfCount);
        }
    }""",
        """            final Map<String, int[]> ints = new LinkedHashMap<>();
            final Map<String, float[]> floats = new LinkedHashMap<>();
            ints.put("tileId", readInts(ch, byName.get("tileId")));
            floats.put("presentCongestionCosts", readFloats(ch, byName.get("presentCongestionCosts")));
            long pfCount = (countIntentOrd >= 0) ? countIntMatches(ch, byName.get("intentCode"), countIntentOrd) : -1L;
            return new FidesRrgLoader(N, M, version, fileSize, sections, ints, floats, pfCount);
        }
    }

    public static FidesRrgLoader loadHeaderAndPcc(Path bin) throws IOException {
        try (RandomAccessFile raf = new RandomAccessFile(bin.toFile(), "r");
             FileChannel ch = raf.getChannel()) {
            final long fileSize = ch.size();
            if (fileSize < HEADER_SIZE) {
                throw new FormatException("file shorter than the 24-byte header: " + fileSize);
            }
            final ByteBuffer hb = ByteBuffer.wrap(readAt(ch, 0, HEADER_SIZE)).order(ByteOrder.LITTLE_ENDIAN);
            final byte[] magicBytes = new byte[8];
            hb.get(magicBytes);
            final String magic = new String(magicBytes, StandardCharsets.US_ASCII);
            if (!MAGIC.equals(magic)) {
                throw new FormatException("bad magic " + magic + ", expected " + MAGIC);
            }
            final int version = hb.getInt();
            final int N = hb.getInt();
            final int M = hb.getInt();
            final int sectionCount = hb.getInt();
            if (version != FORMAT_VERSION) {
                throw new FormatException("format version " + version + ", expected " + FORMAT_VERSION);
            }
            if (N <= 0 || M < N) {
                throw new FormatException("implausible N=" + N + " M=" + M);
            }
            if (sectionCount <= 0 || sectionCount > 256) {
                throw new FormatException("implausible sectionCount=" + sectionCount);
            }
            final byte[] tbl = readAt(ch, HEADER_SIZE, sectionCount * SECTION_ENTRY_SIZE);
            final ByteBuffer tb = ByteBuffer.wrap(tbl).order(ByteOrder.LITTLE_ENDIAN);
            final List<Section> sections = new ArrayList<>(sectionCount);
            final Map<String, Section> byName = new LinkedHashMap<>();
            for (int s = 0; s < sectionCount; s++) {
                final byte[] nameBytes = new byte[NAME_LEN];
                tb.get(nameBytes);
                int end = 0;
                while (end < NAME_LEN && nameBytes[end] != 0) end++;
                final String name = new String(nameBytes, 0, end, StandardCharsets.US_ASCII);
                final int dtype = tb.get() & 0xff;
                final int rank = tb.get() & 0xff;
                tb.getShort();
                tb.getInt();
                final long count = tb.getLong();
                final long offset = tb.getLong();
                if (rank != 1 || (dtype != DT_I32 && dtype != DT_F32)) {
                    throw new FormatException("invalid section " + name);
                }
                final Section sec = new Section(name, dtype, rank, count, offset);
                if (byName.put(name, sec) != null) throw new FormatException("duplicate section " + name);
                sections.add(sec);
            }
            checkTableIsTight(sections, fileSize);
            for (Object[] col : N_COLUMNS) {
                require(byName, (String) col[0], (Integer) col[1], N);
            }
            require(byName, "offset", DT_I32, (long) N + 1);
            require(byName, "edge", DT_I32, M);
            require(byName, "presentCongestionCosts", DT_F32, 256L);
            if (byName.containsKey("sameNetCount") || sections.size() != N_COLUMNS.length + 3) {
                throw new FormatException("unexpected sections in header load");
            }
            final Map<String, int[]> ints = new LinkedHashMap<>();
            final Map<String, float[]> floats = new LinkedHashMap<>();
            floats.put("presentCongestionCosts", readFloats(ch, byName.get("presentCongestionCosts")));
            return new FidesRrgLoader(N, M, version, fileSize, sections, ints, floats, -1L);
        }
    }""",
    )
    return src


def idmap(src):
    src = v93.idmap(src)
    src = once(
        src,
        """    private final int[] wireOf;

    private final long scanBytes;""",
        """    private final int[] wireOf;
    private static final int HINT_SHIFT = 11;
    private final int[] runHint;

    private static int[] buildRunHint(int n, int[] runFirstId, int runCount) {
        if (n <= 0 || runCount <= 0 || runFirstId == null) {
            return new int[0];
        }
        int hintLen = (n >>> HINT_SHIFT) + 2;
        int[] hint = new int[hintLen];
        int r = 0;
        for (int b = 0; b < hintLen; ++b) {
            long target = ((long) b) << HINT_SHIFT;
            while (r + 1 < runCount && (long) runFirstId[r + 1] <= target) {
                ++r;
            }
            hint[b] = r;
        }
        return hint;
    }

    private final long scanBytes;""",
    )
    src = once(
        src,
        """    private FidesRrgIdMap(Path path, int expectedN, int[] expectTileId) throws IOException {
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
            this.wireOf = s.wireOf;""",
        """    private FidesRrgIdMap(Path path, int expectedN, int[] expectTileId) throws IOException {
        if (expectedN <= 0) {
            throw new IllegalArgumentException("expectedN " + expectedN);
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
            this.runHint = buildRunHint(this.n, this.runFirstId, this.runCount);
            System.out.printf(Locale.ROOT,
                    "F4_V37_IDMAP_CACHE loaded=true n=%d runs=%d time_s=%.3f%n",
                    this.n, this.runCount, this.scanNanos / 1e9);
            return;
        }
        if (expectTileId == null || expectTileId.length < expectedN) {
            throw new IllegalArgumentException("expectTileId shorter than N=" + expectedN);
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
            this.runHint = buildRunHint(this.n, this.runFirstId, this.runCount);""",
    )
    src = once(
        src,
        """    public int tileAddress(int id) {
        checkId(id);
        int lo = 0;
        int hi = runCount - 1;
        int r = -1;""",
        """    public int tileAddress(int id) {
        checkId(id);
        int lo = 0;
        int hi = runCount - 1;
        if (runHint != null && runHint.length > 0) {
            int b = id >>> HINT_SHIFT;
            if (b < runHint.length) lo = runHint[b];
            if (b + 1 < runHint.length) hi = Math.min(hi, runHint[b + 1]);
        }
        int r = -1;""",
    )
    src = once(
        src,
        """                if (runFirstId[0] != 0 || expectTileId[n - 1] != runTile[runCount - 1]) return null;
                for (int r = 0; r < runCount; ++r) {
                    int fid = runFirstId[r];
                    if (fid < 0 || fid >= n || runTile[r] != expectTileId[fid]) return null;""",
        """                if (runFirstId[0] != 0 || (expectTileId != null && expectTileId[n - 1] != runTile[runCount - 1])) return null;
                for (int r = 0; r < runCount; ++r) {
                    int fid = runFirstId[r];
                    if (fid < 0 || fid >= n || (expectTileId != null && runTile[r] != expectTileId[fid])) return null;""",
    )
    return src


def gate(src):
    src = v93.gate(src)
    src = once(
        src,
        """    static void preloadStaticSlim(String rrgPath, String manifestPath, int pinfeedOrd) throws IOException {
        if (rrgPath == null || manifestPath == null) return;
        PreloadedStatic cur = v67Preloaded;
        if (cur != null && cur.matches(rrgPath, manifestPath)) return;
        FidesRrgLoader loadedRrg = FidesRrgLoader.loadSlim(Paths.get(rrgPath), pinfeedOrd);
        FidesRrgIdMap loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), loadedRrg);
        v67Preloaded = new PreloadedStatic(rrgPath, manifestPath, loadedRrg, loadedMap);
    }""",
        """    static void preloadStaticSlim(String rrgPath, String manifestPath, int pinfeedOrd) throws IOException {
        if (rrgPath == null || manifestPath == null) return;
        PreloadedStatic cur = v67Preloaded;
        if (cur != null && cur.matches(rrgPath, manifestPath)) return;
        FidesRrgLoader loadedRrg = null;
        FidesRrgIdMap loadedMap = null;
        String idCache = System.getProperty("fides.v37.idmapCache");
        if (idCache != null && !idCache.isEmpty() && Files.isRegularFile(Path.of(idCache))) {
            try {
                FidesRrgLoader hdrRrg = FidesRrgLoader.loadHeaderAndPcc(Paths.get(rrgPath));
                loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), hdrRrg);
                loadedRrg = hdrRrg;
            } catch (Throwable ignored) {
                loadedRrg = null;
                loadedMap = null;
            }
        }
        if (loadedRrg == null) {
            loadedRrg = FidesRrgLoader.loadSlim(Paths.get(rrgPath), -1);
            loadedMap = FidesRrgIdMap.open(Paths.get(manifestPath), loadedRrg);
        }
        v67Preloaded = new PreloadedStatic(rrgPath, manifestPath, loadedRrg, loadedMap);
    }""",
    )
    src = once(
        src,
        """        long artifactPinfeed = 0;
        if (rrg.slimPinfeedCount >= 0L) {
            artifactPinfeed = rrg.slimPinfeedCount;
        } else {
            int pfOrd = IntentCode.NODE_PINFEED.ordinal();
            for (int i = 0; i < rrg.N; i++) {
                if (rrg.intentCode[i] == pfOrd) {
                    artifactPinfeed++;
                }
            }
        }""",
        """        long artifactPinfeed = 0;
        if (rrg.slimPinfeedCount >= 0L) {
            artifactPinfeed = rrg.slimPinfeedCount;
        } else if (rrg.intentCode != null) {
            int pfOrd = IntentCode.NODE_PINFEED.ordinal();
            for (int i = 0; i < rrg.N; i++) {
                if (rrg.intentCode[i] == pfOrd) {
                    artifactPinfeed++;
                }
            }
        } else {
            artifactPinfeed = -1L;
        }""",
    )
    return src


def router(src):
    return v93.router(src)
