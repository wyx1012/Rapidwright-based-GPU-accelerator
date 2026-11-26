package com.xilinx.rapidwright.rwroute;

import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Arrays;
import java.util.HashSet;
import org.json.JSONArray;
import org.json.JSONObject;

final class F4Theia50perCapture {
    static final String STOP = "F4_THEIA50PER_SCOPE_RIP_COMMITTED";
    private final Path dir;
    private FidesLane.Attempt selected;
    private long featureScore;

    F4Theia50perCapture(Path dir) {
        this.dir = dir;
    }

    private static void require(boolean ok, String message) {
        if (!ok) throw new IllegalStateException(message);
    }

    private void json(String name, JSONObject value) throws IOException {
        Path pending = step.resolve(name + ".pending");
        Files.writeString(pending, value.toString(2) + "\n", StandardOpenOption.CREATE_NEW);
        Files.move(pending, step.resolve(name), java.nio.file.StandardCopyOption.ATOMIC_MOVE);
    }

    private void dump(String name, int[] ints, float[] floats) throws IOException {
        int count = ints == null ? floats.length : ints.length;
        ByteBuffer buffer = ByteBuffer.allocate(1 << 20).order(ByteOrder.LITTLE_ENDIAN);
        try (FileChannel stream = FileChannel.open(dir.resolve(name), StandardOpenOption.CREATE_NEW,
                StandardOpenOption.WRITE)) {
            int at = 0;
            while (at < count) {
                buffer.clear();
                while (at < count && buffer.remaining() >= Integer.BYTES) {
                    buffer.putInt(ints == null ? Float.floatToRawIntBits(floats[at]) : ints[at]);
                    ++at;
                }
                buffer.flip();
                while (buffer.hasRemaining()) stream.write(buffer);
            }
        }
    }

    private static JSONObject rawAttempt(FidesLane.Attempt attempt) throws ReflectiveOperationException {
        JSONObject raw = new JSONObject();
        for (Field field : FidesLane.Attempt.class.getFields()) {
            if (Modifier.isStatic(field.getModifiers())) continue;
            Object value = field.get(attempt);
            raw.put(field.getName(), value instanceof int[] ? new JSONArray(value) : value);
            if (field.getType() == float.class) {
                raw.put(field.getName() + "_rawBits", Float.floatToRawIntBits(field.getFloat(attempt)));
            }
        }
        return raw;
    }

    private static Object mirrorColumn(FidesMutableMirror mirror, String name) throws ReflectiveOperationException {
        Field field = FidesMutableMirror.class.getDeclaredField(name);
        field.setAccessible(true);
        return field.get(mirror);
    }




    private Path step;
    private FidesMutableMirror mutableMirror;
    private final F4HostJournal journal = new F4HostJournal();
    private int lastIteration;
    private boolean v20GrowthEarned;
    private int phase;
    private long lastSealNs;
    long lastSealNs() { return lastSealNs; }
    private float[] firstPcc;
    private float[] lastPcc;

    boolean want(Connection c, int iteration) {
        return iteration >= lastIteration && !c.hasAltSinks()
                && c.getSinkRnode().getType().isAnyExclusiveSink()
                && c.getNetWrapper().getDoubleHpwl() > 0;
    }

    F4HostJournal journal() { return journal; }

    void markHistory(Iterable<RouteNode> overUsed) {
        overUsed.forEach(journal::mark);
    }

    private static void word(java.io.DataOutputStream out, int value) throws IOException {
        out.writeInt(Integer.reverseBytes(value));
    }

