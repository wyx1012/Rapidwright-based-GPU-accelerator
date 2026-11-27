#pragma once

#include <cstddef>
#include <cstdint>

struct alignas(16) FidesGpuNodeHot {
    uint32_t w0;       // [9:0]=endTileX, [20:10]=endTileYRaw, [31:21]=tileY
    uint32_t w1;       // [21:0]=tileId, [31:22]=length
    uint32_t w2;       // [10:0]=preFlags(+bit10 uturnDelay), [16:11]=typeFlags, [21:17]=nodeType, [24:22]=endSlrRaw+1, [27:25]=endSlrTileY+1
    float    baseCost;
};
static_assert(sizeof(FidesGpuNodeHot) == 16, "FidesGpuNodeHot must be 16 bytes");

struct alignas(8) FidesGpuDynNode {
    int32_t occ;
    float   hist;
};
static_assert(sizeof(FidesGpuDynNode) == 8, "FidesGpuDynNode must be 8 bytes");

enum FidesGpuPreFlags : uint32_t {
    FG_PF_SLL          = 1u << 0,
    FG_PF_LAGUNA_TILE  = 1u << 1,
    FG_PF_CLB_TILE     = 1u << 2,
    FG_PF_INT_TILE     = 1u << 3,
    FG_PF_PINBOUNCE    = 1u << 4,
    FG_PF_PINFEED      = 1u << 5,
    FG_PF_CLE_OUTPUT   = 1u << 6,
    FG_PF_LAGUNA_WIRE  = 1u << 7,
    FG_PF_LAGUNA_NORTH = 1u << 8,
    FG_PF_LAGUNA_SOUTH = 1u << 9,
    FG_PF_UTURN_DELAY  = 1u << 10
};

enum FidesGpuQueryFlags : uint32_t {
    FG_QF_USE_BOX   = 1u << 0,
    FG_QF_UTURN     = 1u << 1,
    FG_QF_CROSS     = 1u << 2,
    FG_QF_NORTH     = 1u << 3,
    FG_QF_SOUTH     = 1u << 4,
    FG_QF_SINK_CTRL = 1u << 5
};

enum FidesGpuStatus : int32_t {
    FG_STATUS_FALLBACK        = -1,
    FG_STATUS_CROSSED_BUDGET  = -2,
    FG_STATUS_NO_PATH         = -3,
    FG_STATUS_HASH_OVERFLOW   = -4
};

struct alignas(16) FidesGpuQuery {
    uint32_t src;
    uint32_t sink;
    int32_t  xmin, ymin, xmax, ymax;
    int32_t  origXmin, origXmax;
    int32_t  sinkBeginX, sinkBeginY, sinkTile, sinkType;
    int32_t  sinkEndX, sinkEndY;
    int32_t  sinkSlr;
    uint32_t flags;
    float    xCenter, yCenter;
    float    netSizeF, doubleHpwlF, invBiasDenom;
    int32_t  budget;          // <= 0 means unlimited
    uint32_t sameNetMode;     // 0=empty, 1=smallList+bloom, 2=waveHash, 3=cachedMegaHash
    uint64_t sameBloom64;
    uint32_t sameOffset;      // offset into sameKeys/sameVals (mode 1/2) or megaSlot (mode 3)
    uint32_t sameParam;       // len (mode 1) or mask (mode 2/3)
    uint64_t oldBloom64;      // 64-bit bloom over interior oldPath[1..len-2] (sorted)
    uint32_t oldOffset;       // offset into wave oldNodes array
    uint32_t oldLen;          // number of interior nodes in oldPath (0 if pre-ripped or unrouted)
    uint32_t outPathOffset;   // offset into outPaths buffer
    uint32_t outPathCap;      // max path length allowed in outPaths
};

struct FidesGpuWaveParams {
    float    shareWeight;
    float    rnodeCostWeight;
    float    rnodeWLWeight;
    float    estWlWeight;
    uint32_t bucketShift;     // IEEE-754 float bits >> bucketShift (default 18 => 3.125% width)
    uint32_t popBatch;        // max nodes popped per step (Tier-A: 8..32, Tier-B1: 64..256)
    uint32_t pccLen;
};

struct FidesGpuResult {
    int32_t  status;          // >0: path length written (sink..src), <0: FidesGpuStatus
    uint32_t pathOffset;      // actual offset in outPaths where path[0..status-1] was written
    int64_t  pops;
};

struct FidesGpuInitInfo {
    bool     ready;
    int      deviceId;
    char     deviceName[256];
    char     deviceUuid[64];
    size_t   totalBytes;
    size_t   residentBytes;
    size_t   arenaBytes;
    size_t   freeBytesAfter;
    uint32_t tierAWarps;
    uint32_t tierAHashCap;
    uint32_t tierBCtas;
    uint32_t tierBHashCap;
    double   initSec;
};

static constexpr int FG_MAX_GPUS = 4;

struct FidesGpuMultiInitInfo {
    int numGpus;
    FidesGpuInitInfo cards[FG_MAX_GPUS];
    double initSec;
};

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
    FidesGpuMultiInitInfo* outInfo);

int fidesGpuGetNumGpus();

bool fidesGpuInit(
    uint32_t n, uint32_t m,
    const uint32_t* offset, const uint32_t* edge,
    const uint8_t* coreDeg,
    const FidesGpuNodeHot* hot, const FidesGpuDynNode* dyn,
    const uint32_t* ord16,
    int32_t sllLen,
    const int32_t* nextLaguna, const int32_t* prevLaguna, int32_t lagLen,
    const float* pcc, int32_t pccLen,
    FidesGpuInitInfo* outInfo);

