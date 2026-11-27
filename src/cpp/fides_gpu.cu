#include "fides_gpu.h"
#include "fides_arith.cuh"

#include <cooperative_groups.h>
#include <cuda_runtime.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <thread>
#include <vector>
#ifdef __linux__
#include <sched.h>
#endif

namespace {

using U32 = uint32_t;
using U64 = uint64_t;
using I32 = int32_t;
using I64 = int64_t;

enum Ordinal {
    SLL = 0, NONLOCAL = 1, RESERVED = 2, EAST = 3, WEST = 4, BOTH = 5,
    EX_BOTH = 6, EX_EAST = 7, EX_WEST = 8, EX_NONLOCAL = 9,
    INT_TILE = 10, PINFEED = 11, PINBOUNCE = 12, CLE_OUTPUT = 13, SLL_INPUT = 14,
    CAPACITY = 15, OrdinalCount = 16
};

enum TypeFlagBits : U32 {
    TF_LOCAL     = 1u,
    TF_EAST      = 2u,
    TF_WEST      = 4u,
    TF_LEADING   = 8u,
    TF_EXCLUSIVE = 16u,
    TF_NONLOCAL  = 32u
};

constexpr U32 EMPTY_KEY   = 0xFFFFFFFFu;
constexpr U32 CLOSED_BIT  = 0x80000000u;
constexpr U32 NODE_MASK   = 0x7FFFFFFFu;
constexpr U64 INF_GPREV   = 0xFFFFFFFFFFFFFFFFULL;
constexpr int NUM_BUCKETS = 256;
constexpr int MASK_WORDS  = 4; // 4 * 64 = 256 buckets
constexpr int MAX_MEGA_SCOPES = 1024;
constexpr int MAX_LAG_X   = 1024;
constexpr int MAX_PCC     = 512;

// Tier-A constants (Warp-per-Connection: 32 threads/conn)
constexpr U32 TA_HASH_CAP    = 1u << 18;           // 262,144 slots (~180k unique nodes)
constexpr U32 TA_HASH_MASK   = TA_HASH_CAP - 1u;
constexpr U32 TA_MAX_TOUCHED = (TA_HASH_CAP * 3u) / 4u;
constexpr U32 TA_BCK_CAP     = 512u;               // 256 * 512 * 4B = 0.5 MiB
constexpr U32 TA_OV_CAP      = 131072u;
constexpr U32 TA_LA_CAP      = 4096u;
constexpr U32 TA_MAX_PROBES  = 128u;
constexpr int TA_WARPS_PER_BLOCK = 4;

// Tier-B1 constants (CTA-per-Connection: 256 threads/conn, 1 CTA/SM = 80 CTAs, 8M-slot hash table!)
constexpr U32 TB_HASH_CAP    = 1u << 23;           // 8,388,608 slots (~6.29M unique nodes)
constexpr U32 TB_MAX_TOUCHED = (TB_HASH_CAP * 3u) / 4u;
constexpr U32 TB_BCK_CAP     = 24576u;             // 256 * 24576 * 4B = 24 MiB (+8 MiB/CTA)
constexpr U32 TB_OV_CAP      = 4718592u;           // 4.5M overflow per ping-pong (+16 MiB/CTA)
constexpr U32 TB_LA_CAP      = 262144u;            // 256K lookahead buffer (prevents SLL lookahead overflow)
constexpr U32 TB_MAX_PROBES  = 256u;
constexpr int TB_THREADS     = 256;

// Tier-B2 constants (unallocated in v36; kept for symbol compatibility)
constexpr U32 TM_MAX_TOUCHED = 16u << 20;
constexpr U32 TM_BCK_CAP     = 65536u;
constexpr U32 TM_OV_CAP      = 16u << 20;
constexpr U32 TM_LA_CAP      = 262144u;
constexpr U32 TM_FRONT_CAP   = 32768u;

__constant__ U32 c_ord[OrdinalCount];
__constant__ float c_pcc[MAX_PCC];
__constant__ I32 c_pccLen;
__constant__ I32 c_sllLen;
__constant__ I32 c_lagLen;
__constant__ I32 c_nextLag[MAX_LAG_X];
__constant__ I32 c_prevLag[MAX_LAG_X];
__constant__ U32* c_megaKeys[MAX_MEGA_SCOPES];
__constant__ I32* c_megaVals[MAX_MEGA_SCOPES];

struct GpuGraphDevice {
    U32 n;
    const U32* offset;
    const U32* edge;
    const uint8_t* coreDeg;
    const FidesGpuNodeHot* hot;
    const FidesGpuDynNode* dyn;
};

struct TierAWarpMem {
    U32* keys;
    U64* gPrev;
    U32* touched;
    U32* buckets;
    U32* ovNode[2];
    U32* ovKey[2];
    U32* laBuf[2];
};

struct TierBCtaMem {
    U32* keys;
    U64* gPrev;
    U32* touched;
    U32* buckets;
    U32* ovNode[2];
    U32* ovKey[2];
    U32* laBuf[2];
};

struct TierBMegaCtrl {
    U32  baseKey;
    U64  activeMask[MASK_WORDS];
    U32  bckCnt[NUM_BUCKETS];
    U32  ovCur;
    U32  ovCnt[2];
    U32  laCur;
    U32  laCnt[2];
    U32  laDepth;
    U32  touchedCnt;
    U32  frontCnt;
    U32  activeCnt;
    I64  pops;
    U32  foundSink;
    U64  bestSinkGP;
    I32  abortStatus;
    U32  done;
    U32  minOvKey;
};

struct TierBMegaMem {
    U32* state;
    U64* gPrev;
    U32* touched;
    U32* buckets;
    U32* ovNode[2];
    U32* ovKey[2];
    U32* laBuf[2];
    U32* frontRaw;
    U32* frontActive;
    TierBMegaCtrl* ctrl;
};

struct GpuState {
    bool ready = false;
    int cudaDev = 0;
    int numaNode = -1;
    U32 n = 0;
    U32 m = 0;
    cudaStream_t stream = nullptr;

    U32* d_offset = nullptr;
    U32* d_edge = nullptr;
    uint8_t* d_coreDeg = nullptr;
    FidesGpuNodeHot* d_hot = nullptr;
    FidesGpuDynNode* d_dyn = nullptr;

    U32 taWarps = 0;
    void* d_taPool = nullptr;
    size_t taSlotBytes = 0;
    size_t taTotalBytes = 0;

    U32 tbCtas = 0;
    void* d_tbPool = nullptr;
    size_t tbSlotBytes = 0;
    size_t tbTotalBytes = 0;
    int tbActiveMode = 0;

    void* d_tmPool = nullptr;
    size_t tmTotalBytes = 0;
    U32 tmGridBlocks = 80;
    TierBMegaMem tm{};
    TierBMegaCtrl* h_tmCtrlPinned = nullptr;

    U32* d_nextQuery = nullptr;
    U32* d_outPathCursor = nullptr;
    U32* h_outPathCursorPinned = nullptr;
    FidesGpuQuery* d_queries = nullptr;
    size_t capQueries = 0;
    FidesGpuResult* d_results = nullptr;
    size_t capResults = 0;
    U32* d_outPaths = nullptr;
    size_t capOutPaths = 0;
    U32* d_sameKeys = nullptr;
    I32* d_sameVals = nullptr;
    size_t capSame = 0;
    U32* d_oldNodes = nullptr;
    size_t capOldNodes = 0;

    FidesGpuQuery* h_queriesPinned = nullptr;
    FidesGpuResult* h_resultsPinned = nullptr;
    U32* h_outPathsPinned = nullptr;
    U32* h_sameKeysPinned = nullptr;
    I32* h_sameValsPinned = nullptr;
    U32* h_oldNodesPinned = nullptr;

    U32* d_deltaIds = nullptr;
    I32* d_deltaOcc = nullptr;
    float* d_deltaHist = nullptr;
    U32* h_deltaIdsPinned = nullptr;
    I32* h_deltaOccPinned = nullptr;
    float* h_deltaHistPinned = nullptr;
    size_t capDeltas = 0;

    U32* d_megaKeysHost[MAX_MEGA_SCOPES]{};
    I32* d_megaValsHost[MAX_MEGA_SCOPES]{};
    size_t megaCap[MAX_MEGA_SCOPES]{};
    U32* h_megaStageKeysPinned = nullptr;
    I32* h_megaStageValsPinned = nullptr;
    size_t capMegaStage = 0;
    cudaEvent_t megaStageEvent = nullptr;
    bool megaStageEventRecorded = false;
    FidesGpuMegaPatch* d_megaPatches = nullptr;
    FidesGpuMegaPatch* h_megaPatchesPinned = nullptr;
    size_t capMegaPatches = 0;
};

static GpuState GS_ARR[FG_MAX_GPUS];
static int g_numGpus = 0;

inline bool checkCuda(cudaError_t err, const char* where) {
    if (err != cudaSuccess) {
        fprintf(stderr, "FIDES_V38_GPU CUDA error at %s: %s\n", where, cudaGetErrorString(err));
        fflush(stderr);
        return false;
    }
    return true;
}

static void bindThreadToPciNuma(const cudaDeviceProp& prop, int* outNuma) {
    int numa = -1;
#ifdef __linux__
    char path[128];
    std::snprintf(path, sizeof(path), "/sys/bus/pci/devices/%04x:%02x:%02x.0/numa_node",
                  prop.pciDomainID, prop.pciBusID, prop.pciDeviceID);
    if (FILE* f = std::fopen(path, "r")) {
        int val = -1;
        if (std::fscanf(f, "%d", &val) == 1) numa = val;
        std::fclose(f);
    }
    if (numa >= 0) {
        char cpuPath[128];
        std::snprintf(cpuPath, sizeof(cpuPath), "/sys/devices/system/node/node%d/cpulist", numa);
        if (FILE* f = std::fopen(cpuPath, "r")) {
            char line[256]{};
            if (std::fgets(line, sizeof(line), f)) {
                cpu_set_t set;
                CPU_ZERO(&set);
                char* p = line;
                while (*p) {
                    while (*p == ' ' || *p == ',' || *p == '\n') ++p;
                    if (!*p) break;
                    int a = 0, b = 0;
                    if (std::sscanf(p, "%d-%d", &a, &b) == 2) {
                        for (int c = a; c <= b && c < CPU_SETSIZE; ++c) CPU_SET(c, &set);
                    } else if (std::sscanf(p, "%d", &a) == 1) {
                        if (a >= 0 && a < CPU_SETSIZE) CPU_SET(a, &set);
                    }
                    while (*p && *p != ',' && *p != '\n') ++p;
                }
                if (CPU_COUNT(&set) > 0) sched_setaffinity(0, sizeof(set), &set);
            }
            std::fclose(f);
        }
    }
#endif
    if (outNuma) *outNuma = numa;
}

struct GpuWorkerPool {
    std::mutex mu;
    std::condition_variable cvWork;
    std::condition_variable cvDone;
    std::thread threads[FG_MAX_GPUS];
    bool started = false;
    bool stop = false;
    uint64_t epoch = 0;
    int activeSlots = 0;
    FidesGpuDevTaskFn curFn = nullptr;
    void* curUser = nullptr;
    int pending = 0;

    void start(int numSlots) {
        if (started) return;
        stop = false;
        activeSlots = numSlots;
        for (int i = 0; i < FG_MAX_GPUS; ++i) {
            threads[i] = std::thread([this, i]() {
                uint64_t seen = 0;
                int boundDev = -1;
                while (true) {
                    FidesGpuDevTaskFn fn = nullptr;
                    void* user = nullptr;
                    {
                        std::unique_lock<std::mutex> lk(mu);
                        cvWork.wait(lk, [&]() { return stop || epoch != seen; });
                        if (stop) return;
                        seen = epoch;
                        if (i >= activeSlots) {
                            if (--pending == 0) cvDone.notify_one();
                            continue;
                        }
                        fn = curFn;
                        user = curUser;
                    }
                    if (GS_ARR[i].cudaDev >= 0 && boundDev != GS_ARR[i].cudaDev) {
                        cudaSetDevice(GS_ARR[i].cudaDev);
                        cudaDeviceProp prop{};
                        if (cudaGetDeviceProperties(&prop, GS_ARR[i].cudaDev) == cudaSuccess) {
                            bindThreadToPciNuma(prop, nullptr);
                        }
                        boundDev = GS_ARR[i].cudaDev;
                    }
                    if (fn) fn(i, user);
                    {
                        std::lock_guard<std::mutex> lk(mu);
                        if (--pending == 0) cvDone.notify_one();
                    }
                }
            });
        }
        started = true;
    }

    void run(FidesGpuDevTaskFn fn, void* user) {
        if (g_numGpus <= 1) {
            if (g_numGpus == 1) {
                cudaSetDevice(GS_ARR[0].cudaDev);
                fn(0, user);
            }
            return;
        }
        if (!started) start(FG_MAX_GPUS);
        {
            std::lock_guard<std::mutex> lk(mu);
            activeSlots = g_numGpus;
            curFn = fn;
            curUser = user;
            pending = FG_MAX_GPUS;
            ++epoch;
        }
        cvWork.notify_all();
        std::unique_lock<std::mutex> lk(mu);
        cvDone.wait(lk, [&]() { return pending == 0; });
    }

