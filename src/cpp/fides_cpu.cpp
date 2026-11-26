#include <jni.h>
#include <deque>
#include "fides_gpu.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <limits>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>
#ifdef __linux__
#include <sys/syscall.h>
#include <unistd.h>
#endif

#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#define FIDES_PAUSE() _mm_pause()
#else
#define FIDES_PAUSE() ((void)0)
#endif

namespace {

using U16 = uint16_t;
using I16 = int16_t;
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
enum Flags { Local = 1, East = 2, West = 4, Leading = 8, Exclusive = 16, Nonlocal = 32 };

enum PreFlags : U16 {
    PF_SLL          = 1u << 0,
    PF_LAGUNA_TILE  = 1u << 1,
    PF_CLB_TILE     = 1u << 2,
    PF_INT_TILE     = 1u << 3,
    PF_PINBOUNCE    = 1u << 4,
    PF_PINFEED      = 1u << 5,
    PF_CLE_OUTPUT   = 1u << 6,
    PF_LAGUNA_WIRE  = 1u << 7,
    PF_LAGUNA_NORTH = 1u << 8,
    PF_LAGUNA_SOUTH = 1u << 9
};

enum SearchStatus {
    STATUS_FALLBACK = -1,
    STATUS_CROSSED_BUDGET = -2,
    STATUS_NO_PATH_CORRIDOR = -3
};

inline U32 mixId(U32 x) {
    x ^= x >> 16;
    x *= 0x7feb352du;
    x ^= x >> 15;
    x *= 0x846ca68bu;
    x ^= x >> 16;
    return x;
}

inline size_t nextPow2(size_t x) {
    size_t p = 1;
    while (p < x) p <<= 1;
    return p;
}

struct ScopeSlot {
    U32 key;    // EMPTY_KEY = 0xFFFFFFFFu
    I32 count;  // >= 0
};

struct NetScopeData {
    static constexpr U32 EMPTY_KEY = 0xFFFFFFFFu;
    bool ready = false;
    bool isFlat = false;
    U32 mask = 0;
    U32 bloomMask = 0;
    U32 liveCount = 0;
    U32 usedSlots = 0;
    U32 layoutVersion = 1;
    bool megaSyncTracked = false;
    I32 megaSlot = -1;
    std::vector<U32> megaDirtyIdx;
    std::vector<U32> smallIds;
    std::vector<I32> smallCounts;
    std::vector<ScopeSlot> slots;
    std::vector<U64> bloom;

    void resetBloomForCap(size_t cap) {
        size_t bw = nextPow2(std::min<size_t>(8192u, std::max<size_t>(256u, cap >> 4)));
        bloom.assign(bw, 0ULL);
        bloomMask = static_cast<U32>(bw - 1u);
    }

    inline void addBloom(U32 id) {
        if (bloom.empty()) return;
        U32 h = mixId(id);
        U64 pat = (1ULL << ((h >> 16) & 63u)) | (1ULL << ((h >> 22) & 63u));
        __atomic_fetch_or(&bloom[h & bloomMask], pat, __ATOMIC_RELAXED);
    }

    inline bool mayContainBloom(U32 id) const {
        if (bloom.empty()) return true;
        U32 h = mixId(id);
        U64 pat = (1ULL << ((h >> 16) & 63u)) | (1ULL << ((h >> 22) & 63u));
        U64 w = __atomic_load_n(&bloom[h & bloomMask], __ATOMIC_RELAXED);
        return (w & pat) == pat;
    }

    void initFlatFromPairs(const std::vector<U32>& ids, const std::vector<I32>& counts, size_t countPairs) {
        size_t minCap = std::max<size_t>(1024, countPairs * 4);
        size_t cap = nextPow2(minCap);
        while ((countPairs + 512) * 10 >= cap * 7) cap <<= 1;
        slots.assign(cap, ScopeSlot{EMPTY_KEY, 0});
        mask = static_cast<U32>(cap - 1);
        resetBloomForCap(cap);
        liveCount = 0;
        usedSlots = 0;
        for (size_t i = 0; i < countPairs; ++i) {
            if (counts[i] <= 0) continue;
            U32 id = ids[i];
            U32 idx = mixId(id) & mask;
            while (slots[idx].key != EMPTY_KEY) idx = (idx + 1u) & mask;
            slots[idx].key = id;
            slots[idx].count = counts[i];
            addBloom(id);
            ++liveCount;
            ++usedSlots;
        }
        isFlat = true;
        ++layoutVersion;
        megaSyncTracked = false;
        megaDirtyIdx.clear();
    }

    void rehashFlat() {
        size_t minCap = std::max<size_t>(1024, static_cast<size_t>(liveCount) * 4);
        size_t cap = nextPow2(minCap);
        while ((static_cast<size_t>(liveCount) + 512) * 10 >= cap * 7) cap <<= 1;
        std::vector<ScopeSlot> next(cap, ScopeSlot{EMPTY_KEY, 0});
        U32 nextMask = static_cast<U32>(cap - 1);
        resetBloomForCap(cap);
        U32 newLive = 0;
        for (const ScopeSlot& s : slots) {
            if (s.key != EMPTY_KEY && s.count > 0) {
                U32 idx = mixId(s.key) & nextMask;
                while (next[idx].key != EMPTY_KEY) idx = (idx + 1u) & nextMask;
                next[idx].key = s.key;
                next[idx].count = s.count;
                addBloom(s.key);
                ++newLive;
            }
        }
        slots.swap(next);
        mask = nextMask;
        liveCount = newLive;
        usedSlots = newLive;
        ++layoutVersion;
        megaSyncTracked = false;
        megaDirtyIdx.clear();
    }

    inline bool nearFullForDispatch() const {
        if (!isFlat) return false;
        return (static_cast<size_t>(usedSlots) + 256) * 10 >= slots.size() * 7;
    }

    inline I32 findFlatConcurrent(U32 id) const {
        U32 m = mask;
        U32 idx = mixId(id) & m;
        while (true) {
            U32 k = __atomic_load_n(&slots[idx].key, __ATOMIC_ACQUIRE);
            if (k == EMPTY_KEY) return 0;
            if (k == id) return __atomic_load_n(&slots[idx].count, __ATOMIC_ACQUIRE);
            idx = (idx + 1u) & m;
        }
    }
};

struct alignas(32) NodeGeom {
    I16 endTileX;
    I16 endTileYRaw;
    I16 tileY;
    U16 length;
    U16 nodeType;
    U16 tileType;
    U16 preFlags;
    U16 typeFlags;
    I32 tileId;
    float baseCost;
    float delay;
    I16 endSlrRaw;
    I16 endSlrTileY;
};
static_assert(sizeof(NodeGeom) == 32, "NodeGeom must be 32 bytes");

struct DynNode {
    I32 occ;
    float hist;
};
static_assert(sizeof(DynNode) == sizeof(FidesGpuDynNode), "DynNode must match FidesGpuDynNode");

struct ConnData {
    U32 netId;
    U32 src;
    U32 sink;
    I32 origXmin;
    I32 origYmin;
    I32 origXmax;
    I32 origYmax;
    I32 sinkBeginX;
    I32 sinkBeginY;
    I32 sinkTile;
    I32 sinkType;
    I32 doubleHpwl;
    I32 netSize;
    float xCenter;
    float yCenter;
    uint8_t flags;  // 0:shapeOk, 1:useOrigBox, 2:uturn, 3:cross, 4:north, 5:south, 6:sinkCtrl
};

struct GraphData {
    U32 n = 0;
    U32 m = 0;
    std::vector<U32> offset;
    std::vector<U32> edge;
    std::vector<NodeGeom> geom;
    std::vector<DynNode> dyn;

    std::vector<U32> ord;
    std::vector<U32> typeFlags;
    std::vector<U32> tileFlags;
    std::vector<I32> slr;
    std::vector<I32> nextLaguna;
    std::vector<I32> prevLaguna;
    I32 sllLen = 0;

    std::vector<float> pcc;
    std::vector<I32> overusedPos;
    std::vector<U32> overusedList;
    std::vector<U32> lastCommitSeq;
    std::vector<U32> lastCommitNet;
    std::vector<U32> prevOtherSeq;
    U32 commitSeq = 1;

    std::vector<NetScopeData> netScopes;
    std::vector<ConnData> conns;
    std::vector<std::vector<U32>> connPaths;
    std::vector<uint8_t> connRouted;
    std::vector<I64> lastPops;
    std::vector<uint8_t> consecCongested;
    std::vector<uint8_t> connWiden;
    std::vector<uint8_t> tailFailStreak;
    bool ready = false;

    // v40 multi-GPU (1..4 x V100) CUDA dual-engine state & sync buffers
    bool gpuEnabled = false;
    int gpuCount = 0;
    FidesGpuMultiInitInfo gpuMulti{};
    std::vector<U32> gpuDirtyOccStamp;
    U32 gpuDirtyOccEpoch = 1;
    std::vector<U32> gpuDirtyOccIds;
    std::vector<I32> gpuDirtyOccVals;
    std::vector<U32> waveOverusedStamp;
    std::vector<I32> waveOvClaimRem;
    U32 waveOverusedEpoch = 1;
    std::vector<U64> waveCommitKey;
    U32 waveCommitEpoch = 1;
    U32 gpuStampWaveEpoch = 0;
    I64 lastTailFbCount = 0;
    int microStuckRounds = 0;
    struct MegaCacheEntry { U32 netId = UINT32_MAX; U32 layoutVersion = 0; U32 mask = 0; };
    MegaCacheEntry megaCache[1024];
    U32 megaCacheClock = 0;
};

static GraphData G;

constexpr U32 PAGE_SHIFT = 12;
constexpr U32 PAGE_SIZE = 1u << PAGE_SHIFT;
constexpr U32 PAGE_MASK = PAGE_SIZE - 1u;

struct NodeSlot {
    I32 stamp;
    U32 prev;
    float upstream;
    float total;
};

struct StatePage {
    NodeSlot slots[PAGE_SIZE];
};

struct HeapItem {
    float total;
    U32 node;
};

struct OverrideSlot {
    U32 id;
    I32 stamp;
    I32 same;
    I32 occDelta;
};

struct CpuSearchContext {
    static constexpr int TINY_OV_CAP = 32;
    std::vector<StatePage*> pages;
    std::vector<HeapItem> heap;
    std::vector<OverrideSlot> ov;
    OverrideSlot tinyOv[TINY_OV_CAP];
    const NetScopeData* baseScope = nullptr;
    U64 tinyBloom = 0;
    I32 seq = 0;
    I32 ovStamp = 0;
    size_t ovCap = 65536;
    U32 ovMask = 65535u;
    int tinyCount = 0;
    bool useTinyOv = false;
    bool ovEmpty = true;

    CpuSearchContext() : ov(65536) {}

    ~CpuSearchContext() {
        for (StatePage* p : pages) delete p;
    }

    void ensureN(U32 n) {
        size_t needPages = (static_cast<size_t>(n) + PAGE_MASK) >> PAGE_SHIFT;
        if (pages.size() < needPages) pages.resize(needPages, nullptr);
    }

    void beginSearch(U32 n) {
        ensureN(n);
        if (seq >= std::numeric_limits<I32>::max() - 2) {
            for (StatePage* p : pages) {
                if (p) std::memset(p, 0, sizeof(StatePage));
            }
            seq = 0;
        }
        ++seq;
        heap.clear();
    }

    void initHashOv(size_t expectedItems) {
        size_t need = 256;
        while (need < expectedItems * 4) need <<= 1;
        if (ov.size() < need) {
            ov.assign(need, OverrideSlot{0, 0, 0, 0});
            ovStamp = 0;
        }
        ovCap = need;
        ovMask = static_cast<U32>(need - 1);
        if (ovStamp >= std::numeric_limits<I32>::max() - 2) {
            std::memset(ov.data(), 0, ov.size() * sizeof(OverrideSlot));
            ovStamp = 0;
        }
        ++ovStamp;
    }

    void beginOverrides(size_t expectedItems, const NetScopeData* base) {
        baseScope = base;
        tinyCount = 0;
        tinyBloom = 0;
        ovEmpty = true;
        if (expectedItems <= static_cast<size_t>(TINY_OV_CAP)) {
            useTinyOv = true;
            return;
        }
        useTinyOv = false;
        initHashOv(expectedItems);
    }

    OverrideSlot* insertIntoHashOv(U32 id, bool* inserted = nullptr) {
        U32 mask = ovMask;
        U32 idx = mixId(id) & mask;
        while (true) {
            OverrideSlot& s = ov[idx];
            if (s.stamp != ovStamp) {
                s.id = id;
                s.stamp = ovStamp;
                s.same = 0;
                s.occDelta = 0;
                if (inserted) *inserted = true;
                return &s;
            }
            if (s.id == id) {
                if (inserted) *inserted = false;
                return &s;
            }
            idx = (idx + 1u) & mask;
        }
    }

    inline OverrideSlot* findOrInsertOv(U32 id, bool* inserted = nullptr) {
        ovEmpty = false;
        if (useTinyOv) {
            for (int i = 0; i < tinyCount; ++i) {
                if (tinyOv[i].id == id) {
                    if (inserted) *inserted = false;
                    return &tinyOv[i];
                }
            }
            if (tinyCount < TINY_OV_CAP) {
                OverrideSlot& s = tinyOv[tinyCount++];
                s.id = id;
                s.stamp = 0;
                s.same = 0;
                s.occDelta = 0;
                tinyBloom |= (1ULL << (mixId(id) & 63u));
                if (inserted) *inserted = true;
                return &s;
            }
            // Spill tinyOv cleanly to hash table if unique items exceed TINY_OV_CAP.
            useTinyOv = false;
            initHashOv(128);
            for (int i = 0; i < tinyCount; ++i) {
                OverrideSlot* hs = insertIntoHashOv(tinyOv[i].id);
                hs->same = tinyOv[i].same;
                hs->occDelta = tinyOv[i].occDelta;
            }
        }
        return insertIntoHashOv(id, inserted);
    }

    inline const OverrideSlot* findOv(U32 id) const {
        U32 mask = ovMask;
        U32 idx = mixId(id) & mask;
        while (true) {
            const OverrideSlot& s = ov[idx];
            if (s.stamp != ovStamp) return nullptr;
            if (s.id == id) return &s;
            idx = (idx + 1u) & mask;
        }
    }

    inline NodeSlot* slotForRead(U32 id) const {
        StatePage* p = pages[id >> PAGE_SHIFT];
        return p ? &p->slots[id & PAGE_MASK] : nullptr;
    }

    inline NodeSlot* slotForWrite(U32 id) {
        U32 pi = id >> PAGE_SHIFT;
        StatePage* p = pages[pi];
        if (!p) {
            p = new StatePage();
            std::memset(p, 0, sizeof(StatePage));
            pages[pi] = p;
        }
        return &p->slots[id & PAGE_MASK];
    }

    inline bool isVisited(U32 id) const {
        NodeSlot* s = slotForRead(id);
        return s != nullptr && s->stamp == seq;
    }

    inline void pushHeap(float total, U32 node) {
        size_t idx = heap.size();
        heap.push_back({total, node});
        HeapItem* data = heap.data();
        while (idx > 0) {
            size_t parent = (idx - 1) >> 2;
            if (data[parent].total <= total) break;
            data[idx] = data[parent];
            idx = parent;
        }
        data[idx] = {total, node};
    }

