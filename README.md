# Rapidwright-based-GPU-accelerator

Multi-GPU (`Tesla V100-PCIE-32GB`) + 36-core NUMA C++/CUDA/Java hybrid router accelerator built on RapidWright for runtime-first large-scale FPGA detailed routing across all 13 competition benchmarks (`xcvu13p-fhgb2104-2-e`; largest design `THEIA_50per`: `4,909,155` indirect connections, `43,564` direct connections, `92,497,168` RRG nodes, `391,524,527` edges).

## Latest Milestone (`v94` — All 13/13 Double-Verified PASS on `2 × V100`)

Due to co-resident workloads occupying GPUs `[0, 1]` on `gpu02`, **`v94` ran all 13 competition designs strictly isolated on `2 × Tesla V100-PCIE-32GB` (`--gpus 2,3`)** and achieved a new overall best while beating `v76` (`4 × V100` on `THEIA_50per`) and achieving **`25.08×`** end-to-end speedup over the local Xeon Gold 5220 Java CPU baseline:

| Benchmark / Suite | `v94` (`2 × V100`) | `v76` (`THEIA_50per` `4×`, 12 `2×`) | `v70` (`4 × V100`) | Local Java CPU (`CPU布线日志.txt`)* | Server Native Java (`gpu02`, `Route design` only)** |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **13-Case Total `E2E`** | **`1,814.86 s` (`30.25 min`)** | `2,092.91 s` (`-278.05 s`) | `2,798.95 s` (`-984.09 s`) | `45,517.14 s` (**`25.08×`** speedup) | — |
| **13-Case Total `Route design`** | **`1,298.34 s`** | `1,353.98 s` (`-55.64 s`) | `2,248.16 s` (`-949.82 s`) | `45,240.79 s` (**`34.85×`** speedup) | — |
| **`THEIA_50per` `E2E` / `Route design`** | `723.85 s` / `625.88 s` (`2×V100`, `56` rds) | `719.80 s` / `601.24 s` (`4×V100`, `58` rds) | `798.97 s` / `684.62 s` (`4×V100`, `64` rds) | `29,717.56 s` / `29,665.52 s` (**`41.05×`** E2E) | `Route design` only: **`68,237.38 s`** (**`109.0×`** Route speedup) |
| **`THEIA_20per` `E2E` / `Route design`** | **`347.95 s` / `287.48 s`** (`50` rds) | `363.07 s` / `295.62 s` (`49` rds) | `1,162.83 s` / `1,096.54 s` (`74` rds) | `9,247.26 s` / `9,217.76 s` (**`26.58×`** E2E) | — |
| **`k_800_ram` `E2E` / `Route design`** | **`163.13 s` / `46.36 s`** (`14` rds) | `297.83 s` / `54.88 s` (`12` rds) | `184.60 s` / `60.50 s` (`14` rds) | `621.18 s` / `528.35 s` (**`3.81×`** E2E) | — |
| **DCP Legality** | **13 / 13 Double-Verified PASS** | 13 / 13 Double-Verified PASS | 13 / 13 Double-Verified PASS | 13 / 13 `over=0` | `unroutedPins=0, sharedPipNodes=0` |

*\*Local Java CPU baseline (`Intel Xeon Gold 5220 @2.20 GHz`, `CPU布线日志.txt`): sums `Read DCP + Route design + Write DCP`, with `lutPinSwapping=true` enabled on the 9 non-`k` designs.*  
*\*\*Server native Java reference on `gpu02` (`lutPinSwapping=false`, `-XX:ActiveProcessorCount=4`): measures **`Route design` only** (not full E2E), e.g., `THEIA_50per Route design = 68,237.38 s`, `THEIA_jpeg Route design = 3,573–3,806 s`.*

- **PPT-Style Technical Report (`v94`)**: [docs/v94_ppt_report.md](docs/v94_ppt_report.md)
- **Detailed 13-Case Benchmark Sheet (`v94`)**: [docs/v94_all13_benchmark.md](docs/v94_all13_benchmark.md) & [results/v94_all13/summary.jsonl](results/v94_all13/summary.jsonl)
- **Design note (not implemented): GPU acceleration with `lutPinSwapping=true`**: [docs/pin_swap_gpu_plan.md](docs/pin_swap_gpu_plan.md)
- **Experimental `v95` Spatial Isolation Study (`2 × V100`)**: [docs/v95_spatial_benchmark.md](docs/v95_spatial_benchmark.md) & [results/v95_all13/summary.jsonl](results/v95_all13/summary.jsonl) ([v95 plan](docs/v95_optimization_plan.md))

## Experimental `v95`: Isolated Spatial Region Partitioning (`2 × V100`)

`v95` extends `v94` by partitioning Round 1–2 `Pass 1` connections into mutually isolated `reg0` and `reg1` spatial regions routed concurrently on separate CUDA streams, `WaveThreadPool` workers, and 512-slot per-region mega-caches before syncing dirty occupancies and routing cross-cut `bound` nets (`F4_V95_SPLIT_SKIP` guard falls back to `v94` LPT if boundary > 25%, e.g., 4-SLR `THEIA_50per`).

