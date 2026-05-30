# Event Driven Simulation Runtime Specification

## Purpose

Define the baseline behavior of the simulator's event-driven runtime, including module construction, event scheduling, trace ingestion, preconditioning, and termination semantics.

## Requirements

### Requirement: 事件优先队列驱动仿真推进

`Engine` SHALL 使用按时间排序的 `PriorityQueue` 作为统一的仿真推进机制，并通过 `SimEvent` 驱动所有模块行为。

#### Scenario: 执行最早到期事件

- **WHEN** 仿真过程中已经注册了多个未来事件
- **THEN** `Engine` MUST 取出时间最早的事件，将 `current_time` 更新为该事件的 `time`，并调用 `event.target.execute(event)`

### Requirement: Engine 构建统一模块拓扑并暴露全局调度钩子

`Engine` SHALL 在初始化时构建 `Host`、`Device` 和 `PCIe_link`，并通过 `common.py` 暴露当前仿真时间提供器和未来事件注册器，使下层模块可以在不直接依赖 `Engine` 的情况下访问时间与调度能力。

#### Scenario: Engine 初始化模块图

- **WHEN** 创建一个新的 `Engine`
- **THEN** 系统 MUST 创建 `Host`、`Device` 和 `PCIe_link` 实例，完成它们之间的引用注入，并设置 `_time_provider` 与 `_event_scheduler`

### Requirement: 仿真启动前执行构造校验与预处理

事件驱动仿真在真正回放 trace 之前 SHALL 先校验模块构造，再调用 `block_manager.preconditioning(...)` 建立初始 flash 状态和映射基线。

#### Scenario: 启动仿真

- **WHEN** 调用 `Engine.Start_simulation(trace_path)`
- **THEN** 系统 MUST 先验证 `Host`、`Device` 和 `PCIe_link` 的构造，再执行 block manager 预处理，然后才开始初始化事件队列

### Requirement: Trace 命令必须被转换为初始请求事件

事件驱动入口 SHALL 通过 `parse_trace(...)` 读取 JSON trace，并把每条命令转换为一个 `REQ_INIT` 事件，事件参数中携带 `Request` 对象与原始调度时间。

#### Scenario: 从 trace 初始化事件队列

- **WHEN** trace 中包含带有 `time`、`type`、`start_lha` 和 `size` 的命令
- **THEN** `Engine` MUST 为每条命令创建一个 `Request`，并向事件队列注册一个以该命令 `time` 为触发时间的 `REQ_INIT` 事件

### Requirement: 仿真在事件耗尽时结束

主运行循环 SHALL 持续执行事件，直到事件队列为空为止。

#### Scenario: 事件队列清空

- **WHEN** `Run()` 执行过程中事件队列已经为空
- **THEN** `Engine` MUST 停止继续取事件，并以当前仿真时间作为最终结束时间
