# `v95` 下一阶段优化方案备忘（基于 `v94` 实测 `13` 用例 `E2E = 1,814.86 s`）

> **当前基线**：`v94` 全 13 用例（`2 × Tesla V100`）：`E2E = 1,814.86 s`，`Route design = 1,298.34 s`，13/13 双重合法校验通过。  
> **目标方向**：在严守 `unroutedPins=0, sharedPipNodes=0` 的前提下，优先压减固定启动 I/O 税、Java 单线程前处理瓶颈、以及中大型设计的物化与尾盘收敛时间，力争在 `2×V100` / `4×V100` 下突破 **`1,500 s`**（反超 PPT Slide 18 `T54 = 1,657.64 s`）。

---

## 优化项 P0：`FidesRrgIdMap.tryLoadBinaryCache` 快速加载与并发校验（预计 `-60 ~ -100 s` / 全 13 用例）

### 实测瓶颈
- 在 `v94` 的 13 用例运行中，`F4_V37_IDMAP_CACHE` 累计耗时 **`~132 s`**（第 1 批 6 用例 `4.80–6.59 s`/用例；第 2 批 7 用例在冷页缓存与跨路带宽争抢下达 `11.74–12.96 s`/用例）。
- 这直接导致 5 个最小设计（`mcml_RLE`、`RLE_BlobMerging`、`oc_des_des3perf`、`aquarius_b15_1`、`RS8FreqDecode`）尽管 `Route design` 仅 `9.64–12.40 s`（已持平或快于 Java 基线），`E2E` 却被拖到 `20.88–28.22 s`。

### 代码根因（[FidesRrgIdMap.java](../src/java/FidesRrgIdMap.java#L713-L774)）
1. `readIntArray(ch, n)` 通过 Java `FileChannel` 单线程分配并逐字节序转换读取 `wireOf`（`n ≈ 87M–92.5M` ints ≈ `350–370 MiB`）。
2. `computeWireChecksum(wireOf, n)` 对全量 `wireOf` 执行单线程串行乘法累加校验（`for (int i = 0; i < n; ++i)`）。
3. 实际 `wireOf` 仅在阶段末尾回写 / `assignNodes` / `materialize` 时按命中的活跃节点反查，或在多线程读盘中可并行解码。

### 改进方案
1. **JNI / C++ 多线程 `pread` + SIMD 批量解码校验**，或使用 `FileChannel.map(READ_ONLY)` + 多线程 `computeWireChecksum` 分片校验（或直接复用 C++ 侧并行读取，同时由 Java/C++ 共享同一块校验逻辑）。
2. **后台渐进式延迟校验**：先仅加载并校验轻量 `runTile + runFirstId`（合计仅 `~2.2 MiB`，耗时 `< 5 ms`）以供 `preinit/Routing` 与按 `tileAddress` 查询；体积 `370 MiB` 的 `wireOf` 在 `Design.readCheckpoint` 与前处理期间由独立后台线程异步读入并校验，在路由末尾 `f4NativeMaterializeAll` 前 `join` 等待。

---

## 优化项 P1：`RWRoute.preprocess` 单线程热点并行化（预计 `-35 ~ -50 s` / 全 13 用例）

### 实测瓶颈
- `F4_V71_PREPROC` 在 `THEIA_50per` 耗 **`17.73 s`**（`makePhys = 11.96 s`, `staticPins = 1.47 s`, `missingPins = 4.30 s`），在 `THEIA_20per` / `k_800_ram` / `THEIA_jpeg` / `THEIA` 上也各消耗 `4–9 s`，13 用例合计约 **`55–65 s`**。

### 改进方案
1. 分析并并行化 `makePhysNetNamesConsistent` 内部的单线程 `EDIFNetlist.getParentNetMap()` / 全网表遍历。
2. 将 `DesignTools.createPossiblePinsToStaticNets` 与 `createMissingSitePinInsts` 按 `SiteInst` / `Net` 无共享写冲突切分到 36 核线程池并发执行。

---

## 优化项 P2：`THEIA_50per` / `THEIA_20per` / `THEIA_jpeg` 中后期 CPU 尾盘与后处理流水（预计 `-40 ~ -70 s`）

### 实测瓶颈
1. **`THEIA_jpeg` 轮次反弹**：`v94` 为 `41` 轮 / `97.90 s`，高于 `v76` 的 `28` 轮 / `93.55 s`（需排查 Pass 3 阈值调整对 `THEIA_jpeg` 等中型拥塞设计收敛轮数的影响，按设计规模自适应 `ultraPopCut` 或保留中等规模 `Pass 3` GPU 阈值）。
2. **`THEIA_50per` Pass 3 中尾段（`R5–R18`）与微尾（`R19–R50`）**：观察 `R7–R18` 每轮 `p3_s` 仍达 `10–20 s`、`R22/R24/R27/R28/R32/R33/R37` 出现周期性 `~5–6 s` 尖峰；需针对高拥塞/高重重试连接（`rel_h / fb` 候选）分析是 GPU `overflow` 串行退避还是单条超大 Net（`R45–R47` 长尾）占满单线程。
3. **`THEIA_50per` 路由后串行阶段（`~28.1 s`）**：`f4NativeMaterializeAll = 13.29 s`（`cache=5.64s, bind=3.41s, users=3.41s`）+ `F4_V66_POST = 8.93 s`（`assign=1.79s, fix=5.84s, srcPin=1.30s`）+ `SETPIPS = 5.86 s`；可将 `materialize` 并行构建 `Net -> RouteNode[]` 与 `fixRoutes` 深度合并。

---

## 优化项 P3：`4 × Tesla V100` 全卡重测（GPU `0, 1` 释放后）

- `v94` 目前全部成绩基于 **`2 × V100`（`2,048` Warps / `160` CTAs）**；待 `gpu02` 上 GPU `0, 1` 的外部任务释放后，将基于 `v94/v95` 进行 **`4 × V100`（`4,096` Warps / `320` CTAs）** 对比实测，进一步压低 `THEIA_50per` 与 `THEIA_20per` 的 `R1–R18` GPU 搜索墙钟。
