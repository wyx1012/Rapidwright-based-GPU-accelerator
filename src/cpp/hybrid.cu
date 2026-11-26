#include <cuda_runtime.h>
#include <cub/device/device_scan.cuh>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <sstream>
#include "fides_arith.cuh"
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <functional>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <future>
#include <mutex>
#include <tuple>
#include <cerrno>
#include <sys/stat.h>
#include <vector>

using U32 = uint32_t;
using U64 = unsigned long long;
constexpr U32 Inf = 0x7f800000;
constexpr int Target = 1, Dominance = 2, Bloom = 4, Stale = 8;

struct alignas(16) Arc {
    U32 u, v, center, edge;

    // When denied, center holds the original reason, otherwise its exact float32 bits.
    __device__ __forceinline__ bool denied() const { return v == UINT32_MAX; }
};
static_assert(sizeof(Arc) == 16 && alignof(Arc) == 16, "Projected arc layout");
// Bits in Price::flags cache the immutable access class and path witness.
constexpr U32 PriceChanged = 1, BloomHit = 2, ArcDenied = 4;
struct Price { U32 first, second, flags; };
struct Weights { float share, node, wl; int netSize, hpwl; };
struct Mutable {
    const U32* offset;
    const U32* edge;
    int* occ;
    int* same;
    U32* hist;
    U32* pcc;
    unsigned char* dirty;
    U32 n;
    int pccSize;
};
struct UsageRow {
    U32 node;
    int32_t beforeOcc, beforeSame, afterOcc, afterSame;
};
static_assert(sizeof(UsageRow) == 20, "Private usage row ABI");

struct UsageView {
    const UsageRow* rows;
    U32 count;
    U32 first;
    U32 last;
};

struct UsageCounts { int occ, same; };

__device__ UsageCounts laneUsage(const Mutable& data, UsageView view, U32 node) {
    if (view.count && view.first <= node && node <= view.last) {
        U32 low = 0, high = view.count;
        while (low < high) {
            const U32 mid = low + (high - low) / 2;
            const U32 key = view.rows[mid].node;
            if (key == node) return {view.rows[mid].afterOcc, view.rows[mid].afterSame};
            if (key < node) low = mid + 1;
            else high = mid;
        }
    }
    return {data.occ[node], data.same[node]};
}

__global__ void checkUsageRows(Mutable data, UsageView view, U32* invalid) {
    const U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= view.count) return;
    const auto row = view.rows[i];
    if (row.node >= data.n
        || (i > 0 && view.rows[i - 1].node >= row.node)
        || row.beforeOcc <= 0 || row.beforeSame <= 0 || row.afterSame < 0
        || row.afterSame >= row.beforeSame
        || row.afterOcc != row.beforeOcc - (row.afterSame == 0)
        || data.occ[row.node] != row.beforeOcc || data.same[row.node] != row.beforeSame) {
        atomicMin(invalid, i);
    }
}

struct Delta { U32 node; int occ, same; U32 hist; };
static_assert(sizeof(Arc) == 16 && sizeof(Delta) == 16, "Compact projection or delta ABI");

constexpr unsigned MaxChannels = 8;
constexpr size_t GpuReserve = size_t(1) << 30;
struct Allocation { void* ptr; size_t bytes; };
thread_local std::vector<Allocation> allocations;
std::mutex allocationMutex;
size_t allocationBudget = 0;
size_t allocatedBytes = 0;
void publish(const std::string& path, const std::string& content);
U32 checkDeviceErrors(U32* errors);


enum Counter { Candidate, Rejected, Exact, FalseHit, Bound, Dominated, StaleCut,
               Atomic, Equal, Invalid, CounterCount };
enum CutoffCounter { SkippedSources, SkippedSourceArcs, SkippedEnqueues,
                     SkippedEnqueueArcs, CutoffCount };
// This is 2^94. The label bound 2^120 is a fixed point of round-to-nearest
// addition of any increment up to 2^94, so source-zero labels cannot overflow.
constexpr U32 SafeIncrement = 0x6e800000u;
constexpr U32 UnsafeCutoff = 1u << 31;

void need(bool pass, const char* message) {
    if (!pass) throw std::runtime_error(message);
}
void checkCuda(cudaError_t rc) {
    if (rc != cudaSuccess) throw std::runtime_error(cudaGetErrorString(rc));
}
class MemoryPressure : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

void needMemory(bool pass, const char* message) {
    if (!pass) throw MemoryPressure(message);
}

// Only used during cold initialization and the first live state upload.
bool v20Startup = false;
const char* v20Column = "context";

struct V20Column {
    const char* previous;
    explicit V20Column(const char* name) : previous(v20Column) { v20Column = name; }
    ~V20Column() { v20Column = previous; }
};

void v20Event(const char* api, size_t bytes, cudaError_t result = cudaSuccess) {
    if (!v20Startup) return;
    size_t freeBytes = 0, totalBytes = 0;
    uint64_t reserved = 0, used = 0;
    auto freeStatus = cudaMemGetInfo(&freeBytes, &totalBytes);
    cudaMemPool_t pool;
    auto poolStatus = cudaDeviceGetDefaultMemPool(&pool, 0);
    if (poolStatus == cudaSuccess) {
        poolStatus = cudaMemPoolGetAttribute(pool, cudaMemPoolAttrReservedMemCurrent, &reserved);
        if (poolStatus == cudaSuccess)
            poolStatus = cudaMemPoolGetAttribute(pool, cudaMemPoolAttrUsedMemCurrent, &used);
    }
    std::cerr << "V20_CUDA column=" << v20Column << " api=" << api
              << " bytes=" << bytes << " rc=" << static_cast<int>(result)
              << " free=" << freeBytes << " total=" << totalBytes
              << " pool_reserved=" << reserved << " pool_used=" << used
              << " free_rc=" << static_cast<int>(freeStatus)
              << " pool_rc=" << static_cast<int>(poolStatus) << std::endl;
}

void v20Check(const char* api, size_t bytes, cudaError_t status) {
    v20Event(api, bytes, status);
    if (status != cudaSuccess) {
        throw std::runtime_error(std::string("V20_CUDA ") + v20Column + " " + api
                                 + ": " + cudaGetErrorString(status));
    }
}

void* reserveDevice(size_t bytes) {
    std::lock_guard<std::mutex> guard(allocationMutex);
    v20Event("before-alloc", bytes);
    need(allocatedBytes <= allocationBudget, "GPU allocation accounting exceeds its budget");
    needMemory(bytes <= allocationBudget - allocatedBytes, "Concurrent GPU scratch would breach one-GiB reserve");
    size_t freeBytes, totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes, &totalBytes));
    needMemory(freeBytes >= GpuReserve && bytes <= freeBytes - GpuReserve, "GPU allocation has less than one-GiB headroom");
    void* ptr = nullptr;
    const auto status = cudaMallocAsync(&ptr, bytes, cudaStreamPerThread);
    if (status == cudaErrorMemoryAllocation) {
        // Clear this allocator error before stream cleanup. Other errors remain fatal.
        const auto last = cudaGetLastError();
        v20Event("cudaMallocAsync", bytes, status);
        if (last != cudaErrorMemoryAllocation) checkCuda(last);
        throw MemoryPressure("CUDA stream-ordered scratch allocation exhausted memory");
    }
    v20Event("cudaMallocAsync", bytes, status);
    checkCuda(status);
    allocatedBytes += bytes;
    return ptr;
}

void freeDevice(Allocation block) {
    std::lock_guard<std::mutex> guard(allocationMutex);
    checkCuda(cudaFreeAsync(block.ptr, cudaStreamPerThread));
    need(allocatedBytes >= block.bytes, "GPU allocation accounting underflow");
    allocatedBytes -= block.bytes;
}

cudaError_t copyInStream(void* to, const void* from, size_t bytes, cudaMemcpyKind kind) {
    cudaError_t result = cudaMemcpyAsync(to, from, bytes, kind, cudaStreamPerThread);
    return result == cudaSuccess && kind != cudaMemcpyDeviceToDevice
           ? cudaStreamSynchronize(cudaStreamPerThread) : result;
}

cudaError_t setInStream(void* to, int value, size_t bytes) {
    return cudaMemsetAsync(to, value, bytes, cudaStreamPerThread);
}

template<class T> T* allocate(size_t count) {
    const size_t bytes = count * sizeof(T);
    auto ptr = static_cast<T*>(reserveDevice(bytes));
    allocations.push_back({ptr, bytes});
    return ptr;
}
void releaseAfter(size_t mark) {
    while (allocations.size() > mark) {
        freeDevice(allocations.back());
        allocations.pop_back();
    }
}
template<class T> std::vector<T> readWords(std::ifstream& stream, size_t count) {
    std::vector<T> result(count);
    stream.read(reinterpret_cast<char*>(result.data()), count * sizeof(T));
    need(bool(stream), "Truncated graph");
    return result;
}
float fromBits(U32 bits) {
    float value;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}
__host__ __device__ U32 mix(U32 value) {
    value ^= value >> 16;
    value *= 0x7feb352d;
    value ^= value >> 15;
    value *= 0x846ca68b;
    return value ^ (value >> 16);
}
__host__ __device__ U32 hashPosition(U32 i, U32 salt, U32 mask) {
    return mix(i ^ salt) & mask;
}
__device__ bool maybeDenied(const U32* bloom, U32 mask, U32 id) {
    U32 a = hashPosition(id, 0x8ea7d5d1, mask), b = hashPosition(id, 0x13c6ef37, mask);
    return (bloom[a / 32] & (1u << (a % 32))) && (bloom[b / 32] & (1u << (b % 32)));
}


__device__ Price priceOf(const Arc& arc, const Mutable& data, const Weights& weights,
                        U32 node, int occ, int same, U32 baseBits, U32 length) {
    const float sharing = fides_sharingFactor(weights.share, same);
    const float nodeCost = fides_nodeCost(__uint_as_float(baseBits), weights.netSize,
            __uint_as_float(arc.center), static_cast<short>(weights.hpwl),
            __uint_as_float(data.hist[node]), __uint_as_float(data.pcc[occ - (same != 0)]), sharing);
    return {__float_as_uint(weights.node * nodeCost),
            __float_as_uint(weights.wl * length / sharing), 0};
}

__global__ void priceArcs(const Arc* arcs, const U32* ids, U32 m, Mutable data, Weights weights,
                          Price* prices, U32* invalid, UsageView usage,
                          const U32* base, const int* lengths, const U32* bloom, U32 bloomMask) {
    U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= m) return;
    const Arc arc = arcs[i];
    const U32 parent = ids[arc.u];
    if (parent >= data.n || arc.edge < data.offset[parent] || arc.edge >= data.offset[parent + 1]) {
        atomicAdd(invalid, 1u);
        return;
    }
    const U32 accessClass = maybeDenied(bloom, bloomMask, i) ? BloomHit : 0u;
    if (arc.denied()) {
        prices[i] = Price{0u, 0u, accessClass | ArcDenied};
        return;
    }
    const U32 node = data.edge[arc.edge];
    if (node >= data.n || node != ids[arc.v]) {
        atomicAdd(invalid, 1u);
        return;
    }
    const auto user = laneUsage(data, usage, node);
    const int occ = user.occ, same = user.same;
    const int pcc = occ - (same != 0);
    if (same < 0 || pcc < 0 || pcc >= data.pccSize || (data.dirty[node] && same <= 0)) {
        atomicAdd(invalid, 1u);
        return;
    }
    const U32 baseBits = base[node], length = static_cast<U32>(lengths[node]);
    Price result = priceOf(arc, data, weights, node, occ, same, baseBits, length);
    if (result.first > SafeIncrement || result.second > SafeIncrement) {
        atomicOr(invalid, UnsafeCutoff);
    }
    if (data.dirty[node]) {
        const Price stale = priceOf(arc, data, weights, node, occ - (same == 1), same - 1,
                                    baseBits, length);
        if (result.first != stale.first || result.second != stale.second) {
            result.flags |= PriceChanged;
        }
    }
    result.flags |= accessClass;
    prices[i] = result;
}

__global__ void applyDelta(Mutable data, const Delta* rows, U32 count, U32* errors) {
    U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= count) return;
    const Delta d = rows[i];
    if (d.node >= data.n || d.occ != data.occ[d.node] + (data.same[d.node] == 0)
            || d.same != data.same[d.node] + 1 || d.hist != data.hist[d.node]) {
        atomicAdd(errors, 1u);
        return;
    }
    data.occ[d.node] = d.occ;
    data.same[d.node] = d.same;
    data.dirty[d.node] = 1;
}


