# v70 Full 13-Case Benchmark Report

> Benchmark driver: [run_all13.py](../scripts/all13/run_all13.py) & [deploy_all13.py](../scripts/all13/deploy_all13.py)  
> Machine-readable results: [summary.json](../results/v70_all13/summary.json)  
> Platform: `gpu02` (`2 × Intel Xeon Gold 5220` 36 cores, `376 GiB` RAM, `4 × NVIDIA Tesla V100-PCIE-32GB`), target FPGA `xcvu13p-fhgb2104-2-e`.

---

## 1. Per-Case Adaptation Summary

Without modifying the `v70` core routing engine (`fides_cpu.cpp`, CUDA kernels, `F1Corridor`, `F4SpatialRouter`, or `FidesLaneGate` routing logic), `run_all13.py` parameterizes the harness and prepares each case cleanly:

1. **Per-design static RRG closure (`--phase prepare`)**:
   - Although all 13 competition designs target `xcvu13p-fhgb2104-2-e`, `rrg.bin` and `rrg.manifest.txt` capture only the design-reachable closure materialized by `RouteNodeGraph.initializeRouting()`. For example, reusing `THEIA_50per`'s manifest on `oc_des_des3perf` leaves `23,994` initial live nodes unmapped.
   - `RWRoute.java` adds a prepare-only guard (`-Dfides.rrgDump.exitAfter=true`) to exit cleanly right after `FidesRrgFlattener.flattenAndDump(...)`, followed by `FidesRrgIdMap` building the per-case binary `rrg.idmap.bin` cache.
2. **Unpinning `THEIA_50per`-specific harness checks**:
   - `F2Prewarm.java`: relaxes the `THEIA_50per` count assertion (`indirectCount != 4_909_155 || nAll != 4_952_719`) to `indirectCount < 0 || nAll <= 0` while preserving structural and bounding-box checks.
   - `worker.py`: reads `case.json` dynamically for input DCP path, `resource_input_bytes`, `live_rows = live_bytes // 72`, `direct_connections >= 0` (several cases have zero direct connections), and `<case>_gpu_full.dcp` output path.
   - `FidesLaneGate.java`: includes `firstUnmapped` node name in the unmapped-node exception message for diagnostics.

---

## 2. Full 13-Case Results (`13 / 13` Double-Verified PASS)

Every run executed sequentially on an idle `gpu02` (`~370 GiB` available host memory, 4 × V100 free) and passed **both** the in-process `F4BaselineDcpCheck` (overlapped with `writeCheckpoint`) and a standalone cold-JVM `F4BaselineDcpCheck` re-read (`unroutedPins=0, sharedPipNodes=0`, matching PIP counts verbatim).

