# FTL Scheduling And Media Model Specification

## Purpose

Define the baseline behavior of the FTL, mapping subsystem, transaction scheduler, block manager, garbage collection flow, and PHY media execution model used by the current simulator.
## Requirements
### Requirement: AMU 使用 CMT、GMT 和 GTD 维护地址映射

`Address_Mapping_Unit` SHALL 维护 CMT、GMT 和 GTD 三层映射状态，并在翻译 `USER_READ` / `USER_WRITE` 时根据映射缓存命中情况生成对应的 mapping read 或 mapping write 事务。对于与 Host 请求关联的 `READ`，当 `AMU` 发现目标 LPA 对应的 mapping page 不存在、mapping page 中目标槽位无效，或者 mapping 响应中的 PPA 为 `INVALID_PPA` 时，`AMU` MUST 将该情况转化为请求级错误并连同报错信息回传给上层，而不得以未捕获异常终止模拟。

#### Scenario: 地址映射缺失时读取 mapping page

- **WHEN** 一个用户事务需要的 LPA 映射不在 CMT 中且必须访问 mapping page
- **THEN** `Address_Mapping_Unit` MUST 生成 `MAPPING_READ` 事务，等待其返回后再继续补全目标事务的物理地址

#### Scenario: Host read 访问不存在的 mapping page

- **WHEN** `AMU` 在翻译一个与 Host 请求关联的 `USER_READ` 时发现目标 `mvpn` 不在 `GTD` 中
- **THEN** `AMU` MUST 将该读请求标记为请求级错误并附带报错信息，且 MUST NOT 为该 LPA 提交 `MAPPING_READ` 或 `USER_READ` 到 `TSU`

#### Scenario: Host read 访问 mapping 中无效槽位或无效 PPA

- **WHEN** `AMU` 在翻译或处理 `MAPPING_READ` 响应时发现目标 LPA 的 mapping 槽位无效，或者返回的 PPA 为 `INVALID_PPA`
- **THEN** `AMU` MUST 将相关 Host 读请求标记为请求级错误并附带报错信息，清理该 LPA 的等待读事务，而不得继续提交依赖该映射的 `USER_READ`

### Requirement: 用户页更新采用异地更新

当前 FTL 模型 SHALL 对用户数据和 mapping page 都采用 out-of-place update 语义，即新的内容写入新的物理页，再由映射信息指向新的位置，并在适当时机失效旧页。

#### Scenario: 用户写入覆盖已有 LPA

- **WHEN** 一个 `USER_WRITE` 针对已经存在映射的 LPA 产生新版本数据
- **THEN** 系统 MUST 为该 LPA 分配新的物理页地址并更新映射，同时把旧物理页标记为后续可失效对象

### Requirement: 静态区域请求必须映射到专用 static chip 路径

`SEARCH`、`COMPUTE` 和 `STATIC_WRITE` SHALL 使用 `FTL.get_static_address(...)` 计算静态区域地址，并由 TSU 仅在被标记为 static chip 的 chip 上调度这些事务。

#### Scenario: Search 请求进入 FTL 路径

- **WHEN** `HIL` 为 `SEARCH`、`COMPUTE` 或 `STATIC_WRITE` 切分事务
- **THEN** 每个事务 MUST 带有静态区域物理地址，并在后续调度中只进入 static chip 的事务队列

### Requirement: Block Manager 支持数据驱动的预处理初始化

`Block_Manager` SHALL 支持在仿真开始前读取预处理数据，向 PHY 存储中写入初始用户页和 mapping page 内容，初始化 GTD，并预热部分 CMT 条目。

#### Scenario: 引擎启动前执行预处理

- **WHEN** `Engine.Start_simulation(...)` 调用 `block_manager.preconditioning(...)`
- **THEN** `Block_Manager` MUST 根据预处理数据建立初始 valid/invalid page 分布、写入初始物理页内容、写入 mapping page，并填充对应的映射元数据

### Requirement: TSU 以每 chip 队列为中心执行优先级调度

`TSU` SHALL 维护按 `channel -> chip -> transaction_type` 分组的事务队列，并在 channel 空闲时以 round-robin 方式遍历 chip，按事务优先级和 chip 类型尝试激活请求。对于普通数据 chip，默认优先级仍以当前读、写、擦除顺序为基础；但当 `Data_Cache` 因容量耗尽而触发 flush，且仍存在由该 flush 生成并等待落盘的 `USER_WRITE` 事务时，TSU MUST 进入 cache-pressure drain mode，在这些普通 chip 上优先尝试 `USER_WRITE`，直到这批累积写事务全部写入 flash array 后再恢复常规优先级。静态 chip 仍按 `COMPUTE`、`SEARCH` 和 `STATIC_WRITE` 路径独立调度。

#### Scenario: Channel 空闲触发常规调度