constexpr U32 FrontierLanes = 4;
constexpr U32 BucketShift = 20;
struct BucketProgress {
    U32 touched, frontier, pending, minKey;
};

__global__ void relaxFrontier(const Arc* arcs, const Price* prices, const U32* offset, U32 target,
                              U32 mode, const U32* frontier, U32 frontierCount, const U32* bucketFrontier,
                              const U32* best, const U32* prior, const U32* changedAt, U32 wave,
                              U32* proposals, U32* touched, U32* touchedCount, U64* counters,
                              U32* channelActivity, U32 channel, U32* incumbent, U64* cutoff) {
    if (bucketFrontier) frontierCount = *bucketFrontier;
    const U32 thread = blockIdx.x * blockDim.x + threadIdx.x;
    const U32 slot = thread / FrontierLanes;
    const U32 lane = threadIdx.x & 31u;
    const U32 groupLane = lane & (FrontierLanes - 1);
    const U32 groupMask = ((1u << FrontierLanes) - 1u) << (lane - groupLane);
    const U32 bound = (cutoff || (mode & Target)) ? best[target] : Inf;
    if (thread == 0) *incumbent = cutoff ? bound : Inf;
    U32 local[CounterCount] = {};
    if (slot < frontierCount) {
        const U32 u = frontier[slot];
        const U32 from = best[u];
        const U32 first = offset[u];
        const U32 end = offset[u + 1];
        if (cutoff && from > bound) {
            if (groupLane == 0) {
                atomicAdd(cutoff + SkippedSources, 1ull);
                atomicAdd(cutoff + SkippedSourceArcs, static_cast<U64>(end - first));
            }
        } else {
            if (channelActivity) {
                if (groupLane == 0) {
                    atomicAdd(channelActivity + channel, 1u);
                    for (U32 other = 0; other < MaxChannels; ++other) {
                        if (other != channel && atomicAdd(channelActivity + other, 0u) != 0) {
                            atomicOr(channelActivity + MaxChannels, (1u << channel) | (1u << other));
                        }
                    }
                }
                __syncwarp(groupMask);
            }
            for (U32 id = first + groupLane; id < end; id += FrontierLanes) {
                const Price price = prices[id];
                const bool denied = (price.flags & ArcDenied) != 0;
                ++local[Candidate];
                if (!(mode & Bloom)) {
                    ++local[Exact];
                    if (denied) {
                        ++local[Rejected];
                        continue;
                    }
                } else if ((price.flags & BloomHit)) {
                    ++local[Exact];
                    if (denied) {
                        ++local[Rejected];
                        continue;
                    }
                    ++local[FalseHit];
                }
                if (denied) {
                    ++local[Invalid];
                    continue;
                }
                const U32 v = arcs[id].v;
                U32 candidate = __float_as_uint(__fadd_rn(__fadd_rn(__uint_as_float(from),
                                                    __uint_as_float(price.first)), __uint_as_float(price.second)));
                U32 current = best[v];
                U32 old = changedAt[v] == wave ? prior[v] : current;
                if (candidate < from || candidate >= Inf || old < current) {
                    ++local[Invalid];
                    continue;
                }
                if ((mode & Target) && candidate > bound) {
                    ++local[Bound];
                    continue;
                }
                if ((mode & Stale) && candidate > old) {
                    ++local[StaleCut];
                    continue;
                }
                if ((mode & Dominance) && candidate > current) {
                    ++local[Dominated];
                    continue;
                }
                ++local[Atomic];
                U32 observed = atomicMin(&proposals[v], candidate);
                if (observed == Inf) touched[atomicAdd(touchedCount, 1u)] = v;
                if (candidate == min(observed, current)) ++local[Equal];
            }
            if (channelActivity) {
                __syncwarp(groupMask);
                if (groupLane == 0) atomicSub(channelActivity + channel, 1u);
            }
        }
    }
    // Inactive and cutoff groups contribute zero. Reduce counts once per full warp.
    #pragma unroll
    for (int i=0;i<CounterCount;++i) {
        U32 total=local[i];
        #pragma unroll
        for (int step=16;step;step>>=1) total+=__shfl_down_sync(0xffffffffu,total,step);
        if (lane==0 && total) atomicAdd(&counters[i],static_cast<U64>(total));
    }
}

__global__ void initializeWave(U32 n, U32 source, U32* best, U32* prior,
                               U32* proposals, U32* changedAt, U32* frontier, U64* counters) {
    const U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) {
        best[i] = i == source ? 0u : Inf;
        prior[i] = Inf;
        proposals[i] = Inf;
        changedAt[i] = 0u;
    }
    if (i < CounterCount + CutoffCount) counters[i] = 0;
    if (i == 0) frontier[0] = source;
}

__global__ void advanceFrontier(U32* best, U32* prior, U32* changedAt, U32 wave,
                                U32* proposals, const U32* touched, const U32* touchedCount,
                                U32 stateCount, U32* frontier, U32* frontierCount,
                                const U32* incumbent, const U32* offset, U64* cutoff) {
    const U32 count = *touchedCount;
    if (count > stateCount) return;
    const U32 stride = blockDim.x * gridDim.x;
    const U32 bound = *incumbent;
    U32 skipped = 0;
    U64 skippedArcs = 0;
    for (U32 i = blockIdx.x * blockDim.x + threadIdx.x; i < count; i += stride) {
        const U32 v = touched[i];
        const U32 proposed = proposals[v];
        proposals[v] = Inf;
        const U32 current = best[v];
        if (proposed < current) {
            prior[v] = current;
            best[v] = proposed;
            changedAt[v] = wave;
            if (cutoff && proposed > bound) {
                ++skipped;
                skippedArcs += offset[v + 1] - offset[v];
            } else {
                frontier[atomicAdd(frontierCount, 1u)] = v;
            }
        }
    }
    if (cutoff) {
        #pragma unroll
        for (int step = 16; step; step >>= 1) {
            skipped += __shfl_down_sync(0xffffffffu, skipped, step);
            skippedArcs += __shfl_down_sync(0xffffffffu, skippedArcs, step);
        }
        if ((threadIdx.x & 31) == 0 && skipped) {
            atomicAdd(cutoff + SkippedEnqueues, static_cast<U64>(skipped));
            atomicAdd(cutoff + SkippedEnqueueArcs, skippedArcs);
        }
    }
}



enum Ordinal { SLL, NONLOCAL, RESERVED, EAST, WEST, BOTH, EX_BOTH, EX_EAST, EX_WEST, EX_NONLOCAL,
               INT_TILE, PINFEED, PINBOUNCE, CLE_OUTPUT, SLL_INPUT, CAPACITY, OrdinalCount };
enum Flags { Local=1, East=2, West=4, Leading=8, Exclusive=16, Nonlocal=32 };
struct Access {
    const int *type, *intent, *tileType, *tile, *x, *y, *tileY, *length, *slr;
    const U32 *base, *delay, *typeFlags, *tileFlags, *ord;
};
struct Attempt {
    U32 source, sink;
    int xmin,ymin,xmax,ymax,box,uturn,cross,north,south,sinkX,sinkY,sinkTile,sinkType,sinkCtrl;
    U32 centerX, centerY, share, node, wl; int netSize, hpwl;
};
class NoPath : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct SearchWork {
    U32 vertices = 0, arcs = 0, waves = 0;
    U64 candidates = 0, atomics = 0;
};

struct ProjectionResult {
    U32 vertices, arcs, target, denied, error, failedParent, failedChild;
};
__device__ int endY(const Access& a, U32 child, U32 parent) {
    return a.type[child] == a.ord[SLL] && parent != UINT32_MAX && a.y[parent] == a.y[child]
            ? a.tileY[child] : a.y[child];
}
__device__ int slrIndex(const Access& a, U32 id, U32 parent) {
    const int y = endY(a,id,parent);
    return y >= 0 && y < a.slr[2] ? a.slr[6+y] : -1;
}
__device__ int lagunaDir(const Access& a, int type) {
    return a.slr[6+a.slr[2]+2*a.slr[3]+type];
}
__device__ bool lagunaWire(const Access& a, int tile) {
    return tile >= 0 && tile < a.slr[5] && a.slr[6+a.slr[2]+2*a.slr[3]+a.slr[4]+tile];
}
__device__ bool willOveruse(const Access& a, UsageCounts user) {
    const int cap = a.ord[CAPACITY];
    return user.occ > cap || (user.occ == cap && user.same == 0);
}
struct AccessDecision { bool allowed, valid; };
__device__ AccessDecision graphAccess(const Access& a, const Attempt& q, U32 parent, U32 entering,
                                      U32 child, int sinkSlr) {
    const U32 pt = a.type[parent], ct = a.type[child];
    const U32 pf = a.typeFlags[pt], cf = a.typeFlags[ct];
    const int childTile = a.tile[child];
    if (!(cf & Local)) {
        if (ct != a.ord[NONLOCAL]) return {true,true};
        if (pf & Local) {
            const bool pinfeed = a.intent[parent] == a.ord[PINFEED];
            if (a.tileFlags[a.tileType[child]] & 1) {
                if (!q.cross || slrIndex(a,child,parent) == sinkSlr) return {false,true};
                return {true, pinfeed && (pf & Leading)};
            }
            return {true, pinfeed && (a.tileFlags[a.tileType[child]] & 2)
                          && a.intent[child] == a.ord[CLE_OUTPUT]};
        }
        if (pt == a.ord[SLL]) {
            return entering != UINT32_MAX && a.tile[entering] == childTile
                    ? AccessDecision{false,true} : AccessDecision{true,entering != UINT32_MAX};
        }
        return {true,true};
    }
    bool valid = a.tileType[child] == a.ord[INT_TILE];
    if (q.cross && (cf & Leading) && slrIndex(a,child,parent) != sinkSlr) {
        const int dir = lagunaDir(a,ct);
        if (((dir & 1) && !q.north) || ((dir & 2) && !q.south)) return {false,true};
        const int x = a.x[child];
        return {true, valid && lagunaWire(a,childTile) && childTile != q.sinkTile
                      && x >= 0 && x < a.slr[3] && a.slr[6+a.slr[2]+x] == x};
    }
    if (q.sinkType == a.ord[EAST] || q.sinkType == a.ord[EX_EAST]) {
        if ((cf & West) || ct == a.ord[RESERVED]) return {false,true};
        valid &= q.sinkType != a.ord[EAST]; // This arm requires one exclusive target.
    } else if (q.sinkType == a.ord[WEST] || q.sinkType == a.ord[EX_WEST]) {
        if ((cf & East) || ct == a.ord[RESERVED]) return {false,true};
        valid &= q.sinkType != a.ord[WEST];
    } else if (q.sinkType == a.ord[EX_BOTH]) {
        if (childTile != q.sinkTile || ct != a.ord[BOTH]) return {false,true};
        valid &= q.sinkCtrl != 0;
    } else if (q.sinkType == a.ord[EX_NONLOCAL]) {
        return {false,true};
    } else {
        return {true,false};
    }
    return {childTile == q.sinkTile || (a.x[child] == q.sinkX
            && fides_absInt(endY(a,child,parent)-q.sinkY) <= 1), valid};
}
__device__ U32 denyReason(const Access& a, Mutable d, UsageView usage, const Attempt& q, U32 parent, U32 entering,
                          U32 child, int sinkSlr) {
    if (child == q.source) return 1;
    if (child != q.sink && q.box && !(a.x[child] > q.xmin && a.x[child] < q.xmax
                && a.y[child] > q.ymin && a.y[child] < q.ymax)) return 2;
    const U32 pt = a.type[parent], ct = a.type[child];
    const U32 pf = a.typeFlags[pt], cf = a.typeFlags[ct];
    const bool bounce = a.intent[child] == a.ord[PINBOUNCE];
    const auto user = laneUsage(d, usage, child);
    const bool overused = user.occ > a.ord[CAPACITY];
    if (child == q.sink) {
        return (cf & Exclusive) && (overused || willOveruse(a,user) || (user.same != 1 && !bounce))
                ? 4 : 0;
    }
    if (cf & Local) {
        const auto access = graphAccess(a,q,parent,entering,child,sinkSlr);
        if (!access.allowed) return 3;
        const bool exempt = (ct == a.ord[RESERVED] && a.type[q.sink] == a.ord[EX_BOTH])
                            || a.intent[child] == a.ord[SLL_INPUT];
        return access.valid && (exempt || ((!(pf & East) || (cf & East))
                                    && (!(pf & West) || (cf & West)))) ? 0 : 4;
    }
    if (cf & Nonlocal) {
        if ((pf & Local) && !(pf & Leading)) return 4;
        const auto access = graphAccess(a,q,parent,entering,child,sinkSlr);
        if (!access.allowed || (!q.uturn && __uint_as_float(a.delay[child]) > 10000.0f)) return 3;
        return access.valid ? 0 : 4;
    }
    if (cf & Exclusive) {
        if (user.same == 0 || !bounce) return 3;
        const bool parentOk = (ct != a.ord[EX_EAST] || pt == a.ord[EAST] || (pf & Leading))
                && (ct != a.ord[EX_WEST] || pt == a.ord[WEST] || (pf & Leading))
                && (ct != a.ord[EX_BOTH] || pt == a.ord[BOTH]);
        return parentOk && !overused && user.same > 0 && !willOveruse(a,user) ? 0 : 4;
    }
    if (ct == a.ord[SLL]) return q.cross && slrIndex(a,parent,entering) != sinkSlr ? 0 : 4;
    return 1;
}