- On spatial-isolated benchmarks (`7/7` applicable designs achieved **98%–99.9%** `reg0`/`reg1` balance with only `2.5%–11.3%` boundary connections), `v95-t4` sped up `THEIA_jpeg` (**`135.68 s → 116.90 s`**, `-13.8%` E2E / `29` vs `41` rds), `k_140000fd_500000lut` (**`60.64 s → 54.60 s`**, `-10.0%`), `k_800_ram` `Route design` (**`46.36 s → 43.22 s`**, `-6.8%`, `11` vs `14` rds), and `k_100_dsp` `Route design` (`26.91 s → 25.54 s`).
- `v94` remains the lowest full-suite 13-case total (`1,814.86 s`), as `v95-t4` hit a `22`-round serial `RouteTail` stall on `THEIA_20per` (`716.51 s`, vs `349.70 s` E2E / `286.74 s` Route under `v95-t3`) and `THEIA_50per` in [results/v95_all13/summary.jsonl](results/v95_all13/summary.jsonl) records the `v95-t3` PASS (`829.47 s`) while `v95-t4` tail stabilization (`v95-t5`) proceeds.

## Key Engineering Highlights (`v22 -> v94`)

1. **In-Process Dual-Engine C++/CUDA Core (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.cu`)**:
   - Zero-IPC JNI bridge + multi-GPU `Tier-A` warp wavefront (`2,048` warps on `2×V100`, `4,096` on `4×V100`) and `Tier-B1` CTA wavefront (`160` CTAs / `40,960` threads on `2×V100`, `8M`-slot closed hash table) with LPT net-sharded dispatch and 36-core lock-free CPU `DynamicExecutor`.
   - **`v94` `hasPrePicked` Wave-Cap Fix & `2.8M` Pass 3 Overlap**: Fixes the `hasPrePicked` guard that previously skipped mid-wave `R3/R4` spill limits (cutting `THEIA_50per R3+R4` from `120.32 s` to `68.99 s`), drains tiny Pass 3 tails (`<64` picked / `<96` total), and overlaps heavy Pass 3 nets (`> 2.8M` pops) on 36 CPU cores concurrently with GPU `Tier-B1`.
   - **SoA & Early-Skip CPU A\* (`v76+`)**: `16B NodeGeomHot` + `8B NodeGeomCold`, `PF_UTURN_DELAY` bit-packing, thread-local `S.slotForRead(child)` pre-skip, division elimination, and parallel `nativeUpdateHistAndFeedback`.
2. **End-to-End Fixed-Stage Pipelining & Fast Cache Loading (`v67 -> v94`)**:
   - **`v94` Header-Only RRG Preload & Block-Hint `tileAddress`**: When `rrg.idmap.bin` cache is valid, `FidesRrgLoader.loadHeaderAndPcc` skips reading `740 MiB` of `tileId + intentCode`, and `FidesRrgIdMap` adds a `HINT_SHIFT = 11` block-hint table (`runHint`) for fast `tileAddress(id)` lookup during materialization.
   - Overlaps static RRG pre-allocation (`nativePreinitStatic`), `rrg.idmap.bin` preload, and Java static init inside `Design.readCheckpoint`; runs `writeCheckpoint`, 64-shard `F4BaselineDcpCheck`, and deferred `STATS_WL` concurrently at writeback.
3. **De-Jittered Micro-Tail Convergence (`v73`–`v94`)**: Tightened neighbor ripup caps (`nbrLim = 20u` at `over <= 2`, `36u` under `widenNbr`, `maxNbrNet = 64`), single-item unbounded escape guard (`maxUnbounded = 1`), and gentler `microMult (2× / 3×)`.

## Repository Layout

- `src/cpp/` — Native C++ router & multi-GPU CUDA kernels (`fides_cpu.cpp`, `fides_gpu.cu`, `fides_gpu.h`, `hybrid.cu`, `feature_kernel.cu`).
- `src/java/` — Modified RapidWright Java classes, JNI bindings (`FidesNativeCpu.java`), fast loader/idmap (`FidesRrgLoader.java`, `FidesRrgIdMap.java`), and parallel DCP verifier (`F4BaselineDcpCheck.java`).
- `scripts/v94/` (plus `scripts/v70/`, `v73/`, `v76/`, `v92/`, `v93/`) — Build, deployment, launch, and Stage-1/2/3 patch scripts.
- `scripts/all13/` — Case-generic multi-design preparation & benchmark runner ([run_all13.py](scripts/all13/run_all13.py), [deploy_all13.py](scripts/all13/deploy_all13.py)).
- `scripts/runtime/` — Runtime GPU orchestration (`worker.py`, `worker_v94.py`) and brokers.
- `results/v94/`, `results/v94_all13/` (and prior `v70*`, `v73*`, `v76*`) — Verified 13-case receipts, `THEIA_50per` telemetry, and cold double-check logs.
- `docs/` — PPT-style report ([v94_ppt_report.md](docs/v94_ppt_report.md)), `v94` benchmark sheet ([v94_all13_benchmark.md](docs/v94_all13_benchmark.md)), `v95` optimization plan ([v95_optimization_plan.md](docs/v95_optimization_plan.md)), and prior milestone reports (`v76`, `v70`, `v22–v24`).