    void before(FidesLane.Attempt attempt,int iteration,FidesRrgLoader rrg,FidesMutableMirror mirror,
                float[] pcc,int[] slr,Connection c, long score, FidesRrgIdMap idMap) {
        require(selected==null && want(c, iteration), "PPT F4 ordered handoff");
        require(attempt.targetIds.length==1 && attempt.targetIds[0]==attempt.sinkId && !attempt.hasAltSinks,
                "Pilot needs one original target");
        boolean roundChanged = iteration != lastIteration;
        lastIteration = iteration;
        boolean refreshPcc = roundChanged || lastPcc == null || !Arrays.equals(lastPcc, pcc);
        featureScore = score;
        phase++;
        try {
            step=dir.resolve("step"+phase);
            int deltaRows=0,historyRows=0,occRows=0,sameRows=0;
            long sealStarted = System.nanoTime();
            if (phase==1) {
                mutableMirror=mirror;
                journal.bind((int[]) mirrorColumn(mirror, "occ"), (int[]) mirrorColumn(mirror, "same"),
                        (float[]) mirrorColumn(mirror, "hist"));
                journal.seal(c.getNetWrapper(), idMap, null);
                firstPcc = pcc.clone();
                lastPcc = pcc.clone();
                Files.createDirectory(dir);
                if (!Boolean.getBoolean("fides.ppt.f4.residentStaticRrg")) {
                    dump("offset.i32le",rrg.offset,null);
                    dump("edge.i32le",rrg.edge,null);
                }
                dump("occ.i32le",(int[])mirrorColumn(mirror,"occ"),null);
                dump("same.i32le",(int[])mirrorColumn(mirror,"same"),null);
                dump("hist.i32le",null,(float[])mirrorColumn(mirror,"hist"));
                dump("pcc.i32le",null,pcc);
                dump("slr.i32le",slr,null);
                Files.createDirectory(step);
            } else {
                require(mutableMirror == mirror, "Unexpected mirror change");
                Files.createDirectory(step);
                F4HostJournal.Rows rows;
                try (var out = new java.io.BufferedOutputStream(
                        Files.newOutputStream(step.resolve("between.bin"), StandardOpenOption.CREATE_NEW))) {
                    rows = journal.seal(c.getNetWrapper(), idMap, out);
                }
                deltaRows = rows.changed;
                occRows = rows.occupancy;
                sameRows = rows.sameNet;
                historyRows = rows.history;
                if (refreshPcc) {
                    try (var out = new java.io.DataOutputStream(new java.io.BufferedOutputStream(
                            Files.newOutputStream(step.resolve("new_pcc.bin"), StandardOpenOption.CREATE_NEW)))) {
                        for (float v : pcc) word(out, Float.floatToRawIntBits(v));
                    }
                    lastPcc = pcc.clone();
                }
            }
            lastSealNs = System.nanoTime() - sealStarted;
            sendBatchFrame("snapshot", new JSONObject().put("N",rrg.N).put("M",rrg.M)
                    .put("initial_pcc_words",pcc.length).put("iteration",iteration).put("was_routed",c.isRouted())
                    .put("attempt",rawAttempt(attempt)).put("delta_rows",deltaRows)
                    .put("history_rows",historyRows).put("occupancy_rows",occRows).put("same_rows",sameRows)
                    .put("refresh_pcc", refreshPcc).put("feature_score", featureScore)
                    .put("journal", journal.receipt()).put("v20_growth_earned", v20GrowthEarned));
        } catch (IOException | ReflectiveOperationException error) {
            throw new IllegalStateException("Cannot capture live iterative GPU attempt",error);
        }
        selected=attempt;
    }

    static final class BatchPath {
        private final RouteNode[] chain;
        private final int[] ids;
        private final int[] bits;

        BatchPath(RouteNode[] chain, int[] ids, int[] bits) {
            this.chain = chain;
            this.ids = ids;
            this.bits = bits;
        }

        RouteNode[] chain() { return chain; }
        int[] ids() { return ids; }

        void apply(int sequence) {
            for (int i = chain.length - 2; i >= 0; --i) {
                chain[i].setPrev(chain[i + 1]);
                chain[i].setUpstreamPathCost(Float.intBitsToFloat(bits[i]));
                chain[i].setVisited(sequence);
            }
        }
    }