    void shutdown() {
        if (!started) return;
        {
            std::lock_guard<std::mutex> lk(mu);
            stop = true;
            ++epoch;
        }
        cvWork.notify_all();
        for (int i = 0; i < FG_MAX_GPUS; ++i) {
            if (threads[i].joinable()) threads[i].join();
        }
        started = false;
    }
};

static GpuWorkerPool g_gpuPool;

inline size_t alignUp(size_t v, size_t a) { return (v + a - 1) & ~(a - 1); }

// ============================================================================
// Device inline helpers
// ============================================================================

__device__ __forceinline__ U32 d_mixId(U32 x) {
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

__device__ __forceinline__ FidesGpuNodeHot loadHot(const FidesGpuNodeHot* arr, U32 id) {
    uint4 v = __ldg(reinterpret_cast<const uint4*>(arr) + id);
    FidesGpuNodeHot h;
    h.w0 = v.x; h.w1 = v.y; h.w2 = v.z; h.baseCost = __uint_as_float(v.w);
    return h;
}

__device__ __forceinline__ FidesGpuDynNode loadDyn(const FidesGpuDynNode* arr, U32 id) {
    uint2 v = __ldg(reinterpret_cast<const uint2*>(arr) + id);
    FidesGpuDynNode d;
    d.occ = static_cast<I32>(v.x); d.hist = __uint_as_float(v.y);
    return d;
}

__device__ __forceinline__ int hotEndX(const FidesGpuNodeHot& h)      { return static_cast<int>(h.w0 & 0x3FFu); }
__device__ __forceinline__ int hotEndYRaw(const FidesGpuNodeHot& h)   { return static_cast<int>((h.w0 >> 10) & 0x7FFu); }
__device__ __forceinline__ int hotTileY(const FidesGpuNodeHot& h)     { return static_cast<int>((h.w0 >> 21) & 0x7FFu); }
__device__ __forceinline__ int hotTileId(const FidesGpuNodeHot& h)    { return static_cast<int>(h.w1 & 0x3FFFFFu); }
__device__ __forceinline__ int hotLength(const FidesGpuNodeHot& h)    { return static_cast<int>((h.w1 >> 22) & 0x3FFu); }
__device__ __forceinline__ U32 hotPreFlags(const FidesGpuNodeHot& h)  { return h.w2 & 0x7FFu; }
__device__ __forceinline__ U32 hotTypeFlags(const FidesGpuNodeHot& h) { return (h.w2 >> 11) & 0x3Fu; }
__device__ __forceinline__ U32 hotNodeType(const FidesGpuNodeHot& h)  { return (h.w2 >> 17) & 0x1Fu; }
__device__ __forceinline__ int hotSlrRaw(const FidesGpuNodeHot& h)    { return static_cast<int>((h.w2 >> 22) & 0x7u) - 1; }
__device__ __forceinline__ int hotSlrTileY(const FidesGpuNodeHot& h)  { return static_cast<int>((h.w2 >> 25) & 0x7u) - 1; }

__device__ __forceinline__ int d_endY(const FidesGpuNodeHot& cg, const FidesGpuNodeHot& pg) {
    return ((hotPreFlags(cg) & FG_PF_SLL) && hotEndYRaw(pg) == hotEndYRaw(cg))
            ? hotTileY(cg) : hotEndYRaw(cg);
}

__device__ __forceinline__ int d_slrFast(const FidesGpuNodeHot& cg, const FidesGpuNodeHot& pg) {
    return ((hotPreFlags(cg) & FG_PF_SLL) && hotEndYRaw(pg) == hotEndYRaw(cg))
            ? hotSlrTileY(cg) : hotSlrRaw(cg);
}

__device__ __forceinline__ int findMinBucket256(const U64 m[MASK_WORDS]) {
    if (m[0]) return __ffsll(static_cast<long long>(m[0])) - 1;
    if (m[1]) return 64 + __ffsll(static_cast<long long>(m[1])) - 1;
    if (m[2]) return 128 + __ffsll(static_cast<long long>(m[2])) - 1;
    if (m[3]) return 192 + __ffsll(static_cast<long long>(m[3])) - 1;
    return -1;
}

__device__ __forceinline__ bool anyMask256(const U64 m[MASK_WORDS]) {
    return (m[0] | m[1] | m[2] | m[3]) != 0ULL;
}

__device__ __forceinline__ U32 floatBucketKey(float f, U32 shift) {
    if (f <= 0.0f) return 0u;
    return __float_as_uint(f) >> shift;
}

// Compute baseKey giving generous downward headroom for RWRoute's greedy heuristic (estWlWeight=0.80, rnodeWLWeight=0.20),
// where f = g + h drops from f_start down toward ~0.25*f_start along the optimal path!
__device__ __forceinline__ U32 computeInitialBaseKey(const GpuGraphDevice& G, const FidesGpuQuery& q, const FidesGpuWaveParams& wp) {
    FidesGpuNodeHot sh = loadHot(G.hot, q.src);
    int d0 = fides_absInt(hotEndX(sh) - q.sinkBeginX) + fides_absInt(hotEndYRaw(sh) - q.sinkBeginY);
    float f0 = wp.estWlWeight * static_cast<float>(d0);
    if (f0 < 1.0f) f0 = 1.0f;
    // Lower floor at 0.005 * f0 (covers 7 octaves = 128x downward drop at shift=18 on high-fanout trunks)
    U32 floorBk = floatBucketKey(f0 * 0.005f, wp.bucketShift);
    U32 startBk = floatBucketKey(f0, wp.bucketShift);
    // Keep f0 within the upper ~1/8 of the 256-bucket window while spanning ~7 octaves below f0 at shift=18
    U32 maxBelow = 224u;
    if (startBk > floorBk && (startBk - floorBk) > maxBelow) {
        floorBk = startBk - maxBelow;
    }
    return floorBk;
}

__device__ __forceinline__ int d_lookupSameNet(
        const FidesGpuQuery& q, U32 child,
        const U32* sameKeys, const I32* sameVals) {
    U32 mode = q.sameNetMode;
    if (mode == 0u) return 0;
    if (mode == 1u) {
        if ((q.sameBloom64 & (1ULL << (d_mixId(child) & 63u))) == 0ULL) return 0;
        U32 off = q.sameOffset, len = q.sameParam;
        for (U32 i = 0; i < len; ++i) {
            if (__ldg(sameKeys + off + i) == child) return __ldg(sameVals + off + i);
        }
        return 0;
    }
    const U32* keys = (mode == 2u) ? (sameKeys + q.sameOffset) : c_megaKeys[q.sameOffset];
    const I32* vals = (mode == 2u) ? (sameVals + q.sameOffset) : c_megaVals[q.sameOffset];
    U32 mask = q.sameParam;
    U32 idx = d_mixId(child) & mask;
    for (U32 p = 0; p < 128u; ++p) {
        U32 k = __ldg(keys + idx);
        if (k == EMPTY_KEY) return 0;
        if (k == child) return __ldg(vals + idx);
        idx = (idx + 1u) & mask;
    }
    return 0;
}

__device__ __forceinline__ int d_countOldPath(
        const FidesGpuQuery& q, U32 child,
        const U32* oldNodes, const U32* sOld, U32 sOldCap) {
    if (q.oldLen == 0u || (q.oldBloom64 & (1ULL << (d_mixId(child) & 63u))) == 0ULL) {
        return 0;
    }
    const bool useSmem = (sOld != nullptr && q.oldLen <= sOldCap);
    const U32* arr = useSmem ? sOld : (oldNodes + q.oldOffset);
    U32 n = q.oldLen;
    if (n <= 12u) {
        int cnt = 0;
        for (U32 i = 0u; i < n; ++i) {
            U32 v = useSmem ? arr[i] : __ldg(arr + i);
            if (v == child) ++cnt;
            else if (v > child) break;
        }
        return cnt;
    }
    U32 lo = 0u, hi = n;
    while (lo < hi) {
        U32 mid = (lo + hi) >> 1;
        U32 v = useSmem ? arr[mid] : __ldg(arr + mid);
        if (v < child) lo = mid + 1u;
        else hi = mid;
    }
    int cnt = 0;
    while (lo < n) {
        U32 v = useSmem ? arr[lo] : __ldg(arr + lo);
        if (v != child) break;
        ++cnt;
        ++lo;
    }
    return cnt;
}

__device__ __forceinline__ bool d_willOveruse(int occVal, int sameUses) {
    int cap = static_cast<int>(c_ord[CAPACITY]);
    return occVal > cap || (occVal == cap && sameUses == 0);
}

__device__ __forceinline__ int d_checkAccessAndLookahead(
        const GpuGraphDevice& G,
        const FidesGpuNodeHot& pg, int enteringTileId,
        U32 child, const FidesGpuNodeHot& cg,
        int childX, int childY,
        int occVal, int sameUses,
        const FidesGpuQuery& q,
        bool& lookahead, bool& earlyTerm) {
    lookahead = false;
    earlyTerm = false;

    U32 cf = hotTypeFlags(cg);
    if (child == q.sink) {
        earlyTerm = (cf & TF_EXCLUSIVE) ? true : !d_willOveruse(occVal, sameUses);
        return 0;
    }

    U32 ct = hotNodeType(cg);
    U32 pf = hotTypeFlags(pg);
    U32 cpf = hotPreFlags(cg);
    U32 ppf = hotPreFlags(pg);
    int childTile = hotTileId(cg);
    bool cross = (q.flags & FG_QF_CROSS) != 0u;

    if (cf & TF_LOCAL) {
        if (cross && (cf & TF_LEADING) && d_slrFast(cg, pg) != q.sinkSlr) {
            int dir = ((cpf & FG_PF_LAGUNA_NORTH) ? 1 : 0) | ((cpf & FG_PF_LAGUNA_SOUTH) ? 2 : 0);
            if (((dir & 1) && !(q.flags & FG_QF_NORTH)) || ((dir & 2) && !(q.flags & FG_QF_SOUTH))) return 1;
            if (!(cpf & FG_PF_INT_TILE) || !(cpf & FG_PF_LAGUNA_WIRE) || childTile == q.sinkTile) return -1;
            if (!d_willOveruse(occVal, sameUses)) lookahead = true;
        } else {
            bool allowed = false;
            U32 st = static_cast<U32>(q.sinkType);
            if (st == c_ord[EAST] || st == c_ord[EX_EAST]) {
                if ((cf & TF_WEST) || ct == c_ord[RESERVED]) return 1;
                if (st != c_ord[EX_EAST]) return -1;
                allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
            } else if (st == c_ord[WEST] || st == c_ord[EX_WEST]) {
                if ((cf & TF_EAST) || ct == c_ord[RESERVED]) return 1;
                if (st != c_ord[EX_WEST]) return -1;
                allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
            } else if (st == c_ord[EX_BOTH]) {
                if (childTile != q.sinkTile || ct != c_ord[BOTH] || !(q.flags & FG_QF_SINK_CTRL)) return 1;
                allowed = true;
            } else if (st == c_ord[EX_NONLOCAL]) {
                return 1;
            } else {
                return -1;
            }
            if (!allowed) return 1;
            if (!(cpf & FG_PF_INT_TILE)) return -1;
        }
        return 0;
    }

    if (cf & TF_NONLOCAL) {
        bool parentLaguna = (ppf & FG_PF_LAGUNA_TILE) != 0u;
        bool childLaguna = (cpf & FG_PF_LAGUNA_TILE) != 0u;
        if (ct == c_ord[NONLOCAL]) {
            if (pf & TF_LOCAL) {
                if (childLaguna) {
                    if (!cross || d_slrFast(cg, pg) == q.sinkSlr) return 1;
                } else {
                    if (!(ppf & FG_PF_PINFEED) || !(cpf & FG_PF_CLB_TILE) || !(cpf & FG_PF_CLE_OUTPUT)) return -1;
                }
            } else if (ppf & FG_PF_SLL) {
                if (enteringTileId < 0 || enteringTileId == childTile) return 1;
            }
        } else if (cpf & (FG_PF_LAGUNA_NORTH | FG_PF_LAGUNA_SOUTH)) {
            if (cross && d_slrFast(cg, pg) != q.sinkSlr) {
                int dir = ((cpf & FG_PF_LAGUNA_NORTH) ? 1 : 0) | ((cpf & FG_PF_LAGUNA_SOUTH) ? 2 : 0);
                if (((dir & 1) && (q.flags & FG_QF_NORTH)) || ((dir & 2) && (q.flags & FG_QF_SOUTH))) {
                    if (!d_willOveruse(occVal, sameUses)) lookahead = true;
                }
            }
        } else {
            return -1;
        }
        if (!(q.flags & FG_QF_UTURN) && (cpf & FG_PF_UTURN_DELAY)) return 1;
        if (parentLaguna || childLaguna) lookahead = true;
        return 0;
    }

    if (cf & TF_EXCLUSIVE) {
        if (!(sameUses != 0 && (cpf & FG_PF_PINBOUNCE) != 0u)) return 1;
        return 0;
    }

    if (cpf & FG_PF_SLL) return 0;
    return -1;
}

__device__ __forceinline__ bool d_computeCost(
        const FidesGpuNodeHot& pg, const FidesGpuNodeHot& cg,
        int childX, int childY,
        int occVal, float histVal, int sameUses,
        float uUpstream,
        const FidesGpuQuery& q, const FidesGpuWaveParams& wp,
        float& outPartial, float& outTotal) {
    int pccIdx = occVal - (sameUses != 0 ? 1 : 0);
    if (pccIdx < 0) pccIdx = 0;
    float pccCost;
    if (pccIdx < c_pccLen) {
        pccCost = c_pcc[pccIdx];
    } else if (c_pccLen >= 2) {
        int last = c_pccLen - 1;
        pccCost = c_pcc[last] + static_cast<float>(pccIdx - last) * (c_pcc[last] - c_pcc[last - 1]);
    } else {
        return false;
    }

    float distToCenter = fabsf(static_cast<float>(childX) - q.xCenter)
                       + fabsf(static_cast<float>(childY) - q.yCenter);
    float bc = cg.baseCost;
    float bias = bc * distToCenter * q.invBiasDenom;
    float invSharing = (sameUses == 0) ? 1.0f : (1.0f / (1.0f + wp.shareWeight * static_cast<float>(sameUses)));
    float rawNc = bc * histVal * pccCost;
    float nc = (sameUses == 0) ? rawNc : (rawNc * invSharing);
    float nodeCost = nc + fides_minJdk(bias, nc * 0.5f);

    float newPartial = uUpstream + wp.rnodeCostWeight * nodeCost;
    int len = hotLength(cg);
    newPartial += (sameUses == 0)
            ? (wp.rnodeWLWeight * static_cast<float>(len))
            : (wp.rnodeWLWeight * static_cast<float>(len) * invSharing);

    int deltaX = fides_absInt(childX - q.sinkBeginX);
    int deltaY = fides_absInt(childY - q.sinkBeginY);
    if (q.flags & FG_QF_CROSS) {
        int childSlr = d_slrFast(cg, pg);
        int deltaSLR = fides_absInt(q.sinkSlr - childSlr);
        if (deltaSLR != 0) {
            if (deltaSLR == 1) {
                int overshootByY = deltaY - c_sllLen;
                if (overshootByY < 0) deltaY = c_sllLen - overshootByY;
            }
            if (childX >= 0 && childX < c_lagLen) {
                int nextCol = c_nextLag[childX];
                int prevCol = c_prevLag[childX];
                if (nextCol != prevCol) {
                    int dNext, dAndFromNext, dPrev, dAndFromPrev;
                    if (nextCol == INT32_MAX || nextCol >= q.origXmax) {
                        dNext = INT32_MAX; dAndFromNext = INT32_MAX;
                    } else {
                        dNext = fides_absInt(nextCol - childX);
                        dAndFromNext = dNext + fides_absInt(q.sinkBeginX - nextCol);
                    }
                    if (prevCol == INT32_MIN || prevCol <= q.origXmin) {
                        dPrev = INT32_MAX; dAndFromPrev = INT32_MAX;
                    } else {
                        dPrev = fides_absInt(prevCol - childX);
                        dAndFromPrev = dPrev + fides_absInt(q.sinkBeginX - prevCol);
                    }
                    if (dNext == dPrev) deltaX = (dAndFromNext < dAndFromPrev) ? dAndFromNext : dAndFromPrev;
                    else if (dNext < dPrev && dAndFromNext <= dAndFromPrev + 4) deltaX = dAndFromNext;
                    else if (dPrev < dNext && dAndFromPrev <= dAndFromNext + 4) deltaX = dAndFromPrev;
                    else deltaX = (dNext < dPrev) ? dAndFromNext : dAndFromPrev;
                }
            }
        }
    }
    int distToSink = deltaX + deltaY;
    float newTotal = (sameUses == 0)
            ? (newPartial + wp.estWlWeight * static_cast<float>(distToSink))
            : (newPartial + wp.estWlWeight * static_cast<float>(distToSink) * invSharing);

    outPartial = newPartial;
    outTotal = newTotal;
    return true;
}

__device__ __forceinline__ U64 packGPrev(float g, U32 prev) {
    U32 gb = (g <= 0.0f) ? 0u : __float_as_uint(g);
    return (static_cast<U64>(gb) << 32) | static_cast<U64>(prev);
}

__device__ __forceinline__ U32 hashFindSlot(
        const U32* keys, U32 mask, U32 maxProbes, U32 nodeId) {
    U32 idx = d_mixId(nodeId) & mask;
    for (U32 p = 0; p < maxProbes; ++p) {
        U32 k = keys[idx];
        if (k == EMPTY_KEY) return UINT32_MAX;
        if ((k & NODE_MASK) == nodeId) return idx;
        idx = (idx + 1u) & mask;
    }
    return UINT32_MAX;
}

__device__ __forceinline__ int hashMarkOnPush(
        U32* keys, U64* gPrev, U32* touched, U32* touchedCnt,
        U32 mask, U32 maxTouched, U32 maxProbes,
        U32 child, U32 parent, float newG) {
    U32 idx = d_mixId(child) & mask;
    U64 newGP = packGPrev(newG, parent);
    for (U32 p = 0; p < maxProbes; ++p) {
        U32 k = atomicAdd(keys + idx, 0u);
        if (k == EMPTY_KEY) {
            U32 prevK = atomicCAS(keys + idx, EMPTY_KEY, child);
            if (prevK == EMPTY_KEY) {
                U32 tPos = atomicAdd(touchedCnt, 1u);
                if (tPos < maxTouched) {
                    touched[tPos] = idx;
                } else {
                    return -1;
                }
                gPrev[idx] = newGP;
                return 1;
            }
            k = prevK;
        }
        if ((k & NODE_MASK) == child) return 0;
        idx = (idx + 1u) & mask;
    }
    return -1;
}

__device__ __forceinline__ int hashInsertOrRelax(
        U32* keys, U64* gPrev, U32* touched, U32* touchedCnt,
        U32 mask, U32 maxTouched, U32 maxProbes,
        U32 child, U32 parent, float newG) {
    U32 idx = d_mixId(child) & mask;
    U64 newGP = packGPrev(newG, parent);
    for (U32 p = 0; p < maxProbes; ++p) {
        U32 k = atomicAdd(keys + idx, 0u);
        if (k == EMPTY_KEY) {
            U32 prevK = atomicCAS(keys + idx, EMPTY_KEY, child);
            if (prevK == EMPTY_KEY) {
                U32 tPos = atomicAdd(touchedCnt, 1u);
                if (tPos < maxTouched) {
                    touched[tPos] = idx;
                } else {
                    return -1;
                }
                U64 oldGP = atomicMin(reinterpret_cast<unsigned long long*>(gPrev + idx),
                                      static_cast<unsigned long long>(newGP));
                return (newGP < oldGP) ? 1 : 0;
            }
            k = prevK;
        }
        if ((k & NODE_MASK) == child) {
            if (k & CLOSED_BIT) return 0;
            U64 oldGP = atomicMin(reinterpret_cast<unsigned long long*>(gPrev + idx),
                                  static_cast<unsigned long long>(newGP));
            return (newGP < oldGP) ? 1 : 0;
        }
        idx = (idx + 1u) & mask;
    }
    return -1;
}

__global__ void kApplyOccDeltas(FidesGpuDynNode* dyn, const U32* ids, const I32* occs, U32 count) {
    U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dyn[ids[i]].occ = occs[i];
}

__global__ void kApplyHistDeltas(FidesGpuDynNode* dyn, const U32* ids, const float* hists, U32 count) {
    U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) dyn[ids[i]].hist = hists[i];
}

__global__ void kApplyMegaPatches(const FidesGpuMegaPatch* patches, U32 count) {
    U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) {
        FidesGpuMegaPatch p = patches[i];
        c_megaKeys[p.slot][p.idx] = p.key;
        c_megaVals[p.slot][p.idx] = p.val;
    }
}

// ============================================================================
// Tier-A: High-Parallel Warp-per-Connection (256 buckets, 32 threads/conn)
// ============================================================================

__device__ __forceinline__ TierAWarpMem getTierAWarpMem(void* pool, size_t slotBytes, U32 warpSlot) {
    uint8_t* base = reinterpret_cast<uint8_t*>(pool) + static_cast<size_t>(warpSlot) * slotBytes;
    TierAWarpMem m;
    size_t off = 0;
    m.keys      = reinterpret_cast<U32*>(base + off); off += size_t(TA_HASH_CAP) * sizeof(U32);
    m.gPrev     = reinterpret_cast<U64*>(base + off); off += size_t(TA_HASH_CAP) * sizeof(U64);
    m.touched   = reinterpret_cast<U32*>(base + off); off += size_t(TA_MAX_TOUCHED) * sizeof(U32);
    m.buckets   = reinterpret_cast<U32*>(base + off); off += size_t(NUM_BUCKETS) * TA_BCK_CAP * sizeof(U32);
    m.ovNode[0] = reinterpret_cast<U32*>(base + off); off += size_t(TA_OV_CAP) * sizeof(U32);
    m.ovNode[1] = reinterpret_cast<U32*>(base + off); off += size_t(TA_OV_CAP) * sizeof(U32);
    m.ovKey[0]  = reinterpret_cast<U32*>(base + off); off += size_t(TA_OV_CAP) * sizeof(U32);
    m.ovKey[1]  = reinterpret_cast<U32*>(base + off); off += size_t(TA_OV_CAP) * sizeof(U32);
    m.laBuf[0]  = reinterpret_cast<U32*>(base + off); off += size_t(TA_LA_CAP) * sizeof(U32);
    m.laBuf[1]  = reinterpret_cast<U32*>(base + off);
    return m;
}

struct TierAWarpSmem {
    U32  touchedCnt;
    U32  baseKey;
    U64  activeMask[MASK_WORDS];
    U32  bckCnt[NUM_BUCKETS];
    U32  ovCur;
    U32  ovCnt[2];
    U32  laCur;
    U32  laCnt[2];
    U32  laDepth;
    U32  foundSink;
    U64  bestSinkGP;
    I32  abortStatus;
    I64  pops;
    U32  popNodes[32];
    float popG[32];
    U32  popPrev[32];
    U32  popCount;
    U32  edgeStart[32];
    U32  edgeEnd[32];
};

