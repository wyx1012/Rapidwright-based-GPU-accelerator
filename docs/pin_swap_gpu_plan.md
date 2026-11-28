# 开启 LUT 引脚交换（`lutPinSwapping=true`）下的 GPU 加速策略与 `v96` 实现验证

> **状态（2026-11-28 更新）：`v96` 已按本文档思路完成代码实现、`gpu02` 编译，并在 `gpu02` 同 NUMA 双卡 `--gpus 2,3` 上完成了本机 CPU 基线开启引脚交换的全部 `9 / 9` 非 `k_*` 用例（含 `THEIA_50per`）运行与双重校验，全部一次通过（`unroutedPins=0, sharedPipNodes=0`）。完整数据与对比分析见 [v96_pin_swap_report.md](v96_pin_swap_report.md)。**  
> 基线版本：`v95`（空间分区与热循环引擎）→ **`v96`（`theia50per-ppt-hybrid-v96`）**。

## 1. 现状与动机

- `v70` / `v76` / `v94` / `v95` 在 13 个用例上均以 `lutPinSwapping=false` 运行（`gpu02` 日志均打印 `LUT pin swapping: false`，且 `FidesLaneGate` 守卫 `lutRoutethru=false`）。
- 本机 Java CPU 基线（`CPU布线日志.txt`）中，9 个非 `k_*` 用例为 `lutPinSwapping=true`，4 个 `k_*` 用例为 `false`；因此两者的线长/PIP 口径并不完全相同（见 `docs/v94_ppt_report.md`）。
- 若要与开启交换的 Java 基线做严格同口径比较，或追求更低线长，需要让 GPU 路径支持引脚交换。

## 2. Java 参考实现中引脚交换是怎么做的（已读源码核实）

| 阶段 | 位置 | 行为 |
| :--- | :--- | :--- |
| 连接建立 | [RWRoute.java L808–L857](../src/java/RWRoute.java) | 对 LUT 输入汇点，默认允许 6 个引脚互换；对每个等价引脚取其 INT 节点，经 `routingGraph.getOrCreate(node, sinkType)` 创建 `altSinkRnode`，并 `connection.addAltSinkRnode(...)`。 |
| 不允许交换的情形 | 同上 L812–L840 | 连接的 cell 不在同字母 LUT（分布式 RAM 共用引脚）→ 0；`H` BEL 上的 RAM → 0；`SRL*` → 0；存在 5LUT cell → 仅前 5 个引脚可换。 |
| 汇点类型 | 同上 L859–L870 | 有 alt sink 时汇点保持 `LOCAL_*`（非独占）；无 alt sink 才改成 `EXCLUSIVE_SINK_*` 并立即 `incrementUser`。 |
| 图扩展约束 | [RouteNodeGraph.java L784–L790](../src/java/RouteNodeGraph.java) | `NODE_PINFEED` 子节点在 `lutRoutethru \|\| lutPinSwapping` 时允许是 local 类型。 |
| 路由后提交 | [RWRoute.java L3707–L3728](../src/java/RWRoute.java) | `postRouteProcess` 中，若 `rnodes.get(0)`（路径实际终点）不等于原 `sinkRnode`，则收集 `(旧 SitePinInst → 新引脚名)`，一次性调用 `LUTTools.swapMultipleLutPins(pinSwaps)` 更新站内布线。 |
| 现有 fast-path 排除 | `RWRoute.java` 中 `c.hasAltSinks()` 相关判断（如 L1961、L1968、L2149、L2539） | 带 alt sink 的连接已被若干快速路径显式排除，说明这类连接本来就是“特殊处理”的。 |

要点：Java 版本的交换本质上是 **“一条连接有多个可接受的终点节点 + 路径结束后再把站内引脚映射改过去”**。

## 3. 为什么当前 GPU 路径没有开启

- GPU 内核按“单 `src` → 单 `sink`”组织（`ConnData{src, sink}`，A\* 的目标判定与启发式 `estWl` 均针对单一汇点）。
- 汇点被固化为 `EXCLUSIVE_SINK`，占用计数在连接建立时即 `incrementUser`；而有 alt sink 的汇点是 `LOCAL_*`，占用在路由期间才确定。
- 扁平 RRG（`rrg.bin` / `rrg.manifest.txt`）是为 `lutPinSwapping=false` 的节点集合导出的；alt sink 对应的 INT 节点是否都在“设计可达闭包”中，未做验证。

## 4. 拟议的 GPU 加速思路

