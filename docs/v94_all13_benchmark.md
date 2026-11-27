# v94 Full 13-Case Benchmark Report (`2 × V100`)

> **Build & Runner**: `theia50per-ppt-hybrid-v94` ([prepare_v94.py](../scripts/v94/prepare_v94.py), [v94_patch.py](../scripts/v94/v94_patch.py), [run_all13.py](../scripts/all13/run_all13.py))  
> **Machine-Readable Results**: [v94_all13/summary.jsonl](../results/v94_all13/summary.jsonl) & [v94/result.json](../results/v94/result.json)  
> **Hardware Platform (`gpu02`)**: `2 × Intel Xeon Gold 5220` (`36` physical cores / `36` threads, `2` NUMA nodes, `376 GiB` RAM), **`2 × NVIDIA Tesla V100-PCIE-32GB` (`--gpus 2,3`, NUMA Node 1)** (`0, 1` occupied by co-resident workload). Target FPGA: `xcvu13p-fhgb2104-2-e`, `lutPinSwapping=false`, `--nonTimingDriven`.

---

## 1. Key Changes in `v94` (`v76 → v94`)

1. **`hasPrePicked` Wave-Cap Fix (`src/cpp/fides_cpu.cpp`)**: Extracted `drainReadyToCpuFallback(bool includePicked)` and moved the `passTier == 1 && routeIter >= 3` mid-wave spill (`maxMidWaves`, `minMidItems`) and Pass 3 tail spill (`picked < 64 && rem + picked < 96`) outside `if (!hasPrePicked)`. Guarded `prePickThread` launch so the last wave no longer prepares an unused next batch. On `THEIA_50per`, `R3 + R4` dropped from `120.32 s` (`v93`) to **`68.99 s`** (`144` + `66` waves).
2. **Pass 3 `2.8M`-Pop Overlap (`src/cpp/fides_cpu.cpp`)**: Lowered `ultraPopCut` for `< 4` GPU runs from `6.5M` to `2.8M` pops so heavy Pass 3 items execute on 36 CPU threads overlapped with GPU `Tier-B1`.
3. **Header-Only Preload + `tileAddress` Block Hint (`src/java/FidesRrgLoader.java`, `src/java/FidesRrgIdMap.java`, `src/java/FidesLaneGate.java`)**: Added `FidesRrgLoader.loadHeaderAndPcc` (skips reading `740 MiB` of `tileId + intentCode` when `rrg.idmap.bin` cache is valid) and `HINT_SHIFT = 11` `runHint` lookup table to speed up `tileAddress(id)`.

---

## 2. Full 13-Case Results (`13 / 13` Double-Verified PASS on `2 × V100`)

All 13 designs ran on `--gpus 2,3` (`2 × V100`) and passed **both** in-process 64-shard verification and standalone cold-JVM `F4BaselineDcpCheck` (`unroutedPins=0, sharedPipNodes=0`, matching PIP counts).