__global__ void __launch_bounds__(TA_WARPS_PER_BLOCK * 32, 4)
kTierAWarpSearch(
        GpuGraphDevice G,
        void* taPool, size_t taSlotBytes, U32 numWarpSlots,
        FidesGpuWaveParams wp,
        const FidesGpuQuery* queries, U32 numQueries, U32* nextQuery,
        const U32* sameKeys, const I32* sameVals, const U32* oldNodes,
        FidesGpuResult* outResults, U32* outPaths, U32 outPathsCap, U32* outPathCursor) {
    const int lane = threadIdx.x & 31;
    const int warpInBlk = threadIdx.x >> 5;
    const U32 warpSlot = blockIdx.x * TA_WARPS_PER_BLOCK + warpInBlk;
    if (warpSlot >= numWarpSlots) return;

    __shared__ TierAWarpSmem smemAll[TA_WARPS_PER_BLOCK];
    TierAWarpSmem& S = smemAll[warpInBlk];
    TierAWarpMem M = getTierAWarpMem(taPool, taSlotBytes, warpSlot);
    const U32 popBatch = (wp.popBatch > 0u && wp.popBatch <= 32u) ? wp.popBatch : 32u;

    while (true) {
        U32 qIdx = 0;
        if (lane == 0) qIdx = atomicAdd(nextQuery, 1u);
        qIdx = __shfl_sync(0xFFFFFFFFu, qIdx, 0);
        if (qIdx >= numQueries) break;

        FidesGpuQuery q = queries[qIdx];

        if (lane == 0) {
            S.touchedCnt = 0u;
            S.baseKey = computeInitialBaseKey(G, q, wp);
            for (int w = 0; w < MASK_WORDS; ++w) S.activeMask[w] = 0ULL;
            S.ovCur = 0u; S.ovCnt[0] = 0u; S.ovCnt[1] = 0u;
            S.laCur = 0u; S.laCnt[0] = 0u; S.laCnt[1] = 0u;
            S.laDepth = 0u;
            S.foundSink = 0u;
            S.bestSinkGP = INF_GPREV;
            S.abortStatus = 0;
            S.pops = 0;
        }
        for (int b = lane; b < NUM_BUCKETS; b += 32) S.bckCnt[b] = 0u;
        __syncwarp();

        if (lane == 0) {
            int rc = hashInsertOrRelax(
                    M.keys, M.gPrev, M.touched, &S.touchedCnt,
                    TA_HASH_MASK, TA_MAX_TOUCHED, TA_MAX_PROBES,
                    q.src, UINT32_MAX, 0.0f);
            if (rc > 0) {
                M.buckets[0] = q.src;
                S.bckCnt[0] = 1u;
                S.activeMask[0] = 1ULL;
            } else {
                S.abortStatus = FG_STATUS_FALLBACK;
            }
        }
        __syncwarp();

        while (S.abortStatus == 0 && S.foundSink == 0u) {
            bool doingLa = (S.laCnt[S.laCur] > 0u);
            if (!doingLa) {
                if (lane == 0) S.laDepth = 0u;
                if (!anyMask256(S.activeMask)) {
                    U32 ovC = S.ovCur;
                    U32 ovN = S.ovCnt[ovC];
                    if (ovN == 0u) break;

                    U32 localMin = UINT32_MAX;
                    const U32* ovNodes = M.ovNode[ovC];
                    const U32* ovKeys  = M.ovKey[ovC];
                    for (U32 i = lane; i < ovN; i += 32u) {
                        U32 nid = ovNodes[i];
                        U32 sl = hashFindSlot(M.keys, TA_HASH_MASK, TA_MAX_PROBES, nid);
                        if (sl != UINT32_MAX && !(M.keys[sl] & CLOSED_BIT)) {
                            U32 bk = ovKeys[i];
                            if (bk < localMin) localMin = bk;
                        }
                    }
                    for (int off = 16; off > 0; off >>= 1) {
                        U32 other = __shfl_down_sync(0xFFFFFFFFu, localMin, off);
                        if (other < localMin) localMin = other;
                    }
                    localMin = __shfl_sync(0xFFFFFFFFu, localMin, 0);
                    if (localMin == UINT32_MAX) {
                        if (lane == 0) S.ovCnt[ovC] = 0u;
                        __syncwarp();
                        break;
                    }
                    if (lane == 0) {
                        // Leave downward headroom (64 buckets below localMin) for greedy f drops!
                        S.baseKey = (localMin > 64u) ? (localMin - 64u) : 0u;
                        S.ovCnt[ovC ^ 1u] = 0u;
                    }
                    __syncwarp();

                    U32 bKeyBase = S.baseKey;
                    U32 nextOv = ovC ^ 1u;
                    U32* dstOvNodes = M.ovNode[nextOv];
                    U32* dstOvKeys  = M.ovKey[nextOv];
                    U64 lm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};
                    for (U32 i = lane; i < ovN; i += 32u) {
                        U32 nid = ovNodes[i];
                        U32 sl = hashFindSlot(M.keys, TA_HASH_MASK, TA_MAX_PROBES, nid);
                        if (sl == UINT32_MAX || (M.keys[sl] & CLOSED_BIT)) continue;
                        U32 bk = ovKeys[i];
                        U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                        bool pushed = false;
                        if (rel < static_cast<U32>(NUM_BUCKETS) && S.bckCnt[rel] < TA_BCK_CAP) {
                            U32 pos = atomicAdd(&S.bckCnt[rel], 1u);
                            if (pos < TA_BCK_CAP) {
                                M.buckets[rel * TA_BCK_CAP + pos] = nid;
                                lm[rel >> 6] |= (1ULL << (rel & 63u));
                                pushed = true;
                            } else {
                                atomicSub(&S.bckCnt[rel], 1u);
                            }
                        }
                        if (!pushed) {
                            U32 opos = atomicAdd(&S.ovCnt[nextOv], 1u);
                            if (opos < TA_OV_CAP) {
                                dstOvNodes[opos] = nid;
                                dstOvKeys[opos] = bk;
                            } else {
                                atomicCAS(&S.abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                            }
                        }
                    }
                    for (int w = 0; w < MASK_WORDS; ++w) {
                        for (int off = 16; off > 0; off >>= 1) lm[w] |= __shfl_down_sync(0xFFFFFFFFu, lm[w], off);
                    }
                    if (lane == 0) {
                        for (int w = 0; w < MASK_WORDS; ++w) S.activeMask[w] |= lm[w];
                        S.ovCnt[ovC] = 0u;
                        S.ovCur = nextOv;
                    }
                    __syncwarp();
                    if (S.abortStatus != 0) break;
                    if (!anyMask256(S.activeMask)) continue;
                }
            } else {
                if (lane == 0) {
                    if (++S.laDepth >= 256u) S.abortStatus = FG_STATUS_FALLBACK;
                }
                __syncwarp();
                if (S.abortStatus != 0) break;
            }

            // Phase 1: Pop up to popBatch nodes and mark CLOSED
            U32 uCand = UINT32_MAX;
            if (doingLa) {
                U32 curLa = S.laCur;
                U32 cnt = S.laCnt[curLa];
                U32 take = (cnt < popBatch) ? cnt : popBatch;
                U32 start = cnt - take;
                if (static_cast<U32>(lane) < take) uCand = M.laBuf[curLa][start + lane];
                if (lane == 0) { S.laCnt[curLa] = start; S.popCount = 0u; }
            } else {
                int b = findMinBucket256(S.activeMask);
                U32 cnt = S.bckCnt[b];
                U32 take = (cnt < popBatch) ? cnt : popBatch;
                U32 start = cnt - take;
                if (static_cast<U32>(lane) < take) uCand = M.buckets[static_cast<U32>(b) * TA_BCK_CAP + start + lane];
                if (lane == 0) {
                    S.bckCnt[b] = start;
                    if (start == 0u) S.activeMask[b >> 6] &= ~(1ULL << (b & 63));
                    S.popCount = 0u;
                }
            }
            __syncwarp();

            bool live = false;
            float myG = 0.0f;
            U32 myPrev = UINT32_MAX;
            if (uCand != UINT32_MAX) {
                U32 sl = hashFindSlot(M.keys, TA_HASH_MASK, TA_MAX_PROBES, uCand);
                if (sl != UINT32_MAX) {
                    U32 oldK = atomicOr(M.keys + sl, CLOSED_BIT);
                    if ((oldK & CLOSED_BIT) == 0u) {
                        live = true;
                        U64 gp = M.gPrev[sl];
                        myG = __uint_as_float(static_cast<U32>(gp >> 32));
                        myPrev = static_cast<U32>(gp & 0xFFFFFFFFu);
                    }
                }
            }

            U32 liveMask = __ballot_sync(0xFFFFFFFFu, live);
            U32 liveCnt = static_cast<U32>(__popc(liveMask));
            if (liveCnt == 0u) {
                if (lane == 0 && doingLa && S.laCnt[S.laCur] == 0u && S.laCnt[S.laCur ^ 1u] > 0u) {
                    S.laCur ^= 1u;
                }
                __syncwarp();
                continue;
            }

            if (live) {
                if (uCand == q.sink) {
                    atomicMin(reinterpret_cast<unsigned long long*>(&S.bestSinkGP),
                              static_cast<unsigned long long>(packGPrev(myG, myPrev)));
                    atomicExch(&S.foundSink, 1u);
                } else {
                    U32 rank = static_cast<U32>(__popc(liveMask & ((1u << lane) - 1u)));
                    S.popNodes[rank] = uCand;
                    S.popG[rank] = myG;
                    S.popPrev[rank] = myPrev;
                    U32 e0 = __ldg(G.offset + uCand);
                    U32 e1 = __ldg(G.offset + uCand + 1u);
                    if (G.coreDeg != nullptr) {
                        uint8_t cd = __ldg(G.coreDeg + uCand);
                        if (cd < 255u && (e0 + static_cast<U32>(cd) < e1)) {
                            FidesGpuNodeHot ugh = loadHot(G.hot, uCand);
                            int ux = hotEndX(ugh);
                            int uy = hotEndYRaw(ugh);
                            bool needSeg1 = (ux == q.sinkEndX && fides_absInt(uy - q.sinkEndY) <= 3)
                                         || (ux == q.sinkBeginX && fides_absInt(uy - q.sinkBeginY) <= 2);
                            if (!needSeg1) e1 = e0 + static_cast<U32>(cd);
                        }
                    }
                    S.edgeStart[rank] = e0;
                    S.edgeEnd[rank] = e1;
                }
            }
            if (lane == 0) {
                S.pops += static_cast<I64>(liveCnt);
                S.popCount = liveCnt;
            }
            __syncwarp();

            if (S.foundSink != 0u) break;
            if (q.budget > 0 && S.pops > static_cast<I64>(q.budget)) {
                if (lane == 0) S.abortStatus = FG_STATUS_CROSSED_BUDGET;
                __syncwarp();
                break;
            }

            // Phase 2: Expand all popped nodes in parallel
            const U32 pc = S.popCount;
            const U32 nextLa = S.laCur ^ 1u;
            const U32 ovC = S.ovCur;
            const U32 bKeyBase = S.baseKey;
            const bool useBox = (q.flags & FG_QF_USE_BOX) != 0u;
            U64 nm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};

            auto processEdge = [&](U32 u, const FidesGpuNodeHot& pg, float uG, int enteringTileId, U32 child) {
                FidesGpuNodeHot cg = loadHot(G.hot, child);
                int childX = hotEndX(cg);
                int childY = d_endY(cg, pg);
                if (useBox && child != q.sink) {
                    if (!(childX > q.xmin && childX < q.xmax && childY > q.ymin && childY < q.ymax)) return;
                }
                // Fast 1-probe closed check (catches ~85% of closed hits in 1 load without full double probing)
                U32 h0 = d_mixId(child) & TA_HASH_MASK;
                if (M.keys[h0] == (child | CLOSED_BIT)) return;

                FidesGpuDynNode dy = loadDyn(G.dyn, child);
                int occVal = dy.occ;
                int sameUses = (occVal > 0 && q.sameNetMode != 0u)
                        ? d_lookupSameNet(q, child, sameKeys, sameVals) : 0;
                if (occVal > 0 && q.oldLen > 0u) {
                    int oldCnt = d_countOldPath(q, child, oldNodes, nullptr, 0u);
                    for (int r = 0; r < oldCnt; ++r) {
                        if (sameUses > 0 && --sameUses == 0) --occVal;
                    }
                    if (occVal < 0) occVal = 0;
                }

                bool la = false, earlyTerm = false;
                int acc = d_checkAccessAndLookahead(
                        G, pg, enteringTileId, child, cg, childX, childY, occVal, sameUses, q, la, earlyTerm);
                if (acc == 1) return;
                if (acc < 0) { atomicCAS(&S.abortStatus, 0, FG_STATUS_FALLBACK); return; }

                float newPartial = 0.0f, newTotal = 0.0f;
                if (!d_computeCost(pg, cg, childX, childY, occVal, dy.hist, sameUses, uG, q, wp, newPartial, newTotal)) {
                    atomicCAS(&S.abortStatus, 0, FG_STATUS_FALLBACK);
                    return;
                }

                if (earlyTerm) {
                    atomicMin(reinterpret_cast<unsigned long long*>(&S.bestSinkGP),
                              static_cast<unsigned long long>(packGPrev(newPartial, u)));
                    atomicExch(&S.foundSink, 1u);
                    return;
                }

                int ins = hashInsertOrRelax(
                        M.keys, M.gPrev, M.touched, &S.touchedCnt,
                        TA_HASH_MASK, TA_MAX_TOUCHED, TA_MAX_PROBES,
                        child, u, newPartial);
                if (ins < 0) { atomicCAS(&S.abortStatus, 0, FG_STATUS_HASH_OVERFLOW); return; }
                if (ins == 0) return;

                if (la) {
                    U32 lpos = atomicAdd(&S.laCnt[nextLa], 1u);
                    if (lpos < TA_LA_CAP) M.laBuf[nextLa][lpos] = child;
                    else atomicCAS(&S.abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                } else {
                    U32 bk = floatBucketKey(newTotal, wp.bucketShift);
                    U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                    bool pushed = false;
                    if (rel < static_cast<U32>(NUM_BUCKETS) && S.bckCnt[rel] < TA_BCK_CAP) {
                        U32 bpos = atomicAdd(&S.bckCnt[rel], 1u);
                        if (bpos < TA_BCK_CAP) {
                            M.buckets[rel * TA_BCK_CAP + bpos] = child;
                            nm[rel >> 6] |= (1ULL << (rel & 63u));
                            pushed = true;
                        } else {
                            atomicSub(&S.bckCnt[rel], 1u);
                        }
                    }
                    if (!pushed) {
                        U32 opos = atomicAdd(&S.ovCnt[ovC], 1u);
                        if (opos < TA_OV_CAP) {
                            M.ovNode[ovC][opos] = child;
                            M.ovKey[ovC][opos] = bk;
                        } else {
                            atomicCAS(&S.abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                        }
                    }
                }
            };

            if (pc >= 8u) {
                if (static_cast<U32>(lane) < pc) {
                    U32 u = S.popNodes[lane];
                    float uG = S.popG[lane];
                    U32 uPrev = S.popPrev[lane];
                    FidesGpuNodeHot pg = loadHot(G.hot, u);
                    int enteringTileId = ((hotPreFlags(pg) & FG_PF_SLL) && uPrev != UINT32_MAX)
                            ? hotTileId(loadHot(G.hot, uPrev)) : -1;
                    U32 e0 = S.edgeStart[lane], e1 = S.edgeEnd[lane];
                    for (U32 e = e0; e < e1; ++e) {
                        if (S.foundSink != 0u || S.abortStatus != 0) break;
                        processEdge(u, pg, uG, enteringTileId, __ldg(G.edge + e));
                    }
                }
            } else {
                for (U32 i = 0; i < pc; ++i) {
                    if (S.foundSink != 0u || S.abortStatus != 0) break;
                    U32 u = S.popNodes[i];
                    float uG = S.popG[i];
                    U32 uPrev = S.popPrev[i];
                    FidesGpuNodeHot pg = loadHot(G.hot, u);
                    int enteringTileId = ((hotPreFlags(pg) & FG_PF_SLL) && uPrev != UINT32_MAX)
                            ? hotTileId(loadHot(G.hot, uPrev)) : -1;
                    U32 e0 = S.edgeStart[i], e1 = S.edgeEnd[i];
                    for (U32 e = e0 + lane; e < e1; e += 32u) {
                        processEdge(u, pg, uG, enteringTileId, __ldg(G.edge + e));
                    }
                }
            }

            for (int w = 0; w < MASK_WORDS; ++w) {
                for (int off = 16; off > 0; off >>= 1) nm[w] |= __shfl_down_sync(0xFFFFFFFFu, nm[w], off);
            }
            if (lane == 0) {
                for (int w = 0; w < MASK_WORDS; ++w) S.activeMask[w] |= nm[w];
                if (S.laCnt[nextLa] > 0u && S.laCnt[S.laCur] == 0u) S.laCur = nextLa;
            }
            __syncwarp();
        }

        if (lane == 0) {
            FidesGpuResult res{};
            res.pops = S.pops;
            res.pathOffset = 0u;
            if (S.foundSink != 0u) {
                // First measure exact path length, then reserve exact slice in outPaths via atomicAdd!
                U32 len = 1u;
                U32 cur = static_cast<U32>(S.bestSinkGP & 0xFFFFFFFFu);
                bool ok = (cur != UINT32_MAX || q.sink == q.src);
                U32 maxAllowed = (q.outPathCap > 0u) ? q.outPathCap : 4096u;
                while (ok && cur != UINT32_MAX) {
                    ++len;
                    if (len > maxAllowed) { ok = false; break; }
                    if (cur == q.src) break;
                    U32 sl = hashFindSlot(M.keys, TA_HASH_MASK, TA_MAX_PROBES, cur);
                    if (sl == UINT32_MAX) { ok = false; break; }
                    cur = static_cast<U32>(M.gPrev[sl] & 0xFFFFFFFFu);
                }
                if (ok && (cur == q.src || q.sink == q.src)) {
                    U32 pOff = atomicAdd(outPathCursor, len);
                    if (pOff + len <= outPathsCap) {
                        U32* dst = outPaths + pOff;
                        U32 w = 0u;
                        dst[w++] = q.sink;
                        U32 c2 = static_cast<U32>(S.bestSinkGP & 0xFFFFFFFFu);
                        while (c2 != UINT32_MAX && w < len) {
                            dst[w++] = c2;
                            if (c2 == q.src) break;
                            U32 sl = hashFindSlot(M.keys, TA_HASH_MASK, TA_MAX_PROBES, c2);
                            c2 = static_cast<U32>(M.gPrev[sl] & 0xFFFFFFFFu);
                        }
                        res.status = static_cast<I32>(len);
                        res.pathOffset = pOff;
                    } else {
                        res.status = FG_STATUS_FALLBACK;
                    }
                } else {
                    res.status = FG_STATUS_FALLBACK;
                }
            } else if (S.abortStatus != 0) {
                res.status = S.abortStatus;
            } else {
                res.status = FG_STATUS_NO_PATH;
            }
            outResults[qIdx] = res;
        }
        __syncwarp();

        U32 tc = S.touchedCnt;
        if (tc < TA_HASH_CAP / 4u && tc <= TA_MAX_TOUCHED) {
            for (U32 i = lane; i < tc; i += 32u) {
                U32 sl = M.touched[i];
                M.keys[sl] = EMPTY_KEY;
                M.gPrev[sl] = INF_GPREV;
            }
        } else {
            for (U32 i = lane; i < TA_HASH_CAP; i += 32u) {
                M.keys[i] = EMPTY_KEY;
                M.gPrev[i] = INF_GPREV;
            }
        }
        __syncwarp();
    }
}

// ============================================================================
// Tier-B1: Heavy-Tail CTA-per-Connection (256 buckets, 256 threads/conn)
// ============================================================================

struct TierB1Cfg {
    U32 hashCap;
    U32 hashMask;
    U32 maxTouched;
    U32 bckCap;
    U32 ovCap;
    U32 laCap;
    U32 maxProbes;
    size_t slotBytes;
};

static inline TierB1Cfg makeTierB1Cfg(int mode) {
    TierB1Cfg c{};
    if (mode == 2) {
        c.hashCap = 1u << 25;       // 32M slots
        c.maxTouched = (c.hashCap * 3u) / 4u;
        c.bckCap = 32768u;
        c.ovCap = 12582912u;        // 12M overflow
        c.laCap = 1048576u;         // 1M lookahead
        c.maxProbes = 512u;
    } else if (mode == 1) {
        c.hashCap = 1u << 24;       // 16M slots
        c.maxTouched = (c.hashCap * 3u) / 4u;
        c.bckCap = 24576u;
        c.ovCap = 7340032u;         // 7M overflow
        c.laCap = 524288u;          // 512K lookahead
        c.maxProbes = 512u;
    } else {
        c.hashCap = TB_HASH_CAP;    // 8M slots
        c.maxTouched = TB_MAX_TOUCHED;
        c.bckCap = TB_BCK_CAP;
        c.ovCap = TB_OV_CAP;
        c.laCap = TB_LA_CAP;
        c.maxProbes = TB_MAX_PROBES;
    }
    c.hashMask = c.hashCap - 1u;
    size_t bytes = size_t(c.hashCap) * sizeof(U32)
                 + size_t(c.hashCap) * sizeof(U64)
                 + size_t(c.maxTouched) * sizeof(U32)
                 + size_t(NUM_BUCKETS) * size_t(c.bckCap) * sizeof(U32)
                 + 4 * size_t(c.ovCap) * sizeof(U32)
                 + 2 * size_t(c.laCap) * sizeof(U32);
    c.slotBytes = (bytes + 255u) & ~size_t(255u);
    return c;
}

__device__ __forceinline__ TierBCtaMem getTierBCtaMem(void* pool, U32 ctaSlot, const TierB1Cfg& cfg) {
    uint8_t* base = reinterpret_cast<uint8_t*>(pool) + static_cast<size_t>(ctaSlot) * cfg.slotBytes;
    TierBCtaMem m;
    size_t off = 0;
    m.keys      = reinterpret_cast<U32*>(base + off); off += size_t(cfg.hashCap) * sizeof(U32);
    m.gPrev     = reinterpret_cast<U64*>(base + off); off += size_t(cfg.hashCap) * sizeof(U64);
    m.touched   = reinterpret_cast<U32*>(base + off); off += size_t(cfg.maxTouched) * sizeof(U32);
    m.buckets   = reinterpret_cast<U32*>(base + off); off += size_t(NUM_BUCKETS) * size_t(cfg.bckCap) * sizeof(U32);
    m.ovNode[0] = reinterpret_cast<U32*>(base + off); off += size_t(cfg.ovCap) * sizeof(U32);
    m.ovNode[1] = reinterpret_cast<U32*>(base + off); off += size_t(cfg.ovCap) * sizeof(U32);
    m.ovKey[0]  = reinterpret_cast<U32*>(base + off); off += size_t(cfg.ovCap) * sizeof(U32);
    m.ovKey[1]  = reinterpret_cast<U32*>(base + off); off += size_t(cfg.ovCap) * sizeof(U32);
    m.laBuf[0]  = reinterpret_cast<U32*>(base + off); off += size_t(cfg.laCap) * sizeof(U32);
    m.laBuf[1]  = reinterpret_cast<U32*>(base + off);
    return m;
}

__global__ void __launch_bounds__(TB_THREADS, 2)
kTierBCtaSearch(
        GpuGraphDevice G,
        void* tbPool, TierB1Cfg cfg, U32 numCtaSlots,
        FidesGpuWaveParams wp,
        const FidesGpuQuery* queries, U32 numQueries, U32* nextQuery,
        const U32* sameKeys, const I32* sameVals, const U32* oldNodes,
        FidesGpuResult* outResults, U32* outPaths, U32 outPathsCap, U32* outPathCursor) {
    const int tid = threadIdx.x;
    const U32 ctaSlot = blockIdx.x;
    if (ctaSlot >= numCtaSlots) return;

    __shared__ U32  s_qIdx;
    __shared__ U32  s_touchedCnt;
    __shared__ U32  s_baseKey;
    __shared__ U64  s_activeMask[MASK_WORDS];
    __shared__ U32  s_bckCnt[NUM_BUCKETS];
    __shared__ U32  s_ovCur;
    __shared__ U32  s_ovCnt[2];
    __shared__ U32  s_laCur;
    __shared__ U32  s_laCnt[2];
    __shared__ U32  s_laDepth;
    __shared__ U32  s_foundSink;
    __shared__ U64  s_bestSinkGP;
    __shared__ I32  s_abortStatus;
    __shared__ I64  s_pops;
    __shared__ U32  s_minKey;
    __shared__ U32  s_popCount;
    __shared__ U32  s_popNodes[TB_THREADS];
    __shared__ float s_popG[TB_THREADS];
    __shared__ U32  s_popPrev[TB_THREADS];
    __shared__ U32  s_edgeStart[TB_THREADS];
    __shared__ U32  s_edgeEnd[TB_THREADS];
    __shared__ U32  s_oldNodes[512];
    __shared__ U64  s_warpMasks[TB_THREADS / 32][MASK_WORDS];

    TierBCtaMem M = getTierBCtaMem(tbPool, ctaSlot, cfg);
    const U32 popBatch = (wp.popBatch > 0u && wp.popBatch <= TB_THREADS) ? wp.popBatch : TB_THREADS;
    const int lane = tid & 31;
    const int warpId = tid >> 5;

    while (true) {
        if (tid == 0) s_qIdx = atomicAdd(nextQuery, 1u);
        __syncthreads();
        U32 qIdx = s_qIdx;
        if (qIdx >= numQueries) break;

        FidesGpuQuery q = queries[qIdx];

        if (q.oldLen > 0u && q.oldLen <= 512u) {
            for (U32 i = static_cast<U32>(tid); i < q.oldLen; i += TB_THREADS) {
                s_oldNodes[i] = oldNodes[q.oldOffset + i];
            }
        }

        if (tid == 0) {
            s_touchedCnt = 0u;
            s_baseKey = computeInitialBaseKey(G, q, wp);
            for (int w = 0; w < MASK_WORDS; ++w) s_activeMask[w] = 0ULL;
            s_ovCur = 0u; s_ovCnt[0] = 0u; s_ovCnt[1] = 0u;
            s_laCur = 0u; s_laCnt[0] = 0u; s_laCnt[1] = 0u;
            s_laDepth = 0u;
            s_foundSink = 0u;
            s_bestSinkGP = INF_GPREV;
            s_abortStatus = 0;
            s_pops = 0;
        }
        if (tid < NUM_BUCKETS) s_bckCnt[tid] = 0u;
        __syncthreads();

        if (tid == 0) {
            int rc = hashInsertOrRelax(
                    M.keys, M.gPrev, M.touched, &s_touchedCnt,
                    cfg.hashMask, cfg.maxTouched, cfg.maxProbes,
                    q.src, UINT32_MAX, 0.0f);
            if (rc > 0) {
                M.buckets[0] = q.src;
                s_bckCnt[0] = 1u;
                s_activeMask[0] = 1ULL;
            } else {
                s_abortStatus = FG_STATUS_FALLBACK;
            }
        }
        __syncthreads();

        while (s_abortStatus == 0 && s_foundSink == 0u) {
            bool doingLa = (s_laCnt[s_laCur] > 0u);
            if (!doingLa) {
                if (tid == 0) s_laDepth = 0u;
                if (!anyMask256(s_activeMask)) {
                    U32 ovC = s_ovCur;
                    U32 ovN = s_ovCnt[ovC];
                    if (ovN == 0u) break;

                    if (tid == 0) s_minKey = UINT32_MAX;
                    __syncthreads();

                    U32 localMin = UINT32_MAX;
                    const U32* ovNodes = M.ovNode[ovC];
                    const U32* ovKeys  = M.ovKey[ovC];
                    for (U32 i = tid; i < ovN; i += TB_THREADS) {
                        U32 nid = ovNodes[i];
                        U32 sl = hashFindSlot(M.keys, cfg.hashMask, cfg.maxProbes, nid);
                        if (sl != UINT32_MAX && !(M.keys[sl] & CLOSED_BIT)) {
                            U32 bk = ovKeys[i];
                            if (bk < localMin) localMin = bk;
                        }
                    }
                    for (int off = 16; off > 0; off >>= 1) {
                        U32 o = __shfl_down_sync(0xFFFFFFFFu, localMin, off);
                        if (o < localMin) localMin = o;
                    }
                    if (lane == 0 && localMin < UINT32_MAX) atomicMin(&s_minKey, localMin);
                    __syncthreads();

                    if (s_minKey == UINT32_MAX) {
                        if (tid == 0) s_ovCnt[ovC] = 0u;
                        __syncthreads();
                        break;
                    }
                    if (tid == 0) {
                        s_baseKey = (s_minKey > 64u) ? (s_minKey - 64u) : 0u;
                        s_ovCnt[ovC ^ 1u] = 0u;
                    }
                    __syncthreads();

                    U32 bKeyBase = s_baseKey;
                    U32 nextOv = ovC ^ 1u;
                    U32* dstOvNodes = M.ovNode[nextOv];
                    U32* dstOvKeys  = M.ovKey[nextOv];
                    U64 lm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};
                    for (U32 i = tid; i < ovN; i += TB_THREADS) {
                        U32 nid = ovNodes[i];
                        U32 sl = hashFindSlot(M.keys, cfg.hashMask, cfg.maxProbes, nid);
                        if (sl == UINT32_MAX || (M.keys[sl] & CLOSED_BIT)) continue;
                        U32 bk = ovKeys[i];
                        U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                        bool pushed = false;
                        if (rel < static_cast<U32>(NUM_BUCKETS) && s_bckCnt[rel] < cfg.bckCap) {
                            U32 pos = atomicAdd(&s_bckCnt[rel], 1u);
                            if (pos < cfg.bckCap) {
                                M.buckets[rel * cfg.bckCap + pos] = nid;
                                lm[rel >> 6] |= (1ULL << (rel & 63u));
                                pushed = true;
                            } else {
                                atomicSub(&s_bckCnt[rel], 1u);
                            }
                        }
                        if (!pushed) {
                            U32 opos = atomicAdd(&s_ovCnt[nextOv], 1u);
                            if (opos < cfg.ovCap) {
                                dstOvNodes[opos] = nid;
                                dstOvKeys[opos] = bk;
                            } else {
                                atomicCAS(&s_abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                            }
                        }
                    }
                    for (int w = 0; w < MASK_WORDS; ++w) {
                        for (int off = 16; off > 0; off >>= 1) lm[w] |= __shfl_down_sync(0xFFFFFFFFu, lm[w], off);
                        if (lane == 0) s_warpMasks[warpId][w] = lm[w];
                    }
                    __syncthreads();
                    if (tid == 0) {
                        for (int w = 0; w < MASK_WORDS; ++w) {
                            U64 comb = 0ULL;
                            for (int wi = 0; wi < TB_THREADS / 32; ++wi) comb |= s_warpMasks[wi][w];
                            s_activeMask[w] |= comb;
                        }
                        s_ovCnt[ovC] = 0u;
                        s_ovCur = nextOv;
                    }
                    __syncthreads();
                    if (s_abortStatus != 0) break;
                    if (!anyMask256(s_activeMask)) continue;
                }
            } else {
                if (tid == 0) { if (++s_laDepth >= 256u) s_abortStatus = FG_STATUS_FALLBACK; }
                __syncthreads();
                if (s_abortStatus != 0) break;
            }

            // Phase 1: Pop up to popBatch nodes and mark CLOSED
            U32 uCand = UINT32_MAX;
            if (doingLa) {
                U32 curLa = s_laCur;
                U32 cnt = s_laCnt[curLa];
                U32 take = (cnt < popBatch) ? cnt : popBatch;
                U32 start = cnt - take;
                if (static_cast<U32>(tid) < take) uCand = M.laBuf[curLa][start + tid];
                __syncthreads();
                if (tid == 0) { s_laCnt[curLa] = start; s_popCount = 0u; }
            } else {
                int b = findMinBucket256(s_activeMask);
                U32 cnt = s_bckCnt[b];
                U32 take = (cnt < popBatch) ? cnt : popBatch;
                U32 start = cnt - take;
                if (static_cast<U32>(tid) < take) uCand = M.buckets[static_cast<U32>(b) * cfg.bckCap + start + tid];
                __syncthreads();
                if (tid == 0) {
                    s_bckCnt[b] = start;
                    if (start == 0u) s_activeMask[b >> 6] &= ~(1ULL << (b & 63));
                    s_popCount = 0u;
                }
            }
            __syncthreads();

            if (uCand != UINT32_MAX) {
                U32 sl = hashFindSlot(M.keys, cfg.hashMask, cfg.maxProbes, uCand);
                if (sl != UINT32_MAX) {
                    U32 oldK = atomicOr(M.keys + sl, CLOSED_BIT);
                    if ((oldK & CLOSED_BIT) == 0u) {
                        U64 gp = M.gPrev[sl];
                        float myG = __uint_as_float(static_cast<U32>(gp >> 32));
                        U32 myPrev = static_cast<U32>(gp & 0xFFFFFFFFu);
                        if (uCand == q.sink) {
                            atomicMin(reinterpret_cast<unsigned long long*>(&s_bestSinkGP),
                                      static_cast<unsigned long long>(packGPrev(myG, myPrev)));
                            atomicExch(&s_foundSink, 1u);
                        } else {
                            U32 pos = atomicAdd(&s_popCount, 1u);
                            s_popNodes[pos] = uCand;
                            s_popG[pos] = myG;
                            s_popPrev[pos] = myPrev;
                            U32 e0 = __ldg(G.offset + uCand);
                            U32 e1 = __ldg(G.offset + uCand + 1u);
                            if (G.coreDeg != nullptr) {
                                uint8_t cd = __ldg(G.coreDeg + uCand);
                                if (cd < 255u && (e0 + static_cast<U32>(cd) < e1)) {
                                    FidesGpuNodeHot ugh = loadHot(G.hot, uCand);
                                    int ux = hotEndX(ugh);
                                    int uy = hotEndYRaw(ugh);
                                    bool needSeg1 = (ux == q.sinkEndX && fides_absInt(uy - q.sinkEndY) <= 3)
                                                 || (ux == q.sinkBeginX && fides_absInt(uy - q.sinkBeginY) <= 2);
                                    if (!needSeg1) e1 = e0 + static_cast<U32>(cd);
                                }
                            }
                            s_edgeStart[pos] = e0;
                            s_edgeEnd[pos] = e1;
                        }
                    }
                }
            }
            __syncthreads();

            if (s_foundSink != 0u) break;
            U32 pc = s_popCount;
            if (pc == 0u) {
                if (tid == 0 && doingLa && s_laCnt[s_laCur] == 0u && s_laCnt[s_laCur ^ 1u] > 0u) {
                    s_laCur ^= 1u;
                }
                __syncthreads();
                continue;
            }
            if (tid == 0) s_pops += static_cast<I64>(pc);
            __syncthreads();

            if (q.budget > 0 && s_pops > static_cast<I64>(q.budget)) {
                if (tid == 0) s_abortStatus = FG_STATUS_CROSSED_BUDGET;
                __syncthreads();
                break;
            }

            // Phase 2: Expand popped nodes
            const U32 nextLa = s_laCur ^ 1u;
            const U32 ovC = s_ovCur;
            const U32 bKeyBase = s_baseKey;
            const bool useBox = (q.flags & FG_QF_USE_BOX) != 0u;
            U64 nm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};

            auto processEdge = [&](U32 u, const FidesGpuNodeHot& pg, float uG, int enteringTileId, U32 child) {
                FidesGpuNodeHot cg = loadHot(G.hot, child);
                int childX = hotEndX(cg);
                int childY = d_endY(cg, pg);
                if (useBox && child != q.sink) {
                    if (!(childX > q.xmin && childX < q.xmax && childY > q.ymin && childY < q.ymax)) return;
                }
                U32 h0 = d_mixId(child) & cfg.hashMask;
                if (M.keys[h0] == (child | CLOSED_BIT)) return;

                FidesGpuDynNode dy = loadDyn(G.dyn, child);
                int occVal = dy.occ;
                int sameUses = (occVal > 0 && q.sameNetMode != 0u)
                        ? d_lookupSameNet(q, child, sameKeys, sameVals) : 0;
                if (occVal > 0 && q.oldLen > 0u) {
                    int oldCnt = d_countOldPath(q, child, oldNodes, s_oldNodes, 512u);
                    for (int r = 0; r < oldCnt; ++r) {
                        if (sameUses > 0 && --sameUses == 0) --occVal;
                    }
                    if (occVal < 0) occVal = 0;
                }

                bool la = false, earlyTerm = false;
                int acc = d_checkAccessAndLookahead(
                        G, pg, enteringTileId, child, cg, childX, childY, occVal, sameUses, q, la, earlyTerm);
                if (acc == 1) return;
                if (acc < 0) { atomicCAS(&s_abortStatus, 0, FG_STATUS_FALLBACK); return; }

                float newPartial = 0.0f, newTotal = 0.0f;
                if (!d_computeCost(pg, cg, childX, childY, occVal, dy.hist, sameUses, uG, q, wp, newPartial, newTotal)) {
                    atomicCAS(&s_abortStatus, 0, FG_STATUS_FALLBACK);
                    return;
                }

                if (earlyTerm) {
                    atomicMin(reinterpret_cast<unsigned long long*>(&s_bestSinkGP),
                              static_cast<unsigned long long>(packGPrev(newPartial, u)));
                    atomicExch(&s_foundSink, 1u);
                    return;
                }

                int ins = hashMarkOnPush(
                        M.keys, M.gPrev, M.touched, &s_touchedCnt,
                        cfg.hashMask, cfg.maxTouched, cfg.maxProbes,
                        child, u, newPartial);
                if (ins < 0) { atomicCAS(&s_abortStatus, 0, FG_STATUS_HASH_OVERFLOW); return; }
                if (ins == 0) return;

                if (la) {
                    U32 lpos = atomicAdd(&s_laCnt[nextLa], 1u);
                    if (lpos < cfg.laCap) M.laBuf[nextLa][lpos] = child;
                    else atomicCAS(&s_abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                } else {
                    U32 bk = floatBucketKey(newTotal, wp.bucketShift);
                    U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                    bool pushed = false;
                    if (rel < static_cast<U32>(NUM_BUCKETS) && s_bckCnt[rel] < cfg.bckCap) {
                        U32 bpos = atomicAdd(&s_bckCnt[rel], 1u);
                        if (bpos < cfg.bckCap) {
                            M.buckets[rel * cfg.bckCap + bpos] = child;
                            nm[rel >> 6] |= (1ULL << (rel & 63u));
                            pushed = true;
                        } else {
                            atomicSub(&s_bckCnt[rel], 1u);
                        }
                    }
                    if (!pushed) {
                        U32 opos = atomicAdd(&s_ovCnt[ovC], 1u);
                        if (opos < cfg.ovCap) {
                            M.ovNode[ovC][opos] = child;
                            M.ovKey[ovC][opos] = bk;
                        } else {
                            atomicCAS(&s_abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                        }
                    }
                }
            };

            if (pc >= 32u) {
                if (static_cast<U32>(tid) < pc) {
                    U32 u = s_popNodes[tid];
                    float uG = s_popG[tid];
                    U32 uPrev = s_popPrev[tid];
                    FidesGpuNodeHot pg = loadHot(G.hot, u);
                    int enteringTileId = ((hotPreFlags(pg) & FG_PF_SLL) && uPrev != UINT32_MAX)
                            ? hotTileId(loadHot(G.hot, uPrev)) : -1;
                    U32 e0 = s_edgeStart[tid], e1 = s_edgeEnd[tid];
                    for (U32 e = e0; e < e1; ++e) {
                        if (s_foundSink != 0u || s_abortStatus != 0) break;
                        processEdge(u, pg, uG, enteringTileId, __ldg(G.edge + e));
                    }
                }
            } else {
                for (U32 i = warpId; i < pc; i += (TB_THREADS / 32)) {
                    U32 u = s_popNodes[i];
                    float uG = s_popG[i];
                    U32 uPrev = s_popPrev[i];
                    FidesGpuNodeHot pg = loadHot(G.hot, u);
                    int enteringTileId = ((hotPreFlags(pg) & FG_PF_SLL) && uPrev != UINT32_MAX)
                            ? hotTileId(loadHot(G.hot, uPrev)) : -1;
                    U32 e0 = s_edgeStart[i], e1 = s_edgeEnd[i];
                    for (U32 e = e0 + lane; e < e1; e += 32u) {
                        processEdge(u, pg, uG, enteringTileId, __ldg(G.edge + e));
                    }
                }
            }

            for (int w = 0; w < MASK_WORDS; ++w) {
                for (int off = 16; off > 0; off >>= 1) nm[w] |= __shfl_down_sync(0xFFFFFFFFu, nm[w], off);
                if (lane == 0) s_warpMasks[warpId][w] = nm[w];
            }
            __syncthreads();
            if (tid == 0) {
                for (int w = 0; w < MASK_WORDS; ++w) {
                    U64 comb = 0ULL;
                    for (int wi = 0; wi < TB_THREADS / 32; ++wi) comb |= s_warpMasks[wi][w];
                    s_activeMask[w] |= comb;
                }
                if (s_laCnt[nextLa] > 0u && s_laCnt[s_laCur] == 0u) s_laCur = nextLa;
            }
            __syncthreads();
        }

        if (tid == 0) {
            FidesGpuResult res{};
            res.pops = s_pops;
            res.pathOffset = 0u;
            if (s_foundSink != 0u) {
                U32 len = 1u;
                U32 cur = static_cast<U32>(s_bestSinkGP & 0xFFFFFFFFu);
                bool ok = (cur != UINT32_MAX || q.sink == q.src);
                U32 maxAllowed = (q.outPathCap > 0u) ? q.outPathCap : 8192u;
                while (ok && cur != UINT32_MAX) {
                    ++len;
                    if (len > maxAllowed) { ok = false; break; }
                    if (cur == q.src) break;
                    U32 sl = hashFindSlot(M.keys, cfg.hashMask, cfg.maxProbes, cur);
                    if (sl == UINT32_MAX) { ok = false; break; }
                    cur = static_cast<U32>(M.gPrev[sl] & 0xFFFFFFFFu);
                }
                if (ok && (cur == q.src || q.sink == q.src)) {
                    U32 pOff = atomicAdd(outPathCursor, len);
                    if (pOff + len <= outPathsCap) {
                        U32* dst = outPaths + pOff;
                        U32 w = 0u;
                        dst[w++] = q.sink;
                        U32 c2 = static_cast<U32>(s_bestSinkGP & 0xFFFFFFFFu);
                        while (c2 != UINT32_MAX && w < len) {
                            dst[w++] = c2;
                            if (c2 == q.src) break;
                            U32 sl = hashFindSlot(M.keys, cfg.hashMask, cfg.maxProbes, c2);
                            c2 = static_cast<U32>(M.gPrev[sl] & 0xFFFFFFFFu);
                        }
                        res.status = static_cast<I32>(len);
                        res.pathOffset = pOff;
                    } else {
                        res.status = FG_STATUS_FALLBACK;
                    }
                } else {
                    res.status = FG_STATUS_FALLBACK;
                }
            } else if (s_abortStatus != 0) {
                res.status = s_abortStatus;
            } else {
                res.status = FG_STATUS_NO_PATH;
            }
            outResults[qIdx] = res;
        }
        __syncthreads();

        U32 tc = s_touchedCnt;
        if (tc < cfg.hashCap / 4u && tc <= cfg.maxTouched) {
            for (U32 i = tid; i < tc; i += TB_THREADS) {
                U32 sl = M.touched[i];
                M.keys[sl] = EMPTY_KEY;
                M.gPrev[sl] = INF_GPREV;
            }
        } else {
            for (U32 i = tid; i < cfg.hashCap; i += TB_THREADS) {
                M.keys[i] = EMPTY_KEY;
                M.gPrev[i] = INF_GPREV;
            }
        }
        __syncthreads();
    }
}

// ============================================================================
// Tier-B2: Whole-GPU Cooperative Multi-CTA Mega-Tail (zero host launch overhead!)
// ============================================================================

__global__ void __launch_bounds__(256, 1)
kTierBMegaCoopSearch(
        GpuGraphDevice G, TierBMegaMem M,
        FidesGpuWaveParams wp,
        const FidesGpuQuery* queries, U32 numQueries,
        const U32* sameKeys, const I32* sameVals, const U32* oldNodes,
        FidesGpuResult* outResults, U32* outPaths) {
    namespace cg = cooperative_groups;
    cg::grid_group grid = cg::this_grid();
    const U32 gtid = blockIdx.x * blockDim.x + threadIdx.x;
    const U32 gstride = gridDim.x * blockDim.x;
    const int lane = threadIdx.x & 31;
    const U32 maxPop = (wp.popBatch > 0u && wp.popBatch <= TM_FRONT_CAP) ? wp.popBatch : 8192u;
    TierBMegaCtrl* C = M.ctrl;

    for (U32 qi = 0; qi < numQueries; ++qi) {
        FidesGpuQuery q = queries[qi];

        if (gtid == 0u) {
            C->baseKey = computeInitialBaseKey(G, q, wp);
            for (int w = 0; w < MASK_WORDS; ++w) C->activeMask[w] = 0ULL;
            C->activeMask[0] = 1ULL;
            for (int b = 0; b < NUM_BUCKETS; ++b) C->bckCnt[b] = 0u;
            C->bckCnt[0] = 1u;
            C->ovCur = 0u; C->ovCnt[0] = 0u; C->ovCnt[1] = 0u;
            C->laCur = 0u; C->laCnt[0] = 0u; C->laCnt[1] = 0u;
            C->laDepth = 0u;
            C->touchedCnt = 1u;
            C->frontCnt = 0u;
            C->activeCnt = 0u;
            C->pops = 0;
            C->foundSink = 0u;
            C->bestSinkGP = INF_GPREV;
            C->abortStatus = 0;
            C->done = 0u;
            C->minOvKey = UINT32_MAX;

            M.state[q.src] = 1u;
            M.gPrev[q.src] = packGPrev(0.0f, UINT32_MAX);
            M.touched[0] = q.src;
            M.buckets[0] = q.src;
        }
        grid.sync();

        while (C->abortStatus == 0 && C->foundSink == 0u) {
            bool doingLa = (C->laCnt[C->laCur] > 0u);
            if (!doingLa) {
                if (gtid == 0u) C->laDepth = 0u;
                if (!anyMask256(C->activeMask)) {
                    U32 ovC = C->ovCur;
                    U32 ovN = C->ovCnt[ovC];
                    if (ovN == 0u) break;

                    if (gtid == 0u) C->minOvKey = UINT32_MAX;
                    grid.sync();

                    U32 localMin = UINT32_MAX;
                    const U32* ovNodes = M.ovNode[ovC];
                    const U32* ovKeys  = M.ovKey[ovC];
                    for (U32 i = gtid; i < ovN; i += gstride) {
                        U32 nid = ovNodes[i];
                        if (M.state[nid] == 1u) {
                            U32 bk = ovKeys[i];
                            if (bk < localMin) localMin = bk;
                        }
                    }
                    for (int off = 16; off > 0; off >>= 1) {
                        U32 o = __shfl_down_sync(0xFFFFFFFFu, localMin, off);
                        if (o < localMin) localMin = o;
                    }
                    if (lane == 0 && localMin < UINT32_MAX) atomicMin(&C->minOvKey, localMin);
                    grid.sync();

                    U32 minK = C->minOvKey;
                    if (minK == UINT32_MAX) {
                        if (gtid == 0u) C->ovCnt[ovC] = 0u;
                        grid.sync();
                        break;
                    }
                    U32 bKeyBase = (minK > 64u) ? (minK - 64u) : 0u;
                    U32 nextOv = ovC ^ 1u;
                    if (gtid == 0u) {
                        C->baseKey = bKeyBase;
                        C->ovCnt[nextOv] = 0u;
                    }
                    grid.sync();

                    U32* dstNodes = M.ovNode[nextOv];
                    U32* dstKeys  = M.ovKey[nextOv];
                    U64 lm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};
                    for (U32 i = gtid; i < ovN; i += gstride) {
                        U32 nid = ovNodes[i];
                        if (M.state[nid] != 1u) continue;
                        U32 bk = ovKeys[i];
                        U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                        bool pushed = false;
                        if (rel < static_cast<U32>(NUM_BUCKETS) && C->bckCnt[rel] < TM_BCK_CAP) {
                            U32 pos = atomicAdd(&C->bckCnt[rel], 1u);
                            if (pos < TM_BCK_CAP) {
                                M.buckets[rel * TM_BCK_CAP + pos] = nid;
                                lm[rel >> 6] |= (1ULL << (rel & 63u));
                                pushed = true;
                            } else {
                                atomicSub(&C->bckCnt[rel], 1u);
                            }
                        }
                        if (!pushed) {
                            U32 opos = atomicAdd(&C->ovCnt[nextOv], 1u);
                            if (opos < TM_OV_CAP) {
                                dstNodes[opos] = nid;
                                dstKeys[opos] = bk;
                            } else {
                                atomicCAS(&C->abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                            }
                        }
                    }
                    for (int w = 0; w < MASK_WORDS; ++w) {
                        for (int off = 16; off > 0; off >>= 1) lm[w] |= __shfl_down_sync(0xFFFFFFFFu, lm[w], off);
                        if (lane == 0 && lm[w] != 0ULL) {
                            atomicOr(reinterpret_cast<unsigned long long*>(&C->activeMask[w]),
                                     static_cast<unsigned long long>(lm[w]));
                        }
                    }
                    grid.sync();
                    if (gtid == 0u) {
                        C->ovCnt[ovC] = 0u;
                        C->ovCur = nextOv;
                    }
                    grid.sync();
                    if (C->abortStatus != 0) break;
                    if (!anyMask256(C->activeMask)) continue;
                }
            } else {
                if (gtid == 0u && ++C->laDepth >= 256u) C->abortStatus = FG_STATUS_FALLBACK;
                grid.sync();
                if (C->abortStatus != 0) break;
            }

            // Phase 1: Pop up to maxPop nodes and mark CLOSED
            U32 take = 0u;
            const U32* srcBuf = nullptr;
            int poppedBucket = -1;
            if (doingLa) {
                U32 curLa = C->laCur;
                U32 cnt = C->laCnt[curLa];
                take = (cnt < maxPop) ? cnt : maxPop;
                srcBuf = M.laBuf[curLa] + (cnt - take);
            } else {
                poppedBucket = findMinBucket256(C->activeMask);
                U32 cnt = C->bckCnt[poppedBucket];
                take = (cnt < maxPop) ? cnt : maxPop;
                srcBuf = M.buckets + static_cast<U32>(poppedBucket) * TM_BCK_CAP + (cnt - take);
            }
            if (gtid == 0u) C->activeCnt = 0u;
            grid.sync();

            for (U32 i = gtid; i < take; i += gstride) {
                U32 u = srcBuf[i];
                U32 prevSt = atomicCAS(M.state + u, 1u, 2u);
                if (prevSt == 1u) {
                    if (u == q.sink) {
                        U64 gp = M.gPrev[u];
                        atomicMin(reinterpret_cast<unsigned long long*>(&C->bestSinkGP),
                                  static_cast<unsigned long long>(gp));
                        atomicExch(&C->foundSink, 1u);
                    } else {
                        U32 pos = atomicAdd(&C->activeCnt, 1u);
                        M.frontActive[pos] = u;
                    }
                }
            }
            grid.sync();

            if (gtid == 0u) {
                if (doingLa) {
                    C->laCnt[C->laCur] -= take;
                } else {
                    C->bckCnt[poppedBucket] -= take;
                    if (C->bckCnt[poppedBucket] == 0u) {
                        C->activeMask[poppedBucket >> 6] &= ~(1ULL << (poppedBucket & 63));
                    }
                }
                C->pops += static_cast<I64>(C->activeCnt + (C->foundSink ? 1u : 0u));
                if (C->foundSink == 0u && q.budget > 0 && C->pops > static_cast<I64>(q.budget)) {
                    C->abortStatus = FG_STATUS_CROSSED_BUDGET;
                }
            }
            grid.sync();

            if (C->foundSink != 0u || C->abortStatus != 0) break;
            U32 ac = C->activeCnt;
            if (ac == 0u) {
                if (gtid == 0u && doingLa && C->laCnt[C->laCur] == 0u && C->laCnt[C->laCur ^ 1u] > 0u) {
                    C->laCur ^= 1u;
                }
                grid.sync();
                continue;
            }

            // Phase 2: Expand all popped nodes in parallel across all 80 SMs
            const U32 nextLa = C->laCur ^ 1u;
            const U32 ovC = C->ovCur;
            const U32 bKeyBase = C->baseKey;
            const bool useBox = (q.flags & FG_QF_USE_BOX) != 0u;
            U64 lm[MASK_WORDS] = {0ULL, 0ULL, 0ULL, 0ULL};

            for (U32 i = gtid; i < ac; i += gstride) {
                if (C->foundSink != 0u || C->abortStatus != 0) break;
                U32 u = M.frontActive[i];
                U64 gp = M.gPrev[u];
                float uG = __uint_as_float(static_cast<U32>(gp >> 32));
                U32 uPrev = static_cast<U32>(gp & 0xFFFFFFFFu);
                FidesGpuNodeHot pg = loadHot(G.hot, u);
                int enteringTileId = ((hotPreFlags(pg) & FG_PF_SLL) && uPrev != UINT32_MAX)
                        ? hotTileId(loadHot(G.hot, uPrev)) : -1;
                U32 e0 = __ldg(G.offset + u), e1 = __ldg(G.offset + u + 1u);
                if (G.coreDeg != nullptr) {
                    uint8_t cd = __ldg(G.coreDeg + u);
                    if (cd < 255u && (e0 + static_cast<U32>(cd) < e1)) {
                        const int ux = hotEndX(pg), uy = hotEndYRaw(pg);
                        const bool needSeg1 = (ux == q.sinkEndX && fides_absInt(uy - q.sinkEndY) <= 3)
                                           || (ux == q.sinkBeginX && fides_absInt(uy - q.sinkBeginY) <= 2);
                        if (!needSeg1) e1 = e0 + static_cast<U32>(cd);
                    }
                }
                for (U32 e = e0; e < e1; ++e) {
                    U32 child = __ldg(G.edge + e);
                    if (M.state[child] == 2u) continue;
                    FidesGpuNodeHot cg = loadHot(G.hot, child);
                    int childX = hotEndX(cg);
                    int childY = d_endY(cg, pg);
                    if (useBox && child != q.sink) {
                        if (!(childX > q.xmin && childX < q.xmax && childY > q.ymin && childY < q.ymax)) continue;
                    }
                    FidesGpuDynNode dy = loadDyn(G.dyn, child);
                    int occVal = dy.occ;
                    int sameUses = (occVal > 0 && q.sameNetMode != 0u)
                            ? d_lookupSameNet(q, child, sameKeys, sameVals) : 0;
                    if (occVal > 0 && q.oldLen > 0u) {
                        int oldCnt = d_countOldPath(q, child, oldNodes, nullptr, 0u);
                        for (int r = 0; r < oldCnt; ++r) {
                            if (sameUses > 0 && --sameUses == 0) --occVal;
                        }
                        if (occVal < 0) occVal = 0;
                    }

                    bool la = false, earlyTerm = false;
                    int acc = d_checkAccessAndLookahead(
                            G, pg, enteringTileId, child, cg, childX, childY, occVal, sameUses, q, la, earlyTerm);
                    if (acc == 1) continue;
                    if (acc < 0) { atomicCAS(&C->abortStatus, 0, FG_STATUS_FALLBACK); break; }
                    float newPartial = 0.0f, newTotal = 0.0f;
                    if (!d_computeCost(pg, cg, childX, childY, occVal, dy.hist, sameUses, uG, q, wp, newPartial, newTotal)) {
                        atomicCAS(&C->abortStatus, 0, FG_STATUS_FALLBACK);
                        break;
                    }
                    if (earlyTerm) {
                        atomicMin(reinterpret_cast<unsigned long long*>(&C->bestSinkGP),
                                  static_cast<unsigned long long>(packGPrev(newPartial, u)));
                        atomicExch(&C->foundSink, 1u);
                        break;
                    }

                    U32 prevSt = atomicCAS(M.state + child, 0u, 1u);
                    if (prevSt != 0u) continue;
                    U32 tPos = atomicAdd(&C->touchedCnt, 1u);
                    if (tPos < TM_MAX_TOUCHED) M.touched[tPos] = child;
                    else { atomicCAS(&C->abortStatus, 0, FG_STATUS_HASH_OVERFLOW); break; }
                    M.gPrev[child] = packGPrev(newPartial, u);

                    if (la) {
                        U32 lpos = atomicAdd(&C->laCnt[nextLa], 1u);
                        if (lpos < TM_LA_CAP) M.laBuf[nextLa][lpos] = child;
                        else atomicCAS(&C->abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                    } else {
                        U32 bk = floatBucketKey(newTotal, wp.bucketShift);
                        U32 rel = (bk >= bKeyBase) ? (bk - bKeyBase) : 0u;
                        bool pushed = false;
                        if (rel < static_cast<U32>(NUM_BUCKETS) && C->bckCnt[rel] < TM_BCK_CAP) {
                            U32 bpos = atomicAdd(&C->bckCnt[rel], 1u);
                            if (bpos < TM_BCK_CAP) {
                                M.buckets[rel * TM_BCK_CAP + bpos] = child;
                                lm[rel >> 6] |= (1ULL << (rel & 63u));
                                pushed = true;
                            } else {
                                atomicSub(&C->bckCnt[rel], 1u);
                            }
                        }
                        if (!pushed) {
                            U32 opos = atomicAdd(&C->ovCnt[ovC], 1u);
                            if (opos < TM_OV_CAP) {
                                M.ovNode[ovC][opos] = child;
                                M.ovKey[ovC][opos] = bk;
                            } else {
                                atomicCAS(&C->abortStatus, 0, FG_STATUS_HASH_OVERFLOW);
                            }
                        }
                    }
                }
            }
            for (int w = 0; w < MASK_WORDS; ++w) {
                for (int off = 16; off > 0; off >>= 1) lm[w] |= __shfl_down_sync(0xFFFFFFFFu, lm[w], off);
                if (lane == 0 && lm[w] != 0ULL) {
                    atomicOr(reinterpret_cast<unsigned long long*>(&C->activeMask[w]),
                             static_cast<unsigned long long>(lm[w]));
                }
            }
            grid.sync();
            if (gtid == 0u) {
                if (C->laCnt[nextLa] > 0u && C->laCnt[C->laCur] == 0u) C->laCur = nextLa;
            }
            grid.sync();
        }

        // Reconstruct path (gtid == 0)
        if (gtid == 0u) {
            FidesGpuResult res{};
            res.pops = C->pops;
            res.pathOffset = q.outPathOffset;
            if (C->foundSink != 0u) {
                U32 len = 0u, cap = q.outPathCap;
                U32* dst = outPaths + q.outPathOffset;
                if (cap > 0u) dst[len++] = q.sink;
                U32 cur = static_cast<U32>(C->bestSinkGP & 0xFFFFFFFFu);
                bool ok = (cur != UINT32_MAX || q.sink == q.src);
                while (ok && cur != UINT32_MAX) {
                    if (len >= cap) { ok = false; break; }
                    dst[len++] = cur;
                    if (cur == q.src) break;
                    if (M.state[cur] == 0u) { ok = false; break; }
                    cur = static_cast<U32>(M.gPrev[cur] & 0xFFFFFFFFu);
                }
                res.status = (ok && len > 0u && dst[len - 1u] == q.src) ? static_cast<I32>(len) : FG_STATUS_FALLBACK;
            } else if (C->abortStatus != 0) {
                res.status = C->abortStatus;
            } else {
                res.status = FG_STATUS_NO_PATH;
            }
            outResults[qi] = res;
        }
        grid.sync();

        // Parallel sparse cleanup across all 80 SMs (< 50 us!)
        U32 tc = C->touchedCnt;
        if (tc > TM_MAX_TOUCHED) tc = TM_MAX_TOUCHED;
        for (U32 i = gtid; i < tc; i += gstride) {
            U32 nid = M.touched[i];
            M.state[nid] = 0u;
            M.gPrev[nid] = INF_GPREV;
        }
        grid.sync();
    }
}

bool ensureWaveBuffersDev(int d, U32 numQueries, U32 sameLen, U32 outPathsCap, U32 oldNodesLen = 0u) {
    GpuState& GS = GS_ARR[d];
    if (numQueries > GS.capQueries) {
        size_t nc = std::max<size_t>(numQueries, 16384u);
        if (GS.d_queries) cudaFree(GS.d_queries);
        if (GS.d_results) cudaFree(GS.d_results);
        if (GS.h_queriesPinned) cudaFreeHost(GS.h_queriesPinned);
        if (GS.h_resultsPinned) cudaFreeHost(GS.h_resultsPinned);
        if (!checkCuda(cudaMalloc(&GS.d_queries, nc * sizeof(FidesGpuQuery)), "malloc d_queries")) return false;
        if (!checkCuda(cudaMalloc(&GS.d_results, nc * sizeof(FidesGpuResult)), "malloc d_results")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_queriesPinned, nc * sizeof(FidesGpuQuery)), "mallocHost h_queries")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_resultsPinned, nc * sizeof(FidesGpuResult)), "mallocHost h_results")) return false;
        GS.capQueries = nc; GS.capResults = nc;
    }
    if (sameLen > GS.capSame) {
        size_t nc = std::max<size_t>(sameLen, 2u << 20);
        if (GS.d_sameKeys) cudaFree(GS.d_sameKeys);
        if (GS.d_sameVals) cudaFree(GS.d_sameVals);
        if (GS.h_sameKeysPinned) cudaFreeHost(GS.h_sameKeysPinned);
        if (GS.h_sameValsPinned) cudaFreeHost(GS.h_sameValsPinned);
        if (!checkCuda(cudaMalloc(&GS.d_sameKeys, nc * sizeof(U32)), "malloc d_sameKeys")) return false;
        if (!checkCuda(cudaMalloc(&GS.d_sameVals, nc * sizeof(I32)), "malloc d_sameVals")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_sameKeysPinned, nc * sizeof(U32)), "mallocHost h_sameKeys")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_sameValsPinned, nc * sizeof(I32)), "mallocHost h_sameVals")) return false;
        GS.capSame = nc;
    }
    if (outPathsCap > GS.capOutPaths) {
        size_t nc = std::max<size_t>(outPathsCap, 8u << 20);
        if (GS.d_outPaths) cudaFree(GS.d_outPaths);
        if (GS.h_outPathsPinned) cudaFreeHost(GS.h_outPathsPinned);
        if (!checkCuda(cudaMalloc(&GS.d_outPaths, nc * sizeof(U32)), "malloc d_outPaths")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_outPathsPinned, nc * sizeof(U32)), "mallocHost h_outPaths")) return false;
        GS.capOutPaths = nc;
    }
    if (oldNodesLen > GS.capOldNodes) {
        size_t nc = std::max<size_t>(oldNodesLen, 1u << 20);
        if (GS.d_oldNodes) cudaFree(GS.d_oldNodes);
        if (GS.h_oldNodesPinned) cudaFreeHost(GS.h_oldNodesPinned);
        if (!checkCuda(cudaMalloc(&GS.d_oldNodes, nc * sizeof(U32)), "malloc d_oldNodes")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_oldNodesPinned, nc * sizeof(U32)), "mallocHost h_oldNodes")) return false;
        GS.capOldNodes = nc;
    }
    return true;
}

