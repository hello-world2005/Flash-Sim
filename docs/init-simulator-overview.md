# Flash-Sim Current Simulator Overview

## Overview

本文件总结当前仓库中已经实现的 Flash 仿真器能力，作为 `openspec/specs/` 中主规格的配套说明。它描述的是当前代码现状，而不是未来目标架构。

当前仓库同时包含两条主要使用路径：

- 事件驱动仿真路径：以 `flash_sim/engine.py` 为入口，围绕 `Host -> PCIe -> HIL -> FTL -> PHY` 推进完整事件流。
- 独立延迟计算路径：以 `flash_sim/simulator.py` 和 `flash_sim/cli.py` 为入口，用于快速执行命令、输出延迟和调试几何模型。

## Architecture

### Event-driven path

事件驱动路径由 `Engine` 统一维护一个 `PriorityQueue`。所有需要建模时间延迟的动作都会注册为未来 `SimEvent`，由引擎不断取出队首事件并推进 `current_time`。

主要模块关系如下：

1. `Engine` 构造 `Host`、`Device` 和 `PCIe_link`，并将时间提供器与事件注册器注入 `common.py`。
2. `Host` 接收初始 `REQ_INIT` 事件，完成轻量级 SQ 选择、IO flow 占用和请求发送。
3. `PCIe_link` 维护双向消息队列，并用固定链路时延把消息转为未来 `DELIVER` 事件。
4. `Device` 当前主要把外部事件转交给 `HIL` 处理。
5. `HIL` 负责请求切分、控制器缓存访问、向 Host 取数，以及把需要进一步处理的事务送入 `FTL`。
6. `FTL` 负责地址映射、事务调度、块状态管理和垃圾回收。
7. `PHY` 负责模拟芯片命令传输、阵列内部执行、数据回传，以及页内容读写。

### Standalone path

`FlashSimulator` 不依赖事件调度引擎。它更像一个简化的命令执行接口，直接基于 `FlashChip` 和简化 `FTL` 返回每条命令的物理地址与延迟。这条路径目前主要服务于：

- `flash_sim/cli.py run`
- `info` / `lba` / `addr` / `interactive` / `bench`
- 快速验证 timing 和 geometry 逻辑

## Core Runtime Behavior

### Engine and events

`Engine` 在启动仿真时会先做构造校验，然后调用 `device.ftl.block_manager.preconditioning(...)` 建立初始 flash 状态。之后它通过 `parse_trace(...)` 读取 JSON trace，把每条命令包装成 `Request` 并注册为 `REQ_INIT` 事件。

仿真结束条件非常直接：当事件队列为空时，`Run()` 终止，当前时间即为最终仿真时间。

### Preconditioning

Block Manager 的预处理阶段会从 `pre_data/precondition_data.json` 读取数据，完成以下动作：

- 初始化每个 plane 上的 valid / invalid / free 页面分布
- 把初始用户页内容直接写入 `PHY._storage`
- 为 mapping page 写入初始内容并建立 `GTD`
- 预热部分 `CMT` 条目

这使得仿真一开始就处于“已有部分数据和映射”的状态，而不是完全空白的 flash。

## Host, PCIe, and HIL

### Host model

当前 Host 采用轻量化建模方式。它维护：

- `sq_heads` / `sq_tails`
- `cq_heads` / `cq_tails`
- `sq_entries`
- `IO_Flow`
- `waiting_req`

这些结构足以表达请求分配、队列占用和完成通知，但它还不是完整的 host-memory-backed NVMe 队列模型。当前实现更接近“Host 侧控制流 + 简化数据存取”。

### PCIe model

`PCIe_link` 有两条消息队列：

- `host_to_device_queue`
- `device_to_host_queue`

每个方向同一时刻只推进一条消息，链路延迟目前是固定值 `100`。这说明当前 PCIe 路径是串行消息通道模型，而不是带宽/分片/协议细节齐全的总线模型。

### HIL responsibilities

`HIL` 是 SSD 的接口层，核心职责包括：

- 接收 Host 发来的 PCIe 请求消息
- 将 `READ` / `WRITE` 按页和 sector bitmap 切分为事务
- 将 `SEARCH` / `COMPUTE` / `STATIC_WRITE` 按静态区域粒度切分为事务
- 为写、搜、算请求向 Host 取 payload 数据
- 查询和写入控制器侧 `Data_Cache`
- 把需要继续处理的事务交给 `FTL`

### Cache behavior

控制器侧 cache 由 `Data_Cache` 和 `Cache_Manager` 实现。当前行为有几个重要特征：

- `READ` 会先查 cache，命中后直接完成，不进入 flash 访问路径
- `WRITE` 和 `STATIC_WRITE` 的数据先写入 cache
- 当 cache 空间不足时，`Cache_Manager.write_flush()` 会把待刷写内容重新封装成事务并交给 AMU/FTL

一个非常关键的现状是：`WRITE` 和 `STATIC_WRITE` 在数据进入控制器 cache 后，`HIL` 就会向 Host 发送 `REQ_COMP`。也就是说，当前完成语义是“控制器已接收”，不是“NAND program 已落盘”。

## FTL, TSU, Block Manager, and GC

### Address Mapping Unit

AMU 使用三层映射相关状态：

- `CMT`
- `GMT`
- `GTD`

