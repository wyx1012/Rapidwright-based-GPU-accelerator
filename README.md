# Rapidwright-based-GPU-accelerator

Multi-GPU (`4 × Tesla V100-PCIE-32GB`) + 36-core NUMA C++/Java hybrid router accelerator built on RapidWright for large-scale partial routing (`THEIA_50per`: `4,909,155` indirect connections, `43,564` direct connections, `92,497,168` RRG nodes, `391,524,527` edges).

## Milestone Snapshot (`v76` — Latest Best)

| Metric | `v76` (`4 × V100-PCIE-32GB`) | Baseline Comparison |
| :--- | :---: | :--- |
| **End-to-End (`E2E`)** | **`719.80 s` (`11m 59.80s`)** | **`-38.10 s`** vs `v73` (`757.90 s`); Beats PPT Slide 15 (`807.24 s`) by **`-87.44 s`** |
| **`route_wall`** | **`716.97 s`** | External orchestration tax `2.83 s` (`resident_preflight = 0.072 s`) |
| **`Route design`** | **`601.24 s`** (`Init 22.25 s + Routing 578.99 s`) | **`-37.73 s`** vs `v73` (`638.97 s`); **`-83.38 s`** vs `v70` (`684.62 s`) |
| **Convergence** | **`58` rounds, `over=0`** | **`sum_cpp = 499.61 s`** (`-40.86 s` vs `v73`); tail `R53–58 = 0.58 s` |
| **Total Wirelength** | **`18,974,092`** | `26,424,392,144` total pops (`18.48B` GPU + `7.94B` CPU, `-700M` vs `v73`) |
| **DCP Legality** | **Double-verified PASS** (`unroutedPins=0, sharedPipNodes=0, pips=31082870`) | Identical between in-process parallel write/check & cold `F4BaselineDcpCheck` |

### Previous Verified Milestones (`v73`, `v70`)

| Metric | `v73` (`4 × V100`) | `v70` (`4 × V100`) | Baseline Comparison |
| :--- | :---: | :---: | :--- |
| **End-to-End (`E2E`)** | `757.90 s` | `798.97 s` | `v70` first beat PPT Slide 15 (`807.24 s`) |
| **`Route design` / Convergence** | `638.97 s` (`57` rounds) | `684.62 s` (`64` rounds) | `v73` tail `R53–57 = 0.88 s`, `WL = 18,964,265`, `pips=31071296` |

## Full 13-Case Competition Suite & `v73` `THEIA_20per` Re-Verification

Using the case-generic runner ([scripts/all13/run_all13.py](scripts/all13/run_all13.py), now defaulted to `--engine v76` with `all13-rrg-cache` hardlink caching):

- **`v73` `THEIA_20per` Re-Test (`3.17×` E2E Speedup)**:
  - **`E2E = 366.99 s`** (`route_wall = 364.61 s`, **`Route design = 296.16 s`**) vs `v70` `1,162.83 s` (`Route design = 1,096.54 s`) — **`-795.84 s` faster**, resolving cleanly in **`53` rounds (`over=0`)** with **`WL = 11,661,635`** (`-15,693`) and double-verified legality (`unroutedPins=0, sharedPipNodes=0, pips=18882479`, recorded in [results/v73_all13/summary.jsonl](results/v73_all13/summary.jsonl)).
- **`v70` Baseline 13/13 Full Sweep** (all `13/13` passed cleanly with double-verified DCP legality):
  - **Total E2E / Route Design**: **`2,798.95 s`** / **`2,248.16 s`** across `mcml_RLE`, `RLE_BlobMerging`, `oc_des_des3perf`, `aquarius_b15_1`, `RS8FreqDecode`, `k_300dsp_300ram`, `k_100_dsp`, `k_140000fd_500000lut`, `THEIA_jpeg`, `THEIA`, `k_800_ram`, `THEIA_20per`, and `THEIA_50per` ([docs/v70_all13_benchmark.md](docs/v70_all13_benchmark.md), [results/v70_all13/summary.json](results/v70_all13/summary.json)).

## Key Engineering Highlights (`v22 -> v76`)

