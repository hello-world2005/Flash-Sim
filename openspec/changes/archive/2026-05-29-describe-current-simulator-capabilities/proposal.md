## Why

当前仓库已经具备事件驱动 Flash 仿真器的主干实现，但这些能力仍主要散落在代码和临时说明中，缺少一个可被后续 change 复用的正式基线描述。现在补齐这份 change，可以把“当前已经实现了什么”和“当前仍然简化了什么”明确下来，避免后续设计、实现和评审反复回到口头约定。

## What Changes

- 为当前仿真器建立一份 OpenSpec 基线，描述事件驱动运行时、模块连接关系和请求到事务的生命周期。
- 描述 Host、PCIe、HIL 之间的请求流，包括 SQ/CQ 元数据、数据获取、请求切分和控制器侧 data cache 的现状。
- 描述 FTL、AMU、TSU、Block Manager、GC、PHY 的职责分工，以及 search/compute/static write 的专用静态区域路径。
- 描述现有辅助能力，包括 trace 解析、地址转换工具、独立 `FlashSimulator`/CLI，以及 timeline 记录和可视化。
- 显式记录当前实现中的简化边界和已知约束，例如 Host 侧并未建模完整的 host-memory-backed NVMe 队列。

## Capabilities

### New Capabilities
- `event-driven-simulation-runtime`: 描述事件队列、模块构建、trace 注入和预处理驱动的整体运行时。
- `host-device-request-flow`: 描述 Host、PCIe 链路、HIL、控制器缓存和请求完成语义。
- `ftl-scheduling-and-media-model`: 描述地址映射、事务调度、块状态管理、垃圾回收和 PHY 执行模型。
- `simulator-tooling`: 描述 trace/parser、地址转换工具、独立仿真入口、CLI 和时间线可视化能力。

### Modified Capabilities

None.

## Impact

这次 change 只新增 OpenSpec 文档基线，不修改运行时代码。受影响的实现参考范围包括 `flash_sim/main.py`、`engine.py`、`Host.py`、`Device.py`、`HIL.py`、`FTL.py`、`PHY.py`、`pcie_link.py`、`common.py`、`parser.py`、`utils.py`、`simulator.py`、`cli.py`、`timeline_recorder.py` 和 `visualizer.py`。后续所有涉及这些模块行为的 change 都应基于这份基线增量修改。
