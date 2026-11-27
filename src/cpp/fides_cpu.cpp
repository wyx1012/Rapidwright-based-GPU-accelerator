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
#include <new>
#include <queue>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#ifdef __linux__
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

#if defined(__x86_64__) || defined(_M_X64)
#include <immintrin.h>
#define FIDES_PAUSE() _mm_pause()
#else
#define FIDES_PAUSE() ((void)0)
#endif

template <typename T>
struct HugeBuf {
    static_assert(std::is_trivially_copyable_v<T>, "HugeBuf requires trivial POD");
    T* ptr = nullptr;
    size_t count = 0;
    size_t capBytes = 0;

    HugeBuf() = default;
    HugeBuf(const HugeBuf&) = delete;
    HugeBuf& operator=(const HugeBuf&) = delete;
    ~HugeBuf() { release(); }

    void release() {
#ifdef __linux__
        if (ptr != nullptr && capBytes > 0) munmap(ptr, capBytes);
#else
        std::free(ptr);
#endif
        ptr = nullptr;
        count = 0;
        capBytes = 0;
    }

    void swap(HugeBuf<T>& other) noexcept {
        std::swap(ptr, other.ptr);
        std::swap(count, other.count);
        std::swap(capBytes, other.capBytes);
    }

    // Allocates virtual memory without eager single-threaded zero-fill so parallel workers fault pages with THP.
    void resizeNoInit(size_t n) {
        if (n == count && ptr != nullptr) return;
        size_t needBytes = n * sizeof(T);
        if (needBytes == 0) {
            release();
            return;
        }
        if (needBytes > capBytes) {
            release();
#ifdef __linux__
            constexpr size_t kHugeAlign = (2ULL << 20);
            size_t mapBytes = (needBytes + kHugeAlign - 1ULL) & ~(kHugeAlign - 1ULL);
            void* p = mmap(nullptr, mapBytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (p == MAP_FAILED) throw std::bad_alloc();
#ifdef MADV_HUGEPAGE
            (void) madvise(p, mapBytes, MADV_HUGEPAGE);
#endif
            ptr = static_cast<T*>(p);
            capBytes = mapBytes;
#else
            ptr = static_cast<T*>(std::malloc(needBytes));
            if (!ptr) throw std::bad_alloc();
            capBytes = needBytes;
#endif
        }
        count = n;
    }

    T* data() { return ptr; }
    const T* data() const { return ptr; }
    size_t size() const { return count; }
    bool empty() const { return count == 0; }
    T* begin() { return ptr; }
    const T* begin() const { return ptr; }
    T* end() { return ptr + count; }
    const T* end() const { return ptr + count; }
    T& operator[](size_t i) { return ptr[i]; }
    const T& operator[](size_t i) const { return ptr[i]; }
};

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
    PF_LAGUNA_SOUTH = 1u << 9,
    PF_UTURN_DELAY  = 1u << 10
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
    bool megaSyncTrackedReg[2] = {false, false};
    I32 megaSlotReg[2] = {-1, -1};
    std::vector<U32> megaDirtyReg[2];
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
        for (int r = 0; r < 2; ++r) {
            megaSyncTrackedReg[r] = false;
            megaDirtyReg[r].clear();
        }
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
        for (int r = 0; r < 2; ++r) {
            megaSyncTrackedReg[r] = false;
            megaDirtyReg[r].clear();
        }
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

struct alignas(16) NodeGeomHot {
    I16 endTileX;
    I16 endTileYRaw;
    I16 tileY;
    U16 length;
    U16 preFlags;
    uint8_t typeFlags;
    uint8_t nodeType;
    float baseCost;
};
static_assert(sizeof(NodeGeomHot) == 16, "NodeGeomHot must be 16 bytes");

struct alignas(8) NodeGeomCold {
    I32 tileId;
    I16 endSlrRaw;
    I16 endSlrTileY;
};
static_assert(sizeof(NodeGeomCold) == 8, "NodeGeomCold must be 8 bytes");

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
    HugeBuf<U32> offset;
    HugeBuf<U32> edge;
    HugeBuf<uint8_t> coreDeg;
    HugeBuf<NodeGeomHot> geomHot;
    HugeBuf<NodeGeomCold> geomCold;
    HugeBuf<I32> occ;
    HugeBuf<float> hist;

    std::vector<U32> ord;
    std::vector<U32> typeFlags;
    std::vector<U32> tileFlags;
    std::vector<I32> slr;
    std::vector<I32> nextLaguna;
    std::vector<I32> prevLaguna;
    I32 sllLen = 0;
    I32 maxSllSpanY = 64;

    std::vector<float> pcc;
    HugeBuf<I32> overusedPos;
    std::vector<U32> overusedList;
    HugeBuf<U32> lastCommitSeq;
    HugeBuf<U32> lastCommitNet;
    HugeBuf<U32> prevOtherSeq;
    U32 commitSeq = 1;

    std::vector<NetScopeData> netScopes;
    std::vector<ConnData> conns;
    std::vector<std::vector<U32>> connPaths;
    std::vector<uint8_t> connRouted;
    std::vector<I64> lastPops;
    std::vector<uint8_t> consecCongested;
    std::vector<uint8_t> connWiden;
    std::vector<uint8_t> tailFailStreak;
    std::vector<uint8_t> gpuOverflowed;
    std::vector<I32> corrBx0;
    std::vector<I32> corrBy0;
    std::vector<I32> corrBx1;
    std::vector<I32> corrBy1;
    std::vector<uint8_t> corrBuse;
    std::vector<uint8_t> corrMode;
    bool corrInit = false;
    bool ready = false;

    struct SnapEntry { U32 node; U32 conn; };
    static constexpr U32 SNAP_BUCKETS = 1u << 22;
    bool snapReady = false;
    std::vector<U32> snapOffsets;
    std::vector<SnapEntry> snapEntries;
    std::vector<uint8_t> snapDirtyFlag;
    std::vector<U32> snapDirtyList;
    std::vector<U32> snapCandStamp;
    U32 snapCandEpoch = 1u;
    std::vector<I32> lastRoundActive;
    std::vector<uint8_t> activeStreakFlag;
    std::vector<U32> activeStreakList;

    // v40 multi-GPU (1..4 x V100) CUDA dual-engine state & sync buffers
    bool gpuEnabled = false;
    int gpuCount = 0;
    FidesGpuMultiInitInfo gpuMulti{};
    HugeBuf<U32> gpuDirtyOccStamp;
    U32 gpuDirtyOccEpoch = 1;
    std::vector<U32> gpuDirtyOccIds;
    std::vector<I32> gpuDirtyOccVals;
    HugeBuf<U32> waveOverusedStamp;
    HugeBuf<I32> waveOvClaimRem;
    U32 waveOverusedEpoch = 1;
    HugeBuf<U64> waveCommitKey;
    U32 waveCommitEpoch = 1;
    U32 gpuStampWaveEpoch = 0;
    I64 lastTailFbCount = 0;
    int microStuckRounds = 0;
    struct MegaCacheEntry { U32 netId = UINT32_MAX; U32 layoutVersion = 0; U32 mask = 0; };
    MegaCacheEntry megaCache[1024];
    U32 megaCacheClock = 0;
};

static GraphData G;
static U32 s_ord[OrdinalCount]{};
static const float* s_pccPtr = nullptr;
static int s_pccLen = 0;

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

inline int endY(const NodeGeomHot& cg, const NodeGeomHot* pg) {
    return ((cg.preFlags & PF_SLL) && pg != nullptr && pg->endTileYRaw == cg.endTileYRaw)
            ? static_cast<int>(cg.tileY)
            : static_cast<int>(cg.endTileYRaw);
}

inline int slrFast(U32 id, const NodeGeomHot& cg, const NodeGeomHot* pg) {
    return ((cg.preFlags & PF_SLL) && pg != nullptr && pg->endTileYRaw == cg.endTileYRaw)
            ? static_cast<int>(G.geomCold[id].endSlrTileY)
            : static_cast<int>(G.geomCold[id].endSlrRaw);
}

inline int getUsage(U32 id, int& sameUses) {
    I32 rawOcc = __atomic_load_n(&G.occ[id], __ATOMIC_ACQUIRE);
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
                        I32 ovOcc = __atomic_load_n(&G.occ[id], __ATOMIC_ACQUIRE) + S.tinyOv[i].occDelta;
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
                I32 ovOcc = __atomic_load_n(&G.occ[id], __ATOMIC_ACQUIRE) + ov->occDelta;
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
        I32 occVal = __atomic_load_n(&G.occ[id], __ATOMIC_ACQUIRE);
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
    uint8_t sinkDirKind; // 0=EX_EAST, 1=EX_WEST, 2=EX_BOTH, 3=EX_NONLOCAL, 4=EAST/WEST, 5=OTHER
    int sinkBeginX, sinkBeginY, sinkTile, sinkType, sinkCtrl, sinkSlr;
    int sinkEndX, sinkEndY;
    float xCenter, yCenter, shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight;
    float invBiasDenom;
    int budget;
    bool earlyTermination;
    I64 nodesPopped;
};

inline bool isAccessibleSinkCached(
        U32 child, const NodeGeomHot& cg, const SearchQuery& q,
        int& outOcc, int& outSame, bool& hasUsage) {
    if (child == q.sink) return true;
    if (!(cg.preFlags & PF_PINBOUNCE)) return false;
    outOcc = getUsage(child, outSame);
    hasUsage = true;
    return outSame != 0;
}