1. **In-Process Dual-Engine C++/CUDA Core (`src/cpp/fides_cpu.cpp`, `src/cpp/fides_gpu.cu`)**:
   - Zero-IPC JNI bridge + multi-GPU `Tier-A` warp wavefront (`R1–R2`) and `Tier-B1` CTA wavefront (`320` CTAs across `4 × V100`) with LPT net-sharded dispatch, calibrated `Wide >= 16` / `Pass 3 >= 512` thresholds, and 36-core lock-free CPU fallback.
   - **`v76` CPU A\* SoA & Early-Skip Engine**: Splits `NodeGeom` (`32B`) into `16B NodeGeomHot` (`4` nodes/64B cache line) + `8B NodeGeomCold` (`tileId`, `endSlr*` read only on `Local`/`cross-SLR`), packs `delay > 10000` into `PF_UTURN_DELAY`, checks thread-local `S.slotForRead(child)` before global `G.geomHot[child]`, eliminates `3–5` `divss` ops per shared edge, parallelizes `chunkNext` prefix compaction in `nativeUpdateHistAndFeedback`, and lowers `cpuBypassPass3Items` cutoff to `6.5M` pops to overlap overflowing heavy nets concurrently with GPU.
2. **End-to-End Fixed-Stage Pipelining (`scripts/v76/v76_patch.py`, `scripts/v73/v73_patch.py`, `scripts/v70/v67_patch.py`)**:
   - Overlaps static RRG pre-allocation (`nativePreinitStatic`), `rrg.idmap.bin` preload, and Java static init (`11.2–12.1 s`) inside `Design.readCheckpoint`.
   - 36-core parallel `PartialRouter.preprocess` + deterministic `getUnroutedPins` + background async `F2Prewarm` (native 13-case compatible).
   - **`v76` Contention-Free Post-Route**: Keeps only lightweight 64-shard PIP exclusivity verification inside `checkFuture` while running deferred `STATS_WL` cleanly right after `writeCheckpoint` (`5.90 s`), pre-sizes `F1Corridor` 64 shards (`1<<17`) and `assignNodesToConnections` node lists, and shrinks `f4NativeMaterializeAll` scratch `ArrayList` churn.
3. **De-Jittered Micro-Tail Convergence (`v73`–`v76`)**: Tightened neighbor ripup caps (`nbrLim = 20u` at `over <= 2`, `36u` under `widenNbr`, `maxNbrNet = 64`), single-item unbounded escape guard (`maxUnbounded = 1`), and gentler `microMult (2× / 3×)` reduce `THEIA_50per` tail (`R53+`) to **`0.58–0.88 s`** and accelerate `THEIA_20per` `3.17×` (`1,162.83 s -> 366.99 s`, `53` rounds).

## Repository Layout

- `src/cpp/` — Native C++ router & multi-GPU CUDA kernels (`fides_cpu.cpp`, `fides_gpu.cu`, `fides_gpu.h`, `hybrid.cu`, `feature_kernel.cu`).
- `src/java/` — Modified RapidWright Java classes, JNI bindings (`FidesNativeCpu.java`), and parallel DCP verifier (`F4BaselineDcpCheck.java`).
- `scripts/v76/`, `scripts/v73/`, & `scripts/v70/` — Build, deployment, launch, round monitor, and Stage-1/2/3 patch scripts (`v76`, `v73`, `v70`).
- `scripts/all13/` — Case-generic multi-design preparation & benchmark runner ([run_all13.py](scripts/all13/run_all13.py), [deploy_all13.py](scripts/all13/deploy_all13.py)).
- `scripts/runtime/` — Runtime GPU orchestration (`worker.py`), brokers, and patch ancestry.
- `results/v76/`, `results/v73/`, & `results/v70/` — Verified receipts, metrics (`result.json`, `status.json`), `native_multichannel.log`, and cold double-check logs (`cold_dcp_doublecheck.log`).
- `results/v70_all13/` — Aggregated `v70` 13-case benchmark receipts and legality checks ([summary.json](results/v70_all13/summary.json)).
- `docs/` — Full evolution log ([FIDES_v22_v24_experiments.md](docs/FIDES_v22_v24_experiments.md)), 13-case benchmark report ([v70_all13_benchmark.md](docs/v70_all13_benchmark.md)), and next-stage roadmap toward `517.28 s` ([NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md)).

## Next-Stage Roadmap

See [docs/NEXT_OPTIMIZATION_PLAN.md](docs/NEXT_OPTIMIZATION_PLAN.md) for detailed `v71+` targets (sparse working-table & edge SoA layout, `R3–5` conflict-free GPU offload, adaptive multi-GPU lane scheduling, and deeper pre/post-route pipelining).