constexpr U32 NewKey = 1u<<31;
constexpr U32 EmptyKey = UINT32_MAX;
constexpr U32 Unseen = UINT32_MAX;

__global__ void projectionDegrees(Mutable d, Attempt a, const U32* ids, U32 first,
                                  U32 width, U32* degrees) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>width) return;
    if (i==width || ids[first+i]==a.sink) {
        degrees[i]=0;
        return;
    }
    const U32 id=ids[first+i];
    degrees[i]=d.offset[id+1]-d.offset[id];
}

__global__ void fillLayer(Mutable d, Access access, UsageView usage, Attempt a, const U32* ids, const U32* ingress,
                          U32 first, U32 width, const U32* layerOffset, U32 arcBase,
                          U32* offset, Arc* arcs, U32* discovered, U32* error) {
    const U32 thread = blockIdx.x * blockDim.x + threadIdx.x;
    const U32 i = thread / FrontierLanes;
    const U32 lane = thread % FrontierLanes;
    if (i>=width) return;
    const U32 u=first+i,parent=ids[u],entering=ingress[u];
    const U32 start=arcBase+layerOffset[i];
    if (lane == 0) offset[u] = start;
    if (parent==a.sink) return;
    const int sinkSlr=slrIndex(access,a.sink,Unseen);
    for (U32 e = d.offset[parent] + lane, pos = start + lane; e < d.offset[parent+1];
            e += FrontierLanes, pos += FrontierLanes) {
        const U32 child=d.edge[e];
        if (child>=d.n) {
            atomicExch(error,1u);
            arcs[pos]={u,Unseen,1,e};
            continue;
        }
        const U32 reason=denyReason(access,d,usage,a,parent,entering,child,sinkSlr);
        if (reason) {
            arcs[pos]={u,Unseen,reason,e};
            continue;
        }
        U32 key=child;
        if (access.type[child]==access.ord[SLL]) {
            U32 canonical=d.offset[parent];
            while (d.edge[canonical]!=child) ++canonical;
            key=d.n+canonical;
        }
        atomicMin(discovered+key,NewKey|pos);
        const float center=fides_distToCenter(access.x[child],__uint_as_float(a.centerX),
                endY(access,child,parent),__uint_as_float(a.centerY));
        arcs[pos]={u,key,__float_as_uint(center),e};
    }
}

__global__ void markLayer(const Arc* arcs, U32 start, U32 count, const U32* discovered, U32* marks) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>count) return;
    if (i==count) {
        marks[i]=0;
        return;
    }
    const U32 at=start+i;
    const Arc a=arcs[at];
    marks[i]=!a.denied() && discovered[a.v]==(NewKey|at);
}

__global__ void assignLayer(const Arc* arcs, U32 start, U32 count, U32 next, const U32* marks,
                            const U32* prefix, Mutable d, U32* discovered, U32* ids, U32* ingress,
                            U32* mapKeys) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>=count || !marks[i]) return;
    const Arc a=arcs[start+i];
    const U32 v=next+prefix[i],key=a.v;
    discovered[key]=v;
    mapKeys[v]=key;
    ids[v]=d.edge[a.edge];
    ingress[v]=key<d.n ? Unseen : ids[a.u];
}

__global__ void finaliseLayer(Arc* arcs, U32 start, U32 count, const U32* discovered,
                              U32* error, U32* denied) {
    const U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    const bool rejected = i < count && arcs[start + i].denied();
    const unsigned mask = __ballot_sync(0xffffffffu, rejected);
    if ((threadIdx.x & 31) == 0 && mask) atomicAdd(denied, __popc(mask));
    if (i >= count || rejected) return;
    const U32 assigned = discovered[arcs[start + i].v];
    if (assigned >= NewKey) atomicExch(error, 2u);
    arcs[start + i].v = assigned;
}

__global__ void resetDiscovered(U32* discovered, const U32* keys, U32 count) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i<count) discovered[keys[i]]=EmptyKey;
}

template<class T> void growOnDevice(T*& ptr,size_t previous,size_t target,size_t used,const char* name) {
    need(target>previous && used<=previous,"Device growth shape");
    size_t freeBytes,totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    if (target*sizeof(T)+(size_t(1)<<30)>freeBytes) {
        std::cerr << "GROWTH_REFUSED " << name << " old=" << previous << " required=" << target
                  << " free=" << freeBytes << std::endl;
        throw MemoryPressure("Growing projection would breach the explicit one-GiB reserve");
    }
    auto entry=std::find_if(allocations.begin(), allocations.end(),
                            [ptr](const Allocation& block) { return block.ptr == ptr; });
    need(entry != allocations.end(), "Projection allocator lost the old buffer");
    auto replacement = static_cast<T*>(reserveDevice(target*sizeof(T)));
    if (used) checkCuda(copyInStream(replacement,ptr,used*sizeof(T),cudaMemcpyDeviceToDevice));
    freeDevice(*entry);
    ptr = replacement;
    *entry = {replacement, target*sizeof(T)};
}

U32 grownCount(U32 old,U32 required) {
    need(required>old && required<UINT32_MAX/2,"Device projection index space");
    const U64 geometric=U64(old)+old/2;
    return static_cast<U32>(std::max<U64>(required,std::min<U64>(geometric,UINT32_MAX/2-1)));
}


ProjectionResult projectInParallel(Mutable data, Access access, UsageView usage, Attempt attempt, U32* discovered,
                                   U32*& ids, U32*& ingress, U32*& offset, Arc*& arcs,
                                   U32 vertexCap, U32 arcCap, U32& levels) {
    constexpr U32 threads=128;
    auto scanRequired=[](U32 entries) {
        size_t bytes=0;
        checkCuda(cub::DeviceScan::ExclusiveSum(nullptr,bytes,static_cast<U32*>(nullptr),
              static_cast<U32*>(nullptr),int(entries), cudaStreamPerThread));
        return bytes;
    };
    U32 scratchCap=1u<<18;
    size_t scanBytes=scanRequired(scratchCap);
    size_t freeBytes,totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    const size_t scratch=size_t(2)*scratchCap*sizeof(U32)+size_t(vertexCap+2)*sizeof(U32)+scanBytes;
    needMemory(scratch+(size_t(1)<<30)<=freeBytes,"Parallel projection initial scratch with one GiB reserve");
    auto marks=allocate<U32>(scratchCap),prefix=allocate<U32>(scratchCap);
    auto keys=allocate<U32>(vertexCap),error=allocate<U32>(1),denied=allocate<U32>(1);
    auto scan=allocate<unsigned char>(scanBytes);
    auto resizeScan=[&](U32 required) {
        if (required<=scratchCap) return;
        const U32 next=grownCount(scratchCap,required);
        growOnDevice(marks,scratchCap,next,0,"scan_marks");
        growOnDevice(prefix,scratchCap,next,0,"scan_prefix");
        const size_t nextScan=scanRequired(next);
        if (nextScan>scanBytes) {
            growOnDevice(scan,scanBytes,nextScan,0,"scan_workspace");
            scanBytes=nextScan;
        }
        scratchCap=next;
    };
    checkCuda(setInStream(error,0,sizeof(U32)));
    checkCuda(setInStream(denied,0,sizeof(U32)));
    checkCuda(setInStream(discovered+attempt.source,0,sizeof(U32)));
    checkCuda(copyInStream(keys,&attempt.source,sizeof(U32),cudaMemcpyHostToDevice));
    checkCuda(copyInStream(ids,&attempt.source,sizeof(U32),cudaMemcpyHostToDevice));
    checkCuda(setInStream(ingress,0xff,sizeof(U32)));
    U32 begin=0,vertices=1,arcCount=0;
    levels=0;
    while (begin<vertices) {
        const U32 width=vertices-begin;
        resizeScan(width+1);
        projectionDegrees<<<(width+threads)/threads,threads>>>(data,attempt,ids,begin,width,marks);
        checkCuda(cudaGetLastError());
        checkCuda(cub::DeviceScan::ExclusiveSum(scan,scanBytes,marks,prefix,int(width+1), cudaStreamPerThread));
        const U32 layerArcs=checkDeviceErrors(prefix+width);
        need(U64(arcCount)+layerArcs<UINT32_MAX/2,"Projection arc index space");
        if (layerArcs>arcCap-arcCount) {
            const U32 next=grownCount(arcCap,arcCount+layerArcs);
            growOnDevice(arcs,arcCap,next,arcCount,"arcs");
            arcCap=next;
        }
        constexpr U32 parentsPerBlock = threads / FrontierLanes;
        fillLayer<<<(width + parentsPerBlock - 1) / parentsPerBlock,threads>>>(data,access,usage,attempt,ids,ingress,begin,width,prefix,
                                                       arcCount,offset,arcs,discovered,error);
        checkCuda(cudaGetLastError());
        begin=vertices;
        if (layerArcs) {
            resizeScan(layerArcs+1);
            markLayer<<<(layerArcs+threads)/threads,threads>>>(arcs,arcCount,layerArcs,discovered,marks);
            checkCuda(cudaGetLastError());
            checkCuda(cub::DeviceScan::ExclusiveSum(scan,scanBytes,marks,prefix,int(layerArcs+1), cudaStreamPerThread));
            const U32 newVertices=checkDeviceErrors(prefix+layerArcs);
            need(U64(vertices)+newVertices<UINT32_MAX/2,"Projection vertex index space");
            if (newVertices>vertexCap-vertices) {
                const U32 next=grownCount(vertexCap,vertices+newVertices);
                growOnDevice(ids,vertexCap,next,vertices,"vertex_ids");
                growOnDevice(ingress,vertexCap,next,vertices,"vertex_ingress");
                growOnDevice(offset,size_t(vertexCap)+1,size_t(next)+1,vertices,"vertex_offsets");
                growOnDevice(keys,vertexCap,next,vertices,"vertex_map_keys");
                vertexCap=next;
            }
            assignLayer<<<(layerArcs+threads-1)/threads,threads>>>(arcs,arcCount,layerArcs,vertices,marks,
                       prefix,data,discovered,ids,ingress,keys);
            checkCuda(cudaGetLastError());
            finaliseLayer<<<(layerArcs+threads-1)/threads,threads>>>(arcs,arcCount,layerArcs,discovered,error,denied);
            checkCuda(cudaGetLastError());
            vertices+=newVertices;
            arcCount+=layerArcs;
        }
        ++levels;
    }
    U32 target;
    checkCuda(copyInStream(&target,discovered+attempt.sink,sizeof(U32),cudaMemcpyDeviceToHost));
    need(checkDeviceErrors(error) == 0 && (target == EmptyKey || (target < vertices && arcCount > 0)),
         "Parallel projection result");
    checkCuda(copyInStream(offset+vertices,&arcCount,sizeof(U32),cudaMemcpyHostToDevice));
    const U32 deniedCount=checkDeviceErrors(denied);
    resetDiscovered<<<(vertices+threads-1)/threads,threads>>>(discovered,keys,vertices);
    checkCuda(cudaGetLastError());
    return ProjectionResult{vertices,arcCount,static_cast<U32>(target),deniedCount,0,0,0};
}


__global__ void buildBloom(const Arc* arcs, U32 m, U32* bloom, U32 mask) {
    const U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= m || !arcs[i].denied()) return;
    const U32 a = hashPosition(i,0x8ea7d5d1u,mask), b = hashPosition(i,0x13c6ef37u,mask);
    atomicOr(&bloom[a/32],1u << (a%32));
    atomicOr(&bloom[b/32],1u << (b%32));
}

