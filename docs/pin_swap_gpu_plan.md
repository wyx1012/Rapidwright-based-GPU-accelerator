# 开启 LUT 引脚交换（`lutPinSwapping=true`）下的 GPU 加速策略记录

> **状态：仅为设计思路，尚未实现、尚未在服务器上验证，本文不含任何实测性能数字。**
> 记录时间：2026-11-27。适用基线：`v94` / `v95` 的 `fides_cpu.cpp` + `fides_gpu.cu` 常驻 CSR 架构。

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

## 6. 建议的实施顺序（未执行）

1. 只读核查：`FidesLaneGate`、`F1Corridor`、`FidesRrgLoader` 对 alt sink 的假设。
2. 在 `mcml_RLE` / `RLE_BlobMerging` 等小用例上开启 `lutPinSwapping=true`，先走 CPU 路径（不启用 GPU 波前）验证 alt sink 的 C++ 目标集逻辑与 DCP 合法性。
3. 把 Goal Set 接入 Tier-A；再接入 Tier-B1；最后处理 CPU `DynamicExecutor` 与 Pass 3 重叠路径。
4. 全 13 用例双重校验后，再与 `v94` 非交换口径、本机 Java 交换口径分别对比。
