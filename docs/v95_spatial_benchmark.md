# v95 Spatial Region Isolation Benchmark Report (`2 × V100`, `lutPinSwapping=false`)

> **Build & Runner**: `theia50per-ppt-hybrid-v95` (`v95-t6`, [deploy_v95.py](../scripts/v95/deploy_v95.py), [prepare_v95.py](../scripts/v95/prepare_v95.py), [v95_patch.py](../scripts/v95/v95_patch.py), [run_all13.py](../scripts/all13/run_all13.py))  
> **Machine-Readable Results**: [v95_all13/summary.jsonl](../results/v95_all13/summary.jsonl) (`driver_v95t6_all13_20261127_233312.log`, 13/13 PASS)  
> **Hardware Platform (`gpu02`)**: `2 × Intel Xeon Gold 5220` (`36` cores / `36` threads, `2` NUMA nodes, `376 GiB` RAM), **`2 × NVIDIA Tesla V100-PCIE-32GB` (`--gpus 1,3`, cross-NUMA `SYS`)**. Target FPGA: `xcvu13p-fhgb2104-2-e`, **`lutPinSwapping=false`**, `lutRoutethru=false`, `--nonTimingDriven`.

---

## 1. Architecture Summary (`v94 → v95-t6`: Isolated Spatial Region Partitioning)