    private BatchPath validate(JSONObject result, RouteNodeGraph graph, FidesRrgLoader rrg,
            FidesRrgIdMap ids, Connection connection, RWRoute router, RWRoute.ConnectionState state) {
            require(result.getInt("sequence") == selected.sequence
                    && result.getInt("source") == selected.sourceId && result.getInt("target") == selected.sinkId,
                    "GPU answer belongs to a different attempt");
            JSONArray path = result.getJSONArray("path"), costs = result.getJSONArray("partial_bits");
            JSONArray ingress = result.getJSONArray("ingress");
            int length = path.length();
            require(length >= 2 && costs.length() == length && ingress.length() == length && path.getInt(0) == selected.sinkId
                    && path.getInt(length - 1) == selected.sourceId && costs.getInt(length - 1) == 0,
                    "Invalid GPU path endpoints or cost vector");
            RouteNode[] chain = new RouteNode[length];
            chain[length - 1] = connection.getSourceRnode();
            require(ids.idOf(chain[length - 1]) == selected.sourceId && chain[length - 1].getPrev() == null,
                    "GPU path source changed");
            HashSet<Integer> seen = new HashSet<>();
            seen.add(selected.sourceId);
            for (int i = length - 2; i >= 0; --i) {
                int child = path.getInt(i);
                require(0 <= child && child < rrg.N && seen.add(child), "GPU path has an invalid or repeated node");
                for (RouteNode candidate : chain[i + 1].getChildren(graph)) {
                    if (ids.idOf(candidate) == child) {
                        chain[i] = candidate;
                        break;
                    }
                }
                require(chain[i] != null, "GPU path hop is outside the live RouteNode adjacency");
            }
            require(chain[0] == connection.getSinkRnode() && chain[0].isTarget(), "GPU path sink changed");
            require(ingress.getInt(length - 1) == -1, "Unexpected source ingress");
            RouteNode[] previous = new RouteNode[length];
            float[] upstream = new float[length];
            for (int i = 0; i < length; ++i) {
                previous[i] = chain[i].getPrev();
                upstream[i] = chain[i].getUpstreamPathCost();
            }
            int longLines = 0;
            try {
                for (int i = length - 2; i >= 0; --i) {
                    boolean longLine = chain[i].getType() == RouteNodeType.SUPER_LONG_LINE;
                    require(ingress.getInt(i) == (longLine ? path.getInt(i + 1) : -1),
                            "Wrong ingress at path node " + path.getInt(i));
                    if (longLine) ++longLines;
                    int bits = router.f4CheckLiveArc(state, chain[i + 1], chain[i]);
                    require(bits == costs.getInt(i), "Native float32 cost differs at " + path.getInt(i)
                            + ": native=" + bits + " GPU=" + costs.getInt(i));
                    chain[i].setUpstreamPathCost(Float.intBitsToFloat(bits));
                }
                require(connection.isCrossSLR() || longLines == 0, "Unexpected long line for same-SLR route");
            } finally {
                for (int i = 0; i < length - 1; ++i) {
                    if (previous[i] == null) chain[i].clearPrev();
                    else chain[i].setPrev(previous[i]);
                    chain[i].setUpstreamPathCost(upstream[i]);
                }
            }
        int[] pathIds = new int[length];
        int[] pathCosts = new int[length];
        for (int i = 0; i < length; ++i) {
            pathIds[i] = path.getInt(i);
            pathCosts[i] = costs.getInt(i);
        }
        return new BatchPath(chain, pathIds, pathCosts);
    }


    private java.io.BufferedReader bridgeInput;
    private java.io.BufferedWriter bridgeOutput;

    private void sendBatchFrame(String kind, JSONObject payload) throws IOException {
        if (bridgeOutput == null) {
            int port = Integer.getInteger("fides.ppt.f4.batchPort");
            var connection = new java.net.Socket(java.net.InetAddress.getByName("127.0.0.1"), port);
            connection.setTcpNoDelay(true);
            var utf8 = java.nio.charset.StandardCharsets.UTF_8;
            bridgeInput = new java.io.BufferedReader(new java.io.InputStreamReader(connection.getInputStream(), utf8));
            bridgeOutput = new java.io.BufferedWriter(new java.io.OutputStreamWriter(connection.getOutputStream(), utf8));
            sendBatchFrame("hello", new JSONObject().put("pid", ProcessHandle.current().pid())
                    .put("token", System.getenv("FIDES_V11_BATCH_TOKEN")));
        }
        bridgeOutput.write(new JSONObject().put("kind", kind).put("batch", phase)
                .put("payload", payload).toString());
        bridgeOutput.newLine();
        bridgeOutput.flush();
    }

    private JSONArray readBatchAnswers() throws IOException {
        String line = bridgeInput.readLine();
        if (line == null) throw new java.io.EOFException("GPU broker closed the batch socket");
        JSONObject frame = new JSONObject(line);
        require(frame.getString("kind").equals("answers") && frame.getInt("batch") == phase,
                "Out-of-order GPU batch reply");
        JSONObject payload = frame.getJSONObject("payload");
        deviceWidth = payload.getInt("device_width");
        return payload.getJSONArray("members");
    }

    private JSONArray commits;
    private int deviceWidth;
    int f4DeviceWidth() { return deviceWidth; }