Attempt readAttempt(std::istream& in) {
    Attempt q{};
    in >> q.source >> q.sink >> q.xmin >> q.ymin >> q.xmax >> q.ymax >> q.box >> q.uturn
       >> q.cross >> q.north >> q.south >> q.sinkX >> q.sinkY >> q.sinkTile >> q.sinkType >> q.sinkCtrl
       >> q.centerX >> q.centerY >> q.share >> q.node >> q.wl >> q.netSize >> q.hpwl;
    need(bool(in), "Cannot read packed attempt");
    in >> std::ws;
    need(in.eof(), "Unexpected trailing packed attempt data");
    return q;
}
struct TraceRow { U32 id, cost, changed; int32_t ingress; };
struct TraceStatus { U32 size, status; };


__device__ bool tight(U32 u,const Arc& arc,const Price& price,const U32* best) {
    if (arc.denied() || best[arc.v]==Inf) return false;
    const float sum=__fadd_rn(__uint_as_float(best[u]),__uint_as_float(price.first));
    return __float_as_uint(__fadd_rn(sum,__uint_as_float(price.second)))==best[arc.v];
}
__global__ void traceDegrees(const U32* offset,const U32* queue,U32 head,U32 width,U32* out) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>width) return;
    if (i==width) out[i]=0;
    else {
        const U32 u=queue[head+i];
        out[i]=offset[u+1]-offset[u];
    }
}
__global__ void proposeTrace(const U32* offset,const Arc* arcs,const Price* prices,const U32* best,
                             const U32* queue,const U32* predecessor,U32 head,U32 width,U64* winner) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>=width) return;
    const U32 u=queue[head+i];
    const U64 priority=U64(i)<<32;
    for (U32 edge=offset[u];edge<offset[u+1];++edge) {
        const Arc a=arcs[edge];
        if (tight(u,a,prices[edge],best) && predecessor[a.v]==UINT32_MAX)
            atomicMin(winner+a.v,priority|edge);
    }
}
__global__ void markTrace(const U32* offset,const Arc* arcs,const U32* queue,U32 head,U32 width,
                          const U32* inputOffset,const U64* winner,U32* marks) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>width) return;
    if (i==width) {
        marks[inputOffset[width]]=0;
        return;
    }
    const U32 u=queue[head+i];
    const U64 priority=U64(i)<<32;
    U32 at=inputOffset[i];
    for (U32 edge=offset[u];edge<offset[u+1];++edge,++at) {
        const Arc a=arcs[edge];
        marks[at]=!a.denied() && winner[a.v]==(priority|edge);
    }
}
__global__ void enqueueTrace(const U32* offset,const Arc* arcs,U32* queue,U32 head,U32 width,
                             const U32* inputOffset,const U32* marks,const U32* prefix,U32 tail,
                             U32* predecessor,U64* winner) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>=width) return;
    const U32 u=queue[head+i];
    U32 at=inputOffset[i];
    for (U32 edge=offset[u];edge<offset[u+1];++edge,++at) {
        if (!marks[at]) continue;
        const U32 v=arcs[edge].v;
        queue[tail+prefix[at]]=v;
        predecessor[v]=edge;
        winner[v]=UINT64_MAX;
    }
}
__global__ void compactTrace(U32 source,U32 target,U32 n,const U32* best,const Arc* arcs,
                             const Price* prices,const U32* ids,const U32* ingress,
                             const U32* predecessor,TraceRow* rows,TraceStatus* status) {
    status->status=1;
    status->size=0;
    if (predecessor[target]==UINT32_MAX) return;
    for (U32 v=target;status->size<n;) {
        const U32 edge=predecessor[v];
        rows[status->size++]={ids[v],best[v],v==source?0u:(prices[edge].flags & PriceChanged),static_cast<int32_t>(ingress[v])};
        if (v==source) {
            status->status=0;
            return;
        }
        v=arcs[edge].u;
    }
}
void parallelTrace(U32 source,U32 target,U32 n,const U32* best,const U32* offset,const Arc* arcs,
                   const Price* prices,const U32* ids,const U32* ingress,
                   U32* queue,U32* predecessor,TraceRow* rows,TraceStatus* status) {
    constexpr U32 threads=128;
    size_t freeBytes,totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    U32 cap=1u<<18;
    auto sizeFor=[](U32 count) {
        size_t required=0;
        checkCuda(cub::DeviceScan::ExclusiveSum(nullptr,required,static_cast<U32*>(nullptr),
                  static_cast<U32*>(nullptr),int(count), cudaStreamPerThread));
        return required;
    };
    size_t scratchBytes=sizeFor(cap);
    needMemory(size_t(n)*sizeof(U64)+size_t(3)*cap*sizeof(U32)+scratchBytes+(size_t(1)<<30)<=freeBytes,
         "Parallel trace scratch with one GiB reserve");
    const auto mark=allocations.size();
    auto winner=allocate<U64>(n);
    auto marks=allocate<U32>(cap), prefix=allocate<U32>(cap), inputOffset=allocate<U32>(cap);
    auto scratch=allocate<unsigned char>(scratchBytes);
    auto ensure=[&](U32 required) {
        if (required<=cap) return;
        const U32 grown=grownCount(cap,required);
        growOnDevice(marks,cap,grown,0,"trace_marks");
        growOnDevice(prefix,cap,grown,0,"trace_prefix");
        growOnDevice(inputOffset,cap,grown,0,"trace_offsets");
        const size_t next=sizeFor(grown);
        if (next>scratchBytes) {
            growOnDevice(scratch,scratchBytes,next,0,"trace_scan_workspace");
            scratchBytes=next;
        }
        cap=grown;
    };
    checkCuda(setInStream(winner,0xff,size_t(n)*sizeof(U64)));
    checkCuda(setInStream(predecessor,0xff,size_t(n)*sizeof(U32)));
    const U32 root=UINT32_MAX-1;
    checkCuda(copyInStream(predecessor+source,&root,sizeof(root),cudaMemcpyHostToDevice));
    checkCuda(copyInStream(queue,&source,sizeof(source),cudaMemcpyHostToDevice));
    U32 head=0,tail=1;
    while (head<tail && checkDeviceErrors(predecessor+target)==UINT32_MAX) {
        const U32 width=tail-head;
        ensure(width+1);
        traceDegrees<<<(width+threads)/threads,threads>>>(offset,queue,head,width,marks);
        checkCuda(cudaGetLastError());
        checkCuda(cub::DeviceScan::ExclusiveSum(scratch,scratchBytes,marks,inputOffset,int(width+1), cudaStreamPerThread));
        const U32 edges=checkDeviceErrors(inputOffset+width);
        if (!edges) break;
        if (edges+1>cap) {
            ensure(edges+1);
            // Preserve the layer's input offsets when the scan workspace grows.
            traceDegrees<<<(width+threads)/threads,threads>>>(offset,queue,head,width,marks);
            checkCuda(cudaGetLastError());
            checkCuda(cub::DeviceScan::ExclusiveSum(scratch,scratchBytes,marks,inputOffset,int(width+1), cudaStreamPerThread));
        }
        proposeTrace<<<(width+threads-1)/threads,threads>>>(offset,arcs,prices,best,queue,predecessor,head,width,winner);
        checkCuda(cudaGetLastError());
        markTrace<<<(width+threads)/threads,threads>>>(offset,arcs,queue,head,width,inputOffset,winner,marks);
        checkCuda(cudaGetLastError());
        checkCuda(cub::DeviceScan::ExclusiveSum(scratch,scratchBytes,marks,prefix,int(edges+1), cudaStreamPerThread));
        const U32 additions=checkDeviceErrors(prefix+edges);
        need(additions<=n-tail,"Parallel trace frontier capacity");
        enqueueTrace<<<(width+threads-1)/threads,threads>>>(offset,arcs,queue,head,width,inputOffset,
                     marks,prefix,tail,predecessor,winner);
        checkCuda(cudaGetLastError());
        head=tail;
        tail+=additions;
    }
    compactTrace<<<1,1>>>(source,target,n,best,arcs,prices,ids,ingress,predecessor,rows,status);
    checkCuda(cudaGetLastError());
    releaseAfter(mark);
}


// Each pending state is unique. Kernel boundaries serialize membership and label updates.
__global__ void seedBucketMembership(const U32* pending, U32 count, U32* flags) {
    const U32 i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < count) flags[pending[i]] = 1;
}

__global__ void splitBucket(const U32* pending, U32 pendingCount, const U32* best, U32 target,
                            U32 minKey, U32* flags, U32* frontier, U32* nextPending,
                            BucketProgress* progress) {
    const U32 stride = blockDim.x * gridDim.x;
    const U32 bound = best[target];
    for (U32 i = blockIdx.x * blockDim.x + threadIdx.x; i < pendingCount; i += stride) {
        const U32 v = pending[i];
        const U32 label = best[v];
        if (label > bound) {
            flags[v] = 0;
        } else if ((label >> BucketShift) == minKey) {
            flags[v] = 0;
            frontier[atomicAdd(&progress->frontier, 1u)] = v;
        } else {
            nextPending[atomicAdd(&progress->pending, 1u)] = v;
        }
    }
}

__global__ void advanceBucket(U32* best, U32* prior, U32* changedAt, U32 wave,
                              U32* proposals, const U32* touched, U32 stateCount,
                              U32* flags, U32* pending, BucketProgress* progress, const U32* incumbent) {
    const U32 count = progress->touched;
    if (count > stateCount) return;
    const U32 bound = *incumbent;
    const U32 stride = blockDim.x * gridDim.x;
    for (U32 i = blockIdx.x * blockDim.x + threadIdx.x; i < count; i += stride) {
        const U32 v = touched[i];
        const U32 label = proposals[v];
        proposals[v] = Inf;
        const U32 current = best[v];
        if (label < current) {
            prior[v] = current;
            best[v] = label;
            changedAt[v] = wave;
            if (label <= bound && !flags[v]) {
                flags[v] = 1;
                pending[atomicAdd(&progress->pending, 1u)] = v;
            }
        }
    }
}

__global__ void findBucket(const U32* pending, const U32* best, U32 target,
                           BucketProgress* progress, U32 stateCount) {
    const U32 count = progress->pending;
    if (count > stateCount) return;
    const U32 stride = blockDim.x * gridDim.x;
    const U32 bound = best[target];
    U32 key = UINT32_MAX;
    for (U32 i = blockIdx.x * blockDim.x + threadIdx.x; i < count; i += stride) {
        const U32 label = best[pending[i]];
        if (label <= bound) key = min(key, label >> BucketShift);
    }
    #pragma unroll
    for (int step=16; step; step>>=1) {
        key = min(key, __shfl_down_sync(0xffffffffu, key, step));
    }
    if ((threadIdx.x & 31) == 0 && key != UINT32_MAX) atomicMin(&progress->minKey, key);
}

#include "active_frontier_v9.cuh"
#include "active_frontier_v9_trace.cuh"

U32 activeFrontierKeys = 0;