    inline U32 popHeap() {
        HeapItem* data = heap.data();
        U32 top = data[0].node;
        HeapItem last = heap.back();
        heap.pop_back();
        size_t n = heap.size();
        if (n > 0) {
            size_t idx = 0;
            while (true) {
                size_t first = (idx << 2) + 1;
                if (first >= n) break;
                size_t end = std::min(n, first + 4);
                size_t best = first;
                float bestVal = data[first].total;
                for (size_t c = first + 1; c < end; ++c) {
                    if (data[c].total < bestVal) {
                        bestVal = data[c].total;
                        best = c;
                    }
                }
                if (last.total <= bestVal) break;
                data[idx] = data[best];
                idx = best;
            }
            data[idx] = last;
        }
        return top;
    }
};

thread_local CpuSearchContext S;

inline int fides_absInt(int a) { return (a < 0) ? -a : a; }

inline float fides_minJdk(float x, float y) {
    if (x != x) return x;
    U32 yb;
    std::memcpy(&yb, &y, sizeof(yb));
    if (x == 0.0f && y == 0.0f && yb == 0x80000000u) return y;
    return (x <= y) ? x : y;
}

inline int endY(const NodeGeom& cg, const NodeGeom* pg) {
    return ((cg.preFlags & PF_SLL) && pg != nullptr && pg->endTileYRaw == cg.endTileYRaw)
            ? static_cast<int>(cg.tileY)
            : static_cast<int>(cg.endTileYRaw);
}

inline int slrFast(const NodeGeom& cg, const NodeGeom* pg) {
    return ((cg.preFlags & PF_SLL) && pg != nullptr && pg->endTileYRaw == cg.endTileYRaw)
            ? static_cast<int>(cg.endSlrTileY)
            : static_cast<int>(cg.endSlrRaw);
}

inline int getUsage(U32 id, int& sameUses) {
    I32 rawOcc = __atomic_load_n(&G.dyn[id].occ, __ATOMIC_ACQUIRE);
    if (rawOcc <= 0) {
        sameUses = 0;
        return rawOcc;
    }
    if (!S.ovEmpty) {
        if (S.useTinyOv) {
            if (S.tinyBloom & (1ULL << (mixId(id) & 63u))) {
                for (int i = 0; i < S.tinyCount; ++i) {
                    if (S.tinyOv[i].id == id) {
                        sameUses = S.tinyOv[i].same;
                        I32 ovOcc = __atomic_load_n(&G.dyn[id].occ, __ATOMIC_ACQUIRE) + S.tinyOv[i].occDelta;
                        if (ovOcc <= 0 && S.baseScope != nullptr) {
                            sameUses = 0;
                        }
                        return ovOcc;
                    }
                }
            }
        } else {
            const OverrideSlot* ov = S.findOv(id);
            if (ov) {
                sameUses = ov->same;
                I32 ovOcc = __atomic_load_n(&G.dyn[id].occ, __ATOMIC_ACQUIRE) + ov->occDelta;
                if (ovOcc <= 0 && S.baseScope != nullptr) {
                    sameUses = 0;
                }
                return ovOcc;
            }
        }
    }
    if (S.baseScope != nullptr) {
        if (!S.baseScope->mayContainBloom(id)) {
            sameUses = 0;
            return rawOcc;
        }
        // Read baseScope first, then occ (paired with addToScope: increment occ before key, clear count before decrement occ).
        sameUses = S.baseScope->findFlatConcurrent(id);
        I32 occVal = __atomic_load_n(&G.dyn[id].occ, __ATOMIC_ACQUIRE);
        if (occVal <= 0) {
            sameUses = 0;
        }
        return occVal;
    }
    sameUses = 0;
    return rawOcc;
}

inline bool willOveruseCached(U32 id, int& outOcc, int& outSame, bool& hasUsage) {
    int cap = static_cast<int>(G.ord[CAPACITY]);
    outOcc = getUsage(id, outSame);
    hasUsage = true;
    return outOcc > cap || (outOcc == cap && outSame == 0);
}

struct SearchQuery {
    U32 source, sink;
    int xmin, ymin, xmax, ymax;
    int origXmin, origXmax;
    bool useBox, uturn, cross, north, south;
    int sinkBeginX, sinkBeginY, sinkTile, sinkType, sinkCtrl, sinkSlr;
    float xCenter, yCenter, shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight;
    float netSizeF, doubleHpwlF;
    int budget;
    bool earlyTermination;
    I64 nodesPopped;
};

inline bool isAccessibleSinkCached(
        U32 child, const NodeGeom& cg, const SearchQuery& q,
        int& outOcc, int& outSame, bool& hasUsage) {
    if (child == q.sink) return true;
    if (!(cg.preFlags & PF_PINBOUNCE)) return false;
    outOcc = getUsage(child, outSame);
    hasUsage = true;
    return outSame != 0;
}

inline int checkAccessAndLookahead(
        U32 /*parent*/, const NodeGeom& pg,
        U32 entering, U32 child, const NodeGeom& cg,
        int childX, int childY,
        SearchQuery& q, bool& lookahead,
        int& outOcc, int& outSame, bool& hasUsage) {
    lookahead = false;
    if (child == q.sink) {
        if (cg.typeFlags & Exclusive) {
            q.earlyTermination = true;
        } else {
            q.earlyTermination = !willOveruseCached(child, outOcc, outSame, hasUsage);
        }
        return 0;
    }

    U32 ct = cg.nodeType;
    U32 pf = pg.typeFlags;
    U32 cf = cg.typeFlags;
    int childTile = cg.tileId;

    if (cf & Local) {
        if (q.cross && (cf & Leading) && slrFast(cg, &pg) != q.sinkSlr) {
            int dir = (cg.preFlags & PF_LAGUNA_NORTH ? 1 : 0) | (cg.preFlags & PF_LAGUNA_SOUTH ? 2 : 0);
            if (((dir & 1) && !q.north) || ((dir & 2) && !q.south)) return 1;
            if (!(cg.preFlags & PF_INT_TILE) || !(cg.preFlags & PF_LAGUNA_WIRE) || childTile == q.sinkTile) {
                return -1;
            }
            if (!willOveruseCached(child, outOcc, outSame, hasUsage)) lookahead = true;
        } else {
            bool allowed = false;
            if (static_cast<U32>(q.sinkType) == G.ord[EAST] || static_cast<U32>(q.sinkType) == G.ord[EX_EAST]) {
                if ((cf & West) || ct == G.ord[RESERVED]) return 1;
                if (static_cast<U32>(q.sinkType) != G.ord[EX_EAST]) return -1;
                allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
            } else if (static_cast<U32>(q.sinkType) == G.ord[WEST] || static_cast<U32>(q.sinkType) == G.ord[EX_WEST]) {
                if ((cf & East) || ct == G.ord[RESERVED]) return 1;
                if (static_cast<U32>(q.sinkType) != G.ord[EX_WEST]) return -1;
                allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
            } else if (static_cast<U32>(q.sinkType) == G.ord[EX_BOTH]) {
                if (childTile != q.sinkTile || ct != G.ord[BOTH] || q.sinkCtrl == 0) return 1;
                allowed = true;
            } else if (static_cast<U32>(q.sinkType) == G.ord[EX_NONLOCAL]) {
                return 1;
            } else {
                return -1;
            }
            if (!allowed) return 1;
            if (!(cg.preFlags & PF_INT_TILE)) return -1;
        }
        return 0;
    }

    if (cf & Nonlocal) {
        bool parentLaguna = (pg.preFlags & PF_LAGUNA_TILE) != 0;
        bool childLaguna = (cg.preFlags & PF_LAGUNA_TILE) != 0;
        if (ct == G.ord[NONLOCAL]) {
            if (pf & Local) {
                if (childLaguna) {
                    if (!q.cross || slrFast(cg, &pg) == q.sinkSlr) return 1;
                } else {
                    if (!(pg.preFlags & PF_PINFEED)
                        || !(cg.preFlags & PF_CLB_TILE)
                        || !(cg.preFlags & PF_CLE_OUTPUT)) {
                        return -1;
                    }
                }
            } else if (pg.preFlags & PF_SLL) {
                if (entering == UINT32_MAX || G.geom[entering].tileId == childTile) return 1;
            }
        } else if (cg.preFlags & (PF_LAGUNA_NORTH | PF_LAGUNA_SOUTH)) {
            if (q.cross && slrFast(cg, &pg) != q.sinkSlr) {
                int dir = (cg.preFlags & PF_LAGUNA_NORTH ? 1 : 0) | (cg.preFlags & PF_LAGUNA_SOUTH ? 2 : 0);
                if (((dir & 1) && q.north) || ((dir & 2) && q.south)) {
                    if (!willOveruseCached(child, outOcc, outSame, hasUsage)) lookahead = true;
                }
            }
        } else {
            return -1;
        }
        if (!q.uturn && cg.delay > 10000.0f) return 1;
        if (parentLaguna || childLaguna) lookahead = true;
        return 0;
    }

    if (cf & Exclusive) {
        if (!isAccessibleSinkCached(child, cg, q, outOcc, outSame, hasUsage)) return 1;
        return 0;
    }

    if (cg.preFlags & PF_SLL) {
        return 0;
    }

    return -1;
}

int expandNode(U32 u, float uUpstream, U32 entering, SearchQuery& q, int depth) {
    if (depth >= 256) return STATUS_FALLBACK;
    const NodeGeom& pg = G.geom[u];
    U32 first = G.offset[u];
    U32 last = G.offset[u + 1];
    for (U32 e = first; e < last; ++e) {
        if (e + 2u < last) {
            __builtin_prefetch(&G.geom[G.edge[e + 2u]], 0, 1);
        }
        U32 child = G.edge[e];
        const NodeGeom& cg = G.geom[child];
        int childX = static_cast<int>(cg.endTileX);
        int childY = endY(cg, &pg);
        if (q.useBox && child != q.sink) {
            if (!(childX > q.xmin && childX < q.xmax && childY > q.ymin && childY < q.ymax)) {
                continue;
            }
        }
        NodeSlot* cs = S.slotForRead(child);
        if (cs != nullptr && cs->stamp == S.seq) continue;

        bool lookahead = false;
        int sameUses = 0;
        int occVal = 0;
        bool hasUsage = false;
        int acc = checkAccessAndLookahead(u, pg, entering, child, cg, childX, childY, q, lookahead, occVal, sameUses, hasUsage);
        if (acc == 1) continue;
        if (acc < 0) return STATUS_FALLBACK;

        if (!hasUsage) {
            occVal = getUsage(child, sameUses);
        }
        int pccIdx = occVal - (sameUses != 0 ? 1 : 0);
        // Clamp negative transient delta under concurrent reads instead of forcing a Java fallback.
        if (pccIdx < 0) pccIdx = 0;
        float pccCost;
        if (pccIdx < static_cast<int>(G.pcc.size())) {
            pccCost = G.pcc[pccIdx];
        } else if (G.pcc.size() >= 2) {
            int lastIdx = static_cast<int>(G.pcc.size()) - 1;
            pccCost = G.pcc[lastIdx] + static_cast<float>(pccIdx - lastIdx) * (G.pcc[lastIdx] - G.pcc[lastIdx - 1]);
        } else {
            return STATUS_FALLBACK;
        }

        float distToCenter = std::fabs(static_cast<float>(childX) - q.xCenter)
                           + std::fabs(static_cast<float>(childY) - q.yCenter);
        float bc = cg.baseCost;
        float bias = bc / q.netSizeF * distToCenter / q.doubleHpwlF;
        float sharing = (sameUses == 0) ? 1.0f : (1.0f + q.shareWeight * static_cast<float>(sameUses));
        float rawNc = bc * G.dyn[child].hist * pccCost;
        float nc = (sameUses == 0) ? rawNc : (rawNc / sharing);
        float nodeCost = nc + fides_minJdk(bias, nc * 0.5f);

        float newPartial = uUpstream + q.rnodeCostWeight * nodeCost;
        newPartial += (sameUses == 0)
                ? (q.rnodeWLWeight * static_cast<float>(cg.length))
                : (q.rnodeWLWeight * static_cast<float>(cg.length) / sharing);

        int deltaX = fides_absInt(childX - q.sinkBeginX);
        int deltaY = fides_absInt(childY - q.sinkBeginY);
        if (q.cross) {
            int childSlr = slrFast(cg, &pg);
            int deltaSLR = fides_absInt(q.sinkSlr - childSlr);
            if (deltaSLR != 0) {
                if (deltaSLR == 1) {
                    int overshootByY = deltaY - G.sllLen;
                    if (overshootByY < 0) {
                        deltaY = G.sllLen - overshootByY;
                    }
                }
                int nextCol = G.nextLaguna[childX];
                int prevCol = G.prevLaguna[childX];
                if (nextCol != prevCol) {
                    int dNext, dAndFromNext, dPrev, dAndFromPrev;
                    if (nextCol == std::numeric_limits<I32>::max() || nextCol >= q.origXmax) {
                        dNext = std::numeric_limits<I32>::max();
                        dAndFromNext = std::numeric_limits<I32>::max();
                    } else {
                        dNext = fides_absInt(nextCol - childX);
                        dAndFromNext = dNext + fides_absInt(q.sinkBeginX - nextCol);
                    }
                    if (prevCol == std::numeric_limits<I32>::min() || prevCol <= q.origXmin) {
                        dPrev = std::numeric_limits<I32>::max();
                        dAndFromPrev = std::numeric_limits<I32>::max();
                    } else {
                        dPrev = fides_absInt(prevCol - childX);
                        dAndFromPrev = dPrev + fides_absInt(q.sinkBeginX - prevCol);
                    }
                    if (dNext == dPrev) {
                        deltaX = std::min(dAndFromNext, dAndFromPrev);
                    } else if (dNext < dPrev && dAndFromNext <= dAndFromPrev + 4) {
                        deltaX = dAndFromNext;
                    } else if (dPrev < dNext && dAndFromPrev <= dAndFromNext + 4) {
                        deltaX = dAndFromPrev;
                    } else {
                        deltaX = (dNext < dPrev) ? dAndFromNext : dAndFromPrev;
                    }
                }
            }
        }
        int distToSink = deltaX + deltaY;
        float newTotal = (sameUses == 0)
                ? (newPartial + q.estWlWeight * static_cast<float>(distToSink))
                : (newPartial + q.estWlWeight * static_cast<float>(distToSink) / sharing);

        if (cs == nullptr) cs = S.slotForWrite(child);
        cs->stamp = S.seq;
        cs->prev = u;
        cs->upstream = newPartial;
        cs->total = newTotal;
        if (lookahead) {
            ++q.nodesPopped;
            int rc = expandNode(child, newPartial, u, q, depth + 1);
            if (rc != 0) return rc;
            if (q.earlyTermination) return 0;
        } else {
            if (q.earlyTermination) {
                S.heap.clear();
                S.pushHeap(newTotal, child);
                return 0;
            }
            S.pushHeap(newTotal, child);
        }
    }
    return 0;
}

I32 runProbe(
        U32 connIdx,
        int xmin, int ymin, int xmax, int ymax, bool useBox,
        float shareWeight, float rnodeCostWeight, float rnodeWLWeight, float estWlWeight,
        int budget,
        std::vector<U32>& outPath, I64& outPops,
        int padRelax = 0) {
    const ConnData& c = G.conns[connIdx];
    outPops = 0;
    if (!(c.flags & 1u) || c.doubleHpwl <= 0) return STATUS_FALLBACK;
    if (c.netId >= G.netScopes.size() || !G.netScopes[c.netId].ready) return STATUS_FALLBACK;

    const NetScopeData& sc = G.netScopes[c.netId];
    const auto& oldPath = G.connPaths[connIdx];
    size_t oldLen = G.connRouted[connIdx] ? oldPath.size() : 0;
    bool useZeroCopyScope = sc.isFlat;
    size_t oldOvItems = (oldLen > 2) ? (oldLen - 2) : 0;
    if (useZeroCopyScope) {
        S.beginOverrides(oldOvItems, &sc);
        if (oldLen > 2) {
            for (size_t i = 1; i + 1 < oldLen; ++i) {
                U32 oid = oldPath[i];
                bool ins = false;
                OverrideSlot* sl = S.findOrInsertOv(oid, &ins);
                if (ins) {
                    sl->same = sc.findFlatConcurrent(oid);
                }
                if (sl->same > 0 && --sl->same == 0) --sl->occDelta;
            }
        }
    } else if (!sc.isFlat) {
        S.beginOverrides(sc.smallIds.size() + oldOvItems, nullptr);
        for (size_t i = 0; i < sc.smallIds.size(); ++i) {
            OverrideSlot* sl = S.findOrInsertOv(sc.smallIds[i]);
            sl->same = sc.smallCounts[i];
        }
        if (oldLen > 2) {
            for (size_t i = 1; i + 1 < oldLen; ++i) {
                OverrideSlot* sl = S.findOrInsertOv(oldPath[i]);
                if (sl->same > 0 && --sl->same == 0) --sl->occDelta;
            }
        }
    } else {
        S.beginOverrides(static_cast<size_t>(sc.liveCount) + oldLen + 16, nullptr);
        for (const ScopeSlot& slot : sc.slots) {
            U32 k = __atomic_load_n(&slot.key, __ATOMIC_ACQUIRE);
            if (k == NetScopeData::EMPTY_KEY) continue;
            I32 cnt = __atomic_load_n(&slot.count, __ATOMIC_ACQUIRE);
            if (cnt > 0) {
                OverrideSlot* sl = S.findOrInsertOv(k);
                sl->same = cnt;
            }
        }
        if (oldLen > 2) {
            for (size_t i = 1; i + 1 < oldLen; ++i) {
                OverrideSlot* sl = S.findOrInsertOv(oldPath[i]);
                if (sl->same > 0 && --sl->same == 0) --sl->occDelta;
            }
        }
    }

    SearchQuery q{};
    q.source = c.src;
    q.sink = c.sink;
    bool useOrigBox = (c.flags & 2u) != 0;
    if (useBox && useOrigBox) {
        q.xmin = std::max(xmin, c.origXmin);
        q.ymin = std::max(ymin, c.origYmin);
        q.xmax = std::min(xmax, c.origXmax);
        q.ymax = std::min(ymax, c.origYmax);
        q.useBox = true;
    } else if (useBox) {
        q.xmin = xmin;
        q.ymin = ymin;
        q.xmax = xmax;
        q.ymax = ymax;
        q.useBox = true;
    } else {
        q.xmin = c.origXmin;
        q.ymin = c.origYmin;
        q.xmax = c.origXmax;
        q.ymax = c.origYmax;
        q.useBox = useOrigBox;
    }
    if (padRelax < 0) {
        q.useBox = false;
    } else if (padRelax > 0 && useOrigBox) {
        q.xmin = std::max(-1, c.origXmin - padRelax);
        q.xmax = c.origXmax + padRelax;
        q.ymin = std::max(-1, c.origYmin - padRelax * 2);
        q.ymax = c.origYmax + padRelax * 2;
        q.useBox = true;
    }
    q.origXmin = c.origXmin;
    q.origXmax = c.origXmax;
    q.uturn = (c.flags & 4u) != 0;
    q.cross = (c.flags & 8u) != 0;
    q.north = (c.flags & 16u) != 0;
    q.south = (c.flags & 32u) != 0;
    q.sinkBeginX = c.sinkBeginX;
    q.sinkBeginY = c.sinkBeginY;
    q.sinkTile = c.sinkTile;
    q.sinkType = c.sinkType;
    q.sinkCtrl = (c.flags & 64u) != 0 ? 1 : 0;
    q.sinkSlr = static_cast<int>(G.geom[q.sink].endSlrRaw);
    q.xCenter = c.xCenter;
    q.yCenter = c.yCenter;
    q.netSizeF = static_cast<float>(c.netSize);
    q.doubleHpwlF = static_cast<float>(c.doubleHpwl);
    q.shareWeight = shareWeight;
    q.rnodeCostWeight = rnodeCostWeight;
    q.rnodeWLWeight = rnodeWLWeight;
    q.estWlWeight = estWlWeight;
    q.budget = budget;
    q.earlyTermination = false;
    q.nodesPopped = 0;

    S.beginSearch(G.n);
    NodeSlot* srcSlot = S.slotForWrite(q.source);
    srcSlot->stamp = S.seq;
    srcSlot->prev = UINT32_MAX;
    srcSlot->upstream = 0.0f;
    srcSlot->total = 0.0f;
    S.pushHeap(0.0f, q.source);

    U32 target = UINT32_MAX;
    while (!S.heap.empty()) {
        U32 u = S.popHeap();
        ++q.nodesPopped;
        if (u == q.sink) {
            target = u;
            break;
        }
        NodeSlot* us = S.slotForRead(u);
        int rc = expandNode(u, us->upstream, us->prev, q, 0);
        if (rc < 0) {
            outPops = q.nodesPopped;
            return rc;
        }
        if (q.budget > 0 && q.nodesPopped > q.budget && !S.heap.empty()) {
            outPops = q.nodesPopped;
            return STATUS_CROSSED_BUDGET;
        }
    }

    outPops = q.nodesPopped;
    if (target == UINT32_MAX) return STATUS_NO_PATH_CORRIDOR;

    outPath.clear();
    U32 cur = target;
    while (cur != UINT32_MAX) {
        outPath.push_back(cur);
        if (cur == q.source) break;
        if (outPath.size() > static_cast<size_t>(q.nodesPopped) + 4096) return STATUS_FALLBACK;
        NodeSlot* cs = S.slotForRead(cur);
        if (!cs || cs->stamp != S.seq) return STATUS_FALLBACK;
        cur = cs->prev;
    }
    if (outPath.empty() || outPath.back() != q.source) return STATUS_FALLBACK;
    return static_cast<I32>(outPath.size());
}

inline void markGpuOccDirty(U32 id) {
    if (G.gpuEnabled) {
        if (G.gpuDirtyOccStamp[id] != G.gpuDirtyOccEpoch) {
            G.gpuDirtyOccStamp[id] = G.gpuDirtyOccEpoch;
            G.gpuDirtyOccIds.push_back(id);
        }
    }
}

inline void onOccChanged(U32 id, I32 beforeOcc, I32 afterOcc) {
    markGpuOccDirty(id);
    I32 cap = static_cast<I32>(G.ord[CAPACITY]);
    if (beforeOcc <= cap && afterOcc > cap) {
        if (G.overusedPos[id] < 0) {
            G.overusedPos[id] = static_cast<I32>(G.overusedList.size());
            G.overusedList.push_back(id);
        }
    } else if (beforeOcc > cap && afterOcc <= cap) {
        I32 pos = G.overusedPos[id];
        if (pos >= 0) {
            U32 backId = G.overusedList.back();
            if (static_cast<size_t>(pos) + 1 < G.overusedList.size()) {
                G.overusedList[pos] = backId;
                G.overusedPos[backId] = pos;
            }
            G.overusedList.pop_back();
            G.overusedPos[id] = -1;
        }
    }
}

inline void ensureScopeCapacitySingleThread(NetScopeData& sc, size_t addNodes) {
    if (sc.isFlat && (static_cast<size_t>(sc.usedSlots) + addNodes + 128u) * 10u >= sc.slots.size() * 7u) {
        sc.rehashFlat();
    }
}

inline void noteMegaSlotDirty(NetScopeData& sc, U32 idx) {
    if (sc.megaSyncTracked) {
        if (sc.megaDirtyIdx.size() < 65536u) {
            sc.megaDirtyIdx.push_back(idx);
        } else {
            sc.megaDirtyIdx.clear();
            ++sc.layoutVersion;
            sc.megaSyncTracked = false;
        }
    }
}

template <class FnOccDelta>
inline void addToScopeCore(NetScopeData& sc, U32 id, I32 delta, FnOccDelta&& fnOccDelta) {
    if (id >= G.n) return;
    if (!sc.isFlat) {
        size_t sz = sc.smallIds.size();
        size_t idx = sz;
        for (size_t i = 0; i < sz; ++i) {
            if (sc.smallIds[i] == id) { idx = i; break; }
        }
        if (idx == sz) {
            if (delta > 0) {
                fnOccDelta(id, +1);
                if (sz >= 64) {
                    sc.smallIds.push_back(id);
                    sc.smallCounts.push_back(delta);
                    sc.initFlatFromPairs(sc.smallIds, sc.smallCounts, sc.smallIds.size());
                    sc.smallIds.clear();
                    sc.smallCounts.clear();
                    sc.smallIds.shrink_to_fit();
                    sc.smallCounts.shrink_to_fit();
                } else {
                    sc.smallIds.push_back(id);
                    sc.smallCounts.push_back(delta);
                }
            }
            return;
        }
        I32 before = sc.smallCounts[idx];
        I32 after = before + delta;
        if (after <= 0) {
            size_t lastIdx = sz - 1;
            if (idx != lastIdx) {
                sc.smallIds[idx] = sc.smallIds[lastIdx];
                sc.smallCounts[idx] = sc.smallCounts[lastIdx];
            }
            sc.smallIds.pop_back();
            sc.smallCounts.pop_back();
            if (before > 0) fnOccDelta(id, -1);
        } else {
            if (before == 0) fnOccDelta(id, +1);
            sc.smallCounts[idx] = after;
        }
        return;
    }

    // Flat table path: Fix #4 store ordering so readers never see sameUses>0 with occ=0.
    U32 m = sc.mask;
    U32 idx = mixId(id) & m;
    while (true) {
        ScopeSlot& sl = sc.slots[idx];
        if (sl.key == NetScopeData::EMPTY_KEY) {
            if (delta > 0) {
                sc.addBloom(id);
                fnOccDelta(id, +1);
                __atomic_store_n(&sl.count, delta, __ATOMIC_RELEASE);
                __atomic_store_n(&sl.key, id, __ATOMIC_RELEASE);
                ++sc.liveCount;
                ++sc.usedSlots;
                noteMegaSlotDirty(sc, idx);
            }
            return;
        }
        if (sl.key == id) {
            I32 before = sl.count;
            I32 after = before + delta;
            if (after < 0) after = 0;
            if (before == 0 && after > 0) {
                sc.addBloom(id);
                fnOccDelta(id, +1);
                __atomic_store_n(&sl.count, after, __ATOMIC_RELEASE);
                ++sc.liveCount;
            } else if (before > 0 && after == 0) {
                __atomic_store_n(&sl.count, 0, __ATOMIC_RELEASE);
                --sc.liveCount;
                fnOccDelta(id, -1);
            } else {
                __atomic_store_n(&sl.count, after, __ATOMIC_RELEASE);
            }
            noteMegaSlotDirty(sc, idx);
            return;
        }
        idx = (idx + 1u) & m;
    }
}

void addToScope(NetScopeData& sc, U32 id, I32 delta) {
    addToScopeCore(sc, id, delta, [&](U32 nid, I32 occDelta) {
        I32 before = G.dyn[nid].occ;
        I32 after = before + occDelta;
        __atomic_store_n(&G.dyn[nid].occ, after, __ATOMIC_RELEASE);
        onOccChanged(nid, before, after);
    });
}

inline void addToScopeDeferred(NetScopeData& sc, U32 id, I32 delta, std::vector<std::pair<U32, I32>>& occDeltas) {
    addToScopeCore(sc, id, delta, [&](U32 nid, I32 occDelta) {
        occDeltas.emplace_back(nid, occDelta);
    });
}

template <class Fn>
inline void addToScopeDeferredFn(NetScopeData& sc, U32 id, I32 delta, Fn&& fnOccDelta) {
    addToScopeCore(sc, id, delta, std::forward<Fn>(fnOccDelta));
}

struct alignas(64) DynamicSlot {
    std::mutex mtx;
    std::condition_variable cv;
    std::atomic<int> state{0}; // 0=IDLE, 1=RUN, 2=DONE, -1=STOP
    alignas(64) std::atomic<bool> waiting{false};
    int taskKind = 0; // 0=probe, 1=parallelFor range
    I32 actIdx = -1;
    I32 curBudget = 0;
    U32 dispatchSeq = 0;
    I32 rc = 0;
    I64 pops = 0;
    std::vector<U32> outPath;
};

struct DynamicExecutor {
    std::vector<std::thread> workers;
    std::vector<DynamicSlot*> slots;
    std::mutex coordMtx;
    std::condition_variable coordCv;
    alignas(64) std::atomic<U64> doneMask{0};
    alignas(64) std::atomic<bool> coordWaiting{false};
    int numWorkers = 0;

    // Generic parallelFor payload (taskKind == 1)
    const std::function<void(size_t, size_t)>* parFn = nullptr;
    alignas(64) std::atomic<size_t> parNext{0};
    size_t parTotal = 0;
    size_t parGrain = 1;

    const I32* activeConns = nullptr;
    const I32* bx0 = nullptr;
    const I32* by0 = nullptr;
    const I32* bx1 = nullptr;
    const I32* by1 = nullptr;
    const uint8_t* buse = nullptr;
    float shareWeight = 0.0f;
    float rnodeCostWeight = 0.0f;
    float rnodeWLWeight = 0.0f;
    float estWlWeight = 0.0f;

    void init(int requestedWorkers) {
        int want = std::max(1, std::min(63, requestedWorkers));
        if (numWorkers >= want) return;
        shutdown();
        numWorkers = want;
        slots.resize(numWorkers, nullptr);
        for (int w = 0; w < numWorkers; ++w) slots[w] = new DynamicSlot();
        doneMask.store(0, std::memory_order_relaxed);
        coordWaiting.store(false, std::memory_order_relaxed);
        workers.reserve(numWorkers);
        for (int w = 0; w < numWorkers; ++w) {
            workers.emplace_back([this, w]() {
                DynamicSlot& sl = *slots[w];
                while (true) {
                    int st = sl.state.load(std::memory_order_acquire);
                    if (st != 1 && st >= 0) {
                        for (int sp = 0; sp < 256; ++sp) {
                            FIDES_PAUSE();
                            st = sl.state.load(std::memory_order_acquire);
                            if (st == 1 || st < 0) break;
                        }
                    }
                    if (st != 1 && st >= 0) {
                        std::unique_lock<std::mutex> lk(sl.mtx);
                        sl.waiting.store(true, std::memory_order_seq_cst);
                        sl.cv.wait(lk, [&]() {
                            int cur = sl.state.load(std::memory_order_seq_cst);
                            return cur == 1 || cur < 0;
                        });
                        sl.waiting.store(false, std::memory_order_relaxed);
                        st = sl.state.load(std::memory_order_acquire);
                    }
                    if (st < 0) return;

                    if (sl.taskKind == 1) {
                        const auto* fnPtr = parFn;
                        const size_t total = parTotal;
                        const size_t grain = parGrain;
                        while (true) {
                            size_t lo = parNext.fetch_add(grain, std::memory_order_relaxed);
                            if (lo >= total) break;
                            size_t hi = std::min(total, lo + grain);
                            (*fnPtr)(lo, hi);
                        }
                    } else {
                        I32 aIdx = sl.actIdx;
                        U32 connIdx = static_cast<U32>(activeConns[aIdx]);
                        sl.rc = runProbe(connIdx, bx0[aIdx], by0[aIdx], bx1[aIdx], by1[aIdx],
                                         buse[aIdx] != 0,
                                         shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                         sl.curBudget, sl.outPath, sl.pops);
                    }

                    sl.state.store(2, std::memory_order_release);
                    doneMask.fetch_or(1ULL << w, std::memory_order_seq_cst);
                    if (coordWaiting.load(std::memory_order_seq_cst)) {
                        std::lock_guard<std::mutex> clk(coordMtx);
                        coordCv.notify_one();
                    }
                }
            });
        }
    }

    template <class Fn>
    void parallelFor(size_t total, size_t grain, Fn&& fn) {
        if (total == 0u) return;
        size_t g = std::max<size_t>(1u, grain);
        if (numWorkers <= 0 || total <= g) {
            fn(0u, total);
            return;
        }
        int activeW = static_cast<int>(std::min<size_t>(static_cast<size_t>(numWorkers), (total + g - 1u) / g));
        if (activeW <= 1) {
            fn(0u, total);
            return;
        }
        std::function<void(size_t, size_t)> cb(fn);
        parFn = &cb;
        parTotal = total;
        parGrain = g;
        parNext.store(0u, std::memory_order_relaxed);
        doneMask.store(0ULL, std::memory_order_relaxed);
        for (int w = 0; w < activeW; ++w) {
            DynamicSlot& sl = *slots[w];
            sl.taskKind = 1;
            sl.state.store(1, std::memory_order_seq_cst);
            if (sl.waiting.load(std::memory_order_seq_cst)) {
                std::lock_guard<std::mutex> lk(sl.mtx);
                sl.cv.notify_one();
            }
        }
        const U64 targetMask = (activeW == 64) ? ~0ULL : ((1ULL << activeW) - 1ULL);
        U64 done = 0ULL;
        while (done != targetMask) {
            U64 m = waitDoneMask();
            done |= m;
            while (m != 0ULL) {
                int w = __builtin_ctzll(m);
                m &= (m - 1ULL);
                slots[w]->state.store(0, std::memory_order_relaxed);
                slots[w]->taskKind = 0;
            }
        }
        parFn = nullptr;
    }

