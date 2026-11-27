package com.xilinx.rapidwright.rwroute;

import java.io.EOFException;
import java.io.IOException;
import java.io.PrintStream;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * Loads the flattened RRG dump ({@code fides_rrg_v2_*.bin}, 6.53 GiB) into plain Java arrays on the
 * heap, and proves the load was faithful.
 *
 * <p>Both halves matter.  The dump is the GPU kernel's entire input: if a section lands at the wrong
 * offset, or an {@code f32} section is read as {@code i32}, or the endianness is wrong, every
 * downstream comparison against the golden is meaningless and the error is silent -- the arrays are
 * still full of plausible numbers.  So this class does not just parse; it re-derives cross-section
 * identities that can only hold if several arrays were each read correctly and consistently, and it
 * emits a digest table that an independent reader ({@code rrg_digest.py}, numpy) must reproduce
 * exactly.  The gate is {@code diff} on those two tables, not a green log line.
 *
 * <h2>Why not MappedByteBuffer on the hot path</h2>
 * Not a performance preference -- a hard limit.  {@link java.nio.MappedByteBuffer} is a
 * {@link ByteBuffer}, so its capacity is an {@code int}: one mapping covers at most
 * {@code Integer.MAX_VALUE} bytes (2 GiB).  This file is 6,531 MiB, so it <em>cannot be mapped in
 * one piece at all</em>; index arithmetic would have to become {@code map[i >>> 30]} across four
 * mappings.  (The 64-bit-mapping escape hatch is FFM's {@code MemorySegment}, which only became
 * final in Java 22 -- this project builds on Java 11.  That is the same finding as the plan's
 * "the first rung of the fallback ladder is empty on Java 11".)  Plain {@code int[]} also lets the
 * JIT hoist bounds checks and keep loads unboxed, which a {@code ByteBuffer} accessor cannot.
 *
 * <p>The read path is: one reusable direct {@link ByteBuffer} of {@link #CHUNK_BYTES}, filled by
 * positional reads, then a bulk {@code IntBuffer}/{@code FloatBuffer} transfer into the destination
 * array.  Chunked rather than one giant read because {@code FileChannel.read} on a heap buffer
 * allocates a temporary direct buffer the size of the remainder, which for {@code edge} would be a
 * second 1.4 GiB allocation.
 *
 * <h2>Layout (measured, not assumed)</h2>
 * <pre>
 *   header        24 B   magic[8] version:u32 N:u32 M:u32 sectionCount:u32   (all little-endian)
 *   section table 56 B x sectionCount, starting at 24
 *                        name[32] dtype:u8 rank:u8 pad:u16 pad:u32 count:u64 offset:u64
 *   payload       sections back to back, 4 B per element, last section ends exactly at EOF
 * </pre>
 * dtype 0 = i32, 1 = f32.  Element counts are fixed by the format: fifteen columns of {@code N}, one
 * of {@code N+1} ({@code offset}), one of {@code M} ({@code edge}), one of 256
 * ({@code presentCongestionCosts}) -- so the payload is exactly {@code (16N + M + 257) * 4} bytes.
 *
 * <p>Note what is <em>not</em> in the dump: {@code sameNetCount} ({@code countConnectionsOfUser} for
 * the current net).  It drifts per connection, so it cannot be snapshotted -- the lane must fill it
 * per dispatch.  {@link #load} refuses a file that grows a section it does not know, which keeps a
 * future format change from being ignored.
 */
public final class FidesRrgLoader {

    public static final String MAGIC = "FIDESRRG";
    public static final int FORMAT_VERSION = 2;
    public static final int HEADER_SIZE = 24;
    public static final int SECTION_ENTRY_SIZE = 56;
    public static final int NAME_LEN = 32;

    public static final int DT_I32 = 0;
    public static final int DT_F32 = 1;

    /** Frozen by step2; a mismatch means the CSV/host are not looking at the frozen artifact. */
    public static final int EXPECTED_N = 87_040_026;
    public static final int EXPECTED_M = 361_396_479;
    public static final int EXPECTED_SECTION_COUNT = 18;

    /** RouteNodeType.SUPER_LONG_LINE ordinal, used by the endTileY identity below. */
    private static final int RT_SUPER_LONG_LINE = 5;
    private static final int EXPECTED_SLL_COUNT = 69_120;

    private static final int CHUNK_BYTES = 8 << 20;

    /* ------------------------------------------------------------------ columns */

    /** count == N.  Read in file order; the field order here mirrors the section table. */
    public final int[] nodeType;
    public final int[] intentCode;
    public final int[] tileType;
    public final int[] tileId;
    public final int[] endTileX;
    /** Resolved at dump time.  Kept as the host-side oracle; the kernel re-derives it per arc. */
    public final int[] endTileY;
    /** The raw private field -- the other operand of the arc-local rule.  Kernel-visible. */
    public final int[] endTileYRaw;
    public final int[] tileX;
    public final int[] tileY;
    public final int[] length;
    public final float[] baseCost;
    public final float[] delay;
    public final int[] isArcLocked;
    public final int[] occupancy;

    /** count == N+1.  Children of node i are edge[offset[i] .. offset[i+1]). */
    public final int[] offset;
    /** count == M.  Order is getChildren() order and must never be re-sorted. */
    public final int[] edge;

    public final float[] presentCongestionCosts;
    public final float[] historicalCongestionCost;

    public final int N;
    public final int M;
    public final int version;
    public final long fileSize;
    public final long slimPinfeedCount;

    /** Section table in file order, for digesting and for provenance. */
    public final List<Section> sections;

    /* ------------------------------------------------------------------ section table */

    public static final class Section {
        public final String name;
        public final int dtype;
        public final int rank;
        public final long count;
        public final long offset;

        Section(String name, int dtype, int rank, long count, long offset) {
            this.name = name;
            this.dtype = dtype;
            this.rank = rank;
            this.count = count;
            this.offset = offset;
        }

        public long end() {
            return offset + 4L * count;
        }

        public String dtypeName() {
            return dtype == DT_I32 ? "i32" : dtype == DT_F32 ? "f32" : ("?" + dtype);
        }

        @Override
        public String toString() {
            return String.format("%s %s count=%d offset=%d end=%d", name, dtypeName(), count, offset, end());
        }
    }

    /** Columns of length N that must be present, with the dtype the kernel expects. */
    private static final Object[][] N_COLUMNS = {
        {"nodeType", DT_I32}, {"intentCode", DT_I32}, {"tileType", DT_I32}, {"tileId", DT_I32},
        {"endTileX", DT_I32}, {"endTileY", DT_I32}, {"endTileYRaw", DT_I32}, {"tileX", DT_I32},
        {"tileY", DT_I32}, {"length", DT_I32}, {"baseCost", DT_F32}, {"delay", DT_F32},
        {"isArcLocked", DT_I32}, {"historicalCongestionCost", DT_F32}, {"occupancy", DT_I32},
    };

    private FidesRrgLoader(int N, int M, int version, long fileSize, List<Section> sections,
                           Map<String, int[]> ints, Map<String, float[]> floats) {
        this(N, M, version, fileSize, sections, ints, floats, -1L);
    }

    private FidesRrgLoader(int N, int M, int version, long fileSize, List<Section> sections,
                           Map<String, int[]> ints, Map<String, float[]> floats, long slimPinfeedCount) {
        this.slimPinfeedCount = slimPinfeedCount;
        this.N = N;
        this.M = M;
        this.version = version;
        this.fileSize = fileSize;
        this.sections = sections;
        this.nodeType = ints.get("nodeType");
        this.intentCode = ints.get("intentCode");
        this.tileType = ints.get("tileType");
        this.tileId = ints.get("tileId");
        this.endTileX = ints.get("endTileX");
        this.endTileY = ints.get("endTileY");
        this.endTileYRaw = ints.get("endTileYRaw");
        this.tileX = ints.get("tileX");
        this.tileY = ints.get("tileY");
        this.length = ints.get("length");
        this.baseCost = floats.get("baseCost");
        this.delay = floats.get("delay");
        this.isArcLocked = ints.get("isArcLocked");
        this.occupancy = ints.get("occupancy");
        this.offset = ints.get("offset");
        this.edge = ints.get("edge");
        this.presentCongestionCosts = floats.get("presentCongestionCosts");
        this.historicalCongestionCost = floats.get("historicalCongestionCost");
    }

    /* ------------------------------------------------------------------ loading */

    /** Any structural or format violation.  Fail-closed: the caller must not proceed. */
    public static final class FormatException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        public FormatException(String msg) {
            super(msg);
        }
    }

    public static FidesRrgLoader loadSlim(Path bin, int countIntentOrd) throws IOException {
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
                throw new FormatException("unexpected sections in slim load");
            }
            final Map<String, int[]> ints = new LinkedHashMap<>();
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
    }

    private static long countIntMatches(FileChannel ch, Section sec, int target) throws IOException {
        final int n = (int) sec.count;
        final ByteBuffer bb = ByteBuffer.allocateDirect(Math.min(CHUNK_BYTES, Math.max(4, n * 4)))
                .order(ByteOrder.LITTLE_ENDIAN);
        final int[] tmp = new int[bb.capacity() / 4];
        ch.position(sec.offset);
        int readDone = 0;
        long hits = 0L;
        while (readDone < n) {
            final int take = Math.min(n - readDone, tmp.length);
            bb.clear();
            bb.limit(take * 4);
            while (bb.hasRemaining()) {
                if (ch.read(bb) < 0) throw new EOFException("short read in " + sec.name);
            }
            bb.flip();
            bb.asIntBuffer().get(tmp, 0, take);
            for (int i = 0; i < take; ++i) {
                if (tmp[i] == target) ++hits;
            }
            readDone += take;
        }
        return hits;
    }

    public static FidesRrgLoader load(Path bin) throws IOException {
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
                throw new FormatException("format version " + version + ", expected " + FORMAT_VERSION
                        + " (version 2 is the one carrying endTileYRaw)");
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
                while (end < NAME_LEN && nameBytes[end] != 0) {
                    end++;
                }
                final String name = new String(nameBytes, 0, end, StandardCharsets.US_ASCII);
                final int dtype = tb.get() & 0xff;
                final int rank = tb.get() & 0xff;
                tb.getShort();      // reserved
                tb.getInt();        // reserved
                final long count = tb.getLong();
                final long offset = tb.getLong();
                if (rank != 1) {
                    throw new FormatException("section " + name + " has rank " + rank + "; only rank 1 is defined");
                }
                if (dtype != DT_I32 && dtype != DT_F32) {
                    throw new FormatException("section " + name + " has unknown dtype " + dtype);
                }
                final Section sec = new Section(name, dtype, rank, count, offset);
                if (byName.put(name, sec) != null) {
                    throw new FormatException("duplicate section name " + name);
                }
                sections.add(sec);
            }

            checkTableIsTight(sections, fileSize);

            // Fail-closed on anything unexpected in either direction: a section we need that is
            // missing, or a section we have never heard of (which could be one we would need).
            for (Object[] col : N_COLUMNS) {
                final String name = (String) col[0];
                final int want = (Integer) col[1];
                final Section sec = byName.get(name);
                if (sec == null) {
                    throw new FormatException("missing required column " + name);
                }
                if (sec.count != N) {
                    throw new FormatException(name + " has count " + sec.count + ", expected N=" + N);
                }
                if (sec.dtype != want) {
                    throw new FormatException(name + " is " + sec.dtypeName() + ", expected "
                            + (want == DT_I32 ? "i32" : "f32"));
                }
            }
            require(byName, "offset", DT_I32, (long) N + 1);
            require(byName, "edge", DT_I32, M);
            require(byName, "presentCongestionCosts", DT_F32, 256L);
            if (byName.containsKey("sameNetCount")) {
                throw new FormatException("dump carries sameNetCount, which this loader does not expect: "
                        + "it drifts per connection and must not be snapshotted (layout doc section 2.2)");
            }
            final int known = N_COLUMNS.length + 3;
            if (known != EXPECTED_SECTION_COUNT) {
                throw new FormatException("loader's own section inventory is " + known
                        + " but the frozen format has " + EXPECTED_SECTION_COUNT);
            }
            if (sections.size() != known) {
                final List<String> extra = new ArrayList<>();
                for (Section s : sections) {
                    if (!isKnown(s.name)) {
                        extra.add(s.name);
                    }
                }
                throw new FormatException("section count is " + sections.size() + ", expected " + known
                        + "; unknown sections: " + extra);
            }

            final Map<String, int[]> ints = new LinkedHashMap<>();
            final Map<String, float[]> floats = new LinkedHashMap<>();
            for (Section sec : sections) {
                if (sec.dtype == DT_I32) {
                    ints.put(sec.name, readInts(ch, sec));
                } else {
                    floats.put(sec.name, readFloats(ch, sec));
                }
            }
            return new FidesRrgLoader(N, M, version, fileSize, sections, ints, floats);
        }
    }

    private static boolean isKnown(String name) {
        for (Object[] col : N_COLUMNS) {
            if (col[0].equals(name)) {
                return true;
            }
        }
        return name.equals("offset") || name.equals("edge") || name.equals("presentCongestionCosts");
    }

    private static void require(Map<String, Section> byName, String name, int dtype, long count) {
        final Section sec = byName.get(name);
        if (sec == null) {
            throw new FormatException("missing required section " + name);
        }
        if (sec.dtype != dtype) {
            throw new FormatException(name + " is " + sec.dtypeName() + ", expected "
                    + (dtype == DT_I32 ? "i32" : "f32"));
        }
        if (sec.count != count) {
            throw new FormatException(name + " has count " + sec.count + ", expected " + count);
        }
    }

    /**
     * Structural proof that the table describes the file exactly: sections contiguous, in ascending
     * order, starting right after the table, ending exactly at EOF, every offset 4-byte aligned.
     * This needs no external reference, and it is what catches an off-by-one in the table stride or
     * a stale table from a partially-written dump.
     */
    private static void checkTableIsTight(List<Section> sections, long fileSize) {
        long expect = HEADER_SIZE + (long) sections.size() * SECTION_ENTRY_SIZE;
        for (Section s : sections) {
            if ((s.offset & 3L) != 0) {
                throw new FormatException(s.name + " offset " + s.offset + " is not 4-byte aligned");
            }
            if (s.offset != expect) {
                throw new FormatException("section " + s.name + " starts at " + s.offset
                        + ", expected " + expect + " (gap or overlap in the section table)");
            }
            if (s.count < 0) {
                throw new FormatException(s.name + " has negative count " + s.count);
            }
            expect = s.end();
        }
        if (expect != fileSize) {
            throw new FormatException("payload ends at " + expect + " but file is " + fileSize
                    + " bytes (truncated or padded dump)");
        }
    }

    private static byte[] readAt(FileChannel ch, long off, int len) throws IOException {
        final ByteBuffer b = ByteBuffer.allocate(len);
        ch.position(off);
        while (b.hasRemaining()) {
            if (ch.read(b) < 0) {
                throw new EOFException("short read at " + off);
            }
        }
        return b.array();
    }

    private static int[] readInts(FileChannel ch, Section sec) throws IOException {
        if (sec.count > Integer.MAX_VALUE) {
            throw new FormatException(sec.name + " has " + sec.count + " elements, over the array limit");
        }
        final int n = (int) sec.count;
        final int[] dst = new int[n];
        final ByteBuffer bb = ByteBuffer.allocateDirect(Math.min(CHUNK_BYTES, Math.max(4, n * 4)))
                .order(ByteOrder.LITTLE_ENDIAN);
        ch.position(sec.offset);
        int written = 0;
        while (written < n) {
            final int take = Math.min(n - written, bb.capacity() / 4);
            bb.clear();
            bb.limit(take * 4);
            while (bb.hasRemaining()) {
                if (ch.read(bb) < 0) {
                    throw new EOFException("short read in " + sec.name + " at element " + written);
                }
            }
            bb.flip();
            // asIntBuffer() inherits this buffer's byte order -- measured, not assumed: a probe on
            // a direct LITTLE_ENDIAN buffer holding {01 00 00 00} returned 1, while the BIG_ENDIAN
            // control returned 16777216.  (IntBuffer exposes only the order() getter, so relying on
            // inheritance is also the only option.)
            bb.asIntBuffer().get(dst, written, take);
            written += take;
        }
        return dst;
    }

    private static float[] readFloats(FileChannel ch, Section sec) throws IOException {
        if (sec.count > Integer.MAX_VALUE) {
            throw new FormatException(sec.name + " has " + sec.count + " elements, over the array limit");
        }
        final int n = (int) sec.count;
        final float[] dst = new float[n];
        final ByteBuffer bb = ByteBuffer.allocateDirect(Math.min(CHUNK_BYTES, Math.max(4, n * 4)))
                .order(ByteOrder.LITTLE_ENDIAN);
        ch.position(sec.offset);
        int written = 0;
        while (written < n) {
            final int take = Math.min(n - written, bb.capacity() / 4);
            bb.clear();
            bb.limit(take * 4);
            while (bb.hasRemaining()) {
                if (ch.read(bb) < 0) {
                    throw new EOFException("short read in " + sec.name + " at element " + written);
                }
            }
            bb.flip();
            bb.asFloatBuffer().get(dst, written, take);   // inherits LITTLE_ENDIAN, see readInts
            written += take;
        }
        return dst;
    }

    /**
     * "Is this the file we think it is", as opposed to "is this file well-formed".
     *
     * <p>Deliberately not called from {@link #load}: keeping format validation and artifact identity
     * separate is what lets {@code FidesRrgLoaderSelfTest} exercise the parser and the readers on
     * synthetic kilobyte-sized files, which is the only affordable way to run negative controls
     * (corrupt an offset, swap two sections' payloads, truncate) against the read path.
     */
    public void verifyFrozenArtifact() {
        if (version != FORMAT_VERSION) {
            throw new FormatException("format version " + version + ", expected " + FORMAT_VERSION);
        }
        if (N != EXPECTED_N || M != EXPECTED_M) {
            throw new FormatException("N/M are " + N + "/" + M + ", expected "
                    + EXPECTED_N + "/" + EXPECTED_M + " (frozen step2 artifact)");
        }
        if (sections.size() != EXPECTED_SECTION_COUNT) {
            throw new FormatException("section count " + sections.size()
                    + ", expected " + EXPECTED_SECTION_COUNT);
        }
        // Routed here rather than into checkSemantics on purpose: the SLL *count* is a property of
        // this device's graph, not of a well-formed dump, so a synthetic kilobyte-sized file can
        // satisfy every semantic identity while having three SLL nodes instead of 69,120.
        if (countNodeType(RT_SUPER_LONG_LINE) != EXPECTED_SLL_COUNT) {
            throw new FormatException("SUPER_LONG_LINE count is " + countNodeType(RT_SUPER_LONG_LINE)
                    + ", expected " + EXPECTED_SLL_COUNT);
        }
    }

    public int countNodeType(int type) {
        int c = 0;
        for (int t : nodeType) {
            if (t == type) {
                c++;
            }
        }
        return c;
    }

    /* ------------------------------------------------------------------ digests */

    /**
     * A whole-array digest, computed over the elements' unsigned 32-bit bit patterns so that
     * {@code i32} and {@code f32} sections are digested the same way and NaN payloads cannot hide.
     *
     * <p>Fields are chosen to be cheap on both sides and sensitive to the failure modes that matter:
     * {@code sum} and {@code xor} catch a wrong offset, a wrong section, or a byte swap;
     * {@code minU}/{@code maxU}/{@code nz} catch truncation and misalignment; {@code fnv} is computed
     * over a deterministic spread of <em>(index, value)</em> pairs, so it is positional and catches a
     * pure shift that could in principle leave a sum unchanged.
     *
     * <p><b>What this digest cannot see, by construction: dtype.</b> Because it hashes raw bit
     * patterns, a section read as {@code i32} and the same section read as {@code f32} produce
     * <em>identical</em> digests. That is the price of making NaN payloads and signed zeros unable to
     * hide, and it means Java-Python digest agreement is not evidence that any {@code f32} column was
     * decoded as a float. Dtype is pinned elsewhere instead: {@link #load} compares the dtype byte in
     * the section table against the expected one, and the
     * {@code presentCongestionCosts == [1..256]} identity discriminates -- read as {@code i32},
     * {@code 1.0f} is {@code 0x3F800000} = 1065353216, not 1. (The other float columns do not
     * discriminate: {@code baseCost > 0} also holds as an integer, and {@code delay == 0} holds
     * either way.)
     */
    public static final class Digest {
        public final String name;
        public final long count;
        public final long sum;
        public final int xor;
        public final long nz;
        public final long minU;
        public final long maxU;
        public final long fnv;

        Digest(String name, long count, long sum, int xor, long nz, long minU, long maxU, long fnv) {
            this.name = name;
            this.count = count;
            this.sum = sum;
            this.xor = xor;
            this.nz = nz;
            this.minU = minU;
            this.maxU = maxU;
            this.fnv = fnv;
        }

        /** One line, stable format, diffable against rrg_digest.py's output. */
        public String line() {
            return String.format("DIGEST %-26s count=%-12d sum=%-22s xor=%-12s nz=%-12d minU=%-12d maxU=%-12d fnv=%016x",
                    name, count, Long.toUnsignedString(sum), Integer.toUnsignedString(xor),
                    nz, minU, maxU, fnv);
        }
    }

    private static final long FNV_OFFSET = 0xcbf29ce484222325L;
    private static final long FNV_PRIME = 0x100000001b3L;

    /** Deterministic, index-spread sample of positions; identical on the Python side. */
    public static int[] sampleIndices(int n) {
        final TreeSet<Integer> set = new TreeSet<>();
        for (int i = 0; i < 16 && i < n; i++) {
            set.add(i);
        }
        for (int i = Math.max(0, n - 16); i < n; i++) {
            set.add(i);
        }
        for (int k = 1; k < 2048; k++) {
            final long idx = (long) k * n / 2048;
            if (idx < n) {
                set.add((int) idx);
            }
        }
        final int[] out = new int[set.size()];
        int i = 0;
        for (int v : set) {
            out[i++] = v;
        }
        return out;
    }

    static long fnvStep(long h, long index, long u) {
        for (int b = 0; b < 8; b++) {
            h = (h ^ ((index >>> (8 * b)) & 0xFFL)) * FNV_PRIME;
        }
        for (int b = 0; b < 4; b++) {
            h = (h ^ ((u >>> (8 * b)) & 0xFFL)) * FNV_PRIME;
        }
        return h;
    }

    public static Digest digest(String name, int[] a) {
        long sum = 0, nz = 0;
        int xor = 0;
        long minU = 0xFFFFFFFFL, maxU = 0;
        for (int i = 0; i < a.length; i++) {
            final int v = a[i];
            final long u = v & 0xFFFFFFFFL;
            sum += u;
            xor ^= v;
            if (v != 0) {
                nz++;
            }
            if (u < minU) {
                minU = u;
            }
            if (u > maxU) {
                maxU = u;
            }
        }
        long h = FNV_OFFSET;
        for (int idx : sampleIndices(a.length)) {
            h = fnvStep(h, idx, a[idx] & 0xFFFFFFFFL);
        }
        return new Digest(name, a.length, sum, xor, nz, minU, maxU, h);
    }

    public static Digest digest(String name, float[] a) {
        long sum = 0, nz = 0;
        int xor = 0;
        long minU = 0xFFFFFFFFL, maxU = 0;
        for (int i = 0; i < a.length; i++) {
            final int bits = Float.floatToRawIntBits(a[i]);
            final long u = bits & 0xFFFFFFFFL;
            sum += u;
            xor ^= bits;
            if (bits != 0) {
                nz++;
            }
            if (u < minU) {
                minU = u;
            }
            if (u > maxU) {
                maxU = u;
            }
        }
        long h = FNV_OFFSET;
        for (int idx : sampleIndices(a.length)) {
            h = fnvStep(h, idx, Float.floatToRawIntBits(a[idx]) & 0xFFFFFFFFL);
        }
        return new Digest(name, a.length, sum, xor, nz, minU, maxU, h);
    }

    /** Digest of the section's bytes as they sit in the file, via a direct positional read. */
    public static Digest digestFromFile(FileChannel ch, Section sec) throws IOException {
        if (sec.dtype == DT_I32) {
            return digest(sec.name, readInts(ch, sec));
        }
        return digest(sec.name, readFloats(ch, sec));
    }

    public List<Digest> allDigests() {
        final List<Digest> out = new ArrayList<>(sections.size());
        for (Section s : sections) {
            out.add(digestOf(s.name));
        }
        return out;
    }

    /** The loaded column, by name.  Convenience for the host and for the self-test. */
    public int[] intColumn(String name) {
        switch (name) {
            case "nodeType": return nodeType;
            case "intentCode": return intentCode;
            case "tileType": return tileType;
            case "tileId": return tileId;
            case "endTileX": return endTileX;
            case "endTileY": return endTileY;
            case "endTileYRaw": return endTileYRaw;
            case "tileX": return tileX;
            case "tileY": return tileY;
            case "length": return length;
            case "isArcLocked": return isArcLocked;
            case "occupancy": return occupancy;
            case "offset": return offset;
            case "edge": return edge;
            default: throw new FormatException(name + " is not an i32 column");
        }
    }

    public float[] floatColumn(String name) {
        switch (name) {
            case "baseCost": return baseCost;
            case "delay": return delay;
            case "historicalCongestionCost": return historicalCongestionCost;
            case "presentCongestionCosts": return presentCongestionCosts;
            default: throw new FormatException(name + " is not an f32 column");
        }
    }

    public Digest digestOf(String name) {
        for (Section s : sections) {
            if (s.name.equals(name)) {
                return s.dtype == DT_I32
                        ? digest(name, intColumn(name))
                        : digest(name, floatColumn(name));
            }
        }
        throw new FormatException("no such section " + name);
    }

    /* ------------------------------------------------------------------ semantic checks */

    /**
     * Identities that span several arrays.  Their value here is not that they re-check what
     * {@code verify_rrg.py} already checked -- it is that they cannot hold unless the loader put
     * every one of those arrays in the right place with the right dtype.  A per-section digest can
     * be right while the arrays are cross-wired; these cannot.
     */
    public void checkSemantics(PrintStream out, int[] fails) {
        // CSR self-consistency: ties offset and edge together.
        check(out, fails, "offset[0] == 0", offset[0] == 0, "offset[0]=" + offset[0]);
        check(out, fails, "offset[N] == M", offset[N] == M, "offset[N]=" + offset[N] + " M=" + M);
        boolean mono = true;
        long degSum = 0;
        int maxDeg = 0, minDeg = Integer.MAX_VALUE, zeroDeg = 0;
        for (int i = 0; i < N; i++) {
            final int d = offset[i + 1] - offset[i];
            if (d < 0) {
                mono = false;
            }
            degSum += d;
            if (d > maxDeg) {
                maxDeg = d;
            }
            if (d < minDeg) {
                minDeg = d;
            }
            if (d == 0) {
                zeroDeg++;
            }
        }
        check(out, fails, "offset monotonic non-decreasing", mono, "");
        check(out, fails, "sum(out-degree) == M", degSum == M, "sum=" + degSum + " M=" + M);
        int edgeLo = Integer.MAX_VALUE, edgeHi = Integer.MIN_VALUE;
        for (int e : edge) {
            if (e < edgeLo) {
                edgeLo = e;
            }
            if (e > edgeHi) {
                edgeHi = e;
            }
        }
        check(out, fails, "all edge targets in [0, N)", edgeLo >= 0 && edgeHi < N,
                "range=[" + edgeLo + ", " + edgeHi + "]");
        out.printf("         out-degree min=%d max=%d mean=%.3f zero-degree=%d%n",
                minDeg, maxDeg, (double) M / N, zeroDeg);

        // Section D: the congestion table must be the materialised [1..256], not the raw zeros that
        // RouteNodeGraph leaves before the routing loop (layout doc).
        boolean pccOk = presentCongestionCosts.length == 256;
        for (int i = 0; pccOk && i < 256; i++) {
            pccOk = presentCongestionCosts[i] == (i + 1);
        }
        check(out, fails, "presentCongestionCosts == [1..256] (materialised)", pccOk,
                "first=" + presentCongestionCosts[0] + " last=" + presentCongestionCosts[255]);

        boolean bcPos = true;
        float bcMin = Float.MAX_VALUE;
        for (float v : baseCost) {
            if (!(v > 0)) {
                bcPos = false;
            }
            if (v < bcMin) {
                bcMin = v;
            }
        }
        check(out, fails, "baseCost > 0 everywhere", bcPos, "min=" + bcMin);

        boolean dlyZero = true;
        for (float v : delay) {
            if (v != 0) {
                dlyZero = false;
                break;
            }
        }
        check(out, fails, "delay all zero (droppable column)", dlyZero, "");

        // The cross-array identity that ties FIVE sections together: nodeType, endTileY,
        // endTileYRaw, tileY, length.  endTileY can only differ from the raw field when the node is
        // a SUPER_LONG_LINE, and for those |raw - tileY| == length exactly.
        int sll = 0, differ = 0, differNonSll = 0, sllBad = 0;
        for (int i = 0; i < N; i++) {
            final boolean isSll = nodeType[i] == RT_SUPER_LONG_LINE;
            if (isSll) {
                sll++;
                if (Math.abs((long) endTileYRaw[i] - tileY[i]) != Math.abs((long) length[i])) {
                    sllBad++;
                }
            }
            if (endTileYRaw[i] != endTileY[i]) {
                differ++;
                if (!isSll) {
                    differNonSll++;
                }
            }
        }
        check(out, fails, "endTileYRaw != endTileY implies SUPER_LONG_LINE", differNonSll == 0,
                differNonSll + " non-SLL violations");
        check(out, fails, "SLL |endTileYRaw - tileY| == length", sllBad == 0, sllBad + " mismatches");
        out.printf("         SLL=%d, endTileYRaw!=endTileY at %d (dump-time prev held: %d)%n",
                sll, differ, differ);

        int occNz = 0, occMax = 0;
        for (int v : occupancy) {
            if (v != 0) {
                occNz++;
                if (v > occMax) {
                    occMax = v;
                }
            }
        }
        out.printf("         occupancy nonzero=%d max=%d%n", occNz, occMax);
    }

    private static void check(PrintStream out, int[] fails, String label, boolean ok, String detail) {
        out.printf("  [%s] %s%s%n", ok ? "PASS" : "FAIL", label,
                (detail == null || detail.isEmpty()) ? "" : " -- " + detail);
        if (!ok) {
            fails[0]++;
        }
    }

    /* ------------------------------------------------------------------ main */

    public static void main(String[] args) throws IOException {
        if (args.length < 1) {
            System.err.println("usage: FidesRrgLoader <rrg.bin> [--semantics-only] [--digests-only]"
                    + " [--any-identity]");
            System.exit(2);
        }
        final Path bin = Path.of(args[0]);
        final PrintStream out = System.out;
        final int[] fails = {0};
        boolean semanticsOnly = false;
        boolean digestsOnly = false;
        boolean anyIdentity = false;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--semantics-only": semanticsOnly = true; break;
                case "--digests-only": digestsOnly = true; break;
                case "--any-identity": anyIdentity = true; break;
                default: throw new IllegalArgumentException("unknown option " + args[i]);
            }
        }

        long t0 = System.nanoTime();
        final FidesRrgLoader r = load(bin);
        if (!anyIdentity) {
            r.verifyFrozenArtifact();
        }
        long t1 = System.nanoTime();
        out.printf("loaded %s in %.2f s%n", bin, (t1 - t0) / 1e9);
        out.printf("  N=%,d M=%,d version=%d sections=%d fileSize=%,d%n",
                r.N, r.M, r.version, r.sections.size(), r.fileSize);
        for (Section s : r.sections) {
            out.printf("    %-26s %-4s count=%,13d offset=%,15d%n",
                    s.name, s.dtypeName(), s.count, s.offset);
        }

        if (!digestsOnly) {
            out.println();
            out.println("== cross-array identities ==");
            r.checkSemantics(out, fails);
            if (semanticsOnly) {
                out.printf("%nRESULT: %s%n", fails[0] == 0 ? "ALL CHECKS PASSED" : (fails[0] + " FAILED"));
                System.exit(fails[0] == 0 ? 0 : 1);
            }
        }

        final long t2 = System.nanoTime();
        out.println();
        out.println("== digests ==");
        for (Digest d : r.allDigests()) {
            out.println(d.line());
        }
        final long t3 = System.nanoTime();
        out.printf("digest pass: %.2f s%n", (t3 - t2) / 1e9);

        out.printf("%nRESULT: %s%n", fails[0] == 0 ? "ALL CHECKS PASSED" : (fails[0] + " FAILED"));
        if (fails[0] != 0) {
            System.exit(1);
        }
    }
}
