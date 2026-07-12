# Host Device Request Flow Specification

## Purpose

Define the baseline request path between Host, PCIe, HIL, and the controller-side cache, including request queueing, delivery, segmentation, data fetch, and completion semantics.
## Requirements
### Requirement: Host 维护轻量级提交与完成队列元数据

`Host` SHALL 在进程内维护 `sq_heads`、`sq_tails`、`cq_heads`、`cq_tails`、`sq_entries` 和 `IO_Flow` 等轻量级数据结构，用于请求分配、并发流占用和完成状态跟踪，而不是模拟完整的 host-memory-backed NVMe 队列 DMA 机制。

#### Scenario: 请求进入 Host

- **WHEN** `Host` 收到一个 `REQ_INIT` 事件并接收新的 `Request`
- **THEN** `Host` MUST 选择一个未满的 SQ，将请求写入本地队列结构，更新对应队列尾指针，并在可用时把该 SQ 绑定到一个 `IO_Flow`

### Requirement: Host 在提交队列不可用时延后请求

当前模型 SHALL 使用 `waiting_req` 作为退避队列，当所有 SQ 都已满时，请求不会立即丢弃，而是等待后续流释放后再提交。

#### Scenario: 所有 SQ 均不可用

- **WHEN** `Host` 收到新请求但找不到可用的 SQ
- **THEN** `Host` MUST 将请求放入 `waiting_req`，并在后续移除已完成请求时尝试重新提交

### Requirement: PCIe 链路按方向串行化消息并注册未来投递事件

`PCIe_link` SHALL 分别维护 Host 到 Device 和 Device 到 Host 的消息队列；每个方向同一时刻仅推进队首消息，并根据当前消息的传输字节数把消息投递建模为未来 `DELIVER` 事件。每条消息的传输字节数 MUST 计算为 `用户发送的数据量 + PCIe 打包附加的固定数据量`。其中，携带用户数据负载的消息 MUST 从消息 payload 中推导用户数据字节数；不携带用户数据的请求、控制和完成消息 MUST 将用户数据字节数视为 `0`，仅保留固定 PCIe 打包开销。链路延时 MUST 由 `传输字节数 / PCIe 接口带宽` 计算，并向上取整到仿真器使用的时间粒度，从而保证控制消息和数据消息都具有确定的非零传输耗时。

#### Scenario: Host 向 Device 发送不带用户数据的控制消息

- **WHEN** `Host` 通过 `PCIe_link.send(...)` 向 `Device` 发送一条不携带 `payload["data"]` 的消息
- **THEN** 链路 MUST 将消息压入 `host_to_device_queue`，并在当前仿真时间之后按 `固定 PCIe 打包开销 / PCIe 接口带宽` 向上取整得到的延时注册一个 `DELIVER` 事件

#### Scenario: Host 向 Device 发送带数据负载的消息

- **WHEN** `Host` 通过 `PCIe_link.send(...)` 向 `Device` 发送一条携带 `payload["data"]` 的消息
- **THEN** 链路 MUST 从该 payload 推导用户数据字节数，将其与固定 PCIe 打包开销相加，并在当前仿真时间之后按计算出的消息专属延时注册一个 `DELIVER` 事件

#### Scenario: 更大的数据消息具有更长的链路延时

- **WHEN** 两条其他条件相同但用户数据量不同的 PCIe 数据消息分别进入同一方向的链路延时估算
- **THEN** 用户数据量更大的那条消息 MUST 得到更长的估算链路延时

#### Scenario: Device 向 Host 发送完成消息

- **WHEN** `Device` 通过 `PCIe_link.send(...)` 向 `Host` 发送 `REQ_COMP` 或其他不携带用户数据的完成消息
- **THEN** 链路 MUST 将消息压入 `device_to_host_queue`，并使用仅包含固定 PCIe 打包开销的延时模型注册未来 `DELIVER` 事件

### Requirement: HIL 必须按请求类型切分事务并处理读缓存