bool tryActiveFrontier(const Attempt& attempt, Mutable data, U32 mode, Access access, UsageView usage,
                       U32* labels, U32* activity, U32 channel, SearchWork& work, std::string& answer,
                       bool narrowed) {
    static const bool enabled = [] {
        const char* value = std::getenv("FIDES_GPU_ACTIVE_FRONTIER_V9");
        return value && std::strcmp(value, "1") == 0;
    }();
    static const bool sameSlrEnabled = [] {
        const char* value = std::getenv("FIDES_GPU_SAME_SLR_FRONTIER_V11");
        return value && std::strcmp(value, "1") == 0;
    }();
    if (!enabled || mode != (Target | Dominance | Bloom | Stale) || !activeFrontierKeys
        || attempt.box != 1
        || (attempt.cross ? !narrowed || attempt.north == attempt.south : !sameSlrEnabled)
        || attempt.source >= data.n || attempt.sink >= data.n || attempt.source == attempt.sink
        || attempt.share > SafeIncrement || attempt.node > SafeIncrement || attempt.wl > SafeIncrement
        || attempt.netSize <= 0 || attempt.hpwl <= 0 || attempt.hpwl > 32767)
        return false;

    const Weights weights{fromBits(attempt.share), fromBits(attempt.node), fromBits(attempt.wl),
                          attempt.netSize, attempt.hpwl};
    const auto mark = allocations.size();
    const auto start = std::chrono::steady_clock::now();
    auto traceStart = start;
    ActiveFrontierSearch search;
    std::vector<TraceRow> path;
    try {
        search = runActiveFrontier(data, access, usage, attempt, weights, labels, activeFrontierKeys,
                                   activity, channel);
        traceStart = std::chrono::steady_clock::now();
        if (!search.counts.failure && !attempt.cross && search.targetCost == Inf) {
            search.counts.failure = ActiveSameSlrNoPath;
        }
        if (!search.counts.failure && search.targetCost != Inf
            && !activeFrontierTrace(data, access, usage, attempt, weights, labels, search.counts.touched, path)) {
            search.counts.failure = ActiveTraceFailure;
        }
    } catch (const MemoryPressure&) {
        search.counts.failure = ActiveScratchPressure;
        if (!search.touched) traceStart = std::chrono::steady_clock::now();
    }
    clearActiveFrontier(labels, activeFrontierKeys, search);
    releaseAfter(mark);
    checkCuda(cudaStreamSynchronize(cudaStreamPerThread));

    const auto elapsed = [](auto from, auto to) {
        return std::chrono::duration_cast<std::chrono::microseconds>(to - from).count();
    };
    const auto traceEnd = std::chrono::steady_clock::now();
    const auto searchUs = elapsed(start, traceStart), traceUs = elapsed(traceStart, traceEnd);
    const auto& stats = search.counts.events;
    std::ostringstream reply;
    reply << "ACTIVE_FRONTIER " << (search.counts.failure ? 2 : 1) << ' ' << search.counts.failure
          << ' ' << searchUs << ' ' << traceUs << ' ' << search.counts.touched << ' '
          << search.waves << ' ' << search.largest << '\n';
    if (search.counts.failure) {
        answer = reply.str();
        return false;
    }
    work.vertices = search.counts.touched;
    work.arcs = static_cast<U32>(std::min<U64>(stats[Candidate], UINT32_MAX));
    work.waves = search.waves;
    work.candidates = stats[Candidate];
    work.atomics = stats[Atomic];
    if (search.targetCost == Inf) throw NoPath("Resident active frontier exhausted without a path");
    if (path.empty() || path.front().id != attempt.sink || path.back().id != attempt.source
        || path.front().cost != search.targetCost || path.back().cost != 0) {
        answer = "ACTIVE_FRONTIER 2 4 0 0 0 0 0\n";
        return false;
    }
    reply << "ARC_BYTES 0 0\nBUILD 0 0\nPROJECTION 0 0 0 0\nGPU_BUCKETS 1 " << BucketShift << '\n';
    reply << "PHASE_US " << searchUs << ' ' << traceUs << "\n";
    reply << "TRACE_TRANSFER " << path.size() * sizeof(TraceRow) << " 0\nCHANGED";
    for (const auto& row : path) if (row.changed) reply << ' ' << row.id;
    reply << "\nPATH " << path.front().cost;
    for (const auto& row : path) reply << ' ' << row.id;
    reply << "\nPARTIAL";
    for (const auto& row : path) reply << ' ' << row.cost;
    reply << "\nINGRESS";
    for (const auto& row : path) reply << ' ' << row.ingress;
    reply << "\nARM " << mode << ' ' << search.waves;
    for (int i = 0; i < CounterCount; ++i) reply << ' ' << stats[i];
    reply << "\nTARGET_CUTOFF 0 0 0 0\nFRONTIER " << search.expanded << ' ' << search.largest
          << ' ' << search.counts.touched << "\nWAVE_US " << searchUs << '\n';
    answer = reply.str();
    return true;
}


bool equalSameSlrPath(const std::string& active, const std::string& legacy) {
    for (const std::string field : {"\nCHANGED", "\nPATH", "\nPARTIAL", "\nINGRESS"}) {
        const auto a = active.find(field), b = legacy.find(field);
        if (a == std::string::npos || b == std::string::npos) return false;
        const auto endA = active.find('\n', a + 1), endB = legacy.find('\n', b + 1);
        if (endA == std::string::npos || endB == std::string::npos
            || active.compare(a, endA - a, legacy, b, endB - b) != 0)
            return false;
    }
    return true;
}

std::string solveAttempt(const Attempt& attempt, Mutable data, U32 mode, Access access, UsageView usage,
                         U32* discovered, U32* channelActivity, U32 channel, SearchWork& work,
                         bool narrowed = false, bool legacyOnly = false) {
    need(attempt.source<data.n && attempt.sink<data.n && attempt.source!=attempt.sink,
         "Qualified attempt endpoints");
    const Weights weights{fromBits(attempt.share),fromBits(attempt.node),fromBits(attempt.wl),
                          attempt.netSize,attempt.hpwl};
    need(weights.netSize>0 && weights.hpwl>0 && weights.hpwl<=32767,"Attempt dimensions");
    std::string activeFrontierReply;
    if (!legacyOnly && tryActiveFrontier(attempt, data, mode, access, usage, discovered, channelActivity,
                                          channel, work, activeFrontierReply, narrowed)) {
        static const bool requireSameSlrParity = [] {
            const char* value = std::getenv("FIDES_GPU_SAME_SLR_PARITY_V11");
            return value && std::strcmp(value, "1") == 0;
        }();
        if (!attempt.cross && requireSameSlrParity) {
            static std::once_flag sampled;
            static bool equalPaths = false;
            std::call_once(sampled, [&] {
                SearchWork legacyWork;
                const auto start = std::chrono::steady_clock::now();
                try {
                    const auto legacy = solveAttempt(attempt, data, mode, access, usage, discovered, nullptr,
                                                     channel, legacyWork, narrowed, true);
                    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - start).count();
                    equalPaths = equalSameSlrPath(activeFrontierReply, legacy);
                    if (equalPaths) {
                        std::ostringstream receipt;
                        receipt << "SAME_SLR_PARITY 1 " << elapsed << ' ' << legacyWork.candidates << '\n';
                        activeFrontierReply += receipt.str();
                        std::cerr << receipt.str();
                    }
                } catch (const NoPath&) {
                    // Do not let a same-scope parity difference pass as an ordinary corridor retry.
                    equalPaths = false;
                }
            });
            need(equalPaths, "Same-SLR resident search disagrees with matched legacy GPU path");
        }
        return activeFrontierReply;
    }
    // Qualification capacities are explicit and failures abort before any path handback.
    constexpr U32 vertexCap = 1u<<19, arcCap = 1u<<22;
    const size_t projectionBytes = size_t(arcCap)*sizeof(Arc) + (size_t(3)*vertexCap+1)*sizeof(U32);
    size_t freeBytes,totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    needMemory(projectionBytes+(size_t(1)<<30)<=freeBytes,"Parallel projection capacity with one GiB reserve");
    auto deviceArcs=allocate<Arc>(arcCap);
    auto deviceOffset=allocate<U32>(vertexCap+1);
    auto deviceIds=allocate<U32>(vertexCap);
    auto deviceIngress=allocate<U32>(vertexCap);
    const auto scratch=allocations.size();
    U32 levels;
    const auto buildStart=std::chrono::steady_clock::now();
    const auto built=projectInParallel(data,access,usage,attempt,discovered,deviceIds,deviceIngress,deviceOffset,
                                      deviceArcs,vertexCap,arcCap,levels);
    releaseAfter(scratch);
    const auto buildUs=std::chrono::duration_cast<std::chrono::microseconds>(
                           std::chrono::steady_clock::now()-buildStart).count();
    work.vertices = built.vertices;
    work.arcs = built.arcs;
    if (built.target == EmptyKey) throw NoPath("GPU projection cannot reach the sink");
    const U32 n=built.vertices,m=built.arcs,source=0,target=built.target;
    U32 bitCount=1024;
    while (bitCount<4*built.denied) bitCount*=2;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    const size_t needed=size_t(m)*sizeof(Price)+(size_t(11)*n+5)*sizeof(U32)
                       +size_t(bitCount/32)*sizeof(U32)+CounterCount*sizeof(U64);
    needMemory(needed+(size_t(1)<<30)<=freeBytes,"Wavefront and trace with one GiB reserve");
    std::ostringstream common;
    common << "ARC_BYTES " << sizeof(Arc) << " " << size_t(m) * sizeof(Arc) << std::endl;
    common << "BUILD " << levels << " " << buildUs << std::endl;
    common << "BUDGET " << needed << " " << freeBytes << " " << totalBytes << std::endl;
    common << "PROJECTION " << n << " " << m << " " << built.denied << " "
          << 0 << std::endl;
    auto devicePrices=allocate<Price>(m);
    auto invalidPrice=allocate<U32>(1);
    auto deviceBloom=allocate<U32>(bitCount/32);
    checkCuda(setInStream(deviceBloom,0,size_t(bitCount/32)*sizeof(U32)));
    buildBloom<<<(m+127)/128,128>>>(deviceArcs,m,deviceBloom,bitCount-1);
    checkCuda(cudaGetLastError());
    auto best=allocate<U32>(n),prior=allocate<U32>(n),changedAt=allocate<U32>(n);
    auto proposals=allocate<U32>(n),touched=allocate<U32>(n);
    auto current=allocate<U32>(n),next=allocate<U32>(n);
    auto dWaveCounts=allocate<U32>(2);
    auto incumbent=allocate<U32>(1);
    auto deviceCounts=allocate<U64>(CounterCount + CutoffCount);
    checkCuda(setInStream(invalidPrice, 0, sizeof(U32)));
    checkCuda(setInStream(devicePrices, 0, size_t(m) * sizeof(Price)));
    priceArcs<<<(m + 127) / 128, 128>>>(deviceArcs, deviceIds, m, data, weights, devicePrices, invalidPrice, usage,
                                             access.base, access.length, deviceBloom, bitCount-1);
    checkCuda(cudaGetLastError());
    const U32 priceStatus = checkDeviceErrors(invalidPrice);
    need((priceStatus & ~UnsafeCutoff) == 0, "Resident CSR or mutable price validation failed");
    const bool safeCutoff = (priceStatus & UnsafeCutoff) == 0;
    std::ostringstream reply;
    reply << activeFrontierReply << common.str();
    const auto waveStart=std::chrono::steady_clock::now();
    constexpr U32 threads = 128, groups = threads / FrontierLanes;
    initializeWave<<<(n + threads - 1) / threads, threads>>>(
        n, source, best, prior, proposals, changedAt, current, deviceCounts);
    checkCuda(cudaGetLastError());
    U32 frontierCount = 1, waves = 0, maxFrontier = 1;
    U64 expanded = 0, touchedTotal = 0;
    U64* cutoff = (mode & Target) && safeCutoff ? deviceCounts + CounterCount : nullptr;
    constexpr U32 MinimumObservedWaves = 24;
    constexpr U64 ExpandedForBuckets = 1u << 18;
    const bool canUseBuckets = safeCutoff && mode == (Target | Dominance | Bloom | Stale);
    auto flood = [&](bool considerBuckets) {
        while (frontierCount) {
            need(++waves <= n + 1, "Sparse frontier failed to converge");
            expanded += frontierCount;
            maxFrontier = std::max(maxFrontier, frontierCount);
            checkCuda(setInStream(dWaveCounts, 0, 2 * sizeof(U32)));
            const U32 blocks = (frontierCount + groups - 1) / groups;
            relaxFrontier<<<blocks, threads>>>(
                deviceArcs, devicePrices, deviceOffset, target, mode,
                current, frontierCount, nullptr, best, prior, changedAt, waves, proposals, touched,
                dWaveCounts, deviceCounts, channelActivity, channel, incumbent, cutoff);
            checkCuda(cudaGetLastError());
            // The ordered stream makes the device count visible before advancement.
            advanceFrontier<<<std::min<U32>(blocks, 256u), threads>>>(
                best, prior, changedAt, waves + 1, proposals, touched, dWaveCounts, n, next, dWaveCounts + 1,
                incumbent, deviceOffset, cutoff);
            checkCuda(cudaGetLastError());
            U32 counts[2];
            checkCuda(copyInStream(counts, dWaveCounts, sizeof(counts), cudaMemcpyDeviceToHost));
            need(counts[0] <= n, "More touched states than vertices");
            need(counts[1] <= n, "More frontier states than vertices");
            touchedTotal += counts[0];
            frontierCount = counts[1];
            if (counts[0]) std::swap(current, next);
            if (considerBuckets && waves >= MinimumObservedWaves && expanded >= ExpandedForBuckets) break;
        }
    };
    flood(canUseBuckets);
    checkCuda(cudaMemGetInfo(&freeBytes, &totalBytes));
    const size_t bucketBytes = size_t(3) * n * sizeof(U32) + sizeof(BucketProgress);
    bool useBuckets = frontierCount && canUseBuckets
                      && bucketBytes + (size_t(1) << 30) <= freeBytes;
    if (useBuckets) {
        const auto mark = allocations.size();
        auto pending = allocate<U32>(n);
        auto nextPending = allocate<U32>(n);
        auto flags = allocate<U32>(n);
        auto progress = allocate<BucketProgress>(1);
        checkCuda(setInStream(flags, 0, n * sizeof(U32)));
        checkCuda(copyInStream(pending, current, frontierCount * sizeof(U32), cudaMemcpyDeviceToDevice));
        seedBucketMembership<<<(frontierCount + threads - 1) / threads, threads>>>(pending, frontierCount, flags);
        checkCuda(cudaGetLastError());
        BucketProgress state{0, 0, frontierCount, UINT32_MAX};
        checkCuda(copyInStream(progress, &state, sizeof(state), cudaMemcpyHostToDevice));
        findBucket<<<std::min<U32>((frontierCount + threads - 1) / threads, 1024u), threads>>>(
            pending, best, target, progress, n);
        checkCuda(cudaGetLastError());
        checkCuda(copyInStream(&state, progress, sizeof(state), cudaMemcpyDeviceToHost));
        while (state.minKey != UINT32_MAX) {
            if (waves == n + 1) {
                useBuckets = false;
                break;
            }
            ++waves;
            const U32 blocks = (state.pending + groups - 1) / groups;
            const U32 compactBlocks = std::min<U32>(blocks, 1024u);
            checkCuda(setInStream(progress, 0, 3 * sizeof(U32)));
            checkCuda(setInStream(&progress->minKey, 0xff, sizeof(U32)));
            splitBucket<<<compactBlocks, threads>>>(
                pending, state.pending, best, target, state.minKey, flags, current, nextPending, progress);
            checkCuda(cudaGetLastError());
            relaxFrontier<<<blocks, threads>>>(
                deviceArcs, devicePrices, deviceOffset, target, mode,
                current, 0, &progress->frontier, best, prior, changedAt, waves, proposals, touched,
                &progress->touched, deviceCounts, channelActivity, channel, incumbent, cutoff);
            checkCuda(cudaGetLastError());
            advanceBucket<<<compactBlocks, threads>>>(
                best, prior, changedAt, waves + 1, proposals, touched, n, flags, nextPending, progress, incumbent);
            checkCuda(cudaGetLastError());
            findBucket<<<compactBlocks, threads>>>(nextPending, best, target, progress, n);
            checkCuda(cudaGetLastError());
            checkCuda(copyInStream(&state, progress, sizeof(state), cudaMemcpyDeviceToHost));
            need(state.frontier > 0 && state.frontier <= n && state.touched <= n && state.pending <= n,
                 "Invalid CUDA bucket state");
            expanded += state.frontier;
            maxFrontier = std::max(maxFrontier, state.frontier);
            touchedTotal += state.touched;
            std::swap(pending, nextPending);
        }
        releaseAfter(mark);
        if (!useBuckets) {
            // A difficult queue schedule restarts in the existing CUDA flood.
            initializeWave<<<(n + threads - 1) / threads, threads>>>(
                n, source, best, prior, proposals, changedAt, current, deviceCounts);
            checkCuda(cudaGetLastError());
            frontierCount = 1;
            waves = 0;
            maxFrontier = 1;
            expanded = touchedTotal = 0;
        }
    }
    if (!useBuckets && frontierCount) flood(false);
    U64 count[CounterCount + CutoffCount];
    checkCuda(copyInStream(count, deviceCounts, sizeof(count), cudaMemcpyDeviceToHost));
    need(count[Invalid] == 0 && count[Candidate] == count[Rejected] + count[Bound]
         + count[Dominated] + count[StaleCut] + count[Atomic], "Unbalanced GPU candidate counters");
    need(mode ? count[Exact]==count[Rejected]+count[FalseHit]
              : count[Exact]==count[Candidate] && count[FalseHit]==0 && !count[Bound]
                && !count[Dominated] && !count[StaleCut], "Unbalanced exact-access counters");
    reply << "GPU_BUCKETS " << useBuckets << " " << BucketShift << std::endl;
    work.waves = waves;
    work.candidates = count[Candidate];
    work.atomics = count[Atomic];
    const U32 targetCost = checkDeviceErrors(best + target);
    if (targetCost == Inf) throw NoPath("GPU relaxation exhausted the frontier without a path");
    need(targetCost < Inf, "Invalid GPU sink cost");
    need(m < UINT32_MAX - 1, "Trace edge sentinel range");
    auto deviceRows = allocate<TraceRow>(n);
    auto deviceStatus = allocate<TraceStatus>(1);
    const auto traceStart=std::chrono::steady_clock::now();
    parallelTrace(source,target,n,best,deviceOffset,deviceArcs,devicePrices,deviceIds,deviceIngress,
                  current,next,deviceRows,deviceStatus);
    TraceStatus status;
    checkCuda(copyInStream(&status, deviceStatus, sizeof(status), cudaMemcpyDeviceToHost));
    need(status.status == 0 && status.size > 0 && status.size <= n, "Device tight-path trace failed");
    std::vector<TraceRow> path(status.size);
    checkCuda(copyInStream(path.data(), deviceRows, path.size() * sizeof(TraceRow), cudaMemcpyDeviceToHost));
    const auto traceEnd=std::chrono::steady_clock::now();
    reply << "PHASE_US " << std::chrono::duration_cast<std::chrono::microseconds>(traceStart-waveStart).count()
          << " " << std::chrono::duration_cast<std::chrono::microseconds>(traceEnd-traceStart).count() << std::endl;
    reply << "TRACE_TRANSFER " << sizeof(status) + path.size() * sizeof(TraceRow) << " "
          << n * sizeof(U32) + size_t(m) * sizeof(Price) << std::endl;
    reply << "CHANGED";
    for (const auto& row : path) if (row.changed) reply << " " << row.id;
    reply << std::endl << "PATH " << path.front().cost;
    for (const auto& row : path) reply << " " << row.id;
    reply << std::endl << "PARTIAL";
    for (const auto& row : path) reply << " " << row.cost;
    reply << std::endl << "INGRESS";
    for (const auto& row : path) reply << " " << row.ingress;
    reply << std::endl;
    reply << "ARM " << mode << " " << waves;
    for (int i = 0; i < CounterCount; ++i) reply << " " << count[i];
    reply << std::endl << "TARGET_CUTOFF";
    for (int i = 0; i < CutoffCount; ++i) reply << " " << count[CounterCount + i];
    reply << std::endl;
    reply << "FRONTIER " << expanded << " " << maxFrontier << " " << touchedTotal << std::endl;
    const auto waveUs=std::chrono::duration_cast<std::chrono::microseconds>(
                          std::chrono::steady_clock::now()-waveStart).count();
    reply << "WAVE_US " << waveUs << std::endl;
    return reply.str();
}