    inline void dispatchTo(int w, I32 actIdx, int curBudget, U32 seq) {
        DynamicSlot& sl = *slots[w];
        sl.actIdx = actIdx;
        sl.curBudget = curBudget;
        sl.dispatchSeq = seq;
        sl.state.store(1, std::memory_order_seq_cst);
        if (sl.waiting.load(std::memory_order_seq_cst)) {
            std::lock_guard<std::mutex> lk(sl.mtx);
            sl.cv.notify_one();
        }
    }

    inline U64 waitDoneMask() {
        U64 m = doneMask.exchange(0, std::memory_order_acquire);
        if (m != 0) return m;
        for (int sp = 0; sp < 128; ++sp) {
            FIDES_PAUSE();
            m = doneMask.exchange(0, std::memory_order_acquire);
            if (m != 0) return m;
        }
        std::unique_lock<std::mutex> clk(coordMtx);
        coordWaiting.store(true, std::memory_order_seq_cst);
        coordCv.wait(clk, [&]() { return doneMask.load(std::memory_order_seq_cst) != 0; });
        coordWaiting.store(false, std::memory_order_relaxed);
        return doneMask.exchange(0, std::memory_order_acquire);
    }

    void shutdown() {
        for (int w = 0; w < numWorkers; ++w) {
            if (!slots[w]) continue;
            {
                std::lock_guard<std::mutex> lk(slots[w]->mtx);
                slots[w]->state.store(-1, std::memory_order_release);
            }
            slots[w]->cv.notify_one();
        }
        for (auto& th : workers) {
            if (th.joinable()) th.join();
        }
        workers.clear();
        for (DynamicSlot* p : slots) delete p;
        slots.clear();
        numWorkers = 0;
    }

    ~DynamicExecutor() { shutdown(); }
};

static DynamicExecutor EXEC;

inline void flushGpuOccDeltas() {
    if (!G.gpuEnabled || G.gpuDirtyOccIds.empty()) return;
    U32 cnt = static_cast<U32>(G.gpuDirtyOccIds.size());
    G.gpuDirtyOccVals.resize(cnt);
    const U32* ids = G.gpuDirtyOccIds.data();
    I32* vals = G.gpuDirtyOccVals.data();
    EXEC.parallelFor(cnt, 4096u, [&](size_t lo, size_t hi) {
        for (size_t i = lo; i < hi; ++i) {
            vals[i] = G.dyn[ids[i]].occ;
        }
    });
    fidesGpuApplyOccDeltasMulti(ids, vals, cnt);
    G.gpuDirtyOccIds.clear();
    if (++G.gpuDirtyOccEpoch == 0u) {
        std::memset(G.gpuDirtyOccStamp.data(), 0, G.gpuDirtyOccStamp.size() * sizeof(U32));
        G.gpuDirtyOccEpoch = 1u;
    }
}

struct BoundaryPool {
    template <class F>
    static void runChunks(int chunks, F&& f) {
        if (chunks <= 1) { f(0); return; }
        std::vector<std::thread> ths;
        ths.reserve(chunks - 1);
        for (int c = 1; c < chunks; ++c) ths.emplace_back(f, c);
        f(0);
        for (auto& th : ths) th.join();
    }
};

template <class T>
bool readSection(std::ifstream& file, U64 at, U64 count, std::vector<T>& out) {
    out.resize(static_cast<size_t>(count));
    file.seekg(static_cast<std::streamoff>(at));
    file.read(reinterpret_cast<char*>(out.data()), static_cast<std::streamsize>(count * sizeof(T)));
    return static_cast<bool>(file);
}

struct InFlightBox {
    I32 x0, y0, x1, y1;
};

inline bool boxesOverlap(const InFlightBox& a, const InFlightBox& b) {
    return a.x0 <= b.x1 && b.x0 <= a.x1 && a.y0 <= b.y1 && b.y0 <= a.y1;
}

}  // namespace

static std::mutex g_preinitMtx;
static bool g_staticPreinitOk = false;

template <typename Fn>
static void parallelForU32(U32 count, int maxThreads, Fn&& fn) {
    if (count == 0u) return;
    int hw = static_cast<int>(std::thread::hardware_concurrency());
    if (hw <= 0) hw = 16;
    int nt = std::min(maxThreads, std::min(hw, 36));
    if (nt <= 1 || count < 65536u) {
        fn(0u, count);
        return;
    }
    std::vector<std::thread> threads;
    threads.reserve(static_cast<size_t>(nt - 1));
    U32 chunk = (count + static_cast<U32>(nt) - 1u) / static_cast<U32>(nt);
    for (int t = 1; t < nt; ++t) {
        U32 lo = static_cast<U32>(t) * chunk;
        if (lo >= count) break;
        U32 hi = std::min(count, lo + chunk);
        threads.emplace_back([&, lo, hi]() { fn(lo, hi); });
    }
    fn(0u, std::min(count, chunk));
    for (auto& th : threads) th.join();
}

static bool preinitStaticImpl(const std::string& rrgPath, const std::string& layPath) {
    if (G.ready || g_staticPreinitOk) return true;
    auto t0 = std::chrono::steady_clock::now();

    std::ifstream rrg(rrgPath, std::ios::binary);
    if (!rrg) return false;
    char magic[8];
    U32 version = 0, n = 0, m = 0, secCount = 0;
    rrg.read(magic, 8);
    rrg.read(reinterpret_cast<char*>(&version), 4);
    rrg.read(reinterpret_cast<char*>(&n), 4);
    rrg.read(reinterpret_cast<char*>(&m), 4);
    rrg.read(reinterpret_cast<char*>(&secCount), 4);
    if (!rrg || std::memcmp(magic, "FIDESRRG", 8) != 0 || version != 2) return false;

    struct Sec { std::string name; U64 count; U64 offset; };
    std::vector<Sec> secs;
    secs.reserve(secCount);
    for (U32 i = 0; i < secCount; ++i) {
        char nameBuf[32];
        U32 dtype = 0, pad = 0;
        U64 cnt = 0, off = 0;
        rrg.read(nameBuf, 32);
        rrg.read(reinterpret_cast<char*>(&dtype), 4);
        rrg.read(reinterpret_cast<char*>(&pad), 4);
        rrg.read(reinterpret_cast<char*>(&cnt), 8);
        rrg.read(reinterpret_cast<char*>(&off), 8);
        size_t len = 0;
        while (len < 32 && nameBuf[len] != '\0') ++len;
        secs.push_back({std::string(nameBuf, len), cnt, off});
    }
    auto findSec = [&](const char* name, U64& cnt, U64& off) -> bool {
        for (const auto& s : secs) {
            if (s.name == name) { cnt = s.count; off = s.offset; return true; }
        }
        return false;
    };

    G.n = n;
    G.m = m;
#ifdef __linux__
    struct NumaInterleaveGuard {
        bool active = false;
        NumaInterleaveGuard() {
            unsigned long mask = 0;
            for (int node = 0; node < 64; ++node) {
                char p[64];
                std::snprintf(p, sizeof(p), "/sys/devices/system/node/node%d", node);
                if (access(p, F_OK) == 0) mask |= (1UL << node);
            }
            if ((mask & (mask - 1UL)) != 0UL) {
                if (syscall(SYS_set_mempolicy, 3 /*MPOL_INTERLEAVE*/, &mask, 8 * sizeof(unsigned long)) == 0) {
                    active = true;
                }
            }
        }
        ~NumaInterleaveGuard() {
            if (active) syscall(SYS_set_mempolicy, 0 /*MPOL_DEFAULT*/, nullptr, 0);
        }
    } numaInterleaveGuard;
#endif
    U64 cnt = 0, off = 0;
    std::vector<I32> nodeType, intentCode, tileType, tileId, endTileX, endTileYRaw, tileY, length;
    std::vector<float> baseCost, delay;
    if (!findSec("offset", cnt, off) || cnt != n + 1 || !readSection(rrg, off, cnt, G.offset)) return false;
    if (!findSec("edge", cnt, off) || cnt != m || !readSection(rrg, off, cnt, G.edge)) return false;
    if (!findSec("nodeType", cnt, off) || cnt != n || !readSection(rrg, off, cnt, nodeType)) return false;
    if (!findSec("intentCode", cnt, off) || cnt != n || !readSection(rrg, off, cnt, intentCode)) return false;
    if (!findSec("tileType", cnt, off) || cnt != n || !readSection(rrg, off, cnt, tileType)) return false;
    if (!findSec("tileId", cnt, off) || cnt != n || !readSection(rrg, off, cnt, tileId)) return false;
    if (!findSec("endTileX", cnt, off) || cnt != n || !readSection(rrg, off, cnt, endTileX)) return false;
    if (!findSec("endTileYRaw", cnt, off) || cnt != n || !readSection(rrg, off, cnt, endTileYRaw)) return false;
    if (!findSec("tileY", cnt, off) || cnt != n || !readSection(rrg, off, cnt, tileY)) return false;
    if (!findSec("length", cnt, off) || cnt != n || !readSection(rrg, off, cnt, length)) return false;
    if (!findSec("baseCost", cnt, off) || cnt != n || !readSection(rrg, off, cnt, baseCost)) return false;
    if (!findSec("delay", cnt, off) || cnt != n || !readSection(rrg, off, cnt, delay)) return false;

    std::ifstream layout(layPath);
    if (!layout) return false;
    U32 slrWords = 0, types = 0, tiles = 0;
    layout >> slrWords >> types >> tiles;
    if (!layout || types == 0 || tiles == 0) return false;
    G.ord.resize(OrdinalCount);
    G.typeFlags.resize(types);
    G.tileFlags.resize(tiles);
    for (U32& v : G.ord) layout >> v;
    for (U32& v : G.typeFlags) layout >> v;
    for (U32& v : G.tileFlags) layout >> v;
    if (!layout) return false;

    G.geom.resize(n);
    std::atomic<bool> boundsOk{true};
    parallelForU32(n, 36, [&](U32 lo, U32 hi) {
        bool localOk = true;
        for (U32 i = lo; i < hi; ++i) {
            if (endTileX[i] < -32768 || endTileX[i] > 32767
                    || endTileYRaw[i] < -32768 || endTileYRaw[i] > 32767
                    || tileY[i] < -32768 || tileY[i] > 32767
                    || length[i] < 0 || length[i] > 65535
                    || nodeType[i] < 0 || static_cast<U32>(nodeType[i]) >= types
                    || tileType[i] < 0 || static_cast<U32>(tileType[i]) >= tiles) {
                fprintf(stdout, "FIDES_NATIVE_CPU v29 geom bounds exceeded at i=%u x=%d y=%d ty=%d len=%d nt=%d tt=%d\n",
                        i, endTileX[i], endTileYRaw[i], tileY[i], length[i], nodeType[i], tileType[i]);
                fflush(stdout);
                localOk = false;
                break;
            }
            NodeGeom& g = G.geom[i];
            g.endTileX = static_cast<I16>(endTileX[i]);
            g.endTileYRaw = static_cast<I16>(endTileYRaw[i]);
            g.tileY = static_cast<I16>(tileY[i]);
            g.length = static_cast<U16>(length[i]);
            g.nodeType = static_cast<U16>(nodeType[i]);
            g.tileType = static_cast<U16>(tileType[i]);
            g.typeFlags = static_cast<U16>(G.typeFlags[nodeType[i]]);
            g.tileId = tileId[i];
            g.baseCost = baseCost[i];
            g.delay = delay[i];
            g.endSlrRaw = -1;
            g.endSlrTileY = -1;

            U16 pf = 0;
            if (static_cast<U32>(nodeType[i]) == G.ord[SLL]) pf |= PF_SLL;
            if (G.tileFlags[tileType[i]] & 1u) pf |= PF_LAGUNA_TILE;
            if (G.tileFlags[tileType[i]] & 2u) pf |= PF_CLB_TILE;
            if (static_cast<U32>(tileType[i]) == G.ord[INT_TILE]) pf |= PF_INT_TILE;
            if (static_cast<U32>(intentCode[i]) == G.ord[PINBOUNCE]) pf |= PF_PINBOUNCE;
            if (static_cast<U32>(intentCode[i]) == G.ord[PINFEED]) pf |= PF_PINFEED;
            if (static_cast<U32>(intentCode[i]) == G.ord[CLE_OUTPUT]) pf |= PF_CLE_OUTPUT;
            g.preFlags = pf;
        }
        if (!localOk) boundsOk.store(false, std::memory_order_relaxed);
    });
    if (!boundsOk.load(std::memory_order_relaxed)) return false;

    {
        std::vector<I32>().swap(nodeType);
        std::vector<I32>().swap(intentCode);
        std::vector<I32>().swap(tileType);
        std::vector<I32>().swap(tileId);
        std::vector<I32>().swap(endTileX);
        std::vector<I32>().swap(endTileYRaw);
        std::vector<I32>().swap(tileY);
        std::vector<I32>().swap(length);
        std::vector<float>().swap(baseCost);
        std::vector<float>().swap(delay);
    }

    G.dyn.resize(n);
    G.overusedPos.assign(n, -1);
    G.overusedList.clear();
    G.lastCommitSeq.assign(n, 0);
    G.lastCommitNet.assign(n, UINT32_MAX);
    G.prevOtherSeq.assign(n, 0);
    G.commitSeq = 1;
    G.gpuStampWaveEpoch = 0;
    G.lastTailFbCount = 0;
    G.microStuckRounds = 0;
    G.netScopes.reserve(1 << 21);
    S.ensureN(n);

    const char* disableGpuEnv = std::getenv("FIDES_V37_DISABLE_GPU");
    bool tryGpu = (disableGpuEnv == nullptr || std::strcmp(disableGpuEnv, "1") != 0);
    if (tryGpu) {
        G.gpuDirtyOccStamp.assign(n, 0u);
        G.gpuDirtyOccEpoch = 1u;
        G.gpuDirtyOccIds.reserve(1 << 20);
        G.gpuDirtyOccVals.reserve(1 << 20);
        G.waveOverusedStamp.assign(n, 0u);
        G.waveOvClaimRem.assign(n, 0);
        G.waveOverusedEpoch = 1u;
        G.waveCommitKey.assign(n, 0ULL);
        G.waveCommitEpoch = 1u;
        for (int ms = 0; ms < 1024; ++ms) G.megaCache[ms] = {UINT32_MAX, 0u, 0u};
    }

    g_staticPreinitOk = true;
    double elapsedSec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    fprintf(stdout, "FIDES_NATIVE_CPU v67 preinit_static=true N=%u M=%u time_s=%.2f\n", n, m, elapsedSec);
    fflush(stdout);
    return true;
}

static bool attachDynamicImpl(
        JNIEnv* env,
        jintArray jSlrData, jint sllLen, jintArray jNextLag, jintArray jPrevLag,
        jintArray jOcc, jfloatArray jHist, jfloatArray jPcc) {
    if (G.ready) return true;
    if (!g_staticPreinitOk) return false;
    auto t0 = std::chrono::steady_clock::now();
    const U32 n = G.n;

    G.sllLen = sllLen;
    jsize slrLen = env->GetArrayLength(jSlrData);
    G.slr.resize(slrLen);
    env->GetIntArrayRegion(jSlrData, 0, slrLen, G.slr.data());

    jsize nlLen = env->GetArrayLength(jNextLag);
    G.nextLaguna.resize(nlLen);
    env->GetIntArrayRegion(jNextLag, 0, nlLen, G.nextLaguna.data());

    jsize plLen = env->GetArrayLength(jPrevLag);
    G.prevLaguna.resize(plLen);
    env->GetIntArrayRegion(jPrevLag, 0, plLen, G.prevLaguna.data());

    auto slrAtY = [&](int y) -> I16 {
        return static_cast<I16>((G.slr[0] == 1 && y >= 0 && y < G.slr[2]) ? G.slr[6 + y] : -1);
    };
    auto lagDir = [&](int nt) -> int {
        return G.slr[6 + G.slr[2] + 2 * G.slr[3] + nt];
    };
    auto isLagWire = [&](int tile) -> bool {
        return tile >= 0 && tile < G.slr[5] && G.slr[6 + G.slr[2] + 2 * G.slr[3] + G.slr[4] + tile] != 0;
    };

    std::vector<I32> occTmp(n);
    std::vector<float> histTmp(n);
    env->GetIntArrayRegion(jOcc, 0, static_cast<jsize>(n), occTmp.data());
    env->GetFloatArrayRegion(jHist, 0, static_cast<jsize>(n), histTmp.data());
    G.overusedList.clear();
    I32 cap = static_cast<I32>(G.ord[CAPACITY]);
    for (U32 id = 0; id < n; ++id) {
        G.dyn[id].occ = occTmp[id];
        G.dyn[id].hist = histTmp[id];
        if (occTmp[id] > cap) {
            G.overusedPos[id] = static_cast<I32>(G.overusedList.size());
            G.overusedList.push_back(id);
        }
    }

    jsize pccLen = env->GetArrayLength(jPcc);
    G.pcc.resize(pccLen);
    env->GetFloatArrayRegion(jPcc, 0, pccLen, G.pcc.data());

    const char* disableGpuEnv = std::getenv("FIDES_V37_DISABLE_GPU");
    bool tryGpu = (disableGpuEnv == nullptr || std::strcmp(disableGpuEnv, "1") != 0);
    std::vector<FidesGpuNodeHot> gpuHot(tryGpu ? n : 0);
    std::atomic<bool> boundsOk{true};
    parallelForU32(n, 36, [&](U32 lo, U32 hi) {
        bool localOk = true;
        for (U32 i = lo; i < hi; ++i) {
            NodeGeom& g = G.geom[i];
            g.endSlrRaw = slrAtY(g.endTileYRaw);
            g.endSlrTileY = slrAtY(g.tileY);

            U16 pf = g.preFlags & static_cast<U16>(~(PF_LAGUNA_WIRE | PF_LAGUNA_NORTH | PF_LAGUNA_SOUTH));
            if (isLagWire(g.tileId)) pf |= PF_LAGUNA_WIRE;
            int ld = lagDir(g.nodeType);
            if (ld & 1) pf |= PF_LAGUNA_NORTH;
            if (ld & 2) pf |= PF_LAGUNA_SOUTH;
            g.preFlags = pf;

            if (tryGpu && localOk) {
                if (g.endTileX < 0 || g.endTileX > 1023
                        || g.endTileYRaw < 0 || g.endTileYRaw > 2047
                        || g.tileY < 0 || g.tileY > 2047
                        || g.tileId < 0 || g.tileId > 4194303
                        || g.length > 1023
                        || g.nodeType > 31
                        || g.endSlrRaw < -1 || g.endSlrRaw > 6
                        || g.endSlrTileY < -1 || g.endSlrTileY > 6) {
                    localOk = false;
                } else {
                    U32 gpf = static_cast<U32>(pf) & 0x3FFu;
                    if (g.delay > 10000.0f) gpf |= FG_PF_UTURN_DELAY;
                    FidesGpuNodeHot h{};
                    h.w0 = (static_cast<U32>(g.endTileX) & 0x3FFu)
                         | ((static_cast<U32>(g.endTileYRaw) & 0x7FFu) << 10)
                         | ((static_cast<U32>(g.tileY) & 0x7FFu) << 21);
                    h.w1 = (static_cast<U32>(g.tileId) & 0x3FFFFFu)
                         | ((static_cast<U32>(g.length) & 0x3FFu) << 22);
                    h.w2 = (gpf & 0x7FFu)
                         | ((static_cast<U32>(g.typeFlags) & 0x3Fu) << 11)
                         | ((static_cast<U32>(g.nodeType) & 0x1Fu) << 17)
                         | ((static_cast<U32>(g.endSlrRaw + 1) & 0x7u) << 22)
                         | ((static_cast<U32>(g.endSlrTileY + 1) & 0x7u) << 25);
                    h.baseCost = g.baseCost;
                    gpuHot[i] = h;
                }
            }
        }
        if (!localOk) boundsOk.store(false, std::memory_order_relaxed);
    });

    if (tryGpu) {
        int reqMaxGpus = FG_MAX_GPUS;
        if (const char* mgEnv = std::getenv("FIDES_V38_MAX_GPUS")) {
            int parsed = std::atoi(mgEnv);
            if (parsed >= 1 && parsed <= FG_MAX_GPUS) reqMaxGpus = parsed;
        }
        bool ok = boundsOk.load(std::memory_order_relaxed);
        if (ok && fidesGpuInitMulti(
                G.n, G.m, G.offset.data(), G.edge.data(),
                gpuHot.data(), reinterpret_cast<const FidesGpuDynNode*>(G.dyn.data()),
                G.ord.data(), G.sllLen,
                G.nextLaguna.data(), G.prevLaguna.data(), static_cast<I32>(G.nextLaguna.size()),
                G.pcc.data(), static_cast<I32>(G.pcc.size()), reqMaxGpus, &G.gpuMulti)) {
            G.gpuEnabled = true;
            G.gpuCount = G.gpuMulti.numGpus;
            U32 totTa = 0u, totTb = 0u;
            for (int d = 0; d < G.gpuCount; ++d) {
                const auto& ci = G.gpuMulti.cards[d];
                totTa += ci.tierAWarps;
                totTb += ci.tierBCtas;
                fprintf(stdout,
                        "FIDES_V40_GPU_DEV slot=%d dev=%d uuid=%s res=%.2fGiB arena=%.2fGiB free=%.2fGiB taWarps=%u tbCtas=%u init_s=%.2f\n",
                        d, ci.deviceId, ci.deviceUuid,
                        ci.residentBytes / double(1ULL << 30),
                        ci.arenaBytes / double(1ULL << 30),
                        ci.freeBytesAfter / double(1ULL << 30),
                        ci.tierAWarps, ci.tierBCtas, ci.initSec);
            }
            fprintf(stdout,
                    "FIDES_V37_GPU ready=true numGpus=%d dev=%s uuid=%s res=%.2fGiB arena=%.2fGiB free=%.2fGiB taWarps=%u tbCtas=%u init_s=%.2f\n",
                    G.gpuCount, G.gpuMulti.cards[0].deviceName, G.gpuMulti.cards[0].deviceUuid,
                    G.gpuMulti.cards[0].residentBytes / double(1ULL << 30),
                    G.gpuMulti.cards[0].arenaBytes / double(1ULL << 30),
                    G.gpuMulti.cards[0].freeBytesAfter / double(1ULL << 30),
                    totTa, totTb, G.gpuMulti.initSec);
            fflush(stdout);
        } else {
            fprintf(stdout, "FIDES_V37_GPU ready=false boundsOk=%d (falling back to CPU)\n", (int)ok);
            fflush(stdout);
        }
    }

    G.ready = true;
    double elapsedSec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    fprintf(stdout, "FIDES_V67_NATIVE_ATTACH time_s=%.2f\n", elapsedSec);
    fflush(stdout);
    return true;
}

