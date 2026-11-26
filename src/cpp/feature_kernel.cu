#include <cstdint>
struct Input {
    int32_t id, xlo, xhi, ylo, yhi, hpwl, fanout, source_slr, sink_slr;
};
struct Output {
    uint64_t id, width, height, area, span, hpwl, fanout_log2, cross_slr, packed;
};
static_assert(sizeof(Input)==36 && sizeof(Output)==72, "Unexpected file layout");

extern "C" __global__ void route_feature_score(const Input* rows, Output* scores, int count, uint64_t area_cap, uint64_t span_cap) {
    int i=blockIdx.x*blockDim.x+threadIdx.x;
    if (i>=count) return;
    Input c=rows[i];
    uint64_t w=int64_t(c.xhi)-c.xlo+1, h=int64_t(c.yhi)-c.ylo+1;
    uint64_t area=w*h, span=w+h;
    uint64_t hpwl=c.hpwl>0 ? c.hpwl : 0;
    uint64_t logfan=c.fanout>0 ? 31-__clz(static_cast<unsigned>(c.fanout)) : 0;
    uint64_t cross=c.source_slr!=c.sink_slr;
    uint64_t work=area+32*span+64*hpwl+256*logfan+4096*cross;
    if (work>0x7fffffffULL) work=0x7fffffffULL;
    uint64_t eligible=area<=area_cap && span<=span_cap;
    scores[i]={static_cast<uint64_t>(c.id),w,h,area,span,hpwl,logfan,cross,work|(eligible<<31)};
}