`HIL` SHALL 将 `READ` / `WRITE` 请求按页与 sector bitmap 切分为用户事务；将 `SEARCH` / `COMPUTE` / `STATIC_WRITE` 按静态区域子平面粒度切分；对 `WRITE` 请求，HIL MUST 在完成 segmentation 后立即在控制器侧 `Data_Cache` 中为每个 `USER_WRITE` 事务注册一个携带 logical address 的事务级缓存条目；对 `READ` 请求，HIL MUST 先按 logical address 查询这些事务级缓存条目，只有未命中的事务才继续送入 `FTL`。在请求进入数据获取、cache 查询或 `FTL` 之前，`HIL` MUST 校验请求访问的地址域是否合法：`SEARCH` / `COMPUTE` / `STATIC_WRITE` 必须完整落在 static 区域内，普通 `WRITE` 必须完整落在 random-access 区域内。任何违反这些规则的请求 MUST 立刻以 `ERROR` 完成，并附带报错信息，而不得继续触发 `Host` 数据获取、cache 写入或 `FTL` / `PHY` 调度。

#### Scenario: 读请求命中与未命中缓存

- **WHEN** `HIL` 收到一个 `READ_REQ`
- **THEN** `HIL` MUST 先生成 `USER_READ` 事务列表并按 logical address 查询 `Data_Cache`，对命中的事务直接标记完成并从待翻译列表中移除，对未命中的事务继续交给 `FTL`

#### Scenario: 单个读请求部分事务命中 controller cache

- **WHEN** 一个 `READ_REQ` 的 `USER_READ` 事务中，只有一部分在 `Data_Cache` 中找到匹配的 logical address 条目
- **THEN** `HIL` MUST 立即完成命中的事务，只将未命中的事务继续交给 `FTL`，并且在这些未命中事务也完成之前不得向 Host 发送 `REQ_COMP`

#### Scenario: 写请求在 payload 返回前注册缓存条目

- **WHEN** `HIL` 收到一个 `WRITE_REQ`
- **THEN** `HIL` MUST 先完成 segmentation，并为每个 `USER_WRITE` 事务创建至少包含 `lpa`、bitmap 和 payload readiness 状态的 `Data_Cache` 条目，然后再向 `Host` 请求 `WRITE_DATA`

#### Scenario: Search 请求访问 random-access 区域

- **WHEN** `HIL` 收到一个 `SEARCH_REQ`，且该请求的 `start_lha` / `size` 范围没有完整落在 static 区域内
- **THEN** `HIL` MUST 直接以 `ERROR` 完成该请求并附带报错信息，且 MUST NOT 向 `Host` 请求 `SEARCH_DATA`，也 MUST NOT 将该请求提交给 `FTL`

#### Scenario: Compute 请求访问 random-access 区域

- **WHEN** `HIL` 收到一个 `COMPUTE_REQ`，且该请求的 `start_lha` / `size` 范围没有完整落在 static 区域内
- **THEN** `HIL` MUST 直接以 `ERROR` 完成该请求并附带报错信息，且 MUST NOT 向 `Host` 请求 `COMPUTE_DATA`，也 MUST NOT 将该请求提交给 `FTL`

#### Scenario: 普通写请求访问 static 区域

- **WHEN** `HIL` 收到一个普通 `WRITE_REQ`，且该请求的任一访问地址落入 static 区域
- **THEN** `HIL` MUST 直接以 `ERROR` 完成该请求并附带报错信息，且 MUST NOT 在 `Data_Cache` 中注册写条目，也 MUST NOT 向 `Host` 请求 `WRITE_DATA`

#### Scenario: 合法 static_write 请求进入静态路径

- **WHEN** `HIL` 收到一个 `STATIC_WRITE_REQ`，且该请求的 `start_lha` / `size` 范围完整落在 static 区域内
- **THEN** `HIL` MUST 按静态区域子平面粒度切分事务，并继续后续的 `STATIC_WRITE_DATA` 获取与 static 写入流程

### Requirement: 写入类请求必须先向 Host 获取数据并写入控制器缓存

对通过地址域校验的 `WRITE`、`SEARCH`、`COMPUTE` 和 `STATIC_WRITE`，`HIL` SHALL 先向 `Host` 请求数据载荷；但 `Host` MUST 仅基于 `Request.size` 生成等长占位数据，不再依赖 `Request.data_address` 或 `Request.data_size`。对 `WRITE` 和 `STATIC_WRITE`，数据到达后 MUST 按事务粒度写回已经注册的控制器 cache 条目，并以“控制器已接收”为语义向 Host 返回 `REQ_COMP`，其中 `status` 必须为 `SUCCESS`，`error_message` 必须为空；如果 `Data_Cache` 空间不足，`HIL` MUST 先把当前 cache 中的全部条目封装成后续写请求并发送给 `AMU`，再继续接收新的写入数据。未通过地址域校验的请求 MUST NOT 触发任何数据获取消息。