void fidesGpuShutdown();
bool fidesGpuIsReady();

void fidesGpuUpdatePcc(const float* pcc, int32_t pccLen);
void fidesGpuSyncDynFull(const FidesGpuDynNode* dyn, uint32_t n);
void fidesGpuApplyOccDeltas(const uint32_t* nodeIds, const int32_t* newOccs, uint32_t count);
void fidesGpuApplyHistDeltas(const uint32_t* nodeIds, const float* newHists, uint32_t count);

// Upload mega-net flat hash table(s) (mode 3) into cached slot [0..1023]
struct FidesGpuMegaUpload {
    int slot;
    const uint32_t* keys;
    const int32_t* counts;
    uint32_t cap;
};
bool fidesGpuUploadMegaScopesBatch(const FidesGpuMegaUpload* items, uint32_t count);
int fidesGpuUploadMegaScope(int slot, const uint32_t* keys, const int32_t* counts, uint32_t cap);

struct alignas(16) FidesGpuMegaPatch {
    uint32_t slot;
    uint32_t idx;
    uint32_t key;
    int32_t  val;
};
static_assert(sizeof(FidesGpuMegaPatch) == 16, "FidesGpuMegaPatch must be 16 bytes");

bool fidesGpuApplyMegaPatches(const FidesGpuMegaPatch* patches, uint32_t count);

// Tier-A: High-parallel Warp-per-Connection (32 threads/conn, persistent dynamic work-stealing)
bool fidesGpuRunTierA(
    const FidesGpuWaveParams& wp,
    const FidesGpuQuery* queries, uint32_t numQueries,
    const uint32_t* sameKeys, const int32_t* sameVals, uint32_t sameLen,
    FidesGpuResult* outResults, uint32_t* outPaths, uint32_t outPathsCap,
    const uint32_t* oldNodes = nullptr, uint32_t oldNodesLen = 0u);

// Tier-B1: Heavy-tail CTA-per-Connection (256 threads/conn, large hash table, persistent work-stealing)
bool fidesGpuRunTierB1(
    const FidesGpuWaveParams& wp,
    const FidesGpuQuery* queries, uint32_t numQueries,
    const uint32_t* sameKeys, const int32_t* sameVals, uint32_t sameLen,
    FidesGpuResult* outResults, uint32_t* outPaths, uint32_t outPathsCap,
    const uint32_t* oldNodes = nullptr, uint32_t oldNodesLen = 0u);

// Tier-B2: Whole-GPU Multi-CTA Mega-Tail (all 80 SMs, O(1) flat N-array, zero hash collisions)
bool fidesGpuRunTierB2(
    const FidesGpuWaveParams& wp,
    const FidesGpuQuery* queries, uint32_t numQueries,
    const uint32_t* sameKeys, const int32_t* sameVals, uint32_t sameLen,
    FidesGpuResult* outResults, uint32_t* outPaths, uint32_t outPathsCap,
    const uint32_t* oldNodes = nullptr, uint32_t oldNodesLen = 0u);

// v35 Zero-Copy CUDA Pinned Wave Buffer Interface (eliminates intermediate std::memcpy!)
struct FidesGpuPinnedWaveBufs {
    FidesGpuQuery*  queries;
    uint32_t*       sameKeys;
    int32_t*        sameVals;
    uint32_t*       oldNodes;
    FidesGpuResult* results;
    uint32_t*       outPaths;
    uint32_t*       deltaIds;
    int32_t*        deltaOcc;
};

bool fidesGpuAcquireWaveBufs(
    uint32_t maxQueries, uint32_t maxSameLen, uint32_t maxOutPaths,
    uint32_t maxOldNodes, uint32_t maxDeltas,
    FidesGpuPinnedWaveBufs* outBufs);

bool fidesGpuAcquireWaveBufsDev(
    int dev,
    uint32_t maxQueries, uint32_t maxSameLen, uint32_t maxOutPaths,
    uint32_t maxOldNodes, uint32_t maxDeltas,
    FidesGpuPinnedWaveBufs* outBufs);

bool fidesGpuApplyOccDeltasMulti(const uint32_t* nodeIds, const int32_t* newOccs, uint32_t count);

bool fidesGpuRunTierADev(
    int dev,
    const FidesGpuWaveParams& wp,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords);

bool fidesGpuRunTierB1Dev(
    int dev,
    const FidesGpuWaveParams& wp,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords);

bool fidesGpuRunTierB1WideDev(
    int dev,
    const FidesGpuWaveParams& wp, int mode,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords, uint32_t* outCtasUsed = nullptr);

typedef void (*FidesGpuDevTaskFn)(int dev, void* user);
void fidesGpuRunParallelDevs(FidesGpuDevTaskFn fn, void* user);

bool fidesGpuApplyOccDeltasDirect(uint32_t count);

bool fidesGpuRunTierADirect(
    const FidesGpuWaveParams& wp,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords);

bool fidesGpuRunTierB1Direct(
    const FidesGpuWaveParams& wp,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords);

// mode: 0 = 8M/80 CTAs, 1 = 16M-Wide/~40 CTAs, 2 = 32M-Ultra/~21 CTAs (all inside d_tbPool)
bool fidesGpuRunTierB1WideDirect(
    const FidesGpuWaveParams& wp, int mode,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen,
    uint32_t* outUsedPathWords, uint32_t* outCtasUsed = nullptr);

bool fidesGpuRunTierB2Direct(
    const FidesGpuWaveParams& wp,
    uint32_t numQueries, uint32_t sameLen, uint32_t outPathsCap, uint32_t oldNodesLen);
