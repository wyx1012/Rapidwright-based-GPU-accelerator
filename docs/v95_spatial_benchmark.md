# v95 Spatial Region Isolation Benchmark Report (`2 × V100`)

> **Build & Runner**: `theia50per-ppt-hybrid-v95` ([deploy_v95.py](../scripts/v95/deploy_v95.py), [prepare_v95.py](../scripts/v95/prepare_v95.py), [v95_patch.py](../scripts/v95/v95_patch.py), [run_all13.py](../scripts/all13/run_all13.py))  
> **Machine-Readable Results**: [v95_all13/summary.jsonl](../results/v95_all13/summary.jsonl)  
> **Hardware Platform (`gpu02`)**: `2 × Intel Xeon Gold 5220` (`36` cores / `36` threads, `2` NUMA nodes, `376 GiB` RAM), **`2 × NVIDIA Tesla V100-PCIE-32GB` (`--gpus 1,3`, cross-NUMA `SYS`)**. Target FPGA: `xcvu13p-fhgb2104-2-e`, `lutPinSwapping=false`, `--nonTimingDriven`.

---

## 1. Architecture Summary (`v94 → v95`: Isolated Spatial Region Partitioning)

To increase multi-GPU isolation on 2 GPUs, `v95` partitions Round 1 & Round 2 `Pass 1` (`passItems >= 32,768`) into mutually isolated spatial sub-regions routed concurrently on dedicated per-GPU pipelines (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.h`, `src/cpp/fides_gpu.cu`):

1. **Per-Connection `ItemBBox` + SLL Span Guard**: Computes static/dynamic/prior-path bounds per connection plus actual `G.maxSllSpanY` during `preinitStatic` (`sllMargin = G.maxSllSpanY + 4` for cross-SLR edges, `±1` for same-SLR edges). Evaluates `10%–90%` X/Y cuts weighted by `(G.lastPops > 0) ? G.lastPops : (128 + min(256, doubleHpwl))` (`v95-t4`) to equalize GPU runtime.
2. **Independent Concurrent Region Execution (`reg0 || reg1` then `bound`)**:
   - `reg0` and `reg1` execute in parallel across two GPUs via dedicated `g_regPools[2]` (`WaveThreadPool`), independent CUDA streams/pinned delta & mega-scope buffers (`fidesGpuApplyOccDeltasDev`, `fidesGpuUploadMegaScopesBatchDev`, `fidesGpuApplyMegaPatchesDev`), and isolated per-device 512-slot mega-caches (`megaSlotReg[2]`, `megaDirtyReg[2]`) with striped locks (`g_regNetMtx[4096]`) for shared nets.
   - After `reg0 || reg1` join, dirty occupancy deltas sync across devices (`flushGpuOccDeltas` / `fidesGpuApplyOccDeltasDev`), followed by cross-cut `bound` routing to preserve `HPWL_FIRST`.
3. **Adaptive Cross-SLR Isolation Gate (`F4_V95_SPLIT_SKIP`)**: When no cut keeps boundary connections `<= 25%` and min-region work `>= 25%` (e.g., 4-SLR `THEIA_50per`, which has `33%` boundary connections), `v95` cleanly skips Layer-1 and runs `v94` LPT wavefront scheduling (`targetWave = 8192`).

---

## 2. Recorded 13-Case Results (`12` `v95-t4` + `1` `v95-t3`, Double-Verified PASS)

> **Provenance Note**: `12` benchmarks below were verified under `v95-t4` (`--gpus 1,3`). `THEIA_50per` under `v95-t4` was terminated at Round 73 after tail stall (`fb=1..5`) when GPUs 0/1/2 became occupied by co-resident jobs; its verified PASS entry below comes from the preceding `v95-t3` run (`--gpus 1,3`). Also shown in footnotes are the `v95-t3` results on `THEIA` (`141.60 s`) and `THEIA_20per` (`349.70 s`).

| Case | Build | Layer-1 Cut & `reg0` vs `reg1` (R1) | Boundary % | `v95 E2E (s)` | `v94 E2E (s)` | `v95 rdes (s)` | `v94 rdes (s)` | Rounds (`v95`/`v94`) | Verified `PIPs` (`unroutedPins=0, sharedPipNodes=0`) |
| :--- | :---: | :--- | :---: | ---: | ---: | ---: | ---: | :---: | ---: |
| `mcml_RLE` | `v95-t4` | `< 32,768` conns | — | 50.86* | **21.92** | **12.09** | 12.40 | 45 / 45 | 149,084 |
| `oc_des_des3perf` | `v95-t4` | `< 32,768` conns | — | 26.17 | **20.88** | **10.44** | 10.96 | 16 / 17 | 188,864 |
| `RLE_BlobMerging` | `v95-t4` | `< 32,768` conns | — | **18.48** | 28.22 | **9.44** | 9.64 | 11 / 9 | 135,035 |
| `aquarius_b15_1` | `v95-t4` | `< 32,768` conns | — | **19.21** | 25.94 | 10.23 | **10.17** | 30 / 22 | 211,411 |
| `RS8FreqDecode` | `v95-t4` | `< 32,768` conns | — | **19.32** | 25.47 | 10.44 | **10.05** | 11 / 12 | 185,406 |
| `k_100_dsp` | `v95-t4` | Y `[372,374]`: `318,255 (0.70s)` vs `315,988 (0.79s)` | 6.2% | 67.12 | **56.32** | **25.54** | 26.91 | 9 / 13 | 3,658,762 |
| `k_300dsp_300ram` | `v95-t4` | Y `[398,400]`: `319,399 (0.69s)` vs `318,132 (0.70s)` | 5.8% | **61.71** | 62.40 | 24.97 | **24.54** | 8 / 6 | 3,610,295 |
| `k_140000fd_500000lut` | `v95-t4` | Y `[392,394]`: `313,937 (0.71s)` vs `311,727 (0.73s)` | 8.1% | **54.60** | 60.64 | **23.78** | 26.23 | 7 / 8 | 3,698,811 |
| `THEIA_jpeg` | `v95-t4` | Y `[378,380]`: `289,013 (1.89s)` vs `278,480 (1.48s)` | 11.3% | **116.90** | 135.68 | **82.61** | 97.90 | 29 / 41 | 5,023,980 |
| `THEIA` | `v95-t4` | Y `[471,473]`: `502,763 (2.90s)` vs `504,229 (2.63s)` | 2.5% | 151.29** | **142.46** | 114.91** | **109.82** | 31 / 30 | 7,854,225 |
| `k_800_ram` | `v95-t4` | Y `[342,344]`: `1,008,292 (2.39s)` vs `1,007,335 (2.32s)` | 2.75% | 237.23* | **163.13** | **43.22** | 46.36 | 11 / 14 | 10,510,109 |
| `THEIA_20per` | `v95-t4` | Y `[447,449]`: `1,191,839 (6.30s)` vs `1,166,728 (6.10s)` | 4.06% | 716.51** | **347.95** | 649.16** | **287.48** | 77 / 50 | 18,910,756 |
| `THEIA_50per` | `v95-t3` | `F4_V95_SPLIT_SKIP` (`cb=33% > 25%`) | 33.2% | 829.47 | **723.85** | 724.47 | **625.88** | 64 / 56 | 31,077,182 |
| **Sum (13 cases)** | **`v95-t4/t3`** | **7 Layer-1 isolated, 1 skipped, 5 `<32k`** | — | **2,368.87** | **1,814.86** | **1,741.30** | **1,298.34** | — | **13 / 13 PASS** |

*\*`mcml_RLE` waited ~30s for previous GPU memory cleanup; `k_800_ram` E2E (`237.23 s`) saw concurrent host/GPU startup from co-resident jobs starting at 22:42, while its `Route design` time dropped to **`43.22 s`**.*  
*\*\*Under `v95-t3` (`--gpus 1,3`), `THEIA` passed in **`141.60 s`** E2E (`110.41 s` route, 31 rds) and `THEIA_20per` passed in **`349.70 s`** E2E (**`286.74 s`** route, 53 rds). Under `v95-t4`, `THEIA_20per` R1–R2 achieved `9.28 s` / `7.10 s` before encountering a 22-round serial `RouteTail` oscillation (`R55–R77`, `over=1..3`) before `routeIter >= 78` unlocked `maxUnboundedThisRound = 8`.*