auto nonBoxFields(const Attempt& q) {
    return std::tie(q.source, q.sink, q.uturn, q.cross, q.north, q.south, q.sinkX, q.sinkY,
                    q.sinkTile, q.sinkType, q.sinkCtrl, q.centerX, q.centerY, q.share,
                    q.node, q.wl, q.netSize, q.hpwl);
}

void workLine(std::ostringstream& report, const char* label,
              std::chrono::steady_clock::time_point start, const SearchWork& work) {
    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - start).count();
    report << label << " " << elapsed << " " << work.vertices << " " << work.arcs << " "
           << work.candidates << " " << work.atomics << " " << work.waves << "\n";
}

UsageView loadUsage(const std::string& file, Mutable data) {
    struct stat info{};
    if (::stat(file.c_str(), &info) != 0) {
        need(errno == ENOENT, "Cannot stat private GPU usage");
        return {};
    }
    need(S_ISREG(info.st_mode) && info.st_size >= 0, "Invalid private GPU usage file");
    const U64 bytes = static_cast<U64>(info.st_size);
    need(bytes % sizeof(UsageRow) == 0 && bytes / sizeof(UsageRow) <= data.n,
         "Invalid private GPU usage size");
    const U32 count = static_cast<U32>(bytes / sizeof(UsageRow));
    if (!count) return {};
    std::ifstream input(file, std::ios::binary);
    need(bool(input), "Cannot read private GPU usage");
    const auto rows = readWords<UsageRow>(input, count);
    need(input.peek() == EOF, "Unexpected private GPU usage data");
    const auto mark = allocations.size();
    auto deviceRows = allocate<UsageRow>(count);
    checkCuda(copyInStream(deviceRows, rows.data(), bytes, cudaMemcpyHostToDevice));
    const UsageView view{deviceRows, count, rows.front().node, rows.back().node};
    auto invalid = allocate<U32>(1);
    checkCuda(setInStream(invalid, 0xff, sizeof(U32)));
    checkUsageRows<<<(count + 127) / 128, 128>>>(data, view, invalid);
    checkCuda(cudaGetLastError());
    const U32 row = checkDeviceErrors(invalid);
    need(row == UINT32_MAX, "Private GPU usage differs from frozen committed mirror");
    releaseAfter(mark + 1);
    return view;
}

void solve(const std::string& response, Mutable data, U32 mode, Access access,
           U32* discovered, const std::string& attemptFile, U32* channelActivity, U32 channel) {
    std::ifstream originalFile(attemptFile);
    std::string attemptLine;
    need(bool(std::getline(originalFile, attemptLine)), "Missing original search bounds");
    std::istringstream originalStream(attemptLine);
    const Attempt original = readAttempt(originalStream);
    unsigned hasCorridor;
    need(bool(originalFile >> hasCorridor) && hasCorridor <= 1, "Invalid corridor marker");
    Attempt narrow = original;
    if (hasCorridor) {
        need(bool(originalFile >> narrow.xmin >> narrow.ymin >> narrow.xmax >> narrow.ymax),
             "Incomplete corridor coordinates");
    }
    originalFile >> std::ws;
    need(originalFile.eof(), "Unexpected trailing corridor data");
    const auto stem = attemptFile.substr(0, attemptFile.size() - 8);
    const UsageView usage = loadUsage(stem + ".usage", data);
    const auto mark = allocations.size();
    const auto usageRecord = "USAGE_OVERLAY " + std::to_string(usage.count) + "\n";
    std::ostringstream report;
    report << usageRecord;
    if (hasCorridor) {
        need((!original.cross || (original.north != original.south))
             && narrow.box == 1 && original.box == 1
             && original.xmin <= narrow.xmin && narrow.xmin < narrow.xmax && narrow.xmax <= original.xmax
             && original.ymin <= narrow.ymin && narrow.ymin < narrow.ymax && narrow.ymax <= original.ymax
             && (original.xmin != narrow.xmin || original.ymin != narrow.ymin
                     || original.xmax != narrow.xmax || original.ymax != narrow.ymax),
             "Invalid narrow-box GPU attempt");
        SearchWork discarded;
        const auto start = std::chrono::steady_clock::now();
        try {
            const auto reply = solveAttempt(narrow, data, mode, access, usage, discovered, channelActivity, channel, discarded, true);
            publish(response, usageRecord + "F1_RETRY 0\n" + reply);
            return;
        } catch (const NoPath&) {
            workLine(report, "F1_DISCARDED", start, discarded);
            releaseAfter(mark);
            checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
        }
        report << "F1_RETRY 1\n";
    } else {
        report << "F1_RETRY 0\n";
    }

    SearchWork work;
    const auto start = std::chrono::steady_clock::now();
    try {
        const auto reply = solveAttempt(original, data, mode, access, usage, discovered, channelActivity, channel, work);
        report << reply;
    } catch (const NoPath&) {
        report << "NO_PATH 1\n";
        workLine(report, "NO_PATH_WORK", start, work);
    }
    publish(response, report.str());
}

template<class T> T* upload(const std::vector<T>& words) {
    auto ptr = allocate<T>(words.size());
    checkCuda(copyInStream(ptr, words.data(), words.size() * sizeof(T), cudaMemcpyHostToDevice));
    return ptr;
}
U32 checkDeviceErrors(U32* errors) {
    U32 count;
    checkCuda(copyInStream(&count, errors, sizeof(count), cudaMemcpyDeviceToHost));
    return count;
}
template<class T> T* loadColumn(const std::string& dir, const char* name, size_t count) {
    std::ifstream in(dir + "/" + name, std::ios::binary);
    auto values = readWords<T>(in, count);
    need(in.peek() == EOF, "Unexpected extra resident column bytes");
    return upload(values);
}