bool ensureDeltaBuffersDev(int d, U32 count) {
    GpuState& GS = GS_ARR[d];
    if (count > GS.capDeltas) {
        size_t nc = std::max<size_t>(count, 1u << 20);
        if (GS.d_deltaIds) cudaFree(GS.d_deltaIds);
        if (GS.d_deltaOcc) cudaFree(GS.d_deltaOcc);
        if (GS.d_deltaHist) cudaFree(GS.d_deltaHist);
        if (GS.h_deltaIdsPinned) cudaFreeHost(GS.h_deltaIdsPinned);
        if (GS.h_deltaOccPinned) cudaFreeHost(GS.h_deltaOccPinned);
        if (GS.h_deltaHistPinned) cudaFreeHost(GS.h_deltaHistPinned);
        if (!checkCuda(cudaMalloc(&GS.d_deltaIds, nc * sizeof(U32)), "malloc d_deltaIds")) return false;
        if (!checkCuda(cudaMalloc(&GS.d_deltaOcc, nc * sizeof(I32)), "malloc d_deltaOcc")) return false;
        if (!checkCuda(cudaMalloc(&GS.d_deltaHist, nc * sizeof(float)), "malloc d_deltaHist")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_deltaIdsPinned, nc * sizeof(U32)), "mallocHost h_deltaIds")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_deltaOccPinned, nc * sizeof(I32)), "mallocHost h_deltaOcc")) return false;
        if (!checkCuda(cudaMallocHost(&GS.h_deltaHistPinned, nc * sizeof(float)), "mallocHost h_deltaHist")) return false;
        GS.capDeltas = nc;
    }
    return true;
}

