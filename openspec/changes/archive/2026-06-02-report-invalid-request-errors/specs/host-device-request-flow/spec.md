## MODIFIED Requirements

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