### 4.1 多目标终点集（Goal Set）接入现有 A\*
1. **静态预处理（CPU，一次）**：沿用 Java 逻辑，对每条 LUT 汇点连接生成 `altSink` 节点集合 `T = {sink, alt1..alt5}`（最多 6 个），并保留 Java 的不可交换判定（RAM / SRL / 5LUT 规则）。把 `T` 以 CSR 形式（`offsets[]`、`nodes[]`）放入 `ConnData` 旁路数组，常驻显存。
2. **目标判定**：Tier-A（warp）与 Tier-B1（CTA）在“弹出节点是否为目标”处，由 `node == sink` 改为“`node ∈ T` 且该节点当前对本网络可用”。`T` 最大 6 个，可放入寄存器/共享内存做线性比较。
3. **启发式**：用 `T` 中各节点 tile 坐标的包围盒/最近点作为 `estWl` 的目标点，保持可采纳性（取最小距离）。
4. **占用语义**：命中某个 `t ∈ T` 时，只对被选中的 `t` 加占用；其余候选不占用（与 Java 中 `LOCAL_*` 汇点在路径提交时才 `incrementUser` 一致）。

### 4.2 两阶段提交（路由内只记“选了哪个”，物理交换延后）
1. GPU 波前提交时，只记录 `chosenSinkIdx[conn]`（`T` 中下标），不触碰站内布线。
2. 每轮或最终写回前，由 CPU 汇总 `(oldSitePin → newPinName)`，调用与 Java 相同的 `LUTTools.swapMultipleLutPins`，保证与 Java 的 DCP 语义一致。
3. 同一 LUT 内多个汇点互相冲突（两条连接都选了同一物理引脚）需在“波内冲突检测”中视为同一资源冲突：复用现有 `waveCommitKey` / `gpuDirtyOccStamp` 机制，把 `t` 当作普通 INT 节点参与判冲突；由此天然保证同一引脚不会被两条连接同时占用。

### 4.3 与 v95 空间分区的关系
- `T` 中节点都在同一 LUT 所在 tile 的 INT 邻域内，空间位置几乎重合，因此不会破坏 v95 `reg0/reg1` 的“互不相交”判定；`ItemBBox` 只需把 `nx0..ny1` 扩到 `T` 的包围盒即可。
- 作为边界处理：若 `T` 跨越切线（极少见），该连接归入 `bound` 集合，照常处理。

## 5. 主要风险与待验证点

| 风险 | 说明 | 验证方式 |
| :--- | :--- | :--- |
| RRG 导出闭包是否包含全部 alt sink 节点 | 当前 `rrg.bin` 是按 `swap=false` 导出的 | 开 `lutPinSwapping=true` 重新导出并比较 `N`/`M`；检查 `FidesLaneGate` 的 “Initial live nodes missing from the RRG manifest” 是否触发 |
| `FidesLaneGate` 守卫 | 目前对 `lutRoutethru` 有 `must be false` 守卫；需确认对 `lutPinSwapping` 的假设 | 阅读 `FidesLaneGate.java` 相关守卫后再改 |
| fast-path 与 `hasAltSinks()` 排除 | Java 侧已有多处对带 alt sink 连接的特殊排除；GPU/C++ 侧需对齐，避免走到不支持 alt sink 的路径 | 对照 `RWRoute.java` L1961 / L1968 / L2149 / L2539 逐条核对 |
| 合法性 | 交换会改站内布线，必须仍满足 `unroutedPins=0, sharedPipNodes=0` | 沿用现有“进程内 64 分片 + 冷启动 `F4BaselineDcpCheck`”双重校验 |
| 与 Java 结果一致性 | 线长应与开启交换的 Java 基线同量级 | 在 9 个非 `k_*` 用例上与 `CPU布线日志.txt` 对比 `WL` / `PIPs` |

## 6. `v96` 实际落地与实测验证记录（2026-11-28）

### 6.1 已落地代码与构建脚本
- **Java 侧接入与物化**：
  - [FidesNativeCpu.java](../src/java/FidesNativeCpu.java)：新增 `nativeRegisterAltSinks(int[] altOffsets, int[] altIds)` JNI 接口。
  - [FidesLaneGate.java](../src/java/FidesLaneGate.java)：`f4NativeRegisterAll` 放行 `LOCAL_EAST / LOCAL_WEST / LOCAL_BOTH` 且含 `1..5` 个候选交换汇点（标志位 `bit 7`），批量注册 `altSinks`；`f4NativeMaterializeAll` 与 20 路校验核对路径实际终点 `path[0]`，绑定正确 `actualSink` 并更新局部引用计数，由 `RWRoute.postRouteProcess()` 中的 `LUTTools.swapMultipleLutPins` 完成站内交换与修复。
  - [RWRoute.java](../src/java/RWRoute.java) 与 [PartialRouter.java](../src/java/PartialRouter.java)：在 `THEIA_jpeg` 实测中定位并修复了 RapidWright `categorizeNets()` 在 `lutPinSwapping=true` 下 `preserveAsync()` 与 `routingGraph.isPreserved(node)` 并发竞态导致的 `Initial live nodes missing from the RRG manifest`（`unmapped=5`）问题——开启引脚交换时改为两阶段确定性顺序，先统一提交并 `awaitPreserve()` 所有保留节点再构建 `altSinkRnodes`（同步修复 `removeIf(RouteNode::isUsed)` 清空候选后的 `EXCLUSIVE_SINK` 兜底转换）。