void shutdownDevice(int d) {
    GpuState& GS = GS_ARR[d];
    cudaSetDevice(GS.cudaDev);
    if (GS.stream) {
        cudaStreamSynchronize(GS.stream);
        cudaStreamDestroy(GS.stream);
        GS.stream = nullptr;
    }
    auto freeDev = [](auto*& p) { if (p) { cudaFree(p); p = nullptr; } };
    auto freeHost = [](auto*& p) { if (p) { cudaFreeHost(p); p = nullptr; } };
    freeDev(GS.d_offset); freeDev(GS.d_edge); freeDev(GS.d_coreDeg); freeDev(GS.d_hot); freeDev(GS.d_dyn);
    freeDev(GS.d_taPool); freeDev(GS.d_tbPool); freeDev(GS.d_tmPool);
    freeDev(GS.d_nextQuery); freeDev(GS.d_outPathCursor); freeDev(GS.d_queries); freeDev(GS.d_results);
    freeDev(GS.d_outPaths); freeDev(GS.d_sameKeys); freeDev(GS.d_sameVals); freeDev(GS.d_oldNodes);
    freeDev(GS.d_deltaIds); freeDev(GS.d_deltaOcc); freeDev(GS.d_deltaHist); freeDev(GS.d_megaPatches);
    for (int i = 0; i < MAX_MEGA_SCOPES; ++i) {
        freeDev(GS.d_megaKeysHost[i]); freeDev(GS.d_megaValsHost[i]);
        GS.megaCap[i] = 0;
    }
    freeHost(GS.h_megaStageKeysPinned); freeHost(GS.h_megaStageValsPinned);
    GS.capMegaStage = 0;
    if (GS.megaStageEvent) {
        cudaEventDestroy(GS.megaStageEvent);
        GS.megaStageEvent = nullptr;
    }
    GS.megaStageEventRecorded = false;
    freeHost(GS.h_tmCtrlPinned); freeHost(GS.h_outPathCursorPinned); freeHost(GS.h_queriesPinned); freeHost(GS.h_resultsPinned);
    freeHost(GS.h_outPathsPinned); freeHost(GS.h_sameKeysPinned); freeHost(GS.h_sameValsPinned); freeHost(GS.h_oldNodesPinned);
    freeHost(GS.h_deltaIdsPinned); freeHost(GS.h_deltaOccPinned); freeHost(GS.h_deltaHistPinned); freeHost(GS.h_megaPatchesPinned);
    GS.capQueries = GS.capResults = GS.capOutPaths = GS.capSame = GS.capOldNodes = GS.capDeltas = GS.capMegaPatches = 0;
    GS.ready = false;
}