- **WHEN** 某个 channel 变为空闲、至少一个 chip 上存在待执行事务，且当前不存在待清空的 cache-pressure flush 写入 backlog
- **THEN** `TSU` MUST 按轮询顺序检查该 channel 下的 chip，并根据读、写、擦除或 static chip 的 search/compute/static write 优先级尝试下发事务

#### Scenario: cache 满触发的 flush backlog 优先写入 flash

- **WHEN** `Data_Cache` 的一次满容量 flush 已经把累积条目提交给 `AMU`，且这些 flush 生成的 `USER_WRITE` 事务仍未全部完成
- **THEN** `TSU` MUST 在普通数据 chip 上先尝试调度这些 `USER_WRITE` 事务，再考虑新的 `USER_READ`，并持续该优先级直到这批 flush backlog 全部写入 flash array

### Requirement: TSU 调度必须满足 die 和 plane 约束并尊重 barrier

对同一 chip 的普通读写擦事务，`TSU` SHALL 以 die 为选择粒度，并在每个 die 内满足 plane 复用和页号一致性约束；同时 TSU MUST 阻止被 LPA/MVPN barrier 或 GC 擦除屏障拦住的事务被下发。

#### Scenario: 事务被 barrier 阻塞

- **WHEN** 某个待调度事务依赖尚未完成的同 LPA、同 MVPN 或 GC 擦除屏障
- **THEN** `TSU` MUST 跳过该事务，直到依赖解除后才允许它被发往 `PHY`

### Requirement: TSU 在条件满足时支持对进行中的写擦操作做挂起

对读请求和部分写请求，当前模型 SHALL 在 chip 正处于写入或擦除阶段且剩余时间高于阈值时允许 suspend，并在高优先级事务结束后恢复原命令。

#### Scenario: Read 抢占长延迟写擦操作

- **WHEN** 某个 chip 正在执行可挂起的 `WRITE` 或 `ERASE`，且剩余时间大于配置阈值，同时队列中出现可调度的读事务
- **THEN** `TSU` MUST 允许通过 `suspension_required` 路径把新读事务发往 `PHY`，并在后续恢复被挂起命令

### Requirement: Block Manager 和 GC 必须维护块状态并在阈值下触发回收

`Block_Manager` SHALL 维护每个 plane / block 的 free、valid、invalid 页面统计与写前沿；`GC_WL_Manager` MUST 在 free block pool 低于阈值时选择 victim block，迁移 valid page，并最终擦除该 block。

#### Scenario: Free block 数量低于阈值

- **WHEN** 某个 plane 的 `free_block_pool` 数量小于或等于 GC 阈值
- **THEN** `GC_WL_Manager` MUST 选择一个 victim block，提交 `GC_READ`、`GC_WRITE` 和 `GC_ERASE` 事务链，并在擦除完成后把该 block 重新放回 free pool

### Requirement: PHY 以事件形式模拟命令传输、阵列执行和数据回传

`PHY` SHALL 把 `TSU` 下发的事务转换为命令传输事件、芯片内部执行完成事件以及必要的数据回传事件；在事务完成时，PHY MUST 更新页存储内容或读取响应数据，并通过回调广播给 `HIL`、`AMU`、`Block Manager` 和 `TSU`。对于与 Host 请求关联的 `USER_READ` 或 `MAPPING_READ`，当 `PHY` 发现访问到了 invalid mapping page、invalid mapping slot、free user page、invalid user page 或 invalid sector 时，PHY MUST 将该事务作为“失败的已完成事务”连同报错信息广播给回调方，而不得让未捕获异常中断事件循环。

#### Scenario: 普通读事务经过 PHY

- **WHEN** `TSU` 向 `PHY` 下发一个读事务批次
- **THEN** `PHY` MUST 先注册命令传输事件，再注册芯片内部读完成事件，最后在数据回传后为每个事务填充响应并广播 transaction serviced 信号

#### Scenario: 用户读访问 free page 或 invalid sector

- **WHEN** `PHY` 在执行一个与 Host 请求关联的 `USER_READ` 时发现目标页为空闲页、无效用户页，或者所请求 sector 为 invalid
- **THEN** `PHY` MUST 为该事务附带失败状态和报错信息，并继续通过 transaction serviced 回调把该失败广播给 `HIL`、`AMU` 和 `TSU`，而不得抛出未捕获异常终止仿真

#### Scenario: mapping 读访问无效 mapping 页或无效槽位

- **WHEN** `PHY` 在执行一个与 Host 请求关联的 `MAPPING_READ` 时发现目标 mapping page 非法，或者所请求的 mapping 槽位无效
- **THEN** `PHY` MUST 为该事务附带失败状态和报错信息，并继续通过 transaction serviced 回调把该失败广播给 `AMU` 和 `HIL`，以便上层把源请求完成为 `ERROR`