它负责：

- 把用户 LPA 翻译到物理地址
- 在映射缺失时生成 `MAPPING_READ`
- 在映射更新时生成 `MAPPING_WRITE`
- 处理 mapping page 的读回响应

当前用户页更新和 mapping page 更新都采用 out-of-place update，即新内容写到新 PPA，再更新映射并让旧位置失效。

### Static area

`SEARCH`、`COMPUTE` 和 `STATIC_WRITE` 走静态区域路径。`FTL.get_static_address(...)` 会把这类请求映射到保留的 static chip 上，使它们与普通随机访问数据路径分离。

### TSU scheduling

TSU 维护一个按 `channel -> chip -> transaction_type` 分组的队列结构。调度器的核心特点是：

- channel 空闲时触发调度
- 对同一 channel 内的 chip 做 round-robin 轮询
- 对普通读写擦操作按优先级尝试激活
- 对 static chip 优先尝试 `COMPUTE`、`SEARCH`、`STATIC_WRITE`
- 发射时按 die 粒度选择事务，并满足 plane 约束
- 对普通读写支持一定条件下的 suspend / resume

此外，TSU 会检查 barrier，阻止以下事务过早发射：

- 与同一 LPA 冲突的写/迁移事务
- 与同一 MVPN 冲突的 mapping 写事务
- 被 GC 擦除屏障挡住的事务

### Block Manager

Block Manager 为每个 plane 维护：

- `free_page_count`
- `valid_page_count`
- `invalid_page_count`
- `free_block_pool`
- `write_frontier_block`
- 每个 block 的 `write_frontier`、`valid_pages`、`invalid_pages`、`wl_level`

这些信息既被 AMU 用于分配新页，也被 GC/TSU 用于执行保护和回收。

### Garbage collection

GC 由 `GC_WL_Manager` 驱动。当前逻辑是：

1. 当 `free_block_pool` 低于阈值时扫描对应 plane。
2. 选择 invalid page 最多的 block 作为 victim。
3. 将 victim 中的 valid page 转换为 `GC_READ + GC_WRITE` 事务链。
4. 为所有迁移完成后的 victim block 提交 `GC_ERASE`。
5. 擦除完成后重置 block bookkeeping，并把 block 放回 free pool。

当前 GC 已经是完整的事务链式模型，不是瞬时清理。

## PHY and Media Execution

`PHY` 是当前 flash 介质执行模型的核心。它维护：

- channel busy 状态
- chip / die bookkeeping
- callback lists
- 多维 `_storage` 作为页内容存储

事务进入 PHY 后通常经历三个阶段：

1. 命令/地址传输完成事件
2. 芯片内部执行完成事件
3. 对读、搜、算类事务的数据回传事件

在写入类事务完成时，PHY 会把 payload 写入 `_storage`；在读取类事务完成时，PHY 会从 `_storage` 取回 `PageData` 作为响应。之后它通过 transaction serviced 回调通知：

- `HIL`
- `AMU`
- `Block_Manager`
- `TSU`

这让上层可以分别完成请求完成、映射更新、块状态更新和重新调度。

## Tooling and Interfaces

### Parser

`parser.py` 负责：

- 读取 JSON 字符串、文件或列表
- 校验 `read` / `write` / `static_write` / `search` / `compute`
- 输出结构化命令列表

### Address helpers

`utils.py` 提供：

- `PPA <-> FlashAddress` 转换
- `LHA -> LPA`
- `LPA -> search bank`
- `LPA -> compute bank`

这些函数支撑 FTL 和静态区域路径。

### CLI

`cli.py` 当前提供多组入口：

- `info`
- `lba`
- `addr`
- `run`
- `run-engine`
- `interactive`
- `bench`

其中 `run` 走独立 `FlashSimulator` 路径，`run-engine` 面向事件驱动仿真与 timeline 导出。

### Timeline and visualization

`timeline_recorder.py` 会通过 hook `Engine` 和 `PHY` 记录请求与事务阶段，导出 JSON。

`visualizer.py` 则把这些 JSON 渲染为两层时间线：

- request 视图：按 `stream_id`
- transaction 视图：按 `channel x chip`

这套工具链很适合分析请求和事务在事件驱动仿真中的流动。

## Current Boundaries And Known Simplifications

当前实现的几个重要边界如下：

- Host 队列是轻量元数据建模，不是完整 host-memory NVMe 模型
- PCIe 链路是固定延迟串行消息模型
- `WRITE` / `STATIC_WRITE` 在控制器接收后即完成，不等待 NAND 落盘
- 仓库同时保留事件驱动引擎与独立 `FlashSimulator` 两套入口
- 一些外围接口仍可继续收敛，例如 CLI 与事件驱动入口之间的对接一致性

这些边界并不代表设计错误，而是当前版本的明确基线。后续如果要改动，应通过新的 OpenSpec change 增量修改。

## Related Specs

当前主规格位于：

- `openspec/specs/event-driven-simulation-runtime/spec.md`
- `openspec/specs/host-device-request-flow/spec.md`
- `openspec/specs/ftl-scheduling-and-media-model/spec.md`
- `openspec/specs/simulator-tooling/spec.md`

推荐把本文件当作面向开发者的概览文档，把 `openspec/specs/` 当作规范性基线。
