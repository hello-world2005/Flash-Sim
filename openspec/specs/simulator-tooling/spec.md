# Simulator Tooling Specification

## Purpose

Define the baseline tooling layer around the simulator, including trace parsing, address conversion helpers, standalone simulator entry points, command-line usage, and timeline export and visualization support.
## Requirements
### Requirement: Trace 解析器必须验证支持的命令格式

`parser.py` SHALL 支持从 JSON 字符串、文件路径或 Python 列表读取命令，并对 `read`、`write`、`static_write`、`search` 和 `compute` 的必填字段执行验证。对于携带数据的命令，`parser.py` MUST 不再要求 `data_address` 或 `data_size`，而是以 `size` 作为唯一的请求长度指示。

#### Scenario: 解析 size-only trace 文件

- **WHEN** 调用 `parse_trace(...)` 读取一个包含 `write`、`static_write`、`search` 或 `compute` 命令的 JSON trace 文件，且这些命令只提供 `time`、`start_lha` 和 `size`
- **THEN** 解析器 MUST 返回命令列表，并仅对受支持的必填字段执行验证

#### Scenario: 解析 legacy extra fields

- **WHEN** 调用 `parse_trace(...)` 读取的命令中仍然包含 `data_address` 或 `data_size`
- **THEN** 解析器 MUST 不将这些字段当作必填条件，并继续基于其他必填字段完成验证

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

`cli.py` SHALL 提供几何信息查询、LBA/物理地址转换、standalone trace 执行、交互模式、benchmark，以及面向事件驱动时间线导出的入口。事件驱动入口 MUST accept the same `-c/--config` configuration file argument used by other CLI commands and MUST pass the parsed runtime policy into `Engine`.

#### Scenario: 查询几何或地址信息

- **WHEN** 用户调用 `info`、`lba` 或 `addr` 等 CLI 子命令
- **THEN** CLI MUST 基于当前配置输出几何参数或地址转换结果，而不要求先运行事件驱动仿真

#### Scenario: run-engine 使用配置化 GC/write-path 策略

- **WHEN** 用户调用 `flash-sim run-engine <trace> -c <config.json>` 且配置文件包含 runtime GC/write-backpressure policy
- **THEN** CLI MUST construct `Engine` with the parsed `FlashConfig` so that the event-driven FTL uses the configured `gc_low_watermark`, `stop_servicing_writes_threshold`, `gc_victim_policy`, and `static_wl_wear_gap_threshold`

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

### Requirement: Standalone trace execution preserves caller-provided logical addresses
The standalone simulator tooling path formed by `parse_trace(...)`, `flash_sim.cli run`, and `FlashSimulator` SHALL preserve non-zero logical addresses and operation parameters from standalone traces. Commands expressed with standalone simulator fields such as `lba`, `address`, `block_address`, `wl_count`, `block_count`, or `layer` MUST be passed through or normalized without silently falling back to zero-valued addresses.

#### Scenario: Standalone trace keeps a non-zero read or write address
- **WHEN** a caller runs a standalone trace containing a `read` or `write` command with a non-zero logical address
- **THEN** the resulting `FlashSimulator` execution MUST target that same non-zero logical address instead of coercing it to `0`

#### Scenario: Standalone trace keeps operation-specific parameters
- **WHEN** a caller runs a standalone `search`, `compute`, or `erase` command that carries `wl_count`, `block_count`, `layer`, or `block_address`
- **THEN** the standalone tooling path MUST preserve those fields so the executed command reflects the caller-provided parameters

### Requirement: Standalone and engine trace schemas fail explicitly when mixed
The repository SHALL distinguish between standalone simulator traces and event-driven engine traces. If a caller routes an engine-style command set using fields such as `time`, `start_lha`, and `size` into the standalone simulator path, the tooling MUST reject it explicitly or require the caller to use the engine entrypoint, rather than silently interpreting the command as a valid standalone simulator request.

#### Scenario: Engine trace is rejected by the standalone runner
- **WHEN** `flash_sim.cli run` or another standalone simulator path receives a trace whose commands are expressed with engine-only fields such as `time`, `start_lha`, and `size`
- **THEN** the tooling MUST return a validation error or redirect to the dedicated engine path instead of executing a different logical address than the trace requested

### Requirement: Event-driven tooling exports request latency reports under the report directory

事件驱动仿真入口在完成仿真后 SHALL 把请求级延时统计导出到 `report/` 目录，而不是混入纯文本运行日志。输出文件名 MUST 能够从输入 trace 文件稳定派生，以避免不同 trace 的报告互相覆盖。

#### Scenario: Engine entrypoint writes a trace-scoped report file

- **WHEN** 用户通过 `flash_sim/main.py` 或其他事件驱动入口运行一个名为 `<trace>.json` 的 trace
- **THEN** 系统 MUST 在 `report/` 目录下生成一个与 `<trace>` 对应的请求级 JSON 报告文件

