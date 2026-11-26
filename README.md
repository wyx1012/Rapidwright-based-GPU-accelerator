# Rapidwright-based-GPU-accelerator

Multi-GPU (`4 × Tesla V100-PCIE-32GB`) + 36-core NUMA C++/Java hybrid router accelerator built on RapidWright for large-scale partial routing (`THEIA_50per`: `4,909,155` indirect connections, `43,564` direct connections, `92,497,168` RRG nodes, `391,524,527` edges).

## Milestone Snapshot (`v73` — Latest Best)

| Metric | `v73` (`4 × V100-PCIE-32GB`) | Baseline Comparison |
| :--- | :---: | :--- |
| **End-to-End (`E2E`)** | **`757.90 s` (`12m 37.90s`)** | **`-41.08 s`** vs `v70` (`798.97 s`); Beats PPT Slide 15 (`807.24 s`) by **`-49.34 s`** |
| **`route_wall`** | **`755.27 s`** | External orchestration tax `2.63 s` (`resident_preflight = 0.075 s`) |
| **`Route design`** | **`638.97 s`** (`Init 20.25 s + Routing 618.72 s`) | **`-45.65 s`** vs `v70` (`684.62 s`); **`-139.13 s`** vs `v62` (`778.10 s`) |
| **Convergence** | **`57` rounds, `over=0`** | `R1–52 = 539.59 s`, tail **`R53–57 = 0.88 s`** (vs `v70` `60.83 s`) |
| **Total Wirelength** | **`18,964,265`** | **`-36,961`** vs `v70` (`19,001,226`); `27,124,486,998` total pops (`18.89B` GPU + `8.24B` CPU) |
| **DCP Legality** | **Double-verified PASS** (`unroutedPins=0, sharedPipNodes=0, pips=31071296`) | Identical between in-process parallel write/check & cold `F4BaselineDcpCheck` |

### Previous Verified Milestone (`v70`)

| Metric | `v70` (`4 × V100-PCIE-32GB`) | Baseline Comparison |
| :--- | :---: | :--- |
| **End-to-End (`E2E`)** | `798.97 s` (`13m 18.97s`) | First run to beat PPT Slide 15 (`807.24 s`) by `-8.27 s` |
| **`Route design` / Convergence** | `684.62 s` (`64` rounds, `over=0`) | `R1–52 = 520.40 s`, tail `R53–64 = 60.83 s`, `WL = 19,001,226`, `pips=31075870` |

## Full 13-Case Competition Suite Verification (`v70`)

Using the case-generic runner ([scripts/all13/run_all13.py](scripts/all13/run_all13.py)), `v70` completed **all `13/13` competition benchmarks** cleanly with `over=0` and double-verified legality (`unroutedPins=0, sharedPipNodes=0` in both in-process parallel verification and cold `F4BaselineDcpCheck`):

- **Total E2E / Route Design**: **`2,798.95 s`** / **`2,248.16 s`** across all 13 cases (`mcml_RLE`, `RLE_BlobMerging`, `oc_des_des3perf`, `aquarius_b15_1`, `RS8FreqDecode`, `k_300dsp_300ram`, `k_100_dsp`, `k_140000fd_500000lut`, `THEIA_jpeg`, `THEIA`, `k_800_ram`, `THEIA_20per`, `THEIA_50per`).
- **Key Outlier Found**: `THEIA_20per` (`1,162.83 s`, `74` rounds) exceeded `THEIA_50per` (`800.27 s`, `63` rounds) solely due to `762.90 s` spent in `R54–74` (`widenNbr` 128-net neighbor ripup oscillation after reaching `over=5` at `R53` in `273.99 s`), confirming micro-tail de-jittering as a high-leverage optimization for `v73+`.
- Full table & per-case metrics: [docs/v70_all13_benchmark.md](docs/v70_all13_benchmark.md) and [results/v70_all13/summary.json](results/v70_all13/summary.json).

## Key Engineering Highlights (`v22 -> v73`)

1. **In-Process Dual-Engine C++/CUDA Core (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.cu`)**: Zero-IPC JNI bridge + multi-GPU `Tier-A` warp wavefront (`R1–R2`) and `Tier-B1` CTA wavefront (`320` CTAs across `4 × V100`) with LPT net-sharded dispatch, calibrated `Wide >= 16` / `Pass 3 >= 512` thresholds, and 36-core lock-free CPU fallback.
2. **End-to-End Fixed-Stage Pipelining (`scripts/v73/v73_patch.py`, `scripts/v70/v66_patch.py`, `scripts/v70/v67_patch.py`)**:
   - Overlaps static RRG pre-allocation (`nativePreinitStatic`), `rrg.idmap.bin` preload, and Java static init (`10.52–12.10 s`) inside `Design.readCheckpoint`.
   - 36-core parallel `PartialRouter.preprocess` + deterministic `getUnroutedPins` + background async `F2Prewarm` (relaxed in `v73` for native 13-case compatibility).
   - Overlaps `writeCheckpoint` (`20.57 s`) sequentially in a single `<= 34`-worker `ForkJoinPool` with 64-shard PIP exclusivity verification (`check_s = 3.01 s`) and deferred `STATS_WL` (`stats_s = 9.11 s`), eliminating both cold JVM DCP re-read and 72-thread CPU contention while skipping redundant `checkPIPsUsage` / `getAllTiles` scans.
3. **De-Jittered Micro-Tail Convergence (`v73`)**: Tightened neighbor ripup caps (`nbrLim = 20u` at `over <= 2`, `36u` under `widenNbr`, `maxNbrNet = 64`), single-item unbounded escape guard (`maxUnbounded = 1`), and gentler `microMult (2× / 3×)` reduce `THEIA_50per` tail (`R53–57`) to **`0.88 s`** (`57` rounds total) while targeting `THEIA_20per` `over=5..16` tail oscillation.

## Repository Layout

- `src/cpp/` — Native C++ router & multi-GPU CUDA kernels (`fides_cpu.cpp`, `fides_gpu.cu`, `fides_gpu.h`, `hybrid.cu`, `feature_kernel.cu`).
- `src/java/` — Modified RapidWright Java classes, JNI bindings (`FidesNativeCpu.java`), and parallel DCP verifier (`F4BaselineDcpCheck.java`).
- `scripts/v73/` & `scripts/v70/` — Build, deployment, launch, round monitor, and Stage-1/2/3 patch scripts (`v73`, `v70`).
- `scripts/all13/` — Case-generic multi-design preparation & benchmark runner ([run_all13.py](scripts/all13/run_all13.py), [deploy_all13.py](scripts/all13/deploy_all13.py)).
- `scripts/runtime/` — Runtime GPU orchestration (`worker.py`), brokers, and patch ancestry.
- `results/v73/` & `results/v70/` — Verified receipts, metrics (`result.json`, `status.json`), `native_multichannel.log`, and cold double-check logs (`cold_dcp_doublecheck.log`).
- `results/v70_all13/` — Aggregated `v70` 13-case benchmark receipts and legality checks ([summary.json](results/v70_all13/summary.json)).
- `docs/` — Full evolution log ([FIDES_v22_v24_experiments.md](docs/FIDES_v22_v24_experiments.md)), 13-case benchmark report ([v70_all13_benchmark.md](docs/v70_all13_benchmark.md)), and next-stage roadmap toward `517.28 s` ([NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md)).

## Next-Stage Roadmap

See [docs/NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md) for detailed `v71+` targets (sparse working-table & edge SoA layout, `R3–5` conflict-free GPU offload, adaptive multi-GPU lane scheduling, and deeper pre/post-route pipelining).
