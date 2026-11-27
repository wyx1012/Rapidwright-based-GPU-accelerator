package com.xilinx.rapidwright.rwroute;

final class FidesNativeCpu {
    private static volatile boolean loaded = false;

    static synchronized void load(String soPath) {
        if (!loaded) {
            System.load(soPath);
            loaded = true;
        }
    }

    static native void nativeSetDesignConnCount(int connCount);

    static native boolean nativeInit(
            String rrgPath,
            String layoutPath,
            int[] slrData,
            int sllLen,
            int[] nextLaguna,
            int[] prevLaguna,
            int[] occ,
            float[] hist,
            float[] pcc);

    static native boolean nativePreinitStatic(String rrgPath, String layoutPath);

    static native boolean nativeAttachDynamicState(
            int[] slrData,
            int sllLen,
            int[] nextLaguna,
            int[] prevLaguna,
            int[] occ,
            float[] hist,
            float[] pcc);

    static native void nativeInitScope(int netId, int[] members);

    static native void nativeInitScopesBatch(int[] scopeOffsets, int[] scopeNodes, int numThreads);

    static native void nativeUpdatePcc(float[] pcc);

    static native void nativeRegisterConnections(
            int[] netId,
            int[] srcId,
            int[] sinkId,
            int[] origXmin,
            int[] origYmin,
            int[] origXmax,
            int[] origYmax,
            int[] sinkBeginX,
            int[] sinkBeginY,
            int[] sinkTile,
            int[] sinkType,
            float[] netXCenter,
            float[] netYCenter,
            int[] netSize,
            int[] doubleHpwl,
            byte[] flags);

    static native int[] nativeExportPortals(int[] slrMin, int[] slrMax);

    static native void nativeInitCorridorGrid(
            int fabricLeft,
            int fabricBottom,
            int fabricRight,
            int fabricTop,
            int coarseLeft,
            int coarseBottom,
            int coarseHeight,
            int[] coarseOccupancy,
            byte[] connCorrMode);

    static native void nativeSeedCrossSlrCorridors(
            int[] crossIdx,
            int count,
            int[] bx0,
            int[] by0,
            int[] bx1,
            int[] by1,
            byte[] buse);

    static native void nativeRouteRound(
            int routeIter,
            int[] activeConns,
            int activeCount,
            int[] boxXmin,
            int[] boxYmin,
            int[] boxXmax,
            int[] boxYmax,
            byte[] boxUse,
            float shareWeight,
            float rnodeCostWeight,
            float rnodeWLWeight,
            float estWlWeight,
            int wavePopCeiling,
            int numThreads,
            long[] outStats);

    static native int nativeUpdateHistAndFeedback(
            int routeIter,
            float histFactor,
            boolean enlargeBbox,
            int extX,
            int extY,
            int[] outNextActive,
            int[] outCongested);

    static native int nativeExportPath(int connIdx, int[] outPath);

    static native int nativeExportOverused(int[] outIds, float[] outHist);

    static native void nativeShutdownGpu();
}
