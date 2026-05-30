## Context

当前仓库同时存在两条使用路径：

- 以 `Engine` 为中心的事件驱动仿真路径，围绕 `Host -> PCIe -> HIL -> FTL -> PHY` 展开。
- 以 `FlashSimulator` 为中心的独立延迟计算路径，主要为命令执行、CLI 和快速测试提供接口。

用户本次要做的不是新增算法，而是把“当前已经存在的能力”变成一个可复用的 OpenSpec 基线。这个基线需要跨越多个模块，并且必须忠实反映代码现状，包括已经实现的行为、简化建模方式，以及尚未补齐的边界。

## Goals / Non-Goals

**Goals:**

- 为当前仿真器建立一份按能力拆分的正式规格基线。
- 优先描述代码中已确认实现的行为，而不是理想中的目标架构。
- 把 Host 队列、控制器缓存、映射页处理、GC、PHY 事件和辅助工具放入统一语义框架。
- 在规格中显式写出简化模型和已知限制，减少后续误解。

**Non-Goals:**

- 不在本 change 中修复 Host 队列实现、CLI 对接或其他运行时缺陷。
- 不把当前简化实现升级为完整 NVMe host-memory、DMA 或协议级模型。
- 不重构 `FlashSimulator` 与事件驱动引擎的双入口结构。
- 不对现有 FTL、TSU、GC 或 PHY 算法做行为变更。

## Decisions

### Decision: 按“已实现行为”而不是“理想架构”建基线

这份 spec 以代码里已经存在的可观察行为为准。例如 Host 侧当前维护了 `sq_heads`、`sq_tails`、`cq_heads`、`cq_tails` 和 `sq_entries`，但并没有建模完整的 host-memory-backed SQ/CQ DMA 语义，因此规格只承诺“轻量队列元数据与请求流控制”，不承诺完整 NVMe host memory。

备选方案是直接按目标架构撰写规格，但那会把尚未实现的内容伪装成现状，降低规格可信度，因此不采用。

### Decision: 将基线拆分为四个 capability

能力拆分为：

- `event-driven-simulation-runtime`
- `host-device-request-flow`
- `ftl-scheduling-and-media-model`
- `simulator-tooling`

这样可以把运行时主循环、请求数据路径、介质内部行为和辅助工具面分开描述。相比单一大 spec，这种拆分更利于后续 change 做局部修改，也更容易把场景写成可验证条目。

### Decision: 将简化点和限制写进规格正文

当前实现中有若干重要边界：

- `PCIe_link` 使用固定延迟模型。
- `WRITE` / `STATIC_WRITE` 请求在数据进入控制器缓存后即可完成，而不是等 NAND program 完成后再向 Host 报告。
- `SEARCH` / `COMPUTE` / `STATIC_WRITE` 通过静态区域地址映射走专用 chip 路径。
- 仓库同时保留事件驱动引擎与独立 `FlashSimulator`，两者并非同一执行内核。

这些点都会直接影响后续 change 的理解，因此应作为规格的一部分，而不是只放在设计备注里。

### Decision: 将辅助脚本与可视化视为正式能力面

`parser.py`、`utils.py`、`cli.py`、`timeline_recorder.py` 和 `visualizer.py` 已经形成面向开发和验证的工作流。它们虽然不是数据通路本身，但决定了用户如何运行、检查和理解仿真器，所以单独纳入 `simulator-tooling` capability。

## Risks / Trade-offs

- [Risk] 代码继续演进后，规格可能与实现漂移。
  Mitigation: 后续任何改变运行时行为的 change 都必须同步更新对应 capability。

- [Risk] 把“当前行为”写进规格，可能会暂时固化一些实现瑕疵或简化模型。
  Mitigation: 在规格和本设计中明确这些是“当前基线”，后续如需修正，应通过新的 change 显式修改 requirement。

- [Risk] 部分路径目前带有实现不完整或接口不一致的迹象，例如 Host 的队列排队辅助方法、CLI 与引擎入口假设。
  Mitigation: 本次只记录已确认的能力边界，不把未证实的理想行为写成 requirement。

## Migration Plan

本 change 只新增 OpenSpec 文档，无需迁移数据或部署代码。未来若围绕这些能力做实现性 change，应以本 change 为基线增量修改。

## Open Questions

- 是否要在后续 change 中把 Host 的 SQ/CQ 建模升级为真正的 host-memory-backed 队列？
- 是否要统一事件驱动引擎与独立 `FlashSimulator` 的对外入口与能力边界？
- 是否要单独立项修正当前 CLI `run-engine` 工作流与引擎接口之间的对接差异？
