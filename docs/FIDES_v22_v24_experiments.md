# FIDES 复现版本演进记录与备份清单（`v22` – `v38`）

> 更新时间：2026-11-24  
> 目标节点：`gpu02`（`2 × Intel Xeon Gold 5220`，共 36 核 / 2 NUMA 节点，376 GiB 内存，4 × Tesla V100-PCIE-32GB）  
> 目标测例：`THEIA_50per`（`THEIA_50per_partial_routed.dcp`，4,909,155 条间接连接，初始 RRG 节点 5,767,244）  
> 参考目录：`/home/yxwang1/FIDES/aquarius-evidence-2026-10-22/ppt-reproduction-2026-11-07/f4-ppt-runtime-measurement-v1/`

---

## 1. 版本目录与备份结构（每版独立目录 + SHA-256 固化清单）

所有版本均**不覆盖前版**，每版保留独立目录、`prepare_v*.py`、`v*_patch.py`、编译后源码与 `.class`、`hybrid` 二进制、`corridor_build_receipt.json`（全文件 SHA-256 指纹）及运行日志：

| 版本 | 服务器目录 | 状态 | 核心定位 |
| :--- | :--- | :--- | :--- |
| `v22` | `.../theia50per-ppt-hybrid-v22` | 已停止备份 | 原始基线对照版（Round 1 `5,440.54 s`，过载 `1,736,277`，Round 2 进行至 ~62.7% 后手动停止） |
| `v23` | `.../theia50per-ppt-hybrid-v23` | 已停止备份（验证完成） | **P0 验证版**：36 核 NUMA 解锁 + 并行 `F4RegionQueue` 初始化 + 扁平页表 `F4ScratchPages` + 投机区域调度 |
| `v24` | `.../theia50per-ppt-hybrid-v24` | 已停止备份（验证完成） | **P0 精调 + P1 版**：去掉 `-ea` + 投机冲突预过滤 + `8-lane` 常驻 GPU + `16k` `tier=1` 预算 + 多连接同 Net 合批 |
| `v25` | `.../theia50per-ppt-hybrid-v25` | 已停止备份（R3 中段停机复盘） | **C++ 扁平 CSR 单线程引擎**：`libfides_cpu.so` + 去掉 `f4LongEnvelope`（R1 `4,342.38 s`，R2 `5,265.56 s`，暴露 `lagunaDir` 33% 回退、`UPSTREAM` 锁死及 `shortBox` 拥塞问题） |
| `v26` | `.../theia50per-ppt-hybrid-v26` | 已完成归档（99 轮未全收敛） | **PPT 对齐重构版**：`HPWL_FIRST` + `shortBox ±4` + C++ 0% 回退 + 36 核 `thread_local` 并行 C++ + GPU `32k/65k/262k`（99 轮 `26,557.94 s`，较 `v3` 快 `2.57×`，R99 滞留 `1,041` 过载 / `70` PIP overuse） |
| `v27` | `.../theia50per-ppt-hybrid-v27` | 已停止归档（完成 R1+部分 R2） | **36 线程 C++ 无锁波前全轮版**：滑窗异 Net 批（`W=128`）+ 节点级精确 epoch 提交 + 轮内全 C++ 状态（R1 `1,242.53 s`、`par=99.99%`，暴露后段 Net 哈希线性退化与波次屏障长尾） |
| `v28` | `.../theia50per-ppt-hybrid-v28` | 已停止归档（完成 R1–R8） | **大网扁平哈希表 + 无波次屏障连续任务池**：R1 `171.56 s`（快于 PPT R1 `176.06 s`），验证 36 核满载，但 R4–R8 出现 `oldPath` 假冲突与 R4 退廊 pop 膨胀 |
| `v29` | `.../theia50per-ppt-hybrid-v29` | 已停止归档（完成 R1–R14） | **实时 `shouldRoute` + `newPath` 冲突判定 + 选择性走廊退场**：R1 `169.32 s`、`fb=0`，R14 压至 `over=1,739`，暴露 `Tier 3` 误杀同网共享与 `prioritySoloActIdx` 单核锁死 |
| `v30` | `.../theia50per-ppt-hybrid-v30` | **已完成并合法通过** | **跨网精确冲突分离 + 无全局抽空 + PPT P11 三级预算 + 逐连接渐进有界放宽**：**86 轮全收敛（`overused=0`）**，86 轮累计 `1,268.72 s`（`Route design 1,780.12 s`，E2E `2,885.00 s`），WL `17,815,441`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=30,542,009`） |
| `v31` | `.../theia50per-ppt-hybrid-v31` | 已完成归档（`dcp_legality` FAIL） | **4 阶紧扩框 + L1 `tinyOv[32]` + 零除法快速路 + Java 全并行后处理（实验首验证）**：R1–R12 C++ 压至 **`694.96 s`**（较 `v30` 降 `-337.73 s`）、`computesNodeUsageAndTotalWirelength` 由 `~850 s` 压至 **`8.66 s`**；暴露 `TileType` 懒加载 `HashMap` 并发竞态（19,874 `Null PIP` → `unroutedPins=3314`）及 R99 尾段 `over=40`（`sharedPipNodes=40`） |
| `v32` | `.../theia50per-ppt-hybrid-v32` | **已完成并合法通过** | **Dekker 无锁调度 + 边预取 + 长尾单调扩框修复 + `TileType` PIP 预热 + 并行物化分片统计**：**74 轮全收敛（`overused=0`）**，`Route design 1,359.05 s`，`route_wall 1,467.67 s`，**E2E `1,619.06 s`**（较 `v30` 缩短 **`43.9%`** / `-1,265.94 s`），WL `17,977,885`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=30,628,236`） |
| `v33` | `.../theia50per-ppt-hybrid-v33` | **已完成并合法通过** | **256-pause Dekker + 扩框重尾预分流 + `overused<=1800` 尾段切换 + 64 分片并行 `RouteNode` 实例化**：**68 轮全收敛（`overused=0`）**，物化 `seq_fix=0`（`15.22 s` vs `v32 70.67 s`），`Route design 1,173.14 s`，`route_wall 1,281.18 s`，**E2E `1,417.10 s`**（较 `v30` 缩短 **`50.9%` / `>2×`**，较 `v26` `Route design` 快 **`22.64×`**），WL `18,088,147`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=30,719,060`） |
| `v34` | `.../theia50per-ppt-hybrid-v34` | **已完成并合法通过** | **进程内单卡 V100 CUDA 双引擎（`libfides_cpu.so` + `fides_gpu.cu` 零 Java IPC）**：常驻 92.5M 节点 / 391.5M 边 + 16B `GpuNodeHot` 占显存 ~28 GiB；`Tier-A` Warp 波前（`R1–R2`，`HPWL_FIRST` 最小堆 + 虚拟 `oldPath` + 自适应尾段 `mpnFill`）+ `Tier-B1` CTA 波前（`Pass 2 > 8000` / `Pass 3 R<=5 && > 1000`）+ 35 核 `DynamicExecutor` 重尾回落；**88 轮全收敛（`overused=0`）**，`sum_cpp 892.24 s`（较 `v33 -64.98 s`），`Route design 1,122.36 s`，`route_wall 1,226.84 s`，**E2E `1,350.42 s`**（较 `v33 -66.69 s`），WL `18,690,728`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,000,382`） |
| `v35` | `.../theia50per-ppt-hybrid-v35` | **已完成并合法通过（Route A 首轮实验）** | **Pinned 零拷贝波前缓冲 + 35 核并行 Pack + 剔除冗余 `commitStampedPath` + 全轮 `Tier-B1/B2` 卸载实验**：**64 轮全收敛（`overused=0`）**，R1+R2 大幅提速（`184.20 s -> 156.10 s`, `-28.10 s`），Pass 3无溢出轮大幅缩短；但误将 R3–R5 Pass 1 留 GPU `Tier-A`（`+35.67 s` 及 R4/R5 `+25–34%` 过载级联）及重尾进串行 `Tier-B2`（`156.67 s`）导致总 `sum_cpp 927.47 s`、`Route design 1,146.31 s`、WL `19,408,174`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,336,564`） |
| `v36` | `.../theia50per-ppt-hybrid-v36` | **已完成并合法通过** | **Route A 次轮精准调优**：`Pass 1 Tier-A` 回归仅 `R1–R2`（`R2 tw=8192`）；退役低效串行 `Tier-B2` 释放 ~1.5 GiB 显存让 **`Tier-B1` 占满 80 SM**（`tbCtas=80, taWarps=1280`）+ `Pass 2 >= 256` / `Pass 3 >= 160` 全进 `Tier-B1`（溢出回落 35 核 CPU HOT）；`Phase 4` 按 Net 35 核并行 `addToScopeDeferred` + `MAX_MEGA_SCOPES=384` 异步上传；**62 轮全收敛（`overused=0`）**，**`sum_cpp 810.43 s`**（较 `v34 -81.81 s`），**`Route design 1,032.72 s`**（较 `v34 -89.64 s`），**`route_wall 1,131.66 s`**，**E2E `1,258.36 s`**（较 `v34 -92.06 s`），WL `19,015,283`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,159,275`） |
| `v37` | `.../theia50per-ppt-hybrid-v37` | **已完成并合法通过** | **Route B 框架瘦身 + 单卡 Tier-B1 Wide（`16M/32M`）**：`direct_finalize` 降至 **`0.20 s`**（较 `v36 80.47 s` 省 `-80.27 s`），`F4_V37_IDMAP_CACHE` **`0.930 s`**（`-14 s`），并行 `assignNodes` (`2.33 s`) + `fixRoutes` (`8.29 s`) + 跳过闭环扫描 + 64 分片并行 `F4BaselineDcpCheck`；后端阶段 `150.58 s -> 113.79 s`。全流程 **69 轮全收敛（`overused=0`）**，**E2E `1,243.55 s`**（较 `v36 -14.81 s`），`Route design 1,041.38 s`，WL `19,043,165`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,164,943`）；同时实证 `Tier-B1 Wide`+`P3>=16` 使 GPU 耗时升至 `514.79 s`（`+159.73 s`）且共享机外部任务造成 `R3–R5` CPU 退化 `+33.84 s` |
| `v37.1` | `.../theia50per-ppt-hybrid-v37_1` | **已完成并合法通过（单卡最快）** | **Route B 全留 + 回退 `Tier-B1 8M -> 35 核 CPU HOT` / `Pass 3 >= 160`**：保留 `v37` 全部 Route B 优化（`direct_finalize 0.18 s`、`idmap 0.864 s`、并行 `assignNodes 1.96 s` / `fixRoutes 12.15 s`、跳过闭环扫描、64 分片 `F4BaselineDcpCheck`）并消除 `Wide 16M/32M` 与 `P3>=16` GPU 退化；**62 轮全收敛（`overused=0`）**，GPU 耗时回落至 `358.79 s`（较 `v37 -156.00 s`），**`Route design 978.16 s`**（**单卡首次破千秒**，较 `v36 -54.56 s`），**`route_wall 1,088.88 s`**，**E2E `1,189.51 s`（`19m 50s`，较 `v36 -68.85 s`、首次破 20 分钟）**，WL `19,040,007`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,172,492`） |
| `v38` | `.../theia50per-ppt-hybrid-v38` | **已完成并合法通过（当前全局最快）** | **Route A+C（4×V100 320 SM 并行分片）+ `v37.1` Route B**：4 卡各自常驻图+动态槽（`taWarps=4,096`, `tbCtas=320`, `init_s=4.38 s`），按 Net LPT 负载均衡分片与全卡稀疏 Delta/MegaScope 同步，放开 `F2Prewarm` 单卡断言；**72 轮全收敛（`overused=0`）**，GPU 并行壁钟降至 **`231.44 s`**（较 `v37.1 -127.36 s`），`sum_cpp 790.05 s`，**`Route design 937.71 s`**（较 `v37.1 -40.45 s`、`v36 -95.01 s`），**`route_wall 1,037.61 s`**，**E2E `1,130.99 s`（`18m 51s`，较 `v37.1 -58.52 s`、`v34 -219.43 s`）**，WL `19,122,135`，`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,193,415`） |
| 参考基线 | `../f4-theia50per-native-whole-v3` | 已完成参考 | 原生全 CPU `PartialRouter` 基线（`UPSTREAM`，42 轮，461 亿 pops，总计 `68,237.38 s`，WL `17,294,240`，R1 `4,903.93 s`，过载 `964,289`） |

---

## 2. 各版本详细改动与实测对比

### 2.1 `theia50per-ppt-hybrid-v22`（原始问题基线）
- **保留原因**：作为问题诊断和性能对比基线；GPU 2 已释放，`/dev/shm/fides-f4-cuda-corridor-x0m6x7hv` 已清理。
- **关键实测问题**：
  - `-XX:ActiveProcessorCount=16` 且 `worker.py` 绑核到单 NUMA（18 核）。
  - `F4RegionQueue`：`F4_REGION_SHIFT = 3`、`F4_REGION_NEAR = 256`、`ready.size() < 12` 且无跨区投机，导致 Round 1 `81.6%`（`4,005,646 / 4,909,155`）、Round 2 `94.8%` 退化为单线程串行 `A*`。
  - 第 1 轮索引建图 `region_index_ms = 169,813 ms`（`169.8 s`）。
  - GPU `resident_lanes = 1` 且 `f4RouteFirstRound` 硬编码 `ArrayList<LaneBatchSlot>(1)`，Round 1 仅卸载 `10` 条连接，Round 2 前 62.7% 卸载 `0` 条；`tier = 1` 预算高达 `262,144` pops（仅 13 条触发）。
  - Round 1 耗时 **`5,440.54 s`**，过载节点 **`1,736,277`**（明显高于原生参考基线的 `964,289`）。

### 2.2 `theia50per-ppt-hybrid-v23`（P0 阶段验证，已备份）
- **改动文件**：`prepare_v23.py`、`v23_patch.py`（补丁作用于 `RWRoute.java`、`F1Corridor.java`、`FidesLaneGate.java`、`RouteNode.java`、`worker.py`、`v20_handoff.py`）。
- **核心改动**：
  1. `v20_handoff.py` 在 GPU 预热完成后恢复全机 CPU 亲和性 `[0–35]`，`worker.py` 将 `-XX:ActiveProcessorCount` 放开至 `36`。
  2. `F1Corridor.java` 增加 `ensurePortalsIndexed` 与无副作用的 `boxParallel` + `commitParallel`；`RWRoute.java` 的 `F4RegionQueue` 改用 `ForkJoinPool` 并行计算走廊框与几何特征。
  3. `RouteNode.java` 将 `F4ScratchPages` 的 `HashMap<Long, Page>` 改为开放寻址 `long[]` 页表，并用 `Page.readStamps` 替代 `IdentityHashMap` 维护 `readNodes`。
  4. `F4_REGION_SHIFT = 4`、`F4_REGION_NEAR = 16384`、`F4_REGION_PENDING = 128`、加入 `regionHeads` 跨区投机。
- **验证结果**（运行至 `t = 608,230 ms`，`pos = 322,624`，`pops = 388.9M` 后停止并备份）：
  - **验证成功**：`region_index_ms` 从 `169,813 ms` 降至 **`20,346 ms`（提速 8.3×）**。
  - **暴露问题**：① 前段大网导致无预过滤的 `regionHeads` 投机失效率高（`128 / 142` 被 `invalidate_after_ordered_write` 回滚）；② Java `-ea` 断言使单次扩展维持在 `~1,446–1,567 ns/pop`；③ 前 183 万条高扇出网（含 `fanout = 32,032`）同 Net 串联且包围盒重叠，仍处于单线程段。

### 2.3 `theia50per-ppt-hybrid-v24`（P0 精调 + P1 多 lane GPU，正在验证）
- **改动文件**：`prepare_v24.py`、`v24_patch.py`（补丁作用于 `hybrid.cu`、`RWRoute.java`、`F1Corridor.java`、`FidesLaneGate.java`、`RouteNode.java`、`F4RoundDiversion.java`、`worker.py`、`v20_handoff.py`、`v14_broker.py`）。
- **核心改动**：
  1. **去掉 `-ea` 断言**，保留 36 核全机 NUMA 解锁、`F4RegionQueue` 并行初始化与扁平页表。
  2. **投机预过滤**：`F4_REGION_NEAR = 65536`，并在 `f4RegionFill` 选取 `regionHeads` 前检查未来 16 条串行窗口冲突，大幅减少无效投机回滚。
  3. **8-lane 常驻 GPU**：`hybrid.cu` 读取 `FIDES_V23_RESIDENT_LANES`（在 `v24` 为 `8`）并重编译 `hybrid`；`v20_handoff.py` 启动即预分配 8 条通道（占用显存 `22.33 GiB`，剩余 `11.40 GiB`）。
  4. **同 Net 多连接合批与 16k 预算**：`F4RoundDiversion.BUDGETS` 调为 `{8_000, 16_384, 262_144}`（同步放行 `v14_broker.py` 校验）；`f4RouteFirstRound` 接入 `f4FlushFirstGpuBatch` 支持最多 8 条同 Net 重尾合批及按成员均摊 `gpuMean` 记账（`warmFloor = 4 ms`）。
- **最终验证观测**（`15:43` 停止前，`elapsed_ms = 1,794,819` / 约 `29.9 分钟`）：
  - `region_index_ms = 17,658 ms`（较 `v22` **9.6×** 加速）；
  - `STATE_MAP 8 1936086780` 确认 8 条 GPU 通道常驻成功；
  - 推进至 `pos = 1,044,096 / 4,909,155`（`13.38 亿` pops，单 pop 约 `~1,328 ns`），对比 `v22` 同位置（`pos = 1,079,296` / `13.98 亿` pops 耗时 `2,266 s`）领先约 **`471 s`**；
  - `parallel_commits = 906`、`invalidate_after_ordered_write = 231`、`gpu_trials_total = 0`（受限于 `pos < 1.83M` 高扇出大网单线程区间与 Java 对象图 `~1,300 ns/pop` 下限）。
  - **已停止并保留完整目录备份**（`gpu02` 进程已清理，`/dev/shm/fides-f4-cuda-corridor-nxuudt9f` 已删除），随即启动 **`v25`** 突破上述天花板。

### 2.4 `theia50per-ppt-hybrid-v25`（已停止备份，完成 R1/R2 实测与根因定位）
- **改动文件**：`prepare_v25.py`、`v25_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **实测结果**：
  - `FIDES_NATIVE_CPU ready=true N=92497168 M=391524527`，`V25_NATIVE_VERIFY_OK checked=20 calls=26`。
  - **Round 1**：`4,342.38 s`（`RRG Nodes = 59,555,660`，`overused = 1,735,397`），`FIDES_NATIVE_CPU calls=4,306,223 hits=2,838,974 fallbacks=1,431,571`（`33.2%` 回退），`GPU` 完成 9 个 `width=1` batch（`gain=+7.61 s -> ADMIT`）。前 183.8 万连接在 `UPSTREAM` 排序下 `99.9%` 串行，耗时 `3,007.7 s`（23.57 亿 pops）。
  - **Round 2**：`5,265.56 s`（`Routed = 3,911,019`，`overused = 1,249,802`），累计 `fallbacks = 2,759,363 / 7,819,181`（`35.3%`），仅 3 个 `width=1` batch（#10–#12），因 `81 ms/member × 2,026 = 164.26 s` 算出 `gain = -156.71 s` 并触发 `HOLD`。
  - **Round 3**：进行至 `pos = 1,832,768 / 4,909,155`（`elapsed_ms = 3,205,182`）后停止（`20:00`），GPU 2 与 `/dev/shm` 已释放清理。

### 2.5 `theia50per-ppt-hybrid-v26`（已完成 99 轮实测并归档）
- **改动文件**：`prepare_v26.py`、`v26_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **与 PPT 对齐的核心改动**：
  1. **Feature-Informed Dispatch 排序对齐**：`worker.py` 注入 `--sortOrder HPWL_FIRST`（打破 `UPSTREAM` 在前 183.8 万连接上的 `99.9%` 单线程锁死）。
  2. **`F1Corridor` 短连接边距修复**：保留去掉 `f4LongEnvelope`，同时将 `HPWL < 16` 的 `shortBox` 边距由 `1`（严格不等式下为 0 tile 余量）放宽至 `4`（允许 ±3 tile 绕行避障），避免 R1 过载翻倍。
  3. **消除 C++ `100%` 的 `r10` 中途回退**：`checkAccessAndLookahead` 将 `(cf & Leading)` 修正为 `lagunaDir(ct) != 0`（覆盖 `NON_LOCAL_LEADING_TO_*_LAGUNA` 类型 10/11）并改用 `sinkBeginX/sinkBeginY`。
  4. **C++ 侧增量 `netScopes` 与 `f4NodeCache`**：消除换 Net 时 Java 遍历 32k 连接重建 `f4FastMembersOf` 与 `idMap.nodeAt` 二分查表；命中 C++ 时跳过冗余 `prepareRouteConnection` 堆入队。
  5. **36 核 `thread_local` 并行 C++ 探测**：`f4FirstProbe` 迁移至 C++ `nativeProbeSearch`，`F4_REGION_PENDING = 256`、`36` 并行线程，协调器侧按 `footprint` + `NetWrapper` 判冲突。
  6. **GPU 准入阈值校准**：`F4RoundDiversion.BUDGETS` 调为 `{32_768, 65_536, 262_144}`、`warmFloor >= 15 ms`。
- **99 轮最终实测结果**（`2026-11-21T05:34` 收尾结束）：
  - **Round 1**：**`3,425.30 s`**（`overused = 845,907`，较 `v22/v25` 砍半且优于 `v3` 的 `964,289`），`FIDES_NATIVE_CPU calls=4,759,282 hits=4,759,282 fallbacks=0 parCalls=254,383 parHits=254,381 parFallbacks=0`，`spatial_commits = 149,873 (3.05%)`，`invalidate = 104,508 (41.1%)`。
  - **Round 2–5**：R2 `2,632.48 s`（`over=297,490`）、R3 `1,686.90 s`（`over=107,339`）、R4 `1,154.12 s`（`over=44,847`）、R5 `981.27 s`（`over=26,990`）。第 4 轮起启用 GPU，共完成 `15,364` 个 `width=1` batch（`gpu_rejected = 0`，R9–R99 多数轮 `ADMIT` 正收益）。
  - **全程汇总**：99 轮 `Route design = 26,557.94 s`（较原生 `v3` `68,237.38 s` 快 **`2.57×`**），`Nodes popped = 21,154,993,702`（较 `v3` `46.1B` 降 `54.1%`），`Total wirelength = 18,733,215`，`fallbackAttempts = 0`，`MIRROR RESULT: PASS`（`11,819,356` 次 attempt 完全对账）。
  - **收敛与合法性尾患**：R25（`6,296`）后收敛变慢，R99 结束仍留 `1,041` 过载节点，最终 `dcp_legality.log` 校验 `unroutedPins=0, sharedPipNodes=1030, ERROR: PIPs overused error: 70`（`status=failed`）。原因定位为 `F1Corridor` 在 R6+ 未退役窄框从而阻止了 `enlargeBoundingBox` 绕行消 congestion，且 R99 仅 `4,141` 条待布线时每轮重建 `F4RegionQueue` 仍耗 `7.35 s`。

### 2.6 `theia50per-ppt-hybrid-v27`（36 线程 C++ 无锁波前全轮版，已完成 R1 与 R2 实测后停止归档）
- **改动文件**：`prepare_v27.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **核心改动**：
  1. **C++ 36 线程无锁波前批并行 (`nativeRouteRound`)**：滑窗（`1024 / 32768`）按 Net 互斥打包 Wave（`W = 128`），36 个常驻 C++ 线程并发无锁搜索，协调器按真实路径节点级 `nodeTouchEpoch` 精确判冲突并提交，`> 65,536` pops 连接推入 `heavyQueue` 后段无上限波前处理。
  2. **轮内状态全驻留 C++**：启动后一次性注册全量连接与 Net 作用域（大扇出走零拷贝只读 `baseScope`），轮内零 Java `RouteNode` / `HashMap` / `F4RegionQueue` 开销；`overusedList` O(1) 增量维护，边界直接返回 `nextActive`。
  3. **R6+ 自动退场 `F1Corridor` 窄框**：R1 全量、R2–R5 仅活跃连接并 `HPWL < 16` 在 R4+ 退场、R6+ 全退场并同步放宽拥塞包围盒（`enlargeBoundingBox`），修复 `v26` R99 滞留 `1,041` 问题。
  4. **收尾全量物化与边界校验**：收敛或结束时由 `f4NativeMaterializeAll` 一次性物化回 Java `Connection` / `RouteNode` 并做前 20 条路径边校验，同步 `mirror.connections` 与 `overUsedRnodes`。
- **启动与重启记录**：
  - 初次启动（`2026-11-21T10:08`）：推进至 R1 `3,000,015 / 4,909,155 (190.2s)` 后遇节点掉线重启，运行态日志已备份至 `pre_reboot_20261121/`。
  - 重启重跑（`2026-11-23T09:11`，`worker_pid = 5859`、`hybrid = 5935`、`java = 6021`，GPU 0 `21,624 MiB / 32,768 MiB`）。
- **实测结果**：
  - `FIDES_NATIVE_CPU ready=true N=92497168 M=391524527`、`FIDES_NATIVE_CPU v27 registered=4909155 nets=858089 init_s=5.82`。
  - **Round 1 完整结束**：总耗时 **`1,242.53 s`**（`corridor_s = 14.67 s`、`cpp_s = 1,227.38 s`、`RRG = 0.00 s`），较 `v26`（`3,425.30 s`）提速 **`2.76×`**、较 `v25`（`4,342.38 s`）提速 **`3.49×`**；`routed = 4,909,155`，`par = 4,908,740`（**`99.99%` 并行**），`ser = 415`，`retries = 194,985`，`heavy = 0`，`fb = 0`，`unrout = 0`，`overused = 848,462`（`excess = 854,442`），`pops = 3,105,098,348`，`F4_PPT11_ROUND next = HOLD`。
  - **R1 分段衰减与瓶颈定位**：`0 -> 2.50M` 仅 **`99.4 s`**（`495M pops`），但 `2.50M -> 4.91M` 耗时 **`1,128 s`**（`>3.0M` 后 `java` `%CPU = 540–749%`，约 28 核空闲等待 `POOL.dispatch` 波次长尾屏障 + `SCAN_LOOKAHEAD` 高扇出大网聚簇饥饿 + `NetScopeData::pos` `std::unordered_map` 堆指针追逐）。
  - **Round 2**：`active = 2,996,999`，推进至 `2,500,017 / 2,996,999`（`t = 730.0 s`, `par = 2,500,017`, `overused = 353,698`, `pops = 1,695,749,502`）后按计划停止，转入 `v28` 优化。

### 2.7 `theia50per-ppt-hybrid-v28`（大网扁平哈希表 + 无波次屏障连续任务池 + 32B 节点对齐，已完成 R1–R8 实测并归档）
- **改动文件**：`prepare_v28.py`、`v28_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **核心改动**：
  1. **开放寻址扁平哈希表 (`NetScopeData` `ScopeSlot`)**：`> 64` 节点网告别 `std::unordered_map` 堆指针追逐，改为 8 字节 `{U32 key, I32 count}` 线性探测表（单 64B Cache Line 容纳 8 槽、内联存储 `count`），配合 `__atomic` 读写与安全 rehash 暂停机制，支持并发读者无锁读取。
  2. **无波次屏障连续动态任务池 (`DynamicExecutor`)**：取消 `POOL.dispatch(128)` 全局同步屏障，1 个轻量化 Coordinator + 35 个常驻 Worker 通过 per-worker `DynamicSlot` + `64-bit doneMask` 流水线异步衔接；通过全节点 `G.lastCommitSeq` 实现无屏障乐观冲突检测，`retryStreak >= 4` 触发防饥饿 solo 保护。
  3. **三级网调度 + 同网空间并发**：Pass 初始化 `O(N)` 建每网 FIFO 与最小堆 `distinctReadyHeap`，消除 `32768` 线性扫描；Tier 1（异网优先）→ Tier 2（活跃扁平大网搜索框互不重叠并发）→ Tier 3（核空闲 `< W/2` 时的尾盘限量大网并行）。
  4. **32B 对齐 `NodeGeom` + 8B `DynNode`**：将 10 个独立静态节点数组（共 ~3.5 GB）并在 `nativeInit` 时打包为 `alignas(32)` 32B `NodeGeom`（预存 `typeFlags`/`preFlags`/`endSlr`），`occ+hist` 打包为 8B `DynNode`，子边扩展 DRAM 随机访存大幅缩减。
- **验证与启动信息**：
  - `-Wall -Wextra -Werror -fsyntax-only` 零警告通过；独立 35 线程 100 万混合任务压测 `V28_UNIT_STRESS_OK` 连续 3 次通过（`1.50–1.60 s`，`~62.6–66.5 万任务/s`）。
  - 编译三项日志（`compile_cpp.log` / `compile_cuda.log` / `compile_java.log`）均 `0` 字节，`worker_pid = 7722`、`hybrid = 7792`、`java = 7821`（GPU 0 `21,624 MiB / 32,768 MiB`）。
  - `cpp_sha256`：`7036c6c3e28df46216dc4e57b29a729ee4fcf0de23cdaef839376ef971d7b79a`、`cuda_sha256`：`65daa2d0720ee07d6ae177ac1b929f7974cb38231c83340fdec962c6e821aa38`。
- **R1–R8 实测与停机归档（`2026-11-23T10:22` 已干净停机）**：
  - `top -H` 实测 R1 中后段与 R6 均为 **35–36 核跑满（`97.0%–97.1% us`）**；
  - **R1**：**`171.56 s`**（`cpp=154.60s`，`over=836,390`，`pops=3.23B`，`fb=1`），**优于 PPT R1（`176.06 s`）**，较 `v27` 提速 `7.24×`、较 `v26` 提速 `19.97×`。
  - **R2**：**`89.89 s`**（`active=2,995,534`，`cpp=88.22s`，`over=311,713`，`pops=2.36B`，`fb=1`）。
  - **R3**：**`78.64 s`**（`active=1,697,285`，`cpp=77.96s`，`over=141,975`，`pops=1.73B`，`heavy=20`，`fb=0`）。
  - **R4–R8**：R4 `191.32s`（`active=955,968`, `heavy=21,215`, `pops=4.47B`, `over=27,865`）、R5 `221.72s`（`active=455,114`, `heavy=19,967`, `pops=4.74B`, `over=9,828`）、R6 `252.50s`（`active=337,112`, `heavy=10,517`, `pops=5.00B`, `retries=101,196`, `over=5,380`）、R7 `cpp=206.48s`（`active=295,013`, `heavy=6,106`, `pops=3.99B`, `retries=101,021`, `over=3,604`）、R8 `cpp=183.62s`（`active=273,802`, `heavy=4,129`, `pops=3.16B`, `retries=113,016 (41.3%)`, `over=2,668`）。
  - **后期瓶颈定位**：虽 `overused` 收敛深度大幅领先 `v26`（R8 `2,668` vs `v26` `14,443`），但 R1–R8 累计 pops 已达 `28.68B`（超过 `v26` 全程 99 轮的 `21.15B`），且 GPU 全程 `HOLD`（`dispatch_ns=0`）。根因：① 缺少派发前实时 `shouldRoute()` 检查导致大量已在轮内消拥塞的连接被盲目重布并放大包围盒；② `F1Corridor` 在 R4/R6 硬退场引爆 `heavy=21,215` 与半芯片搜索框，且 Pass 1 超 `65,536` pops 全量白搜后再重跑 Pass 2；③ `lastCommitSeq` 错误对 `oldPath` 判冲突引发 R6–R8 高达 `30%–41.3%` 的重搜浪费；④ Tier 3 `addToScope` 写序微竞争导致 `fb=1`。