bool initDevice(
        int slot,
        uint32_t n, uint32_t m,
        const uint32_t* offset, const uint32_t* edge,
        const uint8_t* coreDeg,
        const FidesGpuNodeHot* hot, const FidesGpuDynNode* dyn,
        const uint32_t* ord16, int32_t sllLen,
        const int32_t* nextLaguna, const int32_t* prevLaguna, int32_t lagLen,
        const float* pcc, int32_t pccLen,
        int totalSlots,
        FidesGpuInitInfo* outInfo) {
    GpuState& GS = GS_ARR[slot];
    auto t0 = std::chrono::steady_clock::now();
    if (!checkCuda(cudaSetDevice(GS.cudaDev), "init cudaSetDevice")) return false;

    cudaDeviceProp prop{};
    if (!checkCuda(cudaGetDeviceProperties(&prop, GS.cudaDev), "cudaGetDeviceProperties")) return false;
    bindThreadToPciNuma(prop, &GS.numaNode);
    size_t freeBefore = 0, totalBytes = 0;
    if (!checkCuda(cudaMemGetInfo(&freeBefore, &totalBytes), "cudaMemGetInfo")) return false;
    if (lagLen > MAX_LAG_X || pccLen > MAX_PCC) return false;
    if (!checkCuda(cudaStreamCreateWithFlags(&GS.stream, cudaStreamNonBlocking), "cudaStreamCreate")) return false;
    GS.n = n; GS.m = m;

    size_t offBytes  = (size_t(n) + 1) * sizeof(U32);
    size_t edgeBytes = size_t(m) * sizeof(U32);
    size_t coreBytes = (coreDeg != nullptr) ? (size_t(n) * sizeof(uint8_t)) : 0u;
    size_t hotBytes  = size_t(n) * sizeof(FidesGpuNodeHot);
    size_t dynBytes  = size_t(n) * sizeof(FidesGpuDynNode);
    size_t residentBytes = offBytes + edgeBytes + coreBytes + hotBytes + dynBytes;

    if (!checkCuda(cudaMalloc(&GS.d_offset, offBytes), "malloc d_offset")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMalloc(&GS.d_edge, edgeBytes), "malloc d_edge")) { shutdownDevice(slot); return false; }
    if (coreBytes > 0u) {
        if (!checkCuda(cudaMalloc(&GS.d_coreDeg, coreBytes), "malloc d_coreDeg")) { shutdownDevice(slot); return false; }
    }
    if (!checkCuda(cudaMalloc(&GS.d_hot, hotBytes), "malloc d_hot")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMalloc(&GS.d_dyn, dynBytes), "malloc d_dyn")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMalloc(&GS.d_nextQuery, sizeof(U32)), "malloc d_nextQuery")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMalloc(&GS.d_outPathCursor, sizeof(U32)), "malloc d_outPathCursor")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMallocHost(&GS.h_outPathCursorPinned, sizeof(U32)), "mallocHost h_outPathCursor")) { shutdownDevice(slot); return false; }

    if (!checkCuda(cudaMemcpyAsync(GS.d_offset, offset, offBytes, cudaMemcpyHostToDevice, GS.stream), "H2D offset")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMemcpyAsync(GS.d_edge, edge, edgeBytes, cudaMemcpyHostToDevice, GS.stream), "H2D edge")) { shutdownDevice(slot); return false; }
    if (coreBytes > 0u) {
        if (!checkCuda(cudaMemcpyAsync(GS.d_coreDeg, coreDeg, coreBytes, cudaMemcpyHostToDevice, GS.stream), "H2D coreDeg")) { shutdownDevice(slot); return false; }
    }
    if (!checkCuda(cudaMemcpyAsync(GS.d_hot, hot, hotBytes, cudaMemcpyHostToDevice, GS.stream), "H2D hot")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMemcpyAsync(GS.d_dyn, dyn, dynBytes, cudaMemcpyHostToDevice, GS.stream), "H2D dyn")) { shutdownDevice(slot); return false; }

    if (!checkCuda(cudaMemcpyToSymbolAsync(c_ord, ord16, OrdinalCount * sizeof(U32), 0, cudaMemcpyHostToDevice, GS.stream), "sym ord")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMemcpyToSymbolAsync(c_sllLen, &sllLen, sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "sym sllLen")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMemcpyToSymbolAsync(c_lagLen, &lagLen, sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "sym lagLen")) { shutdownDevice(slot); return false; }
    if (lagLen > 0) {
        if (!checkCuda(cudaMemcpyToSymbolAsync(c_nextLag, nextLaguna, lagLen * sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "sym nextLag")) { shutdownDevice(slot); return false; }
        if (!checkCuda(cudaMemcpyToSymbolAsync(c_prevLag, prevLaguna, lagLen * sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "sym prevLag")) { shutdownDevice(slot); return false; }
    }
    if (!checkCuda(cudaMemcpyToSymbolAsync(c_pccLen, &pccLen, sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "sym pccLen")) { shutdownDevice(slot); return false; }
    if (pccLen > 0) {
        if (!checkCuda(cudaMemcpyToSymbolAsync(c_pcc, pcc, pccLen * sizeof(float), 0, cudaMemcpyHostToDevice, GS.stream), "sym pcc")) { shutdownDevice(slot); return false; }
    }

    size_t freeAfterGraph = 0;
    checkCuda(cudaMemGetInfo(&freeAfterGraph, &totalBytes), "meminfo after graph");
    const size_t headroom = (totalSlots >= 2) ? (size_t(5) << 29) : (size_t(9) << 29); // 2.5 GiB (multi) / 4.5 GiB (single)
    size_t usable = (freeAfterGraph > headroom) ? (freeAfterGraph - headroom) : 0;

    GS.tmTotalBytes = 0;
    GS.d_tmPool = nullptr;
    GS.h_tmCtrlPinned = nullptr;

    GS.taSlotBytes = alignUp(
        size_t(TA_HASH_CAP) * sizeof(U32)
        + size_t(TA_HASH_CAP) * sizeof(U64)
        + size_t(TA_MAX_TOUCHED) * sizeof(U32)
        + size_t(NUM_BUCKETS) * TA_BCK_CAP * sizeof(U32)
        + 4 * size_t(TA_OV_CAP) * sizeof(U32)
        + 2 * size_t(TA_LA_CAP) * sizeof(U32), 256);

    GS.tbSlotBytes = alignUp(
        size_t(TB_HASH_CAP) * sizeof(U32)
        + size_t(TB_HASH_CAP) * sizeof(U64)
        + size_t(TB_MAX_TOUCHED) * sizeof(U32)
        + size_t(NUM_BUCKETS) * TB_BCK_CAP * sizeof(U32)
        + 4 * size_t(TB_OV_CAP) * sizeof(U32)
        + 2 * size_t(TB_LA_CAP) * sizeof(U32), 256);

    if (usable < GS.taSlotBytes * 320 + GS.tbSlotBytes * 40) {
        shutdownDevice(slot); return false;
    }

    U32 maxCtas = static_cast<U32>((usable * 72 / 100) / GS.tbSlotBytes);
    GS.tbCtas = std::min<U32>(80u, std::max<U32>(40u, maxCtas));
    GS.tbTotalBytes = size_t(GS.tbCtas) * GS.tbSlotBytes;

    size_t remForTa = usable - GS.tbTotalBytes;
    U32 maxWarps = static_cast<U32>(remForTa / GS.taSlotBytes);
    maxWarps = (maxWarps / TA_WARPS_PER_BLOCK) * TA_WARPS_PER_BLOCK;
    U32 capTaWarps = (totalSlots >= 2) ? 1024u : 1280u;
    GS.taWarps = std::min<U32>(capTaWarps, std::max<U32>(320u, maxWarps));
    GS.taTotalBytes = size_t(GS.taWarps) * GS.taSlotBytes;

    if (!checkCuda(cudaMalloc(&GS.d_taPool, GS.taTotalBytes), "malloc d_taPool")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMalloc(&GS.d_tbPool, GS.tbTotalBytes), "malloc d_tbPool")) { shutdownDevice(slot); return false; }

    if (!checkCuda(cudaMemsetAsync(GS.d_taPool, 0xFF, GS.taTotalBytes, GS.stream), "memset d_taPool")) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaMemsetAsync(GS.d_tbPool, 0xFF, GS.tbTotalBytes, GS.stream), "memset d_tbPool")) { shutdownDevice(slot); return false; }
    GS.tbActiveMode = 0;

    if (!ensureWaveBuffersDev(slot, 16384u, 4u << 20, 8u << 20, 1u << 20)) { shutdownDevice(slot); return false; }
    if (!ensureDeltaBuffersDev(slot, 1u << 20)) { shutdownDevice(slot); return false; }
    if (!checkCuda(cudaEventCreateWithFlags(&GS.megaStageEvent, cudaEventDisableTiming), "megaStageEvent")) { shutdownDevice(slot); return false; }
    GS.megaStageEventRecorded = false;
    GS.tmGridBlocks = 0u;
    if (!checkCuda(cudaStreamSynchronize(GS.stream), "sync init")) { shutdownDevice(slot); return false; }

    size_t freeAfter = 0;
    checkCuda(cudaMemGetInfo(&freeAfter, &totalBytes), "meminfo final");
    double initSec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    GS.ready = true;

    if (outInfo) {
        outInfo->ready = true;
        outInfo->deviceId = GS.cudaDev;
        std::snprintf(outInfo->deviceName, sizeof(outInfo->deviceName), "%s", prop.name);
        const unsigned char* u = reinterpret_cast<const unsigned char*>(prop.uuid.bytes);
        std::snprintf(outInfo->deviceUuid, sizeof(outInfo->deviceUuid),
                      "GPU-%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                      u[0],u[1],u[2],u[3],u[4],u[5],u[6],u[7],u[8],u[9],u[10],u[11],u[12],u[13],u[14],u[15]);
        outInfo->totalBytes = totalBytes;
        outInfo->residentBytes = residentBytes;
        outInfo->arenaBytes = GS.taTotalBytes + GS.tbTotalBytes + GS.tmTotalBytes;
        outInfo->freeBytesAfter = freeAfter;
        outInfo->tierAWarps = GS.taWarps;
        outInfo->tierAHashCap = TA_HASH_CAP;
        outInfo->tierBCtas = GS.tbCtas;
        outInfo->tierBHashCap = TB_HASH_CAP;
        outInfo->initSec = initSec;
    }
    return true;
}

}  // namespace

