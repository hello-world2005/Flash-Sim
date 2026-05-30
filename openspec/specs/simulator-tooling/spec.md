# Simulator Tooling Specification

## Purpose

Define the baseline tooling layer around the simulator, including trace parsing, address conversion helpers, standalone simulator entry points, command-line usage, and timeline export and visualization support.

## Requirements

### Requirement: Trace 解析器必须验证支持的命令格式

`parser.py` SHALL 支持从 JSON 字符串、文件路径或 Python 列表读取命令，并对 `read`、`write`、`static_write`、`search` 和 `compute` 的必填字段执行验证。

#### Scenario: 解析 trace 文件

- **WHEN** 调用 `parse_trace(...)` 读取一个 JSON trace 文件
- **THEN** 解析器 MUST 返回命令列表，并在缺少 `type` 或命令所需字段时抛出验证错误

### Requirement: 工具函数必须提供地址与区域转换能力

`utils.py` SHALL 提供 PPA 与 `FlashAddress` 之间的双向转换，以及把 LPA/LHA 转换为 search、compute 和 static 区域所需地址标识的辅助函数。

#### Scenario: LPA 转换为物理或静态区域标识

- **WHEN** FTL 或上层逻辑需要把逻辑地址转换为 PPA、search bank、compute bank 或 static 区域 LPA
- **THEN** 工具函数 MUST 返回与当前几何参数一致的目标地址分解结果

### Requirement: 仓库必须保留独立于事件驱动引擎的轻量仿真接口

仓库 SHALL 提供独立的 `FlashSimulator` 与对应 CLI 路径，用于在不启动事件调度引擎的情况下执行命令、计算延迟、查看几何信息和运行基准。

#### Scenario: 使用独立仿真器运行 trace

- **WHEN** 用户通过 CLI 的 `run` 子命令执行命令 trace
- **THEN** 系统 MUST 使用 `FlashSimulator` 逐条执行命令并输出每条命令的结果与延迟

### Requirement: CLI 必须同时暴露几何检查和事件驱动相关入口

`cli.py` SHALL 提供几何信息查询、LBA/物理地址转换、standalone trace 执行、交互模式、benchmark，以及面向事件驱动时间线导出的入口。

#### Scenario: 查询几何或地址信息

- **WHEN** 用户调用 `info`、`lba` 或 `addr` 等 CLI 子命令
- **THEN** CLI MUST 基于当前配置输出几何参数或地址转换结果，而不要求先运行事件驱动仿真

### Requirement: 时间线记录器必须导出 request 与 transaction 阶段信息

`TimelineRecorder` SHALL 能够挂接到 `Engine` 和 `PHY`，记录请求与事务在事件驱动执行过程中的关键阶段，并导出为可序列化 JSON。

#### Scenario: 附加时间线记录器

- **WHEN** `TimelineRecorder.attach(engine)` 被调用并随后执行事件驱动仿真
- **THEN** 记录器 MUST 捕获请求阶段、PHY 命令分发阶段和事务完成阶段，并生成包含 request/transaction segments 的 JSON 结构

### Requirement: 可视化工具必须能把时间线 JSON 渲染为交互式图表

`visualizer.py` SHALL 读取时间线 JSON，分别按 request stream 和 `channel x chip` 组织阶段条形图，并导出 HTML 结果。

#### Scenario: 渲染时间线 HTML

- **WHEN** 调用 `visualize_timeline(events_path, html_output, auto_open)`
- **THEN** 系统 MUST 读取事件 JSON，构建请求与事务两层时间线图，并把结果写出为 HTML 文件
