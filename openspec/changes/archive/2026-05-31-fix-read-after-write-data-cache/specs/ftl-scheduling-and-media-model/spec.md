## MODIFIED Requirements

### Requirement: TSU 以每 chip 队列为中心执行优先级调度

`TSU` SHALL 维护按 `channel -> chip -> transaction_type` 分组的事务队列，并在 channel 空闲时以 round-robin 方式遍历 chip，按事务优先级和 chip 类型尝试激活请求。对于普通数据 chip，默认优先级仍以当前读、写、擦除顺序为基础；但当 `Data_Cache` 因容量耗尽而触发 flush，且仍存在由该 flush 生成并等待落盘的 `USER_WRITE` 事务时，TSU MUST 进入 cache-pressure drain mode，在这些普通 chip 上优先尝试 `USER_WRITE`，直到这批累积写事务全部写入 flash array 后再恢复常规优先级。静态 chip 仍按 `COMPUTE`、`SEARCH` 和 `STATIC_WRITE` 路径独立调度。

#### Scenario: Channel 空闲触发常规调度

- **WHEN** 某个 channel 变为空闲、至少一个 chip 上存在待执行事务，且当前不存在待清空的 cache-pressure flush 写入 backlog
- **THEN** `TSU` MUST 按轮询顺序检查该 channel 下的 chip，并根据读、写、擦除或 static chip 的 search/compute/static write 优先级尝试下发事务

#### Scenario: cache 满触发的 flush backlog 优先写入 flash

- **WHEN** `Data_Cache` 的一次满容量 flush 已经把累积条目提交给 `AMU`，且这些 flush 生成的 `USER_WRITE` 事务仍未全部完成
- **THEN** `TSU` MUST 在普通数据 chip 上先尝试调度这些 `USER_WRITE` 事务，再考虑新的 `USER_READ`，并持续该优先级直到这批 flush backlog 全部写入 flash array