extern "C" {

JNIEXPORT jboolean JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativePreinitStatic(
        JNIEnv* env, jclass, jstring jRrgPath, jstring jLayoutPath) {
    std::lock_guard<std::mutex> lock(g_preinitMtx);
    if (G.ready || g_staticPreinitOk) return JNI_TRUE;
    const char* rrgC = env->GetStringUTFChars(jRrgPath, nullptr);
    const char* layC = env->GetStringUTFChars(jLayoutPath, nullptr);
    std::string rrgPath(rrgC);
    std::string layPath(layC);
    env->ReleaseStringUTFChars(jRrgPath, rrgC);
    env->ReleaseStringUTFChars(jLayoutPath, layC);
    return preinitStaticImpl(rrgPath, layPath) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jboolean JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeAttachDynamicState(
        JNIEnv* env, jclass,
        jintArray jSlrData, jint sllLen, jintArray jNextLag, jintArray jPrevLag,
        jintArray jOcc, jfloatArray jHist, jfloatArray jPcc) {
    std::lock_guard<std::mutex> lock(g_preinitMtx);
    if (G.ready) return JNI_TRUE;
    if (!g_staticPreinitOk) return JNI_FALSE;
    return attachDynamicImpl(env, jSlrData, sllLen, jNextLag, jPrevLag, jOcc, jHist, jPcc) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jboolean JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeInit(
        JNIEnv* env, jclass, jstring jRrgPath, jstring jLayoutPath,
        jintArray jSlrData, jint sllLen, jintArray jNextLag, jintArray jPrevLag,
        jintArray jOcc, jfloatArray jHist, jfloatArray jPcc) {
    std::lock_guard<std::mutex> lock(g_preinitMtx);
    if (G.ready) return JNI_TRUE;
    if (!g_staticPreinitOk) {
        const char* rrgC = env->GetStringUTFChars(jRrgPath, nullptr);
        const char* layC = env->GetStringUTFChars(jLayoutPath, nullptr);
        std::string rrgPath(rrgC);
        std::string layPath(layC);
        env->ReleaseStringUTFChars(jRrgPath, rrgC);
        env->ReleaseStringUTFChars(jLayoutPath, layC);
        if (!preinitStaticImpl(rrgPath, layPath)) return JNI_FALSE;
    }
    return attachDynamicImpl(env, jSlrData, sllLen, jNextLag, jPrevLag, jOcc, jHist, jPcc) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeInitScope(
        JNIEnv* env, jclass, jint netId, jintArray jMembers) {
    if (netId < 0) return;
    size_t uid = static_cast<size_t>(netId);
    if (G.netScopes.size() <= uid) G.netScopes.resize(uid + 1);
    NetScopeData& sc = G.netScopes[uid];
    sc.smallIds.clear();
    sc.smallCounts.clear();
    sc.slots.clear();
    sc.bloom.clear();
    sc.bloomMask = 0;
    sc.isFlat = false;
    sc.liveCount = 0;
    sc.usedSlots = 0;
    sc.ready = true;
    if (!jMembers) return;
    jsize len = env->GetArrayLength(jMembers);
    if (len <= 0) return;
    std::vector<U32> sorted;
    sorted.reserve(static_cast<size_t>(len));
    jint* ptr = static_cast<jint*>(env->GetPrimitiveArrayCritical(jMembers, nullptr));
    if (!ptr) return;
    for (jsize i = 0; i < len; ++i) {
        U32 id = static_cast<U32>(ptr[i]);
        if (id < G.n) sorted.push_back(id);
    }
    env->ReleasePrimitiveArrayCritical(jMembers, ptr, JNI_ABORT);
    if (sorted.empty()) return;
    std::sort(sorted.begin(), sorted.end());

    std::vector<U32> uniqIds;
    std::vector<I32> uniqCnt;
    uniqIds.reserve(sorted.size());
    uniqCnt.reserve(sorted.size());
    U32 cur = sorted[0];
    I32 cnt = 1;
    for (size_t i = 1; i < sorted.size(); ++i) {
        if (sorted[i] == cur) {
            ++cnt;
        } else {
            uniqIds.push_back(cur);
            uniqCnt.push_back(cnt);
            cur = sorted[i];
            cnt = 1;
        }
    }
    uniqIds.push_back(cur);
    uniqCnt.push_back(cnt);

    if (uniqIds.size() > 64) {
        sc.initFlatFromPairs(uniqIds, uniqCnt, uniqIds.size());
    } else {
        sc.smallIds.swap(uniqIds);
        sc.smallCounts.swap(uniqCnt);
    }
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeInitScopesBatch(
        JNIEnv* env, jclass, jintArray jOffsets, jintArray jNodes, jint numThreads) {
    if (!jOffsets || !jNodes) return;
    jsize offLen = env->GetArrayLength(jOffsets);
    if (offLen <= 1) return;
    size_t numNets = static_cast<size_t>(offLen - 1);
    jsize nodesLen = env->GetArrayLength(jNodes);

    std::vector<jint> offsets(static_cast<size_t>(offLen));
    env->GetIntArrayRegion(jOffsets, 0, offLen, offsets.data());
    std::vector<jint> nodes(static_cast<size_t>(nodesLen));
    if (nodesLen > 0) env->GetIntArrayRegion(jNodes, 0, nodesLen, nodes.data());

    if (G.netScopes.size() < numNets) G.netScopes.resize(numNets);
    int workerThreads = (numThreads <= 1) ? 0 : std::min(63, static_cast<int>(numThreads) - 1);
    if (workerThreads > 0) EXEC.init(workerThreads);

    auto initOne = [&](size_t uid) {
        NetScopeData& sc = G.netScopes[uid];
        sc.smallIds.clear();
        sc.smallCounts.clear();
        sc.slots.clear();
        sc.bloom.clear();
        sc.bloomMask = 0;
        sc.isFlat = false;
        sc.liveCount = 0;
        sc.usedSlots = 0;
        sc.ready = true;
        I32 s0 = offsets[uid];
        I32 s1 = offsets[uid + 1u];
        if (s1 <= s0) return;
        std::vector<U32> sorted;
        sorted.reserve(static_cast<size_t>(s1 - s0));
        for (I32 p = s0; p < s1; ++p) {
            U32 id = static_cast<U32>(nodes[p]);
            if (id < G.n) sorted.push_back(id);
        }
        if (sorted.empty()) return;
        std::sort(sorted.begin(), sorted.end());

        std::vector<U32> uniqIds;
        std::vector<I32> uniqCnt;
        uniqIds.reserve(sorted.size());
        uniqCnt.reserve(sorted.size());
        U32 cur = sorted[0];
        I32 cnt = 1;
        for (size_t i = 1; i < sorted.size(); ++i) {
            if (sorted[i] == cur) {
                ++cnt;
            } else {
                uniqIds.push_back(cur);
                uniqCnt.push_back(cnt);
                cur = sorted[i];
                cnt = 1;
            }
        }
        uniqIds.push_back(cur);
        uniqCnt.push_back(cnt);

        if (uniqIds.size() > 64) {
            sc.initFlatFromPairs(uniqIds, uniqCnt, uniqIds.size());
        } else {
            sc.smallIds.swap(uniqIds);
            sc.smallCounts.swap(uniqCnt);
        }
    };

    if (workerThreads > 0 && numNets >= 256u) {
        EXEC.parallelFor(numNets, 256u, [&](size_t lo, size_t hi) {
            for (size_t uid = lo; uid < hi; ++uid) initOne(uid);
        });
    } else {
        for (size_t uid = 0; uid < numNets; ++uid) initOne(uid);
    }
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeUpdatePcc(
        JNIEnv* env, jclass, jfloatArray jPcc) {
    jsize len = env->GetArrayLength(jPcc);
    G.pcc.resize(len);
    env->GetFloatArrayRegion(jPcc, 0, len, G.pcc.data());
    if (G.gpuEnabled) {
        fidesGpuUpdatePcc(G.pcc.data(), static_cast<I32>(G.pcc.size()));
    }
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeRegisterConnections(
        JNIEnv* env, jclass,
        jintArray jNetId, jintArray jSrcId, jintArray jSinkId,
        jintArray jOrigXmin, jintArray jOrigYmin, jintArray jOrigXmax, jintArray jOrigYmax,
        jintArray jSinkBeginX, jintArray jSinkBeginY, jintArray jSinkTile, jintArray jSinkType,
        jfloatArray jNetXCenter, jfloatArray jNetYCenter, jintArray jNetSize, jintArray jDoubleHpwl,
        jbyteArray jFlags) {
    jsize n = env->GetArrayLength(jNetId);
    G.conns.resize(static_cast<size_t>(n));
    G.connPaths.resize(static_cast<size_t>(n));
    G.connRouted.assign(static_cast<size_t>(n), 0);
    G.lastPops.assign(static_cast<size_t>(n), 0);
    G.consecCongested.assign(static_cast<size_t>(n), 0);
    G.connWiden.assign(static_cast<size_t>(n), 0);
    G.tailFailStreak.assign(static_cast<size_t>(n), 0);

    std::vector<jint> netId(n), srcId(n), sinkId(n), ox0(n), oy0(n), ox1(n), oy1(n);
    std::vector<jint> sbx(n), sby(n), stile(n), stype(n), nsize(n), dhpwl(n);
    std::vector<jfloat> nxc(n), nyc(n);
    std::vector<jbyte> flg(n);

    env->GetIntArrayRegion(jNetId, 0, n, netId.data());
    env->GetIntArrayRegion(jSrcId, 0, n, srcId.data());
    env->GetIntArrayRegion(jSinkId, 0, n, sinkId.data());
    env->GetIntArrayRegion(jOrigXmin, 0, n, ox0.data());
    env->GetIntArrayRegion(jOrigYmin, 0, n, oy0.data());
    env->GetIntArrayRegion(jOrigXmax, 0, n, ox1.data());
    env->GetIntArrayRegion(jOrigYmax, 0, n, oy1.data());
    env->GetIntArrayRegion(jSinkBeginX, 0, n, sbx.data());
    env->GetIntArrayRegion(jSinkBeginY, 0, n, sby.data());
    env->GetIntArrayRegion(jSinkTile, 0, n, stile.data());
    env->GetIntArrayRegion(jSinkType, 0, n, stype.data());
    env->GetFloatArrayRegion(jNetXCenter, 0, n, nxc.data());
    env->GetFloatArrayRegion(jNetYCenter, 0, n, nyc.data());
    env->GetIntArrayRegion(jNetSize, 0, n, nsize.data());
    env->GetIntArrayRegion(jDoubleHpwl, 0, n, dhpwl.data());
    env->GetByteArrayRegion(jFlags, 0, n, flg.data());

    for (jsize i = 0; i < n; ++i) {
        ConnData& c = G.conns[i];
        c.netId = static_cast<U32>(netId[i]);
        c.src = static_cast<U32>(srcId[i]);
        c.sink = static_cast<U32>(sinkId[i]);
        c.origXmin = ox0[i];
        c.origYmin = oy0[i];
        c.origXmax = ox1[i];
        c.origYmax = oy1[i];
        c.sinkBeginX = sbx[i];
        c.sinkBeginY = sby[i];
        c.sinkTile = stile[i];
        c.sinkType = stype[i];
        c.xCenter = nxc[i];
        c.yCenter = nyc[i];
        c.netSize = nsize[i];
        c.doubleHpwl = dhpwl[i];
        c.flags = static_cast<uint8_t>(flg[i]);
    }
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeRouteRound(
        JNIEnv* env, jclass,
        jint routeIter,
        jintArray jActiveConns, jint activeCount,
        jintArray jBoxXmin, jintArray jBoxYmin, jintArray jBoxXmax, jintArray jBoxYmax, jbyteArray jBoxUse,
        jfloat shareWeight, jfloat rnodeCostWeight, jfloat rnodeWLWeight, jfloat estWlWeight,
        jint wavePopCeiling, jint numThreads,
        jlongArray jOutStats) {
    auto tStarted = std::chrono::steady_clock::now();
    if (!G.ready || activeCount <= 0) {
        jlong zero[12] = {0};
        zero[1] = static_cast<jlong>(G.overusedList.size());
        env->SetLongArrayRegion(jOutStats, 0, 12, zero);
        return;
    }

    int workerThreads = (numThreads <= 1) ? 0 : std::min(63, static_cast<int>(numThreads) - 1);
    if (workerThreads > 0) EXEC.init(workerThreads);

    std::vector<I32> activeConns(activeCount), bx0(activeCount), by0(activeCount), bx1(activeCount), by1(activeCount);
    std::vector<uint8_t> buse(activeCount);
    env->GetIntArrayRegion(jActiveConns, 0, activeCount, activeConns.data());
    env->GetIntArrayRegion(jBoxXmin, 0, activeCount, bx0.data());
    env->GetIntArrayRegion(jBoxYmin, 0, activeCount, by0.data());
    env->GetIntArrayRegion(jBoxXmax, 0, activeCount, bx1.data());
    env->GetIntArrayRegion(jBoxYmax, 0, activeCount, by1.data());
    env->GetByteArrayRegion(jBoxUse, 0, activeCount, reinterpret_cast<jbyte*>(buse.data()));

    // PPT P11 bounded 4-step widening: gentle ±4/±8 -> ±10/±20 -> ±22/±44 -> origBox (buse=0)
    auto applyWidenBox = [&](I32 aIdx, uint8_t lvl) {
        U32 cIdx = static_cast<U32>(activeConns[aIdx]);
        const ConnData& cd = G.conns[cIdx];
        if (buse[aIdx] == 0 || lvl >= 4 || bx0[aIdx] >= bx1[aIdx] || by0[aIdx] >= by1[aIdx]) {
            buse[aIdx] = 0;
            return;
        }
        if (lvl == 0) {
            bx0[aIdx] = std::max(cd.origXmin, bx0[aIdx]);
            by0[aIdx] = std::max(cd.origYmin, by0[aIdx]);
            bx1[aIdx] = std::min(cd.origXmax, bx1[aIdx]);
            by1[aIdx] = std::min(cd.origYmax, by1[aIdx]);
            return;
        }
        static constexpr I32 kPadX[4] = {0, 4, 10, 22};
        static constexpr I32 kPadY[4] = {0, 8, 20, 44};
        I32 padX = kPadX[lvl];
        I32 padY = kPadY[lvl];
        bx0[aIdx] = std::max(cd.origXmin, bx0[aIdx] - padX);
        by0[aIdx] = std::max(cd.origYmin, by0[aIdx] - padY);
        bx1[aIdx] = std::min(cd.origXmax, bx1[aIdx] + padX);
        by1[aIdx] = std::min(cd.origYmax, by1[aIdx] + padY);
    };

    // Preserve F1Corridor geometry across all rounds; widen incrementally per stubborn connection.
    std::vector<I32> baseBx0 = bx0, baseBy0 = by0, baseBx1 = bx1, baseBy1 = by1;
    for (int i = 0; i < activeCount; ++i) {
        U32 cIdx = static_cast<U32>(activeConns[i]);
        applyWidenBox(i, G.connWiden[cIdx]);
    }
    auto bumpWiden = [&](I32 aIdx, uint8_t minTargetLvl) -> bool {
        if (buse[aIdx] == 0) return false;
        U32 cIdx = static_cast<U32>(activeConns[aIdx]);
        uint8_t cur = G.connWiden[cIdx];
        uint8_t next = (cur < minTargetLvl) ? minTargetLvl : static_cast<uint8_t>(std::min<int>(4, cur + 1));
        if (next <= cur) return false;
        G.connWiden[cIdx] = next;
        bx0[aIdx] = baseBx0[aIdx];
        by0[aIdx] = baseBy0[aIdx];
        bx1[aIdx] = baseBx1[aIdx];
        by1[aIdx] = baseBy1[aIdx];
        applyWidenBox(aIdx, next);
        return true;
    };

    EXEC.activeConns = activeConns.data();
    EXEC.bx0 = bx0.data();
    EXEC.by0 = by0.data();
    EXEC.bx1 = bx1.data();
    EXEC.by1 = by1.data();
    EXEC.buse = buse.data();
    EXEC.shareWeight = shareWeight;
    EXEC.rnodeCostWeight = rnodeCostWeight;
    EXEC.rnodeWLWeight = rnodeWLWeight;
    EXEC.estWlWeight = estWlWeight;

    const I32 cap = static_cast<I32>(G.ord[CAPACITY]);
    const size_t numNets = G.netScopes.size();
    std::vector<I32> netQueueHead(numNets, -1);
    std::vector<I32> netQueueTail(numNets, -1);
    std::vector<I32> nextInNet(activeCount, -1);
    std::vector<I32> netInFlight(numNets, 0);
    std::vector<I32> netConflicts(numNets, 0);
    std::vector<uint8_t> netInMultiList(numNets, 0);
    std::vector<uint8_t> netPausedRehash(numNets, 0);
    std::vector<std::vector<InFlightBox>> netBoxes(numNets);

    std::vector<I32> retryQueue, baseHeavyQueue, relaxedQueue, tailSoloQueue;
    retryQueue.reserve(4096);
    baseHeavyQueue.reserve(4096);
    relaxedQueue.reserve(2048);
    tailSoloQueue.reserve(512);
    std::vector<uint8_t> retryStreak(activeCount, 0);

    I64 routedCount = 0, parallelCommits = 0, serialCommits = 0, conflictRetries = 0;
    I64 sameNetOk = 0, baseHeavyCount = 0, relaxedHeavyCount = 0;
    I64 totalPops = 0, fallbackCount = 0, unroutableCount = 0;
    I64 skippedLive = 0;
    I64 tier1Count = 0, tier2Count = 0, tier3Count = 0;
    I64 gpuCommits = 0, gpuPops = 0, gpuNs = 0, gpuWaves = 0, gpuOverflows = 0, gpuFallbacks = 0;
    I64 megaUploadCount = 0;
    double hostPickS = 0.0, hostMetaS = 0.0, hostPackS = 0.0, hostP4AS = 0.0, hostP4BS = 0.0, hostP4CS = 0.0;
    double megaPackS = 0.0, megaGpuS = 0.0;
    double hostMetaLoopS = 0.0, hostMetaSortS = 0.0, hostMetaUpWrapS = 0.0, hostMetaLiveS = 0.0, hostMetaBuildS = 0.0;
    I64 hostMetaDirtyN = 0, hostMetaPatchN = 0, hostMetaWTot = 0, hostMetaPTot = 0;
    I64 nextLogRouted = 500000;
    auto lastLogTime = tStarted;

    // Live RWRoute shouldRoute() check right before dispatch
    auto shouldRouteLive = [&](U32 connIdx) -> bool {
        if (routeIter <= 1 || !G.connRouted[connIdx]) return true;
        const auto& path = G.connPaths[connIdx];
        for (U32 id : path) {
            if (__atomic_load_n(&G.dyn[id].occ, __ATOMIC_RELAXED) > cap) return true;
        }
        return false;
    };

    auto effBoxOf = [&](I32 aIdx) -> InFlightBox {
        const ConnData& cd = G.conns[activeConns[aIdx]];
        if (buse[aIdx] != 0) {
            return InFlightBox{bx0[aIdx], by0[aIdx], bx1[aIdx], by1[aIdx]};
        }
        return InFlightBox{cd.origXmin, cd.origYmin, cd.origXmax, cd.origYmax};
    };

    auto commitStampedPath = [&](U32 netId, const std::vector<U32>& newPath) {
        if (G.commitSeq >= 4000000000u) {
            std::memset(G.lastCommitSeq.data(), 0, G.lastCommitSeq.size() * sizeof(U32));
            std::memset(G.prevOtherSeq.data(), 0, G.prevOtherSeq.size() * sizeof(U32));
            std::fill(G.lastCommitNet.begin(), G.lastCommitNet.end(), UINT32_MAX);
            G.commitSeq = 1;
        }
        U32 cSeq = ++G.commitSeq;
        const U32 stampEp = G.gpuStampWaveEpoch;
        const U64 wKey = (stampEp != 0u) ? ((static_cast<U64>(stampEp) << 32) | static_cast<U64>(netId)) : 0ULL;
        size_t newLen = newPath.size();
        for (size_t i = 1; i + 1 < newLen; ++i) {
            U32 nid = newPath[i];
            if (G.lastCommitNet[nid] != netId) {
                if (G.lastCommitSeq[nid] > G.prevOtherSeq[nid]) {
                    G.prevOtherSeq[nid] = G.lastCommitSeq[nid];
                }
                G.lastCommitNet[nid] = netId;
            }
            G.lastCommitSeq[nid] = cSeq;
            if (stampEp != 0u) {
                __atomic_store_n(&G.waveCommitKey[nid], wKey, __ATOMIC_RELAXED);
            }
        }
    };

    struct ReadyNetItem {
        I64 prio;
        I32 headActIdx;
        U32 netId;
        bool operator>(const ReadyNetItem& o) const {
            if (prio != o.prio) return prio > o.prio;
            return headActIdx > o.headActIdx;
        }
    };

    constexpr int BUDGET_BASE_HEAVY = 262144;

    auto runDynamicPass = [&](const std::vector<I32>& passItems, int curBudget, int passTier) {
        if (passItems.empty()) return;
        auto itemPrio = [&](I32 aIdx) -> I64 {
            return (passTier >= 3) ? -G.lastPops[activeConns[aIdx]] : static_cast<I64>(aIdx);
        };
        std::fill(netQueueHead.begin(), netQueueHead.end(), -1);
        std::fill(netQueueTail.begin(), netQueueTail.end(), -1);
        std::fill(netInFlight.begin(), netInFlight.end(), 0);
        std::fill(netConflicts.begin(), netConflicts.end(), 0);
        std::fill(netInMultiList.begin(), netInMultiList.end(), 0);
        std::fill(netPausedRehash.begin(), netPausedRehash.end(), 0);
        retryQueue.clear();
        tailSoloQueue.clear();
        size_t retryHead = 0;
        size_t tailSoloHead = 0;

        std::priority_queue<ReadyNetItem, std::vector<ReadyNetItem>, std::greater<ReadyNetItem>> distinctReadyHeap;
        std::vector<U32> activeMultiNets;
        activeMultiNets.reserve(128);

        for (I32 aIdx : passItems) {
            nextInNet[aIdx] = -1;
            U32 netId = G.conns[activeConns[aIdx]].netId;
            if (netQueueHead[netId] < 0) {
                netQueueHead[netId] = aIdx;
                netQueueTail[netId] = aIdx;
                distinctReadyHeap.push(ReadyNetItem{itemPrio(aIdx), aIdx, netId});
            } else {
                nextInNet[netQueueTail[netId]] = aIdx;
                netQueueTail[netId] = aIdx;
            }
        }

        auto routeBudgetExceeded = [&](I32 aIdx) {
            if (passTier == 1) {
                baseHeavyQueue.push_back(aIdx);
                ++baseHeavyCount;
            } else if (passTier == 2) {
                bumpWiden(aIdx, 3);
                relaxedQueue.push_back(aIdx);
                ++relaxedHeavyCount;
            } else {
                ++fallbackCount;
            }
        };

        if (workerThreads == 0) {
            std::vector<U32> singlePath;
            for (I32 aIdx : passItems) retryQueue.push_back(aIdx);
            for (size_t idx = 0; idx < retryQueue.size(); ++idx) {
                I32 aIdx = retryQueue[idx];
                U32 connIdx = static_cast<U32>(activeConns[aIdx]);
                if (!shouldRouteLive(connIdx)) {
                    ++skippedLive;
                    continue;
                }
                I64 pops = 0;
                I32 rc = runProbe(connIdx, bx0[aIdx], by0[aIdx], bx1[aIdx], by1[aIdx],
                                  buse[aIdx] != 0, shareWeight, rnodeCostWeight, rnodeWLWeight,
                                  estWlWeight, curBudget, singlePath, pops);
                totalPops += pops;
                G.lastPops[connIdx] = pops;
                if (rc == STATUS_NO_PATH_CORRIDOR) {
                    if (bumpWiden(aIdx, 1)) retryQueue.push_back(aIdx);
                    else ++unroutableCount;
                } else if (rc == STATUS_CROSSED_BUDGET) {
                    routeBudgetExceeded(aIdx);
                } else if (rc <= 0) {
                    ++fallbackCount;
                } else {
                    const auto& oldPath = G.connPaths[connIdx];
                    size_t oldLen = G.connRouted[connIdx] ? oldPath.size() : 0;
                    U32 netId = G.conns[connIdx].netId;
                    NetScopeData& sc = G.netScopes[netId];
                    ensureScopeCapacitySingleThread(sc, singlePath.size());
                    for (size_t i = 1; i + 1 < oldLen; ++i) addToScope(sc, oldPath[i], -1);
                    for (size_t i = 1; i + 1 < singlePath.size(); ++i) addToScope(sc, singlePath[i], +1);
                    commitStampedPath(netId, singlePath);
                    G.connPaths[connIdx].swap(singlePath);
                    G.connRouted[connIdx] = 1;
                    ++routedCount;
                    ++serialCommits;
                }
            }
            return;
        }

        const int W = EXEC.numWorkers;
        std::vector<uint8_t> workerBusy(W, 0);
        std::vector<InFlightBox> workerBox(W);
        int busyCount = 0;

        auto enqueueRetry = [&](I32 aIdx) {
            retryQueue.push_back(aIdx);
            if (retryHead > 4096 && retryHead * 2 > retryQueue.size()) {
                retryQueue.erase(retryQueue.begin(), retryQueue.begin() + static_cast<ptrdiff_t>(retryHead));
                retryHead = 0;
            }
        };

        auto popLiveFromNetQueue = [&](U32 netId) -> I32 {
            while (netQueueHead[netId] >= 0) {
                I32 aIdx = netQueueHead[netId];
                netQueueHead[netId] = nextInNet[aIdx];
                if (netQueueHead[netId] < 0) netQueueTail[netId] = -1;
                nextInNet[aIdx] = -1;
                if (shouldRouteLive(static_cast<U32>(activeConns[aIdx]))) return aIdx;
                ++skippedLive;
            }
            return -1;
        };

        auto markNetIdleIfNeeded = [&](U32 netId) {
            if (netInFlight[netId] == 0) {
                NetScopeData& sc = G.netScopes[netId];
                if (netPausedRehash[netId]) {
                    if (sc.isFlat) sc.rehashFlat();
                    netPausedRehash[netId] = 0;
                }
                if (netQueueHead[netId] >= 0) {
                    I32 h = netQueueHead[netId];
                    distinctReadyHeap.push(ReadyNetItem{itemPrio(h), h, netId});
                }
            }
        };

        auto capForNet = [&](U32 netId, int activeNetCnt) -> int {
            int c = netConflicts[netId];
            if (c >= 64) return 1;
            if (c >= 16) return 2;
            return std::max(4, std::min(16, (W + activeNetCnt - 1) / std::max(1, activeNetCnt)));
        };

        auto canRunOnNet = [&](U32 netId, const InFlightBox& box, int tier) -> bool {
            NetScopeData& sc = G.netScopes[netId];
            if (netPausedRehash[netId]) return false;
            if (netInFlight[netId] > 0 && sc.nearFullForDispatch()) {
                netPausedRehash[netId] = 1;
                return false;
            }
            if (netInFlight[netId] == 0) return true;
            if (!sc.isFlat) return false;
            if (tier == 2) {
                if (netInFlight[netId] >= 16) return false;
                for (const InFlightBox& b : netBoxes[netId]) {
                    if (boxesOverlap(b, box)) return false;
                }
                return true;
            }
            if (tier == 3) {
                return netInFlight[netId] < capForNet(netId, static_cast<int>(activeMultiNets.size()));
            }
            return false;
        };

        auto dispatchTask = [&](int w, I32 aIdx, const InFlightBox& box, int tier) {
            U32 netId = G.conns[activeConns[aIdx]].netId;
            NetScopeData& sc = G.netScopes[netId];
            if (netInFlight[netId] == 0 && sc.nearFullForDispatch()) {
                sc.rehashFlat();
            }
            workerBusy[w] = 1;
            workerBox[w] = box;
            ++busyCount;
            ++netInFlight[netId];
            netBoxes[netId].push_back(box);
            if (!netInMultiList[netId] && netQueueHead[netId] >= 0 && sc.isFlat) {
                netInMultiList[netId] = 1;
                activeMultiNets.push_back(netId);
            }
            if (tier == 1) ++tier1Count;
            else if (tier == 2) ++tier2Count;
            else if (tier == 3) ++tier3Count;
            EXEC.dispatchTo(w, aIdx, curBudget, G.commitSeq);
        };

        auto pickNextAndDispatch = [&](int w) -> bool {
            // 0. Drain retries without ever stalling unrelated workers (defer persistent streak>=4 to end-of-pass tail)
            while (retryHead < retryQueue.size()) {
                bool dispatched = false;
                size_t limit = std::min(retryQueue.size(), retryHead + 128);
                for (size_t i = retryHead; i < limit; ++i) {
                    I32 aIdx = retryQueue[i];
                    U32 connIdx = static_cast<U32>(activeConns[aIdx]);
                    if (!shouldRouteLive(connIdx)) {
                        retryQueue[i] = retryQueue[retryHead++];
                        ++skippedLive;
                        if (limit < retryQueue.size()) ++limit;
                        continue;
                    }
                    if (retryStreak[aIdx] >= 4) {
                        retryQueue[i] = retryQueue[retryHead++];
                        tailSoloQueue.push_back(aIdx);
                        if (limit < retryQueue.size()) ++limit;
                        continue;
                    }
                    U32 netId = G.conns[connIdx].netId;
                    InFlightBox box = effBoxOf(aIdx);
                    int tier = (netInFlight[netId] == 0) ? 1 : (canRunOnNet(netId, box, 2) ? 2 : 0);
                    if (tier == 0 && netInFlight[netId] > 0 && distinctReadyHeap.empty()
                            && canRunOnNet(netId, box, 3)) {
                        tier = 3;
                    }
                    if (tier > 0) {
                        retryQueue[i] = retryQueue[retryHead++];
                        dispatchTask(w, aIdx, box, tier);
                        dispatched = true;
                        break;
                    }
                }
                if (dispatched) return true;
                break;
            }

            // 1. Tier 1: Distinct ready net (live-filtered)
            while (!distinctReadyHeap.empty()) {
                ReadyNetItem top = distinctReadyHeap.top();
                distinctReadyHeap.pop();
                U32 netId = top.netId;
                if (netInFlight[netId] != 0 || netQueueHead[netId] < 0) continue;
                I32 aIdx = popLiveFromNetQueue(netId);
                if (aIdx < 0) continue;
                dispatchTask(w, aIdx, effBoxOf(aIdx), 1);
                return true;
            }

            // Prune activeMultiNets list
            size_t writePos = 0;
            for (size_t i = 0; i < activeMultiNets.size(); ++i) {
                U32 nid = activeMultiNets[i];
                if (netQueueHead[nid] >= 0) {
                    activeMultiNets[writePos++] = nid;
                } else {
                    netInMultiList[nid] = 0;
                }
            }
            activeMultiNets.resize(writePos);

            // 2. Tier 2: Spatially-disjoint same-net connection on flat-scope active nets
            for (U32 netId : activeMultiNets) {
                NetScopeData& sc = G.netScopes[netId];
                if (!sc.isFlat || netPausedRehash[netId]) continue;
                if (sc.nearFullForDispatch()) { netPausedRehash[netId] = 1; continue; }
                if (netInFlight[netId] >= 16) continue;

                I32 prev = -1;
                I32 cur = netQueueHead[netId];
                int checked = 0;
                while (cur >= 0 && checked < 24) {
                    U32 connIdx = static_cast<U32>(activeConns[cur]);
                    if (!shouldRouteLive(connIdx)) {
                        I32 nxt = nextInNet[cur];
                        if (prev < 0) netQueueHead[netId] = nxt;
                        else nextInNet[prev] = nxt;
                        if (netQueueTail[netId] == cur) netQueueTail[netId] = prev;
                        nextInNet[cur] = -1;
                        cur = nxt;
                        ++skippedLive;
                        continue;
                    }
                    ++checked;
                    InFlightBox box = effBoxOf(cur);
                    bool ov = false;
                    for (const InFlightBox& b : netBoxes[netId]) {
                        if (boxesOverlap(b, box)) { ov = true; break; }
                    }
                    if (!ov) {
                        if (prev < 0) netQueueHead[netId] = nextInNet[cur];
                        else nextInNet[prev] = nextInNet[cur];
                        if (netQueueTail[netId] == cur) netQueueTail[netId] = prev;
                        nextInNet[cur] = -1;
                        dispatchTask(w, cur, box, 2);
                        return true;
                    }
                    prev = cur;
                    cur = nextInNet[cur];
                }
            }

            // 3. Tier 3: Safe multi-core sharing on giant flat nets (no false same-net trunk conflict, per-net cap)
            if (busyCount < W) {
                U32 bestNet = UINT32_MAX;
                I32 bestLoad = std::numeric_limits<I32>::max();
                int activeNetCnt = std::max(1, static_cast<int>(activeMultiNets.size()));
                for (U32 netId : activeMultiNets) {
                    NetScopeData& sc = G.netScopes[netId];
                    if (!sc.isFlat || netPausedRehash[netId] || netQueueHead[netId] < 0) continue;
                    if (sc.nearFullForDispatch()) { netPausedRehash[netId] = 1; continue; }
                    int capN = capForNet(netId, activeNetCnt);
                    if (netInFlight[netId] < capN && netInFlight[netId] < bestLoad) {
                        bestLoad = netInFlight[netId];
                        bestNet = netId;
                    }
                }
                if (bestNet != UINT32_MAX) {
                    I32 aIdx = popLiveFromNetQueue(bestNet);
                    if (aIdx >= 0) {
                        dispatchTask(w, aIdx, effBoxOf(aIdx), 3);
                        return true;
                    }
                }
            }

            // 4. Tail drains ONLY after all parallel queues are quiet (busyCount == 0)
            while (busyCount == 0 && retryHead < retryQueue.size()) {
                I32 aIdx = retryQueue[retryHead++];
                if (!shouldRouteLive(static_cast<U32>(activeConns[aIdx]))) {
                    ++skippedLive;
                    continue;
                }
                retryStreak[aIdx] = 0;
                dispatchTask(w, aIdx, effBoxOf(aIdx), 1);
                return true;
            }
            while (busyCount == 0 && tailSoloHead < tailSoloQueue.size()) {
                I32 aIdx = tailSoloQueue[tailSoloHead++];
                if (!shouldRouteLive(static_cast<U32>(activeConns[aIdx]))) {
                    ++skippedLive;
                    continue;
                }
                retryStreak[aIdx] = 0;
                dispatchTask(w, aIdx, effBoxOf(aIdx), 1);
                return true;
            }
            return false;
        };

        auto fillIdleWorkers = [&]() {
            for (int w = 0; w < W; ++w) {
                if (!workerBusy[w]) {
                    if (!pickNextAndDispatch(w)) {
                        if (distinctReadyHeap.empty() && retryHead >= retryQueue.size()
                                && activeMultiNets.empty() && tailSoloHead >= tailSoloQueue.size()) {
                            break;
                        }
                    }
                }
            }
        };

        fillIdleWorkers();
        while (busyCount > 0 || retryHead < retryQueue.size()
                || !distinctReadyHeap.empty() || tailSoloHead < tailSoloQueue.size()) {
            if (busyCount == 0) {
                fillIdleWorkers();
                if (busyCount == 0) break;
            }
            U64 mask = EXEC.waitDoneMask();
            while (mask != 0) {
                int w = __builtin_ctzll(mask);
                mask &= (mask - 1ULL);
                if (!workerBusy[w]) continue;

                DynamicSlot& sl = *EXEC.slots[w];
                sl.state.store(0, std::memory_order_relaxed);
                workerBusy[w] = 0;
                int inFlightAtFinish = busyCount;
                --busyCount;

                I32 aIdx = sl.actIdx;
                U32 connIdx = static_cast<U32>(activeConns[aIdx]);
                U32 netId = G.conns[connIdx].netId;
                --netInFlight[netId];

                auto& boxes = netBoxes[netId];
                const InFlightBox& wb = workerBox[w];
                for (size_t bi = 0; bi < boxes.size(); ++bi) {
                    if (boxes[bi].x0 == wb.x0 && boxes[bi].y0 == wb.y0
                            && boxes[bi].x1 == wb.x1 && boxes[bi].y1 == wb.y1) {
                        boxes[bi] = boxes.back();
                        boxes.pop_back();
                        break;
                    }
                }

                totalPops += sl.pops;
                G.lastPops[connIdx] = sl.pops;
                if (sl.rc == STATUS_NO_PATH_CORRIDOR) {
                    if (bumpWiden(aIdx, 1)) {
                        enqueueRetry(aIdx);
                    } else {
                        ++unroutableCount;
                    }
                    markNetIdleIfNeeded(netId);
                    continue;
                }
                if (sl.rc == STATUS_CROSSED_BUDGET) {
                    routeBudgetExceeded(aIdx);
                    markNetIdleIfNeeded(netId);
                    continue;
                }
                if (sl.rc <= 0) {
                    ++fallbackCount;
                    markNetIdleIfNeeded(netId);
                    continue;
                }

                const auto& oldPath = G.connPaths[connIdx];
                size_t oldLen = G.connRouted[connIdx] ? oldPath.size() : 0;
                const auto& newPath = sl.outPath;
                size_t newLen = newPath.size();

                // Cross-net vs same-net conflict check:
                // Only reject if another net != netId committed an occupied (>=cap) node after dispatchSeq.
                bool conflict = false;
                bool sawSameNetUpdate = false;
                if (sl.dispatchSeq < G.commitSeq) {
                    for (size_t i = 1; i + 1 < newLen; ++i) {
                        U32 nid = newPath[i];
                        if (G.lastCommitSeq[nid] > sl.dispatchSeq && G.dyn[nid].occ >= cap) {
                            if (G.lastCommitNet[nid] != netId || G.prevOtherSeq[nid] > sl.dispatchSeq) {
                                conflict = true;
                                break;
                            }
                            sawSameNetUpdate = true;
                        }
                    }
                }
                if (conflict) {
                    ++conflictRetries;
                    if (netConflicts[netId] < 128) ++netConflicts[netId];
                    if (retryStreak[aIdx] < 250) ++retryStreak[aIdx];
                    if (retryStreak[aIdx] >= 4) tailSoloQueue.push_back(aIdx);
                    else enqueueRetry(aIdx);
                    markNetIdleIfNeeded(netId);
                    continue;
                }
                if (sawSameNetUpdate) ++sameNetOk;
                if (netConflicts[netId] > 0) --netConflicts[netId];

                commitStampedPath(netId, newPath);

                NetScopeData& sc = G.netScopes[netId];
                for (size_t i = 1; i + 1 < oldLen; ++i) addToScope(sc, oldPath[i], -1);
                for (size_t i = 1; i + 1 < newLen; ++i) addToScope(sc, newPath[i], +1);

                G.connPaths[connIdx].swap(sl.outPath);
                G.connRouted[connIdx] = 1;
                ++routedCount;
                if (inFlightAtFinish > 1) ++parallelCommits;
                else ++serialCommits;

                markNetIdleIfNeeded(netId);
            }

            fillIdleWorkers();

            if (passTier == 1 && activeCount >= 100000) {
                auto now = std::chrono::steady_clock::now();
                double sinceLast = std::chrono::duration<double>(now - lastLogTime).count();
                if (routedCount >= nextLogRouted || sinceLast >= 30.0) {
                    while (nextLogRouted <= routedCount) nextLogRouted += 500000;
                    lastLogTime = now;
                    double elapsedS = std::chrono::duration<double>(now - tStarted).count();
                    fprintf(stdout,
                            "F4_V37_WAVE_CPU routed=%lld/%d skip=%lld par=%lld ser=%lld retries=%lld same_ok=%lld base_h=%lld rel_h=%lld overused=%zu pops=%lld t1=%lld t2=%lld t3=%lld busy=%d t=%.1fs\n",
                            static_cast<long long>(routedCount), activeCount,
                            static_cast<long long>(skippedLive),
                            static_cast<long long>(parallelCommits), static_cast<long long>(serialCommits),
                            static_cast<long long>(conflictRetries), static_cast<long long>(sameNetOk),
                            static_cast<long long>(baseHeavyCount), static_cast<long long>(relaxedHeavyCount),
                            G.overusedList.size(), static_cast<long long>(totalPops),
                            static_cast<long long>(tier1Count), static_cast<long long>(tier2Count),
                            static_cast<long long>(tier3Count), busyCount, elapsedS);
                    fflush(stdout);
                }
            }
        }
    };

    // v35 GPU Dual-Engine Pass Runner (Parallel Phase-2 Direct-Pinned, HPWL_FIRST wave packing, Tier-A/B1/B2)
    std::vector<I32> megaQueue;
    megaQueue.reserve(64);
    std::vector<U32> netWaveStamp(numNets, 0u);
    std::vector<U32> netInWaveCnt(numNets, 0u);
    std::vector<I32> netWaveDescIdx(numNets, -1);
    U32 netWaveEpoch = 1u;

    struct SharedSameDesc {
        U32 mode = 0u;
        U32 offset = 0u;
        U32 param = 0u;
        U64 bloom64 = 0ULL;
    };

    struct PendingMegaUpload {
        size_t wi;
        U32 netId;
        int slot;
        U32 capH;
        size_t packOff;
    };
    struct PendingPatchNet {
        U32 netId;
        U32 slot;
        U32 dirtySz;
        U32 patchOff;
    };

    // Prepare metadata & schedule Mode 3 mega-net cache slots/patches sequentially; heavy SoA unpack, dirty sort & batch upload run parallel below.
    auto prepareNetSameDescMeta = [&](size_t wi, U32 netId, U32& sameCursor,
                                      std::vector<PendingMegaUpload>& pendingUploads, size_t& totalPackWords,
                                      std::vector<PendingPatchNet>& pendingPatchNets,
                                      uint8_t* usedMegaSlots, SharedSameDesc& outDesc) -> int {
        NetScopeData& sc = G.netScopes[netId];
        outDesc = SharedSameDesc{};
        if (!sc.isFlat) {
            U32 len = static_cast<U32>(sc.smallIds.size());
            if (len == 0u) {
                outDesc.mode = 0u;
            } else {
                outDesc.mode = 1u;
                outDesc.offset = sameCursor;
                outDesc.param = len;
                sameCursor += len;
            }
            return 0;
        }
        if (sc.liveCount == 0u) {
            outDesc.mode = 0u;
            return 0;
        }
        if (sc.liveCount <= 8192u) {
            U32 capH = static_cast<U32>(nextPow2(std::max<size_t>(16u, static_cast<size_t>(sc.liveCount) * 2u)));
            outDesc.mode = 2u;
            outDesc.offset = sameCursor;
            outDesc.param = capH - 1u;
            sameCursor += capH;
            return 0;
        }
        int slot = -1;
        if (sc.megaSlot >= 0 && sc.megaSlot < 1024 && G.megaCache[sc.megaSlot].netId == netId) {
            slot = sc.megaSlot;
        } else {
            for (int tries = 0; tries < 1024; ++tries) {
                int cand = static_cast<int>((G.megaCacheClock++) % 1024u);
                if (!usedMegaSlots[cand]) { slot = cand; break; }
            }
        }
        if (slot < 0 || usedMegaSlots[slot]) {
            return -1;
        }
        usedMegaSlots[slot] = 1;
        if (G.megaCache[slot].netId != netId || G.megaCache[slot].layoutVersion != sc.layoutVersion) {
            U32 oldNet = G.megaCache[slot].netId;
            if (oldNet != netId && oldNet < G.netScopes.size()) {
                if (G.netScopes[oldNet].megaSlot == slot) G.netScopes[oldNet].megaSlot = -1;
                G.netScopes[oldNet].megaSyncTracked = false;
                G.netScopes[oldNet].megaDirtyIdx.clear();
            }
            sc.megaSlot = slot;
            U32 capH = static_cast<U32>(sc.slots.size());
            pendingUploads.push_back(PendingMegaUpload{wi, netId, slot, capH, totalPackWords});
            totalPackWords += static_cast<size_t>(capH);
            outDesc.mode = 3u;
            outDesc.offset = static_cast<U32>(slot);
            outDesc.param = sc.mask;
            return 0;
        } else if (!sc.megaDirtyIdx.empty()) {
            pendingPatchNets.push_back(PendingPatchNet{
                netId, static_cast<U32>(slot), static_cast<U32>(sc.megaDirtyIdx.size()), 0u
            });
        }
        outDesc.mode = 3u;
        outDesc.offset = static_cast<U32>(slot);
        outDesc.param = G.megaCache[slot].mask;
        return 0;
    };

    auto populateNetSameDescBuf = [&](U32 netId, SharedSameDesc& desc, U32* sameKeysPtr, I32* sameValsPtr) {
        const NetScopeData& sc = G.netScopes[netId];
        if (desc.mode == 1u) {
            const U32 off = desc.offset;
            const U32 len = desc.param;
            U64 bloom = 0ULL;
            for (U32 j = 0; j < len; ++j) {
                U32 id = sc.smallIds[j];
                sameKeysPtr[off + j] = id;
                sameValsPtr[off + j] = sc.smallCounts[j];
                bloom |= (1ULL << (mixId(id) & 63u));
            }
            desc.bloom64 = bloom;
        } else if (desc.mode == 2u) {
            const U32 off = desc.offset;
            const U32 maskH = desc.param;
            const U32 capH = maskH + 1u;
            std::fill_n(sameKeysPtr + off, capH, NetScopeData::EMPTY_KEY);
            std::fill_n(sameValsPtr + off, capH, 0);
            for (const ScopeSlot& sl : sc.slots) {
                if (sl.key != NetScopeData::EMPTY_KEY && sl.count > 0) {
                    U32 idx = mixId(sl.key) & maskH;
                    while (sameKeysPtr[off + idx] != NetScopeData::EMPTY_KEY) idx = (idx + 1u) & maskH;
                    sameKeysPtr[off + idx] = sl.key;
                    sameValsPtr[off + idx] = sl.count;
                }
            }
        }
    };

    auto fillGpuQueryDirect = [&](I32 aIdx, int curBudget, U32 pathOffset, U32 pathCap,
                                  const SharedSameDesc& sd, U32 oldOff, U32 oldLen,
                                  U32* oldNodesPtr, FidesGpuQuery& q) {
        U32 cIdx = static_cast<U32>(activeConns[aIdx]);
        const ConnData& c = G.conns[cIdx];
        q = FidesGpuQuery{};
        q.src = c.src;
        q.sink = c.sink;
        bool useBox = (buse[aIdx] != 0);
        bool useOrigBox = (c.flags & 2u) != 0;
        if (useBox && useOrigBox) {
            q.xmin = std::max(bx0[aIdx], c.origXmin);
            q.ymin = std::max(by0[aIdx], c.origYmin);
            q.xmax = std::min(bx1[aIdx], c.origXmax);
            q.ymax = std::min(by1[aIdx], c.origYmax);
            q.flags |= FG_QF_USE_BOX;
        } else if (useBox) {
            q.xmin = bx0[aIdx]; q.ymin = by0[aIdx];
            q.xmax = bx1[aIdx]; q.ymax = by1[aIdx];
            q.flags |= FG_QF_USE_BOX;
        } else {
            q.xmin = c.origXmin; q.ymin = c.origYmin;
            q.xmax = c.origXmax; q.ymax = c.origYmax;
            if (useOrigBox) q.flags |= FG_QF_USE_BOX;
        }
        q.origXmin = c.origXmin;
        q.origXmax = c.origXmax;
        if (c.flags & 4u) q.flags |= FG_QF_UTURN;
        if (c.flags & 8u) q.flags |= FG_QF_CROSS;
        if (c.flags & 16u) q.flags |= FG_QF_NORTH;
        if (c.flags & 32u) q.flags |= FG_QF_SOUTH;
        if (c.flags & 64u) q.flags |= FG_QF_SINK_CTRL;
        q.sinkBeginX = c.sinkBeginX;
        q.sinkBeginY = c.sinkBeginY;
        q.sinkTile = c.sinkTile;
        q.sinkType = c.sinkType;
        q.sinkSlr = static_cast<I32>(G.geom[c.sink].endSlrRaw);
        q.xCenter = c.xCenter;
        q.yCenter = c.yCenter;
        q.netSizeF = static_cast<float>(c.netSize);
        q.doubleHpwlF = static_cast<float>(c.doubleHpwl);
        q.budget = curBudget;
        q.sameNetMode = sd.mode;
        q.sameOffset = sd.offset;
        q.sameParam = sd.param;
        q.sameBloom64 = sd.bloom64;
        if (oldLen > 0u) {
            const auto& op = G.connPaths[cIdx];
            U64 ob = 0ULL;
            U32* dst = oldNodesPtr + oldOff;
            for (U32 i = 0u; i < oldLen; ++i) {
                U32 nid = op[i + 1u];
                dst[i] = nid;
                ob |= (1ULL << (mixId(nid) & 63u));
            }
            if (oldLen > 1u) {
                std::sort(dst, dst + oldLen);
            }
            q.oldBloom64 = ob;
            q.oldOffset = oldOff;
            q.oldLen = oldLen;
        }
        q.outPathOffset = pathOffset;
        q.outPathCap = pathCap;
    };

    auto restoreRipped = [&](U32 cIdx) {
        U32 netId = G.conns[cIdx].netId;
        NetScopeData& sc = G.netScopes[netId];
        const auto& oldPath = G.connPaths[cIdx];
        ensureScopeCapacitySingleThread(sc, oldPath.size());
        for (size_t i = 1; i + 1 < oldPath.size(); ++i) addToScope(sc, oldPath[i], +1);
    };

    I64 gpuWideOk = 0;
    auto runGpuPass = [&](const std::vector<I32>& passItems, int curBudget, int passTier,
                          const std::vector<I32>* cpuOverlapItems = nullptr) {
        if (passItems.empty()) {
            if (cpuOverlapItems && !cpuOverlapItems->empty()) {
                G.gpuStampWaveEpoch = 0u;
                runDynamicPass(*cpuOverlapItems, curBudget, passTier);
            }
            return;
        }
        const bool usePreRip = false; // v35+: exact virtual oldPath subtraction (d_countOldPath) in Tier-A/B1
        bool cpuOverlapDone = false;

        std::vector<I32> cpuFallbackItems;
        cpuFallbackItems.reserve(256);
        std::vector<I32> curItems = passItems;
        std::vector<I32> wideRetryItems;
        wideRetryItems.reserve(64);
        std::vector<I32> retries;
        retries.reserve(1024);

        struct GpuReadyNet {
            I32 headIdx;
            U32 netId;
            U32 ver;
            bool operator>(const GpuReadyNet& o) const {
                if (headIdx != o.headIdx) return headIdx > o.headIdx;
                return netId > o.netId;
            }
        };
        const U32 numNets = static_cast<U32>(G.netScopes.size());
        std::vector<I32> netItemHead(numNets, -1);
        std::vector<I32> netItemTail(numNets, -1);
        std::vector<I32> nextSameNetItem(static_cast<size_t>(activeCount), -1);
        std::vector<uint8_t> netInHeap(numNets, 0u);
        std::vector<U32> netVer(numNets, 0u);
        std::priority_queue<GpuReadyNet, std::vector<GpuReadyNet>, std::greater<GpuReadyNet>> readyHeap;
        std::vector<U32> waveRequeueNets;
        waveRequeueNets.reserve(8192);
        size_t liveHeapNets = 0;
        size_t remainingItems = 0;

        auto pushHeapNet = [&](U32 netId) {
            if (netItemHead[netId] >= 0) {
                if (!netInHeap[netId]) { netInHeap[netId] = 1u; ++liveHeapNets; }
                U32 v = ++netVer[netId];
                readyHeap.push(GpuReadyNet{netItemHead[netId], netId, v});
            } else {
                if (netInHeap[netId]) { netInHeap[netId] = 0u; --liveHeapNets; }
                ++netVer[netId];
            }
        };
        auto pushNetItem = [&](I32 aIdx, bool toFront, bool updateHeap) {
            U32 netId = G.conns[activeConns[aIdx]].netId;
            if (netItemHead[netId] < 0) {
                nextSameNetItem[aIdx] = -1;
                netItemHead[netId] = aIdx;
                netItemTail[netId] = aIdx;
                if (updateHeap) pushHeapNet(netId);
            } else if (toFront) {
                nextSameNetItem[aIdx] = netItemHead[netId];
                netItemHead[netId] = aIdx;
                if (updateHeap) pushHeapNet(netId);
            } else {
                nextSameNetItem[netItemTail[netId]] = aIdx;
                nextSameNetItem[aIdx] = -1;
                netItemTail[netId] = aIdx;
                if (updateHeap && !netInHeap[netId]) pushHeapNet(netId);
            }
        };
        auto popNetItem = [&](U32 netId) -> I32 {
            I32 aIdx = netItemHead[netId];
            if (aIdx >= 0) {
                netItemHead[netId] = nextSameNetItem[aIdx];
                nextSameNetItem[aIdx] = -1;
                if (netItemHead[netId] < 0) netItemTail[netId] = -1;
            }
            return aIdx;
        };

        std::vector<I32> pickedActIdx;
        std::vector<uint8_t> pickedRipped;
        std::vector<I32> waveActIdx;
        std::vector<uint8_t> wasRipped;
        std::vector<U32> waveOldOff;
        std::vector<U32> waveOldLen;
        std::vector<U32> waveUniqueNets;
        std::vector<SharedSameDesc> waveNetDescs;
        std::vector<uint8_t> waveNetFailed;
        std::vector<U32> megaPackKeys;
        std::vector<I32> megaPackVals;
        std::vector<PendingMegaUpload> pendingUploads;
        std::vector<PendingPatchNet> pendingPatchNets;
        struct MegaPackChunk { size_t upIdx; U32 j0; U32 j1; };
        std::vector<MegaPackChunk> uploadChunks;
        std::vector<FidesGpuMegaUpload> uploadDescs;
        std::vector<uint8_t> piLive;
        std::vector<FidesGpuMegaPatch> megaPatches;
        const int numDevs = std::max(1, G.gpuCount);
        std::vector<int> netAssignedDev;
        std::vector<I64> netWeight;
        std::vector<U32> netQCount;
        std::vector<U32> netOrder;
        std::vector<int> itemDev;
        std::vector<U32> itemLk;
        U32 devB[FG_MAX_GPUS]{};
        U32 devSameCursor[FG_MAX_GPUS]{};
        U32 devOldCursor[FG_MAX_GPUS]{};
        U32 devUsedWords[FG_MAX_GPUS]{};
        bool devOk[FG_MAX_GPUS]{};
        FidesGpuPinnedWaveBufs devBufs[FG_MAX_GPUS]{};

        const U32 pathCap = (passTier == 1) ? 512u : 8192u;
        const U32 outPathsWords = 8u << 20;
        std::vector<I32> commitNetHead(numNets, -1);
        std::vector<I32> commitNetTail(numNets, -1);
        std::vector<I32> nextCommitInNet;
        std::vector<U32> waveCommitNets;
        waveCommitNets.reserve(8192u);
        constexpr size_t P4_SLICES = 32u;
        constexpr size_t P4_SHARDS = 32u;
        struct ShardOccDelta { U32 id; I32 delta; };
        struct ShardOvEvent { U32 id; I32 before; I32 after; };
        std::vector<std::vector<ShardOccDelta>> shardSliceDeltas(P4_SHARDS * P4_SLICES);
        std::vector<std::vector<U32>> shardDirtyIds(P4_SHARDS);
        std::vector<std::vector<ShardOvEvent>> shardOvEvents(P4_SHARDS);
        enum : uint8_t {
            PA_WIN = 0u,
            PA_NO_PATH = 1u,
            PA_CROSSED_BUDGET = 2u,
            PA_HASH_OVERFLOW = 3u,
            PA_BAD_FALLBACK = 4u,
            PA_CONFLICT = 5u
        };
        std::vector<uint8_t> itemAct;
        std::vector<uint8_t> itemSawSame;

        U32 targetWave = 512u;
        if (passTier == 1) {
            targetWave = (G.gpuCount >= 3) ? std::min<U32>(12288u, static_cast<U32>(G.gpuCount) * 3072u) : 8192u;
        } else if (passTier == 2) {
            targetWave = (G.gpuCount >= 3 && curItems.size() > 4096u) ? 2048u : 1024u;
        } else {
            targetWave = (G.gpuCount >= 3) ? 1024u : 512u;
        }

        auto pickAndLpt = [&](U32 tw, I64& outSkipped, std::vector<I32>& outFallback) {
            if (++netWaveEpoch == 0u) {
                std::memset(netWaveStamp.data(), 0, netWaveStamp.size() * sizeof(U32));
                netWaveEpoch = 1u;
            }
            const U32 curNetEp = netWaveEpoch;

            pickedActIdx.clear();
            pickedRipped.clear();
            waveUniqueNets.clear();
            waveRequeueNets.clear();
            U32 megaWaveCnt = 0u;

            auto tryPickOne = [&](I32 aIdx, U32 maxPerNetLimit) -> int {
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                U32 netId = G.conns[cIdx].netId;

                if (!shouldRouteLive(cIdx)) {
                    ++outSkipped;
                    return 0;
                }

                const ConnData& cd = G.conns[cIdx];
                if (!(cd.flags & 1u) || cd.doubleHpwl <= 0 || cIdx >= G.conns.size()
                        || netId >= G.netScopes.size() || !G.netScopes[netId].ready) {
                    outFallback.push_back(aIdx);
                    return 0;
                }

                const NetScopeData& sc = G.netScopes[netId];
                bool seenNet = (netWaveStamp[netId] == curNetEp);
                if (seenNet && netInWaveCnt[netId] >= maxPerNetLimit) return 1;
                if (!seenNet && sc.isFlat && sc.liveCount > 8192u && megaWaveCnt >= 1024u) return 1;

                if (!seenNet) {
                    netWaveStamp[netId] = curNetEp;
                    netInWaveCnt[netId] = 0u;
                    netWaveDescIdx[netId] = static_cast<I32>(waveUniqueNets.size());
                    waveUniqueNets.push_back(netId);
                    if (sc.isFlat && sc.liveCount > 8192u) ++megaWaveCnt;
                }

                pickedActIdx.push_back(aIdx);
                pickedRipped.push_back(usePreRip ? 1u : 0u);
                ++netInWaveCnt[netId];
                return 2;
            };

            // Pass A: pop 1 live connection per distinct net in strict HPWL_FIRST order
            while (pickedActIdx.size() < tw && !readyHeap.empty()) {
                GpuReadyNet top = readyHeap.top();
                readyHeap.pop();
                U32 netId = top.netId;
                if (!netInHeap[netId] || top.ver != netVer[netId]) continue;
                netInHeap[netId] = 0u;
                --liveHeapNets;
                ++netVer[netId];
                while (netItemHead[netId] >= 0) {
                    I32 aIdx = popNetItem(netId);
                    --remainingItems;
                    int pr = tryPickOne(aIdx, 1u);
                    if (pr == 2) break;
                    if (pr == 1) {
                        pushNetItem(aIdx, true, false);
                        ++remainingItems;
                        break;
                    }
                }
                if (netItemHead[netId] >= 0) waveRequeueNets.push_back(netId);
            }

            // Pass B: tail fill only when distinct active nets fall below tw/2
            for (U32 netId : waveRequeueNets) pushHeapNet(netId);
            waveRequeueNets.clear();
            if (pickedActIdx.size() < tw / 2u && liveHeapNets > 0) {
                size_t effNets = (megaWaveCnt >= 1024u && liveHeapNets > 1024u) ? 1024u : liveHeapNets;
                U32 mpnMax = (passTier == 1 && routeIter == 1) ? 64u : 32u;
                U32 mpnFill = std::min<U32>(mpnMax, std::max<U32>(8u, static_cast<U32>((tw + effNets - 1u) / effNets)));
                while (pickedActIdx.size() < tw && !readyHeap.empty()) {
                    GpuReadyNet top = readyHeap.top();
                    readyHeap.pop();
                    U32 netId = top.netId;
                    if (!netInHeap[netId] || top.ver != netVer[netId]) continue;
                    netInHeap[netId] = 0u;
                    --liveHeapNets;
                    ++netVer[netId];
                    while (netItemHead[netId] >= 0 && pickedActIdx.size() < tw) {
                        I32 aIdx = popNetItem(netId);
                        --remainingItems;
                        int pr = tryPickOne(aIdx, mpnFill);
                        if (pr == 1) {
                            pushNetItem(aIdx, true, false);
                            ++remainingItems;
                            break;
                        }
                    }
                    if (netItemHead[netId] >= 0) waveRequeueNets.push_back(netId);
                }
                for (U32 netId : waveRequeueNets) pushHeapNet(netId);
                waveRequeueNets.clear();
            }

            if (pickedActIdx.empty()) return;

            const size_t W = waveUniqueNets.size();
            netWeight.assign(W, 0);
            netQCount.assign(W, 0u);
            for (I32 aIdx : pickedActIdx) {
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                I32 wi = netWaveDescIdx[G.conns[cIdx].netId];
                ++netQCount[wi];
                netWeight[wi] += std::max<I64>(500LL, G.lastPops[cIdx]);
            }
            if (numDevs <= 1) {
                netAssignedDev.assign(W, 0);
            } else {
                netAssignedDev.resize(W);
                netOrder.resize(W);
                for (size_t wi = 0; wi < W; ++wi) netOrder[wi] = static_cast<U32>(wi);
                std::sort(netOrder.begin(), netOrder.end(), [&](U32 a, U32 b) {
                    if (netWeight[a] != netWeight[b]) return netWeight[a] > netWeight[b];
                    return netQCount[a] > netQCount[b];
                });
                I64 devLoad[FG_MAX_GPUS]{};
                U32 devCnt[FG_MAX_GPUS]{};
                for (U32 wi : netOrder) {
                    int bestD = 0;
                    for (int d = 1; d < numDevs; ++d) {
                        if (devLoad[d] < devLoad[bestD]
                                || (devLoad[d] == devLoad[bestD] && devCnt[d] < devCnt[bestD])) {
                            bestD = d;
                        }
                    }
                    netAssignedDev[wi] = bestD;
                    devLoad[bestD] += netWeight[wi];
                    devCnt[bestD] += netQCount[wi];
                }
            }
        };

        for (int tierB1Mode = 0; !curItems.empty() && tierB1Mode <= 2; ++tierB1Mode) {
            if (tierB1Mode > 0 && passTier != 3) break;
            std::fill(netItemHead.begin(), netItemHead.end(), -1);
            std::fill(netItemTail.begin(), netItemTail.end(), -1);
            std::fill(netInHeap.begin(), netInHeap.end(), 0u);
            liveHeapNets = 0;
            while (!readyHeap.empty()) readyHeap.pop();
            for (I32 aIdx : curItems) pushNetItem(aIdx, false, false);
            for (U32 nid = 0; nid < numNets; ++nid) {
                if (netItemHead[nid] >= 0) pushHeapNet(nid);
            }
            remainingItems = curItems.size();
            wideRetryItems.clear();
            bool hasPrePicked = false;

            while (liveHeapNets > 0 || !retries.empty() || hasPrePicked) {
            for (I32 aIdx : retries) {
                pushNetItem(aIdx, true, true);
                ++remainingItems;
            }
            retries.clear();

            if (!hasPrePicked) {
                if (passTier == 1 && remainingItems <= 64u) {
                    while (!readyHeap.empty()) {
                        GpuReadyNet top = readyHeap.top();
                        readyHeap.pop();
                        U32 netId = top.netId;
                        if (!netInHeap[netId] || top.ver != netVer[netId]) continue;
                        netInHeap[netId] = 0u;
                        --liveHeapNets;
                        ++netVer[netId];
                        while (netItemHead[netId] >= 0) {
                            I32 aIdx = popNetItem(netId);
                            --remainingItems;
                            if (!shouldRouteLive(static_cast<U32>(activeConns[aIdx]))) {
                                ++skippedLive;
                            } else {
                                cpuFallbackItems.push_back(aIdx);
                            }
                        }
                    }
                    break;
                }

                auto tPick0 = std::chrono::steady_clock::now();
                pickAndLpt(targetWave, skippedLive, cpuFallbackItems);
                hostPickS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPick0).count();
                if (pickedActIdx.empty()) continue;
            }
            hasPrePicked = false;

            // Phase 2A-meta: Mode 3 mega slots (1024 cache, 35-core parallel dirty sort/patch & batch upload) & per-GPU query offsets
            auto tMeta0 = std::chrono::steady_clock::now();
            megaPatches.clear();
            pendingUploads.clear();
            pendingPatchNets.clear();
            size_t totalPackWords = 0u;
            uint8_t usedMegaSlots[1024] = {0};
            const size_t W = waveUniqueNets.size();
            hostMetaWTot += static_cast<I64>(W);

            waveNetDescs.resize(W);
            waveNetFailed.assign(W, 0u);
            for (int d = 0; d < numDevs; ++d) {
                devSameCursor[d] = 0u;
                devB[d] = 0u;
                devOldCursor[d] = 0u;
                devUsedWords[d] = 0u;
                devOk[d] = false;
            }
            for (size_t wi = 0; wi < W; ++wi) {
                U32 netId = waveUniqueNets[wi];
                int d = netAssignedDev[wi];
                if (prepareNetSameDescMeta(wi, netId, devSameCursor[d], pendingUploads, totalPackWords,
                                           pendingPatchNets, usedMegaSlots, waveNetDescs[wi]) != 0) {
                    waveNetFailed[wi] = 1u;
                }
            }
            auto tLoop1 = std::chrono::steady_clock::now();
            hostMetaLoopS += std::chrono::duration<double>(tLoop1 - tMeta0).count();

            if (!pendingPatchNets.empty()) {
                auto tSort0 = std::chrono::steady_clock::now();
                if (pendingPatchNets.size() > 1u) {
                    std::sort(pendingPatchNets.begin(), pendingPatchNets.end(),
                              [](const PendingPatchNet& a, const PendingPatchNet& b) {
                                  return a.dirtySz > b.dirtySz;
                              });
                }
                size_t totalDirtyWave = 0u;
                for (const PendingPatchNet& pn : pendingPatchNets) totalDirtyWave += pn.dirtySz;
                hostMetaDirtyN += static_cast<I64>(totalDirtyWave);
                auto sortDedupNet = [&](PendingPatchNet& pn) {
                    NetScopeData& sc = G.netScopes[pn.netId];
                    std::sort(sc.megaDirtyIdx.begin(), sc.megaDirtyIdx.end());
                    sc.megaDirtyIdx.erase(std::unique(sc.megaDirtyIdx.begin(), sc.megaDirtyIdx.end()),
                                          sc.megaDirtyIdx.end());
                    pn.dirtySz = static_cast<U32>(sc.megaDirtyIdx.size());
                };
                if (totalDirtyWave >= 2048u && pendingPatchNets.size() > 1u) {
                    EXEC.parallelFor(pendingPatchNets.size(), 1u, [&](size_t lo, size_t hi) {
                        for (size_t i = lo; i < hi; ++i) sortDedupNet(pendingPatchNets[i]);
                    });
                } else {
                    for (size_t i = 0; i < pendingPatchNets.size(); ++i) sortDedupNet(pendingPatchNets[i]);
                }
                U32 patchCursor = 0u;
                for (PendingPatchNet& pn : pendingPatchNets) {
                    pn.patchOff = patchCursor;
                    patchCursor += pn.dirtySz;
                }
                hostMetaPatchN += static_cast<I64>(patchCursor);
                megaPatches.resize(patchCursor);
                auto emitPatches = [&](const PendingPatchNet& pn) {
                    NetScopeData& sc = G.netScopes[pn.netId];
                    FidesGpuMegaPatch* dst = megaPatches.data() + pn.patchOff;
                    const U32 nP = pn.dirtySz;
                    for (U32 j = 0u; j < nP; ++j) {
                        U32 idx = sc.megaDirtyIdx[j];
                        dst[j] = FidesGpuMegaPatch{pn.slot, idx, sc.slots[idx].key, sc.slots[idx].count};
                    }
                    sc.megaDirtyIdx.clear();
                };
                if (patchCursor >= 2048u && pendingPatchNets.size() > 1u) {
                    EXEC.parallelFor(pendingPatchNets.size(), 1u, [&](size_t lo, size_t hi) {
                        for (size_t i = lo; i < hi; ++i) emitPatches(pendingPatchNets[i]);
                    });
                } else {
                    for (size_t i = 0; i < pendingPatchNets.size(); ++i) emitPatches(pendingPatchNets[i]);
                }
                hostMetaSortS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tSort0).count();
            }
            tLoop1 = std::chrono::steady_clock::now();

            if (!pendingUploads.empty()) {
                auto tMP0 = std::chrono::steady_clock::now();
                megaPackKeys.resize(totalPackWords);
                megaPackVals.resize(totalPackWords);
                uploadChunks.clear();
                uploadDescs.resize(pendingUploads.size());
                for (size_t ui = 0; ui < pendingUploads.size(); ++ui) {
                    const PendingMegaUpload& pm = pendingUploads[ui];
                    uploadDescs[ui] = FidesGpuMegaUpload{
                        pm.slot,
                        megaPackKeys.data() + pm.packOff,
                        megaPackVals.data() + pm.packOff,
                        pm.capH
                    };
                    for (U32 j0 = 0u; j0 < pm.capH; j0 += 16384u) {
                        U32 j1 = std::min<U32>(pm.capH, j0 + 16384u);
                        uploadChunks.push_back(MegaPackChunk{ui, j0, j1});
                    }
                }
                EXEC.parallelFor(uploadChunks.size(), 1u, [&](size_t cLo, size_t cHi) {
                    for (size_t ci = cLo; ci < cHi; ++ci) {
                        const MegaPackChunk& ck = uploadChunks[ci];
                        const PendingMegaUpload& pm = pendingUploads[ck.upIdx];
                        const ScopeSlot* slPtr = G.netScopes[pm.netId].slots.data();
                        U32* dstK = megaPackKeys.data() + pm.packOff;
                        I32* dstV = megaPackVals.data() + pm.packOff;
                        for (U32 j = ck.j0; j < ck.j1; ++j) {
                            dstK[j] = slPtr[j].key;
                            dstV[j] = slPtr[j].count;
                        }
                    }
                });
                auto tMG0 = std::chrono::steady_clock::now();
                megaPackS += std::chrono::duration<double>(tMG0 - tMP0).count();
                bool upOk = fidesGpuUploadMegaScopesBatch(uploadDescs.data(), static_cast<U32>(uploadDescs.size()));
                megaGpuS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tMG0).count();
                megaUploadCount += static_cast<I64>(pendingUploads.size());
                if (upOk) {
                    for (const PendingMegaUpload& pm : pendingUploads) {
                        NetScopeData& sc = G.netScopes[pm.netId];
                        G.megaCache[pm.slot] = {pm.netId, sc.layoutVersion, sc.mask};
                        sc.megaSlot = pm.slot;
                        sc.megaDirtyIdx.clear();
                        sc.megaSyncTracked = true;
                    }
                } else {
                    for (const PendingMegaUpload& pm : pendingUploads) {
                        waveNetFailed[pm.wi] = 1u;
                        G.megaCache[pm.slot] = {UINT32_MAX, 0u, 0u};
                        if (G.netScopes[pm.netId].megaSlot == pm.slot) G.netScopes[pm.netId].megaSlot = -1;
                    }
                }
            }

            auto tLive0 = std::chrono::steady_clock::now();
            hostMetaUpWrapS += std::chrono::duration<double>(tLive0 - tLoop1).count();
            const size_t P = pickedActIdx.size();
            hostMetaPTot += static_cast<I64>(P);
            piLive.assign(P, 1u);
            if (routeIter > 1 && P > 0u) {
                EXEC.parallelFor(P, 128u, [&](size_t lo, size_t hi) {
                    for (size_t pi = lo; pi < hi; ++pi) {
                        if (!shouldRouteLive(static_cast<U32>(activeConns[pickedActIdx[pi]]))) {
                            piLive[pi] = 0u;
                        }
                    }
                });
            }

            auto tBuild0 = std::chrono::steady_clock::now();
            hostMetaLiveS += std::chrono::duration<double>(tBuild0 - tLive0).count();
            waveActIdx.clear();
            wasRipped.clear();
            itemDev.clear();
            itemLk.clear();
            waveOldOff.clear();
            waveOldLen.clear();
            waveActIdx.reserve(P);
            wasRipped.reserve(P);
            itemDev.reserve(P);
            itemLk.reserve(P);
            waveOldOff.reserve(P);
            waveOldLen.reserve(P);
            for (size_t pi = 0; pi < P; ++pi) {
                if (!piLive[pi]) {
                    ++skippedLive;
                    continue;
                }
                I32 aIdx = pickedActIdx[pi];
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                U32 netId = G.conns[cIdx].netId;
                I32 dIdx = netWaveDescIdx[netId];
                if (waveNetFailed[dIdx]) {
                    if (pickedRipped[pi]) restoreRipped(cIdx);
                    cpuFallbackItems.push_back(aIdx);
                    ++gpuFallbacks;
                    continue;
                }
                int d = netAssignedDev[dIdx];
                U32 lk = devB[d]++;
                U32 oldLen = (!pickedRipped[pi] && G.connRouted[cIdx] && G.connPaths[cIdx].size() > 2u)
                        ? static_cast<U32>(G.connPaths[cIdx].size() - 2u) : 0u;
                waveActIdx.push_back(aIdx);
                wasRipped.push_back(pickedRipped[pi]);
                itemDev.push_back(d);
                itemLk.push_back(lk);
                waveOldOff.push_back(devOldCursor[d]);
                waveOldLen.push_back(oldLen);
                devOldCursor[d] += oldLen;
            }
            auto tMeta1 = std::chrono::steady_clock::now();
            hostMetaBuildS += std::chrono::duration<double>(tMeta1 - tBuild0).count();
            hostMetaS += std::chrono::duration<double>(tMeta1 - tMeta0).count();

            if (waveActIdx.empty()) continue;

            auto tPack0 = std::chrono::steady_clock::now();
            U32 B = static_cast<U32>(waveActIdx.size());
            bool acqOk = true;
            for (int d = 0; d < numDevs; ++d) {
                if (devB[d] > 0u) {
                    if (!fidesGpuAcquireWaveBufsDev(d, devB[d], devSameCursor[d], outPathsWords, devOldCursor[d],
                                                    0u, &devBufs[d])) {
                        acqOk = false;
                        break;
                    }
                }
            }
            if (!acqOk) {
                hostPackS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPack0).count();
                for (U32 k = 0; k < B; ++k) {
                    I32 aIdx = waveActIdx[k];
                    if (wasRipped[k]) restoreRipped(static_cast<U32>(activeConns[aIdx]));
                    cpuFallbackItems.push_back(aIdx);
                    ++gpuFallbacks;
                }
                continue;
            }

            // Phase 2B (35-core parallel): populate mode 1/2 sameNet tables directly into each GPU's pinned memory
            EXEC.parallelFor(W, 64u, [&](size_t lo, size_t hi) {
                for (size_t wi = lo; wi < hi; ++wi) {
                    if (!waveNetFailed[wi]) {
                        int d = netAssignedDev[wi];
                        populateNetSameDescBuf(waveUniqueNets[wi], waveNetDescs[wi],
                                               devBufs[d].sameKeys, devBufs[d].sameVals);
                    }
                }
            });

            // Phase 2C (35-core parallel): pack queries + sort oldNodes directly into each GPU's pinned memory
            EXEC.parallelFor(B, 128u, [&](size_t lo, size_t hi) {
                for (size_t k = lo; k < hi; ++k) {
                    I32 aIdx = waveActIdx[k];
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    I32 dIdx = netWaveDescIdx[G.conns[cIdx].netId];
                    int d = itemDev[k];
                    U32 lk = itemLk[k];
                    fillGpuQueryDirect(aIdx, curBudget, 0u, pathCap, waveNetDescs[dIdx],
                                       waveOldOff[k], waveOldLen[k], devBufs[d].oldNodes, devBufs[d].queries[lk]);
                }
            });
            hostPackS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPack0).count();

            // Phase 3: Broadcast megaPatches + dirty occ deltas and launch concurrent Multi-GPU kernels
            // Overlap next wave's Phase 1 (readyHeap Pick) + Phase 2A-LPT with 4-GPU kernel execution!
            auto tGpu0 = std::chrono::steady_clock::now();
            if (!megaPatches.empty()) {
                fidesGpuApplyMegaPatches(megaPatches.data(), static_cast<U32>(megaPatches.size()));
            }
            flushGpuOccDeltas();

            FidesGpuWaveParams wp{};
            wp.shareWeight = shareWeight;
            wp.rnodeCostWeight = rnodeCostWeight;
            wp.rnodeWLWeight = rnodeWLWeight;
            wp.estWlWeight = estWlWeight;
            wp.pccLen = static_cast<U32>(G.pcc.size());
            if (passTier == 1) {
                wp.bucketShift = 18u;
                wp.popBatch = 32u;
            } else {
                wp.bucketShift = 17u;
                wp.popBatch = 256u;
            }

            struct MultiLaunchCtx {
                int passTier;
                int tierB1Mode;
                FidesGpuWaveParams wp;
                const U32* devB;
                const U32* devSameCursor;
                U32 outPathsWords;
                const U32* devOldCursor;
                U32* devUsedWords;
                bool* devOk;
            } mctx{passTier, tierB1Mode, wp, devB, devSameCursor, outPathsWords, devOldCursor, devUsedWords, devOk};

            bool launchedPrePick = false;
            I64 preSkippedLive = 0;
            std::vector<I32> preFallbackItems;
            std::thread prePickThread;
            if (passTier == 1 && remainingItems > targetWave * 2u && liveHeapNets > 0) {
                launchedPrePick = true;
                prePickThread = std::thread([&]() {
                    pickAndLpt(targetWave, preSkippedLive, preFallbackItems);
                });
            }

            bool launchedCpuOverlap = false;
            std::thread cpuOverlapThread;
            if (passTier == 3 && tierB1Mode == 0 && !cpuOverlapDone
                    && cpuOverlapItems != nullptr && !cpuOverlapItems->empty()) {
                launchedCpuOverlap = true;
                if (++G.waveCommitEpoch == 0u) {
                    std::memset(G.waveCommitKey.data(), 0, G.waveCommitKey.size() * sizeof(U64));
                    G.waveCommitEpoch = 1u;
                }
                G.gpuStampWaveEpoch = G.waveCommitEpoch;
                cpuOverlapThread = std::thread([&]() {
                    runDynamicPass(*cpuOverlapItems, curBudget, passTier);
                });
            }

            fidesGpuRunParallelDevs([](int d, void* u) {
                auto* c = static_cast<MultiLaunchCtx*>(u);
                if (c->devB[d] == 0u) {
                    c->devUsedWords[d] = 0u;
                    c->devOk[d] = true;
                    return;
                }
                if (c->passTier == 1) {
                    c->devOk[d] = fidesGpuRunTierADev(d, c->wp, c->devB[d], c->devSameCursor[d],
                                                      c->outPathsWords, c->devOldCursor[d], &c->devUsedWords[d]);
                } else {
                    c->devOk[d] = fidesGpuRunTierB1WideDev(d, c->wp, c->tierB1Mode, c->devB[d], c->devSameCursor[d],
                                                           c->outPathsWords, c->devOldCursor[d], &c->devUsedWords[d], nullptr);
                }
            }, &mctx);
            gpuNs += std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now() - tGpu0).count();
            ++gpuWaves;

            if (launchedCpuOverlap && cpuOverlapThread.joinable()) {
                cpuOverlapThread.join();
                G.gpuStampWaveEpoch = 0u;
                cpuOverlapDone = true;
            }

            if (launchedPrePick && prePickThread.joinable()) {
                prePickThread.join();
                skippedLive += preSkippedLive;
                if (!preFallbackItems.empty()) {
                    cpuFallbackItems.insert(cpuFallbackItems.end(), preFallbackItems.begin(), preFallbackItems.end());
                }
                hasPrePicked = !pickedActIdx.empty();
            }

            bool anyGpuOk = false;
            for (int d = 0; d < numDevs; ++d) {
                if (devB[d] > 0u && devOk[d]) { anyGpuOk = true; break; }
            }
            if (!anyGpuOk) {
                for (U32 k = 0; k < B; ++k) {
                    I32 aIdx = waveActIdx[k];
                    if (wasRipped[k]) restoreRipped(static_cast<U32>(activeConns[aIdx]));
                    cpuFallbackItems.push_back(aIdx);
                    ++gpuFallbacks;
                }
                continue;
            }

            // Phase 4A: 35-core parallel path validation + atomic U64 waveCommitKey claim
            auto tP4A0 = std::chrono::steady_clock::now();
            if (!launchedCpuOverlap) {
                if (++G.waveCommitEpoch == 0u) {
                    std::memset(G.waveCommitKey.data(), 0, G.waveCommitKey.size() * sizeof(U64));
                    G.waveCommitEpoch = 1u;
                }
            }
            const U32 curCommitEpoch = G.waveCommitEpoch;

            itemAct.assign(B, PA_WIN);
            itemSawSame.assign(B, 0u);
            EXEC.parallelFor(B, 128u, [&](size_t lo, size_t hi) {
                for (size_t k = lo; k < hi; ++k) {
                    int d = itemDev[k];
                    if (!devOk[d]) { itemAct[k] = PA_BAD_FALLBACK; continue; }
                    const FidesGpuResult& res = devBufs[d].results[itemLk[k]];
                    if (res.status == FG_STATUS_NO_PATH) { itemAct[k] = PA_NO_PATH; continue; }
                    if (res.status == FG_STATUS_CROSSED_BUDGET) { itemAct[k] = PA_CROSSED_BUDGET; continue; }
                    if (res.status == FG_STATUS_HASH_OVERFLOW) { itemAct[k] = PA_HASH_OVERFLOW; continue; }
                    if (res.status < 2 || res.pathOffset + static_cast<U32>(res.status) > devUsedWords[d]) {
                        itemAct[k] = PA_BAD_FALLBACK;
                        continue;
                    }
                    I32 aIdx = waveActIdx[k];
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    const U32 len = static_cast<U32>(res.status);
                    const U32* pPtr = devBufs[d].outPaths + res.pathOffset;
                    if (pPtr[0] != G.conns[cIdx].sink || pPtr[len - 1u] != G.conns[cIdx].src) {
                        itemAct[k] = PA_BAD_FALLBACK;
                        continue;
                    }

                    U32 netId = G.conns[cIdx].netId;
                    const U64 targetKey = (static_cast<U64>(curCommitEpoch) << 32) | static_cast<U64>(netId);
                    bool conflict = false;
                    bool sawSameNetInWave = false;
                    for (U32 i = 1u; i + 1u < len; ++i) {
                        U64 cur = __atomic_load_n(&G.waveCommitKey[pPtr[i]], __ATOMIC_RELAXED);
                        if ((cur >> 32) == curCommitEpoch) {
                            if (static_cast<U32>(cur) != netId) { conflict = true; break; }
                            sawSameNetInWave = true;
                        }
                    }
                    if (!conflict) {
                        for (U32 i = 1u; i + 1u < len; ++i) {
                            U32 nid = pPtr[i];
                            U64 expected = __atomic_load_n(&G.waveCommitKey[nid], __ATOMIC_RELAXED);
                            while (true) {
                                if ((expected >> 32) == curCommitEpoch) {
                                    if (static_cast<U32>(expected) != netId) { conflict = true; break; }
                                    sawSameNetInWave = true;
                                    break;
                                }
                                if (__atomic_compare_exchange_n(&G.waveCommitKey[nid], &expected, targetKey,
                                                                false, std::memory_order_acq_rel, std::memory_order_relaxed)) {
                                    break;
                                }
                            }
                            if (conflict) break;
                        }
                    }
                    if (conflict) {
                        itemAct[k] = PA_CONFLICT;
                    } else {
                        itemAct[k] = PA_WIN;
                        itemSawSame[k] = sawSameNetInWave ? 1u : 0u;
                    }
                }
            });

            nextCommitInNet.assign(B, -1);
            waveCommitNets.clear();
            for (U32 k = 0; k < B; ++k) {
                I32 aIdx = waveActIdx[k];
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                int d = itemDev[k];
                if (!devOk[d]) {
                    if (wasRipped[k]) restoreRipped(cIdx);
                    cpuFallbackItems.push_back(aIdx);
                    ++gpuFallbacks;
                    continue;
                }
                const FidesGpuResult& res = devBufs[d].results[itemLk[k]];
                totalPops += res.pops;
                gpuPops += res.pops;
                G.lastPops[cIdx] = res.pops;

                switch (itemAct[k]) {
                    case PA_NO_PATH:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (bumpWiden(aIdx, 1)) retries.push_back(aIdx);
                        else ++unroutableCount;
                        break;
                    case PA_CROSSED_BUDGET:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (passTier == 1) {
                            baseHeavyQueue.push_back(aIdx);
                            ++baseHeavyCount;
                        } else if (passTier == 2) {
                            bumpWiden(aIdx, 3);
                            relaxedQueue.push_back(aIdx);
                            ++relaxedHeavyCount;
                        } else {
                            cpuFallbackItems.push_back(aIdx);
                            ++gpuFallbacks;
                        }
                        break;
                    case PA_HASH_OVERFLOW:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (tierB1Mode == 0) ++gpuOverflows;
                        if (passTier == 1) {
                            baseHeavyQueue.push_back(aIdx);
                            ++baseHeavyCount;
                        } else if (passTier == 2) {
                            relaxedQueue.push_back(aIdx);
                            ++relaxedHeavyCount;
                        } else {
                            if (tierB1Mode < 2) {
                                wideRetryItems.push_back(aIdx);
                            } else {
                                cpuFallbackItems.push_back(aIdx);
                                ++gpuFallbacks;
                            }
                        }
                        break;
                    case PA_BAD_FALLBACK:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        cpuFallbackItems.push_back(aIdx);
                        ++gpuFallbacks;
                        break;
                    case PA_CONFLICT:
                        ++conflictRetries;
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (retryStreak[aIdx] < 250) ++retryStreak[aIdx];
                        if (retryStreak[aIdx] >= 4) cpuFallbackItems.push_back(aIdx);
                        else retries.push_back(aIdx);
                        break;
                    case PA_WIN: {
                        if (itemSawSame[k]) ++sameNetOk;
                        U32 netId = G.conns[cIdx].netId;
                        if (commitNetHead[netId] < 0) {
                            waveCommitNets.push_back(netId);
                            commitNetHead[netId] = static_cast<I32>(k);
                            commitNetTail[netId] = static_cast<I32>(k);
                        } else {
                            nextCommitInNet[commitNetTail[netId]] = static_cast<I32>(k);
                            commitNetTail[netId] = static_cast<I32>(k);
                        }
                        ++routedCount;
                        ++parallelCommits;
                        ++gpuCommits;
                        if (tierB1Mode > 0) ++gpuWideOk;
                        break;
                    }
                }
            }
            hostP4AS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tP4A0).count();

            // Phase 4B (32-core net-parallel scope update) + Phase 4C (32-shard node-disjoint parallel occ update)
            const size_t numCN = waveCommitNets.size();
            if (numCN > 0u) {
                auto tP4B0 = std::chrono::steady_clock::now();
                const size_t numSlices = std::min<size_t>(numCN, P4_SLICES);
                EXEC.parallelFor(numSlices, 1u, [&](size_t sLo, size_t sHi) {
                    for (size_t s = sLo; s < sHi; ++s) {
                        for (size_t b = 0; b < P4_SHARDS; ++b) shardSliceDeltas[b * P4_SLICES + s].clear();
                        auto pushShardDelta = [&](U32 id, I32 d) {
                            size_t b = (id >> 4) & (P4_SHARDS - 1u);
                            shardSliceDeltas[b * P4_SLICES + s].push_back({id, d});
                        };
                        size_t n0 = (s * numCN) / numSlices;
                        size_t n1 = ((s + 1u) * numCN) / numSlices;
                        for (size_t ni = n0; ni < n1; ++ni) {
                            U32 netId = waveCommitNets[ni];
                            NetScopeData& sc = G.netScopes[netId];
                            for (I32 k = commitNetHead[netId]; k >= 0; k = nextCommitInNet[k]) {
                                U32 cIdx = static_cast<U32>(activeConns[waveActIdx[k]]);
                                int d = itemDev[k];
                                const FidesGpuResult& res = devBufs[d].results[itemLk[k]];
                                const U32 len = static_cast<U32>(res.status);
                                const U32* pPtr = devBufs[d].outPaths + res.pathOffset;
                                if (!wasRipped[k] && G.connRouted[cIdx]) {
                                    const auto& oldPath = G.connPaths[cIdx];
                                    for (size_t i = 1; i + 1 < oldPath.size(); ++i) {
                                        addToScopeDeferredFn(sc, oldPath[i], -1, pushShardDelta);
                                    }
                                }
                                ensureScopeCapacitySingleThread(sc, len);
                                for (U32 i = 1; i + 1 < len; ++i) {
                                    addToScopeDeferredFn(sc, pPtr[i], +1, pushShardDelta);
                                }
                                G.connPaths[cIdx].assign(pPtr, pPtr + len);
                                G.connRouted[cIdx] = 1;
                            }
                            commitNetHead[netId] = -1;
                            commitNetTail[netId] = -1;
                        }
                    }
                });
                auto tP4B1 = std::chrono::steady_clock::now();
                hostP4BS += std::chrono::duration<double>(tP4B1 - tP4B0).count();

                const bool trackDirty = G.gpuEnabled;
                const U32 curDirtyEpoch = G.gpuDirtyOccEpoch;
                EXEC.parallelFor(P4_SHARDS, 1u, [&](size_t bLo, size_t bHi) {
                    for (size_t b = bLo; b < bHi; ++b) {
                        auto& dOut = shardDirtyIds[b];
                        auto& ovOut = shardOvEvents[b];
                        dOut.clear();
                        ovOut.clear();
                        for (size_t s = 0; s < numSlices; ++s) {
                            auto& vec = shardSliceDeltas[b * P4_SLICES + s];
                            for (const auto& kv : vec) {
                                const U32 id = kv.id;
                                const I32 before = G.dyn[id].occ;
                                const I32 after = before + kv.delta;
                                __atomic_store_n(&G.dyn[id].occ, after, __ATOMIC_RELEASE);
                                if (trackDirty && G.gpuDirtyOccStamp[id] != curDirtyEpoch) {
                                    G.gpuDirtyOccStamp[id] = curDirtyEpoch;
                                    dOut.push_back(id);
                                }
                                if ((before <= cap && after > cap) || (before > cap && after <= cap)) {
                                    ovOut.push_back({id, before, after});
                                }
                            }
                            vec.clear();
                        }
                    }
                });
                size_t addDirty = 0u;
                for (size_t b = 0; b < P4_SHARDS; ++b) addDirty += shardDirtyIds[b].size();
                if (addDirty > 0u) {
                    G.gpuDirtyOccIds.reserve(G.gpuDirtyOccIds.size() + addDirty);
                    for (size_t b = 0; b < P4_SHARDS; ++b) {
                        G.gpuDirtyOccIds.insert(G.gpuDirtyOccIds.end(), shardDirtyIds[b].begin(), shardDirtyIds[b].end());
                        shardDirtyIds[b].clear();
                    }
                }
                for (size_t b = 0; b < P4_SHARDS; ++b) {
                    for (const auto& ev : shardOvEvents[b]) {
                        const U32 id = ev.id;
                        if (ev.before <= cap && ev.after > cap) {
                            if (G.overusedPos[id] < 0) {
                                G.overusedPos[id] = static_cast<I32>(G.overusedList.size());
                                G.overusedList.push_back(id);
                            }
                        } else if (ev.before > cap && ev.after <= cap) {
                            I32 pos = G.overusedPos[id];
                            if (pos >= 0) {
                                U32 backId = G.overusedList.back();
                                if (static_cast<size_t>(pos) + 1u < G.overusedList.size()) {
                                    G.overusedList[pos] = backId;
                                    G.overusedPos[backId] = pos;
                                }
                                G.overusedList.pop_back();
                                G.overusedPos[id] = -1;
                            }
                        }
                    }
                    shardOvEvents[b].clear();
                }
                hostP4CS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tP4B1).count();
            }

            if (passTier == 1 && activeCount >= 100000) {
                auto now = std::chrono::steady_clock::now();
                double sinceLast = std::chrono::duration<double>(now - lastLogTime).count();
                if (routedCount >= nextLogRouted || sinceLast >= 30.0) {
                    while (nextLogRouted <= routedCount) nextLogRouted += 500000;
                    lastLogTime = now;
                    double elapsedS = std::chrono::duration<double>(now - tStarted).count();
                    fprintf(stdout,
                            "F4_V37_WAVE routed=%lld/%d skip=%lld gpu=%lld cpu=%lld retries=%lld same_ok=%lld overused=%zu pops=%lld gpu_pops=%lld gpu_ov=%lld gpu_fb=%lld gpu_s=%.2f t=%.1fs\n",
                            static_cast<long long>(routedCount), activeCount,
                            static_cast<long long>(skippedLive),
                            static_cast<long long>(gpuCommits),
                            static_cast<long long>(routedCount - gpuCommits),
                            static_cast<long long>(conflictRetries),
                            static_cast<long long>(sameNetOk),
                            G.overusedList.size(), static_cast<long long>(totalPops),
                            static_cast<long long>(gpuPops),
                            static_cast<long long>(gpuOverflows),
                            static_cast<long long>(gpuFallbacks),
                            gpuNs / 1e9, elapsedS);
                    fflush(stdout);
                }
            }
        }
            if (wideRetryItems.empty() || passTier != 3) break;
            const size_t minWideItems = (G.gpuCount >= 3) ? 16u : ((tierB1Mode == 0) ? 6u : 8u);
            if (tierB1Mode >= 2 || wideRetryItems.size() < minWideItems) {
                cpuFallbackItems.insert(cpuFallbackItems.end(), wideRetryItems.begin(), wideRetryItems.end());
                gpuFallbacks += static_cast<I64>(wideRetryItems.size());
                wideRetryItems.clear();
                break;
            }
            curItems.swap(wideRetryItems);
            wideRetryItems.clear();
        }

        if (!cpuOverlapDone && cpuOverlapItems != nullptr && !cpuOverlapItems->empty()) {
            G.gpuStampWaveEpoch = 0u;
            runDynamicPass(*cpuOverlapItems, curBudget, passTier);
        }

        // Resolve any GPU fallbacks / tiny tail items cleanly on 35-core CPU in true LPT order!
        if (!cpuFallbackItems.empty()) {
            if (passTier >= 2 && cpuFallbackItems.size() > 1u) {
                std::sort(cpuFallbackItems.begin(), cpuFallbackItems.end(), [&](I32 a, I32 b) {
                    return G.lastPops[activeConns[a]] > G.lastPops[activeConns[b]];
                });
            }
            runDynamicPass(cpuFallbackItems, curBudget, passTier);
        }
    };

    // v37.1 Heterogeneous Split (Route B + proven v36 Tier-B1 8M -> 35-core CPU HOT fast fallback):
    // - Pass 1 (R1 & R2 -> GPU Tier-A tw=8192; R3+ -> 35-core CPU HOT)
    // - Pass 2 (>=256 items -> GPU Tier-B1 8M shift=17 pop=256)
    // - Pass 3 (>=256 items -> GPU Tier-B1 8M tw=2048 + concurrent CPU overlap for >=6.5M items)
    double p1S = 0.0, p2S = 0.0, p3S = 0.0, megaS = 0.0;
    size_t p2N = 0, p3N = 0, megaN = 0;

    if ((routeIter >= 45 && activeCount <= 64) || (routeIter >= 50 && activeCount <= 96)) {
        // v52 Bounded Reactive Tail Solver (preserves v49 escNbr(96) + 6M/pad=96 convergence,
        // adds tailFailStreak skip of redundant 2M+4M and bounded 8M/pad=128 before R78+ fallback):
        auto tTail0 = std::chrono::steady_clock::now();
        std::vector<U32> clusterConns(activeCount);
        for (int i = 0; i < activeCount; ++i) clusterConns[i] = static_cast<U32>(activeConns[i]);
        const bool escNbr = (routeIter >= 55 && G.lastTailFbCount > 0) || (routeIter >= 78);
        const bool widenNbr = escNbr || (routeIter >= 58 && G.overusedList.size() <= 12u && activeCount <= 24);

        if (((routeIter >= 54 && activeCount <= 24) || (routeIter >= 56 && activeCount <= 64 && escNbr))
                && !G.overusedList.empty()) {
            struct OvCoord { I32 x; I32 y; };
            std::vector<OvCoord> ovCoords;
            ovCoords.reserve(G.overusedList.size());
            for (U32 id : G.overusedList) {
                ovCoords.push_back(OvCoord{static_cast<I32>(G.geom[id].endTileX), static_cast<I32>(G.geom[id].tileY)});
            }
            const U32 totalConns = static_cast<U32>(G.conns.size());
            std::vector<uint8_t> inAct(totalConns, 0u);
            for (U32 ci : clusterConns) inAct[ci] = 1u;
            int chunks = std::max(1, EXEC.numWorkers + 1);
            U32 step = (totalConns + chunks - 1u) / chunks;
            const I32 maxNbrNet = widenNbr ? 128 : 64;
            const I64 maxNbrPops = widenNbr ? 120000 : 50000;
            const I32 nbrDx = widenNbr ? 4 : 2;
            const I32 nbrDy = widenNbr ? 8 : 4;
            const size_t nbrLim = widenNbr ? 128u : 48u;
            struct NbrCand { I64 score; U32 connIdx; };
            std::vector<std::vector<NbrCand>> chunkCands(chunks);
            BoundaryPool::runChunks(chunks, [&](int c) {
                U32 lo = static_cast<U32>(c) * step;
                U32 hi = std::min(totalConns, lo + step);
                auto& out = chunkCands[c];
                for (U32 ci = lo; ci < hi; ++ci) {
                    if (inAct[ci] || !G.connRouted[ci]) continue;
                    const ConnData& cd = G.conns[ci];
                    if (!(cd.flags & 1u) || cd.doubleHpwl <= 0 || cd.netSize > maxNbrNet || G.lastPops[ci] > maxNbrPops) continue;
                    bool boxNear = false;
                    for (const OvCoord& oc : ovCoords) {
                        if (oc.x >= cd.origXmin - nbrDx && oc.x <= cd.origXmax + nbrDx
                                && oc.y >= cd.origYmin - nbrDy && oc.y <= cd.origYmax + nbrDy) {
                            boxNear = true;
                            break;
                        }
                    }
                    if (!boxNear) continue;
                    I32 bestDist = 1000000;
                    const auto& p = G.connPaths[ci];
                    for (U32 nid : p) {
                        I32 nx = static_cast<I32>(G.geom[nid].endTileX);
                        I32 ny = static_cast<I32>(G.geom[nid].tileY);
                        for (const OvCoord& oc : ovCoords) {
                            if (fides_absInt(nx - oc.x) <= nbrDx && fides_absInt(ny - oc.y) <= nbrDy) {
                                I32 d = fides_absInt(nx - oc.x) * 2 + fides_absInt(ny - oc.y);
                                if (d < bestDist) bestDist = d;
                            }
                        }
                    }
                    if (bestDist < 1000000) {
                        out.push_back(NbrCand{static_cast<I64>(bestDist) * 100000LL + G.lastPops[ci], ci});
                    }
                }
            });
            std::vector<NbrCand> allCands;
            for (const auto& v : chunkCands) allCands.insert(allCands.end(), v.begin(), v.end());
            std::sort(allCands.begin(), allCands.end(), [](const NbrCand& a, const NbrCand& b) {
                if (a.score != b.score) return a.score < b.score;
                return a.connIdx < b.connIdx;
            });
            size_t addLim = std::min<size_t>(nbrLim, allCands.size());
            for (size_t k = 0; k < addLim; ++k) {
                clusterConns.push_back(allCands[k].connIdx);
            }
        }

        const int C = static_cast<int>(clusterConns.size());
        p3N = static_cast<size_t>(C);
        std::vector<std::vector<U32>> savedPaths(C);
        std::vector<uint8_t> savedRouted(C, 0u);
        std::vector<float> itemMaxHist(C, 0.0f);
        std::vector<I32> tailOrder(C);
        for (int i = 0; i < C; ++i) {
            tailOrder[i] = i;
            U32 connIdx = clusterConns[i];
            savedRouted[i] = G.connRouted[connIdx];
            if (savedRouted[i]) {
                savedPaths[i] = G.connPaths[connIdx];
                float mh = 0.0f;
                const auto& p = savedPaths[i];
                for (size_t j = 1; j + 1 < p.size(); ++j) {
                    mh = std::max(mh, G.dyn[p[j]].hist);
                }
                itemMaxHist[i] = mh;
                NetScopeData& sc = G.netScopes[G.conns[connIdx].netId];
                for (size_t j = 1; j + 1 < p.size(); ++j) {
                    addToScope(sc, p[j], -1);
                }
                G.connRouted[connIdx] = 0;
            }
        }
        const bool flipTie = (routeIter >= 58 && (routeIter & 1) != 0);
        std::sort(tailOrder.begin(), tailOrder.end(), [&](I32 a, I32 b) {
            bool aAct = (a < activeCount), bAct = (b < activeCount);
            if (aAct != bAct) return aAct; // route conflicting active connections BEFORE innocent neighbors
            if (aAct && flipTie) {
                U32 ha = clusterConns[a] * 2654435761u ^ static_cast<U32>(routeIter * 1140071481u);
                U32 hb = clusterConns[b] * 2654435761u ^ static_cast<U32>(routeIter * 1140071481u);
                if (ha != hb) return ha > hb;
            }
            if (itemMaxHist[a] != itemMaxHist[b]) return itemMaxHist[a] > itemMaxHist[b];
            I64 pa = G.lastPops[clusterConns[a]];
            I64 pb = G.lastPops[clusterConns[b]];
            if (pa != pb) return pa > pb;
            return clusterConns[a] < clusterConns[b];
        });
        std::vector<U32> singlePath;
        int unboundedUsedThisRound = 0;
        const size_t overNow = G.overusedList.size();
        const bool microTailRegime = (routeIter >= 55 && overNow <= 1u && activeCount <= 2);
        const bool microStalled = microTailRegime && (G.lastTailFbCount > 0 || G.microStuckRounds >= 1);
        const int maxUnboundedThisRound = (routeIter >= 78) ? 8 : (microStalled ? 2 : 1);
        for (I32 idxPos : tailOrder) {
            U32 connIdx = clusterConns[idxPos];
            const bool isAct = (idxPos < activeCount);
            const int rx0 = isAct ? bx0[idxPos] : 0;
            const int ry0 = isAct ? by0[idxPos] : 0;
            const int rx1 = isAct ? bx1[idxPos] : 0;
            const int ry1 = isAct ? by1[idxPos] : 0;
            const bool rUse = isAct ? (buse[idxPos] != 0) : false;
            const int pad0 = (routeIter >= 55) ? (isAct ? 24 : 12) : 0;
            const uint8_t fs = G.tailFailStreak[connIdx];
            I32 rc = 0;
            if (!isAct || (fs == 0u && !(microStalled && G.lastTailFbCount > 0))) {
                I64 pops = 0;
                rc = runProbe(connIdx, rx0, ry0, rx1, ry1, rUse,
                              shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                              2000000, singlePath, pops, pad0);
                totalPops += pops;
                G.lastPops[connIdx] = pops;
                if (rc <= 0 && !(isAct && microStalled)) {
                    I64 pops2 = 0;
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  4000000, singlePath, pops2, isAct ? 48 : 24);
                    totalPops += pops2;
                    G.lastPops[connIdx] += pops2;
                }
            }
            if (isAct && rc <= 0 && (escNbr || fs >= 1u || microStalled || (routeIter >= 62 && activeCount <= 24))) {
                I64 pops3 = 0;
                if ((routeIter >= 74 || microStalled || (routeIter >= 60 && fs >= 3u) || (routeIter >= 62 && activeCount <= 24))
                        && unboundedUsedThisRound < maxUnboundedThisRound) {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  0, singlePath, pops3, -1);
                    if (pops3 > 4000000LL) ++unboundedUsedThisRound;
                } else if (routeIter >= 57 && fs >= 2u) {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  16000000, singlePath, pops3, -1);
                } else if (fs >= 2u) {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  8000000, singlePath, pops3, 128);
                } else {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  6000000, singlePath, pops3, 96);
                }
                totalPops += pops3;
                if (fs == 0u) G.lastPops[connIdx] += pops3;
                else G.lastPops[connIdx] = pops3;
            }
            if (rc > 0) {
                G.tailFailStreak[connIdx] = 0u;
                U32 netId = G.conns[connIdx].netId;
                NetScopeData& sc = G.netScopes[netId];
                ensureScopeCapacitySingleThread(sc, singlePath.size());
                for (size_t j = 1; j + 1 < singlePath.size(); ++j) {
                    addToScope(sc, singlePath[j], +1);
                }
                commitStampedPath(netId, singlePath);
                G.connPaths[connIdx].swap(singlePath);
                G.connRouted[connIdx] = 1;
                ++routedCount;
                ++serialCommits;
            } else {
                if (isAct && G.tailFailStreak[connIdx] < 10u) ++G.tailFailStreak[connIdx];
                if (savedRouted[idxPos]) {
                    U32 netId = G.conns[connIdx].netId;
                    NetScopeData& sc = G.netScopes[netId];
                    const auto& sp = savedPaths[idxPos];
                    ensureScopeCapacitySingleThread(sc, sp.size());
                    for (size_t j = 1; j + 1 < sp.size(); ++j) {
                        addToScope(sc, sp[j], +1);
                    }
                    G.connPaths[connIdx].swap(savedPaths[idxPos]);
                    G.connRouted[connIdx] = 1;
                }
                if (isAct) ++fallbackCount;
            }
        }
        G.lastTailFbCount = (routeIter >= 54) ? fallbackCount : 0;
        p3S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tTail0).count();
    } else {
    G.lastTailFbCount = 0;
    std::vector<I32> cpuFastItems;
    std::vector<I32> gpuPass1Items;
    cpuFastItems.reserve(activeCount);
    gpuPass1Items.reserve(activeCount);

    for (int i = 0; i < activeCount; ++i) {
        U32 connIdx = static_cast<U32>(activeConns[i]);
        I64 lp = G.lastPops[connIdx];
        uint8_t wl = G.connWiden[connIdx];
        const ConnData& cd = G.conns[connIdx];
        if (wavePopCeiling > 0 && (lp > BUDGET_BASE_HEAVY || (wl >= 3 && lp > 98304))) {
            relaxedQueue.push_back(i);
            ++relaxedHeavyCount;
        } else if (wavePopCeiling > 0 && (lp > 36000 || (wl >= 1 && lp > 16384))) {
            baseHeavyQueue.push_back(i);
            ++baseHeavyCount;
        } else if (!G.gpuEnabled || routeIter > 2) {
            cpuFastItems.push_back(i);
        } else if (routeIter == 1 || activeCount > 1024) {
            gpuPass1Items.push_back(i);
        } else {
            if (lp > 0 && lp < 200 && wl == 0 && cd.doubleHpwl <= 12) cpuFastItems.push_back(i);
            else gpuPass1Items.push_back(i);
        }
    }

    auto tP1_0 = std::chrono::steady_clock::now();
    if (!cpuFastItems.empty()) {
        runDynamicPass(cpuFastItems, wavePopCeiling, 1);
    }
    if (!gpuPass1Items.empty()) {
        if (G.gpuEnabled && routeIter <= 2) {
            runGpuPass(gpuPass1Items, wavePopCeiling, 1);
        } else {
            runDynamicPass(gpuPass1Items, wavePopCeiling, 1);
        }
    }
    p1S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tP1_0).count();

    if (!baseHeavyQueue.empty()) {
        std::sort(baseHeavyQueue.begin(), baseHeavyQueue.end());
        std::vector<I32> pass2Items;
        pass2Items.swap(baseHeavyQueue);
        p2N = pass2Items.size();
        auto tP2_0 = std::chrono::steady_clock::now();
        if (G.gpuEnabled && pass2Items.size() >= 256u) runGpuPass(pass2Items, BUDGET_BASE_HEAVY, 2);
        else runDynamicPass(pass2Items, BUDGET_BASE_HEAVY, 2);
        p2S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tP2_0).count();
    }

    if (!relaxedQueue.empty()) {
        std::sort(relaxedQueue.begin(), relaxedQueue.end(), [&](I32 a, I32 b) {
            return G.lastPops[activeConns[a]] > G.lastPops[activeConns[b]];
        });
        std::vector<I32> pass3Items;
        pass3Items.swap(relaxedQueue);
        p3N = pass3Items.size();
        auto tP3_0 = std::chrono::steady_clock::now();
        if (!pass3Items.empty()) {
            const size_t minGpuPass3 = (G.gpuCount >= 4) ? 512u : ((G.gpuCount >= 3) ? 384u : 320u);
            if (G.gpuEnabled && pass3Items.size() >= minGpuPass3) {
                std::vector<U32> netPass3Cnt(numNets, 0u);
                std::vector<U32> netUltraCnt(numNets, 0u);
                for (I32 aIdx : pass3Items) {
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    U32 nid = G.conns[cIdx].netId;
                    ++netPass3Cnt[nid];
                    if (G.lastPops[cIdx] >= 12000000LL) ++netUltraCnt[nid];
                }
                std::vector<uint8_t> bypassNet(numNets, 0u);
                bool anyBypass = false;
                for (I32 aIdx : pass3Items) {
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    U32 nid = G.conns[cIdx].netId;
                    if (netUltraCnt[nid] > 0u && (netPass3Cnt[nid] <= 16u || netUltraCnt[nid] >= 2u)) {
                        bypassNet[nid] = 1u;
                        anyBypass = true;
                    }
                }
                if (anyBypass) {
                    std::vector<I32> gpuPass3Items, cpuBypassPass3Items;
                    gpuPass3Items.reserve(pass3Items.size());
                    for (I32 aIdx : pass3Items) {
                        U32 netId = G.conns[activeConns[aIdx]].netId;
                        if (bypassNet[netId]) cpuBypassPass3Items.push_back(aIdx);
                        else gpuPass3Items.push_back(aIdx);
                    }
                    if (gpuPass3Items.size() >= minGpuPass3) {
                        runGpuPass(gpuPass3Items, 0, 3, &cpuBypassPass3Items);
                    } else {
                        runDynamicPass(pass3Items, 0, 3);
                    }
                } else {
                    runGpuPass(pass3Items, 0, 3, nullptr);
                }
            } else {
                runDynamicPass(pass3Items, 0, 3);
            }
        }
        p3S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tP3_0).count();
    }

    if (!megaQueue.empty()) {
        auto tMg0 = std::chrono::steady_clock::now();
        std::vector<I32> liveMega;
        liveMega.reserve(megaQueue.size());
        for (I32 aIdx : megaQueue) {
            if (!shouldRouteLive(static_cast<U32>(activeConns[aIdx]))) ++skippedLive;
            else liveMega.push_back(aIdx);
        }
        megaN = liveMega.size();
        if (!liveMega.empty()) {
            runDynamicPass(liveMega, 0, 3);
        }
        megaS = std::chrono::duration<double>(std::chrono::steady_clock::now() - tMg0).count();
    }
    }

    if (G.gpuEnabled) {
        fprintf(stdout,
                "F4_V37_PASS iter=%d p1_s=%.2f p2_n=%zu p2_s=%.2f p3_n=%zu p3_s=%.2f mega_n=%zu mega_s=%.2f\n",
                routeIter, p1S, p2N, p2S, p3N, p3S, megaN, megaS);
        fprintf(stdout,
                "F4_V39_HOST iter=%d pick_s=%.2f meta_s=%.2f pack_s=%.2f p4a_s=%.2f p4b_s=%.2f p4c_s=%.2f mega_up=%lld mega_pack_s=%.2f mega_gpu_s=%.2f\n",
                routeIter, hostPickS, hostMetaS, hostPackS, hostP4AS, hostP4BS, hostP4CS,
                static_cast<long long>(megaUploadCount), megaPackS, megaGpuS);
        fprintf(stdout,
                "F4_V41_META iter=%d loop_s=%.2f sort_s=%.2f upw_s=%.2f live_s=%.2f build_s=%.2f dirty_n=%lld patch_n=%lld w_tot=%lld p_tot=%lld\n",
                routeIter, hostMetaLoopS, hostMetaSortS, hostMetaUpWrapS, hostMetaLiveS, hostMetaBuildS,
                static_cast<long long>(hostMetaDirtyN), static_cast<long long>(hostMetaPatchN),
                static_cast<long long>(hostMetaWTot), static_cast<long long>(hostMetaPTot));
        fflush(stdout);
    }

    auto elapsedNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now() - tStarted).count();

    I64 totalExcess = 0;
    for (U32 id : G.overusedList) {
        I32 ov = G.dyn[id].occ - cap;
        if (ov > 0) totalExcess += ov;
    }

    if (G.gpuEnabled) {
        fprintf(stdout,
                "F4_V37_GPU_ROUND iter=%d gpu_ok=%lld wide_ok=%lld cpu_ok=%lld gpu_pops=%lld cpu_pops=%lld gpu_waves=%lld gpu_ov=%lld gpu_fb=%lld gpu_s=%.2f total_s=%.2f\n",
                routeIter, static_cast<long long>(gpuCommits), static_cast<long long>(gpuWideOk),
                static_cast<long long>(routedCount - gpuCommits),
                static_cast<long long>(gpuPops), static_cast<long long>(totalPops - gpuPops),
                static_cast<long long>(gpuWaves), static_cast<long long>(gpuOverflows),
                static_cast<long long>(gpuFallbacks), gpuNs / 1e9, elapsedNs / 1e9);
        fflush(stdout);
    }

    jlong stats[18] = {
        routedCount,
        static_cast<jlong>(G.overusedList.size()),
        totalExcess,
        parallelCommits,
        serialCommits,
        conflictRetries,
        baseHeavyCount,
        totalPops,
        fallbackCount,
        unroutableCount,
        elapsedNs,
        skippedLive,
        relaxedHeavyCount,
        sameNetOk,
        gpuCommits,
        gpuPops,
        gpuNs,
        gpuWaves
    };
    jsize outLen = env->GetArrayLength(jOutStats);
    env->SetLongArrayRegion(jOutStats, 0, std::min<jsize>(outLen, 18), stats);
}

