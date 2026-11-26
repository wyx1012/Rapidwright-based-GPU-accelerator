# Rapidwright-based-GPU-accelerator

Multi-GPU (`4 × Tesla V100-PCIE-32GB`) + 36-core NUMA C++/Java hybrid router accelerator built on RapidWright for large-scale partial routing (`THEIA_50per`: `4,909,155` indirect connections, `43,564` direct connections, `92,497,168` RRG nodes, `391,524,527` edges).

## Milestone Snapshot (`v70`)

| Metric | `v70` (`4 × V100-PCIE-32GB`) | Baseline Comparison |
| :--- | :---: | :--- |
| **End-to-End (`E2E`)** | **`798.97 s` (`13m 18.97s`)** | Beats PPT Slide 15 (`807.24 s`) by **`-8.27 s`**; **`-116.58 s`** vs `v66` (`915.55 s`) |
| **`route_wall`** | **`796.34 s`** | External orchestration tax `2.64 s` (`resident_preflight = 0.09 s`) |
| **`Route design`** | **`684.62 s`** (`Init 23.89 s + Routing 660.72 s`) | **`-93.48 s`** vs `v62` (`778.10 s`) |
| **Convergence** | **`64` rounds, `over=0`** | `R1–52 = 520.40 s`, tail **`R53–64 = 60.83 s`** |
| **Total Wirelength** | **`19,001,226`** | `27,022,101,815` total pops (`18.66B` GPU + `8.36B` CPU) |
| **DCP Legality** | **Double-verified PASS** (`unroutedPins=0, sharedPipNodes=0, pips=31075870`) | Identical between in-process parallel write/check & cold `F4BaselineDcpCheck` |

## Full 13-Case Competition Suite Verification (`v70`)

Using the case-generic runner ([scripts/all13/run_all13.py](scripts/all13/run_all13.py)), `v70` completed **all `13/13` competition benchmarks** cleanly with `over=0` and double-verified legality (`unroutedPins=0, sharedPipNodes=0` in both in-process parallel verification and cold `F4BaselineDcpCheck`):

- **Total E2E / Route Design**: **`2,798.95 s`** / **`2,248.16 s`** across all 13 cases (`mcml_RLE`, `RLE_BlobMerging`, `oc_des_des3perf`, `aquarius_b15_1`, `RS8FreqDecode`, `k_300dsp_300ram`, `k_100_dsp`, `k_140000fd_500000lut`, `THEIA_jpeg`, `THEIA`, `k_800_ram`, `THEIA_20per`, `THEIA_50per`).
- **Key Outlier Found**: `THEIA_20per` (`1,162.83 s`, `74` rounds) exceeded `THEIA_50per` (`800.27 s`, `63` rounds) solely due to `762.90 s` spent in `R54–74` (`widenNbr` 128-net neighbor ripup oscillation after reaching `over=5` at `R53` in `273.99 s`), confirming micro-tail de-jittering as a high-leverage optimization for `v73+`.
- Full table & per-case metrics: [docs/v70_all13_benchmark.md](docs/v70_all13_benchmark.md) and [results/v70_all13/summary.json](results/v70_all13/summary.json).

## Key Engineering Highlights (`v22 -> v70`)

1. **In-Process Dual-Engine C++/CUDA Core (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.cu`)**: Zero-IPC JNI bridge + multi-GPU `Tier-A` warp wavefront (`R1–R2`) and `Tier-B1` CTA wavefront (`320` CTAs across `4 × V100`) with LPT net-sharded dispatch, calibrated `Wide >= 16` / `Pass 3 >= 512` thresholds, and 36-core lock-free CPU fallback.
2. **End-to-End Fixed-Stage Pipelining (`scripts/v70/v66_patch.py`, `scripts/v70/v67_patch.py`)**:
   - Overlaps static RRG pre-allocation (`nativePreinitStatic`), `rrg.idmap.bin` preload, and Java static init (`10.52 s`) inside `Design.readCheckpoint`.
   - 36-core parallel `PartialRouter.preprocess` + deterministic `getUnroutedPins` + background async `F2Prewarm`.
   - Overlaps `writeCheckpoint` (`15.44 s`) with 35-core read-only 64-shard PIP exclusivity verification (`check_s = 2.65 s`), eliminating the `~82 s` cold JVM DCP re-read.
3. **Clean Micro-Tail Convergence**: Tightened single-point unbounded escape gate (`over <= 1 && activeCount <= 2`) + `microStuck` history escalation (`6× / 12×`) without `192`-net neighbor ripup shocks.

## Repository Layout

- `src/cpp/` — Native C++ router & multi-GPU CUDA kernels (`fides_cpu.cpp`, `fides_gpu.cu`, `fides_gpu.h`, `hybrid.cu`, `feature_kernel.cu`).
- `src/java/` — Modified RapidWright Java classes, JNI bindings (`FidesNativeCpu.java`), and parallel DCP verifier (`F4BaselineDcpCheck.java`).
- `scripts/v70/` — `v70` build, deployment, launch, round monitor, and Stage-1/2 patch scripts.
- `scripts/all13/` — Case-generic multi-design preparation & benchmark runner ([run_all13.py](scripts/all13/run_all13.py), [deploy_all13.py](scripts/all13/deploy_all13.py)).
- `scripts/runtime/` — Runtime GPU orchestration (`worker.py`), brokers, and patch ancestry.
- `results/v70/` — Verified `v70` receipts, metrics (`result.json`, `status.json`), `native_multichannel.log`, and cold double-check log (`cold_dcp_doublecheck.log`).
- `results/v70_all13/` — Aggregated `v70` 13-case benchmark receipts and legality checks ([summary.json](results/v70_all13/summary.json)).
- `docs/` — Full evolution log ([FIDES_v22_v24_experiments.md](docs/FIDES_v22_v24_experiments.md)), 13-case benchmark report ([v70_all13_benchmark.md](docs/v70_all13_benchmark.md)), and next-stage roadmap toward `517.28 s` ([NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md)).

## Next-Stage Roadmap

See [docs/NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md) for detailed `v71+` targets (sparse working-table & edge SoA layout, `R3–5` conflict-free GPU offload, adaptive multi-GPU lane scheduling, and deeper pre/post-route pipelining).