### 2.8 `theia50per-ppt-hybrid-v29`（实时 `shouldRoute` + `newPath` 冲突判定 + 选择性走廊退场 + 写序清零，已完成 R1–R14 后停止归档）
- **改动文件**：`prepare_v29.py`、`v29_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **核心改动（对应 #1–#4 修复）**：
  1. **派发前实时 `shouldRouteLive` 过滤**：若连接在轮内其路径 `occ` 已降至 `<= cap`，直接跳过（`skip`）不再拆旧路与重搜。
  2. **`lastCommitSeq` 仅检查与标记 `newPath`**：取消 `oldPath` 释放节点引起的假冲突。
  3. **`F1Corridor` 选择性保留与 LPT**：保留至 R8（R6+ 仅对 `consecCongested >= 3` 解锁 `origBox`，R9+ 全退场）；上轮 `> 48k` pops 直接进 Pass 2 并按 `lastPops` 降序（LPT）派发。
  4. **`addToScope` 写序 + `pccIdx < 0` Clamp**：`0->pos` 先增 `occ`（`RELEASE`）再写 `sl.key`，`pos->0` 先置 `sl.count=0` 再减 `occ`，并发读瞬态 clamp 至 `0`，彻底消除 `fb=1`。
- **验证与启动信息**：
  - `-Wall -Wextra -Werror` 零警告通过；`test_v29_stress`（16 读 1 写写序压测，`clampTriggered=6,819` 零断言失败）通过；三项编译日志均 `0` 字节；`worker_pid = 9759`、`hybrid = 9828`、`java = 9858`（停机释放），`cpp_sha256`：`4454ff485d3c440847df769cbb25e6eeff9e2168a310acbb90b3b738bb73d8b8`。
- **实测结果（R1–R14 后停机归档，全轮 `fb = 0`）**：
  - **R1**：**`169.32 s`**（`over=837,398`, `pops=3.22B`）。
  - **R2**：**`82.62 s`**（`active=2,967,900`, **`skip=753,363 (25.4%)`**, `routed=2,214,537`, `retries=36,506`, `pops=1.91B`, `over=315,471`）。
  - **R3–R5**：R3 `96.25 s`（`over=150,526`, `pops=1.33B`）、R4 `101.09 s`（`over=103,229`, `pops=1.13B`）、R5 `117.55 s`（`over=89,505`, `pops=1.07B`）。
  - **R6–R14**：R6 `290.68 s`（`over=33,070`, `heavy=18,715`, `pops=6.11B`）、R7 `213.42 s`（`over=21,399`, `pops=3.90B`）、R8 `180.36 s`（`over=15,431`, `pops=2.92B`）、R9 `223.93 s`（`over=5,326`, `pops=4.59B`）、R10 `151.01 s`（`over=3,497`, `pops=2.78B`）、R11 `120.94 s`（`over=2,701`, `ser=23,321`）、R12 `103.60 s`（`over=2,267`, `ser=23,331`）、R13 `92.82 s`（`over=2,017`, `ser=23,659`）、R14 `93.21 s`（`over=1,739`, `ser=23,743`, `retries=29,619`, `pops=1.20B`）。
  - **已定位的下一层尾盘与扩展框根因**：① 因同网连接共起点 `src`，`Tier 2` 恒为 `0`，少网尾盘触发的 `Tier 3`（12 并发）+ `lastCommitSeq` 误将同 Net 共享主干（`occ >= cap`）判为冲突，导致 `retries=30k–71k` 并触发 `prioritySoloActIdx` 全池排空（R11–R14 `ser=23,321..23,743` 退化到单核，每轮浪费 `50–85 s`）；② R6 集中取消 `buse` 一步跳进已膨胀 5 轮的 `origBox` 且 R9 全局关停 `F1Corridor`，引爆 R6 `6.11B` 与 R9 `4.59B` pops。

### 2.9 `theia50per-ppt-hybrid-v30`（PPT P11/P18 对齐：跨网冲突精确分离 + 无全局抽空 + 渐进有界放宽，86 轮全收敛 & DCP 合法通过）
- **改动文件**：`prepare_v30.py`、`v30_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds.py`。
- **核心改动**：
  1. **跨 Net / 同 Net 精确冲突分离 + 取消全局停摆**：`GraphData` 新增 `lastCommitNet` 与 `prevOtherSeq`，仅当不同 Net 提交节点造成 `occ >= cap` 时才触发回退，同 Net 主干共享免除假冲突；彻底移除 `prioritySoloActIdx`，持久冲突连接延后到 pass 末 `tailSoloQueue`，`Tier 3` 按 `netConflicts` 单 Net 自适应限流而不阻塞其它 Net 并行。
  2. **PPT P11 三级预算 + 全轮走廊 + 逐连接渐进有界放宽**：取消 R9 全局停用 `F1Corridor`；按 `consecCongested`（`>=2 / >=4 / >=6`）及 `NO_PATH_CORRIDOR` 对每条连接渐进扩宽 `connWiden`（`0 -> ±8/±16 -> ±20/±40 -> origBox`），拥塞清除即复位；执行 `Pass 1 (65,536) -> Pass 2 Base Heavy (262,144 LPT) -> Pass 3 Relaxed Heavy (无上限 LPT)` 三级搜索。
  3. **`getUsage` 撕裂一致性强化**：当 `occVal <= 0` 时归零并发 `sameUses`。
- **编译与压测**：
  - `-Wall -Wextra -Werror` 零警告通过；`test_v30_stress`（200,000 次跨 Net 交错冲突预言机校验 + 16 读 1 写并发写序校验 `badReads=0, clamps=56`）全过；`compile_cpp.log` / `compile_cuda.log` / `compile_java.log` 全 `0` 字节；`cpp_sha256`：`f1d15395465b5c5b9cd0dc656458f89acb818918f7da5e2acc92249cc146e432`。
- **全流程实测与合法性结果（`status="passed", stage="complete"`）**：
  - **R1–R9 关键轮次**：R1 **`159.01 s`**（`over=837,884`, `same_ok=83,326`，优于 PPT R1 `176.06 s`）、R2 **`66.44 s`**（`over=315,329`）、R3 `85.24 s`（`over=103,983`, `retries=14,547`, `same_ok=59,166`）、R4 `117.36 s`（**`over=33,684`**）、R5 `137.41 s`（`over=16,569`）、R6 **`116.72 s`**（`over=11,079`, `pops=2.66B`，较 `v29` R6 `290.68 s` 快 `2.49×`）、R7 `90.20 s`（`over=7,875`）、R8 `81.15 s`（`over=6,043`）、R9 **`71.09 s`**（`over=4,728`, `pops=0.995B`，较 `v29` R9 `223.93 s` 快 `3.15×`）。
  - **后程极速收敛（R10–R86）**：R10 `47.75 s`（`over=3,865`, `same_ok=38,654`）、R13 `15.34 s`（`over=2,468`, `ser=1`, `retries=19`）、R22 `3.90 s`（`over=985`）、R50 `1.13 s`（`over=288`）、**R86 `0.07 s`（`over=0`, `fb=0`, `unrout=0`）**；86 轮累计耗时 **`1,268.72 s`**（前 12 轮 `1,052.60 s` + R13–R86 仅 `216.12 s`），全轮共免除 **`403,775`** 次同 Net 假冲突（`same_ok`）。
  - **汇总与 DCP 合法性**：`V30_MATERIALIZE_VERIFY_OK checked=20`、`FIDES_NATIVE_CPU v30 materialized=4909155 overused=0 time_s=108.47`、`MIRROR RESULT : PASS`、`INFO: No PIP overlaps`、`FIDES fallbackAttempts=0` / `backtracks=0` / `unroutable=0`；**`Total wirelength: 17,815,441`**（较 `v26` `18,733,215` 缩短 **`4.90%`**）、`Nodes popped: 25,863,474,181`、**`Route design: 1,780.12 s`**（`Initialization 22.86s` + `Routing 1,757.26s`，较 `v26` `26,557.94 s` 提速 **`14.92×`**）；**`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=30542009`**，输出文件 `THEIA_50per_gpu_full.dcp`（`347 MiB`）。
  - **后续优化空间**：① 后处理回写/物化阶段耗时约 `488.5 s`（`1,757.26s - 1,268.72s`，含 `108.47s` 节点物化与 RapidWright `fixRoutes`/`mirror` 校验）；② `Route` 外 `computesNodeUsageAndTotalWirelength()` 在单线程下耗 `~850 s`；③ R1–R12 C++ 搜索占 86 轮 `83%`。

### 2.10 `theia50per-ppt-hybrid-v31`（C++ 搜索紧缩与 Java 全并行后处理验证，触发两处明确故障，已归档）
- **改动文件**：`prepare_v31.py`、`v31_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`。
- **核心改动**：
  1. **C++ Track A**：提交时衰减 `netConflicts` 与 `capForNet`（`c>=64->1, c>=16->2`）消除 R8–R12 大网串行（`ser` 由 `7,377..11,280` 降至 `254..2,274`）；4 阶紧扩框（`±4/±8 -> ±10/±20 -> ±22/±44 -> origBox`，分别在 `cc>=2/4/6/8` 触发）；L1 栈内 `tinyOv[32]` + `tinyBloom` + `ovEmpty`；`sameUses==0` 零除法快速路径。
  2. **Java Track B**：`f4SharedPool` 36 核并行化 `f4NativeMaterializeAll`、`setPIPsOfNets()`、64 分片 `checkPIPsUsage()` 与 `computesNodeUsageAndTotalWirelength()`（复用 `rn.getLength()` 缓存）。
- **编译与压测**：`-Wall -Wextra -Werror` 通过；`/tmp/test_v31_stress` 通过（`V31_TINY_OV_OK 2000 trials (500000 queries) passed`、`V31_IEEE_EXACT_OK 100000 float bit-exact checks passed`）；`compile_*.log` 全 `0 B`；`cpp_sha256`：`6afbb46199fe42b72075d61381ffd22cd5aa82c64a3a7ceb57dc7e1adce68963`。
- **实测结果与故障定位（`13:01:51 -> 13:26:38`，`status="failed", error="DCP legality"`）**：
  - **性能兑现**：R1 C++ `125.23 s`（vs `v30` R1 轮 `159.01 s`）、R1–R12 C++ 合计 **`694.96 s`**（vs `v30` `1,032.69 s`，降 **`-337.73 s`**）、99 轮 `sum_cpp_s = 929.11 s`（`21.65B pops`）；`f4NativeMaterializeAll = 93.26 s`（`export=0.32, cache=59.03, bind=2.70, users=31.22`）、`setPIPsOfNets = 10.98 s`、`checkPIPsUsage = 7.24 s`、`computesNodeUsageAndTotalWirelength = 8.66 s`（由 `~850 s` 压至 `8.66 s`）；`Route design = 1,237.37 s`（WL `18,008,458`）。
  - **Fail 根因 #1（`unroutedPins=3314`）**：`F4_V31_SETPIPS` 打印 `19,874` 条 `ERROR: Null PIP connecting these two nodes`（分布于 `18,665` 个 tile）。经 `javap -c -p` 反汇编确认 RapidWright `com.xilinx.rapidwright.device.i.a(int, int)`（`TileType` PIP 映射表）懒初始化 `this.a = new HashMap<Long, g>()` 无锁保护且先写字段再填表，多线程首触并发读到半初始化空表返回 `null` PIP。
  - **Fail 根因 #2（`sharedPipNodes=40`）**：`R99` 触达迭代上限退出时仍留 `over=40`（`ERROR: PIPs overused error: 5`）。根因系 `cc=0 / connWiden=0` 在非拥塞轮直接清零缩框，导致震荡连接难以连续 8 轮受阻并跨过 `cc>=8` 解锁 `origBox`。

### 2.11 `theia50per-ppt-hybrid-v32`（Dekker 无锁调度 + 边预取 + 长尾单调扩框 + `TileType` PIP 预热 + 分片用户记账，74 轮全收敛 & DCP 合法通过）
- **改动文件**：`prepare_v32.py`、`v32_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds_v32.py`。
- **核心改动**：
  1. **修复 `setPIPsOfNets` Null PIP 竞态并强制 Fail-Closed**：并行启动前在主线程按 `TileTypeEnum` 预热调用 `tile.getPIP(0, 0)`（全器件 `200` 类 tile type）；并行阶段核验 `pips.size() == connNodes.size() - 1`。
  2. **修复 R99 尾段震荡收敛（`routeIter` 感知两段式扩框）**：`nativeUpdateHistAndFeedback` 接收 `routeIter`，前中期（`routeIter < 20 && overused > 3500`）保持 4 阶紧扩框且非拥塞按 `cc>>=1, --connWiden` 温和退阶；进入尾段（`routeIter >= 20 || overused <= 3500`）快速升阶（1/2/3 次拥塞即升 `±10/±20 -> ±22/±44 -> origBox`）且 `connWiden` 单调不回缩。
  3. **C++ 调度与搜索微优化**：`DynamicExecutor` 改用 4096 次 pause + `seq_cst` Dekker 唤醒免除千万级互斥/futex 开销；`expandNode()` 合并 `slotForRead/slotForWrite` 为单次页表查询、加入 `G.edge[e + 2]` 软件预取并缓存 `netSizeF/doubleHpwlF`。
  4. **物化与记账并行化 (`f4NativeMaterializeAll`)**：36 线程只读并行预查 `idMap.nodeAt(graph, id)`（缺省回退顺序 `getChildren` 补齐）+ 36 线程并行绑定并在同趟按 `System.identityHashCode(rn) & 63` 分桶，由 64 分片并行调用 `rn.incrementUser(nw)`。
- **编译与压测**：`-Wall -Wextra -Werror` 通过；`/tmp/test_v32_stress` 通过（`V32_DEKKER_EXEC_OK 250000 tasks x 35 workers completed without wakeup loss`、`V32_TAIL_WIDEN_OK`）；`compile_*.log` 全 `0 B`；`cpp_sha256`：`8efa39e0d50aa00eea2af903a4a9a3a2c08ddc5d4cf02154d4ee13bb8118ccd2`。
- **全流程实测与合法性结果（`2026-11-23T13:51:36 -> 14:18:36`，`status="passed", stage="complete"`）**：
  - **轮次与搜索收敛**：**74 轮全收敛到 `over=0`**（`v30` 为 86 轮）：`R1 cpp=136.00 s (round=151.19 s, over=839,402)`、`R2 cpp=66.39 s (over=315,130)`、`R5 cpp=110.84 s (over=16,509)`、`R12 over=3,298`（触发尾段快速放宽）→ `R13` 骤降至 `over=1,666`（较 `v31` 同轮 `3,105` 砍近半）→ `R28 over=534` → **R74 `over=0, fb=0, unrout=0`**；74 轮搜索 `sum_cpp = 1,083.64 s`（R1–R12 `796.59 s` + R13–R74 `287.05 s`，`sum_corr = 17.80 s`，27.64B pops，74 轮累计 `round_s = 1,113.60 s`）。
  - **后处理实测明细**：
    - `V32_MATERIALIZE_VERIFY_OK checked=20`
    - `FIDES_NATIVE_CPU v32 materialized=4909155 overused=0 seq_fix=4909155 time_s=70.67 (export=0.28 cache=60.28 bind=5.28 users=4.83)`（`users` 由 `31.22 s` 降至 **`4.83 s`**）
    - `MIRROR RESULT : PASS`、`FIDES fallbackAttempts=0` / `backtracks=0` / `unroutable=0`
    - `F4_V32_SETPIPS nets=901653 tileTypes=200 time_s=9.49`（**零 Null PIP**）
    - `F4_V32_CHECKPIPS nets=1267485 time_s=7.19`（`INFO: No PIP overlaps`）
    - `F4_V32_STATS_WL nets=901653 time_s=8.95`
  - **汇总与端到端对比（vs `v30` / PPT）**：
    - `Total wirelength: 17,977,885`、`Nodes popped: 27,637,744,581`
    - **`Route design: 1,359.05 s`**（`Initialization 21.67 s` + `Routing 1,337.38 s`，较 `v30` `1,780.12 s` 缩短 `-421.07 s`，较 `v26` 快 **`19.54×`**）
    - **`route_wall_ns`: `1,467.67 s`（`24m 28s`）**（较 `v30` `2,742.38 s` 压缩 **`-1,274.71 s` / `-46.5%` / `1.87×` 加速**）
    - **`end_to_end_with_preparation_seconds`: `1,619.06 s`（`26m 59s`）**（较 `v30` `2,885.00 s` 压缩 **`-1,265.94 s` / `-43.9%`**，含 `resident_preflight 9.41s`、`Read DCP 39.21s`、`direct_finalize 77.50s`、`Write DCP 16.67s`、`dcp_legality 82.83s`）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=30628236`**，输出 `THEIA_50per_gpu_full.dcp`（`347 MiB`）。
  - **距 PPT E2E（`517.28 s`）的剩余拆解与下一轮切入点**：当前 `1,619.06 s` 中 ① `R1–R12` + `R13–R74` C++ 搜索占 `~1,100 s`（`27.64B pops`，其中 R3–R7 与 `R13` 上升 `41.17 s` 是重点），② `f4NativeMaterializeAll` 的 `cache=60.28 s` 仍串行于 `seq_fix=4,909,155`（因大多数路径碰及未实例化 `RouteNode` 触发串行 `getChildren`），③ `direct_finalize` 仍占 `77.50 s`、外部 `dcp_legality` 核验占 `82.83 s`、`Read/Write/Init` 占 `~78 s`。

### 2.12 `theia50per-ppt-hybrid-v33`（64 分片并行 `RouteNode` 创建 + 256-pause Dekker + 精准 `<=1800` 尾段 + 重尾预分流，68 轮全收敛 & DCP 合法通过）
- **改动文件**：`prepare_v33.py`、`v33_patch.py`、`v27_patch.py`、`v24_patch.py`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds_v33.py`。
- **核心改动**：
  1. **消除 `f4NativeMaterializeAll` 的 `cache=60.28 s` 串行瓶颈**：主线程构建 `tileByAddr[]` 与预热 `highestBaseWireIndex` 后，按 `tileAddress & 63` 分 64 个互不相交分片，由 36 核直接并行调用 `graph.getOrCreate(Node.getNode(tile, wi))` 仅创建实际路由途经 `RouteNode`，避免顺序走 `getChildren` 构造约四千万冗余节点，实现 **`seq_fix=0`**。
  2. **回收并超越 `v31` R1–R12 速度**：`DynamicExecutor` 自旋由 `4096` 回调至 `256` 次 pause（保留无锁 Dekker 唤醒且消除 36 核总线争用）、撤除越框前投机 `G.dyn` 预取；前中期（`routeIter < 22 && overused > 1800`）恢复 `v31` 紧凑重置策略，仅当 `overused <= 1800 || routeIter >= 22` 切入单调尾段放宽，避免 `v32` 在 `R13` 上 `55k` 连接集中放宽。
  3. **扩框重尾预分流**：对 `connWiden >= 1 && lastPops > 16,384` 直送 Pass 2（`262,144`）、`connWiden >= 3 && lastPops > 98,304` 直送 Pass 3，显著减少 `R4–R6` 在 Pass 1（`65,536`）上的无效淘汰。
- **编译与压测**：`-Wall -Wextra -Werror` 通过；`/tmp/test_v33_stress` 通过（`V33_DEKKER_256_OK 250000 tasks x 35 workers zero loss`、`V33_REGIME_TIER_OK passed`）；`compile_*.log` 全 `0 B`；`cpp_sha256`：`5ce7795bf51cf0e4815e740a3a4186bd3b8edc9d32cc9855387c850894f5273f`。
- **全流程实测与合法性结果（`2026-11-23T14:31:04 -> 14:54:42`，`status="passed", stage="complete"`）**：
  - **轮次与搜索收敛**：**68 轮全收敛到 `over=0`**（`v30` 86 轮、`v32` 74 轮）：`R1 cpp=133.81 s`、`R2 cpp=68.39 s`、`R3 cpp=61.01 s`、`R4 cpp=68.78 s (2.054B pops)`、`R5 cpp=93.86 s (2.581B pops)`、`R6 cpp=64.09 s (1.750B pops)`、`R12 cpp=14.27 s (over=3,588)` → `R19 over=1,760` 触发尾段切换 → `R20 over=900` → **R68 `over=0, fb=0, unrout=0`**；`R1–R12 sum_cpp = 663.61 s`（较 `v32` `796.59 s` 降 **`-132.98 s`**，较 `v30` `1,032.69 s` 降 **`-369.08 s`**）、全程 `sum_cpp = 957.22 s`（21.34B pops，较 `v32` 少 `6.30B pops`）、68 轮累计 `round_s = 984.08 s`。
  - **后处理实测明细**：
    - `V33_MATERIALIZE_VERIFY_OK checked=20`
    - `FIDES_NATIVE_CPU v33 materialized=4909155 overused=0 seq_fix=0 time_s=15.22 (export=0.68 cache=6.16 bind=4.27 users=4.10)`（总物化耗时 **`15.22 s`**，较 `v32` `70.67 s` 快 **`4.64×`**、较 `v30` `108.47 s` 快 **`7.13×`**）
    - `MIRROR RESULT : PASS`、`FIDES fallbackAttempts=0` / `backtracks=0` / `unroutable=0`
    - `F4_V33_SETPIPS nets=901653 tileTypes=200 time_s=11.83`、`F4_V33_CHECKPIPS nets=1267485 time_s=7.11`（`INFO: No PIP overlaps`）、`F4_V33_STATS_WL nets=901653 time_s=8.14`
  - **汇总与端到端对比（vs `v30` / `v32` / PPT）**：
    - `Total wirelength: 18,088,147`、`Nodes popped: 21,342,845,794`
    - **`Route design: 1,173.14 s`**（`Initialization 23.02 s` + `Routing 1,150.12 s`，较 `v32` `1,359.05 s` 降 `-185.91 s`，较 `v30` `1,780.12 s` 降 `-606.98 s`，较 `v26` 快 **`22.64×`**）
    - **`route_wall_ns`: `1,281.18 s`（`21m 21s`）**（较 `v30` `2,742.38 s` 快 **`2.14×`**）
    - **`end_to_end_with_preparation_seconds`: `1,417.10 s`（`23m 37s`）**（较 `v30` `2,885.00 s` 缩短 **`-1,467.90 s` / `-50.9%`**，含 `resident_preflight 3.55s`、`Read DCP 42.93s`、`direct_finalize 75.57s`、`Write DCP 15.58s`、`dcp_legality 83.97s`）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=30719060`**，输出 `THEIA_50per_gpu_full.dcp`（`347 MiB`）。

### 2.13 `theia50per-ppt-hybrid-v34`（单卡 V100 进程内 CUDA 双引擎，`88` 轮全收敛 & DCP 合法通过）
- **改动文件**：`prepare_v34.py`、`v34_patch.py`、`fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds_v34.py`。
- **核心架构与调优演进（严格 1 × Tesla V100-PCIE-32GB，零 Java IPC）**：
  1. **进程内常驻单卡 V100 显存布局（`init_s = 1.44 s`，`resident_preflight = 0.071 s`）**：由 `libfides_cpu.so` 通过 `fidesGpuInit()` 直接管理单卡 `GPU-81b72957-767e-40f3-b287-f427f1ad6528`，全图 92,497,168 节点 / 391,524,527 边与 16B 压缩 `FidesGpuNodeHot` 常驻（`res = 3.87 GiB`）+ `Tier-A` (`1,236` warps) / `Tier-B1` (`78` CTAs) / `Tier-B2` 显存搜索池（`arena = 23.05 GiB`，`free = 4.42 GiB`，总占用约 `28.27 GiB`），并通过脏页增量 `fidesGpuApplyOccDeltas`/`HistDeltas` 与 192 个硬件常量大网槽位同步。
  2. **虚拟 `oldPath` 扣除与双策略访存一致**：全核引入 `oldBloom64` + 排序 `oldNodes`（`d_countOldPath` 与 `Tier-B1` `__shared__ s_oldNodes[512]`），在 `usePreRip=false` 时与 `v33` `OverrideSlot` 完全等价扣减自身旧路径占用；`Tier-A` 采用 `hashInsertOrRelax`（`shift=18, popBatch=32`），`Tier-B1` 采用与 `v33` 一致的 `hashMarkOnPush`（`shift=17, popBatch=256`）。
  3. **`HPWL_FIRST` 最小堆波前配比 + PPT 异构分流阈值**：`runGpuPass` 采用 `GpuReadyNet` 按 `HPWL_FIRST` 顺序每次每 Net 仅取 1 条活跃连接以保留 `21×` Steiner 同网共享，尾段少于 `targetWave/2` 净网数时自适应放大 `mpnFill`（`min(64/32, max(8, ceil(targetWave/effNets)))`）消除 SM 饥饿；按 PPT 收益阈值仅将 `R1–R2` Pass 1 送入 `Tier-A`、`Pass 2 > 8,000` 送入 `Tier-B1`、`Pass 3 (R<=5 && > 1,000)` 送入 `Tier-B1`（`megaQueue >= 3.5M`），`R7+` 重尾全部无缝回落 35 核 CPU `DynamicExecutor`。
- **编译与核验**：`-Wall -Wextra -Werror` 与 `nvcc -O3 -arch=sm_70` 编译零警告零错误（`compile_*.log` 全 `0 B`）；`cpp_sha256`：`b82acbffed6a631489f8ced5b0cb7e36d87979a20e801ccb9806777c8e8ba567`，`fides_gpu_cu_sha256`：`8a650e004935a2486f0c402ba121ba2e495ce9792e9f8450865a1eefa1e1e240`。
- **全流程实测与合法性结果（Run #17，`2026-11-24T11:44:49 -> 12:07:21`，`status="passed", stage="complete"`）**：
  - **分段实测对比（vs `v33`）**：
    - `R1–R2`：`v34` **`184.20 s`**（GPU `108.82 s`, `5.76B` gpu pops, `over=312,623`）vs `v33` `202.20 s`（`over=315,229`），**`-18.00 s`**
    - `R3–R4`：`v34` **`119.81 s`**（GPU `6.22 s`, `0.45B` gpu pops, `over=39,942`）vs `v33` `129.79 s`（`over=38,675`），**`-9.98 s`**
    - `R5–R6`：`v34` **`107.57 s`**（GPU `35.62 s`, `2.44B` gpu pops, `over=13,545`）vs `v33` `157.95 s`（`over=12,961`），**`-50.38 s`**
    - `R7–R13`（100% CPU）：`v34` `210.29 s`（`over=3,281`）vs `v33` `192.92 s`（`over=3,109`），`+17.37 s`
    - `R14–R22`（100% CPU）：`v34` **`145.57 s`**（`over=742`）vs `v33` `154.24 s`（`over=650`），**`-8.67 s`**
    - `R23–R88`（100% CPU）：`v34` `124.80 s`（**`over=0`**）vs `v33` `R23–R68` `120.12 s`，`+4.68 s`
    - **全轮搜索汇总**：**88 轮全收敛到 `over=0`**，`sum_cpp = 892.24 s`（`cpu_probe_ns = 892.27 s`, `cum_s = 922.04 s`）vs `v33` `957.22 s`（**`-64.98 s`**）；`v34_gpu_commits = 6,968,592`（占 `9,105,015` 总尝试的 `76.5%`），`v34_gpu_pops = 8,648,432,525`（总 `23,103,690,494`），`v34_gpu_ns = 150.66 s`、`2,877` GPU waves、`gpu_ov=0, gpu_fb=0`。
  - **后处理与合法性明细**：
    - `V34_MATERIALIZE_VERIFY_OK checked=20`、`FIDES_NATIVE_CPU v34 materialized=4909155 overused=0 seq_fix=0 time_s=16.29 (export=0.71 cache=7.45 bind=3.63 users=4.49)`
    - `MIRROR RESULT : PASS`、`FIDES fallbackAttempts=0` / `backtracks=0` / `unroutable=0`
    - `F4_V34_SETPIPS nets=901653 tileTypes=200 time_s=9.75`、`F4_V34_CHECKPIPS nets=1267485 time_s=6.34`（`INFO: No PIP overlaps`）、`F4_V34_STATS_WL nets=901653 time_s=10.80`
  - **汇总对比（vs `v33`）**：
    - `Total wirelength: 18,690,728`（vs `v33` `18,088,147`）、`Nodes popped: 23,103,690,494`
    - **`Route design: 1,122.36 s`**（`Initialization 24.25 s` + `Routing 1,098.11 s`，较 `v33` `1,173.14 s` 缩短 **`-50.78 s`**）
    - **`route_wall_ns`: `1,226.84 s`**（较 `v33` `1,281.18 s` 缩短 **`-54.34 s`**）
    - **`end_to_end_with_preparation_seconds`: `1,350.42 s`（`22m 30s`）**（较 `v33` `1,417.10 s` 缩短 **`-66.69 s`**，含 `resident_preflight 0.071 s`、`direct_finalize 79.58 s`、`Write DCP 15.04 s`、`dcp_legality 79.18 s`）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31000382`**，输出 `THEIA_50per_gpu_full.dcp`。

### 2.14 `theia50per-ppt-hybrid-v35`（Route A 首轮实验：零拷贝 Pinned + 并行 Pack + 全轮 Tier-B1/B2，64 轮全收敛 & 定位 R3–R5 / Tier-B2 问题）
- **改动文件**：`prepare_v35.py`、`v35_patch.py`、`fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds_v35.py`。
- **核心改动**：
  1. CUDA Pinned 零拷贝波前缓冲（`fidesGpuAcquireWaveBufs` + `fidesGpuRunTierADirect` / `B1Direct` / `B2Direct`）+ `DynamicExecutor::parallelFor` 35 核并行 Host Pack（`populateNetSameDescBuf` / `fillGpuQueryDirect`）与脏页拼装。
  2. 移除 `runGpuPass` / `Tier-B2` 对 `lastCommitSeq` 等三份 370 MB 全局数组的冗余写操作，R1 升至 `targetWave=8192`。
  3. 尝试将 `Pass 1 >= 250k` 留 GPU `Tier-A`、全轮 `Pass 2 >= 128` / `Pass 3 >= 32` 进 `Tier-B1 (shift=17, pop=256)`，并将 `lastPops >= 3.5M` 与溢出送入 80-SM `Tier-B2`。
- **编译与实测**：`-Wall -Wextra -Werror` 零警告（`cpp_sha256`：`5a69f1fc262edc78ef4ef59d127de193bcf22dd6648efaea6e976123cdbe144c`，`fides_gpu_cu_sha256`：`d4f17462c4fd1b509ed53055726f0a52c8c635a0cee38eb52df2ae482b537d04`）；`smoke_v35_direct` 通过；全流程于 `2026-11-24T12:53 -> 13:16` 完成，**64 轮全收敛（`over=0`）**：
  - `R1 = 84.98 s`（vs `v34 98.67 s`, **`-13.69 s`**）、`R2 = 71.12 s`（vs `v34 85.53 s`, **`-14.41 s`**），`R1+R2` 合计 **`-28.10 s`**。
  - `Pass 2 (n>=256)` 降 **`-16.38 s`**（`91.27 s -> 74.89 s`）、`Pass 3 (n>=200)` 降 **`-125.79 s`**（`303.13 s -> 177.34 s`，如 R16 `26.64 s -> 4.92 s`）。
  - 但暴露两大失分项：① **`R3–R5` Pass 1 强留 `Tier-A` 反增 `+35.67 s`**（R3 `61.25->69.74 s`, R4 `50.49->60.47 s`, R5 `28.12->45.32 s`）并使 R4/R5 过载上升 `25%–34%`，级联推高 `R6+ Pass 1 (+16.55 s)` 与 WL（`19,408,174`）；② **`Tier-B2` 串行全 GPU `grid.sync()` 实际跑出 `SUM mega_s = 156.67 s`**（如 R9 11 条 `21.82 s`、R19 7 条 `15.28 s`；`probe_b2_cmp` 实测 `TOP-16` 重尾下 `Tier-B2` `2.40–8.25 s` 比 35 核 CPU `1.36 s` 和 `Tier-B1` `0.31–0.47 s` 慢 2–8×）。
  - 汇总：`sum_cpp = 927.47 s`（`25.565B` pops）、`Route design = 1,146.31 s`、`route_wall = 1,241.21 s`、`dcp_legality PASS`（`unroutedPins=0, sharedPipNodes=0, pips=31,336,564`）。

### 2.15 `theia50per-ppt-hybrid-v36`（Route A 次轮优化：退役 Tier-B2 + 80-SM Tier-B1 + 35 核并行 Phase 4 + R1–R2 分界回归，62 轮全收敛 & 当前最优）
- **改动文件**：`prepare_v36.py`、`v36_patch.py`、`fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`libfides_cpu.so`、`record_rounds_v36.py`。
- **核心改动**：
  1. **`Pass 1` 分界回归 `R1–R2` 并将 `R2 targetWave` 升至 `8192`**：消除 `R3–R5 Tier-A` 长尾停顿与过载级联（R2 波次由 1,192 减至 993）。
  2. **退役低效串行 `Tier-B2` 释放 ~1.5 GiB 显存**：`fidesGpuInit` 取消 `d_tmPool` 分配，使 **`Tier-B1` 扩至全卡 80 CTA (`tbCtas=80`) + `Tier-A` `1280` warps**（`free=4.43 GiB`），并将 `TB_LA_CAP` 扩至 `262144`、`TB_OV_CAP` 扩至 `3.5M`；取消 `lastPops >= 3.5M` 逐出机制，令 `Pass 2 >= 256`、`Pass 3 >= 160` 全进 `Tier-B1`，仅少数溢出查询直接回退 35 核 CPU HOT。
  3. **R1/R2 Host 提交并行化与异步 Mega 缓存**：`MAX_MEGA_SCOPES` 扩至 `384` 并改 `fidesGpuUploadMegaScope` 为 pinned 异步传输；`runGpuPass` `Phase 4` 拆为“4A 冲突校验 -> 4B 按 `netId` 35 核并行 `addToScopeDeferred` -> 4C `occ` 合并写回”。