JNIEXPORT jint JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeUpdateHistAndFeedback(
        JNIEnv* env, jclass,
        jint routeIter,
        jfloat histFactor, jboolean enlargeBbox, jint extX, jint extY,
        jintArray jOutNextActive, jintArray jOutCongested) {
    I32 cap = static_cast<I32>(G.ord[CAPACITY]);
    std::vector<U32> histIds;
    std::vector<float> histVals;
    if (G.gpuEnabled) {
        histIds.reserve(G.overusedList.size());
        histVals.reserve(G.overusedList.size());
    }
    const bool microStuck = (routeIter >= 55 && !G.overusedList.empty() && G.overusedList.size() <= 2u && G.lastTailFbCount == 0);
    if (microStuck) ++G.microStuckRounds;
    else G.microStuckRounds = 0;
    const float microMult = !microStuck ? 1.0f : ((G.microStuckRounds >= 2) ? 12.0f : 6.0f);
    if (microStuck) {
        fprintf(stdout, "F4_V68_MICRO_STUCK iter=%d over=%zu streak=%d mult=%.1f\n",
                routeIter, G.overusedList.size(), G.microStuckRounds, microMult);
        fflush(stdout);
    }
    for (U32 id : G.overusedList) {
        I32 ov = G.dyn[id].occ - cap;
        if (ov > 0) {
            G.dyn[id].hist += static_cast<float>(ov) * histFactor * microMult;
            if (G.gpuEnabled) {
                histIds.push_back(id);
                histVals.push_back(G.dyn[id].hist);
            }
        }
    }
    if (G.gpuEnabled && !histIds.empty()) {
        fidesGpuApplyHistDeltas(histIds.data(), histVals.data(), static_cast<U32>(histIds.size()));
    }

    const bool midOrTail = (routeIter >= 6);
    const bool tailPhase = (routeIter >= 14) || (G.overusedList.size() <= 2200);
    U32 numConns = static_cast<U32>(G.conns.size());
    std::vector<uint8_t> needRoute(numConns, 0);
    auto promoteWidenFromStreak = [&](U32 idx, uint8_t cc) {
        if (tailPhase) {
            uint8_t next = static_cast<uint8_t>(std::min<int>(4, static_cast<int>(G.connWiden[idx]) + 1));
            if (cc >= 3) next = 4;
            else if (cc >= 2) next = std::max<uint8_t>(next, uint8_t(3));
            else if (cc >= 1) next = std::max<uint8_t>(next, uint8_t(2));
            G.connWiden[idx] = std::max(G.connWiden[idx], next);
        } else if (midOrTail) {
            uint8_t next = G.connWiden[idx];
            if (cc >= 6) next = std::max<uint8_t>(next, uint8_t(4));
            else if (cc >= 4) next = std::max<uint8_t>(next, uint8_t(3));
            else if (cc >= 2) next = std::max<uint8_t>(next, uint8_t(2));
            else if (cc >= 1) next = std::max<uint8_t>(next, uint8_t(1));
            G.connWiden[idx] = next;
        } else {
            if (cc >= 8) G.connWiden[idx] = std::max<uint8_t>(G.connWiden[idx], uint8_t(4));
            else if (cc >= 6) G.connWiden[idx] = std::max<uint8_t>(G.connWiden[idx], uint8_t(3));
            else if (cc >= 4) G.connWiden[idx] = std::max<uint8_t>(G.connWiden[idx], uint8_t(2));
            else if (cc >= 2) G.connWiden[idx] = std::max<uint8_t>(G.connWiden[idx], uint8_t(1));
        }
    };
    auto enlargeConnOrigBox = [&](U32 idx) {
        if (enlargeBbox == JNI_TRUE) {
            ConnData& cd = G.conns[idx];
            cd.origXmin = std::max(-1, cd.origXmin - extX);
            cd.origXmax += extX;
            cd.origYmin = std::max(-1, cd.origYmin - extY);
            cd.origYmax += extY;
        }
    };

    if (!G.overusedList.empty()) {
        int chunks = std::max(1, EXEC.numWorkers + 1);
        U32 step = (numConns + chunks - 1) / chunks;
        BoundaryPool::runChunks(chunks, [&](int c) {
            U32 lo = static_cast<U32>(c) * step;
            U32 hi = std::min(numConns, lo + step);
            for (U32 i = lo; i < hi; ++i) {
                if (!G.connRouted[i]) {
                    needRoute[i] = 1;
                    enlargeConnOrigBox(i);
                    G.consecCongested[i] = (G.consecCongested[i] < 250) ? G.consecCongested[i] + 1 : 250;
                    promoteWidenFromStreak(i, G.consecCongested[i]);
                    continue;
                }
                bool cong = false;
                const auto& path = G.connPaths[i];
                for (U32 id : path) {
                    if (G.dyn[id].occ > cap) {
                        cong = true;
                        needRoute[i] = 1;
                        enlargeConnOrigBox(i);
                        break;
                    }
                }
                if (cong) {
                    G.consecCongested[i] = (G.consecCongested[i] < 250) ? G.consecCongested[i] + 1 : 250;
                    promoteWidenFromStreak(i, G.consecCongested[i]);
                } else {
                    if (midOrTail) {
                        G.consecCongested[i] >>= 1;
                    } else {
                        G.consecCongested[i] = 0;
                        G.connWiden[i] = 0;
                    }
                }
            }
        });
    } else {
        for (U32 i = 0; i < numConns; ++i) {
            if (!G.connRouted[i]) {
                needRoute[i] = 1;
                enlargeConnOrigBox(i);
            } else {
                G.consecCongested[i] = 0;
                G.connWiden[i] = 0;
            }
        }
    }

    std::vector<jint> nextAct;
    nextAct.reserve(std::min<U32>(numConns, 1 << 20));
    for (U32 i = 0; i < numConns; ++i) {
        if (needRoute[i]) nextAct.push_back(static_cast<jint>(i));
    }

    jsize capOut = env->GetArrayLength(jOutNextActive);
    jsize copyLen = std::min<jsize>(capOut, static_cast<jsize>(nextAct.size()));
    if (copyLen > 0) {
        env->SetIntArrayRegion(jOutNextActive, 0, copyLen, nextAct.data());
        if (jOutCongested != nullptr) {
            env->SetIntArrayRegion(jOutCongested, 0, copyLen, nextAct.data());
        }
    }
    return static_cast<jint>(nextAct.size());
}