template<class T> void v20Upload(T* target, std::istream* input, size_t count) {
    constexpr size_t chunkWords = (size_t(64) << 20) / sizeof(T);
    std::vector<T> buffer(std::min(count, chunkWords));
    for (size_t position = 0; position < count;) {
        size_t words = std::min(count - position, buffer.size());
        size_t bytes = words * sizeof(T);
        if (input) {
            input->read(reinterpret_cast<char*>(buffer.data()), bytes);
            need(bool(*input), "V20 resident column truncated");
        }
        v20Check("cudaMemcpyAsync", bytes, cudaMemcpyAsync(target + position, buffer.data(), bytes,
                 cudaMemcpyHostToDevice, cudaStreamPerThread));
        v20Check("cudaStreamSynchronize", bytes, cudaStreamSynchronize(cudaStreamPerThread));
        position += words;
    }
}

template<class T> T* v20Load(const char* name, std::istream* input, size_t count) {
    V20Column column(name);
    auto target = allocate<T>(count);
    v20Upload(target, input, count);
    return target;
}

template<class T> T* v20UploadVector(const char* name, const std::vector<T>& values) {
    V20Column column(name);
    auto target = allocate<T>(values.size());
    const size_t bytes = values.size() * sizeof(T);
    v20Check("cudaMemcpyAsync", bytes, cudaMemcpyAsync(target, values.data(), bytes,
             cudaMemcpyHostToDevice, cudaStreamPerThread));
    v20Check("cudaStreamSynchronize", bytes, cudaStreamSynchronize(cudaStreamPerThread));
    return target;
}

template<class T> void v20LiveColumn(const std::string& dir, const char* name, T* target, size_t count) {
    V20Column column(name);
    std::ifstream input(dir + "/" + name, std::ios::binary);
    v20Upload(target, &input, count);
    need(input.peek() == EOF, "V20 excess bytes in live state column");
}

std::vector<int> v20ReadSlr(std::ifstream& input, U32 words, U32 types) {
    auto values = readWords<int>(input, words);
    need(input.peek() == EOF, "V20 excess bytes in live SLR table");
    need((words==6 && values[0]==0)
          || (values[0]==1 && values[2]>0 && values[3]>0 && values[4]==int(types) && values[5]>0
              && U32(6+values[2]+2*values[3]+values[4]+values[5])==words),"Packed SLR table");
    return values;
}

size_t v20AccessBytes(const char* layoutPath, U32 nodes) {
    std::ifstream layout(layoutPath);
    U32 slrWords=0, types=0, tiles=0;
    need(bool(layout >> slrWords >> types >> tiles), "V20 RRG access dimensions");
    return (size_t(nodes) * 10 + slrWords + OrdinalCount + types + tiles) * sizeof(U32);
}

void v20ReserveFloor(const char* stage) {
    size_t freeBytes=0, totalBytes=0;
    v20Check(stage, allocatedBytes, cudaMemGetInfo(&freeBytes, &totalBytes));
    needMemory(freeBytes >= GpuReserve, "V20 resident ready would breach one-GiB reserve");
}


template<class T> T* loadRrg(std::ifstream& file, U64 at, size_t n, const char* name) {
    file.seekg(at);
    if (v20Startup) return v20Load<T>(name, &file, n);
    return upload(readWords<T>(file,n));
}
Access loadAccess(const std::string& rrgPath, const std::string& layoutPath,
                  const std::string& slrPath, U32 nodes, U64& bytes,
                  bool prepared, U32& savedSlrWords, U32& savedTypes) {
    std::ifstream layout(layoutPath), file(rrgPath,std::ios::binary), slrFile(slrPath,std::ios::binary);
    U32 slrWords, types, tiles;
    layout >> slrWords >> types >> tiles;
    need(slrWords>=6 && types && types<256 && tiles && tiles<4096,"Access lookup dimensions");
    std::vector<U32> ord(OrdinalCount),typeFlags(types),tileFlags(tiles);
    for (auto* values : {&ord,&typeFlags,&tileFlags}) for (auto& value : *values) layout >> value;
    U64 at[10];
    for (auto& value : at) layout >> value;
    need(bool(layout),"RRG layout metadata");
    savedSlrWords = slrWords;
    savedTypes = types;
    std::vector<int> slr;
    if (!prepared) slr = v20ReadSlr(slrFile, slrWords, types);
    bytes = (size_t(nodes)*10 + slrWords + OrdinalCount + types + tiles)*sizeof(U32);
    size_t freeBytes,totalBytes;
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    need(bytes+(size_t(1)<<30)<=freeBytes,"Access column budget with one GiB reserve");
    return Access{
        loadRrg<int>(file,at[0],nodes,"nodeType"), loadRrg<int>(file,at[1],nodes,"intentCode"),
        loadRrg<int>(file,at[2],nodes,"tileType"), loadRrg<int>(file,at[3],nodes,"tileId"),
        loadRrg<int>(file,at[4],nodes,"endTileX"), loadRrg<int>(file,at[5],nodes,"endTileYRaw"),
        loadRrg<int>(file,at[6],nodes,"tileY"), loadRrg<int>(file,at[7],nodes,"length"),
        prepared ? v20Load<int>("slr", nullptr, slrWords) : v20UploadVector("slr", slr),
        loadRrg<U32>(file,at[8],nodes,"baseCost"), loadRrg<U32>(file,at[9],nodes,"delay"),
        v20UploadVector("typeFlags", typeFlags), v20UploadVector("tileFlags", tileFlags),
        v20UploadVector("ordinals", ord)};
}



void publish(const std::string& path, const std::string& content) {
    const std::string pending = path + ".pending";
    std::ofstream out(pending);
    out << content;
    out.close();
    need(bool(out) && std::rename(pending.c_str(), path.c_str()) == 0, "Cannot publish GPU reply");
}

struct Change { U32 node; int oldOcc,oldSame; U32 oldHist; int occ,same; U32 hist; };
static_assert(sizeof(Change)==28,"Aggregate change row");
__global__ void mergeBeforeSearch(Mutable d,const Change* rows,U32 count,U32* errors,U32* histRows) {
    const U32 i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>=count) return;
    const auto r=rows[i];
    if (r.node>=d.n || d.occ[r.node]!=r.oldOcc || d.same[r.node]!=r.oldSame
            || d.hist[r.node]!=r.oldHist || r.same<0 || r.occ<0) {
        atomicMin(errors,i);
        return;
    }
    d.occ[r.node]=r.occ;
    d.same[r.node]=r.same;
    d.hist[r.node]=r.hist;
    if (r.hist!=r.oldHist) atomicAdd(histRows,1u);
}
void syncBeforeSearch(const std::string& step, Mutable data, U32 count,
                      U32 expectedHistory, bool refreshPcc) {
    need(count <= data.n && expectedHistory <= count, "Native mutation count");
    std::ifstream input(step + "/between.bin", std::ios::binary);
    need(bool(input), "Missing native state delta");
    const auto rows = readWords<Change>(input, count);
    need(input.peek() == EOF, "Unexpected bytes in native state delta");
    for (size_t i = 1; i < rows.size(); ++i) {
        need(rows[i - 1].node < rows[i].node, "Unsorted or duplicate mutation node");
    }

    if (count) {
        const auto mark = allocations.size();
        auto device = upload(rows);
        auto errors = allocate<U32>(1);
        auto history = allocate<U32>(1);
        checkCuda(setInStream(errors, 0xff, sizeof(U32)));
        checkCuda(setInStream(history, 0, sizeof(U32)));
        mergeBeforeSearch<<<(count + 127) / 128, 128>>>(data, device, count, errors, history);
        checkCuda(cudaGetLastError());
        const U32 first = checkDeviceErrors(errors);
        if (first != UINT32_MAX) {
            std::cerr << "AGGREGATE_MISMATCH node=" << rows[first].node << std::endl;
            throw std::runtime_error("Resident state differs from the native before-image");
        }
        need(checkDeviceErrors(history) == expectedHistory, "Missing or extra history mutations");
        releaseAfter(mark);
    }

    if (refreshPcc) {
        std::ifstream pccInput(step + "/new_pcc.bin", std::ios::binary);
        need(bool(pccInput), "Missing native PCC update");
        const auto pcc = readWords<U32>(pccInput, data.pccSize);
        need(pccInput.peek() == EOF, "Unexpected bytes in native PCC update");
        checkCuda(copyInStream(data.pcc, pcc.data(), pcc.size() * sizeof(U32), cudaMemcpyHostToDevice));
    }
    publish(step + "/device_synced", std::to_string(expectedHistory) + "\n");
}