- **编译与核验**：`-Wall -Wextra -Werror` 与 `nvcc -O3 -arch=sm_70` 四日志全 `0 B`；`cpp_sha256`：`5d8b3b4ce3ebc4fa0852c85882ee51784ceba9cc4cbc84adc2c5056b8749286e`，`fides_gpu_cu_sha256`：`5d89dee8c3265d75fb2d77bb18540f1e168aa1a71a03e29f7040b56bb6011b5f`；`smoke_v36_direct` 输出 `taWarps=1280 tbCtas=80 freeGiB=4.43 okA=256/256 okB=256/256 slot300=300`。
- **全流程实测与合法性结果（`2026-11-24T14:26:03 -> 14:47:02`，`status="passed", stage="complete"`）**：
  - **分段实测对比（vs `v34`）**：
    - `R1–R2`：`v36` **`150.71 s`**（`R1 86.64 s` + **`R2 64.07 s`**, GPU `93.43 s`）vs `v34` `184.20 s`，**`-33.49 s`**
    - `R3–R5`：`v36` `173.81 s`（`60.12 / 58.45 / 55.24 s`, `over=20,783`）vs `v34` `173.77 s`（消除 `v35` `+33.60 s` 退化）
    - `R6–R22`（`Tier-B1` P2/P3 并行）：`v36` **`322.17 s`**（R6 `40.08 s`, R15 `7.81 s`, R16 `12.52 s`, `mega_s=0.00 s`）vs `v34` `401.84 s`，**`-79.67 s`**
    - `R23–R62`：`v36` `163.74 s`（**62 轮收敛至 `over=0`**）vs `v34` `R23–R88` `132.43 s`（`R7–R31` 中 `gpu_ov=149` 回退 CPU 占用部分 P3 时间）
    - **全轮搜索汇总**：**62 轮全收敛到 `over=0`**，**`sum_cpp = 810.43 s`**（`p1=377.13 s, p2=80.94 s, p3=350.80 s, mega=0.00 s`；GPU `355.06 s` / `16.38B pops` / `2,043 waves`）vs `v34` `892.24 s`（**`-81.81 s` / `-9.2%`**），vs `v35` `927.47 s`（**`-117.04 s` / `-12.6%`**）。
  - **后处理与合法性明细**：
    - `V36_MATERIALIZE_VERIFY_OK checked=20`、`FIDES_NATIVE_CPU v36 materialized=4909155 overused=0 seq_fix=0 time_s=14.47 (export=0.72 cache=5.57 bind=3.62 users=4.56)`
    - `MIRROR RESULT : PASS`、`FIDES fallbackAttempts=0` / `backtracks=0` / `unroutable=0`
    - `F4_V36_SETPIPS nets=901653 tileTypes=200 time_s=9.24`、`F4_V36_CHECKPIPS nets=1267485 time_s=6.18`（`INFO: No PIP overlaps`）、`F4_V36_STATS_WL nets=901653 time_s=11.00`
  - **汇总对比（vs `v34` / `v35`）**：
    - `Total wirelength: 19,015,283`、`Nodes popped: 24,060,800,622`
    - **`Route design: 1,032.72 s`**（`Initialization 23.03 s` + `Routing 1,009.69 s`，较 `v34` `1,122.36 s` 缩短 **`-89.64 s`**，较 `v35` `1,146.31 s` 缩短 **`-113.59 s`**）
    - **`route_wall_ns`: `1,131.66 s`**（较 `v34` `1,226.84 s` 缩短 **`-95.18 s`**）
    - **`end_to_end_with_preparation_seconds`: `1,258.36 s`（`20m 58s`）**（较 `v34` `1,350.42 s` 缩短 **`-92.06 s`**，含 `Read DCP 35.80 s`、`resident_preflight 0.067 s`、`direct_finalize 80.47 s`、`Write DCP 14.03 s`、`dcp_legality 81.47 s`）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31159275`**，输出 `THEIA_50per_gpu_full.dcp`。

### 2.16 `theia50per-ppt-hybrid-v37`（Route B 全栈框架降耗 + Route A-light 单卡 `16M/32M` Wide 实验，69 轮全收敛 & E2E 新低 `1,243.55 s`）
- **改动文件**：`prepare_v37.py`、`v37_patch.py`、`deploy_v37.py`、`launch_v37.py`、`record_rounds_v37.py`、`fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`RWRoute.java`、`FidesRrgIdMap.java`、`worker.py`。
- **核心改动**：
  1. **Route B（后处理 / I/O / 合法性全栈降耗）**：
     - **消除 `direct_finalize` IPC 税**：仅保留 `F4DirectGpu("direct_finalize")` 产出 `cpu_path_searches=0` 合规记录，清零 `f4RawPaths`，预热 `tile.getPIP(0, 0)` + `tile.getForwardTilePIPs(0)`（判 `tile.getWireCount() > 0`），用 36 核 `f4SharedPool` 并行 `assignNodesToConnections()` 投影，再经 `fixRoutes()`（在无过载非时序模式用 `v37RipUpNoJournal` 36 核并行修复）。
     - **二进制 `rrg.idmap.bin` 缓存与闭环镜像跳过**：预生成 `355 MiB` `rrg.idmap.bin`（`-Dfides.v37.idmapCache`，实测 2,083 次双向校验 0 失败 `IDMAP RESULT: PASS`）替代 4.67 GB 文本解析；`-Dfides.v37.skipClosingMirrorScan=true` 跳过 `mirrorClosingScan()` / `mirrorFixedNet()` / `mirrorOutOfBandRip()`（保留 `mirrorInit()`）。
     - **64 分片并行 `F4BaselineDcpCheck.java`**：`DesignTools.updatePinsIsRouted` 按 Net 并行 + 64 分片 `ConcurrentHashMap<Node, Net>` 校验 PIP 独占权。
  2. **Route A-light（单卡 `d_tbPool` 动态 `16M/32M` 扩槽 + Pass 3 阈值 `16`）**：零额外显存将 `d_tbPool` 动态切分为 `mode=0`（`8M/80 CTAs`）、`mode=1`（`16M/40 CTAs`）、`mode=2`（`32M/21 CTAs`），`Pass 3` 溢出尝试 `Wide-16M -> Ultra-32M`，并将 `Pass 3` GPU 阈值从 `160` 降至 `16`。
- **编译与组件冒烟**：`-Wall -Wextra -Werror` 与 `nvcc -O3 -arch=sm_70` 四日志全 `0 B`；`worker_sha256`：`69c4a777db36dd800d2ccd3c7c13b697331018e5bb1949f43a858811a5cb6e41`，`cpp_sha256`：`4dd0a146ad7e51ebdf659f3027f50ad7932debbab66c59276e01c50a61e704a4`，`cuda_sha256`：`e8387764a23b00cc49151be34d704ef2d1b610f8ded4c84fb010f4736dd9f3b4`，`fides_gpu_cu_sha256`：`5c088631ee495c3206dd4bbd0d1327a147799da2baf291ed33be220e3affa52b`；`V37_WIDE_SMOKE_OK modes_ok=4/4 ctas=80/40/21/80 freeGiB=7.07`；`F4BaselineDcpCheck` 对 `v36 DCP` 校验 `99.13 s`（`pins=2.93s, pip_shards=2.45s, unrouted_scan=1.02s, unroutedPins=0, sharedPipNodes=0`）。
- **全流程实测与对照根因（`2026-11-24T15:49:12 -> 16:09:57`，GPU 2 `GPU-4c86a339-05a7-65da-76e1-98f51f23d1f7`，`status="passed", stage="complete"`）**：
  - **Route B 实测兑现**：
    - `F4_V37_IDMAP_CACHE loaded=true n=92497168 runs=276420 time_s=0.930`（较 `v36 ~15 s` 省 **`~14 s`**）。
    - **`direct_finalize = 0.20 s`**（`198,437,106 ns`，较 `v36 80.47 s` 省 **`-80.27 s`**）；`F4_V37_ASSIGN_NODES conns=4909155 time_s=2.33`；`F4_V37_FIX_ROUTES fixed=115597 parallel=true time_s=8.29`；物化 `17.44 s`（`seq_fix=0`）；`F4_V37_SETPIPS 15.02 s`、`CHECKPIPS 13.26 s`、`STATS_WL 6.88 s`、`Write DCP 14.217 s`；整个搜索后阶段（`indirect_flood_bypassed_or_drained -> dcp_legality`）从 **`150.58 s` 降至 `113.79 s`（净省 `-36.79 s`）**。
  - **搜索阶段对照（`69` 轮收敛至 `over=0`，`sum_cpp = 883.81 s` vs `v36 810.43 s`，`+73.38 s`）与两项根因**：
    1. **宿主机外部任务并发争抢 CPU/NUMA 带宽**：运行期 GPU 1/3 存在外部作业；R3 CPU 吞吐由 `v36` `31.35M pops/s` 跌至 `17.10M pops/s`（`-45%`），导致算法完全一致的 `R3–R5` CPU Pass 1 由 `140.40 s` 升至 **`174.24 s`（`+33.84 s`）**，且 `Read DCP`（`35.80 -> 39.86 s`）、`dcp_legality Read`（`79.28 -> 86.01 s`）及 `SETPIPS/CHECKPIPS`（`15.42 -> 28.28 s`）同步偏慢。
    2. **`Tier-B1 Wide` + `P3 >= 16` 在单卡上呈负收益**：`173` 次 `8M` 溢出依次以 `40×16M` / `21×32M` 重试仅解出 **`80 / 173`（`46.2%`）**，其余 `93` 次耗尽预算却未回落 35 核 CPU（`fb=0`），外加 `R32+`（`>=16`）低占空比 GPU 启动占 `33.16 s`，使 GPU 总耗时由 `v36 355.06 s` 升至 **`514.79 s`（`+159.73 s`）**，总轮次由 `62` 增至 `69` 轮。结论：单卡下应保留 Route B，而回退 `Tier-B1 Wide` 与 `P3>=16`（恢复 `8M -> 35 核 CPU HOT` 快速回退，或直接进入多卡 4×V100 Route A+C）。
  - **汇总对比（vs `v36`）**：
    - `Total wirelength: 19,043,165`、`Nodes popped: 24,203,612,563`
    - `Route design: 1,041.38 s`（`Initialization 25.62 s` + `Routing 1,015.76 s`）、`route_wall_ns: 1,140.99 s`
    - **`end_to_end_with_preparation_seconds: 1,243.55 s`（`20m 44s`）**（较 `v36` `1,258.36 s` 缩短 **`-14.81 s`**，含 `resident_preflight 0.075 s`、`direct_finalize 0.20 s`、`Write DCP 14.22 s`、`dcp_legality 96.97 s`（墙钟 `102.19 s`，其中 `pins=2.81s, pip_shards=1.88s, unrouted_scan=0.68s`））
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31164943`**，`dcp_sha256`：`dc406c511c155cc1d7e01d30c2698df778dcc8cfa24316fe784c3cd4d936a366`。

### 2.17 `theia50per-ppt-hybrid-v37_1`（Route B + 恢复 `Tier-B1 8M` / `Pass 3 >= 160`，62 轮收敛、单卡 `Route design 978.16 s` / E2E `1,189.51 s` 新高）
- **改动文件**：`prepare_v37_1.py`、`deploy_v37_1.py`、`launch_v37_1.py`、`fides_cpu.cpp`（在 `v37` 基础上仅做两处精准回归：关闭 `Tier-B1 Wide 16M/32M` 重试令 `8M` `FG_STATUS_HASH_OVERFLOW` 直退 35 核 CPU HOT；`Pass 3` 阈值恢复 `160u`）。复用 `v37` 校验通过的 `rrg.idmap.bin` 与全部 Route B Java 补丁。
- **编译结果**：`-Wall -Wextra -Werror` 与 `nvcc -O3 -arch=sm_70` 四日志全 `0 B`；`cpp_sha256`：`df63e2f905e64d95a74653fa18717d2ec24dad35783ab37236d00df4fb337909`，`cuda_sha256`：`976dce9035581ed10c224225cfa1e253a8d06d1197f3512006bb40b52f1c9b55`，`fides_gpu_cu_sha256`：`5c088631ee495c3206dd4bbd0d1327a147799da2baf291ed33be220e3affa52b`。
- **全流程实测与对照（`2026-11-24T16:22:08 -> 16:41:59`，GPU 1 `GPU-81b72957-767e-40f3-b287-f427f1ad6528`，`status="passed", stage="complete"`）**：
  - **搜索段对比（vs `v37` / `v36`）**：
    - `R1–R2`：`151.02 s`（GPU `93.25 s`, `1,889` waves, `over=315,221`），与 `v36`（`150.71 s`）基本一致。
    - `R3–R5`：`188.63 s`（`69.83 / 61.35 / 57.45 s`, `over=20,732`），较 `v37`（`208.77 s`）提速 `-20.14 s`（仍受约 1–2 核后台 python 轻微争抢，较完全独占的 `v36 173.81 s` 慢 `+14.82 s`）。
    - `R6–R15`：**`263.90 s`**（`gpu_s 124.08 s`, `ov=90, wide=0, fb=90`），显著快于 `v37`（`295.15 s` / `gpu 213.78 s`, `-31.25 s`）和 `v36`（`279.50 s`, `-15.60 s`）。
    - `R16–R31`：`165.37 s`（`gpu_s 107.82 s`, `ov=49, wide=0, fb=49`）vs `v37` `160.86 s`（`gpu 140.92 s`）。
    - `R32+`（31 轮 100% CPU）：**`55.65 s`**（`gpu_s 0.00 s`）vs `v37` `69.83 s`（`gpu_s 33.16 s`, `-14.18 s`）。
    - **全轮搜索汇总**：**62 轮收敛（`over=0`）**，**`sum_cpp = 824.57 s`**（`p1=394.91 s, p2=83.33 s, p3=344.98 s`；GPU `358.79 s` / `16.71B` pops / `2,035` waves / `ov=139, wide=0, fb=139`），GPU 耗时较 `v37`（`514.79 s`）直降 **`-156.00 s`**，搜索耗时降 `-59.24 s`。
  - **后处理与合法性明细**：
    - `F4_V37_IDMAP_CACHE loaded=true n=92497168 runs=276420 time_s=0.864`
    - `V37_MATERIALIZE_VERIFY_OK checked=20`、`FIDES_NATIVE_CPU v37 materialized=4909155 overused=0 seq_fix=0 time_s=16.16`
    - `F4_V37_ASSIGN_NODES conns=4909155 time_s=1.96`、`F4_V37_FIX_ROUTES fixed=116064 parallel=true time_s=12.15`、`direct_finalize.wall_ns = 0.18 s`（`178,868,580 ns`，较 `v36 80.47 s` 省 **`-80.29 s`**）
    - `F4_V37_SETPIPS 12.02 s`、`F4_V37_CHECKPIPS 7.16 s`（`INFO: No PIP overlaps`）、`F4_V37_STATS_WL 10.89 s`、`Write DCP 13.217 s`；整个搜索后阶段（`indirect_flood_bypassed_or_drained -> dcp_legality`）仅 **`99.56 s`**（较 `v36 150.58 s` 省 **`-51.02 s`**）。
  - **汇总对比（vs `v36` / `v37`）**：
    - `Total wirelength: 19,040,007`、`Nodes popped: 24,135,856,676`
    - **`Route design: 978.16 s`**（`Initialization 25.22 s` + `Routing 952.95 s`，单卡首次破千秒，较 `v36 1,032.72 s` 快 **`-54.56 s`**，较 `v37 1,041.38 s` 快 **`-63.22 s`**）
    - **`route_wall_ns: 1,088.88 s`**（`18m 09s`，较 `v36 -42.78 s`、较 `v37 -52.11 s`）
    - **`end_to_end_with_preparation_seconds: 1,189.51 s`（`19m 50s`）**（首次破 20 分钟，较 `v36 1,258.36 s` 快 **`-68.85 s`**、较 `v37 1,243.55 s` 快 **`-54.04 s`**）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31172492`**（`read=87.79s, pins=2.98s, pip_shards=3.16s, unrouted_scan=0.79s`，`[No GC] *Total* 87.643s`），`dcp_sha256`：`dc934f516447c3bed013fa76b8460c103a65415210eac63d1959c41d1274452d`。

### 2.18 `theia50per-ppt-hybrid-v38`（Route A+C：4×V100 320-SM 按网 LPT 负载均衡分片 + `v37.1` Route B，72 轮全收敛、E2E `1,130.99 s` 新低）
- **改动文件**：`prepare_v38.py`、`v38_patch.py`、`deploy_v38.py`、`launch_v38.py`、`record_rounds_v38.py`、`fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`F2Prewarm.java`、`worker.py`。
- **核心改动**：
  1. **4×V100-PCIE-32GB 进程内多卡并行引擎（`FG_MAX_GPUS=4`）**：`fides_gpu.cu` 改为 `GS_ARR[4]` + 4 条持久化且自动按 PCIe 绑定对应 NUMA 节点的 GPU 驱动线程（`init_s = 4.38 s` 并行拷贝 `3.87 GiB` 图结构到 4 卡）；每卡配置 `1,024 Tier-A warps + 80 Tier-B1 CTAs`（合计 **`4,096 warps / 320 CTAs`**，总占用 `~25.3 GiB/卡`，每卡预留 `5.73–6.03 GiB`）。
  2. **跨卡状态一致性广播 + LPT 按 Net 负载均衡分片**：`fides_cpu.cpp` 通过 `fidesGpuApplyOccDeltasMulti` / `ApplyHistDeltas` / `UpdatePcc` / `UploadMegaScope` / `ApplyMegaPatches` 保持 4 卡拥塞与大网表严格同步；每波次先按 Net 累加权重 `max(500, lastPops)` 降序，再以贪心 LPT（优先均衡估计 pops、其次均衡查询数）分派至 4 卡独立 pinned 波次缓冲，最后经 `fidesGpuRunParallelDevs` 4 卡并发执行（R1–R2 `tw=12288`、P2 `tw=2048`、P3 `tw=1024`，保留 `8M -> 35 核 CPU HOT` 快速回退与 `P3 >= 160`）。
  3. **多卡可见性打通**：`worker.py` 新增 `v38_multi_gpu_visible(uuid)` 传入全部可用 V100（主卡 `GPU 1` 居首供 `F2Prewarm` 与 `F4DirectGpu` 使用）；`F2Prewarm.java` 将 `deviceCount() != 1` 放宽为 `< 1`。
- **编译与冒烟**：`-Wall -Wextra -Werror` 与 `nvcc -O3 -arch=sm_70` 四日志全 `0 B`；`worker_sha256`：`8023e5e9c5b19be860e70ca8ca8a66d91a71c057552d2acefe11e5f115be5f4a`，`cpp_sha256`：`c4c3584198d4e697556e1a8277267f8de1e03d211522d463c9066f60c7cee22c`，`cuda_sha256`：`7d7f7559af149a6077da4f63eb4ab366db9565251fe316fd6a72ec78a0bfaec5`，`fides_gpu_cu_sha256`：`78fb6b58cbe8bc0793abcfc4b687d3a22b88e8aecc69f995ea2d924bceeac38a`；4 卡合成冒烟测试 `V38_MULTI_GPU_SMOKE_PASS numGpus=4`（`init_s=0.98 s`）一次通过。
- **全流程实测与瓶颈定位（`2026-11-24T17:09:31 -> 17:28:23`，4×V100 均 `0%` 空闲启动，`status="passed", stage="complete"`）**：
  - **4 卡并行加速实测（vs `v37.1`）**：
    - `R1–R2`：**`125.07 s`**（R1 `60.37 s` vs `86.58 s`；GPU `93.25 -> 45.51 s`，提速 **`2.05×` / `-25.95 s`**）。
    - `R3–R5`：**`157.80 s`**（vs `188.63 s`，**`-30.83 s`**；P2+P3 GPU `33.64 -> 12.13 s`，提速 **`2.77×`**）。
    - `R6–R15`：**`248.90 s`**（vs `263.90 s`，**`-15.00 s`**；GPU `124.08 -> 92.90 s`，其中 R6 `42.59 -> 25.41 s`）。
    - `R16–R31`：`195.85 s`（vs `165.37 s`，`gpu_s 107.82 -> 80.87 s` 虽降 `-26.95 s`，但 `ov/fb` 由 `49` 升至 `94` 拉高 CPU 复算）+ `R32+`（41 轮 CPU）：`62.43 s`。
    - **全轮搜索汇总**：**72 轮全收敛（`over=0`）**，**`v37_gpu_ns = 231.44 s`**（较 `v37.1 358.80 s` 降 **`-127.36 s` / `-35.5%`**，`17.26B` GPU pops / `1,826` waves），**`sum_cpp = 790.05 s`**（`p1=334.42 s, p2=44.78 s, p3=409.57 s`，`ov=232, fb=232`）。
  - **后处理与合法性明细**：
    - `F4_V37_IDMAP_CACHE 1.035 s`、`V37_MATERIALIZE_VERIFY_OK checked=20`、`materialize 16.23 s`、`assignNodes 1.56 s`、`fixRoutes 8.66 s`、`direct_finalize 0.176 s`（`176,295,868 ns`）、`SETPIPS 12.02 s`、`CHECKPIPS 7.12 s`（`No PIP overlaps`）、`STATS_WL 7.28 s`、`Write DCP 12.540 s`；搜索后阶段（`indirect_flood -> dcp_legality`）再降至 **`88.24 s`**。
  - **汇总对比（vs `v37.1` / `v36` / `v34`）**：
    - `Total wirelength: 19,122,135`、`Nodes popped: 25,148,714,172`
    - **`Route design: 937.71 s`**（`Initialization 21.16 s + Routing 916.55 s`，较 `v37.1 -40.45 s`、`v36 -95.01 s`、`v34 -184.65 s`）
    - **`route_wall_ns: 1,037.61 s`（`17m 18s`）**（较 `v37.1 -51.27 s`、`v36 -94.05 s`）
    - **`end_to_end_with_preparation_seconds: 1,130.99 s`（`18m 51s`）**（较 `v37.1 -58.52 s`、`v36 -127.37 s`、`v34 -219.43 s`）
    - **`dcp_legality.log`**：**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31193415`**（`read=82.68s, pins=2.90s, pip_shards=2.43s, unrouted_scan=0.64s`，`[No GC] *Total* 82.545s`），`dcp_sha256`：`931f3350c19e9a2b9d95cc9df8cbb08e1dd305f4731e42fd731ebd7d3036257e`。
  - **下一步演进洞察**：`v38` 中 GPU 并行仅占 E2E 20.5%（`231.4 s`），余下 **~900 s（79.5%）** 全在 Host CPU 侧：① `R1–R2` Host 建波+冲突校验占 `79.56 s`（`63.6%`）；② `Pass 3` `232` 次 `8M` 溢出先在 GPU 跑满后再串行/小批回 CPU 复算（CPU 侧耗 `>200 s`）并多跑 10 轮；③ `R3–R5 Pass 1` `143.92 s` 与 RapidWright DCP 读写/初始化/legality 读盘共 `~341 s`。