JNIEXPORT jint JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeExportPath(
        JNIEnv* env, jclass, jint connIdx, jintArray jOutPath) {
    if (connIdx < 0 || static_cast<size_t>(connIdx) >= G.connPaths.size()) return 0;
    if (!G.connRouted[connIdx]) return 0;
    const auto& path = G.connPaths[connIdx];
    jsize maxLen = env->GetArrayLength(jOutPath);
    jsize len = static_cast<jsize>(path.size());
    if (len > maxLen) return -len;
    env->SetIntArrayRegion(jOutPath, 0, len, reinterpret_cast<const jint*>(path.data()));
    return len;
}

JNIEXPORT jint JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeExportOverused(
        JNIEnv* env, jclass, jintArray jOutIds, jfloatArray jOutHist) {
    jsize len = static_cast<jsize>(G.overusedList.size());
    jsize cap = env->GetArrayLength(jOutIds);
    jsize n = std::min(len, cap);
    if (n > 0) {
        env->SetIntArrayRegion(jOutIds, 0, n, reinterpret_cast<const jint*>(G.overusedList.data()));
        std::vector<jfloat> h(n);
        for (jsize i = 0; i < n; ++i) h[i] = G.dyn[G.overusedList[i]].hist;
        env->SetFloatArrayRegion(jOutHist, 0, n, h.data());
    }
    return len;
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeShutdownGpu(
        JNIEnv*, jclass) {
    if (G.gpuEnabled) {
        fidesGpuShutdown();
        G.gpuEnabled = false;
        fprintf(stdout, "FIDES_V37_GPU shutdown=true\n");
        fflush(stdout);
    }
}

}  // extern "C"
