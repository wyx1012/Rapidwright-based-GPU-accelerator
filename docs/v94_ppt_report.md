# FIDES 异构加速 FPGA 详细布线器项目报告（`v94` 阶段成果，仿答辩 PPT 结构）

> **当前版本**：`theia50per-ppt-hybrid-v94`（全部 `13 / 13` 竞赛用例在 **`2 × Tesla V100`** 上双重校验通过）  
> **端到端总成绩**：`E2E = 1,814.86 s`（`30.25 min`），`Route design = 1,298.34 s`，对比本机 13 用例 Java CPU 基线（`45,517.14 s`）实现 **`25.08×`** 端到端加速（`Route design` 加速 **`34.85×`**）；对比 `v76`（含 `4 × V100` 跑 `THEIA_50per`）再降 **`-278.05 s`**。

---

## Slide 1–2：研究背景与赛题目标

### 1.1 问题定义（对应原 PPT P3–P5）
- **对象与架构**：基于 AMD/Xilinx `RapidWright` 生态与 `xcvu13p-fhgb2104-2-e` 超大规模 UltraScale+ 器件（`92,497,168` RRG 节点、`391,524,527` 条有向边），面向 `13` 个完整局部已布线（partial-routed）竞赛设计完成详细布线。
- **算法骨架**：采用经典 **PathFinder 拥塞协商框架**（外层多轮次拥塞迭代更新历史/当前代价；内层对每条间接连接在动态演化的 RRG 上执行带包围盒与跨 SLR 约束的 A\* / Dijkstra 最短路径搜索）。
- **硬合法性红线**：输出完整 `.dcp` 必须同时通过**进程内 64 分片排他性校验**与**独立冷启动 JVM `F4BaselineDcpCheck` 回读校验**，严格满足 **`unroutedPins = 0` 且 `sharedPipNodes = 0`**，且两侧 verified `PIPs` 数量逐条一致。

### 1.2 优化目标（对应原 PPT P6）
- **运行时间优先（Runtime-First）**：竞赛评估体系中运行时间占核心主导地位（原 PPT 目标函数为 **80% Runtime + 20% Wirelength**），重点压缩全 13 用例总 **End-to-End（`E2E`）运行时间**（含 `Read DCP`、预加载/前处理、`Route design`、物化写回 `Write EDIF/XDEF/XDC` 与在线合法性核验）。

---

## Slide 3：实验硬件条件与平台差异声明（实事求是对照）

> **重要差异说明**：本阶段 `v94` 实测受限于同机 GPU `0, 1` 被其他用户研究任务长期占用，**全 13 个用例（含最大设计 `THEIA_50per`）均在严格隔离的 `2 × Tesla V100` 上完成**，与原 PPT 实验硬件及本机/服务器基线存在明确差异，如实列出如下：