#### Scenario: Different traces do not overwrite each other's reports

- **WHEN** 用户依次运行两个不同文件名的事件驱动 trace
- **THEN** 系统 MUST 为它们生成两个不同的请求级报告文件，而不是复用单一固定文件名

### Requirement: Request latency reports remain machine-readable and testable

请求级报告 MUST 采用稳定的 JSON 结构，至少包含 `meta` 与 `requests` 顶层字段；每条请求记录 MUST 使用固定字段名表达总时延、阶段 breakdown 和状态信息，以便自动化测试直接加载和断言，而不依赖控制台日志文本。

#### Scenario: Automated test validates a generated report

- **WHEN** 一个自动化测试在仿真结束后读取请求级报告文件
- **THEN** 测试 MUST 能够仅通过解析 JSON 结构断言请求数量、阶段字段存在性以及关键延时值，而不需要解析 stdout 或 `output/*.log`

### Requirement: GC pressure matrix runner covers every maintained pressure variant

`python -m flash_sim.gc_pressure_matrix` SHALL run the complete GC pressure validation matrix through the workspace root `.venv` Python when available. By default the matrix MUST include all maintained `gc_pressure_trace*.json` timing/working-set variants, the specialized low-invalid, concurrent-overwrite, post-flush sustained-write, and in-flight-GC re-overwrite regressions, plus the auxiliary `gc_stress_test.json`, `gc_mini_test.json`, and `gc_test.json` regressions. Each trace run MUST write its own request latency JSON/CSV reports and a per-trace log, and the runner MUST aggregate machine-readable results into `report/gc_pressure_matrix_results.json` unless the caller overrides the output path.

#### Scenario: Default matrix includes all pressure traces

- **WHEN** a caller runs `python -m flash_sim.gc_pressure_matrix` without trace filters
- **THEN** the runner MUST execute `gc_pressure_trace.json`, `gc_pressure_trace_fast.json`, `gc_pressure_trace_slow.json`, `gc_pressure_trace_slow2.json`, `gc_pressure_trace_wide.json`, `gc_pressure_trace_5000000ns.json`, `gc_pressure_trace_10000000ns.json`, `gc_pressure_trace_15000000ns.json`, `gc_pressure_trace_20ms.json`, `gc_pressure_low_invalid.json`, `gc_pressure_concurrent_overwrite.json`, `gc_pressure_post_flush_sustained.json`, and `gc_pressure_gc_reoverwrite.json`

#### Scenario: Wide pressure trace has a distinct physical working set

- **WHEN** the maintained pressure trace assets are validated
- **THEN** `gc_pressure_trace_wide.json` MUST exercise more unique LPAs and more physical planes than the single-plane `gc_pressure_trace_20ms.json`; it MUST NOT be a byte-for-byte timing alias

#### Scenario: Runaway trace is bounded without hiding later results

- **WHEN** one trace exceeds the configured per-trace wall-clock timeout
- **THEN** the runner MUST mark that trace failed with a timeout issue and MUST continue executing the remaining traces

#### Scenario: Default matrix includes auxiliary GC regressions

- **WHEN** a caller runs `python -m flash_sim.gc_pressure_matrix` without `--pressure-only`
- **THEN** the runner MUST also execute `gc_stress_test.json`, `gc_mini_test.json`, and `gc_test.json`

#### Scenario: Static WL has an event-path regression

- **WHEN** the automated test suite validates static wear leveling
- **THEN** it MUST exercise a complete static-WL relocation through the real `Engine`, `TSU`, and `PHY`, and MUST verify mapping, media data, block erase state, barrier release, and maintenance-event conservation without substituting a fake scheduler or fake media layer

#### Scenario: Matrix summary records per-trace health and maintenance metrics

- **WHEN** a trace finishes or fails inside the matrix runner
- **THEN** the matrix summary MUST keep a result entry for that trace, including total request count, SUCCESS/ERROR/incomplete counts, final simulation time, request latency report paths, GC/static-WL counts, relocated pages, erased blocks, host/physical-user/physical-GC write page counts, write amplification, minimum free pool, maximum wear skew, current and maximum waiting writes, backpressure wait time, residual waiting queue count, pending cache entry count, and any exception information available

#### Scenario: Matrix distinguishes correctness issues from workload warnings

- **WHEN** a completed trace contains ERROR requests or violates maintenance event conservation between started relocations, completed erases, physical GC writes, and write amplification
- **THEN** the runner MUST record a correctness issue for that trace
- **WHEN** a completed trace has write amplification below `1.0` while its reported counters remain internally consistent
- **THEN** the runner MUST retain that condition as a workload/coalescing warning rather than a correctness issue

#### Scenario: One failing trace does not abort the matrix

- **WHEN** one trace raises an exception or produces incomplete requests
- **THEN** the runner MUST record the issue for that trace and continue executing the remaining traces