int main(int argc, char** argv) try {
    need(argc == 11, "usage: full-hybrid snapshot nodes edges pcc-words output rrg layout slr mode channels");
    const U32 channels = std::stoul(argv[10]);
    need(channels >= 2 && channels <= MaxChannels, "Invalid multichannel width");
    const U32 runMode=std::stoul(argv[9]);
    need(runMode==0 || runMode==15,"Full-route paired mode");
    std::string out(argv[5]);
    std::string dir(argv[1]);
    const char* preparation = std::getenv("FIDES_V20_RRG_OFFSETS");
    v20Startup = preparation != nullptr;
    const U32 nodes = std::stoul(argv[2]), edges = std::stoul(argv[3]);
    const int pccSize = std::stoi(argv[4]);
    int count;
    v20Check("cudaGetDeviceCount", 0, cudaGetDeviceCount(&count));
    need(count == 1 && nodes && edges && pccSize > 0, "Resident input dimensions");
    cudaDeviceProp properties;
    v20Check("cudaGetDeviceProperties", 0, cudaGetDeviceProperties(&properties, 0));
    need(properties.major == 7 && properties.minor == 0, "Expected V100");
    size_t freeBytes, totalBytes;
    v20Check("cudaMemGetInfo", 0, cudaMemGetInfo(&freeBytes, &totalBytes));
    v20Event("context-ready", 0);
    int poolsSupported = 0;
    v20Check("cudaDeviceGetAttribute", 0, cudaDeviceGetAttribute(&poolsSupported, cudaDevAttrMemoryPoolsSupported, 0));
    need(poolsSupported == 1 && freeBytes > GpuReserve, "GPU requires stream-ordered allocation and reserve");
    allocationBudget = freeBytes - GpuReserve;
    const size_t csrBytes = (size_t(nodes) + 1 + edges) * sizeof(U32);
    const size_t stateBytes = (size_t(3) * nodes + pccSize) * sizeof(U32) + nodes;
    need(freeBytes >= csrBytes + stateBytes + (size_t(1) << 30), "Resident budget with one GiB reserve");
    U64 offsetAt=0, edgeAt=0, occAt=0, histAt=0, pccAt=0;
    std::ifstream rrgInput;
    if (preparation) {
        std::istringstream layout(preparation);
        need(bool(layout >> offsetAt >> edgeAt >> occAt >> histAt >> pccAt), "V20 RRG column offsets");
        rrgInput.open(argv[6], std::ios::binary);
        need(bool(rrgInput), "V20 cannot open static RRG");
        const size_t minimum = csrBytes + stateBytes + v20AccessBytes(argv[7], nodes) + csrBytes;
        v20Event("minimum-resident-budget", minimum);
        needMemory(minimum <= freeBytes - GpuReserve, "V20 shared data and one channel breach one-GiB reserve");
    }
    Mutable data{
        preparation ? loadRrg<U32>(rrgInput,offsetAt,size_t(nodes)+1,"offset") : loadColumn<U32>(dir,"offset.i32le",size_t(nodes)+1),
        preparation ? loadRrg<U32>(rrgInput,edgeAt,edges,"edge") : loadColumn<U32>(dir,"edge.i32le",edges),
        preparation ? loadRrg<int>(rrgInput,occAt,nodes,"occ") : loadColumn<int>(dir,"occ.i32le",nodes),
        preparation ? v20Load<int>("same",nullptr,nodes) : loadColumn<int>(dir,"same.i32le",nodes),
        preparation ? loadRrg<U32>(rrgInput,histAt,nodes,"hist") : loadColumn<U32>(dir,"hist.i32le",nodes),
        preparation ? loadRrg<U32>(rrgInput,pccAt,pccSize,"pcc") : loadColumn<U32>(dir,"pcc.i32le",pccSize),
        v20Load<unsigned char>("dirty",nullptr,nodes), nodes, pccSize};
    std::cout << "RESIDENT 1 " << csrBytes << " " << stateBytes << " " << freeBytes << std::endl;
    U64 accessBytes;
    U32 slrWords=0, slrTypes=0;
    auto access = loadAccess(argv[6],argv[7],argv[8],nodes,accessBytes,preparation != nullptr,slrWords,slrTypes);
    checkCuda(cudaMemGetInfo(&freeBytes,&totalBytes));
    const size_t mapEntries = size_t(nodes) + edges;
    need(mapEntries < UINT32_MAX, "Channel state map key space");
    activeFrontierKeys = static_cast<U32>(mapEntries);
    const size_t mapBytes = mapEntries * sizeof(U32);
    U32* activity;
    {
        V20Column column("activity");
        activity = allocate<U32>(MaxChannels + 1);
    }
    cudaMemPool_t pool;
    checkCuda(cudaDeviceGetDefaultMemPool(&pool, 0));
    const auto mapMark = allocations.size();
    std::vector<U32*> discovered;
    discovered.reserve(channels);

    auto growMaps = [&](U32 wanted) {
        while (discovered.size() < wanted) {
            U32* map;
            V20Column column("channel_state");
            try {
                map = allocate<U32>(mapEntries);
            } catch (const MemoryPressure&) {
                if (discovered.empty()) throw;
                break;
            }
            discovered.push_back(map);
            v20Check("cudaMemsetAsync", mapBytes, setInStream(map, 0xff, mapBytes));
        }
        V20Column column("channel_state");
        v20Check("cudaStreamSynchronize", mapBytes * discovered.size(), cudaStreamSynchronize(cudaStreamPerThread));
    };
    auto trimMaps = [&](U32 keep) {
        need(keep >= 1 && keep <= discovered.size(), "Invalid resident map trim");
        need(allocations.size() == mapMark + discovered.size(), "GPU scratch outlived its group");
        releaseAfter(mapMark + keep);
        discovered.resize(keep);
        checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
        checkCuda(cudaMemPoolTrimTo(pool, 0));
    };

    U32 residentLanes = 1;
    if (const char* envLanes = std::getenv("FIDES_V23_RESIDENT_LANES")) {
        residentLanes = std::min<U32>(channels, std::max<U32>(1u, static_cast<U32>(std::stoul(envLanes))));
    }
    growMaps(preparation ? residentLanes : channels);
    constexpr U32 BatchesBeforeGrowing = 32;
    U32 cleanBatches = 0;
    std::cout << "STATE_MAP " << discovered.size() << " " << mapBytes << std::endl;
    std::cout << "ACCESS " << accessBytes << std::endl;
    bool overlapObserved = false;
    v20ReserveFloor("preflight-ready");
    publish(out + "/resident_ready", "ready\n");
    if (preparation) {
        std::string command;
        need(bool(std::cin >> command), "V20 lost startup command");
        if (command == "finish") return 0;
        need(command == "load_first" && bool(std::cin >> dir), "V20 expected first live snapshot");
        out = dir;
        v20LiveColumn(dir,"occ.i32le",data.occ,nodes);
        v20LiveColumn(dir,"same.i32le",data.same,nodes);
        v20LiveColumn(dir,"hist.i32le",data.hist,nodes);
        v20LiveColumn(dir,"pcc.i32le",data.pcc,pccSize);
        std::ifstream slrFile(dir + "/slr.i32le", std::ios::binary);
        auto slr = v20ReadSlr(slrFile,slrWords,slrTypes);
        {
            V20Column column("slr");
            v20Check("cudaMemcpyAsync", slr.size()*sizeof(int),
                     cudaMemcpyAsync(const_cast<int*>(access.slr),slr.data(),slr.size()*sizeof(int),
                                     cudaMemcpyHostToDevice,cudaStreamPerThread));
            v20Check("cudaStreamSynchronize", slr.size()*sizeof(int),cudaStreamSynchronize(cudaStreamPerThread));
        }
        v20ReserveFloor("live-snapshot-ready");
        publish(out + "/resident_snapshot_loaded", "ready\n");
    }
    v20Startup = false;

    for (U32 phase=1;;++phase) {
        std::string operation;
        U32 count;
        need(bool(std::cin>>operation),"Lost native ordered command stream");
        if (operation=="finish") break;
        const std::string step=dir+"/step"+std::to_string(phase);
        if (phase>1) {
            U32 expectedHistory, refreshPcc;
            need(operation == "sync" && bool(std::cin >> count >> expectedHistory >> refreshPcc)
                     && refreshPcc <= 1, "Missing native handoff synchronization");
            syncBeforeSearch(step, data, count, expectedHistory, refreshPcc != 0);
            need(bool(std::cin>>operation),"Missing original route request");
        }
        U32 members;
        const bool growthGranted = operation == "route_scored_grow";
        const bool scored = operation == "route_scored" || growthGranted;
        need((operation == "route" || scored) && bool(std::cin >> count >> members) && count == phase
                && members >= 1 && members <= channels, "Unexpected native batch request");
        U32 featureScores[MaxChannels]{};
        if (scored) {
            for (U32 member = 0; member < members; ++member) {
                need(bool(std::cin >> featureScores[member]) && featureScores[member] <= 0x7fffffffu,
                     "Invalid live feature score");
            }
        }
        if ((!preparation || growthGranted) && cleanBatches == BatchesBeforeGrowing
                && members > discovered.size()) {
            const U32 before = static_cast<U32>(discovered.size());
            growMaps(std::min(members, 2 * before));
            if (discovered.size() != before) {
                std::cout << "GPU_MAPS_GROW " << phase << " " << before << " " << discovered.size() << std::endl;
            }
            cleanBatches = 0;
        }

        // All groups read the same image after syncBeforeSearch. Successful
        // responses are held until every original member has a GPU result.
        std::vector<U32> remaining;
        for (U32 member = 0; member < members; ++member) remaining.push_back(member);
        bool retriedSingle[MaxChannels]{};
        U32 groups = 0, retries = 0, maxLaunched = 0, maxCompleted = 0, mask = 0;
        U32 scoredGroups = 0;
        while (!remaining.empty()) {
            const U32 width = static_cast<U32>(std::min(remaining.size(), discovered.size()));
            std::vector<U32> group(remaining.begin(), remaining.begin() + width);
            bool appliedScores = false;
            if (scored && width > 1 && remaining.size() > width) {
                // The allocator already requires a split. Estimate each group's work
                // using its member score sum, preserving the existing width of every group.
                const size_t parts = (remaining.size() + width - 1) / width;
                std::vector<U32> ordered = remaining;
                std::sort(ordered.begin(), ordered.end(), [&](U32 left, U32 right) {
                    if (featureScores[left] != featureScores[right]) {
                        return featureScores[left] > featureScores[right];
                    }
                    return left < right;
                });
                U64 originalPeak = 0;
                for (size_t part = 0; part < parts; ++part) {
                    U64 load = 0;
                    const size_t end = std::min(remaining.size(), (part + 1) * width);
                    for (size_t index = part * width; index < end; ++index) {
                        load += featureScores[remaining[index]];
                    }
                    originalPeak = std::max(originalPeak, load);
                }
                std::vector<U64> loads(parts, 0);
                std::vector<U32> filled(parts, 0);
                std::vector<U32> balanced;
                balanced.reserve(width);
                for (const U32 member : ordered) {
                    size_t selected = parts;
                    for (size_t part = 0; part < parts; ++part) {
                        const size_t capacity = std::min<size_t>(width, remaining.size() - part * width);
                        if (filled[part] < capacity && (selected == parts || loads[part] < loads[selected])) {
                            selected = part;
                        }
                    }
                    need(selected != parts, "Unable to balance frozen GPU split");
                    ++filled[selected];
                    loads[selected] += featureScores[member];
                    if (selected == 0) balanced.push_back(member);
                }
                if (*std::max_element(loads.begin(), loads.end()) < originalPeak) {
                    std::sort(balanced.begin(), balanced.end());
                    appliedScores = balanced != group;
                    if (appliedScores) {
                        group.swap(balanced);
                        ++scoredGroups;
                    }
                }
            }
            if (appliedScores) {
                for (const U32 member : group) {
                    remaining.erase(std::find(remaining.begin(), remaining.end(), member));
                }
            } else {
                remaining.erase(remaining.begin(), remaining.begin() + width);
            }
            ++groups;
            maxLaunched = std::max(maxLaunched, width);
            const bool probe = width > 1 && !overlapObserved;
            if (probe) checkCuda(setInStream(activity, 0, sizeof(U32) * (MaxChannels + 1)));
            checkCuda(cudaStreamSynchronize(cudaStreamPerThread));

            auto route = [&](U32 slot) -> std::string {
                checkCuda(cudaSetDevice(0));
                const auto mark = allocations.size();
                const U32 member = group[slot];
                const auto path = step + "/member" + std::to_string(member);
                try {
                    solve(path + ".response", data, runMode, access, discovered[slot],
                          path + ".attempt", probe ? activity : nullptr, member);
                } catch (const MemoryPressure& error) {
                    releaseAfter(mark);
                    // Pressure may interrupt projection before its sparse reset.
                    checkCuda(setInStream(discovered[slot], 0xff, mapBytes));
                    checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
                    return error.what();
                }
                releaseAfter(mark);
                checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
                return {};
            };

            std::vector<std::string> outcomes(width);
            if (width == 1) {
                outcomes[0] = route(0);
            } else {
                std::vector<std::future<std::string>> tasks;
                tasks.reserve(width);
                std::exception_ptr fatal;
                try {
                    for (U32 slot = 0; slot < width; ++slot) {
                        tasks.push_back(std::async(std::launch::async, route, slot));
                    }
                } catch (...) {
                    fatal = std::current_exception();
                }
                for (U32 slot = 0; slot < tasks.size(); ++slot) {
                    try {
                        outcomes[slot] = tasks[slot].get();
                    } catch (...) {
                        if (!fatal) fatal = std::current_exception();
                    }
                }
                if (fatal) std::rethrow_exception(fatal);
            }

            std::vector<U32> failed;
            for (U32 slot = 0; slot < width; ++slot) {
                if (!outcomes[slot].empty()) failed.push_back(group[slot]);
            }
            maxCompleted = std::max(maxCompleted, width - static_cast<U32>(failed.size()));
            if (probe) {
                U32 witness[MaxChannels + 1];
                checkCuda(copyInStream(witness, activity, sizeof(witness), cudaMemcpyDeviceToHost));
                for (U32 member = 0; member < MaxChannels; ++member) {
                    need(witness[member] == 0, "Unclosed channel kernel witness");
                }
                if (failed.empty() && witness[MaxChannels] != 0) {
                    mask = witness[MaxChannels];
                    overlapObserved = true;
                    std::cout << "GPU_KERNEL_OVERLAP " << phase << " " << mask << std::endl;
                }
            }
            if (failed.empty()) continue;

            retries += static_cast<U32>(failed.size());
            const U32 before = static_cast<U32>(discovered.size());
            const U32 keep = std::max(1u, width / 2);
            auto first = std::find(group.begin(), group.end(), failed[0]) - group.begin();
            if (width == 1 && before == 1) {
                if (retriedSingle[failed[0]]) {
                    throw MemoryPressure("Single GPU member cannot fit with one-GiB reserve: " + outcomes[0]);
                }
                retriedSingle[failed[0]] = true;
            }
            trimMaps(keep);
            std::cout << "GPU_MEMORY_PRESSURE " << phase << " " << width << " " << failed.size()
                      << " " << before << " " << discovered.size() << " " << outcomes[first] << std::endl;
            remaining.insert(remaining.begin(), failed.begin(), failed.end());
        }

        checkCuda(cudaMemGetInfo(&freeBytes, &totalBytes));
        if (freeBytes < GpuReserve) {
            trimMaps(static_cast<U32>(discovered.size()));
            checkCuda(cudaMemGetInfo(&freeBytes, &totalBytes));
        }
        need(freeBytes >= GpuReserve, "One-GiB reserve after concurrent search");
        cleanBatches = retries == 0 ? std::min(cleanBatches + 1, BatchesBeforeGrowing) : 0;
        std::ostringstream concurrency;
        concurrency << "{\"maximum_launched_width\":" << maxLaunched
                    << ",\"maximum_completed_width\":" << maxCompleted
                    << ",\"groups_launched\":" << groups
                    << ",\"allocation_retries\":" << retries
                    << ",\"score_balanced_groups\":" << scoredGroups
                    << ",\"resident_maps\":" << discovered.size() << "}\n";
        publish(step + "/device_concurrency.json", concurrency.str());
        publish(step + "/device_batch.txt", std::to_string(phase) + " " + std::to_string(members)
                + " " + std::to_string(mask) + " " + std::to_string(freeBytes) + "\n");
    }
    releaseAfter(0);
    checkCuda(cudaStreamSynchronize(cudaStreamPerThread));
    return 0;
} catch (const std::exception& error) {
    std::cerr << error.what() << std::endl;
    return 1;
}