### 2.19 `theia50per-ppt-hybrid-v39`（`P1` Pass 3 溢出直达 + `P2` 并行 `Phase 4A/4C` 与并发 `prePick`：`73` 轮全收敛、`dcp_legality PASS`，并精确定位 `meta_s=53.07 s` 与 GPU 2 外部争抢）
- **改动文件**：`scratch/v39/` 下 `fides_cpu.cpp`、`prepare_v39.py`、`deploy_v39.py`、`launch_v39.py`、`record_rounds_v39.py`、`v39_patch.py`（`fides_gpu.cu` / `fides_gpu.h` 未改）。
- **核心改动**：
  1. **`P1`（Pass 3 溢出分流与 `tw=512`）**：新增 `G.connGpuOv` 标记 Pass 3 `FG_STATUS_HASH_OVERFLOW`，在 `relaxedQueue` 派发前将 `connGpuOv != 0 || lastPops > 3_500_000` 转入 `megaQueue`（35 核 CPU HOT），并将 Pass 3 `targetWave` 恢复为 `512u`。
  2. **`P2`（Host 关键路径并行化与并发预选）**：
     - 将 `waveCommitStamp` + `waveCommitNet` 合并为单 `std::vector<U64> waveCommitKey`（同内存占用下 DRAM 访问减半），并将 `Phase 4A` 端点校验与原子 CAS 冲突认领改为 35 核并行；
     - 将 `Phase 4B` 输出切分为 32 组无节点交集且按 cache-line（`(id >> 4) & 31`）隔离的分片，使 `Phase 4C`（`G.dyn.occ` 与 `gpuDirtyOccStamp` 更新）在 32 核并行执行；
     - 在 `Phase 3`（4 卡 GPU 核函数执行窗口）并发启动 `prePickThread` 执行下一波 `pickAndLpt`（`readyHeap` + LPT 分片），并用 `F4_V39_HOST` 打印每轮 Host 细分计时。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V39_MULTI_GPU_SMOKE_PASS numGpus=4 init_s=0.91`（4 卡可用显存 `9.42–9.91 GiB`）；`worker_sha256`：`8023e5e9c5b19be860e70ca8ca8a66d91a71c057552d2acefe11e5f115be5f4a`，`cpp_sha256`：`6ea8ba5bbe464f5367bf8936adecf05c30984549a56d8a4d26d9be97fca0174b`，`cuda_sha256`：`909b33c5e4f9b5fc97cc6d20a357782f94f7fb0a03b69f28bf65c30e9173fee3`，`fides_gpu_cu_sha256`：`78fb6b58cbe8bc0793abcfc4b687d3a22b88e8aecc69f995ea2d924bceeac38a`。
- **全流程实测与诊断（`2026-11-24T19:16:36 -> 19:38:51`，`status="passed", stage="complete"`）**：
  - **运行环境记录**：启动及收工期间 `gpu02` GPU 2 常驻外部 `python` 进程（PID `171670`，`496 MiB` / `31–32%` 利用率，低于 `v38_multi_gpu_visible` `<=50%` 阈值被纳入 4 卡同步波次屏障；GPU 0 另有 `308 MiB / 0%`）。
  - **多卡初始化**：`slot 0 (GPU 3)` `init_s=1.15s free=6.03GiB`、`slot 1 (GPU 0)` `0.83s 5.73GiB`、`slot 2 (GPU 1)` `0.85s 6.03GiB`、`slot 3 (GPU 2)` `1.16s 5.55GiB`（每卡 `1,024 Tier-A warps + 80 Tier-B1 CTAs`）。
  - **`F4_V39_HOST` Host 拆解实测**：
    - 全程 73 轮总计：`pick_s=0.30s`（几乎被 `Phase 3` 完全隐藏）、`pack_s=2.03s`、`p4a_s=1.89s`、`p4b_s=4.11s`、`p4c_s=1.44s`，而 **`meta_s=53.69s`（其中 `R1=23.00s, R2=30.07s` 合计 `53.07s`，占 `R1–R2` Host `79%`）**；
    - `R1–R2`：`cpp=134.44s`（`R1 63.44s, R2 71.00s`），其中 **`host` 降至 `67.15 s`**（较 `v38` `79.56 s` 降 `-12.41 s`），但 **`gpu_s` 涨至 `67.29 s`**（`R1 31.89s, R2 35.40s` vs `v38 45.51s`，`+21.78 s`）。
  - **轮次与全流程对比 `v38`**：
    - `R3–R5`：`168.63 s`（`gpu=22.81s, p1=144.37s, p2=14.32s, p3=9.67s`）；
    - `R6–R73`：`p2_n=47157 (50.90s)`、`p3_n=15783 (475.33s)`、`mega_n=43 (76.69s)`、`gpu_ov/fb = 167`（较 `v38 232` 降 `-65`，但 `R6–73 gpu_s` 由 `173.77s` 升至 `254.59s`）；
    - 全轮汇总：`73` 轮全收敛（`over=0`），`v37_gpu_ns = 344.69 s`（`1,908` waves / `17.19B` GPU pops / `6,956,145` gpu commits），`sum_cpp = 991.94 s`（`p1=363.24s, p2=65.41s, p3=485.03s, mega=76.69s`）；
    - 后处理与端到端：`idmap=0.948s`、`materialize=16.80s`（`V37_MATERIALIZE_VERIFY_OK checked=20`）、`assignNodes=1.88s`、`fixRoutes=8.44s`、`SETPIPS=11.48s`、`CHECKPIPS=9.09s`、`STATS_WL=9.11s`、`WL=19,159,613`、`Nodes popped=25,164,085,935`；
    - **`Route design = 1,140.91 s`**（`Initialization 22.19s + Routing 1,118.73s`），**`route_wall = 1,238.63 s`**，**`E2E = 1,335.10 s`**（`legality_wall = 94.84s`），`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31193244`（`read=84.21s, pins=3.46s, pip_shards=1.65s, unrouted_scan=0.97s`），`dcp_sha256`：`a8c6d554a94a77ef022a43ebfbcc9727198cd994045a3638c81c542c911782f4`。
- **关键根因结论**：
  1. **外部 GPU 2 争抢同步拖慢全波**：同份 `fides_gpu.cu` 内核在相近波次下，`gpu_s` 由 `v38 231.44 s` 升至 `344.69 s`（`+113.25 s`），主因 `32%` 利用率的外来进程在 GPU 2 拖慢 `fidesGpuRunParallelDevs` 波次同步屏障；多卡筛选门限（`<= 50%`）过宽。
  2. **`P2` 验证有效且精准定位 `meta_s = 53.07 s`**：`pick_s` 已被隐藏至 `0.30 s`、`p4a_s` 压至 `1.89 s`、`p4c_s` 压至 `1.44 s`，真正剩余 `79%` Host CPU 开销集中在单线程 `prepareNetSameDescMeta`（大网 hash 表读取拷贝及 `fidesGpuUploadMegaScope` 多卡同步上传）。
  3. **`P1` 串联 `megaQueue` + `tw=512` 负收益**：`tw=512` 均摊到 4 卡 `320` CTAs 造成长尾利用率不足，且 43 条大网被延迟到 Pass 3 结束后串行执行（`mega_s = 76.69 s`），反而慢于 `v38` `tw=1024` 波内及时 fallback 给 35 核 CPU。

### 2.20 `theia50per-ppt-hybrid-v40`（`3×V100` 净卡屏蔽 GPU 2 + 批量并发 Mega 上传 + 回调 `P1` 串联：`sum_cpp 724.72 s` / `R1–R2 111.65 s` 创纪录，但 `R64–R99` `over=2` 乒乓致 `DCP legality` 未过）
- **改动文件**：`scratch/v40/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`v40_patch.py`、`prepare_v40.py`、`deploy_v40.py`、`launch_v40.py`、`record_rounds_v40.py`。
- **核心改动**：
  1. **消解 `megaScope` 上传阻塞**：`MAX_MEGA_SCOPES` 由 `384 -> 1024`；`NetScopeData` 增加 `O(1)` 槽位缓存 `megaSlot`；`noteMegaSlotDirty` 脏槽门槛由 `8192u -> 65536u`；`Mode 2` 扩至 `sc.liveCount <= 8192u`；`fides_gpu.cu` 取消 per-slot `cudaMallocHost` 改用单份复用 pinned 暂存 `h_megaStageKeysPinned/ValsPinned + megaStageEvent` 与批量接口 `fidesGpuUploadMegaScopesBatch`；Host 端 `ScopeSlot` AoS->SoA 改用 35 核分片并行转换，`R2+ shouldRouteLive` 并行预检。
  2. **清理 `v39` 负收益项并收紧外部 GPU 隔离**：回调 Pass 3 `targetWave`（`>=3` 卡 `1024u`）与 `connGpuOv` 串行 `megaQueue`；将 `v38_multi_gpu_visible` 外卡过滤门限由 `int(ut) <= 50` 收紧至 `<= 10`（自动排除 `31–32%` 占用的 GPU 2，以 **3 张 0% 空闲 V100** 运行，`Pass 1 tw = min(12288, gpuCount * 3072) = 9216u`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V40_MULTI_GPU_SMOKE_PASS numGpus=4 init_s=0.92 batch_ok=1`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`4916221475b6b31967d41ee73c13d83beb74753f8b3a3f9030791ce9585bca81`，`cuda_sha256`：`c9df6edb3a33a4a690ccf002505a1f08b7b70615ea06550cdb172740696c6cbc`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程实测与问题定位（`2026-11-24T19:52:06 -> 20:10:51`，`status="failed", error="DCP legality"`）**：
  - **多卡初始化**：`slot 0 (GPU 3)` `init_s=1.76s free=6.03GiB`、`slot 1 (GPU 0)` `1.74s 5.73GiB`、`slot 2 (GPU 1)` `1.68s 6.03GiB`（仅启用 `3` 张 `0%` V100，成功绕开 GPU 2）。
  - **搜索速度创历史最优**：
    - **`R1–R2`：`111.65 s`**（`R1=54.71s, R2=56.94s`，其中 `gpu=51.93s`、`host=59.72s`，**比 4 卡净跑 `v38 125.07 s` 还快 `-13.42 s`**，`R1 over=800,594`）；
    - **`sum_cpp = 724.72 s`**（**较 `v38 790.05 s` 降 `-65.33 s`**，其中 `p1=382.55s, p2=45.48s, p3=294.95s, mega=0.00s`；`sum_gpu=247.09s` / `2,021` waves / `ov=fb=211`）；
    - `R3–R5 = 180.88 s`（`gpu=14.80s, p1=164.35s`）；`R54` 即降至 `over=10`、`R64` 降至 `over=2`。
  - **`F4_V39_HOST` 新揭露真相**：
    - 全程 `mega_up = 448` 次批量上传累计 **`mega_pack = 0.23 s` + `mega_gpu = 0.66 s` = 仅 `0.89 s`**（`R1` `0.05+0.22=0.27 s`，`R2` `0.01+0.06=0.07 s`），`pick=0.18s`、`pack=2.47s`、`p4a=1.67s`、`p4b=3.86s`、`p4c=1.39s`；
    - 但 **`meta_s` 仍高达 `44.28 s`（`R1=19.90 s, R2=23.45 s`）**——说明 `43 s` 的剩余 `meta_s` 并不在 `pendingUploads`，而在 `Phase 2A-meta` 同段内的非上传逻辑（`std::sort(sc.megaDirtyIdx)` / `waveActIdx` 构造等，待下一版切分精确子计时确认）。
  - **收尾失败根因（`over=2 -> sharedPipNodes=2`）**：
    - 从 `R64` 起仅剩 `active=2..8`、`routed=1..4` 的少量连接在 `over=1..5` 之间持续乒乓震荡（每轮仅 `~0.01–0.08s`）直至 `RWRoute` `iter=99`（100 轮上限）退出；
    - 震荡主因：`runDynamicPass` 在尾盘极小集合（`active <= 8`）仍双核并行派发（如 `R91: active=2 routed=2 par=1 ser=1`），两连接同轮并行绕过彼此旧路径后互相踩踏，未能及时单核顺序更新 `occ` 打破对称；
    - `iter=99` 保留 `overused=2` 进入后处理，导致 `fixRoutes parallel=false time_s=59.87 s`（退回慢速串行且增 `+51 s`），`Route design = 926.52 s`（`Init 24.04s + Routing 902.48s`、`WL=19,008,205`），最终 `dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=2 pips=31105646`（`read=84.07s`）。

### 2.21 `theia50per-ppt-hybrid-v41`（`Phase 2A-meta` 子计时精确抓出 `sort_s = 41.97 s` 根因 + 验证尾轮不收敛真实机制）
- **改动文件**：`scratch/v41/` 下 `fides_cpu.cpp`、`prepare_v41.py`、`deploy_v41.py`、`launch_v41.py`、`record_rounds_v41.py`、`v41_patch.py`。
- **核心改动**：
  1. **`Phase 2A-meta` 无遗漏 5 段细粒度计时（`F4_V41_META`）**：将 `hostMetaS` 拆分为 `loop_s`（`prepareNetSameDescMeta` 除排序外循环）、`sort_s`（`sc.megaDirtyIdx` 的 `std::sort + std::unique` 及构建 `megaPatches`）、`upw_s`（`pendingUploads` 并行 pack + 批量 GPU 上传 + cache 刷新）、`live_s`（R2+ `piLive` 并行预检）、`build_s`（`waveActIdx` 等查询数组组装），并记录 `dirty_n` / `patch_n` / `w_tot` / `p_tot`。
  2. **尾盘单线程串行试探（`activeCount <= 64 && routeIter >= 35`）**：在 `runDynamicPass` 内对尾盘小规模活性集合切换至 `workerThreads == 0` 顺序执行并前置 `ensureScopeCapacitySingleThread`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V41_SMOKE_PASS numGpus=3 init_s=0.69 batch_ok=1`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`7d28cb5b2b9f6187270a72f67a9cd72f261b88f7027345342208008fd8ddfbb3`，`cuda_sha256`：`6f0ecbe68e6f4dfce043770b9895dcdc8f4b6930a0f5423d66d488a80d43a399`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程实测与诊断突破（`2026-11-24T20:39:45 -> 21:00:11`，GPU 0/1/3 空闲 `0%`，`status="failed", error="DCP legality"`）**：
  - **`F4_V41_META` 100% 锁定 Host `meta_s` 根因**：
    - **R1 (`meta_s = 19.70 s`)**：`loop_s=0.13s`、**`sort_s=18.96s`（`96.2%`）**、`upw_s=0.28s`、`live_s=0.00s`、`build_s=0.33s`；`dirty_n=213,572,960 -> patch_n=23,526,286`（`w_tot=3,013,166, p_tot=5,209,414`）；
    - **R2 (`meta_s = 23.15 s`)**：`loop_s=0.05s`、**`sort_s=22.69s`（`98.0%`）**、`upw_s=0.06s`、`live_s=0.13s`、`build_s=0.22s`；`dirty_n=240,357,309 -> patch_n=19,776,338`（`w_tot=981,727, p_tot=2,079,748`）；
    - **全 99 轮累计**：`meta_s=43.82s` 中 **`sort_s = 41.97 s`（`95.8%`）**、`dirty_n=457,988,362`、`patch_n=43,696,407`（重复推入 `10.5×`，却在单核上做 `std::sort + std::unique`），其余 `loop=0.18s, upw=0.93s, live=0.13s, build=0.55s` 合计仅 `1.79 s`。
  - **揭示尾轮不收敛与 P3 长尾双重根因**：
    1. **单纯改为 `ser` 顺序执行仍无法打破尾盘僵局**：`R57..R99` 已 100% 单线程（如 `R71..R99` 连续 29 轮均 `active=11..44, routed=4, skip=7..39, ser=4, over=4..8, pops=1.22M/round`，`R51..99` 共耗 `74.55 s`），最终 `R99` 留 4 个冲突节点（含 1 个 `IMUX` 与 `3` 个 `SDQ`）。根因不在并发，而在 **① `shouldRouteLive` 未先整体 rip-up**（4 组冲突中先走的一条切到另一条已占节点 `v`，导致旧节点 `occ` 降回 `1` 使同宿连接被 `skip`，留占原地并只交换过载位置）；**② Pass1 易连接先于 Pass3 难连接（`~600k pops`）抢占稀缺槽位**；**③ `R60+` 仍受限于 `origBox` 未放开跨箱绕线**。
    2. **`R5..R32` Pass 3 `gpu_ov=251` 回退拖慢 `+130–170 s`**：全 99 轮汇总 `sum_cpp=826.25s`（`p1=318.10s, p2=51.63s, p3=454.57s`；`sum_gpu=259.53s` / `1,957` waves）；尽管 `p1` 较 `v40`（`382.55s`）快 `-64.45s`，但 `R5..R32` 的 `251` 条 8M-hash 溢出条带（如 `R19` 仅 `ov=2` 即令 `p3_s=13.6s`、`gpu_s=5.56s`）回落 CPU 后单条耗数秒，将 `p3` 推至 `454.57 s`。
    - **汇总**：`Route design = 1,033.20 s`（`Init 24.96s + Routing 1,008.23s`，`fixRoutes parallel=false time_s=60.62s`）、`WL = 19,055,213`、`Nodes popped = 25,479,172,046`、`route_wall = 1,132.56 s`、`legality_wall = 91.75 s`、`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=4 pips=31129272`（`read=83.70s`），`dcp_sha256`：`50c22e0b032552b565c780446d5ce3a171bcf198abafc65c4aa6e89c37e45097`。

### 2.22 `theia50per-ppt-hybrid-v42`（35 核并行 `megaDirtyIdx` + 尾盘整簇 Rip-Up：`R1–R2 71.36 s` 新纪录 & `73` 轮全收敛 `dcp_legality PASS`）
- **改动文件**：`scratch/v42/` 下 `fides_cpu.cpp`、`prepare_v42.py`、`deploy_v42.py`、`launch_v42.py`、`record_rounds_v42.py`、`v42_patch.py`（`fides_gpu.cu` / `fides_gpu.h` 未改）。
- **核心改动**：
  1. **35 核 LPT 并行 `megaDirtyIdx` sort/unique/patch**：`prepareNetSameDescMeta` 仅登记去重后的 `PendingPatchNet{netId, slot, dirtySz, 0}`；随后按 `dirtySz` 降序、经 `EXEC.parallelFor` 35 核并行执行单网 `std::sort + std::unique` 与前缀和并行填充 `megaPatches`。
  2. **尾盘整簇 Rip-Up + 顺序重布求解器（`(routeIter>=45 && activeCount<=64) || (routeIter>=50 && activeCount<=96)`）**：跳过 `shouldRouteLive` 提前过滤与分 Pass，先全量 rip-up 全部 `activeCount` 条活性连接并备份旧路径，按路径最大 `hist` 与 `lastPops` 降序（`routeIter>=58` 奇偶扰动）逐条 `budget=0` 顺序重布，`routeIter>=55` 设 `forceUnbounded=true` 解除 `useBox`；`runDynamicPass` 恢复 `workerThreads == 0` 原始条件使 `R35–R44` 保持 35 核并行。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V42_SMOKE_PASS numGpus=3 init_s=0.69 batch_ok=1`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`c4e92d369f6ba4773ea073ef817996f940e71dd9e295bcbef8e06ad73e643e96`，`cuda_sha256`：`56ce829081482e42dee03a3eba49995713b8d6a79e3434a8860bde84722dc2a6`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程实测与诊断（`2026-11-24T21:13:52 -> 21:38:29`，`3×V100` 空闲 GPU 0/1/3，`status="passed", stage="complete"`）**：
  - **`R1–R2` 与 Host 开销双双刷新历史纪录**：
    - **`R1–R2 = 71.36 s`**（`R1 = 35.25s [gpu=24.75s, meta=2.31s, sort=1.53s]` + `R2 = 36.11s [gpu=28.04s, meta=2.75s, sort=2.28s]`），较 `v41 107.59 s` 降 **`-36.23 s`**，较 4 卡净跑 `v38 125.07 s` 降 **`-53.71 s`**；
    - 全 73 轮 Host 总计仅 `15.21 s`（`pick=0.21s, meta=5.81s [loop=0.17s, sort=3.93s, upw=0.96s, live=0.13s, build=0.61s], pack=2.44s, p4a=1.56s, p4b=3.96s, p4c=1.23s`），较 `v41 meta_s=43.82s` 实省 **`-38.01 s`**（`dirty_n=469,039,435 -> patch_n=44,361,292`）。
  - **尾盘整簇 Rip-Up 成功打破死锁并恢复并行后处理**：
    - **`R73` 收敛到 `over=0, excess=0`（`pops=2,737, cpp_s=0.01 s`）**，解锁 **`F4_V37_FIX_ROUTES fixed=115546 parallel=true time_s=8.32s`**（较 `v41` 串行 `60.62 s` 省 **`-52.30 s`**）；
    - 后处理明细：`materialize=16.81s`（`V37_MATERIALIZE_VERIFY_OK checked=20`）、`assignNodes=1.83s`、`SETPIPS=9.52s`、`CHECKPIPS=12.33s`（`INFO: No PIP overlaps`）、`STATS_WL=8.21s`、`Write DCP=13.55s`、`WL=19,027,342`；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31125233`**（`read=78.72s, pins=3.13s, pip_shards=2.64s, unrouted_scan=0.68s`），`dcp_sha256`：`170aec7b7890f75953b8255c14d30e715cfd4519942b7c42242e196e453cd455`。
  - **汇总与唯一负收益项排查**：
    - `sum_cpp = 1,134.89 s`（`R1-2: 71.36s, R3-5: 166.13s, R6-55: 566.39s, R56-73: 331.01s`；`p1=309.56s, p2=47.99s, p3=775.55s`；`sum_gpu=260.69s / 2,017 waves`），`Route design = 1,288.66 s`（`Init 25.14s + Routing 1,263.52s`），`route_wall ≈ 1,388.13 s`，`E2E ≈ 1,477.62 s`（`24m 38s`）。
    - **`R56–R73` 耗 `331.01 s` 根因精确锁定**：`routeIter >= 55` 设为 `forceUnbounded = true`（`q.useBox = false` 全片无界搜索 `92.5M` 节点）使单条受阻连接每轮扩展 `10M–28M` 节点（`10.8s–57.4s/轮`）；且因未同步 rip-up 该冲突点周边仅 `±2` tile 内 `occ=1` 的无害旁路连接，`over=1` 在 `R62–R72` 空等旁路空位耗掉 `11` 轮。

### 2.23 `theia50per-ppt-hybrid-v43`（有界扩框 + `±2` tile 邻域 Rip-Up：**58 轮全收敛 / `Route design 855.46 s` / `E2E 1,059.83 s` 全库新纪录 & `dcp_legality PASS`**）
- **改动文件**：`scratch/v43/` 下 `fides_cpu.cpp`、`prepare_v43.py`、`deploy_v43.py`、`launch_v43.py`、`record_rounds_v43.py`、`v43_patch.py`（`fides_gpu.cu` / `fides_gpu.h` 未改）。
- **核心改动**：
  1. **有界阶梯扩框替代整片洪泛**：`runProbe` 将 `bool forceUnbounded` 改为 `int padRelax = 0`（启用 `useOrigBox` 时扩展至 `origBox ± padRelax / ± 2*padRelax`）；尾盘重布优先 `padRelax=24, budget=2M`，失败再退 `padRelax=48, budget=4M`，彻底杜绝 `28M` 节点整片扩散。
  2. **1-Hop 空间邻域协同 Rip-Up（`routeIter >= 56 && activeCount <= 16`）**：35 核并行扫描穿过 `G.overusedList` `±2` tile（Y 向 `±4`）邻域的灵活旁路连接（`netSize <= 64, lastPops <= 50000`），最多选取最近 `48` 条与原始冲突集一并 rip-up，**严格先重布冲突集、后重布邻路（`padRelax=12`）**，从结构上清空被旁路锁死的局部通道。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V43_SMOKE_PASS numGpus=3 init_s=0.72 batch_ok=1`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`a8f4d3cb1e940074b54debdc5cc87512d7a4df678e46e0d5bff11b083e53a2b4`，`cuda_sha256`：`afa39c85a1e2cc43b44c8d3d7e9f6d67ed301dbed3b94c7d2a31158b7e28a72c`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程实测（`2026-11-24T21:46:05.88 -> 22:03:45.71`，`3×V100` 空闲 GPU 0/1/3，`status="passed", stage="complete"`）**：
  - **轮次与尾盘大幅刷新历史最好水平**：
    - **仅 `58` 轮收敛至 `over=0, excess=0`**（较 `v36/v37.1 62` 轮、`v38 72` 轮、`v42 73` 轮再降；`R56` 吞入 `4+48` 邻路 `0.11s` -> `R57 ov=1` -> **`R58` 吞入 `3+48` 邻路 `pops=21,722, cpp_s=0.05 s` 直清至 `over=0`**）；
    - **`R56+` 仅耗 `12.80 s`（较 `v42 331.01 s` 直砍 `-318.21 s`!!）**；
    - 全轮搜索：**`sum_cpp = 708.87 s`**（`R1-2: 71.40 s, R3-5: 165.21 s, R6-55: 459.46 s, R56-58: 12.80 s`；`p1=282.71 s, p2=45.92 s, p3=378.75 s`；`sum_gpu = 259.68 s / 1,952 waves`），较 `v38`（`790.05 s`）快 **`-81.18 s`**，较 `v42`（`1,134.89 s`）快 **`-426.02 s`**；
    - Host 全程合计 `15.69 s`（`pick=0.20 s, meta=5.70 s [loop=0.17s, sort=3.86s, upw=0.84s, live=0.15s, build=0.62s], pack=2.53 s, p4a=1.81 s, p4b=3.90 s, p4c=1.55 s`，`mega_up=400`, `dirty=462,204,001 -> patch=43,982,083`）。
  - **后处理、端到端与合法性验证**：
    - `materialize=15.89 s`（`V37_MATERIALIZE_VERIFY_OK checked=20`）、`assignNodes=1.77 s`、`F4_V37_FIX_ROUTES fixed=115679 parallel=true time_s=8.51 s`、`SETPIPS=11.65 s`、`CHECKPIPS=6.83 s`（`INFO: No PIP overlaps`）、`STATS_WL=7.28 s`、`Write DCP=12.78 s`（`Write EDIF & XDEF 12.03s + Write XDC 0.75s`）；
    - `Total wirelength = 19,042,130`、`Nodes popped = 24,033,095,431`；
    - **`Route design = 855.46 s`（`14m 15s`）**（`Initialization 25.63 s + Routing 829.83 s`，较此前合法记录 `v38 937.71 s` 快 **`-82.25 s`**，较 `v34 1,122.36 s` 快 **`-266.90 s`**）；
    - **`route_wall ≈ 958.55 s`（`15m 59s`）**（`21:46:05.88 -> 22:02:04.43`，较 `v38 1,037.61 s` 快 **`-79.06 s`**）；
    - **`E2E ≈ 1,059.83 s`（`17m 40s`）**（`21:46:05.88 -> 22:03:45.71`，较 `v38 1,130.99 s` 快 **`-71.16 s`**）；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31143523`**（`read=90.45s, pins=2.95s, pip_shards=2.52s, unrouted_scan=0.92s`），`dcp_sha256`：`1ad32de1870d603592c4b683ccef9860b8d8dd2d7093e0149a6b44677641ff9a`。

### 2.24 `theia50per-ppt-hybrid-v44`（`R3` GPU Tier-A + `Pass 3` `<384u`/`>=2.5M` 预直走 + `R54+` 紧尾：**`R53–R59` 压至 `1.48 s`，验证并剥离两项负收益，为 `v45` 指明精确优化组合**）
- **改动文件**：`scratch/v44/` 下 `fides_cpu.cpp`、`prepare_v44.py`、`deploy_v44.py`、`launch_v44.py`、`record_rounds_v44.py`、`v44_patch.py`（`fides_gpu.cu` / `fides_gpu.h` 未改）。
- **核心改动**：
  1. **测试 `R3` GPU Tier-A 分流**：将 `Pass 1` GPU 分界放宽至 `routeIter <= 3`（`R3` 采用 `targetWave = 4608` / 3 卡）。
  2. **测试 `Pass 3` 阈值与大项预分流**：将 `Pass 3` 进 GPU 门槛由 `160u` 提至 `384u`（`targetWave = 1536u`），并将 `lastPops >= 2,500,000` 在 GPU 前剔除转入波后 CPU `runDynamicPass`。
  3. **收紧尾盘探测预算与提前邻域 Rip-Up**：1-hop 邻域 rip-up 提前至 `routeIter >= 54 && activeCount <= 24`（原 `56 && 16`），且 `routeIter < 60` 时将 `runProbe` 尾盘两级预算从 `2M -> 4M` 收紧至 **`600k -> 1.2M`**。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V44_SMOKE_PASS numGpus=3 init_s=0.68 batch_ok=1 ta0=1024 tb0=80`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`8f90b0aa6e519918db38ba87ffef75866ed7b134282fda60b8e1890bd4bc153a`，`cuda_sha256`：`c3026ca5d78d35c5726fd58fe944fca0fc2ee5db8896bc05682e3e47d836ece2`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程实测与对照定位（`2026-11-24T22:24:55.16 -> 22:43:05.45`，`3×V100` 空闲 GPU 0/1/3，`status="passed", stage="complete"`）**：
  - **两项正收益命中验证**：
    - **尾盘紧预算 + `R54` 邻域拆除大获全胜**：**`R53–R59`（7 轮）总计仅 `1.48 s`**（`R55=0.24s, R56=0.08s, R57=0.03s, R58=0.03s, R59=0.02s -> over=0`，较 `v43` `R53–R58` `26.62 s` 净省 **`-25.14 s`**！）；
    - **`Pass 3` 中小批量（`< 256u`）直走 CPU 显著提速**：`R20–R22` 降至 `5.34 / 3.86 / 7.89 s`（vs `v43` `13.25 / 10.29 / 9.82 s`），消除了不足 `240` CTA 时的 GPU 多波次同步与 `8M` 溢出双倍功；实测交叉点精确落在 **`~256u`**（`R17/R26 >= 256u` 且低 `ov` 时 GPU 仍快于 CPU，故 `v45` 门槛宜定为 `256u`）。
  - **两项负收益精确定位（合计拖慢约 `+60 s`，`v45` 立即剔除）**：
    - **`R3` GPU Tier-A 净亏**：`R3` `cpp_s` 虽从 `70.34 s` 降至 `58.12 s`（`gpu_waves=1,016, gpu_s=49.76 s`），但 `over` 由 `110,161` 升至 `116,838`（`+6.1%`），级联推高 `R4`（`56.55 -> 71.30 s, over=43,882`）及 `R5–R11 Pass 1` 约 `+40 s`；证明 3 卡下 `Pass 1` 仍应严格回归 **`routeIter <= 2`**；
    - **`lastPops >= 2.5M` 预直走 CPU 门槛过低**：`Tier-B1 8M`（`6.29M` 去重节点）实际可稳定解出 `~8M–12M` pops 项，将 `>=2.5M` 提前剔除导致大量本可由 `240` CTA 并行秒解的重连接压回 CPU，拉高 `R7/R8 p3_s`（`R8`: `19.24 -> 31.31 s`）。
  - **汇总与合法性明细**：`59` 轮收敛至 `over=0`；`sum_cpp = 747.39 s`（`R1-2: 73.90s, R3-5: 177.65s, R6-52: 494.36s, R53-59: 1.48s`；`p1=334.58s, p2=44.98s, p3=366.39s`；`sum_gpu = 223.84s / 3,014 waves`, `gpu_ov=gpu_fb=96`；`host=19.75s` [meta `7.62s`, pack `3.42s`, p4a `2.03s`, p4b `4.52s`, p4c `1.90s`]）；`materialize=15.89s`（`V37_MATERIALIZE_VERIFY_OK checked=20`）、`assignNodes=1.83s`、`F4_V37_FIX_ROUTES fixed=115843 parallel=true 8.91s`、`SETPIPS=11.96s`、`CHECKPIPS=7.17s`、`STATS_WL=9.77s`、`WL=19,140,479`、`Nodes popped=25,123,265,770`；**`Route design = 891.78 s`**（`Init 21.93s + Routing 869.85s`）、`route_wall ≈ 996.03 s`、`E2E ≈ 1,090.29 s`（`18m 10s`）、**`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31195358`**（`read=84.09s, pins=2.95s, pip_shards=2.01s, unrouted_scan=0.89s`），`dcp_sha256`：`7a20137f2251dde27ba68d73001d0b9717d60967adb7349d742b8d90cfc911e0`。

### 2.25 `theia50per-ppt-hybrid-v45`（`Pass 3 >= 256u / tw=2048u` 验证 `-29.35 s`，`R60–R74` 尾盘死锁中止）
- **改动文件**：`scratch/v45/` 下 `fides_cpu.cpp`、`prepare_v45.py`、`deploy_v45.py`、`launch_v45.py`、`record_rounds_v45.py`、`v45_patch.py`。在 `v43` 基线上严格恢复 `routeIter <= 2` 与无 `2.5M` 预剔除，仅保留 `Pass 3 >= 256u / tw=2048u` 与 `v44` 尾盘紧预算（`R54+` 邻域拆除、`<60` 为 `600k/1.2M`、`>=60` 为 `2M/4M`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V45_SMOKE_PASS numGpus=3 init_s=0.71 batch_ok=1 ta0=1024 tb0=80`。
- **全流程观测与止损（`2026-11-24T22:49:28.52` 启动，`3×V100`，`R74` 人工清理中止）**：
  - **`R6–R52 Pass 3` 显著降耗**：`R6–R52 p3_s` 由 `v43 346.72 s` 降至 **`317.37 s`（`-29.35 s`）**，`sum_gpu` 降至 **`200.02 s`**（`ov=134` vs `v43 188`）；`R1–R52` 累计 `705.82 s`。
  - **尾盘 `fb=2` 死锁根因锁定**：由于 `R57–R58`（`600k/1.2M`）过紧未解出，进入 `R60–R74`（`active=4, over=2`）后即便 `>=60` 恢复 `2M/4M` 且固定拆除同一批 `48` 条 `netSize<=64` 旁路，仍有 `2` 条连接在 `padRelax=48 / budget=4M` 下越界失败（`fb=2, pops=12.0M/轮, 9.7–12.0s/轮`）并回滚 `savedPaths`，触发无进展死循环；于 `R74` 干净停机并转入 `v46` 修复。