    java.util.List<BatchPath> searchBatch(java.util.List<FidesLane.Attempt> attempts,
            java.util.List<RWRoute.ConnectionState> states, long[] scores, int iteration,
            FidesRrgLoader rrg, FidesMutableMirror mirror, float[] pcc, int[] slr,
            RouteNodeGraph graph, FidesRrgIdMap ids, RWRoute router) {
        require(commits == null && attempts.size() == states.size(), "Bad GPU batch state");
        v20GrowthEarned = attempts.size() > 1 && router.f4ResidentGrowthEarned();
        before(attempts.get(0), iteration, rrg, mirror, pcc, slr, states.get(0).connection, scores[0], ids);
        int sequence = -1;
        try {
            JSONArray members = new JSONArray();
            F1Corridor.Box[] corridors = new F1Corridor.Box[attempts.size()];
            long corridorPlanNs = 0;
            for (int index = 0; index < attempts.size(); ++index) {
                FidesLane.Attempt attempt = attempts.get(index);
                require(attempt.sequence > sequence && attempt.targetIds.length == 1
                        && attempt.targetIds[0] == attempt.sinkId && !attempt.hasAltSinks,
                        "Unexpected GPU batch member");
                sequence = attempt.sequence;
                JSONObject member = new JSONObject().put("attempt", rawAttempt(attempt))
                        .put("feature_score", scores[index])
                        .put("cpu_pops_before_handoff", states.get(index).f4CpuPopsBeforeHandoff)
                        .put("cpu_budget", states.get(index).f4HandoffBudget);
                if (attempt.useBoundingBox) {
                    F1Corridor.Box box = states.get(index).f4Boundary;
                    if (box != null) {
                        require(box.left >= attempt.xMinBB && box.right <= attempt.xMaxBB
                                && box.bottom >= attempt.yMinBB && box.top <= attempt.yMaxBB
                                && box.right - box.left >= 2 && box.top - box.bottom >= 2
                                && (box.left > attempt.xMinBB || box.right < attempt.xMaxBB
                                    || box.bottom > attempt.yMinBB || box.top < attempt.yMaxBB),
                                "GPU corridor differs from the original attempt bounds");
                        corridors[index] = box;
                        member.put("corridor", new JSONObject().put("xMinBB", box.left).put("yMinBB", box.bottom)
                                .put("xMaxBB", box.right).put("yMaxBB", box.top));
                    }
                }
                members.put(member);
            }
            sendBatchFrame("request", router.f4CpuReceipt().put("members", members).put("f1_plan_ns", corridorPlanNs));
            JSONArray replies = readBatchAnswers();
            require(replies.length() == attempts.size(), "Incomplete GPU batch");
            var paths = new java.util.ArrayList<BatchPath>(attempts.size());
            for (int index = 0; index < attempts.size(); ++index) {
                selected = attempts.get(index);
                sequence = selected.sequence;
                var state = states.get(index);
                JSONObject reply = replies.getJSONObject(index);
                require(state.nodesPopped == 0
                        && reply.getInt("cpu_pops_before_handoff") == state.f4CpuPopsBeforeHandoff,
                        "GPU reply does not match this attempt's bounded CPU handoff");
                F1Corridor.Box box = corridors[index];
                boolean retry = reply.getBoolean("f1_original_retry");
                require(!retry || box != null, "Unexpected original-box GPU retry");
                BatchPath path = validate(reply, graph, rrg, ids, state.connection, router, state);
                if (retry) {
                    router.f4RetireCorridor(state.connection);
                } else if (box != null) {
                    for (int hop = 1; hop + 1 < path.ids().length; ++hop) {
                        int nodeId = path.ids()[hop];
                        require(box.contains(rrg.endTileX[nodeId], rrg.endTileYRaw[nodeId]),
                                "GPU path escaped the selected corridor");
                    }
                }
                paths.add(path);
            }
            commits = new JSONArray();
            return paths;
        } catch (IOException | ReflectiveOperationException error) {
            throw new IllegalStateException("Cannot exchange GPU batch", error);
        } catch (RuntimeException | AssertionError error) {
            try {
                sendBatchFrame("reject", new JSONObject().put("sequence", sequence).put("reject", error.toString()));
            } catch (IOException writeError) {
                error.addSuppressed(writeError);
            }
            throw error;
        } finally {
            selected = null;
        }
    }

    void recordBatchCommit(Connection connection, int sequence, BatchPath path, FidesLaneGate gate,
            FidesRrgIdMap idMap, int cpuPops) {
        int[] live = connection.getRnodes().stream().mapToInt(idMap::idOf).toArray();
        require(connection.isRouted() && Arrays.equals(live, path.ids()), "Original commit differs from GPU path");
        commits.put(new JSONObject().put("sequence", sequence)
                .put("cpu_pops_before_handoff", cpuPops));
    }

    void finishBatch() {
        try {
            sendBatchFrame("commit", new JSONObject().put("members", commits));
        } catch (IOException error) {
            throw new IllegalStateException("Cannot record batch commit", error);
        }
        commits = null;
    }
}