To increase multi-GPU isolation on 2 GPUs, `v95-t6` partitions **Round 1** `Pass 1` (`routeIter == 1 && passItems >= 32,768`) into mutually isolated spatial sub-regions routed concurrently on dedicated per-GPU pipelines (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.h`, `src/cpp/fides_gpu.cu`):

1. **Per-Connection `ItemBBox` + SLL Span Guard**: Computes static/dynamic/prior-path bounds per connection plus actual `G.maxSllSpanY` during `preinitStatic` (`sllMargin = G.maxSllSpanY + 4` for cross-SLR edges, `±1` for same-SLR edges). Evaluates `10%–90%` X/Y cuts weighted by `popW = 128 + min(256, doubleHpwl)` and scored by `(min(w0, w1) - wb) * stride` (`evalCuts`, `v95-t6`) to balance dual-GPU work while minimizing cross-cut boundary edges.
2. **Independent Concurrent Region Execution (`reg0 || reg1` then `bound`)**:
   - `reg0` and `reg1` execute in parallel across two GPUs via dedicated `g_regPools[2]` (`WaveThreadPool`), independent CUDA streams/pinned delta & mega-scope buffers (`fidesGpuApplyOccDeltasDev`, `fidesGpuUploadMegaScopesBatchDev`, `fidesGpuApplyMegaPatchesDev`), and isolated per-device 512-slot mega-caches (`megaSlotReg[2]`, `megaDirtyReg[2]`) with striped locks (`g_regNetMtx[4096]`) for shared nets.
   - After `reg0 || reg1` join, dirty occupancy deltas sync across devices (`flushGpuOccDeltas` / `fidesGpuApplyOccDeltasDev`), followed by cross-cut `bound` routing to preserve `HPWL_FIRST`. Round 2+ reuses `v94` LPT wavefronts + `RouteTail` convergence.
3. **Adaptive Cross-SLR Isolation Gate (`F4_V95_SPLIT_SKIP`)**: When no cut keeps boundary connections `<= 25%` and min-region work `>= 25%` (e.g., 4-SLR `THEIA_50per`, which has `w0=75.7M` vs `w1=1.20B`), `v95-t6` skips Layer-1 and runs `v94` LPT wavefront scheduling.

---

## 2. Verified 13-Case Results (`v95-t6` vs `v94` vs `v76`, `lutPinSwapping=false`)

All `13 / 13` cases ran from a single unified `v95-t6` build on `gpu02 --gpus 1,3` and passed both in-process 64-shard legality checking and cold-JVM `F4BaselineDcpCheck` (`unroutedPins=0, sharedPipNodes=0`). Note: both `v95-t6` and `v94` were measured **without LUT pin swapping (`lutPinSwapping=false`)**.

| Case | Layer-1 R1 Cut (`v95-t6`) & `reg0` vs `reg1` | Boundary Conns | `v95-t6 E2E (s)` | `v94 E2E (s)` | `v95-t6 rdes (s)` | `v94 rdes (s)` | Rounds (`t6`/`v94`) | Verified `PIPs` |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :---: | ---: |
| `mcml_RLE` | `small<32k` (skipped) | — | **21.68** | 21.92 | **12.22** | 12.40 | 45 / 45 | 149,392 |
| `oc_des_des3perf` | `small<32k` (skipped) | — | **19.46** | 20.88 | **10.37** | 10.96 | 16 / 17 | 187,600 |
| `RLE_BlobMerging` | `small<32k` (skipped) | — | **18.14** | 28.22 | **9.31** | 9.64 | 14 / 9 | 135,647 |
| `aquarius_b15_1` | `small<32k` (skipped) | — | **19.32** | 25.94 | **9.90** | 10.17 | 19 / 22 | 209,834 |
| `RS8FreqDecode` | `small<32k` (skipped) | — | **19.08** | 25.47 | **10.02** | 10.05 | 10 / 12 | 185,099 |
| `k_100_dsp` | `Y=[372,374]`: `318,255 (0.72s)` vs `315,988 (0.78s)` | 41,953 | 57.25 | **56.32** | **25.51** | 26.91 | 8 / 13 | 3,656,485 |
| `k_300dsp_300ram` | `Y=[398,400]`: `319,399 (0.70s)` vs `318,132 (0.69s)` | 39,013 | 65.12 | **62.40** | 26.35 | **24.54** | 11 / 6 | 3,605,971 |
| `k_140000fd_500000lut` | `Y=[392,394]`: `313,937 (0.72s)` vs `311,727 (0.74s)` | 54,929 | **53.72** | 60.64 | **24.73** | 26.23 | 11 / 8 | 3,694,709 |
| `THEIA_jpeg` | `Y=[378,380]`: `289,013 (1.89s)` vs `278,480 (1.51s)` | 72,144 | **111.08** | 135.68 | **77.66** | 97.90 | 21 / 41 | 5,024,251 |
| `THEIA` | `Y=[471,473]`: `502,763 (2.95s)` vs `504,229 (2.69s)` | 26,201 | **139.64** | 142.46 | **106.37** | 109.82 | 26 / 30 | 7,846,696 |
| `k_800_ram` | `Y=[342,344]`: `1,008,292 (2.39s)` vs `1,007,335 (2.31s)` | 57,042 | **156.85** | 163.13 | **43.07** | 46.36 | 15 / 14 | 10,507,593 |
| `THEIA_20per` | `Y=[447,449]`: `1,191,839 (6.34s)` vs `1,166,728 (6.32s)` | 99,737 | 433.57 | **347.95** | 371.28 | **287.48** | 66 / 50 | 18,922,093 |
| `THEIA_50per` | `SPLIT_SKIP` (`Y=[174,176]`, `c0=446k`, `c1=4.36M`) | 104,211 | 836.64 | **723.85** | 734.89 | **625.88** | 63 / 56 | 31,074,328 |
| **Total (13 cases)** | **`v95-t6` (`--gpus 1,3`) vs `v94` (`--gpus 2,3`)** | — | **1,951.57** | **1,814.86** | **1,461.68** | **1,298.34** | **13/13 PASS** | `46,719,853` WL |

- **vs `v76` (`2,092.91 s` E2E, with 4×V100 on `THEIA_50per`)**: `v95-t6` (`2 × V100` throughout) beats `v76` by **`-141.34 s`**, reducing total wirelength verification-cleanly (`13 / 13 PASS`).
- **vs `v94` (`1,814.86 s` E2E, `--gpus 2,3`, `lutPinSwapping=false`)**: `v95-t6` wins on `9 / 13` cases in E2E (notably `THEIA_jpeg`: `111.08 s` vs `135.68 s`, `-18.1%`; `k_140000fd`: `-6.92 s`; `k_800_ram`: `-6.28 s`; `THEIA`: `-2.82 s`), while lagging on `THEIA_20per` (`+85.62 s`, `bound=99,737` raised congestion rounds `66 vs 50`) and `THEIA_50per` (`+112.79 s`, `SPLIT_SKIP` on cross-NUMA `--gpus 1,3`).

---

## 3. Iteration History Note (`v95-t3 → v95-t6`)

- `v95-t3` / `v95-t4`: Applied Layer-1 spatial cut in both R1 and R2; under `v95-t4`, `THEIA_20per` hit a `RouteTail` stall (`716.51 s`) due to modified tail bounds.
- `v95-t6` (current): Restricted spatial cut to R1 (`routeIter == 1`), refined `evalCuts` to `(min(w0, w1) - wb) * stride`, and restored `v94` `RouteTail`, recovering `13 / 13 PASS` in `1,951.57 s` (`-417.30 s` vs `v95-t4/t3`'s `2,368.87 s`).
