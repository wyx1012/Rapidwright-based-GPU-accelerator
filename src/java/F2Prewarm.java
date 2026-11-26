package com.xilinx.rapidwright.rwroute;

import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.device.Tile;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import jcuda.Pointer;
import jcuda.driver.CUcontext;
import jcuda.driver.CUdeviceptr;
import jcuda.driver.CUevent;
import jcuda.driver.CUfunction;
import jcuda.driver.CUmodule;
import jcuda.driver.JCudaDriver;

final class F2Prewarm {
    private static final int RECORD_WORDS = 9;
    private static final int BLOCK = 128;
    private static final long RESERVE = 1L << 30;
    private static F2Prewarm session;

    private final long origin = System.nanoTime();
    private final long routerThread = Thread.currentThread().getId();
    private final String mode;
    private final ExecutorService worker = Executors.newSingleThreadExecutor(task -> new Thread(task, "f2-context"));
    private Future<?> ready;
    private Future<?> pendingCompute;
    private CUcontext context;
    private long contextWorker, contextSchedule, contextStart, contextReady;
    private boolean completed;

    private F2Prewarm(String mode) {
        this.mode = mode;
    }

    static void start() {
        String mode = System.getProperty("fides.f2.prewarm");
        if (mode == null) return;
        if (session != null || !("background".equals(mode) || "ondemand".equals(mode))) {
            throw new IllegalStateException("Invalid F2 context experiment");
        }
        session = new F2Prewarm(mode);
        if ("background".equals(mode)) session.scheduleContext();
    }

    static void initializeAsync(List<Connection> indirect, List<Connection> direct) {
        if (session != null) session.computeAsync(indirect, direct);
    }

    static void initialize(List<Connection> indirect, List<Connection> direct) {
        if (session != null) session.awaitOrCompute(indirect, direct);
    }

    static void close() {
        F2Prewarm current = session;
        session = null;
        if (current != null && !current.completed) current.release();
    }

    private long stamp() {
        return System.nanoTime() - origin;
    }

    private void scheduleContext() {
        if (ready != null) throw new IllegalStateException("F2 context already scheduled");
        contextSchedule = stamp();
        ready = worker.submit(() -> {
            contextWorker = Thread.currentThread().getId();
            contextStart = stamp();
            FidesCudaRuntime.init();
            if (FidesCudaRuntime.deviceCount() < 1) throw new IllegalStateException("F2 expects at least one visible GPU");
            context = FidesCudaRuntime.createContext(0);
            contextReady = stamp();
        });
    }