### 2.26 `theia50per-ppt-hybrid-v46`（三级尾盘逃逸通道成功破除 `v45` 死锁：**`67` 轮全收敛 & `dcp_legality PASS`**）
- **改动文件**：`scratch/v46/` 下 `fides_cpu.cpp`、`prepare_v46.py`、`deploy_v46.py`、`launch_v46.py`、`record_rounds_v46.py`、`v46_patch.py`。
- **核心改动**：
  1. `runProbe` 新增 `padRelax < 0` 分支显式关闭 `origBox`（`q.useBox = false`）。
  2. `R59+` 触发扩展邻域拆除（`netSize <= 256, lastPops <= 250000, dx=4/dy=8`, 上限 `96` 条），并在 `1.2M / pad=48` 失败时叠加三级逃逸（`R57–R58`: `pad=96 / 4M`；`R59+`: 仅对失败连接解锁 `padRelax=-1 / b3=0` 无界搜索）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V46_SMOKE_PASS numGpus=3 init_s=0.72 batch_ok=1 ta0=1024 tb0=80`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`0d9b033457cd4b98be2310eb7eb5b48082390bdd70a0e04036e61bdb44c57af9`，`cuda_sha256`：`86641548a3d53716c711ee38f5c9f6d167a7757d95965b6d81591e521f12e155`。
- **全流程实测与诊断（`2026-11-24T23:10:57.64 -> 23:32:28.65`，`3×V100` 空闲 GPU 0/1/3，`status="passed", stage="complete"`）**：
  - **`R1–R5` 刷新 3 卡记录**：**`R1–R2 = 68.95 s`**（`R1=35.29s, R2=33.66s`, `gpu=50.69s`）、`R3–R5 = 159.86 s`（`R3=63.89s, R4=55.89s, R5=40.08s`）、`R6–R52 = 467.59 s`（`host=14.90s [meta=5.65s]`）。
  - **逃逸通道破锁但首入无界洪泛成本暴露**：`R55–R56` 因 `600k/1.2M` 过紧出现 `fb=1/2` 拖过 `R58`；`R59` 激活扩展邻域与 `padRelax=-1 / b3=0` 后 **`fb` 立即归 `0`**，但无 `budget` 上限的无界洪泛在 `R59–R66` 耗时 `226.59 s`（`R62=62.10s`），最终 **`R67` 收敛至 `over=0`**（`0.06s`）。
  - **汇总与合法性明细**：`sum_cpp = 949.63 s`（`p1=291.55s, p2=46.48s, p3=610.29s`; `sum_gpu=211.45s / 1,939 waves, ov=234`）；`materialize` 检查 `20` 项通过、`assignNodes=1.82s`、`FIX_ROUTES fixed=116154 parallel=true 8.39s`、`SETPIPS=10.36s`、`CHECKPIPS=12.81s`、`STATS_WL=7.19s`、`WL=19,071,439`、`Nodes popped=25,031,215,850`；**`Route design = 1,097.35 s`**（`Init 22.79s + Routing 1,074.56s`）、`route_wall ≈ 1,198.70 s`、`E2E ≈ 1,291.01 s`（`21m 31s`）；**`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31164735`**（`read=82.11s, pins=2.77s, pip_shards=2.35s, unrouted_scan=0.65s`），`dcp_sha256`：`9aa64cfc2a7b08c8df2b76a1b219f31bb80778d2b8a010a2a7de2998026cdbf5`。

---

## 3. 已定位的 `v25` vs PPT 差异摘要（促成 `v26`）

1. **连接排序口径**：PPT 评测机基线与优化版（Slide 19 `aquarius=66,504`、`THEIA_jpeg=2,159,365`、`THEIA_50per=12,271,872` / `517.28 s`）采用 `HPWL_FIRST` 特征排序；`v22–v25` 仍沿用 `UPSTREAM`（`17.29M` WL），把高扇出网锁死在最前面。
2. **C++ 回退漏洞**：`v25` `lagunaDir` 位缺失导致 `33.2%–35.3%` 调用跑一半回退 Java，`F4RegionQueue` 36 核也未用 C++。
3. **短走廊过度挤压**：`shortBox` ±1 严格不等式导致 0 余量，使 R1 过载较 `v14-r2` 翻倍（`964k -> 1.735M`）。
4. **Java 同步税与 GPU 误判**：`HPWL_FIRST` / 并行提交会频繁切换 Net 触发 `f4FastMembersOf`，且 `8k` pop 预算（C++ ~3ms）送给 `width=1` GPU（~80ms）必然产生负收益。

---

### 2.27 `theia50per-ppt-hybrid-v47`（平衡尾盘阶梯 + `R62+ 8M` 洪泛止损：**`R1–52` 持平 `v43`，`R55–R69` 尾盘放大中止**）
- **改动文件**：`scratch/v47/` 下 `fides_cpu.cpp`、`prepare_v47.py`、`deploy_v47.py`、`launch_v47.py`、`record_rounds_v47.py`、`v47_patch.py`（`fides_gpu.cu` 未改）。
- **核心改动**：`Pass 3 tw=1024u / >=224u`，`R55–R58` 尾盘 `1M -> 2.5M`，`R59+` 叠加 `padRelax=128 / 6M`，`R62+` 叠加 `padRelax=-1 / 8M` 与扩展邻域（`<=96` 条、`netSize <= 256`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V47_SMOKE_PASS numGpus=3 init_s=0.65 batch_ok=1 ta0=1024 tb0=80`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`a0d02d6c0de64b4ca3866839532fdf5a53e88a9e12a81f78bce19673dc11c47e`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程观测与止损（`2026-11-24T23:42:24.24` 启动，`3×V100`，`R69` 止损中止并释放 GPU）**：
  - `R1–R52` 累计 `713.50 s`（`R1-2: 71.16s, R3-5: 171.21s, R6-52: 471.13s`，`gpu_s=226.46s, ov=162, host=15.00s [meta=5.48s]`），基本持平 `v43`；
  - **退化根因**：`R55` `active=53` 未触发邻域 rip-up 仅靠 `1M+2.5M` 出现 `fb=4`（`13.11s`），此后一路链式累加到四级探测（`R59–R63` `fb=3~5, 36.93–53.43s/轮`）；`R64` 解锁后反冲 `109.6M pops`（`R65=93.85s`），至 `R69` 尾盘 `R53–R69` 已耗 `686.37 s`（总 `sum_cpp = 1,399.87 s`）仍在 `over=7` 拉锯，遂干净停机。

### 2.28 `theia50per-ppt-hybrid-v48`（4-ary 最小堆 + `MPOL_INTERLEAVE` + 真 LPT + `v43` 尾盘/`R70+` 逃逸：**验证 CPU `R3–R5 -7.03 s`，`R70` 后备偏晚中止**）
- **改动文件**：`scratch/v48/` 下 `fides_cpu.cpp`、`prepare_v48.py`、`deploy_v48.py`、`launch_v48.py`、`record_rounds_v48.py`、`v48_patch.py`。
- **核心改动（基于 `v43`）**：
  1. `CpuSearchContext` 手写 4-ary min-heap（通过 50 万次随机操作对比 `std::priority_queue` 验证 `heap4_ok=1`）；
  2. `nativeInit` 通过 `SYS_set_mempolicy(MPOL_INTERLEAVE)` 实现双 NUMA 物理页交织（`set_interleave=0 errno=0 restore=0`）；
  3. `runDynamicPass` 引入真 LPT 任务优先级（`ReadyNetItem::prio` + `cpuFallbackItems` 降序）；
  4. 回归 `v43` `2M->4M`（`R56` 邻域拆除）+ `R70+` 终极后备（`escNbr 96 / netSize<=256` + `padRelax=-1, b3=0`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V48_SMOKE_PASS heap4_ok=1 numGpus=3 init_s=0.71 batch_ok=1 ta0=1024 tb0=80`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`6f98921e60d569f266fb2b49d9cf4f01a49e61d22cfbb488e95f0a7eddcae540`，`fides_gpu_cu_sha256`：`e9b2540fcc42106be3b8dd6a887efc7a277901496225f0f80c1efdd079f987e2`。
- **全流程观测与止损（`2026-11-25T00:15:50.80` 启动，`3×V100`，`R70` 后干净中止并释放 GPU）**：
  - **CPU 内核微优化正收益确认**：**`R3–R5 = 158.18 s`**（`R3=62.97s, R4=53.95s, R5=41.26s`），较 `v43 165.21 s` 再快 **`-7.03 s`**（`host=15.04s [meta=6.01s]`）；`R1–2=72.38s`；
  - **两处退化根因确认**：(1) `R6–R52` 因随机提交序落到高溢出路径（`gpu_ov=226` vs `v43 188`, `p3=520.61s`）；(2) **`R59–R69` 在 `active=6, fb=3, over=14->11` 干等 `11` 轮（约 `~192 s`）**才到 `R70` 激活 `escNbr` + `padRelax=-1, b3=0`，虽瞬间打掉 `fb=3 -> 0`，但无界洪泛单轮飙至 `127.31 s / 148.73M pops` 且 `over=11 -> 20`（至 `R70` 累计 `sum_cpp = 1,209.69 s`），遂干净中止并清空 `/dev/shm`。

### 2.29 `theia50per-ppt-hybrid-v49`（`TB_BCK_CAP=24576 / TB_OV_CAP=4.5M` + `Pass 3` GPU/CPU 并发重叠 + `F2Prewarm`/后处理 Java 提速：**`R1–R52` 破历史至 `672.17 s`，`gpu_ov 188->138`、`Init -4.88 s`、`F2 -3.60 s`、`SETPIPS -2.79 s`，全量 `dcp_legality PASS`**）
- **改动文件**：`scratch/v49/` 下 `fides_gpu.cu`、`fides_cpu.cpp`、`v49_patch.py`、`prepare_v49.py`、`deploy_v49.py`、`launch_v49.py`、`record_rounds_v49.py`。
- **核心改动**：
  1. `fides_gpu.cu`：`Tier-B1`（`mode=0`）放宽 `TB_BCK_CAP`（`16384 -> 24576`）与 `TB_OV_CAP`（`3.5M -> 4.5M`），分配率由 `67%` 提至 `72%`，保持每卡全量 `80` CTAs + `1024` Tier-A warps（`arena=23.31 GiB, free=3.86–4.16 GiB`）。
  2. `fides_cpu.cpp`：`checkAccessAndLookahead` 非 `PF_PINBOUNCE` 剪枝及缓存 `occVal/sameUses` 避免重复 `getUsage`；`Pass 3 >= 256u / tw=2048u`，并将 `lastPops >= 6.5M` 重连接所在网拆入 `cpuOverlapThread` 与 Wave 0 GPU 并发执行（共享 `G.waveCommitEpoch` 原子判冲）；`R54+` 邻域拆除 + 前轮 `fb>0` 即时升级 `escNbr (96)` 与有界 `6M / pad=96`。
  3. `v49_patch.py`：并行化 `F2Prewarm.geometry`（`Arrays.parallelSort` + 分块填充，校验 `features.live.bin` SHA-256 逐字节一致）；去除 `postRouteProcess` 中 `Arrays.asList(source, altSource)` 分配；测试 `checkPIPsUsage` 预扩容（实测反慢，待 `v50` 回退）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V49_SMOKE_PASS heap4_ok=1 init_ok=1 numGpus=3 init_s=1.59 ta0=1024 tb0=80 arena0=23.31GiB free0=3.86GiB`；`cpp_sha256`：`5feea81ae567119a747d11522185f0a8e04da37651b7cae94c179147fc20209f`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与诊断（`2026-11-25T00:56:52.88 -> 01:14:51.24`，`3×V100` 空闲 GPU 0/1/3，`status="passed", stage="complete"`）**：
  - **多处高确定性提速命中验证**：
    - **`R1–R52 = 672.17 s`（较 `v43 682.37 s` 快 `-10.20 s`！）**：`R1–2 = 70.01 s`（`-1.39 s`）、`R3–5 = 158.93 s`（`-6.28 s`）、`R6–52 = 443.23 s`（`sum_gpu = 205.67 s` 较 `v43 259.68 s` 降 **`-54.01 s`**，`gpu_ov = 138` 较 `v43 188` 降 **`-50` 项**！其中 `R7 -6.55s, R8 -3.88s, R12 -4.60s, R20 -5.68s, R23 -10.02s, R28 -6.23s`）；
    - **`Initialization = 20.75 s`（`-4.88 s`）、`F2Prewarm.geometry = 2.66 s`（`6.26 -> 2.66s`, `-3.60 s`）、`corridor = 14.53 s`（`-2.01 s`）、`SETPIPS = 8.86 s`（`-2.79 s`）、`F4_V49_POST`：`lut=0.00 assign=1.80 fix=8.35 srcPin=1.89 s`**。
  - **两项拖慢点（直接指导 `v50` 收割）**：
    - `R58–R63` 单条 `fb=1` 连接因未跳过已失败档位、连续 5 轮重跑 `2M+4M+6M = 12M pops/轮`，耗 **`74.88 s`**（较 `v43` `12.80 s` 多 **`+62.08 s`**）；
    - `checkPIPsUsage` 预开 `1<<19` HashMap 引起 `256 MiB` 清零/TLB 开销，由 `6.83 s -> 12.09 s`（`+5.26 s`）。
  - **汇总与合法性明细**：`65` 轮收敛至 `over=0`；`sum_cpp = 747.05 s`（`p1=293.44s, p2=46.91s, p3=405.58s`, `host=14.68s [meta=5.52s]`）；`materialize=15.76s`、`STATS_WL=7.81s`、`WL=19,047,241`、`Nodes popped=24,582,898,836`、`Route design = 887.76 s`（`Init 20.75s + Routing 867.01s`）、`route_wall ≈ 990.13 s`、`E2E ≈ 1,078.27 s`（`00:56:52.97 -> 01:14:51.24`）；**`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31142597`**（`read=78.44s, pins=2.96s, pip_shards=2.01s, unrouted_scan=0.73s`），`dcp_sha256`：`27cff4f6e420a12e2407a915217ba28024c77cde89ef7f59a2fed199c7dd6c1a`。

### 2.30 `theia50per-ppt-hybrid-v50`（`F4_V50_PRE` 实测破案；`5.5M` 重叠与 `padRelax=-1` 提前升级退化，于 `R70` 止损并定位 `v51` 正交组合）
- **改动文件**：`scratch/v50/` 下 `fides_cpu.cpp`、`v50_patch.py`、`prepare_v50.py`、`deploy_v50.py`、`launch_v50.py`、`record_rounds_v50.py`（`fides_gpu.cu` 同 `v49`）。
- **核心改动**：
  1. `v50_patch.py`：回退 `checkPIPsUsage` 至 `1<<16`，并新增 `F4_V50_PRE`（测量 `routeGlobalClkNets / routeStaticNets / preRoutingEstimation / F2Prewarm / f4PrecomputeDirect` 耗时）；
  2. `fides_cpu.cpp`：新增 `G.tailFailStreak[connIdx]`（`fs==1` 跳过 `2M+4M` 走 `6M/pad=96`，`fs>=2` 走 `16M/pad=-1`，`fs>=3` 走 `b=0/pad=-1`）；同时将 `Pass 3` GPU 阈值降至 `224u`、将 `cpuOverlapThread` 重连接分流阈值降至 `lastPops >= 5.5M`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V50_SMOKE_PASS dlopen_ok=1 init_ok=1 numGpus=3 init_s=0.67 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`0d5321fd8f9cacfc11430f0b6e0a77cd842bcd2c13357ad8edf64f5d79384cc9`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **实测关键数据与止损诊断（`2026-11-25T09:29:58.95+08:00` 启动，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被 `20–29% / 518 MiB` 占用，`R70` 干净中止并释放 GPU）**：
  - **`F4_V50_PRE` 预路由打点锁定**：`Read Netlist & PhysDB = 33.227 s`、`F4_V50_PRE clk=0.00 static=0.20 preEst=0.00 f2=5.55 directPre=0.49`、`F4_V37_IDMAP_CACHE = 1.059 s`、`FIDES_NATIVE_CPU init_s = 5.41 s`。证明时钟与静态网仅 `0.20 s`，`pre-R1` 主要优化空间在 `F2Prewarm` / `Initialization` 与 RRG 读取前段。
  - **两处机制级退化定位清楚（对照 `v49`）**：
    1. **`R1–R52 = 817.67 s`（较 `v49 672.17 s` 慢 `+145.50 s`）**：`5.5M` 并发阈值与 `224u` 门槛将大量中等重连接推进单/阻塞并发流与 `Pass 3` GPU，造成 `R08–R12` 与 `R19–R26` 爆发（`R08 +13.09s`, `R09 +10.61s`, `R12 +15.29s`, `R19 +10.19s`, `R20 +11.71s`, `R21 +16.33s` [`cpu_pops` `78.4M -> 158.5M`], `R23 +23.91s` [`gpu_ov` `7->24`], `R24 +19.04s` [`gpu_ov` `4->25`], `R25 +13.54s`, `R26 +12.03s`）；尽管 `R29–R52` 一度把 `over` 压至 `14`（优于 `v49 93`），`R53–R56` 仅耗 `5.99 s`。
    2. **`R57–R70` 无界逃逸放大**：`R58` 曾降至 `over=1`，但 `R59` 触发 `fs>=2`（`16M / padRelax=-1`）叠加 `escNbr (96)` 重拆后（`active=2, routed=98, fb=0`），`over` 从 `1` 反跳至 `9 -> 23 -> 7`，`R53–R66 = 201.91 s`、`R67–R70 = 72.38 s`（`R70` 仍 `over=7, fb=3, 34.40 s`），遂通过 `pkill -9` 干净终止并清理 `/dev/shm` 与显存。

### 2.31 `theia50per-ppt-hybrid-v51`（**`R1–R52` 破大纪录至 `636.80 s`（较 `v43 -45.45 s`、`v49 -35.37 s`！）**，取消 `6M/pad=96` 导致尾盘 `fb=2` 拉锯并于 `R79` 止损）
- **改动文件**：`scratch/v51/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v51_patch.py`、`prepare_v51.py`、`deploy_v51.py`、`launch_v51.py`、`record_rounds_v51.py`。
- **核心改动**：完整保留 `v49` 已验证的 `R1–R52` 引擎（`TB_BCK_CAP=24576 / TB_OV_CAP=4.5M`、`Pass 3 >= 256u / tw=2048u`、`cpuOverlap >= 6.5M`）与 `v50` `checkPIPsUsage 1<<16` 回退/`F4_V51_PRE`；尾盘改用保守 `48` 邻域拆除（`netSize<=64, lastPops<=50k, dx=2, dy=4`）+ `tailFailStreak` 仅跳过 `2M` 直跑有界 `4M/pad=48`，并将 `escNbr(96) / pad=-1` 后撤至 `R72+`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V51_SMOKE_PASS dlopen_ok=1 init_ok=1 numGpus=3 init_s=0.69 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`74f5f0375b674f411fb41341e37ac2a18939ee66e9d0dfb2b2aab6c68ca5155b`，`cuda_sha256`：`cb22970682d1824fd9867c32806c4c84d75596aec9fed6f5d10d9ecf0c5b778d`。
- **实测关键数据与止损诊断（`2026-11-25T09:58:48.23+08:00` 启动，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被占用 `518 MiB`，`R79` 后干净中止并释放 GPU）**：
  - **`R1–R52` 刷新历史最低 `636.80 s`（较 `v43 682.25 s` 快 `-45.45 s`、较 `v49 672.17 s` 快 `-35.37 s`）**：`Read Netlist & PhysDB = 33.365 s`、`F4_V51_PRE clk=0.00 static=0.18 preEst=0.00 f2=5.43 directPre=0.52`、`IDMAP=1.023 s`、`NATIVE_CPU init_s=5.70 s`；`R1–52` 分解 `p1=305.52 s, p2=45.25 s, p3=285.06 s, gpu_s=191.17 s, gpu_ov=147`（对比 `v49`：`R10 -12.81s`, `R14 -8.44s`, `R15 -9.73s`, `R17 -9.53s`, `R30 -8.00s`, `R32 -5.87s`；`R52` `over=26`）。
  - **尾盘退化对照定位（`v49` vs `v51`）**：
    - `v51` 取消了 `v49` 的反应式 `escNbr(96)`（`netSize<=256, dx=4/dy=8`）与有界 `6M / pad=96`，使 `R55–R71` 在 `4M / pad=48` 上连续 `17` 轮 `fb=2` 无法绕开阻塞（`over` 卡在 `3 -> 2`）；至 `R72` 才切入 `escNbr(96) + padRelax=-1 / b=0`，`fb` 虽立即降为 `0`，但无界洪泛把 `over` 保持在 `2 -> 4 -> 3 -> 2 -> 5 -> 4 -> 5`（`R72–R79` 单轮 `13.7–39.8 s`），遂止损停机并释放 GPU（`312 MiB / 4 MiB / 0 MiB`, `0%`）。
    - 反向交叉印证：`v49` `R59–R65` 正是依靠 `escNbr(96) + 有界 6M / pad=96`（从未开 `padRelax=-1`）将 `over` 顺滑消解至 `0`。

### 2.32 `theia50per-ppt-hybrid-v52`（**`R53–R62 = 28.51 s`（较 `v49 -46.37 s`）+ 新最优端到端 `E2E = 1,057.50 s`（超越 `v43 1,059.83 s`）& `dcp_legality PASS`**）
- **改动文件**：`scratch/v52/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v52_patch.py`、`prepare_v52.py`、`deploy_v52.py`、`launch_v52.py`、`record_rounds_v52.py`。
- **核心改动**：保留 `v49/v51` 已验证的 `R1–R52` 引擎（`TB_BCK_CAP=24576 / TB_OV_CAP=4.5M`、`Pass 3 >= 256u / tw=2048u`、`cpuOverlap >= 6.5M`）与 `v50/v51` Java 修复（`checkPIPsUsage 1<<16`、`Arrays.asList` 消除、`F4_V52_PRE/POST`）；尾盘恢复 `v49` 反应式 `escNbr(96) + 有界 6M/pad=96`，仅增加 `tailFailStreak` 跳过冗余 `2M+4M` 与有界 `8M/pad=128`（将 `padRelax=-1` 后撤至 `R78+`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V52_SMOKE_PASS dlopen_ok=1 init_ok=1 numGpus=3 init_s=0.70 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`78fceabbca4f42b46ab7bc9a5d137b6c426b0855c870672575e3e0eb6bf62fc7`，`cuda_sha256`：`1cbeb0ca94cb806556a97d94b8e9de254ae8ba5ec65727736b05d1f636dce957`。
- **全流程实测与诊断（`2026-11-25T10:24:43.06 -> 10:42:20.65`，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被占用 `518 MiB / 18–26%`，`status="passed", stage="complete"`）**：
  - **尾盘方案一举验证成功**：`62` 轮干净收敛至 `over=0`，**`R53–R62 = 28.51 s`（较 `v49 74.88 s` 暴降 `-46.37 s`，全过程未触发 `padRelax=-1`！）**；
  - **全口径成绩（刷新端到端纪录）**：`Read Netlist & PhysDB = 33.594 s`、`F4_V52_PRE clk=0.00 static=0.18 preEst=0.00 f2=5.59 directPre=0.53`、`IDMAP=1.116 s`、`NATIVE_CPU init_s=5.90 s`；`R1–2 = 70.54 s`、`R3–5 = 158.12 s`、`R6–52 = 463.41 s`（`R1–52 = 692.07 s`）、`sum_cpp = 720.58 s`（`p1=302.30 s, p2=45.61 s, p3=371.56 s`, `gs=204.75 s, gpu_ov=162`, `corr=14.99 s, host=14.81 s [meta=5.45 s]`）；`materialize=15.46 s (export=0.69 cache=6.52 bind=3.58 users=4.66)`、`F4_V52_POST lut=0.00 assign=1.70 fix=8.82 srcPin=1.80`、`SETPIPS=9.86 s`、`CHECKPIPS=9.97 s`、`STATS_WL=10.91 s`、`WL=19,043,934`、`Nodes popped=25,421,863,143`；`Route design = 865.80 s`（`Init 24.79 s + Routing 841.02 s`，较 `v49 887.76 s` 快 `-21.96 s`）、`route_wall = 965.76 s`（`10:24:43.15 -> 10:40:48.90`）、**`E2E = 1,057.50 s`（`10:24:43.15 -> 10:42:20.65`，刷新最佳合法 E2E 纪录，超越 `v43 1,059.83 s` 与 `v49 1,078.27 s`）**；
  - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31157681`**（`read=81.51 s, pins=3.02 s, pip_shards=1.80 s, unrouted_scan=0.78 s`），`dcp_sha256`：`b3617dba2c0607418722f1091f31c8906c71bcbc33c83abb20782ff45ec6e269`。
  - **剩余主瓶颈已精确裸露**：本次 `R08–R13` 耗时 `176.67 s`（较 `v43 129.50 s` 多 `+47.17 s`，尤其 `R08 gpu_ov=27 -> p3_s=27.20 s / total=38.90 s`、`R09 gpu_ov=26 -> p3_s=33.19 s / total=43.32 s`，`gpu_ov` 回退 CPU 串行拖尾直接吞噬 `~23 s/轮`）；一旦像 `v51`（`R1–52 = 636.80 s`）那样用 `Tier-B1 Wide` 或消除 `gpu_ov=26` 串行回退，`Route design` 将稳定突破 `800 s`。

### 2.33 `theia50per-ppt-hybrid-v53`（**全口径大破纪录：`55` 轮收官，`Route = 802.97 s`（`-52.49 s`），`E2E = 986.74 s` 首破千秒大关 & `dcp_legality PASS`**）
- **改动文件**：`scratch/v53/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v53_patch.py`、`prepare_v53.py`、`deploy_v53.py`、`launch_v53.py`、`record_rounds_v53.py`。
- **核心改动**：
  1. `fides_gpu.h` + `fides_cpu.cpp`：在 `Pass 3` 启用多卡 `fidesGpuRunTierB1WideDev(mode=1/2)` 接管 `PA_HASH_OVERFLOW`（`>= 4` 条时切 `16M / 40 CTAs × 3 卡` 并行求解，`< 4` 条或 `mode>=2` 才回退 CPU）；
  2. `v53_patch.py`（`RWRoute.java`）：重构 `computesNodeUsageAndTotalWirelength`——单连接网跳过 `HashSet<Node>`，并以扁平 `long[]` 按 `IntentCode` 累计代替 6,200 万次 `EnumMap.merge / Long.valueOf` 装箱（完整保留 `LAGUNA_TILE / UBUMP` 映射语义）；
  3. `v53_patch.py`（`FidesLaneGate.java`）：Phase 2a 懒分配 `localIds[s]`，Phase 2b 以 `unresolvedCount` 统计未命中，全部就绪时跳过 `490` 万条 / `3,100` 万跳的串行 `seqFixes` 复扫。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V53_SMOKE_PASS dlopen_ok=1 init_ok=1 wide1=1(ctas=40) wide0=1(ctas=80) numGpus=3 init_s=0.65 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`2505895bb05f4826f11da27e6fcdfcc8926acfe6e8fc951952cfb100b14fdb50`，`cuda_sha256`：`bc9cc740ad5f935bfcbe889268f32966ad7b728fbae817b942e0da037ad32aae`。
- **全流程实测与诊断（`2026-11-25T10:53:23.49 -> 11:09:50.32`，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被占用 `518 MiB / 20–24%`，`status="passed", stage="complete"`）**：
  - **三项核心改动全数命中预期**：
    - `55` 轮干净收敛至 `over=0`（`R53–55` 仅 **`1.57 s`**，`0.93 + 0.59 + 0.05 s`，`fb=0`！），**`sum_cpp = 665.79 s`**（`R1–2 = 69.94 s`, `R3–5 = 152.75 s`, `R6–52 = 441.53 s`, `R53–55 = 1.57 s`; `p1=267.90 s, p2=44.71 s, p3=352.13 s`）；
    - **`Tier-B1 Wide`**：`gpu_ov = 160` 经多卡 `16M/32M` 宽桶直接完成 **`wide_ok = 44`**，CPU 回退 `gpu_fb` 由 `v52` `162` 降至 **`13`**；
    - **`STATS_WL = 3.46 s`**（较 `v52 10.91 s` 降 **`-7.45 s`**！）、**`materialize = 13.07 s`**（`export=0.61 cache=4.39 bind=3.66 users=4.42`，较 `v52 15.46 s` 降 **`-2.39 s`**！）。
  - **全口径成绩（全面刷新历史最佳合法基线）**：
    - `Read Netlist & PhysDB = 33.425 s`、`F4_V53_PRE clk=0.00 static=0.19 preEst=0.00 f2=5.08 directPre=0.51`、`IDMAP=0.953 s`、`NATIVE_CPU init_s=5.83 s`、`corr=14.44 s`、`host=14.55 s [meta=5.56 s]`、`F4_V53_POST lut=0.00 assign=2.04 fix=8.22 srcPin=1.86`、`SETPIPS=10.87 s`、`CHECKPIPS=8.12 s`、`Write EDIF & XDEF = 12.836 s`、`Write XDC = 0.768 s`；
    - `WL = 19,030,060`、`Nodes popped = 24,201,215,355`（均优于 `v43/v49/v52`）；
    - **`Route design = 802.97 s`**（`Init 22.14 s + Routing 780.84 s`，较 `v43 855.46 s` 快 **`-52.49 s`**，较 `v52 865.80 s` 快 **`-62.83 s`**）；
    - **`route_wall = 895.63 s`**（`10:53:23.58 -> 11:08:19.21`，首破 `900 s`）、**`E2E = 986.74 s`**（`10:53:23.58 -> 11:09:50.32`，**`16m 26.74s`，首破 `1,000 s`！**较 `v43 1,059.83 s` 快 **`-73.09 s`**）；
  - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31140075`**（`read=80.87 s, pins=2.72 s, pip_shards=2.24 s, unrouted_scan=0.85 s`），`dcp_sha256`：`e2cdc6b7aae8e064d55dae5b9a472869c39deb7599db5071bc4ea1ebb7fddd89`。

### 2.34 `theia50per-ppt-hybrid-v54`（`F4_V54_PORTALS 0.40 s` / `F4_V54_SORT 1.27 s` 兑现；停用 `32M` 与 `>=12u` 门槛导致 `gpu_fb 13->85`、CPU 长尾反慢并于 `R59` 止损）
- **改动文件**：`scratch/v54/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v54_patch.py`、`prepare_v54.py`、`deploy_v54.py`、`launch_v54.py`、`record_rounds_v54.py`。
- **核心改动（基于 `v53`）**：
  1. `fides_cpu.cpp`：限制 `Tier-B1 Wide` 最高仅跑 `mode=1 (16M)`（关闭 `mode=2 32M` 重试），并将触发门槛由 `wideRetryItems >= 4u` 提高至 `>= 12u`；
  2. `v54_patch.py`（`F1Corridor.java`）：`indexPortals`（`92.5M` 节点跨 SLR 门户扫描）改为 `1M`-chunk `CompletableFuture` 36 核并行 + 保序合并（`F4_V54_PORTALS`）；
  3. `v54_patch.py`（`RWRoute.java`）：`sortConnections`（`4.91M` 连接排序）改为 `Arrays.parallelSort`（`F4_V54_SORT`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V54_SMOKE_PASS dlopen_ok=1 init_ok=1 wide1=1 wide0=1 numGpus=4 init_s=0.93 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`2ddc639cdfe693e793d4a4845db41e30cf8efd8c218efb2ffb0cbbda8a612a76`，`cuda_sha256`：`44d2de1b3544ab3105b5a72ecdd0967d8d877da2c3008d222c5777d8a3a11fb7`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **实测关键数据与止损结论（`2026-11-25T11:27:51.43+08:00` 启动，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被占用 `518 MiB / 16–23%`，`R59` 中止并释放 GPU `0/1/3`）**：
  - **两项 Java 并行预阶段验证命中**：**`F4_V54_PORTALS portals=1440 time_s=0.40`**、**`F4_V54_SORT conns=4909155 time_s=1.27`**（后续版本可直接保留）；同时测得 `R1` `corridor_s = 14.25 s` 主因仍在 `boxParallel / commitParallel` 而非 `indexPortals`。
  - **关键反证（为何 `v53` 全量 `Tier-B1 Wide >= 4u + 16M/32M` 才是净正收益）**：
    - `v54` `R1–57` 的 GPU 耗时确实由 `v53 278.68 s` 降至 `231.35 s`（`-47.33 s`），但因跳过 `32M` 与 `<12u` 批次，**`gpu_fb` 由 `v53 13` 飙升至 `85`（`+72` 条超重尾连接回落 CPU）**，CPU 侧扩展由 `7.115B` 暴涨至 `8.814B`（有效率因 CPU 重尾串行拉扯与轻度宿主机负载从 `18.38M -> 13.27M pops/s`），把 `p3` 从 `v53 352.13 s` 推高至 `544.96 s`（`R1–57 sum_cpp = 895.64 s`、`R58 = 41.52 s` 累计 **`937.16 s`**），并在 `R55–R58` 陷入 `fb=4 / over=14->10->10->9`（`41.5–48.5 s/轮`）后干净中止。
    - **结论**：`v53` 让 GPU `16M/32M Tier-B1 Wide`（`>=4u`）硬吃重尾是**实实在在的净提速**（少用 CPU ~`200 s`，只花了 GPU `+47 s`）；`v55` 应**完整恢复 `v53` 的 `Tier-B1 Wide` 策略**，保留 `v54` 的并行 `indexPortals` / `sortConnections`，再针对 `v53` 真正热点优化！