| Case | GPUs | `v94 E2E (s)` | `v76 E2E (s)` | `Java E2E (s)`* | `v94 rdes (s)` | `v76 rdes (s)` | `Java rdes (s)` | `Rounds` | `v94 WL` | GPU `commits / waves / time (s)` | Verified `PIPs` | Cold Check (`s`) |
| :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: |
| `mcml_RLE` | `2×V100` | **21.92** | 28.77 | 13.77 | **12.40** | 19.78 | 10.47 | 45 | 140,458 | `0 / 0 / 0.00` | 149,437 | 4.75 |
| `RLE_BlobMerging` | `2×V100` | 28.22 | **24.69** | 11.85 | **9.64** | 15.00 | 8.55 | 9 | 70,238 | `0 / 0 / 0.00` | 135,356 | 4.81 |
| `oc_des_des3perf` | `2×V100` | **20.88** | 46.67 | 13.05 | **10.96** | 17.87 | 9.63 | 17 | 150,089 | `0 / 0 / 0.00` | 189,575 | 4.71 |
| `aquarius_b15_1` | `2×V100` | **25.94** | 47.64 | 14.34 | **10.17** | 18.72 | 10.83 | 22 | 105,668 | `0 / 0 / 0.00` | 210,944 | 4.79 |
| `RS8FreqDecode` | `2×V100` | **25.47** | 45.59 | 12.84 | **10.05** | 17.28 | 9.44 | 12 | 103,272 | `0 / 0 / 0.00` | 185,217 | 4.84 |
| `k_300dsp_300ram` | `2×V100` | **62.40** | 94.89 | 137.17 | **24.54** | 34.12 | 115.40 | 6 | 997,550 | `777,535 / 104 / 0.87` | 3,599,007 | 20.52 |
| `k_100_dsp` | `2×V100` | **56.32** | 69.15 | 153.04 | **26.91** | 34.26 | 135.98 | 13 | 1,410,652 | `775,581 / 107 / 1.11` | 3,645,331 | 18.07 |
| `k_140000fd_500000lut` | `2×V100` | **60.64** | 62.94 | 157.84 | **26.23** | 30.25 | 138.87 | 8 | 1,428,440 | `789,976 / 108 / 1.02` | 3,683,965 | 18.77 |
| `THEIA_jpeg` | `2×V100` | 135.68 | **129.72** | 1,561.64 | 97.90 | **93.55** | 1,548.01 | 41 | 3,110,070 | `912,245 / 281 / 18.60` | 5,008,227 | 22.99 |
| `THEIA` | `2×V100` | **142.46** | 162.15 | 3,855.60 | **109.82** | 121.41 | 3,841.98 | 30 | 5,137,873 | `1,507,571 / 437 / 32.61` | 7,837,732 | 25.61 |
| `k_800_ram` | `2×V100` | **163.13** | 297.83 | 621.18 | **46.36** | 54.88 | 528.35 | 14 | 3,090,945 | `2,494,867 / 332 / 4.43` | 10,491,185 | 39.34 |
| `THEIA_20per` | `2×V100` | **347.95** | 363.07 | 9,247.26 | **287.48** | 295.62 | 9,217.76 | 50 | 11,774,029 | `3,707,938 / 796 / 114.60` | 18,888,531 | 58.97 |
| `THEIA_50per` | `2×V100` | 723.85 | **719.80** (`4×`) | 29,717.56 | 625.88 | **601.24** (`4×`) | 29,665.52 | 56 | 19,067,589 | `7,466,274 / 2,172 / 228.31` | 31,080,646 | 92.54 |
| **Total (13 cases)** | **`2×V100`** | **1,814.86** | 2,092.91 | 45,517.14 | **1,298.34** | 1,353.98 | 45,240.79 | — | 46,586,873 | `18,431,983 / 4,337 / 401.56` | — | 320.71 |

*\*`Java E2E` / `Java rdes`: local Xeon Gold 5220 Java baseline (`CPU布线日志.txt`), `Read + Route + Write`, `lutPinSwapping=true` on the 9 non-`k` designs. In the server-side `gpu02` `lutPinSwapping=false` `-XX:ActiveProcessorCount=4` Java reference, **`Route design` alone** (not full E2E) took `68,237.38 s` on `THEIA_50per` (`v94 = 109.0×` faster on `Route design`) and `3,573–3,806 s` on `THEIA_jpeg`.*

---

## 3. Summary Comparison

- **`v94` (`2×V100`) vs `v76` (`4×V100` for `THEIA_50per`, `2×V100` for 12)**: **`-278.05 s`** total E2E (`2,092.91 s → 1,814.86 s`, **`1.15×`**), **`-55.64 s`** total `Route design` (`1,353.98 s → 1,298.34 s`).
- **`v94` (`2×V100`) vs `v70` (`4×V100`)**: **`-984.09 s`** total E2E (`2,798.95 s → 1,814.86 s`, **`1.54×`**), **`-949.82 s`** total `Route design` (`2,248.16 s → 1,298.34 s`, **`1.73×`**).
- **`v94` (`2×V100`) vs Local Java CPU (`CPU布线日志.txt`)**: **`25.08×`** total E2E speedup (`45,517.14 s → 1,814.86 s`), **`34.85×`** total `Route design` speedup (`45,240.79 s → 1,298.34 s`), **`41.05×`** E2E speedup on `THEIA_50per`.