    private static int[] geometry(List<Connection> indirect, List<Connection> direct) {
        final int nInd = indirect.size();
        final int nDir = direct.size();
        final int nAll = nInd + nDir;
        final Connection[] raw = new Connection[nAll];
        indirect.toArray(raw);
        for (int i = 0; i < nDir; ++i) raw[nInd + i] = direct.get(i);

        final long[] keys = new long[nAll];
        final int workers = Math.min(36, Math.max(1, Runtime.getRuntime().availableProcessors()));
        final int chunk = Math.max(4096, (nAll + workers * 4 - 1) / (workers * 4));
        final int tasks = (nAll + chunk - 1) / chunk;
        var keyJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(tasks);
        for (int ti = 0; ti < tasks; ++ti) {
            final int from = ti * chunk, to = Math.min(nAll, from + chunk);
            keyJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                for (int i = from; i < to; ++i) {
                    keys[i] = (((long) raw[i].hashCode()) << 32) | (i & 0xFFFFFFFFL);
                }
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(keyJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();
        java.util.Arrays.parallelSort(keys);

        final Connection[] sorted = new Connection[nAll];
        final int[] data = new int[nAll * RECORD_WORDS];
        final int[] taskInd = new int[tasks];
        var fillJobs = new ArrayList<java.util.concurrent.CompletableFuture<Void>>(tasks);
        for (int ti = 0; ti < tasks; ++ti) {
            final int idx = ti;
            final int from = ti * chunk, to = Math.min(nAll, from + chunk);
            fillJobs.add(java.util.concurrent.CompletableFuture.runAsync(() -> {
                int indCnt = 0;
                int cursor = from * RECORD_WORDS;
                for (int i = from; i < to; ++i) {
                    long kv = keys[i];
                    int id = (int) (kv >> 32);
                    if (i > 0 && id <= (int) (keys[i - 1] >> 32)) {
                        throw new IllegalStateException("Duplicate F2 connection identity");
                    }
                    Connection c = raw[(int) kv];
                    sorted[i] = c;
                    Tile source = c.isDirect() ? c.getSource().getTile() : c.getSourceRnode().getTile();
                    Tile sink = c.isDirect() ? c.getSink().getTile() : c.getSinkRnode().getTile();
                    int left, right, bottom, top, hpwl;
                    if (c.isDirect()) {
                        int sx = source.getTileXCoordinate(), sy = source.getTileYCoordinate();
                        int tx = sink.getTileXCoordinate(), ty = sink.getTileYCoordinate();
                        left = Math.min(sx, tx);
                        right = Math.max(sx, tx);
                        bottom = Math.min(sy, ty);
                        top = Math.max(sy, ty);
                        hpwl = Math.abs(sx - tx) + Math.abs(sy - ty) + 2;
                    } else {
                        indCnt++;
                        left = c.getXMinBB() + 1;
                        right = c.getXMaxBB() - 1;
                        bottom = c.getYMinBB() + 1;
                        top = c.getYMaxBB() - 1;
                        hpwl = c.getHpwl();
                    }
                    if (left > right || bottom > top) throw new IllegalStateException("Invalid F2 box");
                    data[cursor]     = id;
                    data[cursor + 1] = left;
                    data[cursor + 2] = right;
                    data[cursor + 3] = bottom;
                    data[cursor + 4] = top;
                    data[cursor + 5] = hpwl;
                    data[cursor + 7] = source.getSLR().getId();
                    data[cursor + 8] = sink.getSLR().getId();
                    cursor += RECORD_WORDS;
                }
                taskInd[idx] = indCnt;
            }));
        }
        java.util.concurrent.CompletableFuture.allOf(fillJobs.toArray(new java.util.concurrent.CompletableFuture<?>[0])).join();

        int indirectCount = 0;
        for (int t : taskInd) indirectCount += t;
        var fanout = new IdentityHashMap<Net, Integer>();
        for (int i = 0; i < nAll; ++i) {
            data[i * RECORD_WORDS + 6] = fanout.computeIfAbsent(sorted[i].getNet(), n -> n.getSinkPins().size());
        }
        if (indirectCount < 0 || nAll <= 0) {
            throw new IllegalStateException("F2 geometry universe differs from the pinned design");
        }
        return data;
    }

    private void computeAsync(List<Connection> indirect, List<Connection> direct) {
        if (completed || Thread.currentThread().getId() != routerThread) throw new IllegalStateException("F2 host thread");
        if (pendingCompute != null) return;
        final List<Connection> indirectSnap = new ArrayList<>(indirect);
        final List<Connection> directSnap = new ArrayList<>(direct);
        ExecutorService exec = Executors.newSingleThreadExecutor(r -> {
            Thread th = new Thread(r, "f2-async-prep");
            th.setDaemon(true);
            return th;
        });
        pendingCompute = exec.submit(() -> {
            try {
                runComputeBody(indirectSnap, directSnap);
            } finally {
                exec.shutdown();
            }
        });
    }

    private void awaitOrCompute(List<Connection> indirect, List<Connection> direct) {
        if (Thread.currentThread().getId() != routerThread) throw new IllegalStateException("F2 host thread");
        if (pendingCompute != null) {
            try {
                pendingCompute.get();
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("Interrupted waiting for async F2 feature preparation", error);
            } catch (Exception error) {
                throw new IllegalStateException("Async F2 prewarm feature preparation failed", error);
            } finally {
                pendingCompute = null;
            }
            return;
        }
        runComputeBody(indirect, direct);
    }

    private void runComputeBody(List<Connection> indirect, List<Connection> direct) {
        if (completed) return;
        try {
            long geometryStart = stamp();
            int[] input = geometry(indirect, direct);
            long hostReady = stamp();
            long waitStart = hostReady;
            if (ready == null) scheduleContext();
            ready.get();
            long waitEnd = stamp();
            FeatureResult result = worker.submit(() -> runFeatures(input)).get();
            Path outputFile = Path.of(System.getProperty("fides.ppt.f4.features"));
            try (var table = java.nio.channels.FileChannel.open(
                    outputFile, StandardOpenOption.CREATE_NEW, StandardOpenOption.READ, StandardOpenOption.WRITE)) {
                table.map(java.nio.channels.FileChannel.MapMode.READ_WRITE,
                          0, (long) result.words.length * Long.BYTES)
                    .order(ByteOrder.LITTLE_ENDIAN).asLongBuffer().put(result.words);
            }
            long overlapped = Math.max(0, Math.min(hostReady, contextReady) - contextStart);
            release();
            completed = true;
            System.out.printf("F2_PREWARM mode=%s rows=%d block=%d routerThread=%d cudaThread=%d "
                            + "contextScheduleNs=%d contextStartNs=%d contextReadyNs=%d geometryStartNs=%d "
                            + "hostReadyNs=%d waitStartNs=%d waitEndNs=%d overlapNs=%d moduleNs=%d "
                            + "uploadNs=%d kernelNs=%d downloadNs=%d freeBytes=%d featureContextClosed=1%n",
                    mode,input.length/RECORD_WORDS,BLOCK,routerThread,contextWorker,
                    contextSchedule,contextStart,contextReady,geometryStart,hostReady,waitStart,waitEnd,
                    overlapped,result.moduleNs,result.uploadNs,result.kernelNs,result.downloadNs,result.freeBytes);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted during F2 feature preparation",error);
        } catch (Exception error) {
            throw new IllegalStateException("F2 prewarm feature preparation failed",error);
        }
    }

    private FeatureResult runFeatures(int[] input) throws Exception {
        if (Thread.currentThread().getId() != contextWorker || context == null) {
            throw new IllegalStateException("F2 GPU context ownership");
        }
        long moduleStart = System.nanoTime();
        byte[] image = Files.readAllBytes(Path.of(System.getProperty("fides.f2.cubin")));
        CUmodule module = FidesCudaRuntime.loadCubin(image, "f2Feature");
        long moduleNs = System.nanoTime() - moduleStart;
        CUdeviceptr dInput = new CUdeviceptr(), dOutput = new CUdeviceptr();
        CUevent start = new CUevent(), stop = new CUevent();
        boolean allocatedInput=false, allocatedOutput=false, startOpen=false, stopOpen=false;
        try {
            long[] scores = new long[input.length];
            long inputBytes = (long) input.length * Integer.BYTES;
            long outputBytes = (long) scores.length * Long.BYTES;
            if (FidesCudaRuntime.memInfo()[0] < inputBytes + outputBytes + RESERVE) {
                throw new IllegalStateException("F2 feature allocation would breach reserve");
            }
            JCudaDriver.cuMemAlloc(dInput,inputBytes);
            allocatedInput=true;
            JCudaDriver.cuMemAlloc(dOutput,outputBytes);
            allocatedOutput=true;
            long free = FidesCudaRuntime.memInfo()[0];
            if (free < RESERVE) throw new IllegalStateException("F2 feature reserve");
            CUfunction kernel = FidesCudaRuntime.function(module,"route_feature_score");
            long beforeUpload = System.nanoTime();
            JCudaDriver.cuMemcpyHtoD(dInput,Pointer.to(input),inputBytes);
            long uploadNs = System.nanoTime() - beforeUpload;
            JCudaDriver.cuEventCreate(start,0);
            startOpen=true;
            JCudaDriver.cuEventCreate(stop,0);
            stopOpen=true;
            int count = input.length/RECORD_WORDS;
            JCudaDriver.cuEventRecord(start,null);
            FidesCudaRuntime.launch(kernel,(count+BLOCK-1)/BLOCK,BLOCK,Pointer.to(
                    Pointer.to(dInput),Pointer.to(dOutput),Pointer.to(new int[]{count}),
                    Pointer.to(new long[]{-1L}),Pointer.to(new long[]{-1L})));
            JCudaDriver.cuEventRecord(stop,null);
            JCudaDriver.cuEventSynchronize(stop);
            float[] kernelMs = new float[1];
            JCudaDriver.cuEventElapsedTime(kernelMs,start,stop);
            long kernelNs = Math.round(kernelMs[0]*1_000_000.0);
            if (kernelNs<=0) throw new IllegalStateException("Invalid F2 CUDA event");
            long beforeDownload = System.nanoTime();
            JCudaDriver.cuMemcpyDtoH(Pointer.to(scores),dOutput,outputBytes);
            return new FeatureResult(scores,moduleNs,uploadNs,kernelNs,System.nanoTime()-beforeDownload,free);
        } finally {
            if (stopOpen) JCudaDriver.cuEventDestroy(stop);
            if (startOpen) JCudaDriver.cuEventDestroy(start);
            if (allocatedOutput) JCudaDriver.cuMemFree(dOutput);
            if (allocatedInput) JCudaDriver.cuMemFree(dInput);
            JCudaDriver.cuModuleUnload(module);
        }
    }

    private void release() {
        if (worker.isShutdown()) return;
        try {
            worker.submit(() -> {
                if (context != null) {
                    JCudaDriver.cuCtxDestroy(context);
                    context = null;
                }
            }).get();
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted while closing F2 feature context",error);
        } catch (Exception error) {
            throw new IllegalStateException("F2 context close failed",error);
        } finally {
            worker.shutdown();
        }
    }

    private static final class FeatureResult {
        final long[] words;
        final long moduleNs,uploadNs,kernelNs,downloadNs,freeBytes;
        FeatureResult(long[] words,long moduleNs,long uploadNs,long kernelNs,long downloadNs,long freeBytes) {
            this.words=words;
            this.moduleNs=moduleNs;
            this.uploadNs=uploadNs;
            this.kernelNs=kernelNs;
            this.downloadNs=downloadNs;
            this.freeBytes=freeBytes;
        }
    }
}