| 实验平台维度 | 原答辩 PPT（P14 / P18） | 当前 `v94` 实测服务器（`gpu02`） | 本机 13 用例 Java CPU 基线（`CPU布线日志.txt`） | 服务器原生 Java 参考（`gpu02` 严格同参数） |
| :--- | :--- | :--- | :--- | :--- |
| **CPU 型号与核数** | `2 × Xeon Silver 4314` / `4 × Xeon Gold 6140`（扩展性测试达 `54` / `72` 线程） | **`2 × Intel Xeon Gold 5220 @ 2.20 GHz`**（36 物理核 / 36 线程，双路 NUMA `[0-17] / [18-35]`，无超线程） | **`Intel Xeon Gold 5220 CPU @ 2.20 GHz`** | **`2 × Intel Xeon Gold 5220`**（启动参数 `-XX:ActiveProcessorCount=4`） |
| **主机内存 / 系统** | 多路 Xeon 服务器 | **`376 GiB` DDR4**，Ubuntu `22.04.5 LTS`，`-Xmx256G` | 本地运行日志（`Read/Route/Write` 分项完整记录） | `376 GiB` DDR4，`-Xmx256G` |
| **GPU 配置与隔离** | `Tesla V100 PCIe`（`16 GiB` 与 `32 GiB` 配置，PPT 多卡） | **仅 `2 × Tesla V100-PCIE-32GB`（GPU `2, 3`，NUMA Node 1 亲和）**；GPU `0, 1` 未参与 | 无 GPU（纯 CPU Java `PartialRouter`） | 无 GPU（纯 CPU Java `PartialRouter`） |
| **CUDA / 编译环境** | CUDA Toolkit（支持 V100） | Host Driver `580.95.05`（CUDA `13.0` 运行时 + **`/usr/local/cuda-12.3/bin/nvcc -arch=sm_70`**、`g++ -O3 -march=native`） | — | Amazon Corretto JDK `11.0.31` |
| **布线参数口径** | `--nonTimingDriven`（PPT Slide 15: `T54 = 807.24 s` on `THEIA_50per`；Slide 18: 13 用例 `T54 = 1,657.64 s`） | `--nonTimingDriven`，`lutPinSwapping=false`（13/13 全部统一口径） | `--nonTimingDriven`；其中 9 个非 `k_*` 用例为 `lutPinSwapping=true`，4 个 `k_*` 用例为 `false` | `--nonTimingDriven`，`lutPinSwapping=false`；**仅统计 `Route design` 阶段，不含完整 E2E** |

---

## Slide 4：原基线瓶颈剖析（Why Optimize）