### 2.35 `theia50per-ppt-hybrid-v55`（61 轮全收敛 `dcp_legality PASS`；全量固定开销提速兑现、`WL = 18,987,824` 破 19M，`R6–17 p1` 与 `Read PhysDB` 波动致 `Route = 855.02 s / E2E = 1,044.13 s`）
- **改动文件**：`scratch/v55/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v55_patch.py`、`prepare_v55.py`、`deploy_v55.py`、`launch_v55.py`、`record_rounds_v55.py`。
- **核心改动（基于 `v53`）**：
  1. 完整保留 `v53` `Tier-B1 Wide`（`>= 4u, mode=1/2`）与边界尾盘参数；
  2. `FidesNativeCpu.java` + `fides_cpu.cpp` + `FidesLaneGate.java`：新增 `nativeInitScopesBatch`（35 核并行 `initFlatFromPairs / smallIds`）与 36 核并行 `f4NativeRegisterAll` 特征/成员批抽样；
  3. `F1Corridor.java`：保留并行 `indexPortals`（`F4_V55_PORTALS`），并将 `candidates` / `compactCrossSlr` 改为 64 分片预扩容 `IdentityHashMap` + 64 分片并行 `commitParallel`（消除 `R1` 17 次单线程扩表）；
  4. `RWRoute.java`：保留 `Arrays.parallelSort`（`F4_V55_SORT`），并在 `!design.isTrackingNetChanges()` 时将 `net.setPIPs(newPIPs)` 直接并入 36 核并行循环执行。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V55_SMOKE_PASS dlopen_ok=1 batch_sym=1 init_ok=1 wide1=1 wide0=1 numGpus=4 init_s=0.96 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`cd4a24e37bdb2ba6e4b76f5d868fb893fb14700e42f346612906f7048c76d585`，`cuda_sha256`：`da96bc6376a0a3e810156ecb38d7f14fb44465d31fddb982a68435cbfa877ad8`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T12:33:04.34 -> 12:50:29.78`，`3×V100` 空闲 GPU `0/1/3`，GPU `2` 仍被占用 `518 MiB / 20–27%`，`status="passed", stage="complete"`）**：
  - **全部 5 项并行固定优化实测命中**：
    - `FIDES_NATIVE_CPU v37 registered=4909155 nets=858089 init_s=3.45`（较 `v53 5.83 s` 降 **`-2.38 s`**）；
    - `R1 corridor_s = 11.99 s`（较 `v53 13.44 s` 降 **`-1.45 s`**，`F4_V55_PORTALS = 0.46 s`）；
    - `F4_V55_SORT = 1.29 s`、`F4_V37_SETPIPS = 8.44 s`（较 `v53 10.87 s` 降 **`-2.43 s`**）、`F4_V37_FIX_ROUTES = 7.89 s`（较 `v53 8.22 s` 降 `-0.33 s`）、`direct_finalize = 0.178 s`、`host = 14.33 s`；
    - **`p3 = 338.84 s`**（较 `v53 352.13 s` 降 **`-13.29 s`**）与 **`gs = 259.33 s`**（较 `v53 278.68 s` 降 **`-19.35 s`**，`wide_ok = 57, ov = 127, fb = 11`），且线长 **`Total wirelength = 18,987,824`**（首次低于 19M、`v34` 以来最低！）。
  - **为何总体仍慢于 `v53`（`Route = 855.02 s` vs `v53 802.97 s`）**：
    - `R1–2 = 72.13 s`, `R3–5 = 170.07 s`（vs `152.75 s`, `+17.32 s`）, `R6–17 = 297.56 s`（其中 `p1 = 98.09 s` vs `v53 52.29 s`, **`+45.80 s`**）, `R18–52 = 162.07 s`, `R53–61 = 15.86 s`（`R56–57` `fb=2` 占 `15.17 s` -> `R61 over=0`）；加上受宿主机背景负载影响的 `Read Netlist & PhysDB = 41.364 s`（`+7.94 s`）与 `CHECKPIPS = 14.95 s`（`+6.83 s`），导致 **`p1` 总计多耗时 `+63.34 s`（`331.24 vs 267.90 s`）**；
    - 全口径汇总：`sum_cpp = 717.69 s`（`61` 轮）、`materialize = 14.25 s`、`STATS_WL = 4.61 s`、`Nodes popped = 25,106,014,062`、`Route design = 855.02 s`（`Init 23.97 s + Routing 831.06 s`）、`route_wall = 952.37 s`、**`E2E = 1,044.13 s`（`17m 24.13s`，优于 `v43/v49/v52`，仅次于 `v53`）**；
  - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31126940`**（`read=81.43 s, pins=2.78 s, pip_shards=2.18 s, unrouted_scan=0.59 s`），`dcp_sha256`：`ef4e7bb3a0516667b383ce8b79949e0d189165c18a834cbc12dcdf46ebe1fccb`。

### 2.36 `theia50per-ppt-hybrid-v56`（**`R1–52 = 626.08 s`（破全库纪录！`p1 = 222.46 s, R1–2 = 63.46 s, R3–5 = 123.59 s`）+ `CHECKPIPS = 2.02 s`（`-6.10..-12.93 s`）& `dcp_legality PASS`；尾盘 `R59–77` 空等 `R78` 拖慢**）
- **改动文件**：`scratch/v56/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v56_patch.py`、`prepare_v56.py`、`deploy_v56.py`、`launch_v56.py`、`record_rounds_v56.py`。
- **核心改动（基于 `v55`）**：
  1. `fides_cpu.cpp`（CPU 搜索热路径三重加速）：
     - `NetScopeData` 新增每 Net L2 驻留单缓存行 Bloom 过滤器（`256..8192 × uint64_t`，双 bit 探针），在 `initFlatFromPairs / rehashFlat` 构建并在 `addToScopeCore` 写 `occ/sl.key` 前增量置位；
     - `getUsage()` 先读 `__atomic_load_n(&G.dyn[id].occ, __ATOMIC_ACQUIRE)`：若 `rawOcc <= 0` 直接返回 `(rawOcc, sameUses=0)` 跳过 `S.ov` 与 `baseScope` 哈希查找；若 `rawOcc > 0` 且未命中 `S.ov` 再先查 `mayContainBloom()` 过滤 98%+ 异网 DRAM 探针；
     - `CpuSearchContext::initHashOv` 引入精确 `ovCap / ovMask`（由 `256` 起步按 `4×expectedItems` 扩表而非固定 `65,536 = 1 MiB`）；`expandNode` 循环增加 `&G.geom[G.edge[e + 2]]` 软件预取。
  2. `v56_patch.py`（`RWRoute.java` `checkPIPsUsage` + `FidesLaneGate.java`）：
     - 重构 `checkPIPsUsage` 为 **64 分片原语 `long[]/int[]` + 无装箱开放定址哈希表**（将 `(tile.getUniqueAddress()+1, startWire, endWire)` 打包为 64 位非零 `long`，零 `ArrayList` / `HashMap$Node` 分配，异常/越界/真重叠回退 `checkPIPsUsageSlowFallback`）；
     - `f4NativeMaterializeAll` Phase 3 预扩容 `localNodes/localNets = new ArrayList<>(2048)`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V56_SMOKE_PASS dlopen_ok=1 batch_sym=1 init_ok=1 wide1=1(ctas=40) wide0=1(ctas=80) numGpus=4 init_s=1.01 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`0a86745c49e46412d4b6419fb35c86a4487a312d542737a9502bc8ff190e7178`，`cpp_sha256`：`1f9b66e13c0f23e8b72f6dcb0239706da69a615878bc5e30458127430d5f5713`，`cuda_sha256`：`34b2fcd6715794db562acebd2b2fa1a2fecfa78725ecbfd6a325b60cef8e84b3`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与分段剖析（`2026-11-25T13:15:39.49 -> 13:38:49.64`，`4×V100` 全卡 `0%` 空闲独占启动，`status="passed", stage="complete"`）**：
  - **前中段与后处理全部刷新历史最速纪录**：
    - **`R1–R2 = 63.46 s`**（4 卡全跑：`R1 = 30.39 s [gpu=20.13s]` + `R2 = 33.07 s [gpu=25.98s]`，较 `v53 69.94 s` 快 **`-6.48 s`**）；
    - **`R3–R5 = 123.59 s`**（`R3 = 49.49 s [p1=48.84s, 35.9M/s]`, `R4 = 43.31 s [p1=39.85s, 43.6M/s]`, `R5 = 30.79 s [p1=21.46s, p2=5.45s, p3=3.85s]`，较 `v53 152.75 s` 快 **`-29.16 s`**，`R3/R4` CPU 搜索吞吐分别提升 **`+31%` / `+28%`**！）；
    - **`R1–R52 = 626.08 s`**（**全库最低**！较 `v51 636.80 s` 快 `-10.72 s`、较 `v53 664.22 s` 快 **`-38.14 s`**、较 `v55 701.83 s` 快 **`-75.75 s`**；其中 **`p1 = 222.46 s`** 创历史最低，较 `v53 267.90 s` 快 **`-45.44 s`**；`p2 = 41.80 s`, `gpu_ov=202, wide_ok=52, gpu_fb=3`）；
    - **`F4_V37_CHECKPIPS fast=true = 2.02 s`**（较 `v53 8.12 s` 降 `-6.10 s`、较 `v55 14.95 s` 降 **`-12.93 s`**！）；
    - **`F4_V37_SETPIPS = 7.01 s`**（新低）、`FIDES_NATIVE_CPU init_s = 3.10 s`（新低）、`Initialization = 21.34 s`、`materialize = 14.18 s (bind=3.42 s)`、`STATS_WL = 4.28 s`、`F4_V56_POST`: `assign=2.07 s, fix=8.21 s, srcPin=1.83 s`、`Read Netlist & PhysDB = 33.400 s`。
  - **尾盘唯一失分项精确定位（`R53–R82 = 445.51 s`）**：
    - `R56–R77` 连续 22 轮陷入 `fb=1..3 / over=2..13` 原地空等（`8M / pad=128` 越界失败却因 `routeIter < 78` 死锁等待第 78 轮才解锁 `padRelax=-1`），直白烧掉 **`~348 s`**（`R78` 一解锁 `fb` 立刻归 `0` 并于 **`R82` 收敛至 `over=0`**，但多耗 25 轮将 `Route design` 推高至 `1,193.59 s`）；
    - 全口径汇总：`82` 轮收敛至 `over=0`，`sum_cpp = 1,071.59 s`（`p1=222.46 s, p2=41.80 s, p3=806.06 s`, `gs=298.03 s`, `corr=13.52 s`, `host=14.89 s [meta=6.14 s]`），`WL = 19,084,382`，`Nodes popped = 26,578,338,523`，`Route design = 1,193.59 s`（`Init 21.34 s + Routing 1,172.25 s`），`route_wall = 1,293.19 s`（`13:15:39.49 -> 13:37:12.68`），`E2E = 1,390.15 s`（`13:15:39.49 -> 13:38:49.64`）；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31169356`**（`read=87.32 s, pins=2.78 s, pip_shards=1.66 s, unrouted_scan=0.68 s`），`dcp_sha256`：`103a33d5ae2826beb65fc4f4bdaefbee64bf3d5376a29b9f071b28cab1efe6bd`。

### 2.37 `theia50per-ppt-hybrid-v57`（`R53–60 = 3.82 s`、`CHECKPIPS = 1.30 s` 新纪录、`WL = 18,955,186` 新低、`dcp_legality PASS`；隔离运行于 `1×V100 GPU 0` 与共享 CPU 上完成 `Route = 984.97 s / E2E = 1,193.53 s`）
- **改动文件**：`scratch/v57/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v57_patch.py`、`prepare_v57.py`、`deploy_v57.py`、`launch_v57.py`、`record_rounds_v57.py`。
- **核心改动（基于 `v56`）**：
  1. `fides_cpu.cpp`（修复 `v56` `R59–77` 尾盘空等 + 轻量化邻域拆线）：
     - 将 `routeIter >= 78` 死锁等待提前为分层逃逸：一旦 `routeIter >= 58 && fs >= 3`（即 `pad=96/6M` 与 `pad=128/8M` 已均失败）切入 `16M / padRelax=-1`，若 `fs >= 4 && routeIter >= 60`（或 `routeIter >= 78`）切入 `budget=0 / padRelax=-1`；
     - `escNbr` 触发时将邻域旁路筛选阈值收紧为 `maxNbrNet = 128, maxNbrPops = 120000`（原 `256 / 250000`），避免拉入高扇出难重布邻居。
  2. `v57_patch.py`（`worker.py` 活跃显存多卡隔离）：`v38_multi_gpu_visible` 新增查询 `nvidia-smi --query-compute-apps=gpu_uuid,used_memory`，凡存在 `used_memory >= 400 MiB` 活跃计算进程的外卡一律严格排除，避免与同机训练任务冲突。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V57_SMOKE_PASS dlopen_ok=1 batch_sym=1 init_ok=1 wide1=1(ctas=40) wide0=1(ctas=80) numGpus=1 init_s=0.20 ta0=1280 tb0=80 arena0=24.88GiB free0=6.16GiB`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`e075ee9ac1b28601d9a31563f42b47ec9c4a121163224e23c4c13828bf58db37`，`cuda_sha256`：`0855979b7db8196cf8532349037db8bda0197aee8e324fc6d0598d23d78e6ef2`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与分段剖析（`2026-11-25T13:48:03.99 -> 14:07:57.52`，仅隔离使用空闲 `GPU 0` [1×V100]；期间 GPU 1/2/3 运行着 `topogeonet` 训练任务至 ~`13:56`，`R1–R4` 受共享 CPU `load avg 32.2` 争抢，`status="passed", stage="complete"`）**：
  - **尾盘、后处理与线长验证成功**：
    - **`60` 轮干净收敛（`R53–60 = 3.82 s`, `fb=0`, `over=0`）**：较 `v56` `R53–82 = 445.51 s` 直砍 **`-441.69 s`**；
    - **`F4_V37_CHECKPIPS fast=true = 1.30 s`**（刷新历史最低，较 `v53 8.12 s` 快 `6.2×`、较 `v55 14.95 s` 快 `11.5×`）；
    - **`Total wirelength = 18,955,186`**（`v34` 以来历史最低 WL）；
    - `Initialization = 21.23 s`（`init_s = 3.24 s`、`F4_V57_PORTALS = 0.45 s`、`F4_V57_SORT = 1.38 s`、`IDMAP = 0.926 s`）、`materialize = 13.68 s`（`export=0.75 cache=4.97 bind=3.74 users=4.22`）、`F4_V57_POST`（`assign=2.09 s, fix=7.68 s, srcPin=1.95 s`）、`SETPIPS = 7.75 s`、`STATS_WL = 9.12 s`、`Write EDIF & XDEF = 12.965 s`、`Write XDC = 0.644 s`。
  - **受单卡与前期 CPU 争抢影响的分段对比**：
    - `R1–2 = 117.15 s`（1×V100：`taWarps=1068, tbCtas=76, init_s=0.87s` vs `v56` 4×V100 `63.46 s`）、`R3–5 = 290.97 s`（`R3` 前段遇 `load avg 32.2` 致 `p1=135.66 s` vs 无争抢 `v56 48.84 s`）、`R6–52 = 452.45 s`、`R53–60 = 3.82 s`；`sum_cpp = 864.39 s`（`p1=461.47 s, p2=83.45 s, p3=318.39 s`, `gs=364.09 s`, `wide_ok=38, ov=121, gpu_fb=8`, `host=18.07 s [meta=7.23 s]`）、`Nodes popped = 23,959,036,539`（`gpu=16.223B, cpu=7.736B`）；
    - **`Route design = 984.97 s`**（`Init 21.23 s + Routing 963.74 s`）、**`route_wall = 1,093.81 s`**、**`E2E = 1,193.53 s`（`19m 53.53s`）**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31083242`**（`read=88.66 s, pins=3.12 s, pip_shards=2.76 s, unrouted_scan=0.68 s`），`dcp_sha256`：`3b618e98afda8117be3221a626820e12f651635858c003f67f3e61d46b6a78cb`。

### 2.38 `theia50per-ppt-hybrid-v58`（`3×V100` 下 **`R6–52 = 430.18 s` 优于 `v56` `4×V100` `439.03 s`（`-8.85 s`）** + `dcp_legality PASS`；`zeroCopyScope` 兑现中段提速，尾盘 `R56–81` 暴露 `fs < 4` 与 `escNbr` 无上限扩张 bug 耗 `421.82 s`）
- **改动文件**：`scratch/v58/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v58_patch.py`、`prepare_v58.py`、`deploy_v58.py`、`launch_v58.py`、`record_rounds_v58.py`。
- **核心改动（基于 `v57`）**：
  - `fides_cpu.cpp` 第 859 行：`bool useZeroCopyScope = sc.isFlat;`（对全量扁平作用域均启用零拷贝，消除 `65..512` 节点区间 `sc.slots -> S.ov` 的每连接内存拷贝，直接走 L1 驻留 `sc.bloom + tinyOv`）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V58_SMOKE_PASS dlopen_ok=1 batch_sym=1 init_ok=1 wide1=1(ctas=40) wide0=1(ctas=80) numGpus=3 init_s=0.66 ta0=1024 tb0=80 arena0=23.31GiB free0=7.73GiB`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`8932047380c3cdbf52efc56792d73c3cd27afe3105f062b93fc2f34a4bd32499`，`cuda_sha256`：`3d757300000e429b038bda14c5e5bdb0e76b8d1f27976abaec6f3f171452957c`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与分段剖析（`2026-11-25T14:11:38.53 -> 14:34:25.65`，隔离运行于空闲 `3×V100 [GPU 0/1/3]`，`taWarps=3072, tbCtas=240, init_s=1.38`；GPU `2` 由 `lqwu PID 313089` 占用 `456–516 MiB / 22–32%` 被自动排除，`status="passed", stage="complete"`）**：
  - **前中段与 `zeroCopyScope` 实证**：
    - `R1–2 = 68.84 s`（`R1 = 34.73 s`, `R2 = 34.11 s`）、`R3–5 = 129.38 s`（`R3 = 53.65 s [p1=52.49s]`, `R4 = 41.90 s [p1=38.21s, 历史最低 R4 p1]`, `R5 = 33.83 s [p1=22.08s]`）；前 5 轮仅比 `v56` 4 卡多 `+11.17 s`（少 1 张 V100）；
    - **`R6–52 = 430.18 s`**：即使仅 3 张 GPU，`R6–52` 依然反超 `v56`（4 卡 `439.03 s`）**`-8.85 s`**，`R1–52 = 628.40 s`（`p1=249.37 s, p2=43.36 s, p3=334.58 s, gs=266.01 s, host=14.27 s`）；
    - `Initialization = 23.11 s`（`Read = 35.135 s`, `PRE f2=4.79 directPre=0.51`, `SORT = 1.47 s`, `IDMAP = 0.849 s`, `PORTALS = 0.37 s`, `CPU init_s = 3.51 s`）、`materialize = 13.89 s`（`export=0.75 cache=5.07 bind=3.57 users=4.50, seq_fix=0`）、`F4_V58_POST`（`assign=1.77 s, fix=8.09 s, srcPin=1.95 s`）、`SETPIPS = 7.85 s`、`CHECKPIPS = 1.96 s`、`STATS_WL = 8.23 s`、`Write EDIF & XDEF = 14.133 s`、`Write XDC = 0.798 s`。
  - **尾盘退化精确定位（`R53–86 = 421.82 s`）**：
    - `R53–55 = 0.39 s`（`over=7->4->5`），但 **`R56–77` 共 22 轮在 `over=2..9, fb=1..4` 间空等了 `316.98 s`**（原因：`fs` 在交替失败中始终 `< 4`，无法提前触发 `routeIter >= 60 && fs >= 4` 的 `budget=0`）；
    - 到达第 78 轮触发 `routeIter >= 78` 后，`if (rc <= 0 && (escNbr || fs >= 1u))` 使 `escNbr` 拉入的 **~`96` 条邻域非活跃线（`fs=0`）只要未命中 `2M+4M` 即全部切入无上限 `budget=0 / padRelax=-1`**，导致 **`R78–81` 每轮扩展 `14.9M–47.7M` pops、耗时 `10.87–37.46 s`**（`R78–86 = 104.45 s`），最终 **`86` 轮收敛至 `over=0`**。
  - **全口径汇总**：`sum_cpp = 1,050.22 s`（`p1=249.37 s, p2=43.36 s, p3=756.05 s`, `gs=266.01 s`, `corr=13.26 s`, `host=14.27 s [meta=5.60 s]`, `wide_ok=61, gpu_ov=172, gpu_fb=10`），`WL = 19,078,918`，`Nodes popped = 25,578,565,150`（`gpu=17,211,201,358, cpu=8,367,363,792`），`Route design = 1,174.72 s`（`Init 23.11 s + Routing 1,151.61 s`），`route_wall = 1,276.73 s`（`14:11:38.53 -> 14:32:55.26`），`E2E = 1,367.12 s`（`14:11:38.53 -> 14:34:25.65`, `22m 47.12s`）；
  - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31163199`**（`read=81.41 s, pins=2.70 s, pip_shards=1.29 s, unrouted_scan=0.60 s`），`dcp_sha256`：`c141797b7cb8590a22e2ecc749670c5924dd4481fd348766df513f4ded0eb73f`。

### 2.39 `theia50per-ppt-hybrid-v59`（`73` 轮收敛 + `dcp_legality PASS`；尾盘较 `v58 -159.20 s`，`Route = 1,103.81 s / E2E = 1,298.53 s`，定位 `!isAct` 误计 `fallbackCount` 周期循环与 `R6–52` 波动）
- **改动文件**：`scratch/v59/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v59_patch.py`、`prepare_v59.py`、`deploy_v59.py`、`launch_v59.py`、`record_rounds_v59.py`。
- **核心改动（基于 `v58`）**：
  - `fides_cpu.cpp` 第 3909–3935 行：将 `!isAct` 非活跃邻域线严格限制为有界 `6M / pad=48`（禁入 `16M` 与 `budget=0 / padRelax=-1`），并将 `isAct` 活跃冲突线的 `16M / -1` 与 `budget=0 / -1` 分别提前至 `routeIter >= 57 && fs >= 2u`、`routeIter >= 60 && fs >= 3u` 与 `routeIter >= 64 && activeCount <= 24`（`routeIter >= 74` 兜底）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V59_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1`（`CUDA_VISIBLE_DEVICES=0,1,3`）；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`16f9688fe3f62c7c26afecb336175929ff0a3c1eeee4ad4b8ed89ad5fa7e0c98`，`cuda_sha256`：`c4e971025b1df3691e6dc1ba5c41ef11ece2fb08a95321dd9922f9ab421b319f`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与分段剖析（`2026-11-25T14:43:47.28 -> 15:05:25.82`，隔离运行于空闲 `3×V100 [GPU 0/1/3]`，`taWarps=3072, tbCtas=240, init_s=1.39`；GPU `2` 由 `lqwu PID 313089` 占用 `512–516 MiB / 20–32%` 被自动排除，`status="passed", stage="complete"`）**：
  - **尾盘收益**：`R53–73 = 262.62 s`（较 `v58` `421.82 s` 缩短 **`-159.20 s`**，收敛轮次由 `86` 轮降至 **`73`** 轮），消除 `!isAct` 邻域 `R78–80` 暴走；
  - **其余分段**：
    - `R1–2 = 71.79 s`（`R1 = 36.02 s, R2 = 35.77 s`）、`R3–5 = 136.86 s`（`R3 = 55.21 s, R4 = 46.01 s, R5 = 35.64 s`）、`R6–52 = 511.41 s`（较 `v58 430.18 s` 因 GPU/波次调度与 `lqwu` CPU 伴随负载波动 `+81.23 s`，其中 `gs` 从 `266.01 s` 升至 `324.76 s`）、`R1–52 = 720.06 s`；
    - `Initialization = 21.66 s`（`Read = 34.277 s`, `PRE f2=5.21 directPre=0.50`, `SORT = 1.42 s`, `IDMAP = 0.896 s`, `PORTALS = 0.44 s`, `CPU init_s = 3.26 s`）、`materialize = 14.04 s`（`seq_fix=0`）、`F4_V59_POST`（`assign=1.57 s, fix=7.75 s, srcPin=1.87 s`）、`SETPIPS = 7.45 s`、`CHECKPIPS = 2.17 s`、`STATS_WL = 7.37 s`、`Write EDIF & XDEF = 16.833 s`、`Write XDC = 0.673 s`；
  - **尾盘仍可压缩的精确根因（`R64–73` 出现 `R67→68→69 == R70→71→72` 周期-3 空转）**：
    1. `!isAct` 非活跃邻域线（已合法复原 `savedPaths`）失败时同样执行 `++fallbackCount`，误把 `G.lastTailFbCount` 置为 `1` 并在下一轮强行触发 `escNbr`（拉入 `96` 条邻居白跑 `6–12M` pops）；
    2. `routeIter >= 64 && activeCount <= 24` 仍被包在 `if (rc <= 0 && (escNbr || fs >= 1u))` 内，`escNbr=false && fs=0` 时无法当轮直接逃逸（必多耗 `R67+R68` 两轮 `13.5 s` 才进 `R69`），且 `budget=0, padRelax=-1` 单次无界全片搜索仍耗 `~20.7M pops / 16 s`。
  - **全口径汇总**：`sum_cpp = 982.68 s`（`p1=265.38 s, p2=46.79 s, p3=669.10 s`, `gs=324.76 s`, `corr=13.30 s`, `host=14.59 s [meta=5.61 s]`, `wide_ok=58, gpu_ov=183, gpu_fb=5`），`WL = 19,088,894`，`Nodes popped = 26,277,557,973`（`gpu=17,582,647,746, cpu=8,694,910,227`），`Route design = 1,103.81 s`（`Init 21.66 s + Routing 1,082.15 s`，较 `v58 -70.91 s`），`route_wall = 1,205.76 s`（`14:43:47.28 -> 15:03:53.04`），`E2E = 1,298.53 s`（`14:43:47.28 -> 15:05:25.82`, `21m 38.53s`）；
  - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31168330`**（`read=82.08 s, pins=3.01 s, pip_shards=2.15 s, unrouted_scan=0.62 s`），`dcp_sha256`：`4d352b7854af120b12846f734b03ea533a4f363e8a708fd8fb32176b7804afa1`。

### 2.40 `theia50per-ppt-hybrid-v60`（`2×V100` 运行至 `R81` 止损归档：`R62` 放行 `16M/256 + 0/-1` 串行叠加致尾盘放大至 `R53–81 = 887.27 s`，精确确认反面结论）
- **改动文件**：`scratch/v60/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v60_patch.py`、`prepare_v60.py`、`deploy_v60.py`、`launch_v60.py`、`record_rounds_v60.py`。
- **核心改动（基于 `v59`）**：
  - `fides_cpu.cpp` 第 3909–3969 行：将 `fallbackCount / tailFailStreak` 限制仅对 `isAct` 累加；同时让 `isAct && routeIter >= 62 && activeCount <= 24` 直接放行进入 `16M / pad=256`，失败后再串联进入 `budget=0 / padRelax=-1`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V60_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1`（`CUDA_VISIBLE_DEVICES=0,1`）；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`d67578d254f5ed81d5c0f2b6858fdd9613f90b5474937669be7d3bb2288d4690`，`cuda_sha256`：`65ca89332723b4cd4430c1e393a79c18890078fbd80329fe26dfba093f115761`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **实测与止损定位（`2026-11-25T16:28:57.46+08:00` 启动，自动排除占卡 `GPU 2/3` [`lqwu` 516/460 MiB] 后隔离运行于 `2×V100 [GPU 1/0]`，`taWarps=2048, tbCtas=160, init_s=1.32`；运行至 `R81` 完成、耗时 `~29m30s` 后干净止损并释放 `GPU 0/1` 与 `/dev/shm`）**：
  - **前期与中段实测（`2×V100`）**：
    - `Read Netlist & PhysDB = 39.685 s`、`F4_V60_PRE f2=6.08 directPre=0.51`、`SORT = 3.11 s`、`IDMAP = 0.996 s`、`PORTALS = 0.45 s`、`CPU init_s = 3.18 s`；
    - `R1–2 = 84.19 s`（`R1=45.08 s, R2=39.11 s`）、`R3–5 = 157.50 s`（`R3=63.42 s, R4=50.16 s, R5=43.92 s`）、`R6–52 = 489.44 s`，`R1–52 = 731.13 s`（`p1=316.65 s, p2=54.77 s, p3=358.50 s, gs=307.13 s`）。
  - **关键负向实证（为何 `R53–81` 恶化至 `887.27 s`）**：
    1. **双阶串联惩罚（`16M/256 + 0/-1`）**：对过 `pad=256` 边界的难点线先烧 `16M` 再紧接无上限 `0/-1`，单线膨胀翻倍至 `36M+` pops，把 `R62–66` 单轮拖慢至 `64.74 s / 44.28 s / 43.50 s / 88.84 s / 109.33 s`、`R80–81` 拖慢至 `76.35 s / 97.78 s`；
    2. **全活跃线无界并发拥塞踩踏**：`routeIter >= 62 && activeCount <= 24` 同时对全部 `isAct` 线（而非逐条或仅 `fs >= 2` 顽固线）放开无上限逃逸，造成 `overused` 反弹跳变（`R62: 7→21`、`R64: 5→17`、`R79: 3→14`）；
    3. **屏蔽 `!isAct` `fallbackCount` 关断了 `escNbr`**：`R72–77` 连续 6 轮 `fb=0` 且 `over=3↔4`，因 `escNbr=false` 仅撕拆 `48` 条近邻（未触发 `96` 条），未能撬开局部环路直至 `R78` 强制放行。