#### Scenario: Write 数据到达控制器

- **WHEN** `HIL` 收到 `WRITE_DATA` 或 `STATIC_WRITE_DATA`
- **THEN** `Host` MUST 返回长度为 `Request.size` 的占位数据列表，`HIL` MUST 把数据按事务粒度切片并填充到已注册的 cache 条目中，然后立即发送一个 `REQ_COMP` 给 Host，且该消息的 `status` MUST 为 `SUCCESS`、`error_message` MUST 为空，而不要求等待 NAND program 完成

#### Scenario: 非法 compute 请求不会触发数据获取

- **WHEN** `HIL` 收到一个未通过 static 区域校验的 `COMPUTE_REQ`
- **THEN** `HIL` MUST 直接发送一个带 `ERROR` 和报错信息的 `REQ_COMP`，并且 MUST NOT 向 `Host` 发送 `COMPUTE_DATA_REQ`

#### Scenario: 写入到达时控制器 cache 空间不足

- **WHEN** `HIL` 正在处理一个新的 `WRITE_DATA`，且对应事务写入 `Data_Cache` 会超过 cache 容量
- **THEN** `HIL` MUST 先把 `Data_Cache` 中当前累积的全部条目发送给 `AMU` 进入后续写入路径，在 flush 完成后再继续缓冲当前请求的数据

### Requirement: 读搜索计算请求在事务完成后回传完成状态

对于 `READ`、`SEARCH` 和 `COMPUTE`，请求完成 SHALL 由源请求的终态驱动：当关联事务全部成功完成时，`HIL` MUST 发送一个 `REQ_COMP`，其中 `status` 为 `SUCCESS` 且 `error_message` 为空；当地址翻译或介质访问发现该请求访问了不存在的映射、无效 PPA、free page、invalid page 或 invalid sector 时，`HIL` MUST 发送且只发送一个 `REQ_COMP`，其中 `status` 为 `ERROR`，`error_message` 为对应报错信息。对同一个请求，`HIL` MUST 在发送终态 `REQ_COMP` 的同时把相同的状态与报错信息写入日志，并且在 `ERROR` 之后不得再发送 `SUCCESS` 完成。

#### Scenario: 读请求被 controller cache 直接满足

- **WHEN** 一个 `READ_REQ` 的全部 `USER_READ` 事务都在 `Data_Cache` 中找到匹配的 logical address 条目
- **THEN** `HIL` MUST 直接将该请求标记为成功完成，并向 Host 发送一个 `REQ_COMP` 消息，且该消息的 `status` MUST 为 `SUCCESS`、`error_message` MUST 为空，而不访问 `FTL` 或 `PHY`

#### Scenario: 最后一个未命中事务被服务完成

- **WHEN** `PHY` 通过 transaction serviced 回调通知 `HIL` 某个请求的最后一个未完成事务已成功结束
- **THEN** `HIL` MUST 将该请求标记为成功完成，并向 Host 发送一个 `REQ_COMP` 消息，且该消息的 `status` MUST 为 `SUCCESS`、`error_message` MUST 为空

#### Scenario: 读请求访问不存在的映射

- **WHEN** `AMU` 在处理一个 `READ_REQ` 时发现目标 LPA 没有对应 mapping page、mapping slot 无效或返回 `INVALID_PPA`
- **THEN** `HIL` MUST 将该请求标记为 `ERROR`，通过且仅通过一个 `REQ_COMP` 把报错信息返回给 Host，并在日志中输出相同的错误信息

#### Scenario: 读请求访问 free page 或 invalid sector

- **WHEN** `PHY` 在执行一个与 Host 请求关联的读事务时发现访问到了 free page、invalid user page 或 invalid sector
- **THEN** `HIL` MUST 将源请求标记为 `ERROR`，通过且仅通过一个 `REQ_COMP` 把报错信息返回给 Host，并在日志中输出相同的错误信息

### Requirement: Request flow preserves phase timestamps needed for latency reporting