| Case | Input (`MiB`) | Output (`MiB`) | Indirect Conns | Direct Conns | `E2E (s)` | `Route wall (s)` | `Route design (s)` | Rounds (`over=0`) | Total `WL` | Total `Nodes popped` | GPU `commits / waves / time (s)` | Verified `PIPs` | Cold Check (`s`) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: |
| `mcml_RLE` | 2.11 | 3.70 | 20,259 | 271 | 32.52 | 31.78 | 19.82 | 45 | 156,473 | 26,377,861 | 32,771 / 13 / 0.36 | 154,264 | 4.74 |
| `RLE_BlobMerging` | 2.15 | 3.63 | 20,261 | 271 | 26.56 | 25.94 | 13.65 | 12 | 80,508 | 11,709,844 | 32,339 / 10 / 0.07 | 140,201 | 4.65 |
| `oc_des_des3perf` | 2.46 | 4.24 | 25,972 | 0 | 27.60 | 26.92 | 14.22 | 15 | 147,874 | 19,270,370 | 43,370 / 113 / 0.47 | 186,850 | 4.77 |
| `aquarius_b15_1` | 2.62 | 4.27 | 29,451 | 73 | 30.11 | 29.44 | 17.98 | 20 | 115,577 | 31,018,426 | 48,354 / 14 / 0.10 | 216,021 | 4.72 |
| `RS8FreqDecode` | 2.74 | 4.12 | 31,295 | 0 | 28.11 | 27.38 | 14.75 | 12 | 109,219 | 19,150,044 | 50,579 / 43 / 0.16 | 187,826 | 4.88 |
| `k_300dsp_300ram` | 47.30 | 53.09 | 676,544 | 0 | 70.69 | 69.43 | 31.46 | 7 | 1,008,311 | 109,791,016 | 778,363 / 71 / 0.64 | 3,611,508 | 20.13 |
| `k_100_dsp` | 55.67 | 48.54 | 676,196 | 1 | 69.61 | 68.21 | 37.17 | 10 | 1,431,769 | 148,046,020 | 775,992 / 76 / 0.73 | 3,663,704 | 18.46 |
| `k_140000fd_500000lut` | 55.83 | 47.46 | 680,593 | 1 | 69.95 | 68.66 | 32.87 | 8 | 1,446,095 | 142,023,431 | 790,946 / 75 / 0.73 | 3,697,806 | 18.61 |
| `THEIA_jpeg` | 63.33 | 75.76 | 639,637 | 16,576 | 133.34 | 131.84 | 98.79 | 31 | 3,121,490 | 2,914,910,751 | 838,940 / 228 / 10.74 | 5,024,899 | 22.67 |
| `THEIA` | 74.00 | 96.12 | 1,033,193 | 7,568 | 162.74 | 161.11 | 125.82 | 31 | 5,112,646 | 5,179,681,972 | 1,382,009 / 387 / 25.21 | 7,842,988 | 28.07 |
| `k_800_ram` | 106.84 | 124.59 | 2,072,669 | 0 | 184.60 | 182.80 | 60.50 | 14 | 3,090,310 | 572,713,558 | 2,434,264 / 215 / 2.41 | 10,482,493 | 40.71 |
| `THEIA_20per` | 131.86 | 226.22 | 2,458,304 | 32,960 | **1,162.83** | 1,160.55 | **1,096.54** | **74** | 11,677,328 | 19,629,674,215 | 3,304,125 / 581 / 61.14 | 18,892,286 | 56.70 |
| `THEIA_50per` | 275.88 | 349.00 | 4,909,155 | 43,564 | **800.27** | 797.18 | **684.59** | **63** | 18,992,779 | 26,727,756,679 | 6,888,508 / 1,853 / 179.50 | 31,086,191 | 95.13 |
| **Total (13 cases)** | — | — | — | — | **2,798.95** | — | **2,248.16** | — | — | — | — | — | — |

---

## 3. Diagnostic Findings

1. **Suite-wide Generalization**: All 13 designs generalize cleanly with zero legality failures. `THEIA_50per` via the generic `run_all13.py` driver reproduces `800.27 s` (`Route design = 684.59 s`, `63` rounds, `WL = 18,992,779`), consistent with the standalone `v70` run (`798.97 s` / `684.62 s`).
2. **`THEIA_20per` Tail Oscillation (`1,162.83 s` > `THEIA_50per`)**:
   - `THEIA_20per` already reaches `over=5` at `R53` (`R1–53 cpp_s = 273.99 s`).
   - From `R54` onward, `v70`'s `widenNbr` gate (`iter >= 54 && over <= 12`, ripping up to `128` neighbor nets per round) triggers a `21`-round oscillation (`R54–74`, `routed=131..136` per round, `over` bouncing `5 → 8 → 12 → 14 → 9 → 16 → 9 → 5 → 8 → 2 → 8 → 2 → 0`), consuming **`762.90 s`** (`65.6%` of total E2E).
   - This reinforces the planned `v73` micro-tail de-jittering in [NEXT_OPTIMIZATION_PLAN.md](NEXT_OPTIMIZATION_PLAN.md) (`nbrLim` cap + softer `microMult`).