### 2.41 `theia50per-ppt-hybrid-v61`（**`2×V100` 下刷新全局 `Route design = 800.99 s` 纪录！`58` 轮收敛、`R53–58 = 1.89 s` 历史最快尾盘、`WL = 18,999,540` + `dcp_legality PASS`**）
- **改动文件**：`scratch/v61/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v61_patch.py`、`prepare_v61.py`、`deploy_v61.py`、`launch_v61.py`、`record_rounds_v61.py`。
- **核心改动（基于 `v59` 干净基座，弃用 `v60` 双预算串联）**：
  1. `fides_cpu.cpp` 第 3774–3793 行：`widenNbr = escNbr || (routeIter >= 58 && G.overusedList.size() <= 12u && activeCount <= 24)`，当尾盘残余拥塞 `<= 12` 时将近邻旁路拆线规模放宽至 `nbrLim = 128u`（`maxNbrNet=128, maxNbrPops=120000`），在不触发虚假 `fb` 的前提下直接撬开闭环；
  2. `fides_cpu.cpp` 第 3883–3963 行：彻底禁入 `!isAct` 非活跃邻域的 `6M` / 无界回退与 `fallbackCount / tailFailStreak` 累加；同时保持 `v59` 单次分支结构（绝不叠加双高预算），并将 `budget=0 / padRelax=-1` 限流为 `maxUnboundedThisRound = (routeIter >= 78) ? 8 : 1`（`R60–77` 每轮至多 `1` 条活跃顽固线）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V61_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1`（`CUDA_VISIBLE_DEVICES=0,1`）；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`bb878f69e6204ede05bf445560aac2b310f5756636ec7e060442c9bb576e7824`，`cuda_sha256`：`7a2d986146a0a2249c34f992298331c569b323ab20ea301bfd5a1aa79d5d1735`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T18:00:36.95 -> 18:17:22.75`，隔离运行于空闲 `2×V100 [GPU 0/1]`，`taWarps=2048, tbCtas=160, init_s=1.34`；GPU `2/3` 上 `lqwu` 占用各 `516 MiB` 被自动排除，`status="passed", stage="complete"`）**：
  - **突破性成果**：
    - **`Route design = 800.99 s`**（`Init 25.07 s + Routing 775.92 s`）：在**少 2 张 GPU（仅 `2×V100`）** 的条件下直接打破 `v53`（`4×V100`, `802.97 s`）保持的全局最优合法纪录（`-1.98 s`）！
    - **`58` 轮干净收敛，`R53–58 = 1.89 s` 创历史最快尾盘纪录**（`R53=0.22 s, R54=0.14 s, R55=0.07 s, R56=1.31 s, R57=0.07 s, R58=0.08 s`，全程 `fb=0`，`R58` 借助 `widenNbr=128` 一举将 `over=1→0`）；
    - **`Total wirelength = 18,999,540`**（重回 `< 19.0M`），**`Nodes popped = 23,510,674,193`**（`gpu=16,422,933,423, cpu=7,087,740,770`）；
    - **`F4_V37_SETPIPS = 6.12 s`**（全库新低）、`F4_V37_CHECKPIPS = 1.80 s`、`F4_V37_STATS_WL = 5.33 s`。
  - **其余全口径分段指标**：
    - `Read Netlist & PhysDB = 34.827 s`、`PRE f2=5.14 directPre=0.48`、`SORT = 1.36 s`、`IDMAP = 0.880 s`、`PORTALS = 0.45 s`、`CPU init_s = 3.66 s`、`materialize = 13.64 s`（`seq_fix=0`）、`F4_V61_POST`（`assign=1.72 s, fix=7.21 s, srcPin=1.78 s`）、`Write EDIF & XDEF = 23.250 s`、`Write XDC = 0.424 s`；
    - `R1–2 = 79.57 s`（`R1=41.86 s, R2=37.71 s`）、`R3–5 = 137.67 s`（`R3=56.80 s, R4=42.60 s, R5=38.27 s`）、`R6–52 = 462.46 s` → `R1–52 = 679.70 s`，`sum_cpp = 681.59 s`（`p1=241.79 s, p2=53.71 s, p3=385.02 s, gs=302.11 s, corr=13.38 s, host=14.46 s [meta=5.27 s], wide_ok=36, gpu_ov=125, gpu_fb=11`）；
    - **`route_wall = 907.90 s`**（`18:00:36.95 -> 18:15:44.86`）、**`E2E = 1,005.80 s`（`16m 45.80s`，`18:00:36.95 -> 18:17:22.75`）**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31130515`**（`read=86.97 s, pins=3.08 s, pip_shards=2.28 s, unrouted_scan=0.82 s`），`dcp_sha256`：`945c057208835c3e2b633df2709c8f7aee00352029b9267cc4e8d0e62526485a`。

### 2.42 `theia50per-ppt-hybrid-v62`（**`3×V100` 下实现全口径破纪录！`R1–52 = 514.70 s`（狂降 `-111.38 s`）、`R6–52 = 320.71 s`、`Route = 778.10 s`、`E2E = 975.83 s`、`WL = 18,910,733` + `dcp_legality PASS`**）
- **改动文件**：`scratch/v62/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v62_patch.py`、`prepare_v62.py`、`deploy_v62.py`、`launch_v62.py`、`record_rounds_v62.py`。
- **核心改动（基于 `v61`，对标 PPT 中段架构 4 处精准重构，全部位于 `fides_cpu.cpp`）**：
  1. **修复 `R6–21` 走廊清零回退 & 提前单调放宽**（`nativeUpdateHistAndFeedback` 第 4174–4235 行）：`routeIter >= 6`（`midOrTail`）起取消 `!cong` 时把 `connWiden[i]` 清零退回窄 `F1Corridor` 的行为，改用 `consecCongested[i] >>= 1` 保持已解锁宽度单调不缩，并将 `R6–13` 放宽阶梯调为 `cc >= 1/2/4/6`，同时将 `tailPhase` 提前至 `routeIter >= 14 || overused <= 2200`；
  2. **重标定 `Pass 3` GPU/CPU 分界与 `Tier-B1 Wide` / `bypassNet` 门槛**（第 3734–3742、4010–4072 行）：`Pass 3` 入 GPU 阈值提至 `384`（`< 3` 卡 `320`），`Tier-B1 Wide` 收紧为 `mode=1 (16M) >= 6`、`mode=2 (32M) >= 8`，将 `cpuOverlap` 整网切走门槛调至 `>= 12.0M` 且限定 `netPass3Cnt <= 16 || netUltraCnt >= 2`；
  3. **压缩中段波次快照陈旧度与同网冲突**（第 2326、2888–2897 行）：GPU `Pass 3` `targetWave` 调为 `1024`（`< 3` 卡 `512`）、`Pass 2` `curItems <= 4096` 时降为 `1024`；CPU `Tier 2` 空间不重叠扫描深度扩至 `checked < 24`；
  4. **恢复 `Pass 2` PPT `HPWL_FIRST` 顺序**（第 2080–2082、4010–4011 行）：`Pass 2` 恢复按 `aIdx` 升序出堆（短连接优先疏通主干），仅 `Pass 3` 保留 `-lastPops` 降序 LPT。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V62_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1 hist_sym=1 all_syms=1`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`28ad1cd50400d810c172f42fe8fe72a1f30e02a177fdf013a930476ff3fc7a59`，`cuda_sha256`：`0b60abf6ace994bf0317ef9349caf66f33f49977d2441f8d219d6583a1a54feb`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T19:09:16.72 -> 19:25:33.65`，隔离运行于空闲 `3×V100 [GPU 0/1/3]`，`taWarps=3072, tbCtas=240, init_s=1.39`；GPU `2` 由 `PID 313089` 占用 `516 MiB / 15–29%` 自动排除，`status="passed", stage="complete"`）**：
  - **突破性前中段提速（全面兑现 4 项 PPT 中段对标改动）**：
    - **`R1–52 = 514.70 s`（狂刷全局最优！较 4 卡 `v56 626.08 s` 暴降 `-111.38 s`，较 `v61 679.70 s` 降 `-165.00 s`）**；
    - **`R6–52 = 320.71 s`**（较 `v61 462.46 s` 砍掉 **`-141.75 s`**、较 `v58 430.18 s` 降 `-109.47 s`：`R7 over=5,857` vs `v61 10,540`、`R10 over=2,356` vs `5,896`、`R15 over=953` vs `3,309`、`R19` 起 `GPU = 0.00 s` 纯靠 35 核快速收敛、`R23` 由 `28.52 s` 降至 `3.14 s`）；
    - **`R1–2 = 66.36 s`**（`R1=33.89s, R2=32.47s`, `gs=50.70s`）、**`R3–5 = 127.63 s`**（`p1=112.52s, p2=10.12s, p3=4.85s, gs=13.69s`）；
    - **`Total wirelength = 18,910,733`**（创 `v34` 以来最低记录，较 `v61 -88,807`），`Nodes popped = 26,036,110,218`（`gpu=18,512,726,354, cpu=7,523,383,864`）。
  - **其余分段与端到端汇总（含尾盘 `R53–68` 单点定位）**：
    - `Read Netlist & PhysDB = 39.454 s`、`PRE clk=0.00 static=0.21 preEst=0.00 f2=6.41 directPre=0.53`、`SORT = 2.91 s`、`IDMAP = 0.940 s`、`PORTALS = 0.45 s`、`CPU init_s = 3.35 s`、`materialize = 13.68 s (seq_fix=0)`、`F4_V62_POST (assign=1.83 s, fix=8.03 s, srcPin=1.98 s)`、`SETPIPS = 6.65 s`、`CHECKPIPS = 1.75 s`、`STATS_WL = 3.80 s`、`Write EDIF & XDEF = 21.126 s`、`Write XDC = 0.701 s`；
    - `sum_cpp = 657.54 s`（`p1=223.04 s, p2=43.05 s, p3=390.32 s, gs=229.75 s, corr=12.92 s, host=13.89 s [meta=5.13 s], wide_ok=27, gpu_ov=94, gpu_fb=25`）；
    - **尾盘仍遇 2 线交替 `fs=0` 乒乓拖慢 `R53–68 = 142.84 s`**：`R55–58` 解到 `over=1` 后，`R59–64` 两根 `act=2` 活跃线轮流成功/清零 `tailFailStreak`（每轮回落 `fs=0 -> 2M+4M+6M = 12M pops / ~9.5 s`、`fb=1`），至 `R65–67` 耗 `44.06 s`、最终 **`68` 轮收敛至 `over=0`**；即使带着这 `~141 s` 尾盘波动，仍全面刷新全局最优：
    - **`Route design = 778.10 s`**（`Init 21.91 s + Routing 756.19 s`，较 `v61 -22.89 s`），**`route_wall = 883.42 s`**（较 `v53 -12.21 s`、较 `v61 -23.01 s`），**`E2E = 975.83 s`（`16m 15.83s`，较 `v53 -10.91 s`、较 `v61 -28.87 s`）**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31045493`**（`read=82.20 s, pins=2.57 s, pip_shards=1.82 s, unrouted_scan=1.05 s`），`dcp_sha256`：`0d739e48d1db5a2981127ce0fa74b5800b1531be1d66b8a78a425ac92bb7c36a`。

### 2.43 `theia50per-ppt-hybrid-v63`（**`R1–52 = 508.66 s`（再刷历史最优！`R6–52 = 305.62 s`）、`WL = 18,906,071` 新低 + `dcp_legality PASS`；验证消除 `fb=1` 令牌抢占，定位 `R58–77` `2M+4M` 先跑 + `20.8M` 无界重复空转**）
- **改动文件**：`scratch/v63/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v63_patch.py`、`prepare_v63.py`、`deploy_v63.py`、`launch_v63.py`、`record_rounds_v63.py`。
- **核心改动（基于 `v62`，仅修改尾盘求解器 `fides_cpu.cpp` 第 3883–3966 行）**：
  1. **防 `unboundedUsedThisRound` 令牌抢占**：仅当 `pops3 > 4,000,000` 时才计入 `unboundedUsedThisRound`，防止先跑的 `~7k` 轻线抢占 `0/-1` 配额；
  2. **放宽微尾双线逃逸窗口**：`maxUnboundedThisRound` 在 `routeIter >= 58 && activeCount <= 8` 时设为 `2`，并将无界许可提前至 `routeIter >= 58 && over <= 4 && activeCount <= 16` 与 `routeIter >= 59 && fs >= 2 && activeCount <= 24`；
  3. **滞留连败衰减**：`routeIter >= 58 && activeCount <= 16 && lastTailFbCount > 0 && fs >= 2` 时成功只做 `fs >>= 1`，防止单轮成功彻底清零 `fs`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V63_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1 hist_sym=1 all_syms=1`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`3b1e9274d9cf270bcedbca992892ff84d7dcdcef4ad87d6533d24d046a78c84f`，`cuda_sha256`：`d0e45a675237a6480124ccce40ccf04a98d843e29feb57700e094fa8ceb0d4b6`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T19:33:51.44 -> 19:50:48.21`，隔离运行于空闲 `3×V100 [GPU 0/1/3]`，`taWarps=3072, tbCtas=240, init_s=1.43`；GPU `2` 由 `PID 313089` 占用 `516 MiB / 22–27%` 自动排除，`status="passed", stage="complete"`）**：
  - **前中段与线长再破历史最佳**：
    - **`R1–52 = 508.66 s`**（较 `v62 514.70 s` 再快 `-6.04 s`、`R6–52 = 305.62 s` 较 `v62 -15.09 s`：`R1–2 = 69.22 s`, `R3–5 = 133.82 s`, `R6–52 = 305.62 s`, `p1=228.29 s, p2=41.51 s, p3=237.69 s, gs=232.20 s, corr=13.59 s, host=14.34 s [meta=5.38 s], wide_ok=23, ov=79, fb=12`）；
    - **`Total wirelength = 18,906,071`**（再破 `v62 18,910,733`，`v34` 以来最低）、`Nodes popped = 26,465,937,350`（`gpu=18,840,488,006, cpu=7,625,449,344`）。
  - **尾盘表现定位（`R53–81 = 187.50 s`）**：
    - `R58` 起全部 `fb = 0`（成功消除 `v62` 的 `fb=1` 失败循环），但 `R58/60/63/67/68` 触发了 `5` 轮 `20.7M–37.9M` 无界搜索（仅此 5 轮耗 **`80.0 s`**），且 **`R69–77`（9 轮 `over=1↔2`）每轮均在 `fs=0` 下先跑 `2M+4M = 6.00M` 撞 `pad=48` 墙，再靠 `0/-1` 多绕 `25k` 成功**（白耗 `~35 s`），至 `R81` 清零收敛（`8,969 pops, 0.04 s`）；
    - **其余全口径**：`Read Netlist & PhysDB = 33.841 s`、`PRE f2=6.25 directPre=0.62`、`SORT = 2.72 s`、`IDMAP = 0.935 s`、`PORTALS = 0.47 s`、`CPU init_s = 3.38 s`、`materialize = 14.40 s (seq_fix=0)`、`F4_V63_POST (assign=1.79 s, fix=7.47 s, srcPin=2.05 s)`、`SETPIPS = 6.11 s`、`CHECKPIPS = 1.75 s`、`STATS_WL = 4.23 s`、`Write EDIF & XDEF = 19.947 s`、`Write XDC = 0.446 s`；`sum_cpp = 696.16 s`、**`Route design = 825.80 s`**（`Init 27.65 s + Routing 798.15 s`）、**`route_wall = 924.26 s`**、**`E2E = 1,015.66 s`**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31042285`**（`read=80.79 s, pins=3.01 s, pip_shards=2.17 s, unrouted_scan=0.79 s`），`dcp_sha256`：`949260e96dfb181e6b5e22e868bdfe829a9ca4854d5d06d190182441e6f28f82`。

### 2.44 `theia50per-ppt-hybrid-v64`（**`3×V100` 下 `73` 轮 `over=0` + `dcp_legality PASS`；验证尾盘邻域修复与跳盒有效但 `2.5×–4.0×` 尾盘历史加倍 + `192` 扩邻引入次生反弹，`R53–73 = 139.16 s`、`Route = 881.71 s`、`E2E = 1,067.38 s`**）
- **改动文件**：`scratch/v64/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v64_patch.py`、`prepare_v64.py`、`deploy_v64.py`、`launch_v64.py`、`record_rounds_v64.py`。
- **核心改动（基于 `v63`，仅修改尾盘逻辑 `fides_cpu.cpp` 第 3789–3975、4171–4184 行）**：
  1. **修复冻结无辜邻居漏洞**：第 3897 行由 `if (fs == 0u)` 改为 `if (!isAct || fs == 0u)`，避免此前当过活跃线且有 `fs>0` 的邻域连接被静默跳过（邻居探针上限 `1M/pad=12 -> 2M/pad=24`）；
  2. **跳过低效 `2M(pad=24)+4M(pad=48)` 双撞墙**：`fastTail`（`routeIter >= 56 && activeCount <= 24`）下 `isAct && fs==0` 仅先做 `500k/pad=24`，未命中且需升级时直接跳过 `4M/pad=48` 进入 `6M/pad=128`（`!canEscalate` 则 `2M/pad=96`）；同时收回 `v63` 的早熟 `0/-1`，仅保留 `routeIter >= 72 || (routeIter >= 60 && fs >= 3)` 且保持 `pops3 > 4M` 才扣 `unboundedUsedThisRound` 配额，并限制 `fs>=2 && R57+` 为有界 `12M/-1`；
  3. **小尾盘历史放大与扩邻**：`routeIter >= 56 && over <= 12` 对残余拥塞节点施加 `tailHistMult = 2.5f`（`R60+ && over <= 4` 为 `4.0f`），并当 `routeIter >= 56 && over <= 8 && activeCount <= 24` 时放宽邻居收集上限至 `maxNbrNet=256, maxNbrPops=250k, 6×12, nbrLim=192`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V64_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1 hist_sym=1 all_syms=1`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`d84afa34b033860e110cdca7a463a8575533e96aca3a075d7b06cd3227f03174`，`cuda_sha256`：`71149755aa1fe5e75e4a391350919d1fc0654c0284c091ee2fd20cbdb0861718`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T20:03:25.88 -> 20:21:14.61`，隔离运行于 `3×V100 [GPU 3/0/1]`，`taWarps=3072, tbCtas=240, init_s=1.67`；外部 `PID 313089` 仍占 `GPU 2` 被自动排除，`status="passed", stage="complete"`）**：
  - **尾盘对比定位（`R53–73 = 139.16 s` vs `v62 142.84 s` / `v63 187.50 s`）**：
    - 在 `R55–60` 将早段乒乓压到 `3.61–6.87 s/轮`（`R60` 已压至 `over=1`）；但 **`tailHistMult = 2.5×/4.0×` 叠加 `nbrLim=192` 扩邻** 导致 `R58 (over 2→13)`、`R61 (over 1→6, 18.51M)`、`R62–63 (fb=3/4, 22.53M/26.09M)` 与 `R68 (over 1→8, 23.76M)` 连续出现拥塞外溢反弹，拉长至 `R73`（`128,874 pops, 0.11 s`）才清零。
  - **前中段运行波动**：尽管 `R1–54` 代码与 `v63` 相同，受并发动线竞态路径差异（`R7/R9/R12` 重网落入 CPU `p3` 分别为 `17.06 s / 27.17 s / 27.82 s`）影响，本轮 `R1–52 = 618.83 s`（`R1–2 = 70.04 s, R3–5 = 141.62 s, R6–52 = 407.17 s`）。
  - **全口径汇总**：
    - `Read Netlist & PhysDB = 35.383 s`、`SORT = 1.16 s`、`IDMAP = 0.838 s`、`PORTALS = 0.41 s`、`CPU init_s = 3.25 s`、`materialize = 13.68 s (seq_fix=0)`、`F4_V64_POST (assign=2.19 s, fix=8.17 s, srcPin=2.02 s)`、`SETPIPS = 8.80 s`、`CHECKPIPS = 1.38 s`、`STATS_WL = 4.70 s`、`Write EDIF & XDEF = 11.707 s`、`Write XDC = 0.765 s`；
    - `sum_cpp = 757.99 s`（`p1=269.33 s, p2=43.68 s, p3=443.90 s, gs=254.86 s, corr=13.29 s, host=14.58 s [meta=5.51 s], wide_ok=35, gpu_ov=149, gpu_fb=7`）、`Total wirelength = 18,933,976`、`Nodes popped = 28,134,847,182`；
    - **`Route design = 881.71 s`**（`Init 22.60 s + Routing 859.10 s`）、**`route_wall = 978.29 s`**、**`E2E = 1,067.38 s`**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31063410`**（`read=78.38 s, pins=2.65 s, pip_shards=2.51 s, unrouted_scan=0.60 s`），`dcp_sha256`：`da2a67457b788a794b64b998f3fe48ed7657d6a7715a9b278ff18929dc144165`。

### 2.45 `theia50per-ppt-hybrid-v65`（**`4×V100` 下 `R1–52 = 511.21 s`（`gs=192.47 s` 4 卡提速实证）、`81` 轮 `over=0` + `dcp_legality PASS`；精准定位 `500k(pad=24)→6M(pad=128)` 跳盒是 `R64–69 fb=3..5` 的共同反面根因**）
- **改动文件**：`scratch/v65/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v65_patch.py`、`prepare_v65.py`、`deploy_v65.py`、`launch_v65.py`、`record_rounds_v65.py`。
- **核心改动（基于 `v64` 纯化，仅修改尾盘逻辑 `fides_cpu.cpp` 第 3790–3945、4170–4180 行）**：
  1. **移除 `v64` 两项回弹诱因**：恢复 `tailHistMult = 1.0f`（取消 `2.5×/4.0×` 尾盘历史加倍），并恢复邻域收集上限为 `v61–v63` 原规格（`nbrLim = widenNbr ? 128u : 48u`, `maxNbrNet=128`, `maxNbrPops=120k`）；
  2. **保留 `v64` 其余尾盘改动并加厚邻域备用预算**：保留 `!isAct || fs == 0u`（防冻结邻居，且将邻域第 2 级备选扩为 `4M/pad=48`）、保留 `fastTail` 下活跃线 `500k/pad=24` 截断直跳 `6M/pad=128`（`fs=1` `8M/128`, `fs>=2 && R57+` `12M/-1`），并将 `allowUnbounded` 收紧至 `R74+ || (R60+ && fs >= 3)`（`maxUnbounded = (R78+) ? 8 : 1`、`pops3 > 4M` 配额门槛）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V65_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1 hist_sym=1 all_syms=1`；`worker_sha256`：`5b4d2690d20ad13d885500b5c07bf42adcba291f6d8b98a62ff5b7628ef69a25`，`cpp_sha256`：`bbbb971aff8f88cfe429d45b8ea55fde298d3e62aa3ca578b66bb799765a49c0`，`cuda_sha256`：`e4ddc2fbce8d55b1b34c71d895cb274c725823bc4eea5b614cf47b4bd8de0ef3`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T20:33:34.75 -> 20:54:01.79`，`GPU 2` 释放后运行于全部 **`4×V100 [GPU 0/1/2/3]`**，`taWarps=4096, tbCtas=320, init_s=1.70`；`GPU 0` 上外部进程仅 `308 MiB < 400 MiB` 自动共存未干预，`status="passed", stage="complete"`）**：
  - **4 卡前中段持续稳定领先**：
    - **`R1–52 = 511.21 s`**（`R1–2 = 62.72 s` 较 3 卡 `v62 66.36 s` 再快 `-3.64 s`、`R3–5 = 132.37 s`、`R6–52 = 316.12 s`；连续 3 版 `v62/v63/v65` 将 `R1–52` 稳定在 `508.66–514.70 s`）；
    - **`gs = 192.47 s`**（较 3 卡 `v62 229.75 s` 再降 **`-37.28 s`**、`wide_ok=18, ov=77, fb=11`）。
  - **关键负向归因（为何 `R53–81` 扩大至 `393.23 s`）**：
    - `R62` 已降到 `over=3`，`R63` `fb=0`（`over=5`）后，**`R64–69` 连续爆发 `fb=3, 4, 2, 4, 5, 3`（每轮 `23M–39M pops / 20–38 s`）**，直到 `R74+` 打开无界与 `R78` 放宽并发才在 `R81` 清零（`23,199 pops, 0.05 s`）。
    - **`v64` 与 `v65` 两轮交叉对照确凿证实**：去掉 `tailHistMult` 与 `192` 扩邻后依然爆发 `fb=3..5`，唯一共同诱因是 **把活跃线 `pad=24` 第一探针从 `2M` 砍到 `500k` 并跳过 `4M/pad=48` 直跳 `6M/pad=128`**——原可于 `550k–2M` 在紧凑盒 `pad=24/48` 内解决的线被提前甩进 `pad=128` 巨大搜索盒，触发预算耗尽连环回退（反观未改探针盒的 `v62` 全程 `fb<=1`）。
  - **其余全口径汇总**：
    - `Read Netlist & PhysDB = 41.394 s`（`[No GC] *Total* = 43.579 s`）、`PRE f2=5.37 directPre=0.56`、`SORT = 1.85 s`、`IDMAP = 1.163 s`、`PORTALS = 0.52 s`、`CPU init_s = 3.41 s`、`materialize = 15.82 s (seq_fix=0)`、`F4_V65_POST (assign=1.84 s, fix=8.22 s, srcPin=1.97 s)`、`SETPIPS = 8.24 s`、`CHECKPIPS = 1.68 s`、`STATS_WL = 4.61 s`、`Write EDIF & XDEF = 13.139 s`、`Write XDC = 0.734 s`（写出共计 `13.872 s`）；
    - `sum_cpp = 904.44 s`（`p1=238.35 s, p2=42.29 s, p3=622.42 s, gs=192.47 s, corr=13.60 s, host=14.65 s [meta=5.82 s]`）、`Total wirelength = 19,009,235`、`Nodes popped = 27,558,433,939`（`gpu=19,294,540,188, cpu=8,263,893,751`）；
    - **`Route design = 1,032.95 s`**（`Init 23.34 s + Routing 1,009.60 s`）、**`route_wall = 1,133.02 s`**、**`E2E = 1,225.65 s`**；
    - **`dcp_legality.log`：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31091573`**（`read=81.63 s, pins=2.74 s, pip_shards=1.96 s, unrouted_scan=0.62 s`），`dcp_sha256`：`197c2b356ea6a2e2239ce07f89146003898ab793db5e8351a9965b9bb3989f38`。

### 2.46 `theia50per-ppt-hybrid-v66`（**`4×V100` 下刷新全局最快 `E2E = 915.55 s`（`15m 15.55s`，较 `v62 -60.28 s`）！`60` 轮 `over=0` + 进程内写出/64 分片并行校验完全隐藏 `check_s=6.19 s` + 冷启外置 `F4BaselineDcpCheck` 双盲比对 `pips=31116952` 逐字一致**）
- **改动文件**：`scratch/v66/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v66_patch.py`、`prepare_v66.py`、`deploy_v66.py`、`launch_v66.py`、`record_rounds_v66.py`。
- **核心改动（尾盘回锚 + 固定阶段流水化第一阶段 改造 3 + 4）**：
  1. **尾盘回锚 `v62` + 仅修两处纯缺陷（`fides_cpu.cpp` 第 3895–3936 行）**：恢复 `v62` 完整 `2M/pad=24 + 4M/pad=48 -> 6M/pad=96 -> 8M/pad=128 -> 16M/-1` 探针梯队，仅保留 `!isAct || fs == 0u`（修复冻结无辜邻居）与 `if (pops3 > 4000000LL) ++unboundedUsedThisRound;`（防止 `<4M` 轻线抢占 `0/-1` 配额）；
  2. **改造 3 — `PartialRouter.main` 写出与 64 分片合法性校验并行（`v66_patch.py` `partial_router`）**：在 `writeCheckpoint` 前执行按 `TileTypeEnum` 的只读 `Tile` 预热与 36 核并行 `DesignTools.updatePinsIsRouted`，随后在 `routed.writeCheckpoint(...)` 期间并发执行 35 核只读 `64` 分片 `ConcurrentHashMap<Node, Net>` 互斥检查 + `getUnroutedPins(routed)`，汇合后直接打印 `F4_V66_INPROC_DCP_CHECK` 与 `F4_BASE_DCP`；
  3. **改造 4 — `worker.py` 进程内校验验收适配（`v66_patch.py` `worker`）**：默认验证输出 `.dcp` 存在且 ZIP 内含 `.edf/.xdef`，并解析 `native_multichannel.log` 中 `F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips>0` 记录 `legality_mode="in_process_parallel_with_write"`；同时保留 `FIDES_COLD_DCP_CHECK=1` 开关用于里程碑双盲复核。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V66_SMOKE_PASS dlopen_ok=1 batch_sym=1 route_sym=1 hist_sym=1 all_syms=1` 且 `V66_LOAD_DRIVE_DRYRUN_PASS`；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`1397dfb0669b558f7443943d509d4af147be50bf0d0d051b3a6cb4ccff4852f5`，`cuda_sha256`：`8117b7d4f25c5dd01a4b9844fcd64ee1ed96a10a906c062d2c87179e6124b42c`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与双盲复核结果（`2026-11-25T21:06:25.76 -> 21:21:42.99`，运行于 **`4×V100 [GPU 0/1/2/3]`**，`taWarps=4096, tbCtas=320, init_s=1.97`，`status="passed", stage="complete", legality_mode="in_process_parallel_with_write"`）**：
  - **突破性成果**：
    - **`E2E = 915.55 s`（`15m 15.55s`，较 `v62 975.83 s` 再降 `-60.28 s` 创全局最优）**、`route_wall = 912.94 s`（外置税仅 `2.61 s` vs `v62 92.41 s`，砍掉 **`-89.80 s`**！）；
    - **`F4_V66_INPROC_DCP_CHECK pins=4.90s overlap_wall=24.32s write_s=24.32s check_s=6.19s`**：`6.19 s` 的 `64` 分片校验完全被 `24.32 s` 写出吞并（零额外耗时），输出 `F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31116952`；
    - **冷启双盲复核（`cold_dcp_doublecheck.log`）100% 逐字一致**：独立冷启 `F4BaselineDcpCheck` 重读 `THEIA_50per_gpu_full.dcp` 耗时 `85.27 + 2.80 + 2.05 + 0.76 = ~90.9 s`，输出 **`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31116952`**，与进程内并行校验逐项相同；
    - **尾盘恢复 `60` 轮干净收敛（`R53–60 = 136.43 s`）**：`R55 (50.26 s)` 后 `R56–59` 依次 `over 13→5→3→3`，`R60` 仅 `25,536 pops (0.07 s)` 即清零。
  - **其余全口径汇总**：
    - `Read Netlist & PhysDB = 35.165 s`（`[No GC] *Total* = 37.351 s`）、`PRE clk=0.00 static=0.22 preEst=0.00 f2=5.86 directPre=0.52`、`SORT = 1.56 s`、`IDMAP = 1.012 s`、`PORTALS = 0.39 s`、`CPU init_s = 3.19 s`、`materialize = 14.57 s (seq_fix=0)`、`F4_V66_POST (assign=1.61 s, fix=8.27 s, srcPin=1.80 s)`、`SETPIPS = 6.70 s`、`CHECKPIPS = 1.74 s`、`STATS_WL = 3.49 s`、`Write EDIF & XDEF = 23.438 s`、`Write XDC = 0.730 s`（写出共计 `24.168 s`）；
    - `R1–2 = 62.18 s`、`R3–5 = 140.57 s`、`R6–52 = 337.82 s` → `R1–52 = 540.57 s`，`R53–60 = 136.43 s`，`sum_cpp = 677.00 s`（`p1=238.45 s, p2=38.75 s, p3=398.58 s, gs=242.50 s, corr=13.01 s, host=14.72 s [meta=5.98 s], wide_ok=43, ov=145, fb=12`）；
    - `Total wirelength = 19,027,055`、`Nodes popped = 27,021,692,062`（`gpu=19,878,387,729, cpu=7,143,304,333`）、**`Route design = 803.29 s`**（`Init 26.96 s + Routing 776.32 s`）、`dcp_sha256`：`b946fc7596b2de1e435f70f02876cb3b379b10dc4e6007c0a195cbc1342c71c9`。

