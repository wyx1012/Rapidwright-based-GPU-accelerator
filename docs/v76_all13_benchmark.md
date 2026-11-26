# v76 Full 13-Case Benchmark Report

> Benchmark driver: [run_all13.py](../scripts/all13/run_all13.py) & [deploy_all13.py](../scripts/all13/deploy_all13.py) (`--engine v76`)  
> Machine-readable results: [v76_all13/summary.jsonl](../results/v76_all13/summary.jsonl) & [v76/result.json](../results/v76/result.json)  
> Baseline comparison: [v70 Full 13-Case Report](v70_all13_benchmark.md) ([v70_all13/summary.json](../results/v70_all13/summary.json))  
> Platform: `gpu02` (`2 × Intel Xeon Gold 5220` 36 cores, `376 GiB` RAM, `4 × NVIDIA Tesla V100-PCIE-32GB`), target FPGA `xcvu13p-fhgb2104-2-e`.

---

## 1. Execution Configuration

1. **`THEIA_50per`** ran on all **`4 × V100`** (`numGpus=4`): `E2E = 719.80 s`, `Route design = 601.24 s`, `58` rounds (`over=0`).
2. **Remaining 12 cases** ran sequentially with strict GPU isolation (`--gpus 2,3`, using only the **`2 × idle V100`** on GPUs `[2, 3]` to avoid co-resident user workloads on GPUs `[0, 1]`), populating the persistent `all13-rrg-cache` for all `12` designs so subsequent runs (`v77+`) incur zero RRG export overhead.
3. Every single one of the `13 / 13` cases passed **both** the in-process 64-shard DCP exclusivity check (`unroutedPins=0, sharedPipNodes=0`) and standalone cold-JVM `F4BaselineDcpCheck` re-read with identical PIP counts.

---

## 2. Full 13-Case Results (`13 / 13` Double-Verified PASS)

Values in parentheses show the `v70` (`4 × V100`) baseline for direct comparison:

| Case | GPUs | Input (`MiB`) | Output (`MiB`) | Indirect Conns | Direct Conns | `E2E (s)` (`v70`) | `Route wall (s)` | `Route design (s)` (`v70`) | Rounds (`over=0`) | Total `WL` | Total `Nodes popped` | GPU `commits / waves / time (s)` | Verified `PIPs` | Cold Check (`s`) |
| :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: |
| `mcml_RLE` | `2×V100` | 2.11 | 3.70 | 20,259 | 271 | **28.77** (32.52) | 28.01 | **19.78** (19.82) | 45 | 152,900 | 25,872,605 | 33,019 / 14 / 0.37 | 153,701 | 4.83 |
| `RLE_BlobMerging` | `2×V100` | 2.15 | 3.63 | 20,261 | 271 | **24.69** (26.56) | 23.41 | 15.00 (13.65) | 9 | 79,660 | 11,218,394 | 32,356 / 11 / 0.08 | 139,680 | 4.73 |
| `oc_des_des3perf` | `2×V100` | 2.46 | 4.25 | 25,972 | 0 | 46.67 (27.60) | 34.47 | 17.87 (14.22) | 16 | 149,084 | 19,484,007 | 43,116 / 113 / 0.48 | 188,174 | 4.69 |
| `aquarius_b15_1` | `2×V100` | 2.62 | 4.27 | 29,451 | 73 | 47.64 (30.11) | 35.21 | 18.72 (17.98) | 26 | 115,143 | 29,180,547 | 47,705 / 16 / 0.12 | 215,940 | 4.78 |
| `RS8FreqDecode` | `2×V100` | 2.74 | 4.12 | 31,295 | 0 | 45.59 (28.11) | 33.32 | 17.28 (14.75) | 13 | 110,224 | 18,831,268 | 50,553 / 44 / 0.18 | 188,031 | 4.79 |
| `k_300dsp_300ram` | `2×V100` | 47.30 | 53.04 | 676,544 | 0 | 94.89 (70.69) | 82.09 | 34.12 (31.46) | 7 | 997,924 | 110,756,751 | 777,324 / 104 / 0.93 | 3,600,718 | 21.19 |
| `k_100_dsp` | `2×V100` | 55.67 | 48.46 | 676,196 | 1 | **69.15** (69.61) | 67.26 | **34.26** (37.17) | 10 | 1,411,521 | 150,250,729 | 775,798 / 107 / 1.07 | 3,645,515 | 19.88 |
| `k_140000fd_500000lut` | `2×V100` | 55.83 | 47.40 | 680,593 | 1 | **62.94** (69.95) | 61.07 | **30.25** (32.87) | 9 | 1,429,647 | 135,823,833 | 790,363 / 110 / 1.09 | 3,684,717 | 19.42 |
| `THEIA_jpeg` | `2×V100` | 63.33 | 75.66 | 639,637 | 16,576 | **129.72** (133.34) | 128.04 | **93.55** (98.79) | 28 | 3,091,266 | 2,877,239,233 | 840,820 / 288 / 19.00 | 5,005,128 | 23.23 |
| `THEIA` | `2×V100` | 74.00 | 96.11 | 1,033,193 | 7,568 | **162.15** (162.74) | 160.37 | **121.41** (125.82) | 31 | 5,118,561 | 5,201,797,819 | 1,383,815 / 370 / 29.17 | 7,839,187 | 28.02 |
| `k_800_ram` | `2×V100` | 106.84 | 124.50 | 2,072,669 | 0 | 297.83 (184.60) | 295.65 | **54.88** (60.50) | 12 | 3,070,286 | 532,737,204 | 2,434,832 / 317 / 3.74 | 10,463,348 | 41.88 |
| `THEIA_20per` | `2×V100` | 131.86 | 226.03 | 2,458,304 | 32,960 | **363.07** (1,162.83) | 360.96 | **295.62** (1,096.54) | 49 | 11,620,882 | 16,494,099,836 | 3,312,913 / 712 / 92.20 | 18,851,049 | 54.49 |
| `THEIA_50per` | `4×V100` | 275.88 | 349.01 | 4,909,155 | 43,564 | **719.80** (800.27) | 716.97 | **601.24** (684.59) | 58 | 18,974,092 | 26,424,392,144 | 6,911,020 / 1,791 / 175.72 | 31,082,870 | 77.95* |
| **Total (13 cases)** | — | — | — | — | — | **2,092.91** (2,798.95) | — | **1,353.98** (2,248.16) | — | — | — | — | — | — |

*\*`THEIA_50per` cold check read time (`F4_V37_DCP_CHECK_TIMING read=77.95s pins=2.92s pip_shards=2.42s unrouted_scan=0.50s`).*

---

## 3. Key Analytical Takeaways

1. **Suite-Wide Routing & E2E Speedup Even on `2 × V100`**:
   - Across all `13` designs, cumulative **`Route design`** dropped from **`2,248.16 s`** (`v70`, `4×V100`) to **`1,353.98 s`** (**`-894.18 s` / `1.66×` speedup**), and cumulative **`E2E`** dropped from **`2,798.95 s`** to **`2,092.91 s`** (**`-706.04 s` / `1.34×`**), despite `12/13` cases running on half the GPUs (`2×V100`).
   - On **`THEIA_20per`**, `v76` on **`2 × V100`** (`363.07 s`, `49` rounds, `WL = 11,620,882`) outperformed both `v70` (`1,162.83 s`, **`3.20×` speedup**) and `v73` on `4 × V100` (`366.99 s`, `53` rounds, `WL = 11,661,635`).
2. **Newly Surfaced Fixed-Cost Targets (`v77+`)**:
   - **Tiny designs (`oc_des_des3perf`, `aquarius_b15_1`, `RS8FreqDecode`, `mcml_RLE`)**: `Read DCP` takes only `3.5–4.1 s` and `Route design` takes `15–19 s`, while `F4_V67_PRELOAD` (`12.2 s` loading all `87M` RRG nodes) plus `resident_preflight` (`~11.5 s`) add `~24 s` of unhidden fixed startup overhead.
   - **`k_800_ram`**: `Route design` improved from `60.50 s -> 54.88 s` (`-5.62 s`), while `Read Netlist & PhysDB` (`207.77 s`) accounted for `+113 s` of E2E variance under 18-core NUMA affinity (`--gpus 2,3`) and concurrent host activity (`load average = 12.38`).