`Host`、`PCIe_link`、`HIL`、`AMU`、`TSU` 和 `PHY` MUST 暴露请求级统计所需的阶段边界信息。至少包括：`REQ_INIT` 被执行的时间、请求进入或重入 SQ 的时间、首次经 PCIe 发送的时间、每条 PCIe 消息的发送方向/消息类型/字节数/到达时间、mapping wait 的开始与结束时间、事务进入 TSU 队列的时间、事务首次被发往 `PHY` 的时间，以及 `REQ_COMP` 回到 Host 的时间。

#### Scenario: Request waits in Host SQ before being sent

- **WHEN** 一条请求在 `Host` 中已经进入 SQ，但因为对应 `IO_Flow` 被占用而未能立即发出
- **THEN** 系统 MUST 保留足以计算该请求 `host_sq_wait` 与 `host_dispatch` 延时的阶段时间戳

#### Scenario: PCIe message carries data payload

- **WHEN** `PCIe_link` 发送一条携带 `payload["data"]` 的消息
- **THEN** 系统 MUST 保留该消息的方向、消息类型、估算字节数、发送时间和投递完成时间，以便请求级报告计算对应的 PCIe 阶段延时

### Requirement: Mapping-miss reads expose AMU waiting intervals to the reporting subsystem

当用户请求因为地址映射缺失而生成 `MAPPING_READ` 时，`AMU` MUST 为原始请求保留“等待映射返回”的区间信息；该区间从依赖 mapping 的事务被挂起开始，到映射结果返回并使事务可以重新提交或失败返回为止。

#### Scenario: Mapping read resolves a waiting user read

- **WHEN** 一条 `READ` 请求中的事务因缺少映射而等待 `MAPPING_READ` 返回，且映射随后成功返回
- **THEN** 系统 MUST 为该原始请求记录一个非零的 `amu_mapping_wait` 区间，并在事务重新进入 TSU 之前结束该区间

#### Scenario: Mapping read fails

- **WHEN** 一条等待映射的用户请求因为 `MAPPING_READ` 失败而被错误完成
- **THEN** 系统 MUST 仍然闭合该请求的 mapping-wait 区间，并使报告能够据此计算失败请求的 `amu_mapping_wait`

### Requirement: Buffered write cache preserves origin request lineage through flush

对于先写入控制器 cache 的 `WRITE` / `STATIC_WRITE` 请求，`Data_Cache` 与 `Cache_Manager` MUST 保留足够细粒度的来源请求 lineage，使 flush 生成的后台事务能够关联回原始输入请求。若多个请求共同贡献同一 flush 事务，系统 MUST 允许该后台事务同时关联到多个来源请求；若某个 cache 单元被后续写覆盖，lineage MUST 更新为最新来源。

#### Scenario: Flush transaction contains contributions from multiple requests

- **WHEN** 一个后台 flush 事务包含来自多个输入写请求的数据贡献
- **THEN** 系统 MUST 让该后台事务可被同时归因到这些来源请求，以便报告回填对应的 TSU / PHY 后台时延

#### Scenario: Later write overwrites cached data before flush

- **WHEN** 一个 cache entry 中的某个 sector 或静态写单元在 flush 之前被后续请求覆盖
- **THEN** 系统 MUST 更新该单元的 lineage 为最新请求，而不是把后台持久化时延继续归因给已被覆盖的旧请求

### Requirement: PCIe request, payload, and completion transfers use TLP accounting

The event-driven host link SHALL model NVMe submission, data payload, and completion traffic with 4 B/ns link bandwidth, 128 B maximum payload, and 28 B TLP overhead. Read payload transfer MUST be a real queued device-to-host PCIe event completed before CQ delivery. Write payload transfer MUST occur exactly once before NAND programming.

#### Scenario: 4 KiB read returns payload before completion

- **WHEN** a 4 KiB read finishes its NAND data-out phase
- **THEN** the link MUST transfer 32 payload TLPs (4,992 wire bytes, 1,248 ns) before the 11 ns CQ transfer completes the host request

#### Scenario: Internal controller notification has no extra wire transfer

- **WHEN** HIL and Host exchange an internal queue/data-ready notification that has no MQSim PCIe counterpart
- **THEN** that notification MUST add zero PCIe wire time