### 2.47 `theia50per-ppt-hybrid-v67`（**`4×V100` 下第二阶段固定阶段流水化实证落地并合法完赛！`F4_V67_PRELOAD wall=10.30 s` 全数藏入读盘、`F4_V67_GATE_OPEN = 0.01 s`、`PREPROC+UNROUTED` 由 `43.08 s` 压至 `20.03 s`（`-23.05 s`）、`F2 = 3.47 s`，`66` 轮 `over=0` + `dcp_legality PASS`，`E2E = 919.52 s`、`Route = 796.96 s`**）
- **改动文件**：`scratch/v67/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v66_patch.py`、`v67_patch.py`、`prepare_v67.py`、`deploy_v67.py`、`launch_v67.py`、`record_rounds_v67.py`。
- **核心改动（第二阶段固定阶段流水化，基于 `v66`）**：
  1. **拆解 `nativeInit` 为静态预分配 + 动态挂载（`fides_cpu.cpp` + `FidesNativeCpu.java`）**：新增 `nativePreinitStatic(rrgPath, layoutPath)` 与 `nativeAttachDynamicState(slrData, sllLen, nextLag, prevLag, occ, hist, pcc)`，将 `92.5M/391.5M` RRG 读盘、`G.geom` 36 线程构建与工作数组预分配前移，并保留 36 线程并行填充 SLR/Laguna 位与 16B `gpuHot`；
  2. **`t=0` 并发读盘预加载（`PartialRouter.main` + `FidesLaneGate`）**：在 `Design.readCheckpoint(args[0])` 期间并发拉起 `fides-java-preload`（`FidesRrgLoader.load` + `FidesRrgIdMap.open`）与 `fides-cpp-preinit`（`nativePreinitStatic`）；
  3. **36 核并行前处理与未布线收集（`PartialRouter.preprocess` + `getUnroutedPins`）**：提取共享 `warmTilePIPs` + 36 核 `ForkJoinPool` `DesignTools.updatePinsIsRouted(net)` 复用，并做 36 核分片有序收集 `getUnroutedPins`；
  4. **`F2Prewarm` 异步前移（`RWRoute.initialize` + `F2Prewarm.java`）**：`autoTuneRoutingProfileByDesignSize()` 完毕后立即异步启动 `F2Prewarm.initializeAsync(indirectConnections, directConnections)` 与 `F1Corridor` / 静态网重叠。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V67_SMOKE_PASS dlopen_ok=1 preinit_sym=1 attach_sym=1 all_syms=1`；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`2a698aa4738bdba9d8ea7cf8238c2e16ea7525270fbc05dcabc2cd30b81d89eb`，`cuda_sha256`：`31190680bf4f3a6cc3bdbcd5f000adb6cb81f2b533fe643d92e5aeee14f2fbde`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与合法性结果（`2026-11-25T23:16:07.99 -> 23:31:28.80`，`4×V100 [GPU 0/1/2/3]`，`taWarps=4096, tbCtas=320, init_s=2.44`，`status="passed", stage="complete", legality_mode="in_process_parallel_with_write"`）**：
  - **第二阶段前段提速全面兑现**：
    - `F4_V67_PRELOAD java_s=8.32 cpp_s=10.05 wall_s=10.30`（完全藏进 `Read DCP = 45.941 s`），`F4_V67_GATE_OPEN preloaded=true time_s=0.01`，`FIDES_V67_NATIVE_ATTACH time_s=4.29`；
    - `F4_V67_PREPROC rwPre=16.90s updatePins=2.16s` + `F4_V67_UNROUTED count=4952719 time_s=0.97` → **前处理合计 `20.03 s`（较 `v66 43.08 s` 实降 `-23.05 s`）**；
    - `F4_V66_PRE f2=3.47s`（较 `v66 5.86 s` 再省 `-2.39 s`），`Initialization = 23.74 s`（较 `v66 26.96 s` 降 `-3.22 s`），**`Route design = 796.96 s`**（较 `v66 803.29 s` 降 `-6.33 s`）；
  - **其余分段与微尾归因**：
    - `R1–2 = 66.11 s`、`R3–5 = 127.44 s`、`R6–52 = 358.46 s` → `R1–52 = 552.01 s`；
    - **微尾 `R53–66 = 143.10 s`**：`R55–61` 连续 `7` 轮在 **`act=2, over=1`** 下因未达 `R62` 门槛触发 `fb=1` 失败循环（`6M/12M pops` 耗 `74.43 s`），随后 `R62–65` 连跑 `3–4` 轮 `18.5–19.7M pops`（每轮 `14–20 s`、`fb=0` 但 `histFactor=1.0f` 增幅太慢）直至 `R66` 清零（`0.06 s`）；
    - `Post (assign=2.07s, fix=7.11s, srcPin=2.05s, setpips=5.48s, checkpips=1.86s, stats_wl=7.44s, materialize=14.45s)`、`Write EDIF & XDEF & XDC = 21.654 s`、`F4_V66_INPROC_DCP_CHECK pins=8.74s overlap_wall=21.82s write_s=21.82s check_s=2.67s`；
    - **`E2E = 919.52 s`、`route_wall = 916.89 s`**（受 `Read DCP +8.59 s`、`STATS_WL/pins +7.79 s` 与微尾 `+6.67 s` 波动抵消部分前段净收益），`Total wirelength = 18,985,212`（较 `v66 -41,843`），`Nodes popped = 26,840,525,268`，**`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31089642`**，`dcp_sha256`：`d8883cf31d780a7da9549dc8dcb791cf13a4f26bd4fcd52b9de658d38ab0fe22`。

### 2.48 `theia50per-ppt-hybrid-v68`（**`4×V100` 下刷新 4 卡 `R1–52 = 530.47 s`（`gpu_s = 192.24 s`，较 `v67 -21.54 s`）；于 `R68` 停止归档并精确定位“微尾提前多路 `0/-1` (`maxUnbounded=4`) 引爆 `over=3→17`”归因**）
- **改动文件**：`scratch/v68/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v66_patch.py`、`v67_patch.py`、`prepare_v68.py`、`deploy_v68.py`、`launch_v68.py`、`record_rounds_v68.py`。
- **核心改动（基于 `v67`，仅修改 `fides_cpu.cpp` 第 287、1590、3840、3898、3990、4140、4272 行）**：
  1. **`4×V100` `Wide` 与 `Pass 3` 门槛再标定**：将 `3+` 卡 `minWideItems` 由 `6` 提至 `16`、`4` 卡 `minGpuPass3` 由 `384` 提至 `512`，避免 `6–15` 条溢出小批独占 4 卡引发 `~10 s` 稀疏空等；
  2. **微尾 (`R55+`) 逃逸与定向 `hist` 实验**：在 `routeIter >= 55 && over <= 4 && activeCount <= 8` 时启用 `microStalled`（`maxUnbounded=4` 提前放行 `0/-1`、跳过 `4M/48`），并当 `over <= 2 && fb == 0` 时对滞留节点叠加 `6×/12×` 历史倍率。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V68_SMOKE_PASS dlopen_ok=1 all_syms=1` 且 `V68_LOAD_DRIVE_DRYRUN_PASS`；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`59c61e930021a50f2697265e1fc148495c9e6a812dfbefc24a5108dc9312adf9`，`cuda_sha256`：`569369e814523a5040f4b71622714f13941ac71dba2d0e1d1e4c6f2c63ea1d05`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **实测与停止归因（`2026-11-25T23:39:35.28` 启动于 `4×V100 [GPU 0/1/2/3]`，推至 `R68` `sum_cpp=986.08 s` 后手动终止保留完整目录，GPU 与 `/dev/shm` 已清零）**：
  - **正向实证 (`4×V100` `Wide min=16` & `Pass 3 min=512`)**：**`R1–52 = 530.47 s`**（较 `v67 552.01 s` 大降 **`-21.54 s`**，其中 `R1–2=63.73 s, R3–5=121.49 s, R6–52=345.25 s, gpu_s=192.24 s`：`R8` 由 `37.82 s → 23.91 s`、`R10` 由 `32.62 s → 11.15 s`、`R13` 由 `27.68 s → 14.19 s`、`R18` 由 `8.13 s → 6.04 s`）；
  - **负向归因（为何 `R53–68` 扩至 `455.61 s`）**：`R56–58` 将拥塞压至 `over=4→3`（`act=6`）后，因在 `over=3, act=6` 提前触发了 **`maxUnbounded=4` 并发 `0/-1` 无界探测**，`R59` 起连续出现 `30.79M–58.16M pops/轮`（`33–64 s/轮`），反将拥塞震荡外溢为 `over 3→5→11→6→17→10→4→9→6`（且从未触发 `over<=2 && fb==0` 的定向 `hist` 分支）；证实 **`0/-1` 并发放行绝不可放宽至 `over>=3` / `act>=5`**，仅限 `over<=1 && act<=2`（或 `R74+`）下才安全。

### 2.49 `theia50per-ppt-hybrid-v69`（**微尾盘并发回溢受控：`R53–58` 仅 6 轮压至 `over=1, act=2`；于 `R73` 停止归档并精确定位 `microDeepNbr`（`192` 邻域扩拆）引爆 `over=1→4/5` 三连震荡**）
- **改动文件**：`scratch/v69/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v66_patch.py`、`v67_patch.py`、`prepare_v69.py`、`deploy_v69.py`、`launch_v69.py`、`record_rounds_v69.py`。
- **核心改动（基于 `v68`，仅收紧 `fides_cpu.cpp` 第 3993–3995 行微尾盘门控）**：
  - 保留 `v67` Stage-2 流水化 + `v68` `4×V100` `Wide min=16 / P3 min=512` + `microDeepNbr` + `microStuck` (`6×/12×`)；
  - 将 `microTailRegime` 由 `over <= 4 && activeCount <= 8` 严格收紧至 **`overNow <= 1u && activeCount <= 2`**，并将 `maxUnboundedThisRound` 收紧为 `(routeIter >= 78) ? 8 : (microStalled ? 2 : 1)`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V69_SMOKE_PASS dlopen_ok=1 all_syms=1` 且 `V69_LOAD_DRIVE_DRYRUN_PASS drive_loaded=1 in_process_parallel_with_write=1`；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`ff4dd9f824b6cf8cbd3a0e9e7106f193ea5dcfbdbeaf93cbe7e2e0c99314b943`，`cuda_sha256`：`5a45797dac4b6f470c788195919579f3406355f8806b35a4136beed5ff453f50`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **实测与停止归因（`2026-11-26T00:23:10.25` 启动于 `4×V100 [GPU 0/1/2/3]`，推至 `R73` `sum_cpp=931.51 s` 后手动终止保留完整目录，GPU `1/2/3 = 4 MiB`、`GPU 0 = 312 MiB`、`/dev/shm` 与进程均已清零）**：
  - **前段与微尾收紧正效**：`F4_V67_PRELOAD (java_s=9.49, cpp_s=10.32, wall_s=10.57)` 藏入读盘、`PREPROC (rwPre=19.71s, updatePins=2.21s)`、`UNROUTED=1.04s`、`F2=3.29s`、`GATE_OPEN=0.01s`、`NATIVE_ATTACH=4.54s`；即便 `R1–52 = 564.75 s`（`R1–2=64.60 s, R3–5=133.74 s, R6–52=366.41 s, gpu_s=193.82 s`、`R52 over=28`），`R53–58` 仍仅用 **6 轮 / `54.60 s`** 干净压至 **`R58: over=1, act=2, fb=0`**（`over 8→4→3→4→2→1`，彻底消除了 `v68` 在 `over=3, act=6` 的外溢）。
  - **负向根因（为何 `R53–73` 拖至 `366.76 s`）**：三次精准抓到 **`microDeepNbr`（`maxNbrNet=384, nbrLim=192`）扩拆重布线引发反跳**：
    - `R58–59` 连续 `over=1 (streak=1→2, mult=6×→12×)` 触发 `microDeepNbr` → **`R60 routed=193`**（多拆 `64` 条大邻居）使 `over 1→4→7`（多耗 `5` 轮降回 `over=1`）；
    - `R62–63` 再次 `streak=2` 触发 `microDeepNbr` → **`R64 routed=193`** 使 `over 1→5`（`R64–68` 单段再烧 **`176.67 s`**，其中 `R65–68` 连续 `4` 轮 `fb=1`、`34–46M pops/轮`）；
    - `R71–72` 在 `over=2` 再次 `streak=2` 触发 `microDeepNbr` → **`R73 routed=196`** 再次使 `over 2→4`。
  - **明确结论**：`192` 邻域大拆（`microDeepNbr`）会同时拆毁附近已布通的大线网并制造 `4–7` 个新拥塞点，必须**彻底移除 `microDeepNbr`**，仅保留不加拆邻居的 `microStuck` 滞留历史提权与收紧门控，即可避免微尾反复震荡。

### 2.50 `theia50per-ppt-hybrid-v70`（**`4×V100` 正式合法完赛并首次突破 800 s 大关：`E2E = 798.97 s`（`13m 18.97s`，反超 PPT Slide 15 的 `807.24 s`）！`Route design = 684.62 s`、`64` 轮 `over=0`（尾盘 `R53–64` 压至 `60.83 s`），进程内并行校验与冷启 `F4BaselineDcpCheck` 双盲复核 `pips=31075870` 逐字一致**）
- **改动文件**：`scratch/v70/` 下 `fides_gpu.h`、`fides_gpu.cu`、`fides_cpu.cpp`、`FidesNativeCpu.java`、`F4BaselineDcpCheck.java`、`v66_patch.py`、`v67_patch.py`、`prepare_v70.py`、`deploy_v70.py`、`launch_v70.py`、`record_rounds_v70.py`。
- **核心改动（基于 `v69`，仅删除 `fides_cpu.cpp` 第 3896–3901 行 `microDeepNbr`）**：
  - 彻底移除引爆 `v69` 尾盘三次反跳的 `microDeepNbr`（`192` 邻域扩拆），恢复标准 `widenNbr` 上限（`maxNbrNet=128, maxNbrPops=120000, nbrDx=4, nbrDy=8, nbrLim=128u`）；
  - 完整保留 `v67` Stage-2 全套固定阶段流水化 + `v68` `4×V100` `Wide min=16 / P3 min=512` + `v69` 收紧门控 `microTailRegime (overNow <= 1u && activeCount <= 2)` + `microStuck (6×/12×)` 滞留 `hist` 提权。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V70_SMOKE_PASS dlopen_ok=1 all_syms=1` 且 `V70_LOAD_DRIVE_DRYRUN_PASS drive_loaded=1 in_process_parallel_with_write=1`；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`ade111d12c714d5f26ad3d7dfcd85b36b776dcdb3dcb9d927788b797c75fd109`，`cuda_sha256`：`2f14379053e262b55ba33719214f61ffabe2a16dec54f601139ade0768748fe7`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与双盲复核结果（`2026-11-26T08:50:44.18 -> 09:04:04.34`，`4×V100 [GPU 0/1/2/3]`，`taWarps=4096, tbCtas=320, init_s=2.38`，`status="passed", stage="complete", legality_mode="in_process_parallel_with_write"`）**：
  - **里程碑级全局突破**：
    - **`E2E = 798.97 s`（`13m 18.97s`）** —— 较 `v66 (915.55 s)` 大降 **`-116.58 s`**、较 `v62 (975.83 s)` 大降 **`-176.86 s`**，**首次突破 `800 s` 并反超 PPT Slide 15 基准 `807.24 s`（`-8.27 s`）**；
    - **`Route design = 684.62 s`**（`Init 23.89 s + Routing 660.72 s`）—— 较 `v62 (778.10 s)` 再降 **`-93.48 s`**、较 `v66 (803.29 s)` 大降 **`-118.67 s`**；
    - **`route_wall = 796.34 s`**（外置进程税 `2.64 s`，含 `resident_preflight = 0.09 s`）；
    - **尾盘 `R53–64` 仅耗时 `60.83 s`（较 `v66 136.43 s` 砍半再省 `-75.60 s`！）**：`R53–55` 降 `over 50→42→31→18`（仅 `2.55 s`），`R56–58` 降 `over 18→8→5→1`（`40.53 s`），`R59–63` 在 `microStuck (6×→12×)` 与收紧门控下用 `17.69 s` 无震荡消融单点过载，`R64` 仅 `23,656 pops (0.06 s)` 即清零 `over=0`；
    - **`F4_V66_INPROC_DCP_CHECK pins=3.84s overlap_wall=15.59s write_s=15.59s check_s=2.65s`**：`Write EDIF & XDEF & XDC = 15.437 s`（吞并 `2.65 s` 校验），输出 `F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31075870`；
    - **冷启双盲复核（`cold_dcp_doublecheck.log`）100% 逐字一致**：独立冷启 `F4BaselineDcpCheck` 重读 `THEIA_50per_gpu_full.dcp` 耗时 `81.77 + 2.88 + 2.61 + 0.54 s`，输出 **`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31075870`**。
  - **其余全口径汇总**：
    - `F4_V67_PRELOAD (java_s=8.96, cpp_s=10.27, wall_s=10.52)`（藏入 `Read Netlist & PhysDB = 42.514 s / [No GC] *Total* = 46.355 s`）、`F4_V67_PREPROC (rwPre=19.54s, updatePins=2.14s)`、`F4_V67_UNROUTED = 1.09s`、`F4_V66_PRE (static=0.25, f2=3.64, directPre=0.53)`、`SORT = 1.27 s`、`GATE_OPEN = 0.01 s`、`NATIVE_ATTACH = 4.52 s`、`PORTALS = 0.44 s`；
    - `R1–2 = 62.93 s`、`R3–5 = 129.98 s`、`R6–52 = 327.49 s` → `R1–52 = 520.40 s`，`R53–64 = 60.83 s`，`sum_cpp = 581.23 s`（`p1=231.80 s, p2=38.45 s, p3=309.42 s, gs=192.96 s, corr=15.28 s, host=14.41 s [meta=5.64 s], wide_ok=4, ov=126, gpu_fb=77`）；
    - `Post (assign=2.11 s, fix=7.35 s, srcPin=2.03 s, SETPIPS=7.52 s, CHECKPIPS=1.31 s, STATS_WL=3.97 s, materialize=13.69 s [seq_fix=0])`；
    - `Total wirelength = 19,001,226`、`Nodes popped = 27,022,101,815`（`gpu=18,657,271,619, cpu=8,364,830,196`）、`dcp_sha256`：`6217faa7cef881c52ce1e82c91e5063af7a1fde1ff5ca3934021dfdf0347299d`。

### 2.51 `theia50per-ppt-hybrid-v71`（**前处理真瓶颈精测命中 `makePhys=11.73 s + missingPins=5.78 s`、`R1–5 = 185.97 s (-6.94 s)`；于 `R70` 停止归档并定位 `Pass 3 min=1024 / 6M bypass / 无 wideRetry` 引发 `R16–52` 拥塞抬升**）
- **改动文件**：`scratch/v71/` 下 `fides_cpu.cpp`、`v71_patch.py`、`prepare_v71.py`、`deploy_v71.py`、`launch_v71.py`、`record_rounds_v71.py`。
- **核心改动（基于 `v70`）**：
  1. `fides_cpu.cpp`：`4×V100` 下屏蔽 `Tier-B1 Wide (16M)` GPU 重试、将 `Pass 3` 异步 CPU 旁路门槛由 `12M` 降至 `6M`、将 `4` 卡 `minGpuPass3` 由 `512` 提至 `1024`；微尾将 `microTailRegime` 放宽至 `overNow <= 1u && activeCount <= 4`（`maxUnbounded=1`）；
  2. `v71_patch.py`：`PartialRouter.main` 与 `RWRoute` 将 `STATS_WL (3.97 s)`、`checkPIPsUsage (1.31 s)` 并入 `writeCheckpoint` 并发期并跳过重复 `warmTilePIPs`，同时在 `RWRoute.preprocess` 打点拆分 `makePhys / staticPins / missingPins`。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V71_SMOKE_PASS dlopen_ok=1 all_syms=1` 且 `V71_LOAD_DRIVE_DRYRUN_PASS`；`cpp_sha256`：`822c4a0418d5b982c685941cc80207fbe2b27e1dd34f6a1b972ab68350adb7ed`，`cuda_sha256`：`bf00a48ae8a6e34bb1c1661fe396354cdf550176e7a9793340bfc4663b99034f`。
- **实测与停止归因（`2026-11-26T09:49:09.14` 启动于 `4×V100 [GPU 0/1/2/3]`，推至 `R70 sum_cpp=847.50 s` 后手动终止并清理干净）**：
  - **关键量化收获**：① **`F4_V71_PREPROC makePhys=11.73s staticPins=1.63s missingPins=5.78s`** 明确证明 `rwPre=19.15s` 的主瓶颈是 `makePhysNetNamesConsistent`（内部 `getParentNetMap()` 与 `synchronized(design)`）及 `createMissingSitePinInsts`（`5.78 s`），而非仅占 `1.63 s` 的 `staticPins`；② `Read Netlist & PhysDB = 38.978 s / Total = 42.853 s`、`R3–5 = 122.13 s`（较 `v70 -7.85 s`）。
  - **负向归因**：将 `Pass 3` 门槛抬高到 `1024` + `6M bypass` + 去 `wideRetry` 后，`R16–17` 回退 CPU 耗时反由 `6.90/7.37 s` 升至 `10.32/11.04 s`（`R20 over 508→578`、`R52 over 50→121`），导致 `R6–52` 扩至 **`363.66 s`**（vs `v70 327.49 s`）；且 `R67 (over=1, mult=12.0)` 后 `R68` 因 `128` 邻域围绕 `12×` 高代价节点绕行反跳 `over=1→12`（`R68–70 = 116.05 s`）。**`v72` 立即回锚 `v70` 经验证的 C++ 参数，先兑现 `v71_patch.py` 后处理/写出重叠收益。**

### 2.52 `theia50per-ppt-hybrid-v72`（**回锚 `v70` C++ + 实测 `v72_patch` 后处理并行重叠 `dcp_legality PASS`（`pips=31103661`）；暴露“72 线程双池抢核拖慢 `Write EDIF`”及“`v70` `6×/12×` 尾盘放大倍率存在随机 `over=1→16` 波动”两大根因**）
- **改动文件**：`scratch/v72/` 下 `fides_cpu.cpp`（与 `v70` 逐字节一致）、`v72_patch.py`、`prepare_v72.py`、`deploy_v72.py`、`launch_v72.py`、`record_rounds_v72.py`。
- **核心改动**：在 `v70` 完整 C++/CUDA 引擎上叠加 `v72_patch.py`（跳过 `RWRoute.setPIPsOfNets` 中重复的 `getAllTiles` 预热与 `checkPIPsUsage`、跳过 `PartialRouter.main` 重复 `warmTilePIPs`、将 `STATS_WL` 移入 `writeCheckpoint` 并发期）。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V72_SMOKE_PASS dlopen_ok=1 all_syms=1` 且 `V72_LOAD_DRIVE_DRYRUN_PASS`；`cpp_sha256`：`331617c39a1844390959a66e86beb8ef4d5450a684d477a2fc0e217b98895692`。
- **全流程实测结果（`2026-11-26T10:11:42.07 -> 10:35:33.43`，`4×V100 [GPU 0/1/2/3]`，`status="passed", stage="complete", legality_mode="in_process_parallel_with_write"`）**：
  - **合法性与功能验证**：`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31103661`、`Total wirelength = 19,026,610`、`trackChanges=false`、`tileTypes=0`（成功跳过重复扫描）。
  - **负向根因定位 1（72 线程双池抢核拖慢写盘）**：`PartialRouter.main` 同时开着 `checkPool (36 线程)`、`statsFuture (f4SharedPool 35 线程)` 与 `writeCheckpoint` 主线程（总活跃线程 `> 72 > 36 核`），导致 CPU/内存带宽严重踩踏——`Write EDIF` 从 `8.69 s` 膨至 **`22.86 s`**、`Write EDIF & XDEF` 从 `14.78 s` 膨至 **`31.54 s`**、`STATS_WL` 从 `3.97 s` 膨至 **`26.09 s`**（`overlap_wall = 32.44 s`）。**修正方法**：让 `checkFuture` 与 `STATS_WL` 共用**同一个 `34` 线程池串行执行**（`2.3 + 4.0 = 6.3 s`），彻底消除超订阅并完整藏进 `15 s` 写盘窗口。
  - **负向根因定位 2（`v70` 尾盘 `6×/12×` 提权 + `128` 邻域拆线的强方差）**：相同 `v70` C++ 代码下，`R1–52 = 575.34 s`（`R1–2=66.65s, R3–5=140.49s, R6–52=368.20s`），尾盘 `R53–83` 高达 **`617.10 s`**（`83` 轮才清零、`Route design = 1,300.59 s`、`E2E = 1,430.17 s`）：在 `R65` (`over=1`) → `R66–67` (`act=2, over=4→16`)、`R71` (`over=1`) → `R72` (`over=5`)、`R73` (`over=1`) → `R74` (`over=14`) 连续三次发生“对单点 `over=1` 叠 `6×/12×` 历史倍率 + 同轮拆 `128` 条邻居 + `2` 路并发 `0/-1`”导致的拥塞反跳，直至 `R78` 放开后于 `R83` 清零（`0.14 s`）。

### 2.53 `theia50per-ppt-hybrid-v73`（**`4×V100` 再刷全局最快新纪录：`E2E = 757.90 s`（`12m 37.90s`，较 `v70` 再降 `-41.08 s`、领先 PPT Slide 15 `807.24 s` 达 `-49.34 s`）！`Route design = 638.97 s`、`57` 轮极速清零（`R53–57 = 0.88 s`）、`WL = 18,964,265`（更短）、双盲冷启 `F4BaselineDcpCheck` `pips=31071296` 零违例，并原生解除 `F2Prewarm` 硬编码兼容 13 用例**）
- **改动文件**：`scratch/v73/` 下 `fides_cpu.cpp`、`v73_patch.py`、`prepare_v73.py`、`deploy_v73.py`、`launch_v73.py`、`record_rounds_v73.py`。
- **核心改动**：
  1. **通用尾部收敛去抖动（同时兼顾 `THEIA_50per` 与 `THEIA_20per` `over=5..16` 长尾、`fides_cpu.cpp` 第 3896–3901、3995、4274 行）**：
     - 收窄邻域重拆规模：`maxNbrNet=64`、`maxNbrPops=widenNbr ? 60000 : 50000`、`nbrDx=widenNbr ? 3 : 2`、`nbrDy=widenNbr ? 6 : 4`，并将 `nbrLim` 从 `128u` 严格收窄为 `(routeIter >= 72 && G.lastTailFbCount > 0) ? 64u : (microScope ? 20u : (widenNbr ? 36u : 32u))`（消除无论 `over<=2` 还是 `over=5..16` 下整批撕毁 128 条无辜大网引发的反跳与 30–74 s/轮耗散）；
     - `maxUnboundedThisRound = (routeIter >= 78) ? 8 : 1;`（禁止 `over=1` 同轮双无界互踩）；
     - `microMult = !microStuck ? 1.0f : ((G.microStuckRounds >= 2) ? 3.0f : 2.0f);`（温和历史排斥取代 `6×/12×` 冲击）；
  2. **单线程池顺序 `check + STATS_WL` 重叠（`v73_patch.py`）**：保留跳过重复 `warmTilePIPs`、`getAllTiles` 与 `checkPIPsUsage`，将 `checkPool` 工人上限设为 `<= 34`（为 `writeCheckpoint` 预留 2 核），并在 `checkFuture` 内完成 64 分片校验后顺序执行 `deferredRouter.fidesRunDeferredStats()`，彻底避免 `v72` 72+ 线程超订阅；
  3. **移除 `F2Prewarm` 硬编码（`v73_patch.py`）**：将 `indirectCount != 4_909_155 || nAll != 4_952_719` 放宽为通用下界检查 `indirectCount < 0 || nAll <= 0`，使 `v73` 原生兼容全部 13 个竞赛测例。
- **编译与冒烟**：4 份编译日志均 `0 B`；`V73_SMOKE_PASS`（`libfides_cpu.so` 全 JNI 符号 + `javap` 检查通过）；`worker_sha256`：`686e93d124b29ec3ccfa11c5775d28180680d3927fddbe0c4ecc10b139fd5198`，`cpp_sha256`：`5328d897dfc106f6a4521b47a07e9d54a6a005eab80f48bdfaf8b60723ba78bb`，`cuda_sha256`：`12c504208474d1f8bb5c3a336e4c615fc6f11ea2c52d6b89711c86c8387bcb09`，`fides_gpu_cu_sha256`：`47c9a1c298fe557831a0dd9abc03f73143fc80a128d5e0903fd3c78848807e8c`。
- **全流程实测与双盲复核结果（`2026-11-26T15:38:16.46 -> 15:50:55.47`，`4×V100 [GPU 0/1/2/3]`，`status="passed", stage="complete", legality_mode="in_process_parallel_with_write"`）**：
  - **最新全局最优里程碑**：
    - **`E2E = 757.90 s`（`12m 37.90s`）** —— 较 `v70 (798.97 s)` 再降 **`-41.08 s`**、较 `v66 (915.55 s)` 大降 **`-157.65 s`**、**反超 PPT Slide 15 (`807.24 s`) 达 `-49.34 s`**；
    - **`Route design = 638.97 s`**（`Init 20.25 s + Routing 618.72 s`）—— 较 `v70 (684.62 s)` 再降 **`-45.65 s`**；
    - **`route_wall = 755.27 s`**（外置税仅 `2.63 s`，含 `resident_preflight = 0.075 s`）；
    - **尾盘 `57` 轮闪电收敛（`R53–57` 仅 **`0.88 s`** vs `v70 60.83 s`！）**：`R53` (`over=22, 0.42s`) → `R54` (`over=16, 0.07s`) → `R55` (`over=2, mult=2.0, 0.22s`) → `R56` (`over=1, mult=3.0, routed=52, 0.11s`) → `R57` (`over=0, routed=35, pops=15,835, 0.06s`)；
    - **`F4_V66_INPROC_DCP_CHECK pins=5.48s overlap_wall=20.57s write_s=20.57s check_s=3.01s stats_s=9.11s`**：`3.01 s` 校验与 `9.11 s` 延迟统计均被单池串行藏入 `20.57 s` 写盘窗口（零线程打架），输出 `F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31071296`；
    - **冷启双盲复核（`cold_dcp_doublecheck.log`）100% 逐字一致**：`Read=80.97s, pins=2.80s, pip_shards=2.34s, unrouted_scan=0.53s`，输出 **`F4_BASE_DCP unroutedPins=0 sharedPipNodes=0 pips=31071296`**。
  - **其余全口径汇总**：
    - `F4_V67_PRELOAD (java_s=11.94, cpp_s=11.85, wall_s=12.10)`（藏入 `Read Netlist & PhysDB = 48.281 s`）、`F4_V71_PREPROC (makePhys=11.72s, staticPins=1.41s, missingPins=4.75s)` → `rwPre=17.88s, updatePins=2.69s`、`UNROUTED=1.03s`、`PRE f2=3.42s directPre=0.54`、`SORT=1.44s`、`GATE_OPEN=0.01s`、`PORTALS=0.35s`；
    - `R1–2 = 64.33 s`、`R3–5 = 130.44 s`、`R6–52 = 344.82 s` → `R1–52 = 539.59 s`，`R53–57 = 0.88 s`，`sum_cpp = 540.47 s`（`v37_gpu_pops=18,887,424,403, gpu_ns=186.73 s, gpu_waves=1,863`）；
    - `Post (assign=3.00 s, fix=6.95 s, srcPin=1.81 s, SETPIPS=5.58 s, CHECKPIPS=跳过)`、`STATS_WL=9.11 s`（写盘期隐藏）、`Write EDIF & XDEF=19.690 s`；
    - `Total wirelength = 18,964,265`（较 `v70 -36,961`）、`Nodes popped = 27,124,486,998`、`dcp_sha256`：`bcaf6cea9a625b8de1102e148d1eb2851602bcfb3323473ad588b04c8214ba40`。

---

## 4. 面向全 13 用例复测与 PPT Slide 19 (`517.28 s`) 的下一步规划

基于 `v73` 在 `THEIA_50per` 上取得 **`E2E = 757.90 s`（尾盘 `0.88 s`）** 的实证突破，下一步推进顺序：
1. **将 `v73` 部署并复测 `THEIA_20per`（及全 13 用例）**：验证收窄的 `nbrLim (20–36u)`、`maxNbrNet=64` 与柔和 `microMult (2×/3×)` 是否如预期大幅砍掉 `THEIA_20per` 在 `v70` 下耗时 `762.90 s` 的 `R54–74` 尾盘拉锯，同时让 [run_all13.py](file:///E:/AntigravityUsage/Rapidwright-based-GPU-accelerator/scripts/all13/run_all13.py) 直接复用通用 `v73`（并避免重复 RRG 导出浪费）；
2. **中段核心继续向 PPT `517.28 s` 下探**（参见 [NEXT_OPTIMIZATION_PLAN.md](file:///E:/AntigravityUsage/NEXT_OPTIMIZATION_PLAN.md)）：
   - `makePhysNetNamesConsistent` (`11.72 s`) 与 `createMissingSitePinInsts` (`4.75 s`) 并行/减锁优化；
   - **`R3–5 p1` 与 `R6–52 p3` 稀疏工作表 + 边描述符 `SoA`**（目标再降 `150–200 s`）；
   - **`writeCheckpoint` + 后处理与特征重叠压缩**（目前 `Write EDIF & XDEF = 19.69 s`，可进一步检查 CPU 核配比与特征写入）。