- **CUDA / C++ 多目标 Goal Set**：
  - [fides_gpu.h](../src/cpp/fides_gpu.h) & [fides_gpu.cu](../src/cpp/fides_gpu.cu)：`FidesGpuQuery` 新增 `FG_QF_HAS_ALTS = 1u << 6`、`altCount` 与 `altSinks[5]`（保持 16B 对齐）；Tier-A warp 与 Tier-B1 CTA 实现 Goal Set 包围盒/启发式最小距离、非独占终点的同网碰撞互斥及路径首位 `[sink_chosen .. src]` 重建。
  - [fides_cpu.cpp](../src/cpp/fides_cpu.cpp)：实现 C++ CPU Goal Set、非独占终点 `SINK_CLAIM_BIT` 提交仲裁、`ItemBBox` 覆盖候选汇点集及 `RouteTail` 路径范围对齐。
- **构建与测试入口**：[scripts/v96/](../scripts/v96/)（`v96_patch.py` / `prepare_v96.py` / `deploy_v96.py` / `launch_v96.py`）及 [run_all13.py](../scripts/all13/run_all13.py)（`--pin-swap` 使用隔离缓存 `all13-rrg-cache-pinswap` 并向 `worker.py` 注入 `--lutPinSwapping`）。

### 6.2 `gpu02`（`2 × Tesla V100`，`--gpus 1,3`）双用例验证结果

两个代表性用例均在最终代码编译通过（`compile_java/cpp/gpu_obj/cuda.log` 全 0 报错）后完成**进程内 64 分片 + 冷启动 JVM `F4BaselineDcpCheck` 双重校验（`unroutedPins=0, sharedPipNodes=0`）**：

| 用例 | 口径 / 版本 | 状态 & 合法性 | E2E (s) | Route design (s) | Wirelength | PIPs | 备注 |
| :--- | :--- | :---: | ---: | ---: | ---: | ---: | :--- |
| **`mcml_RLE`**<br>(`20,259` 间接连接) | `v95-t6` (`swap=false`) | PASS (`0 / 0`) | `21.68` | `12.22` | `140,123` | `149,392` | `gpu_commits=0` |
| **`mcml_RLE`** | **`v96` (`swap=true`)** | **PASS (`0 / 0`)** | **`20.82`** | **`11.64`** | **`126,558` (`-9.68%`)** | **`125,158` (`-16.22%`)** | `alt_conns=17,043 / 20,259` (`84.1%`)；`F4_V66_POST lut=0.15s` |
| **`mcml_RLE`** | 本机 Java CPU (`swap=true`) | PASS | `13.77` | `10.47` | `124,083` | — | 纯串行 CPU 基线 |
| **`THEIA_jpeg`**<br>(`639,637` 间接连接) | `v94` (`swap=false`) | PASS (`0 / 0`) | `135.68` | `97.90` | `3,110,070` | `5,008,227` | `2×V100 [2,3]` |
| **`THEIA_jpeg`** | `v95-t6` (`swap=false`) | PASS (`0 / 0`) | `111.08` | `77.66` | `3,144,770` | `5,024,251` | `gpu_commits=907,217`, `gpu_ns=18.01s`, `nodes_popped=3.128B` |
| **`THEIA_jpeg`** | **`v96` (`swap=true`)** | **PASS (`0 / 0`)** | **`110.28`** | **`75.82`** | **`2,271,918` (`-27.76%` vs `v95-t6`)** | **`4,027,195` (`-19.84%`)** | **`alt_conns=520,673` (`81.4%`)；`gpu_commits=781,196`, `gpu_ns=10.13s (-43.7%)`, `nodes_popped=2.280B (-27.1%)`, `lut=3.01s`** |
| **`THEIA_jpeg`** | 本机 Java CPU (`swap=true`) | PASS | `1,561.64` | `1,548.01` | `2,159,365` | — | `v96` 对比 CPU `swap=true` 实现 **E2E `14.16×` / Route `20.42×`** 加速 |

> 注：全套 9 个非 `k_*` 引脚交换用例均已在 `gpu02 --gpus 2,3` 上全部双校验通过，三方对比明细见 [v96_pin_swap_report.md](v96_pin_swap_report.md)（[results/v96_pin_swap_9/summary.jsonl](../results/v96_pin_swap_9/summary.jsonl)）。