// ============================================================================
// Public C++ API
// ============================================================================

bool fidesGpuInitMulti(
        uint32_t n, uint32_t m,
        const uint32_t* offset, const uint32_t* edge,
        const uint8_t* coreDeg,
        const FidesGpuNodeHot* hot, const FidesGpuDynNode* dyn,
        const uint32_t* ord16,
        int32_t sllLen,
        const int32_t* nextLaguna, const int32_t* prevLaguna, int32_t lagLen,
        const float* pcc, int32_t pccLen,
        int maxGpus,
        FidesGpuMultiInitInfo* outInfo) {
    if (g_numGpus > 0) {
        if (outInfo) outInfo->numGpus = g_numGpus;
        return true;
    }
    auto t0 = std::chrono::steady_clock::now();
    int devCount = 0;
    if (!checkCuda(cudaGetDeviceCount(&devCount), "cudaGetDeviceCount") || devCount <= 0) return false;
    int lim = std::min<int>(devCount, std::min<int>(maxGpus, FG_MAX_GPUS));
    std::vector<int> candidates;
    for (int i = 0; i < lim; ++i) {
        if (cudaSetDevice(i) != cudaSuccess) continue;
        size_t freeB = 0, totB = 0;
        if (cudaMemGetInfo(&freeB, &totB) != cudaSuccess) continue;
        if (freeB >= (27ULL << 30)) {
            candidates.push_back(i);
        }
    }
    if (candidates.empty()) return false;

    g_numGpus = static_cast<int>(candidates.size());
    for (int d = 0; d < g_numGpus; ++d) {
        GS_ARR[d] = GpuState{};
        GS_ARR[d].cudaDev = candidates[d];
    }

    struct InitCtx {
        uint32_t n, m;
        const uint32_t* offset; const uint32_t* edge;
        const uint8_t* coreDeg;
        const FidesGpuNodeHot* hot; const FidesGpuDynNode* dyn;
        const uint32_t* ord16; int32_t sllLen;
        const int32_t* nextLaguna; const int32_t* prevLaguna; int32_t lagLen;
        const float* pcc; int32_t pccLen;
        int totalSlots;
        bool ok[FG_MAX_GPUS]{};
        FidesGpuInitInfo info[FG_MAX_GPUS]{};
    } ctx{n, m, offset, edge, coreDeg, hot, dyn, ord16, sllLen, nextLaguna, prevLaguna, lagLen, pcc, pccLen, g_numGpus};

    if (g_numGpus > 1) g_gpuPool.start(FG_MAX_GPUS);
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<InitCtx*>(u);
        c->ok[d] = initDevice(d, c->n, c->m, c->offset, c->edge, c->coreDeg, c->hot, c->dyn,
                              c->ord16, c->sllLen, c->nextLaguna, c->prevLaguna, c->lagLen,
                              c->pcc, c->pccLen, c->totalSlots, &c->info[d]);
    }, &ctx);

    bool allOk = true;
    for (int d = 0; d < g_numGpus; ++d) {
        if (!ctx.ok[d]) allOk = false;
    }
    if (!allOk) {
        fidesGpuShutdown();
        return false;
    }

    double totSec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    if (outInfo) {
        outInfo->numGpus = g_numGpus;
        outInfo->initSec = totSec;
        for (int d = 0; d < g_numGpus; ++d) outInfo->cards[d] = ctx.info[d];
    }
    cudaSetDevice(GS_ARR[0].cudaDev);
    return true;
}

bool fidesGpuInit(
        uint32_t n, uint32_t m,
        const uint32_t* offset, const uint32_t* edge,
        const uint8_t* coreDeg,
        const FidesGpuNodeHot* hot, const FidesGpuDynNode* dyn,
        const uint32_t* ord16,
        int32_t sllLen,
        const int32_t* nextLaguna, const int32_t* prevLaguna, int32_t lagLen,
        const float* pcc, int32_t pccLen,
        FidesGpuInitInfo* outInfo) {
    FidesGpuMultiInitInfo mi{};
    bool ok = fidesGpuInitMulti(n, m, offset, edge, coreDeg, hot, dyn, ord16, sllLen,
                                nextLaguna, prevLaguna, lagLen, pcc, pccLen, 1, &mi);
    if (ok && outInfo) *outInfo = mi.cards[0];
    return ok;
}

void fidesGpuShutdown() {
    if (g_numGpus > 0) {
        g_gpuPool.run([](int d, void*) {
            shutdownDevice(d);
        }, nullptr);
    }
    g_gpuPool.shutdown();
    g_numGpus = 0;
}

bool fidesGpuIsReady() { return g_numGpus > 0 && GS_ARR[0].ready; }
int fidesGpuGetNumGpus() { return g_numGpus; }

void fidesGpuRunParallelDevs(FidesGpuDevTaskFn fn, void* user) {
    g_gpuPool.run(fn, user);
}

void fidesGpuUpdatePcc(const float* pcc, int32_t pccLen) {
    if (g_numGpus <= 0 || pccLen <= 0 || pccLen > MAX_PCC) return;
    struct Ctx { const float* pcc; int32_t len; } ctx{pcc, pccLen};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready) return;
        cudaSetDevice(GS.cudaDev);
        checkCuda(cudaMemcpyToSymbolAsync(c_pccLen, &c->len, sizeof(I32), 0, cudaMemcpyHostToDevice, GS.stream), "upd pccLen");
        checkCuda(cudaMemcpyToSymbolAsync(c_pcc, c->pcc, c->len * sizeof(float), 0, cudaMemcpyHostToDevice, GS.stream), "upd pcc");
    }, &ctx);
}

void fidesGpuSyncDynFull(const FidesGpuDynNode* dyn, uint32_t n) {
    if (g_numGpus <= 0 || n != GS_ARR[0].n) return;
    struct Ctx { const FidesGpuDynNode* dyn; uint32_t n; } ctx{dyn, n};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready || c->n != GS.n) return;
        cudaSetDevice(GS.cudaDev);
        checkCuda(cudaMemcpyAsync(GS.d_dyn, c->dyn, size_t(c->n) * sizeof(FidesGpuDynNode), cudaMemcpyHostToDevice, GS.stream), "syncDynFull");
    }, &ctx);
}

bool fidesGpuApplyOccDeltasMulti(const uint32_t* nodeIds, const int32_t* newOccs, uint32_t count) {
    if (g_numGpus <= 0 || count == 0u) return true;
    struct Ctx { const uint32_t* ids; const int32_t* occs; uint32_t count; bool ok[FG_MAX_GPUS]{}; } ctx{nodeIds, newOccs, count};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready) { c->ok[d] = false; return; }
        cudaSetDevice(GS.cudaDev);
        if (!ensureDeltaBuffersDev(d, c->count)) { c->ok[d] = false; return; }
        std::memcpy(GS.h_deltaIdsPinned, c->ids, size_t(c->count) * sizeof(U32));
        std::memcpy(GS.h_deltaOccPinned, c->occs, size_t(c->count) * sizeof(I32));
        checkCuda(cudaMemcpyAsync(GS.d_deltaIds, GS.h_deltaIdsPinned, size_t(c->count) * sizeof(U32), cudaMemcpyHostToDevice, GS.stream), "H2D deltaIdsM");
        checkCuda(cudaMemcpyAsync(GS.d_deltaOcc, GS.h_deltaOccPinned, size_t(c->count) * sizeof(I32), cudaMemcpyHostToDevice, GS.stream), "H2D deltaOccM");
        kApplyOccDeltas<<<(c->count + 255u) / 256u, 256, 0, GS.stream>>>(GS.d_dyn, GS.d_deltaIds, GS.d_deltaOcc, c->count);
        c->ok[d] = checkCuda(cudaGetLastError(), "kApplyOccDeltasMulti");
    }, &ctx);
    for (int d = 0; d < g_numGpus; ++d) if (!ctx.ok[d]) return false;
    return true;
}

void fidesGpuApplyOccDeltas(const uint32_t* nodeIds, const int32_t* newOccs, uint32_t count) {
    fidesGpuApplyOccDeltasMulti(nodeIds, newOccs, count);
}

void fidesGpuApplyHistDeltas(const uint32_t* nodeIds, const float* newHists, uint32_t count) {
    if (g_numGpus <= 0 || count == 0u) return;
    struct Ctx { const uint32_t* ids; const float* hists; uint32_t count; } ctx{nodeIds, newHists, count};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready) return;
        cudaSetDevice(GS.cudaDev);
        if (!ensureDeltaBuffersDev(d, c->count)) return;
        std::memcpy(GS.h_deltaIdsPinned, c->ids, size_t(c->count) * sizeof(U32));
        std::memcpy(GS.h_deltaHistPinned, c->hists, size_t(c->count) * sizeof(float));
        checkCuda(cudaMemcpyAsync(GS.d_deltaIds, GS.h_deltaIdsPinned, size_t(c->count) * sizeof(U32), cudaMemcpyHostToDevice, GS.stream), "H2D hDeltaIds");
        checkCuda(cudaMemcpyAsync(GS.d_deltaHist, GS.h_deltaHistPinned, size_t(c->count) * sizeof(float), cudaMemcpyHostToDevice, GS.stream), "H2D deltaHist");
        kApplyHistDeltas<<<(c->count + 255u) / 256u, 256, 0, GS.stream>>>(GS.d_dyn, GS.d_deltaIds, GS.d_deltaHist, c->count);
    }, &ctx);
}

bool fidesGpuUploadMegaScopesBatch(const FidesGpuMegaUpload* items, uint32_t count) {
    if (g_numGpus <= 0) return false;
    if (count == 0u || !items) return true;
    size_t totalCap = 0u;
    for (uint32_t i = 0u; i < count; ++i) {
        if (items[i].slot < 0 || items[i].slot >= MAX_MEGA_SCOPES || items[i].cap == 0u) return false;
        totalCap += size_t(items[i].cap);
    }
    struct Ctx {
        const FidesGpuMegaUpload* items;
        uint32_t count;
        size_t totalCap;
        bool ok[FG_MAX_GPUS]{};
    } ctx{items, count, totalCap};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready) { c->ok[d] = false; return; }
        cudaSetDevice(GS.cudaDev);
        if (GS.megaStageEventRecorded) {
            cudaEventSynchronize(GS.megaStageEvent);
            GS.megaStageEventRecorded = false;
        }
        bool needSyncForFree = false;
        for (uint32_t i = 0u; i < c->count; ++i) {
            int s = c->items[i].slot;
            if (c->items[i].cap > GS.megaCap[s] && (GS.d_megaKeysHost[s] || GS.d_megaValsHost[s])) {
                needSyncForFree = true;
                break;
            }
        }
        if (needSyncForFree) {
            cudaStreamSynchronize(GS.stream);
        }
        if (c->totalCap > GS.capMegaStage) {
            size_t nc = std::max<size_t>(c->totalCap, 4u << 20);
            if (GS.h_megaStageKeysPinned) cudaFreeHost(GS.h_megaStageKeysPinned);
            if (GS.h_megaStageValsPinned) cudaFreeHost(GS.h_megaStageValsPinned);
            GS.h_megaStageKeysPinned = nullptr;
            GS.h_megaStageValsPinned = nullptr;
            if (!checkCuda(cudaMallocHost(&GS.h_megaStageKeysPinned, nc * sizeof(U32)), "mallocHost megaStageK")) { c->ok[d] = false; return; }
            if (!checkCuda(cudaMallocHost(&GS.h_megaStageValsPinned, nc * sizeof(I32)), "mallocHost megaStageV")) { c->ok[d] = false; return; }
            GS.capMegaStage = nc;
        }
        size_t off = 0u;
        for (uint32_t i = 0u; i < c->count; ++i) {
            int s = c->items[i].slot;
            U32 cap = c->items[i].cap;
            if (cap > GS.megaCap[s]) {
                if (GS.d_megaKeysHost[s]) cudaFree(GS.d_megaKeysHost[s]);
                if (GS.d_megaValsHost[s]) cudaFree(GS.d_megaValsHost[s]);
                size_t allocCap = std::max<size_t>(cap, 65536u);
                if (!checkCuda(cudaMalloc(&GS.d_megaKeysHost[s], allocCap * sizeof(U32)), "malloc megaKeys")) { c->ok[d] = false; return; }
                if (!checkCuda(cudaMalloc(&GS.d_megaValsHost[s], allocCap * sizeof(I32)), "malloc megaVals")) { c->ok[d] = false; return; }
                GS.megaCap[s] = allocCap;
                checkCuda(cudaMemcpyToSymbolAsync(c_megaKeys, &GS.d_megaKeysHost[s], sizeof(U32*),
                                                  size_t(s) * sizeof(U32*), cudaMemcpyHostToDevice, GS.stream), "sym megaKey");
                checkCuda(cudaMemcpyToSymbolAsync(c_megaVals, &GS.d_megaValsHost[s], sizeof(I32*),
                                                  size_t(s) * sizeof(I32*), cudaMemcpyHostToDevice, GS.stream), "sym megaVal");
            }
            std::memcpy(GS.h_megaStageKeysPinned + off, c->items[i].keys, size_t(cap) * sizeof(U32));
            std::memcpy(GS.h_megaStageValsPinned + off, c->items[i].counts, size_t(cap) * sizeof(I32));
            checkCuda(cudaMemcpyAsync(GS.d_megaKeysHost[s], GS.h_megaStageKeysPinned + off, size_t(cap) * sizeof(U32), cudaMemcpyHostToDevice, GS.stream), "H2D megaKeys");
            checkCuda(cudaMemcpyAsync(GS.d_megaValsHost[s], GS.h_megaStageValsPinned + off, size_t(cap) * sizeof(I32), cudaMemcpyHostToDevice, GS.stream), "H2D megaVals");
            off += size_t(cap);
        }
        checkCuda(cudaEventRecord(GS.megaStageEvent, GS.stream), "record megaStageEvent");
        GS.megaStageEventRecorded = true;
        c->ok[d] = true;
    }, &ctx);
    for (int d = 0; d < g_numGpus; ++d) if (!ctx.ok[d]) return false;
    return true;
}

int fidesGpuUploadMegaScope(int slot, const uint32_t* keys, const int32_t* counts, uint32_t cap) {
    FidesGpuMegaUpload one{slot, keys, counts, cap};
    return fidesGpuUploadMegaScopesBatch(&one, 1u) ? slot : -1;
}