1. **串行 Java PathFinder 搜索耗时巨大**：在原生 Java `PartialRouter`（`lutPinSwapping=false`）下，`THEIA_50per` 仅 `Route design` 即耗时 **`68,237.38 s`（`18.96 h`）**；即便在开启 `lutPinSwapping=true` 的本地日志 [CPU布线日志.txt](file:///E:/EDA赛道_运行时间优先的GPU加速FPGA布线_复旦大学_王永骁/CPU布线日志.txt) 中，13 用例 `Route design` 仍达 **`45,240.79 s`**（`Read + Route + Write = 45,517.14 s`）。
2. **中大型设计（`THEIA*` / `k_800_ram`）瓶颈迁移**：在进入纯 C++/CUDA 混合路由后（`v22 → v76`），传统“Java/GPU IPC 序列化”、“全图 CSR 重复构建”和“优先队列随机 DRAM 访存”逐步转移为三类新瓶颈：
   - **中前期拥塞波前长尾**：`R3–R4` 在启用后台异步预选（`prePickThread`）时存在波数上限判定旁路（`v93` 中 `R3` 跑了 `873` 波、`R4` 跑了 `888` 波），导致 GPU 空转微小波次（均值仅数百条连接/波）。
   - **Pass 3 超重网拥塞拖尾**：百万级 pop 连接留在 GPU Tier-B1 上易触发 `overflow` 并在 GPU 栅栏后串行回退 CPU。
   - **中小用例固定 E2E 税**：`FidesRrgLoader.loadSlim` 即使在 `rrg.idmap.bin` 缓存命中时仍重复读盘 `740 MiB`（`tileId + intentCode`），且 `f4NativeMaterializeAll` 对近千万节点执行二分查找。

---

## Slide 5：核心架构与四大关键策略（How — 参照 PPT P8–P13 体系）

### 策略一：特征引导调度与几何走廊收紧（对应 PPT P9–P10）
- **后台异步 GPU 特征计算（`F2Prewarm`）**：在主路由前利用空闲 GPU 线程后台批量计算全部连接的几何与拓扑特征（覆盖 `4,952,719` 条连接），将 `F2` 主线程等待压至毫秒级并融入初始化阶段。
- **动态走廊与排序调度（`F1Corridor` + `HPWL_FIRST`）**：按 `HPWL asc → fanout desc → hashCode` 稳定排序连接顺序，短线与中长线通过 4×4 粗格走廊收紧搜索边界（`THEIA_50per` 注册 `1,440` 个跨 SLR portals），显著压低无谓节点弹出。

### 策略二：常驻全图双卡异构三级分层搜索（对应 PPT P8, P11, P12）
- **常驻显存零 IPC 架构**：在 `2 × Tesla V100` 上一次性驻留 `92.50M` 节点 / `391.52M` 边 CSR（双卡总 `taWarps = 2,048`、`tbCtas = 160`、每卡 `arena = 23.31 GiB`，初始化仅 `1.32 s`），经 JNI 直接由 C++ 控制，彻底消除外部进程/文件 IPC。
- **三级工作负载精准分流 + 异构双向重叠**：
  1. **Tier-A（Warp-per-Connection）**：`2,048` 个 32 线程 Warp 并发搜索 `R1–R2` 及 `Pass 1` 常规连接（`256` 槽位闭散列表，容量 `262,144`）。
  2. **Tier-B1（CTA-per-Connection）**：`160` 个 256 线程 CTA（共 `40,960` 线程）处理中重高扇出连接，配备 `8,388,608` 槽位大散列表。
  3. **36 核无锁 CPU `DynamicExecutor` 与 GPU 同轮并行（`cpuOverlapThread`）**：在 `Pass 3` 中，把高于 `ultraPopCut`（2 卡调优为 **`2.8M pops`**）的超重网直接在 CPU 36 核并发启动，与双卡 GPU `Tier-B1` 波前真正重叠执行。
- **`v94` 波次防空转与早退机制**：
  - 修复 `hasPrePicked` 旁路，将 `R3/R4` 中后期波次封顶（`maxMidWaves = 200 / 128`）与最小有效批量门限（`minMidItems`）独立到 `prePick` 之外，并阻止末尾波次继续发射冗余预选；在 `THEIA_50per` 上把 `R3+R4` 从 `120.32 s` 降至 **`68.99 s`**。
  - Pass 3 尾盘剩余 `< 64` picked 且总数 `< 96` 时直接排空至 CPU 并发完成，避免发射低利用率 CUDA 波前。

### 策略三：面向缓存行与 I/O 的数据结构工程（对应 PPT P11–P12）
- **紧致 SoA 图表示（`v76+`）**：将 `32B NodeGeom` 拆分为 `16B NodeGeomHot`（每条 64B Cache Line 装入 `4` 节点）与 `8B NodeGeomCold`；将 U-turn 延迟打包进位标志，并在扩展邻居时先查线程私有 `S.slotForRead(child)`，大量跳过全局热表随机访存，消除共享边 `3–5` 次浮点除法。
- **`v94` 缓存命中零冗余加载与 `O(1)` Tile 反查**：
  - 新增 `FidesRrgLoader.loadHeaderAndPcc`，当 `rrg.idmap.bin` 二进制缓存命中时跳过读取 `370 MiB tileId` 与 `370 MiB intentCode`（节省 `740 MiB` 磁盘 I/O 与解析），使小用例 `PRELOAD` 从 `18.60 s` 降至最低 `4.99 s`。
  - 在 `FidesRrgIdMap` 构建 `HINT_SHIFT = 11` 分块索引提示表（`runHint`），把 `tileAddress(id)` 从 `~20` 步全局二分缩减为局部小区间直查。

### 策略四：E2E 全流程流水线重叠与精确性兜底（对应 PPT P13）
- **前端重叠**：`Design.readCheckpoint` 期间后台同步执行 `nativePreinitStatic`（THP 透明大页并行首触分配）与 `rrg.idmap.bin` 预载；`PartialRouter.preprocess` 与 `getUnroutedPins` 36 核并行化。
- **后端重叠与双盲兜底**：写回阶段将 `writeCheckpoint`、`64` 分片并行 `F4BaselineDcpCheck`（`unroutedPins=0, sharedPipNodes=0`）与 `STATS_WL` 流水重叠，确保高并发绝不以牺牲 DCP 合法性为代价。

---

## Slide 6：全 13 用例实验结果与三方基线对比（`v94` `2 × V100`）

> **基线口径重要标注**：
> 1. **服务器原生 Java 参考（`gpu02`，`f4-theia50per-native-whole-v3` / `fresh13-2026-10-23`）**：采用严格同约束（`lutPinSwapping=false`, `-XX:ActiveProcessorCount=4`），**该基线时间仅统计 `Route design` 阶段，不包含完整的 E2E（读盘、预处理、写出与校验）**。
> 2. **本机 Java CPU 基线（[CPU布线日志.txt](file:///E:/EDA赛道_运行时间优先的GPU加速FPGA布线_复旦大学_王永骁/CPU布线日志.txt)）**：包含 `Read DCP + Route design + Write DCP` 完整三段，但其中 `9` 个非 `k_*` 设计默认启用 `lutPinSwapping=true`（因此线长总和更小）。

### 6.1 全 13 用例综合成绩汇总

| 版本 / 基线方案 | 硬件与加速卡条件 | 13 用例总 `Route design (s)` | 13 用例总 `E2E (s)` | 相对 `v94` 加速比 | 合法性状态 |
| :--- | :--- | ---: | ---: | :---: | :--- |
| **本机 Java CPU（全 13）** | Xeon Gold 5220（无 GPU，9 用例 `swap=true`） | `45,240.79 s` | `45,517.14 s`（`Read+Route+Write`） | **E2E `25.08×` / Route `34.85×`** | 13/13 `over=0` |
| **服务器 Java 参考（单列 `THEIA_50per`）** | `gpu02` 2×5220 (`4` 核 JVM, `swap=false`) | **`68,237.38 s`**（**仅 `Route design`，非 E2E**） | 未计全 E2E（写出另耗时 `58.05 s`） | **`THEIA_50per` Route `109.0×`** | `unrouted=0, shared=0` |
| **`v70` GPU-CPU 混合基线** | `gpu02` **`4 × Tesla V100`** | `2,248.16 s` | `2,798.95 s` | E2E `1.54×` / Route `1.73×` | 13/13 双重 PASS |
| **`v76` GPU-CPU 混合里程碑** | `gpu02`（`THEIA_50per` **`4 × V100`**，其余 12 用例 `2 × V100`） | `1,353.98 s` | `2,092.91 s` | E2E `1.15×` / Route `1.04×` | 13/13 双重 PASS |
| **`v94`（本报告主版本）** | `gpu02` **全套 13 用例均 `2 × Tesla V100`** | **`1,298.34 s`** | **`1,814.86 s`**（`30.25 min`） | **基准（`1.00×`）** | **13/13 双重 PASS** |

### 6.2 逐设计详细对比（`13 / 13` 全部 `unroutedPins=0, sharedPipNodes=0`）

| Case | 规模（间接连接） | `v94 E2E (s)` (`2×V100`) | `v76 E2E (s)` | 本机 Java `E2E (s)`* | `v94 rdes (s)` | `v76 rdes (s)` | 本机 Java `rdes (s)` | 服务器 Java `rdes (s)`** | `v94 WL` | `v94 PIPs` |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `mcml_RLE` | 20,259 | **21.92** | 28.77 | 13.77 | 12.40 | 19.78 | 10.47 | 17.41 | 140,458 | 149,437 |
| `RLE_BlobMerging` | 20,261 | 28.22 | **24.69** | 11.85 | **9.64** | 15.00 | 8.55 | 14.50 | 70,238 | 135,356 |
| `oc_des_des3perf` | 25,972 | **20.88** | 46.67 | 13.05 | 10.96 | 17.87 | 9.63 | 22.11 | 150,089 | 189,575 |
| `aquarius_b15_1` | 29,451 | **25.94** | 47.64 | 14.34 | **10.17** | 18.72 | 10.83 | 26.49 | 105,668 | 210,944 |
| `RS8FreqDecode` | 31,295 | **25.47** | 45.59 | 12.84 | 10.05 | 17.28 | 9.44 | 20.34 | 103,272 | 185,217 |
| `k_300dsp_300ram` | 676,544 | **62.40** | 94.89 | 137.17 | **24.54** | 34.12 | 115.40 | 137.05 | 997,550 | 3,599,007 |
| `k_100_dsp` | 676,196 | **56.32** | 69.15 | 153.04 | **26.91** | 34.26 | 135.98 | 178.30 | 1,410,652 | 3,645,331 |
| `k_140000fd_500000lut` | 680,593 | **60.64** | 62.94 | 157.84 | **26.23** | 30.25 | 138.87 | 174.24 | 1,428,440 | 3,683,965 |
| `THEIA_jpeg` | 639,637 | 135.68 | **129.72** | 1,561.64 | 97.90 | **93.55** | 1,548.01 | 3,704.00 | 3,110,070 | 5,008,227 |
| `THEIA` | 1,033,193 | **142.46** | 162.15 | 3,855.60 | **109.82** | 121.41 | 3,841.98 | — | 5,137,873 | 7,837,732 |
| `k_800_ram` | 2,072,669 | **163.13** | 297.83 | 621.18 | **46.36** | 54.88 | 528.35 | — | 3,090,945 | 10,491,185 |
| `THEIA_20per` | 2,458,304 | **347.95** | 363.07 | 9,247.26 | **287.48** | 295.62 | 9,217.76 | — | 11,774,029 | 18,888,531 |
| `THEIA_50per` | 4,909,155 | 723.85 (`2×`) | **719.80** (`4×`) | 29,717.56 | 625.88 (`2×`) | **601.24** (`4×`) | 29,665.52 | **68,237.38** | 19,067,589 | 31,080,646 |
| **Total (13)** | **13,303,529** | **1,814.86** | 2,092.91 | 45,517.14 | **1,298.34** | 1,353.98 | 45,240.79 | — | 46,586,873 | — |

*\*本机 Java `E2E` 为 [CPU布线日志.txt](file:///E:/EDA赛道_运行时间优先的GPU加速FPGA布线_复旦大学_王永骁/CPU布线日志.txt) 中 `Read DCP + Route design + Write DCP` 之和，且 9 个非 `k_*` 设计默认启用 `lutPinSwapping=true`（总 `WL = 32,195,708`）。*  
*\*\*服务器 Java `rdes` 来自 `gpu02` 同参数对照（`lutPinSwapping=false`, `-XX:ActiveProcessorCount=4`：`fresh13-2026-10-23` 中位数与 `f4-theia50per-native-whole-v3`），**仅含 `Route design` 阶段，不含读盘/预处理/写出等完整 E2E 时间**（后 4 个设计中前 3 个在 `fresh13` 尚未完成）。*

---

## Slide 7：`THEIA_50per` 运行时间分解与 `v94` vs PPT 并行度评估

### 7.1 `THEIA_50per`（`2 × V100` `v94`）端到端时间账本（`E2E = 723.85 s`）
- **外部启动与预检**：`~3.00 s`（`route_wall = 720.85 s`，`resident_preflight = 0.094 s`）。
- **读盘与缓存预载（并发隐藏）**：`Read DCP = 38.94 s`（内含并发 `IDMAP_CACHE = 4.80 s` 与 `PRELOAD = 5.13 s`，完全零暴露）。
- **Java 前处理与初始化**：`RWRoute.preprocess = 17.73 s`（`makePhys = 11.96 s` + `staticPins = 1.47 s` + `missingPins = 4.30 s`）+ `Initialization = 20.59 s`（含 `F2 = 3.49 s`、`SORT = 1.43 s`、`GPU ready = 1.32 s`、`native init = 4.55 s`）。
- **异构迭代路由（`Routing = 605.30 s`，`56` 轮收敛 `over=0`）**：
  - `R1–R2`（双卡 GPU 主导）：`39.29 s + 32.45 s = 71.74 s`（GPU 共 `1,874` 波）；
  - `R3–R4`（`v94` 波次防空转修复生效）：`41.81 s + 27.18 s = 68.99 s`（对比 `v93` `120.32 s` 直降 **`-51.33 s`**）；
  - `R5–R18`（Pass 3 GPU+CPU 重叠清拥塞）：双卡 GPU 贡献 `7,466,274` 次成功提交（`21.63B` GPU pops，`228.31 s`）；
  - `R51–R56`（微尾快速收口）：合计 **`3.43 s`**，末轮 `R56` 仅 `0.05 s`。
- **物化、后处理与写回校验**：`materialize = 13.29 s` + `POST = 8.93 s` + `SETPIPS = 5.86 s` + `Write & 并行 64 分片检验 overlap_wall = 18.86 s`（写出 `15.01 s` 吞并 `check = 1.98 s` 与 `STATS_WL = 3.79 s`）。

### 7.2 `v94` 相比原答辩 PPT 并行度是否更高？（明确结论与硬件差异对照）
**明确结论：`v94` 在计算架构、异构协同与全流程流水线上的实际并行度显著高于原答辩 PPT 的做法；与此同时，当前 `gpu02` 实测的物理硬件规模（仅 `2 × V100`、`36` 核 CPU）低于原 PPT 所用的多卡与 `54 / 72` 线程硬件规格。** 具体对照如下：
1. **GPU 连接内 + 跨连接总线程并发度更高**：原 PPT 围绕 `16 / 32` 路并行迷宫展开；`v94` 在双卡常驻全图上同时启用 **`2,048` 个 Tier-A Warp（`65,536` 并发线程）+ `160` 个 Tier-B1 CTA（`40,960` 并发线程，单 CTA `256` 线程维护 `8M` 槽位闭散列）**，若释放全部 4 张 V100 则可进一步翻倍至 `4,096 Warps + 320 CTAs`。
2. **CPU+GPU 真正异构双向并发（`cpuOverlapThread`）**：不同于“批边界上 GPU 与 CPU 串行切换”的模式，`v94` 在 `Pass 3` 让 CPU `36` 核与双卡 V100 **同轮并行**（CPU 并发吸收 `> 2.8M pops` 超重网，GPU 并发跑中等拥塞连接，并由后台 `prePickThread` 流水线式预选下一波）。
3. **E2E 全阶段并发流水线更深**：将 `IDMAP_CACHE` 与 `nativePreinitStatic` 藏进 `Design.readCheckpoint`，将 `F2Prewarm` 异步化，并将 `64` 分片并行 `F4BaselineDcpCheck` 合法性校验完全藏入 `writeCheckpoint` 写出周期内。
4. **物理硬件条件差异（如实声明）**：原 PPT 使用 `2× Xeon Silver 4314` / `4× Xeon Gold 6140`（扩展测试达 `54 / 72` 线程）及多卡 V100；而本次 `v94` 在 `gpu02` 上仅有 `2× Xeon Gold 5220`（`36` 核 / 无超线程），且 GPU `0, 1` 被其他用户占用，**全 13 用例严格隔离运行在 `2 × V100 [2, 3]` 上**。

---

## Slide 8：总结与下一阶段展望（`v95+`）

- **`v94` 已兑现里程碑**：在仅有 **`2 × Tesla V100`** 的实际隔离条件下，实现全 13 用例 **`1,814.86 s`（`30.25 min`）**、100% 双校验合法通过，对比本机 Java CPU 基线提升 **`25.08×`**，超越 `4 × V100` 参与的 `v76` 历史最优记录 **`-278.05 s`**。
- **`v95` 重点冲刺方向**（详见 [v95_optimization_plan.md](v95_optimization_plan.md)）：
  1. 并行化/分块 mmap 加载 `F4_V37_IDMAP_CACHE`（当前 `13` 用例累计消耗 `~132 s`），直接压低 `5` 个小用例与全套固定启动税；
  2. 多线程改造 `RWRoute.preprocess`（`THEIA_50per` 仍占 `17.73 s`）；
  3. 进一步压缩中大型设计物化/后处理（`28.08 s`）与 `R5–R18` CPU 尾盘，并在 GPU `0, 1` 空闲后复测 `4 × V100` 全卡表现，向 **`1,500 s` 以内**（对比 PPT Slide 18 `T54 = 1,657.64 s`）推进。