inline int checkAccessAndLookahead(
        U32 /*parent*/, const NodeGeomHot& pg,
        U32 entering, U32 child, const NodeGeomHot& cg,
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

    if (cf & Local) {
        int childTile = G.geomCold[child].tileId;
        if (q.cross && (cf & Leading) && slrFast(child, cg, &pg) != q.sinkSlr) {
            int dir = (cg.preFlags & PF_LAGUNA_NORTH ? 1 : 0) | (cg.preFlags & PF_LAGUNA_SOUTH ? 2 : 0);
            if (((dir & 1) && !q.north) || ((dir & 2) && !q.south)) return 1;
            if (!(cg.preFlags & PF_INT_TILE) || !(cg.preFlags & PF_LAGUNA_WIRE) || childTile == q.sinkTile) {
                return -1;
            }
            if (!willOveruseCached(child, outOcc, outSame, hasUsage)) lookahead = true;
        } else {
            bool allowed = false;
            switch (q.sinkDirKind) {
                case 0: // EX_EAST
                    if ((cf & West) || ct == s_ord[RESERVED]) return 1;
                    allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
                    break;
                case 1: // EX_WEST
                    if ((cf & East) || ct == s_ord[RESERVED]) return 1;
                    allowed = (childTile == q.sinkTile || (childX == q.sinkBeginX && fides_absInt(childY - q.sinkBeginY) <= 1));
                    break;
                case 2: // EX_BOTH
                    if (childTile != q.sinkTile || ct != s_ord[BOTH] || q.sinkCtrl == 0) return 1;
                    allowed = true;
                    break;
                case 3: // EX_NONLOCAL
                    return 1;
                case 4: // EAST
                    if ((cf & West) || ct == s_ord[RESERVED]) return 1;
                    return -1;
                case 5: // WEST
                    if ((cf & East) || ct == s_ord[RESERVED]) return 1;
                    return -1;
                default:
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
        if (ct == s_ord[NONLOCAL]) {
            if (pf & Local) {
                if (childLaguna) {
                    if (!q.cross || slrFast(child, cg, &pg) == q.sinkSlr) return 1;
                } else {
                    if (!(pg.preFlags & PF_PINFEED)
                        || !(cg.preFlags & PF_CLB_TILE)
                        || !(cg.preFlags & PF_CLE_OUTPUT)) {
                        return -1;
                    }
                }
            } else if (pg.preFlags & PF_SLL) {
                if (entering == UINT32_MAX || G.geomCold[entering].tileId == G.geomCold[child].tileId) return 1;
            }
        } else if (cg.preFlags & (PF_LAGUNA_NORTH | PF_LAGUNA_SOUTH)) {
            if (q.cross && slrFast(child, cg, &pg) != q.sinkSlr) {
                int dir = (cg.preFlags & PF_LAGUNA_NORTH ? 1 : 0) | (cg.preFlags & PF_LAGUNA_SOUTH ? 2 : 0);
                if (((dir & 1) && q.north) || ((dir & 2) && q.south)) {
                    if (!willOveruseCached(child, outOcc, outSame, hasUsage)) lookahead = true;
                }
            }
        } else {
            return -1;
        }
        if (!q.uturn && (cg.preFlags & PF_UTURN_DELAY)) return 1;
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
    const NodeGeomHot& pg = G.geomHot[u];
    U32 first = G.offset[u];
    U32 last = G.offset[u + 1];
    const uint8_t cd = G.coreDeg[u];
    if (cd < 255u && (first + static_cast<U32>(cd) < last)) {
        const bool needSeg1 =
            (static_cast<int>(pg.endTileX) == q.sinkEndX && fides_absInt(static_cast<int>(pg.endTileYRaw) - q.sinkEndY) <= 3)
            || (static_cast<int>(pg.endTileX) == q.sinkBeginX && fides_absInt(static_cast<int>(pg.endTileYRaw) - q.sinkBeginY) <= 2);
        if (!needSeg1) last = first + static_cast<U32>(cd);
    }
    for (U32 e = first; e < last; ++e) {
        if (e + 2u < last) {
            __builtin_prefetch(&G.geomHot[G.edge[e + 2u]], 0, 1);
        }
        U32 child = G.edge[e];
        NodeSlot* cs = S.slotForRead(child);
        if (cs != nullptr && cs->stamp == S.seq) continue;

        const NodeGeomHot& cg = G.geomHot[child];
        int childX = static_cast<int>(cg.endTileX);
        int childY = endY(cg, &pg);
        if (q.useBox && child != q.sink) {
            if (!(childX > q.xmin && childX < q.xmax && childY > q.ymin && childY < q.ymax)) {
                continue;
            }
        }

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
        if (pccIdx < s_pccLen) {
            pccCost = s_pccPtr[pccIdx];
        } else if (s_pccLen >= 2) {
            int lastIdx = s_pccLen - 1;
            pccCost = s_pccPtr[lastIdx] + static_cast<float>(pccIdx - lastIdx) * (s_pccPtr[lastIdx] - s_pccPtr[lastIdx - 1]);
        } else {
            return STATUS_FALLBACK;
        }

        float distToCenter = std::fabs(static_cast<float>(childX) - q.xCenter)
                           + std::fabs(static_cast<float>(childY) - q.yCenter);
        float bc = cg.baseCost;
        float bias = bc * distToCenter * q.invBiasDenom;
        float rawNc = bc * G.hist[child] * pccCost;
        float invSharing;
        float nc;
        if (sameUses == 0) {
            invSharing = 1.0f;
            nc = rawNc;
        } else {
            invSharing = 1.0f / (1.0f + q.shareWeight * static_cast<float>(sameUses));
            nc = rawNc * invSharing;
        }
        float halfNc = nc * 0.5f;
        float nodeCost = nc + ((bias <= halfNc) ? bias : halfNc);

        float newPartial = uUpstream + q.rnodeCostWeight * nodeCost;
        newPartial += (sameUses == 0)
                ? (q.rnodeWLWeight * static_cast<float>(cg.length))
                : (q.rnodeWLWeight * static_cast<float>(cg.length) * invSharing);

        int deltaX = fides_absInt(childX - q.sinkBeginX);
        int deltaY = fides_absInt(childY - q.sinkBeginY);
        if (q.cross) {
            int childSlr = slrFast(child, cg, &pg);
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
                : (newPartial + q.estWlWeight * static_cast<float>(distToSink) * invSharing);

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
    q.sinkEndX = static_cast<int>(G.geomHot[q.sink].endTileX);
    q.sinkEndY = static_cast<int>(G.geomHot[q.sink].endTileYRaw);
    q.sinkTile = c.sinkTile;
    q.sinkType = c.sinkType;
    {
        U32 st = static_cast<U32>(c.sinkType);
        if (st == s_ord[EX_EAST]) q.sinkDirKind = 0;
        else if (st == s_ord[EX_WEST]) q.sinkDirKind = 1;
        else if (st == s_ord[EX_BOTH]) q.sinkDirKind = 2;
        else if (st == s_ord[EX_NONLOCAL]) q.sinkDirKind = 3;
        else if (st == s_ord[EAST]) q.sinkDirKind = 4;
        else if (st == s_ord[WEST]) q.sinkDirKind = 5;
        else q.sinkDirKind = 6;
    }
    q.sinkCtrl = (c.flags & 64u) != 0 ? 1 : 0;
    q.sinkSlr = static_cast<int>(G.geomCold[q.sink].endSlrRaw);
    q.xCenter = c.xCenter;
    q.yCenter = c.yCenter;
    q.invBiasDenom = 1.0f / (static_cast<float>(c.netSize) * static_cast<float>(c.doubleHpwl));
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
    for (int r = 0; r < 2; ++r) {
        if (sc.megaSyncTrackedReg[r]) {
            if (sc.megaDirtyReg[r].size() < 65536u) {
                sc.megaDirtyReg[r].push_back(idx);
            } else {
                sc.megaDirtyReg[r].clear();
                ++sc.layoutVersion;
                sc.megaSyncTrackedReg[r] = false;
            }
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
        I32 before = G.occ[nid];
        I32 after = before + occDelta;
        __atomic_store_n(&G.occ[nid], after, __ATOMIC_RELEASE);
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

struct WaveThreadPool {
    std::vector<std::thread> ths;
    std::mutex mtx;
    std::condition_variable cvWork, cvDone;
    const std::function<void(size_t, size_t)>* fn = nullptr;
    std::atomic<size_t> nextIdx{0};
    std::atomic<uint64_t> gen{0};
    std::atomic<int> pending{0};
    size_t total = 0, grain = 1;
    std::atomic<bool> stop{false};

    void init(int n) {
        if (static_cast<int>(ths.size()) >= n) return;
        shutdown();
        stop.store(false, std::memory_order_release);
        ths.reserve(n);
        for (int i = 0; i < n; ++i) {
            ths.emplace_back([this]() {
                uint64_t myGen = 0;
                while (true) {
                    uint64_t gNow = gen.load(std::memory_order_acquire);
                    if (gNow == myGen) {
                        for (int sp = 0; sp < 128; ++sp) {
                            FIDES_PAUSE();
                            gNow = gen.load(std::memory_order_acquire);
                            if (gNow != myGen || stop.load(std::memory_order_relaxed)) break;
                        }
                    }
                    if (gNow == myGen && !stop.load(std::memory_order_acquire)) {
                        std::unique_lock<std::mutex> lk(mtx);
                        cvWork.wait(lk, [&]() {
                            return stop.load(std::memory_order_relaxed) || gen.load(std::memory_order_relaxed) != myGen;
                        });
                        gNow = gen.load(std::memory_order_acquire);
                    }
                    if (stop.load(std::memory_order_acquire)) return;
                    myGen = gNow;
                    const auto* fPtr = fn;
                    const size_t tot = total;
                    const size_t g = grain;
                    while (true) {
                        size_t lo = nextIdx.fetch_add(g, std::memory_order_relaxed);
                        if (lo >= tot) break;
                        (*fPtr)(lo, std::min(tot, lo + g));
                    }
                    if (pending.fetch_sub(1, std::memory_order_acq_rel) == 1) {
                        std::lock_guard<std::mutex> lk(mtx);
                        cvDone.notify_one();
                    }
                }
            });
        }
    }

    template <class Fn>
    void parallelFor(size_t tot, size_t g, Fn&& f) {
        if (tot == 0u) return;
        g = std::max<size_t>(1u, g);
        if (ths.empty() || tot <= g) {
            f(0u, tot);
            return;
        }
        std::function<void(size_t, size_t)> cb(f);
        {
            std::lock_guard<std::mutex> lk(mtx);
            fn = &cb;
            total = tot;
            grain = g;
            nextIdx.store(0u, std::memory_order_relaxed);
            pending.store(static_cast<int>(ths.size()), std::memory_order_release);
            gen.fetch_add(1, std::memory_order_release);
        }
        cvWork.notify_all();
        while (true) {
            size_t lo = nextIdx.fetch_add(g, std::memory_order_relaxed);
            if (lo >= tot) break;
            cb(lo, std::min(tot, lo + g));
        }
        if (pending.load(std::memory_order_acquire) != 0) {
            for (int sp = 0; sp < 128; ++sp) {
                FIDES_PAUSE();
                if (pending.load(std::memory_order_acquire) == 0) break;
            }
            if (pending.load(std::memory_order_acquire) != 0) {
                std::unique_lock<std::mutex> lk(mtx);
                cvDone.wait(lk, [&]() { return pending.load(std::memory_order_acquire) == 0; });
            }
        }
        fn = nullptr;
    }

    void shutdown() {
        if (ths.empty()) return;
        {
            std::lock_guard<std::mutex> lk(mtx);
            stop.store(true, std::memory_order_release);
            gen.fetch_add(1, std::memory_order_release);
        }
        cvWork.notify_all();
        for (auto& t : ths) {
            if (t.joinable()) t.join();
        }
        ths.clear();
    }

    ~WaveThreadPool() { shutdown(); }
};

static WaveThreadPool g_regPools[2];
static std::mutex g_regNetMtx[4096];

inline void flushGpuOccDeltas() {
    if (!G.gpuEnabled || G.gpuDirtyOccIds.empty()) return;
    U32 cnt = static_cast<U32>(G.gpuDirtyOccIds.size());
    G.gpuDirtyOccVals.resize(cnt);
    const U32* ids = G.gpuDirtyOccIds.data();
    I32* vals = G.gpuDirtyOccVals.data();
    EXEC.parallelFor(cnt, 4096u, [&](size_t lo, size_t hi) {
        for (size_t i = lo; i < hi; ++i) {
            vals[i] = G.occ[ids[i]];
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

struct InFlightBox {
    I32 x0, y0, x1, y1;
};

inline bool boxesOverlap(const InFlightBox& a, const InFlightBox& b) {
    return a.x0 <= b.x1 && b.x0 <= a.x1 && a.y0 <= b.y1 && b.y0 <= a.y1;
}

}  // namespace

static std::mutex g_preinitMtx;
static bool g_staticPreinitOk = false;
static std::mutex g_gpuInitMtx;
static std::thread g_gpuInitThread;
static bool g_gpuInitPending = false;
static bool g_gpuInitOk = false;
static bool g_gpuInitBoundsOk = true;
static int g_pendingReqMaxGpus = FG_MAX_GPUS;
static HugeBuf<FidesGpuNodeHot> g_pendingGpuHot;
static HugeBuf<FidesGpuDynNode> g_pendingGpuDyn;
static std::vector<float> g_pendingGpuPcc;

static std::atomic<I32> g_expectedConnCount{-1};

static void ensureGpuInitJoined() {
    if (!g_gpuInitPending) return;
    std::lock_guard<std::mutex> lk(g_gpuInitMtx);
    if (!g_gpuInitPending) return;
    if (g_gpuInitThread.joinable()) g_gpuInitThread.join();
    g_pendingGpuHot.release();
    g_pendingGpuDyn.release();
    if (g_gpuInitOk) {
        G.gpuEnabled = true;
        G.gpuCount = G.gpuMulti.numGpus;
        if (G.pcc.size() != g_pendingGpuPcc.size()
                || (!G.pcc.empty() && std::memcmp(G.pcc.data(), g_pendingGpuPcc.data(), G.pcc.size() * sizeof(float)) != 0)) {
            fidesGpuUpdatePcc(G.pcc.data(), static_cast<I32>(G.pcc.size()));
        }
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
        fprintf(stdout, "FIDES_V37_GPU ready=false boundsOk=%d (falling back to CPU)\n", (int)g_gpuInitBoundsOk);
        fflush(stdout);
    }
    std::vector<float>().swap(g_pendingGpuPcc);
    g_gpuInitPending = false;
}

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

    struct MmapGuard {
        int fd = -1;
        const uint8_t* ptr = nullptr;
        size_t size = 0;
        ~MmapGuard() {
            if (ptr != nullptr && ptr != MAP_FAILED) munmap(const_cast<uint8_t*>(ptr), size);
            if (fd >= 0) close(fd);
        }
    } mg;

    mg.fd = open(rrgPath.c_str(), O_RDONLY);
    if (mg.fd < 0) return false;
    struct stat st{};
    if (fstat(mg.fd, &st) != 0 || st.st_size < 24) return false;
    mg.size = static_cast<size_t>(st.st_size);
    void* mapped = mmap(nullptr, mg.size, PROT_READ, MAP_PRIVATE, mg.fd, 0);
    if (mapped == MAP_FAILED) return false;
    mg.ptr = static_cast<const uint8_t*>(mapped);
    (void) madvise(mapped, mg.size, MADV_WILLNEED);

    if (std::memcmp(mg.ptr, "FIDESRRG", 8) != 0) return false;
    U32 version = 0, n = 0, m = 0, secCount = 0;
    std::memcpy(&version, mg.ptr + 8, 4);
    std::memcpy(&n, mg.ptr + 12, 4);
    std::memcpy(&m, mg.ptr + 16, 4);
    std::memcpy(&secCount, mg.ptr + 20, 4);
    if (version != 2 || mg.size < 24u + static_cast<size_t>(secCount) * 56u) return false;

    struct Sec { std::string name; U64 count; U64 offset; };
    std::vector<Sec> secs;
    secs.reserve(secCount);
    for (U32 i = 0; i < secCount; ++i) {
        const uint8_t* sp = mg.ptr + 24u + static_cast<size_t>(i) * 56u;
        size_t len = 0;
        while (len < 32 && sp[len] != '\0') ++len;
        U64 cnt = 0, off = 0;
        std::memcpy(&cnt, sp + 40, 8);
        std::memcpy(&off, sp + 48, 8);
        if (off > mg.size || cnt * 4ULL > mg.size - off) return false;
        secs.push_back({std::string(reinterpret_cast<const char*>(sp), len), cnt, off});
    }
    auto findSecPtr = [&](const char* name, U64 expectCnt, const void*& outPtr) -> bool {
        for (const auto& s : secs) {
            if (s.name == name && s.count == expectCnt) {
                outPtr = mg.ptr + s.offset;
                return true;
            }
        }
        return false;
    };

    const void* pOff = nullptr;
    const void* pEdge = nullptr;
    const void* pNodeType = nullptr;
    const void* pIntentCode = nullptr;
    const void* pTileType = nullptr;
    const void* pTileId = nullptr;
    const void* pEndTileX = nullptr;
    const void* pEndTileYRaw = nullptr;
    const void* pTileY = nullptr;
    const void* pLength = nullptr;
    const void* pBaseCost = nullptr;
    const void* pDelay = nullptr;
    if (!findSecPtr("offset", static_cast<U64>(n) + 1ULL, pOff)
            || !findSecPtr("edge", m, pEdge)
            || !findSecPtr("nodeType", n, pNodeType)
            || !findSecPtr("intentCode", n, pIntentCode)
            || !findSecPtr("tileType", n, pTileType)
            || !findSecPtr("tileId", n, pTileId)
            || !findSecPtr("endTileX", n, pEndTileX)
            || !findSecPtr("endTileYRaw", n, pEndTileYRaw)
            || !findSecPtr("tileY", n, pTileY)
            || !findSecPtr("length", n, pLength)
            || !findSecPtr("baseCost", n, pBaseCost)
            || !findSecPtr("delay", n, pDelay)) {
        return false;
    }

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
    for (int oi = 0; oi < OrdinalCount; ++oi) s_ord[oi] = G.ord[oi];

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

    const U32* rawOff = static_cast<const U32*>(pOff);
    const U32* rawEdge = static_cast<const U32*>(pEdge);
    G.offset.resizeNoInit(static_cast<size_t>(n) + 1u);
    parallelForU32(n + 1u, 36, [&](U32 lo, U32 hi) {
        std::memcpy(G.offset.data() + lo, rawOff + lo, static_cast<size_t>(hi - lo) * sizeof(U32));
    });
    G.edge.resizeNoInit(m);
    parallelForU32(m, 36, [&](U32 lo, U32 hi) {
        std::memcpy(G.edge.data() + lo, rawEdge + lo, static_cast<size_t>(hi - lo) * sizeof(U32));
    });

    const I32* nodeType = static_cast<const I32*>(pNodeType);
    const I32* intentCode = static_cast<const I32*>(pIntentCode);
    const I32* tileType = static_cast<const I32*>(pTileType);
    const I32* tileId = static_cast<const I32*>(pTileId);
    const I32* endTileX = static_cast<const I32*>(pEndTileX);
    const I32* endTileYRaw = static_cast<const I32*>(pEndTileYRaw);
    const I32* tileY = static_cast<const I32*>(pTileY);
    const I32* length = static_cast<const I32*>(pLength);
    const float* baseCost = static_cast<const float*>(pBaseCost);
    const float* delay = static_cast<const float*>(pDelay);

    G.geomHot.resizeNoInit(n);
    G.geomCold.resizeNoInit(n);
    G.occ.resizeNoInit(n);
    G.hist.resizeNoInit(n);
    G.coreDeg.resizeNoInit(n);
    G.overusedPos.resizeNoInit(n);
    G.lastCommitSeq.resizeNoInit(n);
    G.lastCommitNet.resizeNoInit(n);
    G.prevOtherSeq.resizeNoInit(n);
    const char* disableGpuEnv = std::getenv("FIDES_V37_DISABLE_GPU");
    bool tryGpu = (disableGpuEnv == nullptr || std::strcmp(disableGpuEnv, "1") != 0);
    if (tryGpu) {
        G.gpuDirtyOccStamp.resizeNoInit(n);
        G.waveOverusedStamp.resizeNoInit(n);
        G.waveOvClaimRem.resizeNoInit(n);
        G.waveCommitKey.resizeNoInit(n);
    }

    const U32 ordSll = s_ord[SLL];
    const U32 ordIntTile = s_ord[INT_TILE];
    const U32 ordPinbounce = s_ord[PINBOUNCE];
    const U32 ordPinfeed = s_ord[PINFEED];
    const U32 ordCleOut = s_ord[CLE_OUTPUT];

    HugeBuf<uint32_t> seg1Pack;
    seg1Pack.resizeNoInit(n);
    std::atomic<bool> boundsOk{true};
    std::atomic<I32> maxSllSpan{0};
    parallelForU32(n, 36, [&](U32 lo, U32 hi) {
        const size_t cnt = static_cast<size_t>(hi - lo);
        std::memset(G.lastCommitSeq.data() + lo, 0, cnt * sizeof(U32));
        std::memset(G.prevOtherSeq.data() + lo, 0, cnt * sizeof(U32));
        if (tryGpu) {
            std::memset(G.gpuDirtyOccStamp.data() + lo, 0, cnt * sizeof(U32));
            std::memset(G.waveOverusedStamp.data() + lo, 0, cnt * sizeof(U32));
            std::memset(G.waveOvClaimRem.data() + lo, 0, cnt * sizeof(I32));
            std::memset(G.waveCommitKey.data() + lo, 0, cnt * sizeof(U64));
        }
        bool localOk = true;
        I32 localMaxSll = 0;
        for (U32 i = lo; i < hi; ++i) {
            I32 ex = endTileX[i];
            I32 ey = endTileYRaw[i];
            I32 ty = tileY[i];
            I32 ln = length[i];
            I32 nt = nodeType[i];
            I32 tt = tileType[i];
            I32 ic = intentCode[i];
            float bc = baseCost[i];
            if (ex < -32768 || ex > 32767
                    || ey < -32768 || ey > 32767
                    || ty < -32768 || ty > 32767
                    || ln < 0 || ln > 65535
                    || nt < 0 || static_cast<U32>(nt) >= types || nt > 255
                    || tt < 0 || static_cast<U32>(tt) >= tiles
                    || G.typeFlags[nt] > 255u
                    || bc < 0.0f || std::isnan(bc)) {
                fprintf(stdout, "FIDES_NATIVE_CPU v29 geom bounds exceeded at i=%u x=%d y=%d ty=%d len=%d nt=%d tt=%d\n",
                        i, ex, ey, ty, ln, nt, tt);
                fflush(stdout);
                localOk = false;
                break;
            }
            NodeGeomHot& gh = G.geomHot[i];
            NodeGeomCold& gc = G.geomCold[i];
            gh.endTileX = static_cast<I16>(ex);
            gh.endTileYRaw = static_cast<I16>(ey);
            gh.tileY = static_cast<I16>(ty);
            gh.length = static_cast<U16>(ln);
            gh.typeFlags = static_cast<uint8_t>(G.typeFlags[nt]);
            gh.nodeType = static_cast<uint8_t>(nt);
            gh.baseCost = bc;
            gc.tileId = tileId[i];
            gc.endSlrRaw = -1;
            gc.endSlrTileY = -1;

            U16 pf = 0;
            if (static_cast<U32>(nt) == ordSll) {
                pf |= PF_SLL;
                localMaxSll = std::max(localMaxSll, fides_absInt(ey - ty));
            }
            if (G.tileFlags[tt] & 1u) pf |= PF_LAGUNA_TILE;
            if (G.tileFlags[tt] & 2u) pf |= PF_CLB_TILE;
            if (static_cast<U32>(tt) == ordIntTile) pf |= PF_INT_TILE;
            if (static_cast<U32>(ic) == ordPinbounce) pf |= PF_PINBOUNCE;
            if (static_cast<U32>(ic) == ordPinfeed) pf |= PF_PINFEED;
            if (static_cast<U32>(ic) == ordCleOut) pf |= PF_CLE_OUTPUT;
            if (delay[i] > 10000.0f) pf |= PF_UTURN_DELAY;
            gh.preFlags = pf;

            const U32 cf = G.typeFlags[nt];
            const bool seg1Cand = ((((cf & Local) != 0u) && ((cf & Leading) == 0u))
                                   || (((cf & Exclusive) != 0u) && ((pf & PF_PINBOUNCE) == 0u)))
                                  && ex >= -8192 && ex < 24576 && ey >= -8192 && ey < 24576;
            const U32 px = (ex >= -8192 && ex < 24576) ? (static_cast<U32>(ex + 8192) & 0x7FFFu) : 0u;
            const U32 py = (ey >= -8192 && ey < 24576) ? (static_cast<U32>(ey + 8192) & 0x7FFFu) : 0u;
            seg1Pack[i] = (seg1Cand ? 0x80000000u : 0u) | px | (py << 15);

            G.overusedPos[i] = -1;
            G.lastCommitNet[i] = UINT32_MAX;
        }
        if (!localOk) boundsOk.store(false, std::memory_order_relaxed);
        I32 prev = maxSllSpan.load(std::memory_order_relaxed);
        while (localMaxSll > prev && !maxSllSpan.compare_exchange_weak(prev, localMaxSll, std::memory_order_relaxed)) {}
    });
    if (!boundsOk.load(std::memory_order_relaxed)) return false;
    G.maxSllSpanY = std::max<I32>(4, maxSllSpan.load(std::memory_order_relaxed));

    std::atomic<U64> seg1Total{0};
    parallelForU32(n, 36, [&](U32 lo, U32 hi) {
        U64 localSeg1 = 0;
        U32 tmpCore[256];
        U32 tmpSeg1[256];
        for (U32 u = lo; u < hi; ++u) {
            U32 e0 = G.offset[u], e1 = G.offset[u + 1u];
            U32 deg = e1 - e0;
            const NodeGeomHot& ug = G.geomHot[u];
            if (deg == 0u || deg > 255u || (ug.preFlags & PF_SLL)
                    || ug.endTileX < -8192 || ug.endTileX >= 24576
                    || ug.endTileYRaw < -8192 || ug.endTileYRaw >= 24576) {
                G.coreDeg[u] = (deg <= 255u) ? static_cast<uint8_t>(deg) : 255u;
                continue;
            }
            const uint32_t up = seg1Pack[u];
            const int upY = static_cast<int>((up >> 15) & 0x7FFFu);
            U32 coreLen = 0u;
            U32 seg1Len = 0u;
            for (U32 e = e0; e < e1; ++e) {
                U32 c = G.edge[e];
                const uint32_t cp = seg1Pack[c];
                if ((cp & 0x80000000u) != 0u
                        && ((cp ^ up) & 0x7FFFu) == 0u
                        && fides_absInt(static_cast<int>((cp >> 15) & 0x7FFFu) - upY) <= 1) {
                    tmpSeg1[seg1Len++] = c;
                } else {
                    tmpCore[coreLen++] = c;
                }
            }
            if (seg1Len == 0u) {
                G.coreDeg[u] = static_cast<uint8_t>(deg);
            } else if (coreLen >= 255u) {
                G.coreDeg[u] = 255u;
            } else {
                for (U32 k = 0u; k < coreLen; ++k) G.edge[e0 + k] = tmpCore[k];
                for (U32 k = 0u; k < seg1Len; ++k) G.edge[e0 + coreLen + k] = tmpSeg1[k];
                G.coreDeg[u] = static_cast<uint8_t>(coreLen);
                localSeg1 += seg1Len;
            }
        }
        seg1Total.fetch_add(localSeg1, std::memory_order_relaxed);
    });

    G.overusedList.clear();
    G.commitSeq = 1;
    G.gpuStampWaveEpoch = 0;
    G.lastTailFbCount = 0;
    G.microStuckRounds = 0;
    G.corrInit = false;
    G.snapReady = false;
    G.snapOffsets.clear();
    G.snapEntries.clear();
    G.snapDirtyFlag.clear();
    G.snapDirtyList.clear();
    G.snapCandStamp.clear();
    G.snapCandEpoch = 1u;
    G.lastRoundActive.clear();
    G.activeStreakFlag.clear();
    G.activeStreakList.clear();
    G.netScopes.reserve(1 << 21);
    S.ensureN(n);

    if (tryGpu) {
        G.gpuDirtyOccEpoch = 1u;
        G.gpuDirtyOccIds.reserve(1 << 20);
        G.gpuDirtyOccVals.reserve(1 << 20);
        G.waveOverusedEpoch = 1u;
        G.waveCommitEpoch = 1u;
        for (int ms = 0; ms < 1024; ++ms) G.megaCache[ms] = {UINT32_MAX, 0u, 0u};
    }

    g_staticPreinitOk = true;
    double elapsedSec = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    fprintf(stdout, "FIDES_NATIVE_CPU v67 preinit_static=true N=%u M=%u seg1=%llu (%.2f%%) time_s=%.2f\n",
            n, m, static_cast<unsigned long long>(seg1Total.load()),
            m ? (static_cast<double>(seg1Total.load()) * 100.0 / static_cast<double>(m)) : 0.0, elapsedSec);
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

    HugeBuf<I32> occTmp;
    occTmp.resizeNoInit(n);
    HugeBuf<float> histTmp;
    histTmp.resizeNoInit(n);
    env->GetIntArrayRegion(jOcc, 0, static_cast<jsize>(n), occTmp.data());
    env->GetFloatArrayRegion(jHist, 0, static_cast<jsize>(n), histTmp.data());
    G.overusedList.clear();
    I32 cap = static_cast<I32>(s_ord[CAPACITY]);
    {
        const int chunks = 36;
        std::vector<std::vector<U32>> ovShards(chunks);
        U32 step = (n + static_cast<U32>(chunks) - 1u) / static_cast<U32>(chunks);
        BoundaryPool::runChunks(chunks, [&](int c) {
            U32 lo = static_cast<U32>(c) * step;
            U32 hi = std::min(n, lo + step);
            auto& ovOut = ovShards[c];
            for (U32 id = lo; id < hi; ++id) {
                I32 ov = occTmp[id];
                G.occ[id] = ov;
                G.hist[id] = histTmp[id];
                if (ov > cap) ovOut.push_back(id);
            }
        });
        for (int c = 0; c < chunks; ++c) {
            for (U32 id : ovShards[c]) {
                G.overusedPos[id] = static_cast<I32>(G.overusedList.size());
                G.overusedList.push_back(id);
            }
        }
    }

    jsize pccLen = env->GetArrayLength(jPcc);
    G.pcc.resize(pccLen);
    env->GetFloatArrayRegion(jPcc, 0, pccLen, G.pcc.data());
    s_pccPtr = G.pcc.data();
    s_pccLen = static_cast<int>(G.pcc.size());

    const char* disableGpuEnv = std::getenv("FIDES_V37_DISABLE_GPU");
    I32 minGpuConns = 100000;
    if (const char* mcEnv = std::getenv("FIDES_V92_GPU_MIN_CONNS")) {
        int parsed = std::atoi(mcEnv);
        if (parsed >= 0) minGpuConns = static_cast<I32>(parsed);
    }
    const I32 expectedConns = g_expectedConnCount.load(std::memory_order_relaxed);
    const bool skipSmallGpu = (expectedConns >= 0 && expectedConns < minGpuConns);
    if (skipSmallGpu) {
        fprintf(stdout, "FIDES_V92_GPU_SKIP expected_conns=%d min=%d\n",
                static_cast<int>(expectedConns), static_cast<int>(minGpuConns));
        fflush(stdout);
    }
    bool tryGpu = !skipSmallGpu && (disableGpuEnv == nullptr || std::strcmp(disableGpuEnv, "1") != 0);
    HugeBuf<FidesGpuNodeHot> gpuHot;
    HugeBuf<FidesGpuDynNode> gpuDyn;
    if (tryGpu) {
        gpuHot.resizeNoInit(n);
        gpuDyn.resizeNoInit(n);
    }
    std::atomic<bool> boundsOk{true};
    parallelForU32(n, 36, [&](U32 lo, U32 hi) {
        bool localOk = true;
        for (U32 i = lo; i < hi; ++i) {
            NodeGeomHot& g = G.geomHot[i];
            NodeGeomCold& gc = G.geomCold[i];
            gc.endSlrRaw = slrAtY(g.endTileYRaw);
            gc.endSlrTileY = slrAtY(g.tileY);

            U16 pf = g.preFlags & static_cast<U16>(~(PF_LAGUNA_WIRE | PF_LAGUNA_NORTH | PF_LAGUNA_SOUTH));
            if (isLagWire(gc.tileId)) pf |= PF_LAGUNA_WIRE;
            int ld = lagDir(g.nodeType);
            if (ld & 1) pf |= PF_LAGUNA_NORTH;
            if (ld & 2) pf |= PF_LAGUNA_SOUTH;
            g.preFlags = pf;

            if (tryGpu && localOk) {
                if (g.endTileX < 0 || g.endTileX > 1023
                        || g.endTileYRaw < 0 || g.endTileYRaw > 2047
                        || g.tileY < 0 || g.tileY > 2047
                        || gc.tileId < 0 || gc.tileId > 4194303
                        || g.length > 1023
                        || g.nodeType > 31
                        || gc.endSlrRaw < -1 || gc.endSlrRaw > 6
                        || gc.endSlrTileY < -1 || gc.endSlrTileY > 6) {
                    localOk = false;
                } else {
                    U32 gpf = static_cast<U32>(pf) & 0x7FFu;
                    FidesGpuNodeHot h{};
                    h.w0 = (static_cast<U32>(g.endTileX) & 0x3FFu)
                         | ((static_cast<U32>(g.endTileYRaw) & 0x7FFu) << 10)
                         | ((static_cast<U32>(g.tileY) & 0x7FFu) << 21);
                    h.w1 = (static_cast<U32>(gc.tileId) & 0x3FFFFFu)
                         | ((static_cast<U32>(g.length) & 0x3FFu) << 22);
                    h.w2 = (gpf & 0x7FFu)
                         | ((static_cast<U32>(g.typeFlags) & 0x3Fu) << 11)
                         | ((static_cast<U32>(g.nodeType) & 0x1Fu) << 17)
                         | ((static_cast<U32>(gc.endSlrRaw + 1) & 0x7u) << 22)
                         | ((static_cast<U32>(gc.endSlrTileY + 1) & 0x7u) << 25);
                    h.baseCost = g.baseCost;
                    gpuHot[i] = h;
                    gpuDyn[i] = FidesGpuDynNode{G.occ[i], G.hist[i]};
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
        g_gpuInitBoundsOk = ok;
        g_gpuInitOk = false;
        if (ok) {
            std::lock_guard<std::mutex> lk(g_gpuInitMtx);
            g_pendingReqMaxGpus = reqMaxGpus;
            g_pendingGpuHot.swap(gpuHot);
            g_pendingGpuDyn.swap(gpuDyn);
            g_pendingGpuPcc = G.pcc;
            g_gpuInitPending = true;
            g_gpuInitThread = std::thread([]() {
                g_gpuInitOk = fidesGpuInitMulti(
                        G.n, G.m, G.offset.data(), G.edge.data(), G.coreDeg.data(),
                        g_pendingGpuHot.data(), g_pendingGpuDyn.data(),
                        G.ord.data(), G.sllLen,
                        G.nextLaguna.data(), G.prevLaguna.data(), static_cast<I32>(G.nextLaguna.size()),
                        g_pendingGpuPcc.data(), static_cast<I32>(g_pendingGpuPcc.size()),
                        g_pendingReqMaxGpus, &G.gpuMulti);
            });
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

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeSetDesignConnCount(
        JNIEnv*, jclass, jint connCount) {
    g_expectedConnCount.store(static_cast<I32>(connCount), std::memory_order_relaxed);
}

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
    s_pccPtr = G.pcc.data();
    s_pccLen = static_cast<int>(G.pcc.size());
    if (g_gpuInitPending) return;
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
    G.gpuOverflowed.assign(static_cast<size_t>(n), 0);

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

namespace {

inline I32 fidesFloorDiv4(I32 a) {
    return (a >= 0) ? (a / 4) : -((-a + 3) / 4);
}

struct NativeCoarseScratch {
    std::vector<I32> tags;
    std::vector<I32> distances;
    std::vector<I32> previous;
    std::vector<I32> heap;
    std::vector<I32> heapPos;
    I32 gen = 0;
    I32 sz = 0;

    void begin(I32 count) {
        if (static_cast<size_t>(count) > tags.size()) {
            size_t cap = std::max<size_t>(static_cast<size_t>(count), std::max<size_t>(128u, 2u * tags.size()));
            tags.assign(cap, 0);
            distances.resize(cap);
            previous.resize(cap);
            heap.resize(cap);
            heapPos.resize(cap);
            gen = 0;
        }
        if (++gen == 0) {
            std::fill(tags.begin(), tags.end(), 0);
            gen = 1;
        }
        sz = 0;
    }

    inline I32 dist(I32 cell) const {
        return (tags[cell] == gen) ? distances[cell] : std::numeric_limits<I32>::max();
    }

    inline bool less(I32 a, I32 b) const {
        I32 ca = distances[a], cb = distances[b];
        return ca < cb || (ca == cb && a < b);
    }

    inline void decrease(I32 cell, I32 cost, I32 from) {
        I32 idx;
        if (tags[cell] != gen) {
            tags[cell] = gen;
            idx = sz++;
        } else {
            idx = heapPos[cell];
        }
        distances[cell] = cost;
        previous[cell] = from;
        while (idx > 0) {
            I32 parent = (idx - 1) >> 1;
            I32 above = heap[parent];
            if (!less(cell, above)) break;
            heap[idx] = above;
            heapPos[above] = idx;
            idx = parent;
        }
        heap[idx] = cell;
        heapPos[cell] = idx;
    }

    inline I32 removeFirst() {
        I32 res = heap[0];
        heapPos[res] = -1;
        I32 last = heap[--sz];
        if (sz == 0) return res;
        I32 idx = 0;
        for (I32 child = 1; child < sz; child = 2 * idx + 1) {
            if (child + 1 < sz && less(heap[child + 1], heap[child])) ++child;
            if (!less(heap[child], last)) break;
            heap[idx] = heap[child];
            heapPos[heap[idx]] = idx;
            idx = child;
        }
        heap[idx] = last;
        heapPos[last] = idx;
        return res;
    }

    inline void relax(I32 from, I32 next, I32 cost, I32 occ) {
        I32 imp = cost + 1 + occ;
        if (imp < dist(next)) decrease(next, imp, from);
    }
};

}  // namespace

JNIEXPORT jintArray JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeExportPortals(
        JNIEnv* env, jclass, jintArray jSlrMin, jintArray jSlrMax) {
    if (!g_staticPreinitOk || G.slr.size() < 6) {
        return env->NewIntArray(0);
    }
    jsize slrCap = env->GetArrayLength(jSlrMin);
    std::vector<I32> slrMin(static_cast<size_t>(slrCap)), slrMax(static_cast<size_t>(slrCap));
    if (slrCap > 0) {
        env->GetIntArrayRegion(jSlrMin, 0, slrCap, slrMin.data());
        env->GetIntArrayRegion(jSlrMax, 0, slrCap, slrMax.data());
    }
    const I32 yLen = (G.slr[0] == 1) ? G.slr[2] : 0;
    const I32* yToSlr = (yLen > 0) ? (G.slr.data() + 6) : nullptr;
    const U32 ordNonlocal = s_ord[NONLOCAL];
    const U32 n = G.n;
    const U32 chunk = 1u << 20;
    const int numChunks = static_cast<int>(std::max<U32>(1u, (n + chunk - 1u) / chunk));

    struct PortalRec {
        I32 from, to;
        U64 cell;
        I32 ingressX, ingressY, sllX, sllRawY, exitX, exitY;
    };
    std::vector<std::vector<PortalRec>> chunkRecs(static_cast<size_t>(numChunks));
    BoundaryPool::runChunks(numChunks, [&](int ci) {
        U32 fromP = static_cast<U32>(ci) * chunk;
        U32 toP = std::min(n, fromP + chunk);
        std::unordered_map<U64, std::unordered_set<U64>> localSeen;
        auto& out = chunkRecs[static_cast<size_t>(ci)];
        for (U32 parent = fromP; parent < toP; ++parent) {
            const NodeGeomHot& pgh = G.geomHot[parent];
            if (pgh.nodeType != ordNonlocal || !(pgh.preFlags & PF_LAGUNA_TILE)) continue;
            I32 py = static_cast<I32>(pgh.endTileYRaw);
            I32 from = (py >= 0 && py < yLen) ? yToSlr[py] : -1;
            if (from < 0 || from >= slrCap || py < slrMin[from] || py > slrMax[from]) continue;
            U32 e0 = G.offset[parent], e1 = G.offset[parent + 1u];
            for (U32 edge = e0; edge < e1; ++edge) {
                U32 sll = G.edge[edge];
                const NodeGeomHot& sgh = G.geomHot[sll];
                if (!(sgh.preFlags & PF_SLL)) continue;
                I32 sy = (py == static_cast<I32>(sgh.endTileYRaw))
                        ? static_cast<I32>(sgh.tileY)
                        : static_cast<I32>(sgh.endTileYRaw);
                I32 to = (sy >= 0 && sy < yLen) ? yToSlr[sy] : -1;
                if (to < 0 || to >= slrCap || sy < slrMin[to] || sy > slrMax[to] || fides_absInt(from - to) != 1) {
                    continue;
                }
                U32 s0 = G.offset[sll], s1 = G.offset[sll + 1u];
                for (U32 outE = s0; outE < s1; ++outE) {
                    U32 child = G.edge[outE];
                    const NodeGeomHot& cgh = G.geomHot[child];
                    if (cgh.nodeType != ordNonlocal || !(cgh.preFlags & PF_LAGUNA_TILE)
                            || G.geomCold[parent].tileId == G.geomCold[child].tileId) {
                        continue;
                    }
                    I32 cy = static_cast<I32>(cgh.endTileYRaw);
                    I32 cSlr = (cy >= 0 && cy < yLen) ? yToSlr[cy] : -1;
                    if (cSlr != to || cy < slrMin[to] || cy > slrMax[to]) continue;
                    U64 dirKey = (static_cast<U64>(static_cast<U32>(from)) << 32) | static_cast<U32>(to);
                    U32 rows = ((static_cast<U32>(py / 4) & 0xFFFFu) << 16) | (static_cast<U32>(cy / 4) & 0xFFFFu);
                    U64 cellKey = (static_cast<U64>(static_cast<U32>(sgh.endTileX)) << 32) | static_cast<U64>(rows);
                    if (localSeen[dirKey].insert(cellKey).second) {
                        out.push_back(PortalRec{
                            from, to, cellKey,
                            static_cast<I32>(pgh.endTileX), py,
                            static_cast<I32>(sgh.endTileX), static_cast<I32>(sgh.endTileYRaw),
                            static_cast<I32>(cgh.endTileX), cy
                        });
                    }
                }
            }
        }
    });

    std::unordered_map<U64, std::unordered_set<U64>> globalSeen;
    std::vector<jint> flat;
    flat.reserve(16384u);
    for (int ci = 0; ci < numChunks; ++ci) {
        for (const PortalRec& pr : chunkRecs[static_cast<size_t>(ci)]) {
            U64 dirKey = (static_cast<U64>(static_cast<U32>(pr.from)) << 32) | static_cast<U32>(pr.to);
            if (globalSeen[dirKey].insert(pr.cell).second) {
                flat.push_back(pr.from);
                flat.push_back(pr.to);
                flat.push_back(pr.ingressX);
                flat.push_back(pr.ingressY);
                flat.push_back(pr.sllX);
                flat.push_back(pr.sllRawY);
                flat.push_back(pr.exitX);
                flat.push_back(pr.exitY);
            }
        }
    }
    jintArray outArr = env->NewIntArray(static_cast<jsize>(flat.size()));
    if (!flat.empty()) {
        env->SetIntArrayRegion(outArr, 0, static_cast<jsize>(flat.size()), flat.data());
    }
    return outArr;
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeInitCorridorGrid(
        JNIEnv* env, jclass,
        jint fLeft, jint fBottom, jint fRight, jint fTop,
        jint coarseLeft, jint coarseBottom, jint coarseHeight,
        jintArray jCoarseOcc, jbyteArray jConnCorrMode) {
    const size_t numConns = G.conns.size();
    G.corrBx0.assign(numConns, 0);
    G.corrBy0.assign(numConns, 0);
    G.corrBx1.assign(numConns, 0);
    G.corrBy1.assign(numConns, 0);
    G.corrBuse.assign(numConns, 0u);
    if (numConns == 0u) {
        G.corrInit = true;
        return;
    }

    jsize occLen = env->GetArrayLength(jCoarseOcc);
    std::vector<I32> coarseOcc(static_cast<size_t>(occLen));
    if (occLen > 0) env->GetIntArrayRegion(jCoarseOcc, 0, occLen, coarseOcc.data());
    std::vector<uint8_t> corrMode(numConns);
    env->GetByteArrayRegion(jConnCorrMode, 0, static_cast<jsize>(numConns), reinterpret_cast<jbyte*>(corrMode.data()));

    const size_t occSize = coarseOcc.size();
    auto weightAt = [&](I32 cx, I32 cy) -> I32 {
        size_t idx = static_cast<size_t>(cx - coarseLeft) * static_cast<size_t>(coarseHeight) + static_cast<size_t>(cy - coarseBottom);
        return (idx < occSize) ? coarseOcc[idx] : 0;
    };
    auto inRect = [](I32 l, I32 b, I32 r, I32 t, I32 x, I32 y) -> bool {
        return l < x && x < r && b < y && y < t;
    };

    const int chunks = std::max(1, std::min(36, static_cast<int>(std::thread::hardware_concurrency())));
    std::atomic<size_t> nextCursor{0};
    constexpr size_t CHUNK_SZ = 256u;
    BoundaryPool::runChunks(chunks, [&](int) {
        NativeCoarseScratch q;
        while (true) {
            size_t lo = nextCursor.fetch_add(CHUNK_SZ, std::memory_order_relaxed);
            if (lo >= numConns) break;
            size_t hi = std::min(numConns, lo + CHUNK_SZ);
            for (size_t i = lo; i < hi; ++i) {
                uint8_t mode = corrMode[i];
                if (mode != 1u && mode != 2u) continue;
                const ConnData& cd = G.conns[i];
                I32 sx = static_cast<I32>(G.geomHot[cd.src].endTileX);
                I32 sy = static_cast<I32>(G.geomHot[cd.src].endTileYRaw);
                I32 tx = static_cast<I32>(G.geomHot[cd.sink].endTileX);
                I32 ty = static_cast<I32>(G.geomHot[cd.sink].endTileYRaw);
                if (!inRect(fLeft, fBottom, fRight, fTop, sx, sy)
                        || !inRect(fLeft, fBottom, fRight, fTop, tx, ty)
                        || !inRect(cd.origXmin, cd.origYmin, cd.origXmax, cd.origYmax, sx, sy)
                        || !inRect(cd.origXmin, cd.origYmin, cd.origXmax, cd.origYmax, tx, ty)) {
                    continue;
                }
                if (mode == 1u) {
                    I32 l = std::max(cd.origXmin, std::min(sx, tx) - 4);
                    I32 b = std::max(cd.origYmin, std::min(sy, ty) - 4);
                    I32 r = std::min(cd.origXmax, std::max(sx, tx) + 4);
                    I32 t = std::min(cd.origYmax, std::max(sy, ty) + 4);
                    if (!(l == cd.origXmin && b == cd.origYmin && r == cd.origXmax && t == cd.origYmax)) {
                        G.corrBx0[i] = l;
                        G.corrBy0[i] = b;
                        G.corrBx1[i] = r;
                        G.corrBy1[i] = t;
                        G.corrBuse[i] = 1u;
                    }
                } else {
                    I32 ax = sx / 4, ay = sy / 4, zx = tx / 4, zy = ty / 4;
                    I32 left = std::max(fidesFloorDiv4(std::max(cd.origXmin, static_cast<I32>(fLeft)) + 1), std::min(ax, zx) - 32);
                    I32 bottom = std::max(fidesFloorDiv4(std::max(cd.origYmin, static_cast<I32>(fBottom)) + 1), std::min(ay, zy) - 32);
                    I32 right = std::min(fidesFloorDiv4(std::min(cd.origXmax, static_cast<I32>(fRight)) - 1), std::max(ax, zx) + 32);
                    I32 top = std::min(fidesFloorDiv4(std::min(cd.origYmax, static_cast<I32>(fTop)) - 1), std::max(ay, zy) + 32);
                    I32 w = right - left + 1;
                    I32 h = top - bottom + 1;
                    if (w <= 0 || h <= 0 || static_cast<I64>(w) * static_cast<I64>(h) > 200000LL) continue;
                    I32 start = (ax - left) * h + (ay - bottom);
                    I32 end = (zx - left) * h + (zy - bottom);
                    q.begin(w * h);
                    q.decrease(start, 0, -1);
                    while (q.sz != 0) {
                        I32 at = q.removeFirst();
                        if (at == end) break;
                        I32 x = at / h, y = at % h, cost = q.distances[at];
                        I32 col = left + x, row = bottom + y;
                        if (x != 0) q.relax(at, at - h, cost, weightAt(col - 1, row));
                        if (y != 0) q.relax(at, at - 1, cost, weightAt(col, row - 1));
                        if (y + 1 != h) q.relax(at, at + 1, cost, weightAt(col, row + 1));
                        if (x + 1 != w) q.relax(at, at + h, cost, weightAt(col + 1, row));
                    }
                    if (q.dist(end) == std::numeric_limits<I32>::max()) continue;
                    I32 x0 = zx, y0 = zy, x1 = zx, y1 = zy;
                    for (I32 at = end;; at = q.previous[at]) {
                        if (at < 0) break;
                        I32 x = left + at / h, y = bottom + (at % h);
                        x0 = std::min(x0, x);
                        y0 = std::min(y0, y);
                        x1 = std::max(x1, x);
                        y1 = std::max(y1, y);
                        if (at == start) break;
                    }
                    I32 tl = std::max(cd.origXmin, (x0 - 1) * 4 - 1);
                    I32 tb = std::max(cd.origYmin, (y0 - 1) * 4 - 1);
                    I32 tr = std::min(cd.origXmax, (x1 + 2) * 4);
                    I32 tt = std::min(cd.origYmax, (y1 + 2) * 4);
                    I64 tArea = static_cast<I64>(tr - tl - 1) * static_cast<I64>(tt - tb - 1);
                    I64 bArea = static_cast<I64>(cd.origXmax - cd.origXmin - 1) * static_cast<I64>(cd.origYmax - cd.origYmin - 1);
                    if (inRect(tl, tb, tr, tt, sx, sy) && inRect(tl, tb, tr, tt, tx, ty) && tArea <= bArea
                            && !(tl == cd.origXmin && tb == cd.origYmin && tr == cd.origXmax && tt == cd.origYmax)) {
                        G.corrBx0[i] = tl;
                        G.corrBy0[i] = tb;
                        G.corrBx1[i] = tr;
                        G.corrBy1[i] = tt;
                        G.corrBuse[i] = 1u;
                    }
                }
            }
        }
    });
    G.corrMode.swap(corrMode);
    G.corrInit = true;
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeSeedCrossSlrCorridors(
        JNIEnv* env, jclass,
        jintArray jIdx, jint count,
        jintArray jBx0, jintArray jBy0, jintArray jBx1, jintArray jBy1, jbyteArray jBuse) {
    if (!G.corrInit || count <= 0) return;
    std::vector<I32> idx(static_cast<size_t>(count)), bx0(static_cast<size_t>(count)),
            by0(static_cast<size_t>(count)), bx1(static_cast<size_t>(count)), by1(static_cast<size_t>(count));
    std::vector<uint8_t> buse(static_cast<size_t>(count));
    env->GetIntArrayRegion(jIdx, 0, count, idx.data());
    env->GetIntArrayRegion(jBx0, 0, count, bx0.data());
    env->GetIntArrayRegion(jBy0, 0, count, by0.data());
    env->GetIntArrayRegion(jBx1, 0, count, bx1.data());
    env->GetIntArrayRegion(jBy1, 0, count, by1.data());
    env->GetByteArrayRegion(jBuse, 0, count, reinterpret_cast<jbyte*>(buse.data()));
    const size_t n = G.conns.size();
    for (int k = 0; k < count; ++k) {
        size_t ci = static_cast<size_t>(idx[k]);
        if (ci < n && buse[k] != 0u) {
            G.corrBx0[ci] = bx0[k];
            G.corrBy0[ci] = by0[k];
            G.corrBx1[ci] = bx1[k];
            G.corrBy1[ci] = by1[k];
            G.corrBuse[ci] = buse[k];
        }
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
    ensureGpuInitJoined();
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
    if (jBoxXmin != nullptr && jBoxUse != nullptr) {
        env->GetIntArrayRegion(jBoxXmin, 0, activeCount, bx0.data());
        env->GetIntArrayRegion(jBoxYmin, 0, activeCount, by0.data());
        env->GetIntArrayRegion(jBoxXmax, 0, activeCount, bx1.data());
        env->GetIntArrayRegion(jBoxYmax, 0, activeCount, by1.data());
        env->GetByteArrayRegion(jBoxUse, 0, activeCount, reinterpret_cast<jbyte*>(buse.data()));
    } else if (G.corrInit) {
        parallelForU32(static_cast<U32>(activeCount), 36, [&](U32 lo, U32 hi) {
            for (U32 i = lo; i < hi; ++i) {
                U32 cIdx = static_cast<U32>(activeConns[i]);
                bx0[i] = G.corrBx0[cIdx];
                by0[i] = G.corrBy0[cIdx];
                bx1[i] = G.corrBx1[cIdx];
                by1[i] = G.corrBy1[cIdx];
                buse[i] = G.corrBuse[cIdx];
            }
        });
    } else {
        std::fill(buse.begin(), buse.end(), 0u);
    }

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
            if (__atomic_load_n(&G.occ[id], __ATOMIC_RELAXED) > cap) return true;
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
                        if (G.lastCommitSeq[nid] > sl.dispatchSeq && G.occ[nid] >= cap) {
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

    struct SharedSameDesc {
        U32 mode = 0u;
        U32 offset = 0u;
        U32 param = 0u;
        U64 bloom64 = 0ULL;
        U32 snapOff = 0u;
        uint8_t isSnap = 0u;
    };

    struct PendingMegaUpload {
        size_t wi;
        U32 netId;
        int slot;
        U32 capH;
        size_t packOff;
        U32 layoutVersion;
        U32 mask;
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
                                      uint8_t* usedMegaSlots, SharedSameDesc& outDesc,
                                      U32 slotBase, U32 slotCount, U32& slotClock,
                                      bool regionMode, int fixedDev, const std::vector<uint8_t>* sharedNetMask,
                                      std::vector<U32>& snapKeys, std::vector<I32>& snapVals) -> int {
        const bool lockShared = regionMode && sharedNetMask != nullptr && (*sharedNetMask)[netId] != 0u;
        U32 evictOld = UINT32_MAX;
        {
            std::unique_lock<std::mutex> lk;
            if (lockShared) lk = std::unique_lock<std::mutex>(g_regNetMtx[netId & 4095u]);
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
                    if (lockShared) {
                        outDesc.isSnap = 1u;
                        outDesc.snapOff = static_cast<U32>(snapKeys.size());
                        U64 bloom = 0ULL;
                        snapKeys.resize(outDesc.snapOff + len);
                        snapVals.resize(outDesc.snapOff + len);
                        for (U32 j = 0; j < len; ++j) {
                            U32 id = sc.smallIds[j];
                            snapKeys[outDesc.snapOff + j] = id;
                            snapVals[outDesc.snapOff + j] = sc.smallCounts[j];
                            bloom |= (1ULL << (mixId(id) & 63u));
                        }
                        outDesc.bloom64 = bloom;
                    }
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
                if (lockShared) {
                    outDesc.isSnap = 1u;
                    outDesc.snapOff = static_cast<U32>(snapKeys.size());
                    const U32 maskH = capH - 1u;
                    snapKeys.resize(outDesc.snapOff + capH, NetScopeData::EMPTY_KEY);
                    snapVals.resize(outDesc.snapOff + capH, 0);
                    U32* sk = snapKeys.data() + outDesc.snapOff;
                    I32* sv = snapVals.data() + outDesc.snapOff;
                    for (const ScopeSlot& sl : sc.slots) {
                        if (sl.key != NetScopeData::EMPTY_KEY && sl.count > 0) {
                            U32 idx = mixId(sl.key) & maskH;
                            while (sk[idx] != NetScopeData::EMPTY_KEY) idx = (idx + 1u) & maskH;
                            sk[idx] = sl.key;
                            sv[idx] = sl.count;
                        }
                    }
                }
                return 0;
            }
            int slot = -1;
            int slotEnd = static_cast<int>(slotBase + slotCount);
            I32& netMegaSlot = regionMode ? sc.megaSlotReg[fixedDev] : sc.megaSlot;
            if (netMegaSlot >= static_cast<int>(slotBase) && netMegaSlot < slotEnd
                    && G.megaCache[netMegaSlot].netId == netId) {
                slot = netMegaSlot;
            } else {
                for (U32 tries = 0; tries < slotCount; ++tries) {
                    int cand = static_cast<int>(slotBase + ((slotClock++) % slotCount));
                    if (!usedMegaSlots[cand]) { slot = cand; break; }
                }
            }
            if (slot < 0 || usedMegaSlots[slot]) {
                return -1;
            }
            usedMegaSlots[slot] = 1;
            std::vector<U32>& dirtyVec = regionMode ? sc.megaDirtyReg[fixedDev] : sc.megaDirtyIdx;
            if (G.megaCache[slot].netId != netId || G.megaCache[slot].layoutVersion != sc.layoutVersion) {
                U32 oldNet = G.megaCache[slot].netId;
                if (oldNet != netId && oldNet < G.netScopes.size()) {
                    evictOld = oldNet;
                }
                netMegaSlot = slot;
                U32 capH = static_cast<U32>(sc.slots.size());
                pendingUploads.push_back(PendingMegaUpload{wi, netId, slot, capH, totalPackWords, sc.layoutVersion, sc.mask});
                totalPackWords += static_cast<size_t>(capH);
                outDesc.mode = 3u;
                outDesc.offset = static_cast<U32>(slot);
                outDesc.param = sc.mask;
            } else if (!dirtyVec.empty()) {
                pendingPatchNets.push_back(PendingPatchNet{
                    netId, static_cast<U32>(slot), static_cast<U32>(dirtyVec.size()), 0u
                });
                outDesc.mode = 3u;
                outDesc.offset = static_cast<U32>(slot);
                outDesc.param = G.megaCache[slot].mask;
            } else {
                outDesc.mode = 3u;
                outDesc.offset = static_cast<U32>(slot);
                outDesc.param = G.megaCache[slot].mask;
            }
        }
        if (evictOld < G.netScopes.size()) {
            std::unique_lock<std::mutex> oldLk;
            if (regionMode && sharedNetMask != nullptr && (*sharedNetMask)[evictOld] != 0u) {
                oldLk = std::unique_lock<std::mutex>(g_regNetMtx[evictOld & 4095u]);
            }
            NetScopeData& osc = G.netScopes[evictOld];
            if (regionMode) {
                if (osc.megaSlotReg[fixedDev] == static_cast<I32>(outDesc.offset)) osc.megaSlotReg[fixedDev] = -1;
                osc.megaSyncTrackedReg[fixedDev] = false;
                osc.megaDirtyReg[fixedDev].clear();
            } else {
                if (osc.megaSlot == static_cast<I32>(outDesc.offset)) osc.megaSlot = -1;
                osc.megaSyncTracked = false;
                osc.megaDirtyIdx.clear();
            }
        }
        return 0;
    };

    auto populateNetSameDescBuf = [&](U32 netId, SharedSameDesc& desc, U32* sameKeysPtr, I32* sameValsPtr,
                                      const std::vector<U32>& snapKeys, const std::vector<I32>& snapVals) {
        if (desc.isSnap) {
            const U32 off = desc.offset;
            const U32 cnt = (desc.mode == 1u) ? desc.param : (desc.param + 1u);
            std::copy_n(snapKeys.data() + desc.snapOff, cnt, sameKeysPtr + off);
            std::copy_n(snapVals.data() + desc.snapOff, cnt, sameValsPtr + off);
            return;
        }
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
        q.sinkEndX = static_cast<I32>(G.geomHot[c.sink].endTileX);
        q.sinkEndY = static_cast<I32>(G.geomHot[c.sink].endTileYRaw);
        q.sinkTile = c.sinkTile;
        q.sinkType = c.sinkType;
        q.sinkSlr = static_cast<I32>(G.geomCold[c.sink].endSlrRaw);
        q.xCenter = c.xCenter;
        q.yCenter = c.yCenter;
        q.netSizeF = static_cast<float>(c.netSize);
        q.doubleHpwlF = static_cast<float>(c.doubleHpwl);
        q.invBiasDenom = 1.0f / (q.netSizeF * q.doubleHpwlF);
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

    struct GpuPassOut {
        std::vector<I32> baseHeavy;
        std::vector<I32> relaxed;
        std::vector<I32> fallback;
        std::vector<I32> mega;
        std::vector<U32> allDirtyIds;
        std::vector<U32> ovCandidates;
        I64 routed = 0, parCommits = 0, retries = 0, sameOk = 0;
        I64 baseH = 0, relH = 0, fb = 0, unrout = 0, pops = 0, skipped = 0;
        I64 gCommits = 0, gPops = 0, gNs = 0, gWaves = 0, gOv = 0, gFb = 0, gWideOk = 0, megaUp = 0;
        double pickS = 0.0, metaS = 0.0, packS = 0.0, p4aS = 0.0, p4bS = 0.0, p4cS = 0.0;
        double mPackS = 0.0, mGpuS = 0.0, mlS = 0.0, msS = 0.0, muS = 0.0, mvS = 0.0, mbS = 0.0;
        I64 mdN = 0, mpN = 0, mwTot = 0, mpTot = 0;
        U32 commitEpochEnd = 0u;
        U32 dirtyEpochEnd = 0u;
    };

    I64 gpuWideOk = 0;
    auto runGpuPassImpl = [&](const std::vector<I32>& passItems, int curBudget, int passTier,
                              const std::vector<I32>* cpuOverlapItems, bool forceTierB1,
                              int fixedDev, WaveThreadPool* wavePool,
                              U32 slotBase, U32 slotCount,
                              U32 commitEpochSeed, U32 dirtyEpochSeed,
                              const std::vector<uint8_t>* sharedNetMask,
                              GpuPassOut& out) {
        const bool useTierB1 = (passTier >= 2) || forceTierB1;
        const bool regionMode = (fixedDev >= 0);
        auto parRun = [&](size_t tot, size_t grain, auto&& fn) {
            if (wavePool) wavePool->parallelFor(tot, grain, fn);
            else EXEC.parallelFor(tot, grain, fn);
        };
        if (passItems.empty()) {
            if (!regionMode && cpuOverlapItems && !cpuOverlapItems->empty()) {
                G.gpuStampWaveEpoch = 0u;
                runDynamicPass(*cpuOverlapItems, curBudget, passTier);
            }
            return;
        }
        const bool usePreRip = false; // v35+: exact virtual oldPath subtraction (d_countOldPath) in Tier-A/B1
        bool cpuOverlapDone = false;

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
        std::vector<U32> netWaveStamp(numNets, 0u);
        std::vector<U32> netInWaveCnt(numNets, 0u);
        std::vector<I32> netWaveDescIdx(numNets, -1);
        U32 netWaveEpoch = 1u;
        U32 regSlotClock = 0u;
        U32 regCommitSeq = 0u;
        U32 regDirtySeq = 0u;
        std::vector<U32> regWaveDirtyIds;
        std::vector<I32> regWaveDirtyVals;
        std::vector<U32> regSharedSnapKeys;
        std::vector<I32> regSharedSnapVals;
        if (regionMode) {
            regWaveDirtyIds.reserve(65536u);
            regWaveDirtyVals.reserve(65536u);
            regSharedSnapKeys.reserve(131072u);
            regSharedSnapVals.reserve(131072u);
            out.allDirtyIds.reserve(passItems.size() * 6u);
        }

        // Preserve strict HPWL_FIRST wave ordering across regions
        std::vector<uint8_t> netCell;
        const bool useCellSpread = false;
        if (useCellSpread) {
            netCell.assign(numNets, 0u);
            I32 rX0 = std::numeric_limits<I32>::max(), rY0 = std::numeric_limits<I32>::max();
            I32 rX1 = std::numeric_limits<I32>::min(), rY1 = std::numeric_limits<I32>::min();
            std::vector<uint8_t> netCellSet(numNets, 0u);
            for (I32 aIdx : passItems) {
                const ConnData& cd = G.conns[activeConns[aIdx]];
                I32 cx = (cd.origXmin + cd.origXmax) >> 1;
                I32 cy = (cd.origYmin + cd.origYmax) >> 1;
                rX0 = std::min(rX0, cx); rX1 = std::max(rX1, cx);
                rY0 = std::min(rY0, cy); rY1 = std::max(rY1, cy);
            }
            I32 spanX = std::max<I32>(1, rX1 - rX0 + 1);
            I32 spanY = std::max<I32>(1, rY1 - rY0 + 1);
            for (I32 aIdx : passItems) {
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                U32 nid = G.conns[cIdx].netId;
                if (netCellSet[nid]) continue;
                netCellSet[nid] = 1u;
                const ConnData& cd = G.conns[cIdx];
                I32 cx = (cd.origXmin + cd.origXmax) >> 1;
                I32 cy = (cd.origYmin + cd.origYmax) >> 1;
                int bx = std::max(0, std::min(7, static_cast<int>(((cx - rX0) * 8) / spanX)));
                int by = std::max(0, std::min(7, static_cast<int>(((cy - rY0) * 8) / spanY)));
                netCell[nid] = static_cast<uint8_t>((bx << 3) | by);
            }
        }

        std::vector<I32> netItemHead(numNets, -1);
        std::vector<I32> netItemTail(numNets, -1);
        std::vector<I32> nextSameNetItem(static_cast<size_t>(activeCount), -1);
        std::vector<uint8_t> netInHeap(numNets, 0u);
        std::vector<U32> netVer(numNets, 0u);
        std::priority_queue<GpuReadyNet, std::vector<GpuReadyNet>, std::greater<GpuReadyNet>> readyHeap;
        std::vector<U32> waveRequeueNets;
        waveRequeueNets.reserve(8192);
        std::vector<U32> deferredCellNets;
        deferredCellNets.reserve(4096);
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

        const U32 pathCap = !useTierB1 ? 512u : 8192u;
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
            if (regionMode) {
                targetWave = (routeIter == 1) ? 5120u : 4096u;
            } else if (useTierB1) {
                targetWave = (G.gpuCount >= 3) ? 2048u : 1024u;
            } else if (routeIter == 3) {
                targetWave = (G.gpuCount >= 3) ? 8192u : 4096u;
            } else if (routeIter >= 4) {
                targetWave = (G.gpuCount >= 3) ? 4096u : 2048u;
            } else {
                targetWave = (G.gpuCount >= 3) ? std::min<U32>(12288u, static_cast<U32>(G.gpuCount) * 3072u)
                                               : 8192u;
            }
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
            deferredCellNets.clear();
            uint16_t cellPicked[64]{};
            const U32 cellCap = (useCellSpread && tw >= 512u) ? std::max<U32>(24u, tw / 32u) : tw;
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
                if (!seenNet && sc.isFlat && sc.liveCount > 8192u && megaWaveCnt >= (regionMode ? 512u : 1024u)) return 1;

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

            auto popOneFromNet = [&](U32 netId, U32 maxPerNetLimit) {
                while (netItemHead[netId] >= 0) {
                    I32 aIdx = popNetItem(netId);
                    --remainingItems;
                    int pr = tryPickOne(aIdx, maxPerNetLimit);
                    if (pr == 2) {
                        if (useCellSpread) ++cellPicked[netCell[netId]];
                        break;
                    }
                    if (pr == 1) {
                        pushNetItem(aIdx, true, false);
                        ++remainingItems;
                        break;
                    }
                }
                if (netItemHead[netId] >= 0) waveRequeueNets.push_back(netId);
            };

            // Pass A: pop 1 live connection per distinct net in strict HPWL_FIRST order (spread across 64 spatial micro-blocks)
            while (pickedActIdx.size() < tw && !readyHeap.empty()) {
                GpuReadyNet top = readyHeap.top();
                readyHeap.pop();
                U32 netId = top.netId;
                if (!netInHeap[netId] || top.ver != netVer[netId]) continue;
                netInHeap[netId] = 0u;
                --liveHeapNets;
                ++netVer[netId];
                if (useCellSpread && cellPicked[netCell[netId]] >= cellCap && deferredCellNets.size() < tw) {
                    deferredCellNets.push_back(netId);
                    continue;
                }
                popOneFromNet(netId, 1u);
            }

            for (size_t di = 0; di < deferredCellNets.size(); ++di) {
                U32 netId = deferredCellNets[di];
                if (pickedActIdx.size() < tw) {
                    popOneFromNet(netId, 1u);
                } else {
                    if (netItemHead[netId] >= 0) waveRequeueNets.push_back(netId);
                }
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
            if (regionMode) {
                netAssignedDev.assign(W, fixedDev);
            } else {
                netWeight.assign(W, 0);
                netQCount.assign(W, 0u);
                for (I32 aIdx : pickedActIdx) {
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    const ConnData& cd = G.conns[cIdx];
                    I32 wi = netWaveDescIdx[cd.netId];
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

            auto drainReadyToCpuFallback = [&](bool includePicked) {
                if (includePicked) {
                    out.fallback.insert(out.fallback.end(), pickedActIdx.begin(), pickedActIdx.end());
                    pickedActIdx.clear();
                }
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
                            ++out.skipped;
                        } else {
                            out.fallback.push_back(aIdx);
                        }
                    }
                }
            };

            if (!hasPrePicked) {
                if (passTier == 1 && remainingItems <= 64u) {
                    drainReadyToCpuFallback(false);
                    break;
                }

                auto tPick0 = std::chrono::steady_clock::now();
                pickAndLpt(targetWave, out.skipped, out.fallback);
                out.pickS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPick0).count();
                if (pickedActIdx.empty()) continue;
            }
            hasPrePicked = false;

            const U32 maxMidWaves = (routeIter == 3) ? 200u : 128u;
            if (passTier == 1 && routeIter >= 3
                    && (pickedActIdx.size() < ((G.gpuCount >= 3) ? 2048u : 1536u)
                        || pickedActIdx.size() < (targetWave / 2u)
                        || out.gWaves >= maxMidWaves)) {
                drainReadyToCpuFallback(true);
                break;
            }
            if (passTier == 3 && tierB1Mode == 0 && out.gWaves >= 1u
                    && pickedActIdx.size() < 64u && (remainingItems + pickedActIdx.size()) < 96u) {
                drainReadyToCpuFallback(true);
                break;
            }

            // Phase 2A-meta: Mode 3 mega slots (1024 cache, parallel dirty sort/patch & batch upload) & per-GPU query offsets
            auto tMeta0 = std::chrono::steady_clock::now();
            megaPatches.clear();
            pendingUploads.clear();
            pendingPatchNets.clear();
            size_t totalPackWords = 0u;
            uint8_t usedMegaSlots[1024] = {0};
            const size_t W = waveUniqueNets.size();
            out.mwTot += static_cast<I64>(W);

            const int startDev = regionMode ? fixedDev : 0;
            const int endDev = regionMode ? (fixedDev + 1) : numDevs;
            auto isNetShared = [&](U32 netId) -> bool {
                return regionMode && sharedNetMask != nullptr && (*sharedNetMask)[netId] != 0u;
            };
            if (regionMode) {
                regSharedSnapKeys.clear();
                regSharedSnapVals.clear();
            }
            waveNetDescs.resize(W);
            waveNetFailed.assign(W, 0u);
            for (int d = startDev; d < endDev; ++d) {
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
                                           pendingPatchNets, usedMegaSlots, waveNetDescs[wi],
                                           slotBase, slotCount, regSlotClock,
                                           regionMode, fixedDev, sharedNetMask,
                                           regSharedSnapKeys, regSharedSnapVals) != 0) {
                    waveNetFailed[wi] = 1u;
                }
            }
            auto tLoop1 = std::chrono::steady_clock::now();
            out.mlS += std::chrono::duration<double>(tLoop1 - tMeta0).count();

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
                out.mdN += static_cast<I64>(totalDirtyWave);
                auto sortDedupNet = [&](PendingPatchNet& pn) {
                    std::unique_lock<std::mutex> lk;
                    if (isNetShared(pn.netId)) lk = std::unique_lock<std::mutex>(g_regNetMtx[pn.netId & 4095u]);
                    NetScopeData& sc = G.netScopes[pn.netId];
                    std::vector<U32>& dVec = regionMode ? sc.megaDirtyReg[fixedDev] : sc.megaDirtyIdx;
                    std::sort(dVec.begin(), dVec.end());
                    dVec.erase(std::unique(dVec.begin(), dVec.end()), dVec.end());
                    pn.dirtySz = static_cast<U32>(dVec.size());
                };
                if (totalDirtyWave >= 2048u && pendingPatchNets.size() > 1u) {
                    parRun(pendingPatchNets.size(), 1u, [&](size_t lo, size_t hi) {
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
                out.mpN += static_cast<I64>(patchCursor);
                megaPatches.resize(patchCursor);
                auto emitPatches = [&](const PendingPatchNet& pn) {
                    std::unique_lock<std::mutex> lk;
                    if (isNetShared(pn.netId)) lk = std::unique_lock<std::mutex>(g_regNetMtx[pn.netId & 4095u]);
                    NetScopeData& sc = G.netScopes[pn.netId];
                    std::vector<U32>& dVec = regionMode ? sc.megaDirtyReg[fixedDev] : sc.megaDirtyIdx;
                    FidesGpuMegaPatch* dst = megaPatches.data() + pn.patchOff;
                    const U32 nP = static_cast<U32>(std::min<size_t>(pn.dirtySz, dVec.size()));
                    for (U32 j = 0u; j < nP; ++j) {
                        U32 idx = dVec[j];
                        dst[j] = FidesGpuMegaPatch{pn.slot, idx, sc.slots[idx].key, sc.slots[idx].count};
                    }
                    for (U32 j = nP; j < pn.dirtySz; ++j) {
                        dst[j] = FidesGpuMegaPatch{pn.slot, 0u, sc.slots[0].key, sc.slots[0].count};
                    }
                    dVec.clear();
                };
                if (patchCursor >= 2048u && pendingPatchNets.size() > 1u) {
                    parRun(pendingPatchNets.size(), 1u, [&](size_t lo, size_t hi) {
                        for (size_t i = lo; i < hi; ++i) emitPatches(pendingPatchNets[i]);
                    });
                } else {
                    for (size_t i = 0; i < pendingPatchNets.size(); ++i) emitPatches(pendingPatchNets[i]);
                }
                out.msS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tSort0).count();
            }
            tLoop1 = std::chrono::steady_clock::now();

            if (!pendingUploads.empty()) {
                auto tMP0 = std::chrono::steady_clock::now();
                megaPackKeys.resize(totalPackWords);
                megaPackVals.resize(totalPackWords);
                uploadChunks.clear();
                uploadDescs.clear();
                uploadDescs.reserve(pendingUploads.size());
                std::vector<size_t> sentUpIdx;
                sentUpIdx.reserve(pendingUploads.size());
                for (size_t ui = 0; ui < pendingUploads.size(); ++ui) {
                    const PendingMegaUpload& pm = pendingUploads[ui];
                    if (isNetShared(pm.netId)) {
                        std::lock_guard<std::mutex> lk(g_regNetMtx[pm.netId & 4095u]);
                        const NetScopeData& sc = G.netScopes[pm.netId];
                        if (sc.layoutVersion != pm.layoutVersion || sc.slots.size() != pm.capH) {
                            waveNetFailed[pm.wi] = 1u;
                            continue;
                        }
                        const ScopeSlot* slPtr = sc.slots.data();
                        U32* dstK = megaPackKeys.data() + pm.packOff;
                        I32* dstV = megaPackVals.data() + pm.packOff;
                        for (U32 j = 0u; j < pm.capH; ++j) {
                            dstK[j] = slPtr[j].key;
                            dstV[j] = slPtr[j].count;
                        }
                        uploadDescs.push_back(FidesGpuMegaUpload{pm.slot, dstK, dstV, pm.capH});
                        sentUpIdx.push_back(ui);
                    } else {
                        uploadDescs.push_back(FidesGpuMegaUpload{
                            pm.slot,
                            megaPackKeys.data() + pm.packOff,
                            megaPackVals.data() + pm.packOff,
                            pm.capH
                        });
                        sentUpIdx.push_back(ui);
                        for (U32 j0 = 0u; j0 < pm.capH; j0 += 16384u) {
                            U32 j1 = std::min<U32>(pm.capH, j0 + 16384u);
                            uploadChunks.push_back(MegaPackChunk{ui, j0, j1});
                        }
                    }
                }
                if (!uploadChunks.empty()) {
                    parRun(uploadChunks.size(), 1u, [&](size_t cLo, size_t cHi) {
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
                }
                auto tMG0 = std::chrono::steady_clock::now();
                out.mPackS += std::chrono::duration<double>(tMG0 - tMP0).count();
                bool upOk = uploadDescs.empty() ? true : (regionMode
                        ? fidesGpuUploadMegaScopesBatchDev(fixedDev, uploadDescs.data(), static_cast<U32>(uploadDescs.size()))
                        : fidesGpuUploadMegaScopesBatch(uploadDescs.data(), static_cast<U32>(uploadDescs.size())));
                out.mGpuS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tMG0).count();
                out.megaUp += static_cast<I64>(uploadDescs.size());
                if (upOk) {
                    for (size_t ui : sentUpIdx) {
                        const PendingMegaUpload& pm = pendingUploads[ui];
                        std::unique_lock<std::mutex> lk;
                        if (isNetShared(pm.netId)) lk = std::unique_lock<std::mutex>(g_regNetMtx[pm.netId & 4095u]);
                        NetScopeData& sc = G.netScopes[pm.netId];
                        if (sc.layoutVersion != pm.layoutVersion) {
                            waveNetFailed[pm.wi] = 1u;
                            G.megaCache[pm.slot] = {UINT32_MAX, 0u, 0u};
                            if (regionMode) {
                                if (sc.megaSlotReg[fixedDev] == pm.slot) sc.megaSlotReg[fixedDev] = -1;
                                sc.megaSyncTrackedReg[fixedDev] = false;
                                sc.megaDirtyReg[fixedDev].clear();
                            } else {
                                if (sc.megaSlot == pm.slot) sc.megaSlot = -1;
                                sc.megaSyncTracked = false;
                                sc.megaDirtyIdx.clear();
                            }
                            continue;
                        }
                        G.megaCache[pm.slot] = {pm.netId, pm.layoutVersion, pm.mask};
                        if (regionMode) {
                            sc.megaSlotReg[fixedDev] = pm.slot;
                            sc.megaDirtyReg[fixedDev].clear();
                            sc.megaSyncTrackedReg[fixedDev] = true;
                        } else {
                            sc.megaSlot = pm.slot;
                            sc.megaDirtyIdx.clear();
                            sc.megaSyncTracked = true;
                        }
                    }
                } else {
                    for (size_t ui : sentUpIdx) {
                        const PendingMegaUpload& pm = pendingUploads[ui];
                        waveNetFailed[pm.wi] = 1u;
                        G.megaCache[pm.slot] = {UINT32_MAX, 0u, 0u};
                        std::unique_lock<std::mutex> lk;
                        if (isNetShared(pm.netId)) lk = std::unique_lock<std::mutex>(g_regNetMtx[pm.netId & 4095u]);
                        if (regionMode) {
                            if (G.netScopes[pm.netId].megaSlotReg[fixedDev] == pm.slot) {
                                G.netScopes[pm.netId].megaSlotReg[fixedDev] = -1;
                            }
                        } else {
                            if (G.netScopes[pm.netId].megaSlot == pm.slot) G.netScopes[pm.netId].megaSlot = -1;
                        }
                    }
                }
            }

            auto tLive0 = std::chrono::steady_clock::now();
            out.muS += std::chrono::duration<double>(tLive0 - tLoop1).count();
            const size_t P = pickedActIdx.size();
            out.mpTot += static_cast<I64>(P);
            piLive.assign(P, 1u);
            if (routeIter > 1 && P > 0u) {
                parRun(P, 128u, [&](size_t lo, size_t hi) {
                    for (size_t pi = lo; pi < hi; ++pi) {
                        if (!shouldRouteLive(static_cast<U32>(activeConns[pickedActIdx[pi]]))) {
                            piLive[pi] = 0u;
                        }
                    }
                });
            }

            auto tBuild0 = std::chrono::steady_clock::now();
            out.mvS += std::chrono::duration<double>(tBuild0 - tLive0).count();
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
                    ++out.skipped;
                    continue;
                }
                I32 aIdx = pickedActIdx[pi];
                U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                U32 netId = G.conns[cIdx].netId;
                I32 dIdx = netWaveDescIdx[netId];
                if (waveNetFailed[dIdx]) {
                    if (pickedRipped[pi]) restoreRipped(cIdx);
                    out.fallback.push_back(aIdx);
                    ++out.gFb;
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
            out.mbS += std::chrono::duration<double>(tMeta1 - tBuild0).count();
            out.metaS += std::chrono::duration<double>(tMeta1 - tMeta0).count();

            if (waveActIdx.empty()) continue;

            auto tPack0 = std::chrono::steady_clock::now();
            U32 B = static_cast<U32>(waveActIdx.size());
            bool acqOk = true;
            for (int d = startDev; d < endDev; ++d) {
                if (devB[d] > 0u) {
                    if (!fidesGpuAcquireWaveBufsDev(d, devB[d], devSameCursor[d], outPathsWords, devOldCursor[d],
                                                    0u, &devBufs[d])) {
                        acqOk = false;
                        break;
                    }
                }
            }
            if (!acqOk) {
                out.packS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPack0).count();
                for (U32 k = 0; k < B; ++k) {
                    I32 aIdx = waveActIdx[k];
                    if (wasRipped[k]) restoreRipped(static_cast<U32>(activeConns[aIdx]));
                    out.fallback.push_back(aIdx);
                    ++out.gFb;
                }
                continue;
            }

            // Phase 2B (parallel): populate mode 1/2 sameNet tables directly into each GPU's pinned memory
            parRun(W, 64u, [&](size_t lo, size_t hi) {
                for (size_t wi = lo; wi < hi; ++wi) {
                    if (!waveNetFailed[wi]) {
                        int d = netAssignedDev[wi];
                        populateNetSameDescBuf(waveUniqueNets[wi], waveNetDescs[wi],
                                               devBufs[d].sameKeys, devBufs[d].sameVals,
                                               regSharedSnapKeys, regSharedSnapVals);
                    }
                }
            });

            // Phase 2C (parallel): pack queries + sort oldNodes directly into each GPU's pinned memory
            parRun(B, 128u, [&](size_t lo, size_t hi) {
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
            out.packS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tPack0).count();

            // Phase 3: Broadcast megaPatches + dirty occ deltas and launch concurrent Multi-GPU / Isolated-Region kernels
            auto tGpu0 = std::chrono::steady_clock::now();
            if (!megaPatches.empty()) {
                if (regionMode) {
                    fidesGpuApplyMegaPatchesDev(fixedDev, megaPatches.data(), static_cast<U32>(megaPatches.size()));
                } else {
                    fidesGpuApplyMegaPatches(megaPatches.data(), static_cast<U32>(megaPatches.size()));
                }
            }
            if (!regionMode) {
                flushGpuOccDeltas();
            } else if (!regWaveDirtyIds.empty()) {
                out.allDirtyIds.insert(out.allDirtyIds.end(), regWaveDirtyIds.begin(), regWaveDirtyIds.end());
                U32 dCnt = static_cast<U32>(regWaveDirtyIds.size());
                regWaveDirtyVals.resize(dCnt);
                const U32* dIds = regWaveDirtyIds.data();
                I32* dVals = regWaveDirtyVals.data();
                parRun(dCnt, 2048u, [&](size_t lo, size_t hi) {
                    for (size_t i = lo; i < hi; ++i) dVals[i] = G.occ[dIds[i]];
                });
                fidesGpuApplyOccDeltasDev(fixedDev, dIds, dVals, dCnt);
                regWaveDirtyIds.clear();
            }

            FidesGpuWaveParams wp{};
            wp.shareWeight = shareWeight;
            wp.rnodeCostWeight = rnodeCostWeight;
            wp.rnodeWLWeight = rnodeWLWeight;
            wp.estWlWeight = estWlWeight;
            wp.pccLen = static_cast<U32>(G.pcc.size());
            if (!useTierB1) {
                wp.bucketShift = 18u;
                wp.popBatch = 32u;
            } else {
                wp.bucketShift = 17u;
                wp.popBatch = 256u;
            }

            struct MultiLaunchCtx {
                bool useTierB1;
                int tierB1Mode;
                FidesGpuWaveParams wp;
                const U32* devB;
                const U32* devSameCursor;
                U32 outPathsWords;
                const U32* devOldCursor;
                U32* devUsedWords;
                bool* devOk;
            } mctx{useTierB1, tierB1Mode, wp, devB, devSameCursor, outPathsWords, devOldCursor, devUsedWords, devOk};

            bool launchedPrePick = false;
            I64 preSkippedLive = 0;
            std::vector<I32> preFallbackItems;
            std::thread prePickThread;
            const U32 preMaxMidWaves = (routeIter == 3) ? 200u : 128u;
            if (passTier == 1 && routeIter <= 4
                    && !(routeIter >= 3 && (out.gWaves + 1u) >= preMaxMidWaves)
                    && remainingItems > targetWave * 2u && liveHeapNets > 0) {
                launchedPrePick = true;
                prePickThread = std::thread([&]() {
                    pickAndLpt(targetWave, preSkippedLive, preFallbackItems);
                });
            }

            bool launchedCpuOverlap = false;
            std::thread cpuOverlapThread;
            if (!regionMode && passTier == 3 && tierB1Mode == 0 && !cpuOverlapDone
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

            if (regionMode) {
                if (devB[fixedDev] == 0u) {
                    devUsedWords[fixedDev] = 0u;
                    devOk[fixedDev] = true;
                } else if (!useTierB1) {
                    devOk[fixedDev] = fidesGpuRunTierADev(fixedDev, wp, devB[fixedDev], devSameCursor[fixedDev],
                                                          outPathsWords, devOldCursor[fixedDev], &devUsedWords[fixedDev]);
                } else {
                    devOk[fixedDev] = fidesGpuRunTierB1WideDev(fixedDev, wp, tierB1Mode, devB[fixedDev], devSameCursor[fixedDev],
                                                               outPathsWords, devOldCursor[fixedDev], &devUsedWords[fixedDev], nullptr);
                }
            } else {
                fidesGpuRunParallelDevs([](int d, void* u) {
                    auto* c = static_cast<MultiLaunchCtx*>(u);
                    if (c->devB[d] == 0u) {
                        c->devUsedWords[d] = 0u;
                        c->devOk[d] = true;
                        return;
                    }
                    if (!c->useTierB1) {
                        c->devOk[d] = fidesGpuRunTierADev(d, c->wp, c->devB[d], c->devSameCursor[d],
                                                          c->outPathsWords, c->devOldCursor[d], &c->devUsedWords[d]);
                    } else {
                        c->devOk[d] = fidesGpuRunTierB1WideDev(d, c->wp, c->tierB1Mode, c->devB[d], c->devSameCursor[d],
                                                               c->outPathsWords, c->devOldCursor[d], &c->devUsedWords[d], nullptr);
                    }
                }, &mctx);
            }
            out.gNs += std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now() - tGpu0).count();
            ++out.gWaves;

            if (launchedCpuOverlap && cpuOverlapThread.joinable()) {
                cpuOverlapThread.join();
                G.gpuStampWaveEpoch = 0u;
                cpuOverlapDone = true;
            }

            if (launchedPrePick && prePickThread.joinable()) {
                prePickThread.join();
                out.skipped += preSkippedLive;
                if (!preFallbackItems.empty()) {
                    out.fallback.insert(out.fallback.end(), preFallbackItems.begin(), preFallbackItems.end());
                }
                hasPrePicked = !pickedActIdx.empty();
            }

            bool anyGpuOk = false;
            for (int d = startDev; d < endDev; ++d) {
                if (devB[d] > 0u && devOk[d]) { anyGpuOk = true; break; }
            }
            if (!anyGpuOk) {
                for (U32 k = 0; k < B; ++k) {
                    I32 aIdx = waveActIdx[k];
                    if (wasRipped[k]) restoreRipped(static_cast<U32>(activeConns[aIdx]));
                    out.fallback.push_back(aIdx);
                    ++out.gFb;
                }
                continue;
            }

            // Phase 4A: parallel path validation + atomic U64 waveCommitKey claim
            auto tP4A0 = std::chrono::steady_clock::now();
            U32 curCommitEpoch = 0u;
            if (regionMode) {
                regCommitSeq += 2u;
                curCommitEpoch = commitEpochSeed + regCommitSeq;
                out.commitEpochEnd = curCommitEpoch;
            } else {
                if (!launchedCpuOverlap) {
                    if (++G.waveCommitEpoch == 0u) {
                        std::memset(G.waveCommitKey.data(), 0, G.waveCommitKey.size() * sizeof(U64));
                        G.waveCommitEpoch = 1u;
                    }
                }
                curCommitEpoch = G.waveCommitEpoch;
            }

            itemAct.assign(B, PA_WIN);
            itemSawSame.assign(B, 0u);
            parRun(B, 128u, [&](size_t lo, size_t hi) {
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
                    out.fallback.push_back(aIdx);
                    ++out.gFb;
                    continue;
                }
                const FidesGpuResult& res = devBufs[d].results[itemLk[k]];
                out.pops += res.pops;
                out.gPops += res.pops;
                G.lastPops[cIdx] = res.pops;

                switch (itemAct[k]) {
                    case PA_NO_PATH:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (regionMode) {
                            out.fallback.push_back(aIdx);
                            ++out.gFb;
                        } else if (bumpWiden(aIdx, 1)) {
                            retries.push_back(aIdx);
                        } else {
                            ++out.unrout;
                        }
                        break;
                    case PA_CROSSED_BUDGET:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (passTier == 1) {
                            out.baseHeavy.push_back(aIdx);
                            ++out.baseH;
                        } else if (passTier == 2) {
                            bumpWiden(aIdx, 3);
                            out.relaxed.push_back(aIdx);
                            ++out.relH;
                        } else {
                            G.gpuOverflowed[cIdx] = 1u;
                            out.fallback.push_back(aIdx);
                            ++out.gFb;
                        }
                        break;
                    case PA_HASH_OVERFLOW:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (tierB1Mode == 0) ++out.gOv;
                        if (passTier == 1) {
                            out.baseHeavy.push_back(aIdx);
                            ++out.baseH;
                        } else if (passTier == 2) {
                            out.relaxed.push_back(aIdx);
                            ++out.relH;
                        } else {
                            if (tierB1Mode < 2) {
                                wideRetryItems.push_back(aIdx);
                            } else {
                                G.gpuOverflowed[cIdx] = 1u;
                                out.fallback.push_back(aIdx);
                                ++out.gFb;
                            }
                        }
                        break;
                    case PA_BAD_FALLBACK:
                        if (wasRipped[k]) restoreRipped(cIdx);
                        out.fallback.push_back(aIdx);
                        ++out.gFb;
                        break;
                    case PA_CONFLICT:
                        ++out.retries;
                        if (wasRipped[k]) restoreRipped(cIdx);
                        if (retryStreak[aIdx] < 250) ++retryStreak[aIdx];
                        if (retryStreak[aIdx] >= 4) out.fallback.push_back(aIdx);
                        else retries.push_back(aIdx);
                        break;
                    case PA_WIN: {
                        if (itemSawSame[k]) ++out.sameOk;
                        U32 netId = G.conns[cIdx].netId;
                        if (commitNetHead[netId] < 0) {
                            waveCommitNets.push_back(netId);
                            commitNetHead[netId] = static_cast<I32>(k);
                            commitNetTail[netId] = static_cast<I32>(k);
                        } else {
                            nextCommitInNet[commitNetTail[netId]] = static_cast<I32>(k);
                            commitNetTail[netId] = static_cast<I32>(k);
                        }
                        ++out.routed;
                        ++out.parCommits;
                        ++out.gCommits;
                        if (tierB1Mode > 0) ++out.gWideOk;
                        break;
                    }
                }
            }
            out.p4aS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tP4A0).count();

            // Phase 4B (net-parallel scope update) + Phase 4C (shard node-disjoint parallel occ update)
            const size_t numCN = waveCommitNets.size();
            if (numCN > 0u) {
                auto tP4B0 = std::chrono::steady_clock::now();
                const size_t numSlices = std::min<size_t>(numCN, P4_SLICES);
                parRun(numSlices, 1u, [&](size_t sLo, size_t sHi) {
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
                            std::unique_lock<std::mutex> netLk;
                            if (isNetShared(netId)) netLk = std::unique_lock<std::mutex>(g_regNetMtx[netId & 4095u]);
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
                out.p4bS += std::chrono::duration<double>(tP4B1 - tP4B0).count();

                const bool trackDirty = G.gpuEnabled;
                U32 curDirtyEpoch = 0u;
                if (regionMode) {
                    regDirtySeq += 2u;
                    curDirtyEpoch = dirtyEpochSeed + regDirtySeq;
                    out.dirtyEpochEnd = curDirtyEpoch;
                } else {
                    curDirtyEpoch = G.gpuDirtyOccEpoch;
                }
                parRun(P4_SHARDS, 1u, [&](size_t bLo, size_t bHi) {
                    for (size_t b = bLo; b < bHi; ++b) {
                        auto& dOut = shardDirtyIds[b];
                        auto& ovOut = shardOvEvents[b];
                        dOut.clear();
                        ovOut.clear();
                        for (size_t s = 0; s < numSlices; ++s) {
                            auto& vec = shardSliceDeltas[b * P4_SLICES + s];
                            for (const auto& kv : vec) {
                                const U32 id = kv.id;
                                const I32 before = G.occ[id];
                                const I32 after = before + kv.delta;
                                __atomic_store_n(&G.occ[id], after, __ATOMIC_RELEASE);
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
                    if (regionMode) {
                        regWaveDirtyIds.reserve(regWaveDirtyIds.size() + addDirty);
                        for (size_t b = 0; b < P4_SHARDS; ++b) {
                            regWaveDirtyIds.insert(regWaveDirtyIds.end(), shardDirtyIds[b].begin(), shardDirtyIds[b].end());
                            shardDirtyIds[b].clear();
                        }
                    } else {
                        G.gpuDirtyOccIds.reserve(G.gpuDirtyOccIds.size() + addDirty);
                        for (size_t b = 0; b < P4_SHARDS; ++b) {
                            G.gpuDirtyOccIds.insert(G.gpuDirtyOccIds.end(), shardDirtyIds[b].begin(), shardDirtyIds[b].end());
                            shardDirtyIds[b].clear();
                        }
                    }
                }
                if (regionMode) {
                    for (size_t b = 0; b < P4_SHARDS; ++b) {
                        for (const auto& ev : shardOvEvents[b]) out.ovCandidates.push_back(ev.id);
                        shardOvEvents[b].clear();
                    }
                } else {
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
                }
                out.p4cS += std::chrono::duration<double>(std::chrono::steady_clock::now() - tP4B1).count();
            }

            if (!regionMode && passTier == 1 && activeCount >= 100000) {
                auto now = std::chrono::steady_clock::now();
                double sinceLast = std::chrono::duration<double>(now - lastLogTime).count();
                if (routedCount + out.routed >= nextLogRouted || sinceLast >= 30.0) {
                    while (nextLogRouted <= routedCount + out.routed) nextLogRouted += 500000;
                    lastLogTime = now;
                    double elapsedS = std::chrono::duration<double>(now - tStarted).count();
                    fprintf(stdout,
                            "F4_V37_WAVE routed=%lld/%d skip=%lld gpu=%lld cpu=%lld retries=%lld same_ok=%lld overused=%zu pops=%lld gpu_pops=%lld gpu_ov=%lld gpu_fb=%lld gpu_s=%.2f t=%.1fs\n",
                            static_cast<long long>(routedCount + out.routed), activeCount,
                            static_cast<long long>(skippedLive + out.skipped),
                            static_cast<long long>(gpuCommits + out.gCommits),
                            static_cast<long long>((routedCount + out.routed) - (gpuCommits + out.gCommits)),
                            static_cast<long long>(conflictRetries + out.retries),
                            static_cast<long long>(sameNetOk + out.sameOk),
                            G.overusedList.size(), static_cast<long long>(totalPops + out.pops),
                            static_cast<long long>(gpuPops + out.gPops),
                            static_cast<long long>(gpuOverflows + out.gOv),
                            static_cast<long long>(gpuFallbacks + out.gFb),
                            (gpuNs + out.gNs) / 1e9, elapsedS);
                    fflush(stdout);
                }
            }
        }
            if (wideRetryItems.empty() || passTier != 3) break;
            const size_t minWideItems = (G.gpuCount >= 3) ? 16u : 12u;
            if (tierB1Mode >= 2 || wideRetryItems.size() < minWideItems) {
                for (I32 wa : wideRetryItems) {
                    G.gpuOverflowed[static_cast<U32>(activeConns[wa])] = 1u;
                }
                out.fallback.insert(out.fallback.end(), wideRetryItems.begin(), wideRetryItems.end());
                out.gFb += static_cast<I64>(wideRetryItems.size());
                wideRetryItems.clear();
                break;
            }
            curItems.swap(wideRetryItems);
            wideRetryItems.clear();
        }

        if (regionMode && !regWaveDirtyIds.empty()) {
            out.allDirtyIds.insert(out.allDirtyIds.end(), regWaveDirtyIds.begin(), regWaveDirtyIds.end());
            U32 dCnt = static_cast<U32>(regWaveDirtyIds.size());
            regWaveDirtyVals.resize(dCnt);
            const U32* dIds = regWaveDirtyIds.data();
            I32* dVals = regWaveDirtyVals.data();
            parRun(dCnt, 2048u, [&](size_t lo, size_t hi) {
                for (size_t i = lo; i < hi; ++i) dVals[i] = G.occ[dIds[i]];
            });
            fidesGpuApplyOccDeltasDev(fixedDev, dIds, dVals, dCnt);
            regWaveDirtyIds.clear();
        }

        if (!regionMode && !cpuOverlapDone && cpuOverlapItems != nullptr && !cpuOverlapItems->empty()) {
            G.gpuStampWaveEpoch = 0u;
            runDynamicPass(*cpuOverlapItems, curBudget, passTier);
        }
    };

    auto mergeGpuPassOut = [&](const GpuPassOut& o) {
        if (!o.baseHeavy.empty()) baseHeavyQueue.insert(baseHeavyQueue.end(), o.baseHeavy.begin(), o.baseHeavy.end());
        if (!o.relaxed.empty()) relaxedQueue.insert(relaxedQueue.end(), o.relaxed.begin(), o.relaxed.end());
        if (!o.mega.empty()) megaQueue.insert(megaQueue.end(), o.mega.begin(), o.mega.end());
        routedCount += o.routed;
        parallelCommits += o.parCommits;
        conflictRetries += o.retries;
        sameNetOk += o.sameOk;
        baseHeavyCount += o.baseH;
        relaxedHeavyCount += o.relH;
        fallbackCount += o.fb;
        unroutableCount += o.unrout;
        totalPops += o.pops;
        skippedLive += o.skipped;
        gpuCommits += o.gCommits;
        gpuPops += o.gPops;
        gpuNs += o.gNs;
        gpuWaves += o.gWaves;
        gpuOverflows += o.gOv;
        gpuFallbacks += o.gFb;
        gpuWideOk += o.gWideOk;
        megaUploadCount += o.megaUp;
        hostPickS += o.pickS;
        hostMetaS += o.metaS;
        hostPackS += o.packS;
        hostP4AS += o.p4aS;
        hostP4BS += o.p4bS;
        hostP4CS += o.p4cS;
        megaPackS += o.mPackS;
        megaGpuS += o.mGpuS;
        hostMetaLoopS += o.mlS;
        hostMetaSortS += o.msS;
        hostMetaUpWrapS += o.muS;
        hostMetaLiveS += o.mvS;
        hostMetaBuildS += o.mbS;
        hostMetaDirtyN += o.mdN;
        hostMetaPatchN += o.mpN;
        hostMetaWTot += o.mwTot;
        hostMetaPTot += o.mpTot;
    };

    auto finishFallbacks = [&](std::vector<I32>& fbItems, int curBudget, int passTier) {
        if (fbItems.empty()) return;
        if (passTier >= 2 && fbItems.size() > 1u) {
            std::sort(fbItems.begin(), fbItems.end(), [&](I32 a, I32 b) {
                return G.lastPops[activeConns[a]] > G.lastPops[activeConns[b]];
            });
        } else if (passTier == 1 && routeIter >= 3 && fbItems.size() > 1u) {
            std::sort(fbItems.begin(), fbItems.end());
        }
        runDynamicPass(fbItems, curBudget, passTier);
    };

    auto runGpuPass = [&](const std::vector<I32>& passItems, int curBudget, int passTier,
                          const std::vector<I32>* cpuOverlapItems = nullptr, bool forceTierB1 = false) {
        if (G.gpuEnabled && passTier == 1 && routeIter <= 2 && !forceTierB1
                && passItems.size() >= 32768u && G.gpuCount >= 2) {
            const size_t nPass = passItems.size();
            struct ItemBBox {
                I64 work;
                I32 qx0, qy0, qx1, qy1;
                I32 nx0, ny0, nx1, ny1;
                uint8_t validB;
                uint8_t cross;
                uint8_t live;
            };
            std::vector<ItemBBox> ib(nPass);
            EXEC.parallelFor(nPass, 4096u, [&](size_t lo, size_t hi) {
                for (size_t i = lo; i < hi; ++i) {
                    I32 aIdx = passItems[i];
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    if (!shouldRouteLive(cIdx)) {
                        ib[i].live = 0u;
                        continue;
                    }
                    const ConnData& cd = G.conns[cIdx];
                    ItemBBox b{};
                    b.live = 1u;
                    b.cross = (cd.flags & 8u) ? 1u : 0u;
                    b.work = (G.lastPops[cIdx] > 0)
                            ? G.lastPops[cIdx]
                            : static_cast<I64>(128 + std::min<I32>(256, cd.doubleHpwl));
                    bool useB = (buse[aIdx] != 0u);
                    bool useOrigB = (cd.flags & 2u) != 0u;
                    if (!useB && !useOrigB) {
                        b.validB = 0u;
                    } else {
                        b.validB = 1u;
                        b.qx0 = useB ? (useOrigB ? std::max(bx0[aIdx], cd.origXmin) : bx0[aIdx]) : cd.origXmin;
                        b.qy0 = useB ? (useOrigB ? std::max(by0[aIdx], cd.origYmin) : by0[aIdx]) : cd.origYmin;
                        b.qx1 = useB ? (useOrigB ? std::min(bx1[aIdx], cd.origXmax) : bx1[aIdx]) : cd.origXmax;
                        b.qy1 = useB ? (useOrigB ? std::min(by1[aIdx], cd.origYmax) : by1[aIdx]) : cd.origYmax;
                    }
                    const NodeGeomHot& gs = G.geomHot[cd.src];
                    const NodeGeomHot& gk = G.geomHot[cd.sink];
                    I32 nx0 = std::min<I32>({static_cast<I32>(gs.endTileX), static_cast<I32>(gk.endTileX), cd.sinkBeginX});
                    I32 nx1 = std::max<I32>({static_cast<I32>(gs.endTileX), static_cast<I32>(gk.endTileX), cd.sinkBeginX});
                    I32 ny0 = std::min<I32>({static_cast<I32>(gs.endTileYRaw), static_cast<I32>(gs.tileY),
                                             static_cast<I32>(gk.endTileYRaw), static_cast<I32>(gk.tileY), cd.sinkBeginY});
                    I32 ny1 = std::max<I32>({static_cast<I32>(gs.endTileYRaw), static_cast<I32>(gs.tileY),
                                             static_cast<I32>(gk.endTileYRaw), static_cast<I32>(gk.tileY), cd.sinkBeginY});
                    if (G.connRouted[cIdx]) {
                        const auto& op = G.connPaths[cIdx];
                        for (size_t j = 1; j + 1 < op.size(); ++j) {
                            const NodeGeomHot& gh = G.geomHot[op[j]];
                            I32 x = static_cast<I32>(gh.endTileX);
                            I32 y0 = std::min(static_cast<I32>(gh.endTileYRaw), static_cast<I32>(gh.tileY));
                            I32 y1 = std::max(static_cast<I32>(gh.endTileYRaw), static_cast<I32>(gh.tileY));
                            if (x < nx0) nx0 = x;
                            if (x > nx1) nx1 = x;
                            if (y0 < ny0) ny0 = y0;
                            if (y1 > ny1) ny1 = y1;
                        }
                    }
                    b.nx0 = nx0; b.ny0 = ny0; b.nx1 = nx1; b.ny1 = ny1;
                    ib[i] = b;
                }
            });

            std::vector<U32> livePos;
            livePos.reserve(nPass);
            I64 totalWork = 0;
            for (size_t i = 0; i < nPass; ++i) {
                if (ib[i].live) {
                    livePos.push_back(static_cast<U32>(i));
                    totalWork += ib[i].work;
                }
            }

            if (livePos.size() >= 32768u) {
                const I32 sllMargin = G.maxSllSpanY + 4;
                std::vector<I32> sampleX, sampleY;
                const size_t stride = std::max<size_t>(1u, livePos.size() / 200000u);
                sampleX.reserve((livePos.size() + stride - 1u) / stride);
                sampleY.reserve((livePos.size() + stride - 1u) / stride);
                for (size_t k = 0; k < livePos.size(); k += stride) {
                    const ItemBBox& b = ib[livePos[k]];
                    if (!b.validB) continue;
                    sampleX.push_back((b.qx0 + b.qx1) >> 1);
                    sampleY.push_back((b.qy0 + b.qy1) >> 1);
                }
                std::sort(sampleX.begin(), sampleX.end());
                std::sort(sampleY.begin(), sampleY.end());

                char bestAxis = 'X';
                I32 bestLo = 0, bestHi = 0;
                I32 bestCrossLo = 0, bestCrossHi = 0;
                I64 bestMinW = 0, bestW0 = 0, bestW1 = 0;
                size_t bestC0 = 0, bestC1 = 0;
                auto evalCuts = [&](char ax, const std::vector<I32>& samples) {
                    if (samples.size() < 64u) return;
                    for (int qi = 1; qi <= 9; ++qi) {
                        size_t idx = (samples.size() * static_cast<size_t>(qi)) / 10u;
                        I32 cut = samples[std::min(idx, samples.size() - 1u)];
                        I32 lo = cut - 1;
                        I32 hi = cut + 1;
                        I32 clo = cut - sllMargin;
                        I32 chi = cut + sllMargin;
                        I64 w0 = 0, w1 = 0;
                        size_t c0 = 0, c1 = 0;
                        for (size_t k = 0; k < livePos.size(); k += stride) {
                            const ItemBBox& b = ib[livePos[k]];
                            if (!b.validB) continue;
                            if (ax == 'X') {
                                if (b.qx1 <= lo && b.nx1 < lo) { w0 += b.work; ++c0; }
                                else if (b.qx0 >= hi && b.nx0 > hi) { w1 += b.work; ++c1; }
                            } else {
                                I32 mlo = b.cross ? clo : lo;
                                I32 mhi = b.cross ? chi : hi;
                                if (b.qy1 <= mlo && b.ny1 < mlo) { w0 += b.work; ++c0; }
                                else if (b.qy0 >= mhi && b.ny0 > mhi) { w1 += b.work; ++c1; }
                            }
                        }
                        I64 mnW = std::min(w0, w1) * static_cast<I64>(stride);
                        if (mnW > bestMinW) {
                            bestMinW = mnW;
                            bestAxis = ax;
                            bestLo = lo;
                            bestHi = hi;
                            bestCrossLo = clo;
                            bestCrossHi = chi;
                            bestW0 = w0 * static_cast<I64>(stride);
                            bestW1 = w1 * static_cast<I64>(stride);
                            bestC0 = c0 * stride;
                            bestC1 = c1 * stride;
                        }
                    }
                };
                evalCuts('X', sampleX);
                evalCuts('Y', sampleY);

                const size_t approxBound = (livePos.size() > bestC0 + bestC1) ? (livePos.size() - bestC0 - bestC1) : 0u;
                if (bestMinW * 4 >= totalWork && approxBound * 4u <= livePos.size()) {
                    skippedLive += static_cast<I64>(nPass - livePos.size());
                    std::vector<I32> reg0Items, reg1Items, boundItems;
                    reg0Items.reserve(livePos.size() / 2u);
                    reg1Items.reserve(livePos.size() / 2u);
                    boundItems.reserve(livePos.size() / 4u);
                    std::vector<uint8_t> netRegMask(numNets, 0u);
                    for (U32 pi : livePos) {
                        const ItemBBox& b = ib[pi];
                        I32 aIdx = passItems[pi];
                        U32 nid = G.conns[activeConns[aIdx]].netId;
                        uint8_t r = 2u;
                        if (b.validB) {
                            if (bestAxis == 'X') {
                                if (b.qx1 <= bestLo && b.nx1 < bestLo) r = 0u;
                                else if (b.qx0 >= bestHi && b.nx0 > bestHi) r = 1u;
                            } else {
                                I32 mlo = b.cross ? bestCrossLo : bestLo;
                                I32 mhi = b.cross ? bestCrossHi : bestHi;
                                if (b.qy1 <= mlo && b.ny1 < mlo) r = 0u;
                                else if (b.qy0 >= mhi && b.ny0 > mhi) r = 1u;
                            }
                        }
                        if (r == 0u) {
                            reg0Items.push_back(aIdx);
                            netRegMask[nid] |= 1u;
                        } else if (r == 1u) {
                            reg1Items.push_back(aIdx);
                            netRegMask[nid] |= 2u;
                        } else {
                            boundItems.push_back(aIdx);
                        }
                    }
                    std::vector<uint8_t> netShared(numNets, 0u);
                    for (U32 nid = 0; nid < numNets; ++nid) {
                        if (netRegMask[nid] == 3u) netShared[nid] = 1u;
                    }

                    auto clearAllMegaCache = [&]() {
                        for (int s = 0; s < 1024; ++s) {
                            U32 on = G.megaCache[s].netId;
                            if (on < numNets) {
                                NetScopeData& osc = G.netScopes[on];
                                osc.megaSlot = -1;
                                osc.megaSyncTracked = false;
                                osc.megaDirtyIdx.clear();
                                for (int r = 0; r < 2; ++r) {
                                    osc.megaSlotReg[r] = -1;
                                    osc.megaSyncTrackedReg[r] = false;
                                    osc.megaDirtyReg[r].clear();
                                }
                            }
                            G.megaCache[s] = {};
                        }
                    };

                    flushGpuOccDeltas();
                    clearAllMegaCache();

                    if (G.waveCommitEpoch >= 3800000000u) {
                        std::memset(G.waveCommitKey.data(), 0, G.waveCommitKey.size() * sizeof(U64));
                        G.waveCommitEpoch = 1u;
                    }
                    if (G.gpuDirtyOccEpoch >= 3800000000u) {
                        std::memset(G.gpuDirtyOccStamp.data(), 0, G.gpuDirtyOccStamp.size() * sizeof(U32));
                        G.gpuDirtyOccEpoch = 1u;
                    }
                    U32 commitBase = (G.waveCommitEpoch + 2u) & ~1u;
                    U32 dirtyBase = (G.gpuDirtyOccEpoch + 2u) & ~1u;

                    // Route mutually isolated reg0 || reg1 FIRST, preserving HPWL_FIRST local-before-long ordering
                    g_regPools[0].init(15);
                    g_regPools[1].init(15);
                    GpuPassOut out0, out1;
                    double reg0S = 0.0, reg1S = 0.0;
                    std::thread t0([&]() {
                        auto tw0 = std::chrono::steady_clock::now();
                        runGpuPassImpl(reg0Items, curBudget, passTier, nullptr, false,
                                       0, &g_regPools[0], 0u, 512u, commitBase + 0u, dirtyBase + 0u, &netShared, out0);
                        reg0S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tw0).count();
                    });
                    std::thread t1([&]() {
                        auto tw1 = std::chrono::steady_clock::now();
                        runGpuPassImpl(reg1Items, curBudget, passTier, nullptr, false,
                                       1, &g_regPools[1], 512u, 512u, commitBase + 1u, dirtyBase + 1u, &netShared, out1);
                        reg1S = std::chrono::duration<double>(std::chrono::steady_clock::now() - tw1).count();
                    });
                    t0.join();
                    t1.join();

                    auto tSync0 = std::chrono::steady_clock::now();
                    G.waveCommitEpoch = std::max<U32>(commitBase + 2u, std::max(out0.commitEpochEnd, out1.commitEpochEnd) + 2u);
                    G.gpuDirtyOccEpoch = std::max<U32>(dirtyBase + 2u, std::max(out0.dirtyEpochEnd, out1.dirtyEpochEnd) + 2u);

                    std::vector<U32> uniqIds0, uniqIds1;
                    std::vector<I32> uniqVals0, uniqVals1;
                    if (!out0.allDirtyIds.empty()) {
                        U32 ep0 = ++G.gpuDirtyOccEpoch;
                        uniqIds0.reserve(out0.allDirtyIds.size());
                        for (U32 id : out0.allDirtyIds) {
                            if (G.gpuDirtyOccStamp[id] != ep0) {
                                G.gpuDirtyOccStamp[id] = ep0;
                                uniqIds0.push_back(id);
                            }
                        }
                        uniqVals0.resize(uniqIds0.size());
                        EXEC.parallelFor(uniqIds0.size(), 4096u, [&](size_t lo, size_t hi) {
                            for (size_t i = lo; i < hi; ++i) uniqVals0[i] = G.occ[uniqIds0[i]];
                        });
                    }
                    if (!out1.allDirtyIds.empty()) {
                        U32 ep1 = ++G.gpuDirtyOccEpoch;
                        uniqIds1.reserve(out1.allDirtyIds.size());
                        for (U32 id : out1.allDirtyIds) {
                            if (G.gpuDirtyOccStamp[id] != ep1) {
                                G.gpuDirtyOccStamp[id] = ep1;
                                uniqIds1.push_back(id);
                            }
                        }
                        uniqVals1.resize(uniqIds1.size());
                        EXEC.parallelFor(uniqIds1.size(), 4096u, [&](size_t lo, size_t hi) {
                            for (size_t i = lo; i < hi; ++i) uniqVals1[i] = G.occ[uniqIds1[i]];
                        });
                    }
                    if (!uniqIds0.empty() || !uniqIds1.empty()) {
                        struct PeerSyncCtx {
                            const U32* ids0; const I32* vals0; U32 cnt0;
                            const U32* ids1; const I32* vals1; U32 cnt1;
                        } pctx{
                            uniqIds0.data(), uniqVals0.data(), static_cast<U32>(uniqIds0.size()),
                            uniqIds1.data(), uniqVals1.data(), static_cast<U32>(uniqIds1.size())
                        };
                        fidesGpuRunParallelDevs([](int d, void* u) {
                            auto* c = static_cast<PeerSyncCtx*>(u);
                            if (d == 1 && c->cnt0 > 0u) fidesGpuApplyOccDeltasDev(1, c->ids0, c->vals0, c->cnt0);
                            if (d == 0 && c->cnt1 > 0u) fidesGpuApplyOccDeltasDev(0, c->ids1, c->vals1, c->cnt1);
                        }, &pctx);
                    }
                    if (++G.gpuDirtyOccEpoch == 0u) {
                        std::memset(G.gpuDirtyOccStamp.data(), 0, G.gpuDirtyOccStamp.size() * sizeof(U32));
                        G.gpuDirtyOccEpoch = 1u;
                    }

                    auto applyOvCandidates = [&](const std::vector<U32>& cands) {
                        for (U32 id : cands) {
                            bool isOv = (G.occ[id] > cap);
                            I32 pos = G.overusedPos[id];
                            if (isOv && pos < 0) {
                                G.overusedPos[id] = static_cast<I32>(G.overusedList.size());
                                G.overusedList.push_back(id);
                            } else if (!isOv && pos >= 0) {
                                U32 backId = G.overusedList.back();
                                if (static_cast<size_t>(pos) + 1u < G.overusedList.size()) {
                                    G.overusedList[pos] = backId;
                                    G.overusedPos[backId] = pos;
                                }
                                G.overusedList.pop_back();
                                G.overusedPos[id] = -1;
                            }
                        }
                    };
                    applyOvCandidates(out0.ovCandidates);
                    applyOvCandidates(out1.ovCandidates);
                    clearAllMegaCache();
                    mergeGpuPassOut(out0);
                    mergeGpuPassOut(out1);
                    double syncS = std::chrono::duration<double>(std::chrono::steady_clock::now() - tSync0).count();

                    auto tBound0 = std::chrono::steady_clock::now();
                    GpuPassOut outB;
                    if (!boundItems.empty()) {
                        runGpuPassImpl(boundItems, curBudget, passTier, nullptr, false,
                                       -1, nullptr, 0u, 1024u, 0u, 0u, nullptr, outB);
                        mergeGpuPassOut(outB);
                    }
                    double boundS = std::chrono::duration<double>(std::chrono::steady_clock::now() - tBound0).count();

                    std::vector<I32> allFb;
                    allFb.reserve(out0.fallback.size() + out1.fallback.size() + outB.fallback.size());
                    allFb.insert(allFb.end(), out0.fallback.begin(), out0.fallback.end());
                    allFb.insert(allFb.end(), out1.fallback.begin(), out1.fallback.end());
                    allFb.insert(allFb.end(), outB.fallback.begin(), outB.fallback.end());
                    finishFallbacks(allFb, curBudget, passTier);

                    fprintf(stdout,
                            "F4_V95_SPATIAL_SPLIT iter=%d axis=%c cut=[%d,%d] reg0=%zu reg1=%zu bound=%zu reg0_s=%.2f reg1_s=%.2f sync_s=%.2f bound_s=%.2f\n",
                            routeIter, bestAxis, bestLo, bestHi,
                            reg0Items.size(), reg1Items.size(), boundItems.size(),
                            reg0S, reg1S, syncS, boundS);
                    fflush(stdout);
                    return;
                } else {
                    fprintf(stdout,
                            "F4_V95_SPLIT_SKIP iter=%d axis=%c cut=[%d,%d] c0=%zu c1=%zu cb=%zu w0=%lld w1=%lld totW=%lld\n",
                            routeIter, bestAxis, bestLo, bestHi,
                            bestC0, bestC1, approxBound,
                            static_cast<long long>(bestW0), static_cast<long long>(bestW1), static_cast<long long>(totalWork));
                    fflush(stdout);
                }
            }
        }

        GpuPassOut out;
        runGpuPassImpl(passItems, curBudget, passTier, cpuOverlapItems, forceTierB1,
                       -1, nullptr, 0u, 1024u, 0u, 0u, nullptr, out);
        mergeGpuPassOut(out);
        finishFallbacks(out.fallback, curBudget, passTier);
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
                ovCoords.push_back(OvCoord{static_cast<I32>(G.geomHot[id].endTileX), static_cast<I32>(G.geomHot[id].tileY)});
            }
            const U32 totalConns = static_cast<U32>(G.conns.size());
            std::vector<uint8_t> inAct(totalConns, 0u);
            for (U32 ci : clusterConns) inAct[ci] = 1u;
            int chunks = std::max(1, EXEC.numWorkers + 1);
            U32 step = (totalConns + chunks - 1u) / chunks;
            const bool microScope = (routeIter >= 58 && G.overusedList.size() <= 2u);
            const I32 maxNbrNet = 64;
            const I64 maxNbrPops = widenNbr ? 60000 : 50000;
            const I32 nbrDx = widenNbr ? 3 : 2;
            const I32 nbrDy = widenNbr ? 6 : 4;
            const size_t nbrLim = (routeIter >= 72 && G.lastTailFbCount > 0) ? 64u : (microScope ? 20u : (widenNbr ? 36u : 32u));
            struct NbrCand { I64 score; U32 connIdx; };
            std::vector<std::vector<NbrCand>> chunkCands(chunks);
            BoundaryPool::runChunks(chunks, [&](int c) {
                U32 lo = static_cast<U32>(c) * step;
                U32 hi = std::min(totalConns, lo + step);
                auto& out = chunkCands[c];
                for (U32 ci = lo; ci < hi; ++ci) {
                    if (inAct[ci] || !G.connRouted[ci]) continue;
                    const ConnData& cd = G.conns[ci];
                    if (!(cd.flags & 1u) || cd.doubleHpwl <= 0) continue;
                    const bool allowDirectBlockerNet = (G.overusedList.size() <= 4u && routeIter >= 54);
                    if (cd.netSize > (allowDirectBlockerNet ? 192 : maxNbrNet)
                            || G.lastPops[ci] > (allowDirectBlockerNet ? 120000 : maxNbrPops)) {
                        continue;
                    }
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
                    bool hitsOverNode = false;
                    const auto& p = G.connPaths[ci];
                    for (U32 nid : p) {
                        if (allowDirectBlockerNet && G.overusedPos[nid] >= 0) {
                            hitsOverNode = true;
                            bestDist = 0;
                            break;
                        }
                        I32 nx = static_cast<I32>(G.geomHot[nid].endTileX);
                        I32 ny = static_cast<I32>(G.geomHot[nid].tileY);
                        for (const OvCoord& oc : ovCoords) {
                            if (fides_absInt(nx - oc.x) <= nbrDx && fides_absInt(ny - oc.y) <= nbrDy) {
                                I32 d = fides_absInt(nx - oc.x) * 2 + fides_absInt(ny - oc.y);
                                if (d < bestDist) bestDist = d;
                            }
                        }
                    }
                    if (!hitsOverNode && (cd.netSize > maxNbrNet || G.lastPops[ci] > maxNbrPops)) continue;
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
                    mh = std::max(mh, G.hist[p[j]]);
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
        const int maxUnboundedThisRound = (routeIter >= 78) ? 8 : 1;
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
            const bool highPopsSkip2M4M = isAct && fs == 0u && routeIter >= 55
                    && overNow <= 8u && activeCount <= 48 && G.lastPops[connIdx] >= 2500000LL;
            I32 rc = 0;
            if (!isAct || (fs == 0u && !(microStalled && G.lastTailFbCount > 0) && !highPopsSkip2M4M)) {
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
            if (isAct && rc <= 0 && (escNbr || fs >= 1u || microStalled || highPopsSkip2M4M || (routeIter >= 62 && activeCount <= 24))) {
                I64 pops3 = 0;
                const bool allowUnboundedNow = (routeIter >= 74
                        || (microStalled && G.microStuckRounds >= 2)
                        || (routeIter >= 60 && fs >= 3u)
                        || (routeIter >= 62 && activeCount <= 24));
                if (allowUnboundedNow && unboundedUsedThisRound < maxUnboundedThisRound) {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  0, singlePath, pops3, -1);
                    if (pops3 > 4000000LL) ++unboundedUsedThisRound;
                } else if (microStalled) {
                    rc = runProbe(connIdx, rx0, ry0, rx1, ry1, false,
                                  shareWeight, rnodeCostWeight, rnodeWLWeight, estWlWeight,
                                  8000000, singlePath, pops3, 160);
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
                if (fs == 0u && !highPopsSkip2M4M) G.lastPops[connIdx] += pops3;
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

    const bool allowGpuPass1Mid = G.gpuEnabled
            && ((routeIter == 3 && activeCount >= 50000)
                || (routeIter == 4 && activeCount >= 100000));
    const I64 midGpuLpCut = (routeIter == 3) ? ((G.gpuCount >= 3) ? 1024LL : 1536LL) : 2048LL;
    const I64 pass2PopsCut = 36000LL;
    for (int i = 0; i < activeCount; ++i) {
        U32 connIdx = static_cast<U32>(activeConns[i]);
        I64 lp = G.lastPops[connIdx];
        uint8_t wl = G.connWiden[connIdx];
        const ConnData& cd = G.conns[connIdx];
        if (wavePopCeiling > 0 && (lp > BUDGET_BASE_HEAVY || (wl >= 3 && lp > 98304))) {
            relaxedQueue.push_back(i);
            ++relaxedHeavyCount;
        } else if (wavePopCeiling > 0 && (lp > pass2PopsCut || (wl >= 1 && lp > 16384))) {
            baseHeavyQueue.push_back(i);
            ++baseHeavyCount;
        } else if (!G.gpuEnabled) {
            cpuFastItems.push_back(i);
        } else if (routeIter <= 2) {
            if (routeIter == 1 || activeCount > 1024) {
                gpuPass1Items.push_back(i);
            } else {
                if (lp > 0 && lp < 200 && wl == 0 && cd.doubleHpwl <= 12) cpuFastItems.push_back(i);
                else gpuPass1Items.push_back(i);
            }
        } else if (allowGpuPass1Mid && (lp >= midGpuLpCut || wl >= 1u || (lp == 0 && cd.doubleHpwl >= 24))) {
            gpuPass1Items.push_back(i);
        } else {
            cpuFastItems.push_back(i);
        }
    }
    if (allowGpuPass1Mid && gpuPass1Items.size() < 8192u && !gpuPass1Items.empty()) {
        cpuFastItems.insert(cpuFastItems.end(), gpuPass1Items.begin(), gpuPass1Items.end());
        gpuPass1Items.clear();
        std::sort(cpuFastItems.begin(), cpuFastItems.end());
    }

    auto tP1_0 = std::chrono::steady_clock::now();
    if (routeIter >= 3) {
        if (!gpuPass1Items.empty()) {
            runGpuPass(gpuPass1Items, wavePopCeiling, 1);
        }
        if (!cpuFastItems.empty()) {
            runDynamicPass(cpuFastItems, wavePopCeiling, 1);
        }
    } else {
        if (!cpuFastItems.empty()) {
            runDynamicPass(cpuFastItems, wavePopCeiling, 1);
        }
        if (!gpuPass1Items.empty()) {
            runGpuPass(gpuPass1Items, wavePopCeiling, 1);
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
            const size_t minGpuPass3 = (G.gpuCount >= 4) ? 512u : 448u;
            if (G.gpuEnabled && pass3Items.size() >= minGpuPass3) {
                std::vector<U32> netPass3Cnt(numNets, 0u);
                std::vector<U32> netUltraCnt(numNets, 0u);
                const I64 ultraPopCut = (G.gpuCount >= 4) ? 4500000LL : 2800000LL;
                for (I32 aIdx : pass3Items) {
                    U32 cIdx = static_cast<U32>(activeConns[aIdx]);
                    U32 nid = G.conns[cIdx].netId;
                    ++netPass3Cnt[nid];
                    if (G.lastPops[cIdx] >= ultraPopCut
                            || G.gpuOverflowed[cIdx] != 0u) {
                        ++netUltraCnt[nid];
                    }
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
        I32 ov = G.occ[id] - cap;
        if (ov > 0) totalExcess += ov;
    }
    G.lastRoundActive.swap(activeConns);

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
    const float microMult = !microStuck ? 1.0f : ((G.microStuckRounds >= 2) ? 4.0f : 2.5f);
    if (microStuck) {
        fprintf(stdout, "F4_V68_MICRO_STUCK iter=%d over=%zu streak=%d mult=%.1f\n",
                routeIter, G.overusedList.size(), G.microStuckRounds, microMult);
        fflush(stdout);
    }
    for (U32 id : G.overusedList) {
        I32 ov = G.occ[id] - cap;
        if (ov > 0) {
            G.hist[id] += static_cast<float>(ov) * histFactor * microMult;
            if (G.gpuEnabled) {
                histIds.push_back(id);
                histVals.push_back(G.hist[id]);
            }
        }
    }
    if (G.gpuEnabled && !histIds.empty()) {
        fidesGpuApplyHistDeltas(histIds.data(), histVals.data(), static_cast<U32>(histIds.size()));
    }

    const bool midOrTail = (routeIter >= 6);
    const bool tailPhase = (routeIter >= 14) || (G.overusedList.size() <= 2200);
    U32 numConns = static_cast<U32>(G.conns.size());
    std::vector<jint> nextAct;
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
            if (G.corrInit && idx < G.corrBuse.size() && G.corrBuse[idx] != 0u && (extX != 0 || extY != 0)) {
                if (idx < G.corrMode.size() && G.corrMode[idx] == 1u) {
                    I32 sx = static_cast<I32>(G.geomHot[cd.src].endTileX);
                    I32 sy = static_cast<I32>(G.geomHot[cd.src].endTileYRaw);
                    I32 tx = static_cast<I32>(G.geomHot[cd.sink].endTileX);
                    I32 ty = static_cast<I32>(G.geomHot[cd.sink].endTileYRaw);
                    I32 l = std::max(cd.origXmin, std::min(sx, tx) - 4);
                    I32 b = std::max(cd.origYmin, std::min(sy, ty) - 4);
                    I32 r = std::min(cd.origXmax, std::max(sx, tx) + 4);
                    I32 t = std::min(cd.origYmax, std::max(sy, ty) + 4);
                    if (l == cd.origXmin && b == cd.origYmin && r == cd.origXmax && t == cd.origYmax) {
                        G.corrBuse[idx] = 0u;
                    } else {
                        G.corrBx0[idx] = l;
                        G.corrBy0[idx] = b;
                        G.corrBx1[idx] = r;
                        G.corrBy1[idx] = t;
                    }
                } else {
                    G.corrBuse[idx] = 0u;
                }
            }
        }
    };

    if (!G.overusedList.empty()) {
        int chunks = std::max(1, EXEC.numWorkers + 1);
        const bool useSparseHist = (routeIter >= 7 && G.overusedList.size() <= 16384u);
        if (useSparseHist) {
            constexpr U32 NB = GraphData::SNAP_BUCKETS;
            constexpr U32 MASK = NB - 1u;
            if (!G.snapReady || G.snapDirtyList.size() > 250000u) {
                std::vector<U32> counts(NB, 0u);
                U32 stepC = (numConns + static_cast<U32>(chunks) - 1u) / static_cast<U32>(chunks);
                BoundaryPool::runChunks(chunks, [&](int c) {
                    U32 lo = static_cast<U32>(c) * stepC;
                    U32 hi = std::min(numConns, lo + stepC);
                    for (U32 i = lo; i < hi; ++i) {
                        if (!G.connRouted[i]) continue;
                        for (U32 nid : G.connPaths[i]) {
                            __atomic_fetch_add(&counts[mixId(nid) & MASK], 1u, __ATOMIC_RELAXED);
                        }
                    }
                });
                G.snapOffsets.resize(static_cast<size_t>(NB) + 1u);
                U32 tot = 0u;
                for (U32 b = 0u; b < NB; ++b) {
                    G.snapOffsets[b] = tot;
                    tot += counts[b];
                }
                G.snapOffsets[NB] = tot;
                std::vector<U32> cursors(G.snapOffsets.begin(), G.snapOffsets.begin() + NB);
                G.snapEntries.resize(tot);
                BoundaryPool::runChunks(chunks, [&](int c) {
                    U32 lo = static_cast<U32>(c) * stepC;
                    U32 hi = std::min(numConns, lo + stepC);
                    for (U32 i = lo; i < hi; ++i) {
                        if (!G.connRouted[i]) continue;
                        for (U32 nid : G.connPaths[i]) {
                            U32 b = mixId(nid) & MASK;
                            U32 pos = __atomic_fetch_add(&cursors[b], 1u, __ATOMIC_RELAXED);
                            G.snapEntries[pos] = {nid, i};
                        }
                    }
                });
                G.snapDirtyFlag.assign(numConns, 0u);
                G.snapDirtyList.clear();
                for (U32 i = 0u; i < numConns; ++i) {
                    if (!G.connRouted[i]) {
                        G.snapDirtyFlag[i] = 1u;
                        G.snapDirtyList.push_back(i);
                    }
                }
                G.snapCandStamp.assign(numConns, 0u);
                G.snapCandEpoch = 1u;
                G.activeStreakFlag.assign(numConns, 0u);
                G.activeStreakList.clear();
                for (U32 i = 0u; i < numConns; ++i) {
                    if (G.consecCongested[i] != 0u) {
                        G.activeStreakFlag[i] = 1u;
                        G.activeStreakList.push_back(i);
                    }
                }
                G.snapReady = true;
            } else {
                for (I32 a : G.lastRoundActive) {
                    if (a < 0) continue;
                    U32 ci = static_cast<U32>(a);
                    if (ci < numConns && !G.snapDirtyFlag[ci]) {
                        G.snapDirtyFlag[ci] = 1u;
                        G.snapDirtyList.push_back(ci);
                    }
                }
            }

            if (++G.snapCandEpoch == 0u) {
                std::fill(G.snapCandStamp.begin(), G.snapCandStamp.end(), 0u);
                G.snapCandEpoch = 1u;
            }
            const U32 curEp = G.snapCandEpoch;
            std::vector<U32> candConns;
            candConns.reserve(G.snapDirtyList.size() + G.activeStreakList.size() + G.overusedList.size() * 4u);
            for (U32 ovId : G.overusedList) {
                U32 b = mixId(ovId) & MASK;
                U32 s0 = G.snapOffsets[b], s1 = G.snapOffsets[b + 1u];
                for (U32 p = s0; p < s1; ++p) {
                    const auto& e = G.snapEntries[p];
                    if (e.node == ovId && G.snapCandStamp[e.conn] != curEp) {
                        G.snapCandStamp[e.conn] = curEp;
                        candConns.push_back(e.conn);
                    }
                }
            }
            for (U32 ci : G.snapDirtyList) {
                if (G.snapCandStamp[ci] != curEp) {
                    G.snapCandStamp[ci] = curEp;
                    candConns.push_back(ci);
                }
            }
            for (U32 ci : G.activeStreakList) {
                if (G.snapCandStamp[ci] != curEp) {
                    G.snapCandStamp[ci] = curEp;
                    candConns.push_back(ci);
                }
            }

            const size_t candN = candConns.size();
            if (candN > 0u) {
                std::sort(candConns.begin(), candConns.end());
                int effChunks = (candN < 4096u) ? 1 : chunks;
                size_t step = (candN + static_cast<size_t>(effChunks) - 1u) / static_cast<size_t>(effChunks);
                std::vector<std::vector<jint>> chunkNext(effChunks);
                BoundaryPool::runChunks(effChunks, [&](int c) {
                    size_t lo = static_cast<size_t>(c) * step;
                    size_t hi = std::min(candN, lo + step);
                    auto& out = chunkNext[c];
                    for (size_t k = lo; k < hi; ++k) {
                        U32 i = candConns[k];
                        if (!G.connRouted[i]) {
                            out.push_back(static_cast<jint>(i));
                            enlargeConnOrigBox(i);
                            G.consecCongested[i] = (G.consecCongested[i] < 250) ? G.consecCongested[i] + 1 : 250;
                            promoteWidenFromStreak(i, G.consecCongested[i]);
                            continue;
                        }
                        bool cong = false;
                        const auto& path = G.connPaths[i];
                        for (U32 id : path) {
                            if (G.occ[id] > cap) {
                                cong = true;
                                out.push_back(static_cast<jint>(i));
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
                size_t totNext = 0u;
                for (int c = 0; c < effChunks; ++c) totNext += chunkNext[c].size();
                nextAct.reserve(totNext);
                for (int c = 0; c < effChunks; ++c) {
                    nextAct.insert(nextAct.end(), chunkNext[c].begin(), chunkNext[c].end());
                }
                size_t wIdx = 0u;
                for (size_t rIdx = 0u; rIdx < G.activeStreakList.size(); ++rIdx) {
                    U32 ci = G.activeStreakList[rIdx];
                    if (G.consecCongested[ci] != 0u) {
                        G.activeStreakList[wIdx++] = ci;
                    } else {
                        G.activeStreakFlag[ci] = 0u;
                    }
                }
                G.activeStreakList.resize(wIdx);
                for (jint ji : nextAct) {
                    U32 ci = static_cast<U32>(ji);
                    if (G.consecCongested[ci] != 0u && !G.activeStreakFlag[ci]) {
                        G.activeStreakFlag[ci] = 1u;
                        G.activeStreakList.push_back(ci);
                    }
                }
            }
        } else {
            G.snapReady = false;
            U32 step = (numConns + chunks - 1) / chunks;
            std::vector<std::vector<jint>> chunkNext(chunks);
            BoundaryPool::runChunks(chunks, [&](int c) {
                U32 lo = static_cast<U32>(c) * step;
                U32 hi = std::min(numConns, lo + step);
                auto& out = chunkNext[c];
                for (U32 i = lo; i < hi; ++i) {
                    if (!G.connRouted[i]) {
                        out.push_back(static_cast<jint>(i));
                        enlargeConnOrigBox(i);
                        G.consecCongested[i] = (G.consecCongested[i] < 250) ? G.consecCongested[i] + 1 : 250;
                        promoteWidenFromStreak(i, G.consecCongested[i]);
                        continue;
                    }
                    bool cong = false;
                    const auto& path = G.connPaths[i];
                    for (U32 id : path) {
                        if (G.occ[id] > cap) {
                            cong = true;
                            out.push_back(static_cast<jint>(i));
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
            std::vector<size_t> offsets(static_cast<size_t>(chunks) + 1u, 0u);
            for (int c = 0; c < chunks; ++c) {
                offsets[c + 1] = offsets[c] + chunkNext[c].size();
            }
            nextAct.resize(offsets[chunks]);
            if (offsets[chunks] > 0u) {
                BoundaryPool::runChunks(chunks, [&](int c) {
                    if (!chunkNext[c].empty()) {
                        std::memcpy(nextAct.data() + offsets[c], chunkNext[c].data(),
                                    chunkNext[c].size() * sizeof(jint));
                    }
                });
            }
        }
    } else {
        nextAct.reserve(1024u);
        for (U32 i = 0; i < numConns; ++i) {
            if (!G.connRouted[i]) {
                nextAct.push_back(static_cast<jint>(i));
                enlargeConnOrigBox(i);
            } else {
                G.consecCongested[i] = 0;
                G.connWiden[i] = 0;
            }
        }
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
        for (jsize i = 0; i < n; ++i) h[i] = G.hist[G.overusedList[i]];
        env->SetFloatArrayRegion(jOutHist, 0, n, h.data());
    }
    return len;
}

JNIEXPORT void JNICALL Java_com_xilinx_rapidwright_rwroute_FidesNativeCpu_nativeShutdownGpu(
        JNIEnv*, jclass) {
    ensureGpuInitJoined();
    if (G.gpuEnabled) {
        fidesGpuShutdown();
        G.gpuEnabled = false;
        fprintf(stdout, "FIDES_V37_GPU shutdown=true\n");
        fflush(stdout);
    }
}

}  // extern "C"
