## ADDED Requirements

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

`PCIe_link` SHALL 分别维护 Host 到 Device 和 Device 到 Host 的消息队列；每个方向同一时刻仅推进队首消息，并用固定链路延迟把消息投递建模为未来 `DELIVER` 事件。

#### Scenario: Host 向 Device 发送消息

- **WHEN** `Host` 通过 `PCIe_link.send(...)` 发送一条面向 `Device` 的消息
- **THEN** 链路 MUST 将消息压入 `host_to_device_queue`，并在当前仿真时间之后固定延迟的位置注册一个 `DELIVER` 事件

### Requirement: HIL 必须按请求类型切分事务并处理读缓存

`HIL` SHALL 将 `READ` / `WRITE` 请求按页与 sector bitmap 切分为用户事务；将 `SEARCH` / `COMPUTE` / `STATIC_WRITE` 按静态区域子平面粒度切分；对 `READ` 请求，HIL MUST 先查询控制器侧 `Data_Cache`，只有未命中的事务才继续送入 FTL。

#### Scenario: 读请求命中与未命中缓存

- **WHEN** `HIL` 收到一个 `READ_REQ`
- **THEN** `HIL` MUST 先生成 `USER_READ` 事务列表并查询 `Data_Cache`，对命中的事务直接填充 payload 并标记完成，对未命中的事务继续交给 `FTL`

### Requirement: 写入类请求必须先向 Host 获取数据并写入控制器缓存

对 `WRITE`、`SEARCH`、`COMPUTE` 和 `STATIC_WRITE`，`HIL` SHALL 先向 `Host` 请求数据载荷；对 `WRITE` 和 `STATIC_WRITE`，数据到达后 MUST 被切片写入控制器 cache，并以“控制器已接收”为语义向 Host 返回请求完成。

#### Scenario: Write 数据到达控制器

- **WHEN** `HIL` 收到 `WRITE_DATA` 或 `STATIC_WRITE_DATA`
- **THEN** `HIL` MUST 把数据按事务粒度切片、写入 cache，并立即发送 `REQ_COMP` 给 Host，而不要求等待 NAND program 完成

### Requirement: 读搜索计算请求在事务完成后回传完成状态

对于 `READ`、`SEARCH` 和 `COMPUTE`，请求完成 SHALL 由事务服务完成驱动；只有当源请求关联的全部事务都被标记完成后，`HIL` 才能向 Host 发送 `REQ_COMP`。

#### Scenario: 最后一个事务被服务完成

- **WHEN** `PHY` 通过 transaction serviced 回调通知 `HIL` 某个请求的最后一个未完成事务已结束
- **THEN** `HIL` MUST 将该请求标记为完成，并向 Host 发送一个 `REQ_COMP` 消息
