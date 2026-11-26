package com.xilinx.rapidwright.rwroute;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

final class F4FeatureDirect {
    private static final int ROW_BYTES = 72;
    private final ByteBuffer rows;
    private final int count;
    private long offered;
    private long accepted;
    private long rejected;
    private long unsupported;
    private long missing;
    private long sameSlr;
    private long crossSlr;

    static F4FeatureDirect open(RWRouteConfig config) {
        String path = System.getProperty("fides.ppt.f4.features");
        if (path == null) return null;
        if (config.isTimingDriven() || config.isFidesLane() || !config.isFidesLaneMirror()) {
            throw new IllegalArgumentException("Direct F4 GPU requires non-timing ordered routing and the mirror");
        }
        try {
            return new F4FeatureDirect(Path.of(path));
        } catch (IOException error) {
            throw new IllegalStateException("Cannot open the GPU-generated feature table", error);
        }
    }

    private F4FeatureDirect(Path path) throws IOException {
        try (var file = FileChannel.open(path, StandardOpenOption.READ)) {
            long size = file.size();
            if (size <= 0 || size % ROW_BYTES != 0 || size > Integer.MAX_VALUE) {
                throw new IOException("Unexpected feature table size");
            }
            count = (int)(size / ROW_BYTES);
            rows = file.map(FileChannel.MapMode.READ_ONLY, 0, size).order(ByteOrder.LITTLE_ENDIAN);
        }
        System.out.printf("F4_DIRECT_INPUT featureRows=%d cpuSearchBudget=0%n", count);
    }

    private int offsetOf(int id) {
        if (id >= 0 && id < count && rows.getLong(id * ROW_BYTES) == id) return id * ROW_BYTES;
        int left = 0, right = count;
        while (left < right) {
            int mid = (left + right) >>> 1;
            int offset = mid * ROW_BYTES;
            long value = rows.getLong(offset);
            if (value < id) left = mid + 1;
            else if (value > id) right = mid;
            else return offset;
        }
        return -1;
    }

    long f21EffectiveWork(Connection connection, int width, int height) {
        int offset = offsetOf(connection.hashCode());
        if (offset < 0) return -1;
        long area = rows.getLong(offset + 24), span = rows.getLong(offset + 32);
        long effectiveArea = Math.min(area, (long) width * height);
        long effectiveSpan = Math.min(span, (long) width + height);
        long score = rows.getLong(offset + 64) & 0x7fffffffL;
        return Math.max(1, score - area - 32 * span + effectiveArea + 32 * effectiveSpan);
    }

    long offer(Connection connection, boolean supported) {
        int offset = offsetOf(connection.hashCode());
        if (offset < 0) {
            ++missing;
            return -1;
        }
        if (!supported) {
            ++unsupported;
            return -1;
        }
        long cross = rows.getLong(offset + 56);
        if (cross < 0 || cross > 1) throw new IllegalStateException("Invalid CUDA SLR feature");
        if (cross == 0) ++sameSlr;
        else ++crossSlr;
        ++offered;
        return rows.getLong(offset + 64) & 0x7fffffffL;
    }

    void finish(boolean acceptedPath) {
        if (acceptedPath) ++accepted;
        else ++rejected;
        if (offered <= 3 || offered % 1_000 == 0) report("F4_DIRECT_PROGRESS");
    }

    void report(String event) {
        System.out.printf("%s offered=%d pathAccepted=%d rejected=%d unsupported=%d missingFeature=%d initialSameSlr=%d initialCrossSlr=%d%n",
                event, offered, accepted, rejected, unsupported, missing, sameSlr, crossSlr);
    }
}