bool fidesGpuApplyMegaPatches(const FidesGpuMegaPatch* patches, uint32_t count) {
    if (g_numGpus <= 0 || count == 0u) return true;
    struct Ctx { const FidesGpuMegaPatch* patches; uint32_t count; bool ok[FG_MAX_GPUS]{}; } ctx{patches, count};
    g_gpuPool.run([](int d, void* u) {
        auto* c = static_cast<Ctx*>(u);
        GpuState& GS = GS_ARR[d];
        if (!GS.ready) { c->ok[d] = false; return; }
        cudaSetDevice(GS.cudaDev);
        if (c->count > GS.capMegaPatches) {
            size_t nc = std::max<size_t>(c->count, 262144u);
            if (GS.d_megaPatches) cudaFree(GS.d_megaPatches);
            if (GS.h_megaPatchesPinned) cudaFreeHost(GS.h_megaPatchesPinned);
            if (!checkCuda(cudaMalloc(&GS.d_megaPatches, nc * sizeof(FidesGpuMegaPatch)), "malloc d_megaPatches")) { c->ok[d] = false; return; }
            if (!checkCuda(cudaMallocHost(&GS.h_megaPatchesPinned, nc * sizeof(FidesGpuMegaPatch)), "mallocHost h_megaPatches")) { c->ok[d] = false; return; }
            GS.capMegaPatches = nc;
        }
        std::memcpy(GS.h_megaPatchesPinned, c->patches, size_t(c->count) * sizeof(FidesGpuMegaPatch));
        checkCuda(cudaMemcpyAsync(GS.d_megaPatches, GS.h_megaPatchesPinned, size_t(c->count) * sizeof(FidesGpuMegaPatch),
                                  cudaMemcpyHostToDevice, GS.stream), "H2D megaPatches");
        kApplyMegaPatches<<<(c->count + 255u) / 256u, 256, 0, GS.stream>>>(GS.d_megaPatches, c->count);
        c->ok[d] = checkCuda(cudaGetLastError(), "kApplyMegaPatches launch");
    }, &ctx);
    for (int d = 0; d < g_numGpus; ++d) if (!ctx.ok[d]) return false;
    return true;
}

bool fidesGpuAcquireWaveBufsDev(
        int dev,
        uint32_t maxQueries, uint32_t maxSameLen, uint32_t maxOutPaths,
        uint32_t maxOldNodes, uint32_t maxDeltas,
        FidesGpuPinnedWaveBufs* outBufs) {
    if (dev < 0 || dev >= g_numGpus || !GS_ARR[dev].ready || !outBufs) return false;
    GpuState& GS = GS_ARR[dev];
    cudaSetDevice(GS.cudaDev);
    if (!ensureWaveBuffersDev(dev, maxQueries, maxSameLen, maxOutPaths, maxOldNodes)) return false;
    if (maxDeltas > 0u && !ensureDeltaBuffersDev(dev, maxDeltas)) return false;
    outBufs->queries  = GS.h_queriesPinned;
    outBufs->sameKeys = GS.h_sameKeysPinned;
    outBufs->sameVals = GS.h_sameValsPinned;
    outBufs->oldNodes = GS.h_oldNodesPinned;
    outBufs->results  = GS.h_resultsPinned;
    outBufs->outPaths = GS.h_outPathsPinned;
    outBufs->deltaIds = GS.h_deltaIdsPinned;
    outBufs->deltaOcc = GS.h_deltaOccPinned;
    return true;
}

bool fidesGpuAcquireWaveBufs(
        uint32_t maxQueries, uint32_t maxSameLen, uint32_t maxOutPaths,
        uint32_t maxOldNodes, uint32_t maxDeltas,
        FidesGpuPinnedWaveBufs* outBufs) {
    return fidesGpuAcquireWaveBufsDev(0, maxQueries, maxSameLen, maxOutPaths, maxOldNodes, maxDeltas, outBufs);
}

bool fidesGpuApplyOccDeltasDirect(uint32_t count) {
    if (g_numGpus <= 0 || !GS_ARR[0].ready || count == 0u) return true;
    GpuState& GS = GS_ARR[0];
    if (count > GS.capDeltas) return false;
    return fidesGpuApplyOccDeltasMulti(GS.h_deltaIdsPinned, GS.h_deltaOccPinned, count);
}

bool fidesGpuRunTierADev(
        int dev,
        const FidesGpuWaveParams& wp,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords) {
    if (dev < 0 || dev >= g_numGpus || !GS_ARR[dev].ready || numQueries == 0u) {
        if (outUsedPathWords) *outUsedPathWords = 0u;
        return (dev >= 0 && dev < g_numGpus && GS_ARR[dev].ready);
    }
    GpuState& GS = GS_ARR[dev];
    cudaSetDevice(GS.cudaDev);
    if (!ensureWaveBuffersDev(dev, numQueries, sameLen, outPathsCap, oldNodesLen)) return false;

    checkCuda(cudaMemcpyAsync(GS.d_queries, GS.h_queriesPinned, size_t(numQueries) * sizeof(FidesGpuQuery),
                              cudaMemcpyHostToDevice, GS.stream), "TAD H2D queries");
    if (sameLen > 0u) {
        checkCuda(cudaMemcpyAsync(GS.d_sameKeys, GS.h_sameKeysPinned, size_t(sameLen) * sizeof(U32),
                                  cudaMemcpyHostToDevice, GS.stream), "TAD H2D sameKeys");
        checkCuda(cudaMemcpyAsync(GS.d_sameVals, GS.h_sameValsPinned, size_t(sameLen) * sizeof(I32),
                                  cudaMemcpyHostToDevice, GS.stream), "TAD H2D sameVals");
    }
    if (oldNodesLen > 0u) {
        checkCuda(cudaMemcpyAsync(GS.d_oldNodes, GS.h_oldNodesPinned, size_t(oldNodesLen) * sizeof(U32),
                                  cudaMemcpyHostToDevice, GS.stream), "TAD H2D oldNodes");
    }
    checkCuda(cudaMemsetAsync(GS.d_nextQuery, 0, sizeof(U32), GS.stream), "TAD reset nextQuery");
    checkCuda(cudaMemsetAsync(GS.d_outPathCursor, 0, sizeof(U32), GS.stream), "TAD reset cursor");

    GpuGraphDevice gd{GS.n, GS.d_offset, GS.d_edge, GS.d_coreDeg, GS.d_hot, GS.d_dyn};
    U32 activeWarps = std::min<U32>(GS.taWarps, numQueries);
    U32 blocks = (activeWarps + TA_WARPS_PER_BLOCK - 1u) / TA_WARPS_PER_BLOCK;
    kTierAWarpSearch<<<blocks, TA_WARPS_PER_BLOCK * 32, 0, GS.stream>>>(
            gd, GS.d_taPool, GS.taSlotBytes, GS.taWarps,
            wp, GS.d_queries, numQueries, GS.d_nextQuery,
            GS.d_sameKeys, GS.d_sameVals, GS.d_oldNodes, GS.d_results, GS.d_outPaths, outPathsCap, GS.d_outPathCursor);
    if (!checkCuda(cudaGetLastError(), "kTierAWarpSearchDev launch")) return false;

    checkCuda(cudaMemcpyAsync(GS.h_resultsPinned, GS.d_results, size_t(numQueries) * sizeof(FidesGpuResult),
                              cudaMemcpyDeviceToHost, GS.stream), "TAD D2H results");
    checkCuda(cudaMemcpyAsync(GS.h_outPathCursorPinned, GS.d_outPathCursor, sizeof(U32),
                              cudaMemcpyDeviceToHost, GS.stream), "TAD D2H cursor");
    if (!checkCuda(cudaStreamSynchronize(GS.stream), "TAD sync 1")) return false;

    U32 usedWords = std::min<U32>(outPathsCap, *GS.h_outPathCursorPinned);
    if (outUsedPathWords) *outUsedPathWords = usedWords;
    if (usedWords > 0u) {
        checkCuda(cudaMemcpyAsync(GS.h_outPathsPinned, GS.d_outPaths, size_t(usedWords) * sizeof(U32),
                                  cudaMemcpyDeviceToHost, GS.stream), "TAD D2H paths");
        if (!checkCuda(cudaStreamSynchronize(GS.stream), "TAD sync 2")) return false;
    }
    return true;
}

bool fidesGpuRunTierADirect(
        const FidesGpuWaveParams& wp,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords) {
    return fidesGpuRunTierADev(0, wp, numQueries, sameLen, outPathsCap, oldNodesLen, outUsedPathWords);
}

bool fidesGpuRunTierB1WideDev(
        int dev,
        const FidesGpuWaveParams& wp, int mode,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords, uint32_t* outCtasUsed) {
    if (dev < 0 || dev >= g_numGpus || !GS_ARR[dev].ready || numQueries == 0u) {
        if (outUsedPathWords) *outUsedPathWords = 0u;
        if (outCtasUsed) *outCtasUsed = 0u;
        return (dev >= 0 && dev < g_numGpus && GS_ARR[dev].ready);
    }
    if (mode < 0 || mode > 2) return false;
    GpuState& GS = GS_ARR[dev];
    cudaSetDevice(GS.cudaDev);
    TierB1Cfg cfg = makeTierB1Cfg(mode);
    U32 maxFit = static_cast<U32>(GS.tbTotalBytes / cfg.slotBytes);
    if (maxFit == 0u) return false;
    U32 modeCtas = (mode == 0) ? GS.tbCtas : std::min<U32>(maxFit, (mode == 1) ? 40u : 24u);
    if (outCtasUsed) *outCtasUsed = modeCtas;

    if (!ensureWaveBuffersDev(dev, numQueries, sameLen, outPathsCap, oldNodesLen)) return false;

    if (GS.tbActiveMode != mode) {
        if (!checkCuda(cudaMemsetAsync(GS.d_tbPool, 0xFF, GS.tbTotalBytes, GS.stream), "TB1W mode clear")) return false;
        GS.tbActiveMode = mode;
    }

    checkCuda(cudaMemcpyAsync(GS.d_queries, GS.h_queriesPinned, size_t(numQueries) * sizeof(FidesGpuQuery),
                              cudaMemcpyHostToDevice, GS.stream), "TB1D H2D queries");
    if (sameLen > 0u) {
        checkCuda(cudaMemcpyAsync(GS.d_sameKeys, GS.h_sameKeysPinned, size_t(sameLen) * sizeof(U32),
                                  cudaMemcpyHostToDevice, GS.stream), "TB1D H2D sameKeys");
        checkCuda(cudaMemcpyAsync(GS.d_sameVals, GS.h_sameValsPinned, size_t(sameLen) * sizeof(I32),
                                  cudaMemcpyHostToDevice, GS.stream), "TB1D H2D sameVals");
    }
    if (oldNodesLen > 0u) {
        checkCuda(cudaMemcpyAsync(GS.d_oldNodes, GS.h_oldNodesPinned, size_t(oldNodesLen) * sizeof(U32),
                                  cudaMemcpyHostToDevice, GS.stream), "TB1D H2D oldNodes");
    }
    checkCuda(cudaMemsetAsync(GS.d_nextQuery, 0, sizeof(U32), GS.stream), "TB1D reset nextQuery");
    checkCuda(cudaMemsetAsync(GS.d_outPathCursor, 0, sizeof(U32), GS.stream), "TB1D reset cursor");

    GpuGraphDevice gd{GS.n, GS.d_offset, GS.d_edge, GS.d_coreDeg, GS.d_hot, GS.d_dyn};
    U32 blocks = std::min<U32>(modeCtas, numQueries);
    kTierBCtaSearch<<<blocks, TB_THREADS, 0, GS.stream>>>(
            gd, GS.d_tbPool, cfg, modeCtas,
            wp, GS.d_queries, numQueries, GS.d_nextQuery,
            GS.d_sameKeys, GS.d_sameVals, GS.d_oldNodes, GS.d_results, GS.d_outPaths, outPathsCap, GS.d_outPathCursor);
    if (!checkCuda(cudaGetLastError(), "kTierBCtaSearchDev launch")) return false;

    checkCuda(cudaMemcpyAsync(GS.h_resultsPinned, GS.d_results, size_t(numQueries) * sizeof(FidesGpuResult),
                              cudaMemcpyDeviceToHost, GS.stream), "TB1D D2H results");
    checkCuda(cudaMemcpyAsync(GS.h_outPathCursorPinned, GS.d_outPathCursor, sizeof(U32),
                              cudaMemcpyDeviceToHost, GS.stream), "TB1D D2H cursor");
    if (!checkCuda(cudaStreamSynchronize(GS.stream), "TB1D sync 1")) return false;

    U32 usedWords = std::min<U32>(outPathsCap, *GS.h_outPathCursorPinned);
    if (outUsedPathWords) *outUsedPathWords = usedWords;
    if (usedWords > 0u) {
        checkCuda(cudaMemcpyAsync(GS.h_outPathsPinned, GS.d_outPaths, size_t(usedWords) * sizeof(U32),
                                  cudaMemcpyDeviceToHost, GS.stream), "TB1D D2H paths");
        if (!checkCuda(cudaStreamSynchronize(GS.stream), "TB1D sync 2")) return false;
    }
    return true;
}

bool fidesGpuRunTierB1Dev(
        int dev,
        const FidesGpuWaveParams& wp,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords) {
    return fidesGpuRunTierB1WideDev(dev, wp, 0, numQueries, sameLen, outPathsCap, oldNodesLen, outUsedPathWords, nullptr);
}

bool fidesGpuRunTierB1WideDirect(
        const FidesGpuWaveParams& wp, int mode,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords, uint32_t* outCtasUsed) {
    return fidesGpuRunTierB1WideDev(0, wp, mode, numQueries, sameLen, outPathsCap, oldNodesLen, outUsedPathWords, outCtasUsed);
}

bool fidesGpuRunTierB1Direct(
        const FidesGpuWaveParams& wp,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
        uint32_t* outUsedPathWords) {
    return fidesGpuRunTierB1WideDev(0, wp, 0, numQueries, sameLen, outPathsCap, oldNodesLen, outUsedPathWords, nullptr);
}

bool fidesGpuRunTierA(
        const FidesGpuWaveParams& wp,
        const FidesGpuQuery* queries, uint32_t numQueries,
        const uint32_t* sameKeys, const int32_t* sameVals, uint32_t sameLen,
        FidesGpuResult* outResults, uint32_t* outPaths, uint32_t outPathsCap,
        const uint32_t* oldNodes, uint32_t oldNodesLen) {
    FidesGpuPinnedWaveBufs b{};
    if (!fidesGpuAcquireWaveBufsDev(0, numQueries, sameLen, outPathsCap, oldNodesLen, 0u, &b)) return false;
    std::memcpy(b.queries, queries, size_t(numQueries) * sizeof(FidesGpuQuery));
    if (sameLen > 0u) {
        std::memcpy(b.sameKeys, sameKeys, size_t(sameLen) * sizeof(U32));
        std::memcpy(b.sameVals, sameVals, size_t(sameLen) * sizeof(I32));
    }
    if (oldNodesLen > 0u) std::memcpy(b.oldNodes, oldNodes, size_t(oldNodesLen) * sizeof(U32));
    U32 uw = 0u;
    if (!fidesGpuRunTierADev(0, wp, numQueries, sameLen, outPathsCap, oldNodesLen, &uw)) return false;
    if (uw > 0u) std::memcpy(outPaths, b.outPaths, size_t(uw) * sizeof(U32));
    std::memcpy(outResults, b.results, size_t(numQueries) * sizeof(FidesGpuResult));
    return true;
}

bool fidesGpuRunTierB1(
        const FidesGpuWaveParams& wp,
        const FidesGpuQuery* queries, uint32_t numQueries,
        const uint32_t* sameKeys, const int32_t* sameVals, uint32_t sameLen,
        FidesGpuResult* outResults, uint32_t* outPaths, uint32_t outPathsCap,
        const uint32_t* oldNodes, uint32_t oldNodesLen) {
    FidesGpuPinnedWaveBufs b{};
    if (!fidesGpuAcquireWaveBufsDev(0, numQueries, sameLen, outPathsCap, oldNodesLen, 0u, &b)) return false;
    std::memcpy(b.queries, queries, size_t(numQueries) * sizeof(FidesGpuQuery));
    if (sameLen > 0u) {
        std::memcpy(b.sameKeys, sameKeys, size_t(sameLen) * sizeof(U32));
        std::memcpy(b.sameVals, sameVals, size_t(sameLen) * sizeof(I32));
    }
    if (oldNodesLen > 0u) std::memcpy(b.oldNodes, oldNodes, size_t(oldNodesLen) * sizeof(U32));
    U32 uw = 0u;
    if (!fidesGpuRunTierB1Dev(0, wp, numQueries, sameLen, outPathsCap, oldNodesLen, &uw)) return false;
    if (uw > 0u) std::memcpy(outPaths, b.outPaths, size_t(uw) * sizeof(U32));
    std::memcpy(outResults, b.results, size_t(numQueries) * sizeof(FidesGpuResult));
    return true;
}

bool fidesGpuRunTierB2(
        const FidesGpuWaveParams&,
        const FidesGpuQuery*, uint32_t,
        const uint32_t*, const int32_t*, uint32_t,
        FidesGpuResult*, uint32_t*, uint32_t,
        const uint32_t*, uint32_t) {
    return false;
}

bool fidesGpuRunTierB2Direct(
        const FidesGpuWaveParams& wp,
        uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen) {
    if (g_numGpus <= 0 || !GS_ARR[0].d_tmPool || numQueries == 0u) return false;
    GpuState& GS = GS_ARR[0];
    cudaSetDevice(GS.cudaDev);
    if (!ensureWaveBuffersDev(0, numQueries, sameLen, outPathsCap, oldNodesLen)) return false;
    GpuGraphDevice gd{GS.n, GS.d_offset, GS.d_edge, GS.d_coreDeg, GS.d_hot, GS.d_dyn};
    dim3 gridDim(GS.tmGridBlocks);
    dim3 blockDim(256);
    FidesGpuWaveParams wpArg = wp;
    U32 nqArg = numQueries;
    void* kArgs[] = {
        &gd, &GS.tm, &wpArg, &GS.d_queries, &nqArg,
        &GS.d_sameKeys, &GS.d_sameVals, &GS.d_oldNodes, &GS.d_results, &GS.d_outPaths
    };
    return checkCuda(cudaLaunchCooperativeKernel(
        reinterpret_cast<void*>(kTierBMegaCoopSearch), gridDim, blockDim, kArgs, 0, GS.stream),
        "kTierBMegaCoopSearchDirect launch");
}
