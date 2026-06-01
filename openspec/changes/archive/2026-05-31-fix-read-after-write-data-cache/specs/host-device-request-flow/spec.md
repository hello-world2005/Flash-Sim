## MODIFIED Requirements

### Requirement: HIL 必须按请求类型切分事务并处理读缓存

`HIL` SHALL 将 `READ` / `WRITE` 请求按页与 sector bitmap 切分为用户事务；将 `SEARCH` / `COMPUTE` / `STATIC_WRITE` 按静态区域子平面粒度切分；对 `WRITE` 请求，HIL MUST 在完成 segmentation 后立即在控制器侧 `Data_Cache` 中为每个 `USER_WRITE` 事务注册一个携带 logical address 的事务级缓存条目；对 `READ` 请求，HIL MUST 先按 logical address 查询这些事务级缓存条目，只有未命中的事务才继续送入 `FTL`。

#### Scenario: 读请求命中与未命中缓存

- **WHEN** `HIL` 收到一个 `READ_REQ`
- **THEN** `HIL` MUST 先生成 `USER_READ` 事务列表并按 logical address 查询 `Data_Cache`，对命中的事务直接标记完成并从待翻译列表中移除，对未命中的事务继续交给 `FTL`

#### Scenario: 单个读请求部分事务命中 controller cache

- **WHEN** 一个 `READ_REQ` 的 `USER_READ` 事务中，只有一部分在 `Data_Cache` 中找到匹配的 logical address 条目
- **THEN** `HIL` MUST 立即完成命中的事务，只将未命中的事务继续交给 `FTL`，并且在这些未命中事务也完成之前不得向 Host 发送 `REQ_COMP`

#### Scenario: 写请求在 payload 返回前注册缓存条目

- **WHEN** `HIL` 收到一个 `WRITE_REQ`
- **THEN** `HIL` MUST 先完成 segmentation，并为每个 `USER_WRITE` 事务创建至少包含 `lpa`、bitmap 和 payload readiness 状态的 `Data_Cache` 条目，然后再向 `Host` 请求 `WRITE_DATA`

### Requirement: 写入类请求必须先向 Host 获取数据并写入控制器缓存

对 `WRITE`、`SEARCH`、`COMPUTE` 和 `STATIC_WRITE`，`HIL` SHALL 先向 `Host` 请求数据载荷；但 `Host` MUST 仅基于 `Request.size` 生成等长占位数据，不再依赖 `Request.data_address` 或 `Request.data_size`。对 `WRITE` 和 `STATIC_WRITE`，数据到达后 MUST 按事务粒度写回已经注册的控制器 cache 条目，并以“控制器已接收”为语义向 Host 返回请求完成；如果 `Data_Cache` 空间不足，`HIL` MUST 先把当前 cache 中的全部条目封装成后续写请求并发送给 `AMU`，再继续接收新的写入数据。

#### Scenario: Write 数据到达控制器

- **WHEN** `HIL` 收到 `WRITE_DATA` 或 `STATIC_WRITE_DATA`
- **THEN** `Host` MUST 返回长度为 `Request.size` 的占位数据列表，`HIL` MUST 把数据按事务粒度切片并填充到已注册的 cache 条目中，然后立即发送 `REQ_COMP` 给 Host，而不要求等待 NAND program 完成

#### Scenario: 写入到达时控制器 cache 空间不足

- **WHEN** `HIL` 正在处理一个新的 `WRITE_DATA`，且对应事务写入 `Data_Cache` 会超过 cache 容量
- **THEN** `HIL` MUST 先把 `Data_Cache` 中当前累积的全部条目发送给 `AMU` 进入后续写入路径，在 flush 完成后再继续缓冲当前请求的数据

### Requirement: 读搜索计算请求在事务完成后回传完成状态

对于 `READ`、`SEARCH` 和 `COMPUTE`，请求完成 SHALL 由“源请求关联事务全部完成”驱动；其中 `READ` 的事务既可以全部由 `Data_Cache` 直接满足，也可以部分由 `Data_Cache` 满足、其余部分由 `PHY` 服务完成后满足。只有当源请求关联的全部事务都被标记完成后，`HIL` 才能向 Host 发送 `REQ_COMP`。

#### Scenario: 读请求被 controller cache 直接满足

- **WHEN** 一个 `READ_REQ` 的全部 `USER_READ` 事务都在 `Data_Cache` 中找到匹配的 logical address 条目
- **THEN** `HIL` MUST 直接将该请求标记为完成，并向 Host 发送一个 `REQ_COMP` 消息，而不访问 `FTL` 或 `PHY`

#### Scenario: 最后一个未命中事务被服务完成

- **WHEN** `PHY` 通过 transaction serviced 回调通知 `HIL` 某个请求的最后一个未完成事务已结束
- **THEN** `HIL` MUST 将该请求标记为完成，并向 Host 发送一个 `REQ_COMP` 消息
