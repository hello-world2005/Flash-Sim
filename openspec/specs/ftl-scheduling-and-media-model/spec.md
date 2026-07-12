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

### Requirement: 用户页采用异地更新，metadata 页采用固定位置更新

当前 FTL 模型 SHALL 对用户数据保持 out-of-place update 语义，即新的 `USER_WRITE` 写入新的物理页，再由映射信息指向新的位置，并在适当时机失效旧页。当前 mapping metadata 页 SHALL 保持 fixed-position 语义：每个 `mvpn` 通过固定规则映射到唯一 metadata 物理页地址，而不是为 metadata 页引入 out-of-place 位置漂移。对于 `USER_WRITE`，当前模型 SHALL 采用 submit-time PPA allocation：`AMU.translate_and_submit(WRITE)` 在事务提交到 `TSU` 前完成目标 plane 计算、PPA 分配、CMT/GMT 更新和 barrier 建立，而不是延迟到 TSU dispatch 阶段才绑定最终 PPA。

#### Scenario: 用户写入覆盖已有 LPA

- **WHEN** 一个 `USER_WRITE` 针对已经存在映射的 LPA 产生新版本数据
- **THEN** 系统 MUST 为该 LPA 分配新的物理页地址并更新映射，同时把旧物理页标记为后续可失效对象

#### Scenario: CMT miss 但 GMT hit 的覆写仍保留旧 PPA

- **WHEN** 一个 `USER_WRITE` 覆写的 LPA 已经从 CMT 逐出、但旧映射仍存在于 GMT
- **THEN** `AMU` MUST 将该写识别为覆写，分配新的 PPA，将旧 GMT 地址写入事务的 `invalidate_target`，并把新映射写入 dirty CMT entry

#### Scenario: CMT miss 且 GMT miss 的覆写回落到固定 metadata 页

- **WHEN** 一个 `USER_WRITE` 的目标 LPA 不在 CMT、也不在 GMT，但其 `mvpn` 的 fixed-position metadata 页中该槽位仍保存着有效旧 PPA
- **THEN** `AMU` MUST 将该写识别为覆写，使用 fixed metadata 页中的旧地址填充 `invalidate_target`，并 MUST NOT 直接把该写当成 first-time write

#### Scenario: 写事务在提交阶段完成 PPA 绑定

- **WHEN** `AMU.translate_and_submit(WRITE)` 接收一个可立即服务的 `USER_WRITE`
- **THEN** 该事务在进入 `TSU` 前 MUST 已经拥有最终 PPA，且 `Block_Manager` MUST 已经为该事务建立必要的 LPA barrier

#### Scenario: Metadata 页按固定位置执行稀疏写回

- **WHEN** `CMT` 驱逐 dirty 条目并触发 `generate_mapping_write_transaction(...)`
- **THEN** `AMU` MUST 把对应 `mvpn` 写回到 `get_plane_address_for_mvpn(mvpn)` 返回的固定 metadata 物理页，并保持稀疏 `bitmap + payload` 写回模型

#### Scenario: 新建 mvpn 的固定 metadata 地址可先保留、后 materialize

- **WHEN** 一个此前不存在的 `mvpn` 第一次触发 `MAPPING_WRITE`
- **THEN** `GTD` MAY 先登记该 `mvpn` 的固定 metadata 地址，再在对应 `MAPPING_WRITE` 完成后把该 metadata 页视为已 materialize；在 materialize 之前，其他写路径对该 fixed metadata 页的 fallback MUST 将其视为 unmapped，而不得误报 metadata corruption

#### Scenario: 稀疏 metadata 写回只补全旧页中真实存在的槽位

- **WHEN** 一个已有 `mvpn` 的 `MAPPING_WRITE` 需要保留旧页中这次未覆盖的部分槽位
- **THEN** 控制器内部发起的 merge `MAPPING_READ` MUST 只请求 `old_valid_bitmap & ~new_delta_bitmap` 对应的槽位，而不得把“这次没写的所有槽位”都作为读取目标

#### Scenario: 同一 mvpn 的后续 flush 必须串接最新 in-flight metadata 状态

- **WHEN** 同一个 `mvpn` 的下一次 `MAPPING_WRITE` 生成时，前一次该 `mvpn` 的 metadata 写回仍在队列中或尚未完成
- **THEN** 新的写回 MUST 依赖并继承前一次 `MAPPING_WRITE` 的最终页状态，而不得重新从当前 on-flash fixed page 读取旧镜像并覆盖掉前一次尚未落盘的槽位

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

对同一 chip 的普通读写擦事务，`TSU` SHALL 以 die 为选择粒度，并在每个 die 内满足 plane 复用和页号一致性约束；同时 `TSU` MUST 阻止被 LPA/MVPN barrier、GC 擦除屏障，或 static wear-leveling 迁移屏障拦住的事务被下发。

#### Scenario: 事务被 GC 或 static WL barrier 阻塞

- **WHEN** 某个待调度事务依赖尚未完成的同 LPA、同 MVPN，或命中了当前正被 GC / static wear leveling 保护的块与页面
- **THEN** `TSU` MUST 跳过该事务，直到对应 barrier 被释放后才允许把事务发送到 `PHY`

### Requirement: TSU 在条件满足时支持对进行中的写擦操作做挂起

对读请求和部分写请求，当前模型 SHALL 在 chip 正处于写入或擦除阶段且剩余时间高于阈值时允许 suspend，并在高优先级事务结束后恢复原命令。

#### Scenario: Read 抢占长延迟写擦操作

- **WHEN** 某个 chip 正在执行可挂起的 `WRITE` 或 `ERASE`，且剩余时间大于配置阈值，同时队列中出现可调度的读事务
- **THEN** `TSU` MUST 允许通过 `suspension_required` 路径把新读事务发往 `PHY`，并在后续恢复被挂起命令

### Requirement: Block Manager 和 GC 必须维护块状态并在阈值下触发回收

`Block_Manager` SHALL 维护每个 plane / block 的 `free`、`valid`、`invalid` 页面统计、写前沿与擦写次数；`GC_WL_Unit` MUST 在 free block pool 达到或低于阈值时选择 GC victim block、提交 `GC_READ`、`GC_WRITE` 和 `GC_ERASE` 事务链，并在擦除完成后把该 block 按当前擦写次数放回可分配 free pool。

#### Scenario: Free block 数量低于 GC 阈值

- **WHEN** 某个 plane 的 `free_block_pool` 数量小于或等于 GC 阈值
- **THEN** `GC_WL_Unit` MUST 选择一个安全的 victim block，提交完整的 `GC_READ`、`GC_WRITE` 和 `GC_ERASE` 事务链，并在擦除完成后更新 block bookkeeping

#### Scenario: GC 擦除完成后块回收到 WL-aware free pool

- **WHEN** 一个 `GC_ERASE` 或 static-WL 擦除事务完成
- **THEN** `Block_Manager` MUST 将被擦除 block 以最新 `wl_level` 重新加入可分配 free pool；GC erase MAY 在 host waiting writes 获得重试机会后评估一次 static wear leveling，static-WL erase MUST NOT 立即递归触发下一轮 static WL

### Requirement: 写分配背压使用 per-plane waiting queue

当 `AMU.translate_and_submit(WRITE)` 准备为 first-time write 分配 PPA，而目标 plane 的 free block pool 已小于或等于 `stop_servicing_writes_threshold` 时，系统 SHALL 将该事务放入以 `(channel, chip, die, plane)` 标识的物理 plane waiting queue，并暂不分配 PPA、暂不更新 CMT/GMT、暂不提交到 `TSU`。覆写事务 MAY 越过普通 first-time write 背压检查，以便产生 invalid page 支撑后续 GC；若分配时确实无可用 block，覆写也 MUST 进入 waiting queue。GC erase 完成并回收 block 后，`Block_Manager` MUST 按严格 FIFO 重试该物理 plane 的 waiting writes；队首无法提交时 MUST 保留整个队列后缀，不得让后续 overwrite 越过。

#### Scenario: Dynamic CWDP 写分配不把 hot LPA 固定到一个 plane

- **WHEN** runtime config sets `write_allocation_mode="dynamic-cwdp"` and `plane_allocation="CWDP"`
- **THEN** `AMU.translate_and_submit(WRITE)` MUST choose new user-program PPAs from a device-wide CWDP plane cursor, skipping planes that are under active maintenance or write backpressure when another data plane can accept the write
- **AND** logical-to-physical mapping MUST still record the selected PPA so later reads and overwrites resolve through CMT/GMT/metadata rather than recomputing the LPA's original plane

#### Scenario: First-time write 因 free pool 阈值进入等待队列

- **WHEN** 一个 first-time `USER_WRITE` 到达，且目标 plane 的 free block pool 数量小于或等于停写阈值
- **THEN** 该事务 MUST 被加入对应 plane 的 waiting queue，且 MUST NOT 获得最终 PPA、更新映射或进入 TSU 队列

#### Scenario: GC erase 唤醒 waiting writes

- **WHEN** 一个 `GC_ERASE` 完成并把 block 归还到某个 plane 的 free block pool
- **THEN** `Block_Manager` MUST 仅按 FIFO 重试该 `(channel, chip, die, plane)` waiting queue 中的写事务；其他 die 或 chip 上局部 plane 编号相同的队列 MUST NOT 被唤醒；每个成功重试的事务 MUST 在 submit-time 分配 PPA、更新 CMT/GMT、建立 barrier 并提交到 `TSU`

#### Scenario: 物理 plane 队列隔离不禁用多 die 批次并行

- **WHEN** 同一 chip 上不同 die 的已就绪事务同时进入 `TSU`，即使它们的局部 plane 编号相同
- **THEN** waiting queue 的物理 plane 隔离 MUST NOT 阻止 `TSU` 在同一调度批次中将这些事务分别下发到对应 die

#### Scenario: waiting retry 复用 submit-time 的 overwrite 判定

- **WHEN** 一个 waiting queue 中的 `USER_WRITE` 被 GC 唤醒后重试，且其旧映射只存在于 fixed-position metadata 页
- **THEN** retry 路径 MUST 复用 submit-time 的 overwrite 解析规则，将其视为覆写并绕过 first-write free-pool 阈值检查；只有真正 unmapped 的写才允许继续按 first-time write 背压处理

#### Scenario: Cache flush 生成的后台写保持可重试映射上下文

- **WHEN** `Data_Cache.write_flush()` 生成的后台 `USER_WRITE` 因背压进入 waiting queue，且该事务没有 `source_req`
- **THEN** 系统 MUST 保留足以在 retry 时选择 AMU domain 的映射上下文，并在 retry 成功后正确更新 CMT/GMT 与 `invalidate_target`

#### Scenario: Backpressured cache flush 不得重复生成事务

- **WHEN** 一个 cache entry 已存在 waiting 或 in-flight flush transaction，且后续再次调用 `write_flush()`
- **THEN** 系统 MUST NOT 为同一 entry generation 再生成 flush transaction；旧 generation 提交后，只有未被更新的 entry 才可删除，期间到达的新 generation MUST 保留并在后续单独 flush

#### Scenario: 保留 block 无法唤醒 first write 时继续 GC

- **WHEN** GC erase 归还一个 block，但 FIFO 队首是 first-time write，且 free pool 仍处于停写保留阈值内而无法提交
- **THEN** 系统 MUST 保持队列不变并为该 plane 继续触发 GC，不得在没有后续 USER_WRITE completion 的情况下留下永久 waiting writes

### Requirement: Dynamic wear leveling chooses the lowest-erase eligible free block

当 `Block_Manager` 需要为用户写、mapping 写或 GC 迁移写切换到新的 free block 时，`GC_WL_Unit` SHALL 基于块的擦写次数选择当前 plane 内擦写次数最低且满足安全条件的 free block，而不是仅按 block 编号或隐式 frontier 顺序消费 free pool。

#### Scenario: 写前沿切换时优先选择最低擦写次数 free block

- **WHEN** 同一 plane 的 free pool 中存在多个可用 free block，且它们的 `wl_level` 不同
- **THEN** 新的 write frontier 或 GC destination block MUST 选择 `wl_level` 最低的那个合格 free block

### Requirement: Static wear leveling migrates cold data after erase completion

在 GC 擦除完成后，`GC_WL_Unit` SHALL 按后台维护策略评估当前 plane 的 wear skew。系统 MUST 仅在该 plane 没有 waiting writes、free block pool 高于 GC low watermark，且实际安全 source/destination 候选的磨损差达到或超过策略阈值时，迁移冷块中的有效页到更高磨损的可用块，并对原块提交擦除。Static-WL 自身的 erase 完成 MUST NOT 同步递归触发下一轮 static wear-leveling。

#### Scenario: 擦除完成后触发 static wear leveling

- **WHEN** 某个 plane 在 GC 擦除完成后没有 waiting writes、容量高于 GC low watermark，且安全 source/destination 候选的磨损差达到或超过 static-WL 阈值
- **THEN** `GC_WL_Unit` MUST 提交一条与 GC 相同结构的迁移事务链，把冷块中的有效页搬迁出去并擦除原块

#### Scenario: Static WL 让位于 host write 和 GC 容量恢复

- **WHEN** 某个 plane 仍有 waiting writes，或者 free block pool 尚未高于 GC low watermark
- **THEN** `GC_WL_Unit` MUST NOT 启动 static WL，GC erase 归还的容量 MUST 先用于 FIFO retry 和必要的后续 GC

#### Scenario: Static WL 使用实际候选磨损差

- **WHEN** 全局 wear skew 达到阈值，但实际安全 destination 与 source 的 `wl_level` 差值低于阈值
- **THEN** `GC_WL_Unit` MUST 跳过该次 static WL，不得仅凭全局最大值与最小值提交无效迁移

#### Scenario: Static WL 通过真实媒体路径完成

- **WHEN** 一个满足条件的低磨损冷数据块被选为 static-WL source
- **THEN** 系统 MUST 通过 `TSU` 和 `PHY` 完成 `GC_READ -> GC_WRITE -> GC_ERASE`，将 LPA 映射更新到 destination，清除 source/destination barrier，并分别记录一次 static-WL start、对应 relocated page、physical GC write 和 completed erase

#### Scenario: Static-WL erase 不同步递归

- **WHEN** static-WL 事务链中的 `GC_ERASE` 完成
- **THEN** 系统 MUST 结束当前 maintenance 流程并释放 barrier，且 MUST NOT 在同一完成回调中立即启动下一条 static-WL 迁移链

### Requirement: GC and static-WL candidates must exclude unsafe blocks

`GC_WL_Unit` MUST 仅从安全块集合中选择 GC victim、static-WL source 和迁移目标块。安全块集合不得包含当前写前沿块、已处于 GC/WL 擦除保护中的块，或仍带有进行中用户 program 归属的块。

#### Scenario: 非安全块不会被选为 GC 或 static-WL 候选

- **WHEN** 一个块仍是活跃写前沿、已被 GC/WL barrier 保护，或仍有进行中的用户写归属
- **THEN** `GC_WL_Unit` MUST 不把该块选作 GC victim、static-WL source，或任何迁移目标块

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

#### Scenario: 内部 metadata merge read 触发 invariant failure

- **WHEN** `PHY` 在执行一个 controller 内部的 `MAPPING_READ`（无 `source_req`）时，发现请求触及了未写入的 metadata 页、invalid mapping slot，或 `INVALID_PPA`
- **THEN** `PHY` MUST 立即将其视为 controller invariant failure 并显式失败，而不得返回空 metadata 页让后续 merge 静默继续

### Requirement: Preconditioning callers provide AMU-backed mapping context
`Block_Manager.preconditioning(...)` SHALL execute with a valid `Address_Mapping_Unit` and `PHY` so it can materialize user-page placement, mapping pages, GTD entries, and any warmed CMT state consistently with the runtime mapping model. Full-engine startup MAY satisfy this contract through injected dependencies, but direct callers and unit-test fixtures MUST provide equivalent mapping context explicitly.

#### Scenario: Engine startup passes preconditioning mapping dependencies
- **WHEN** `Engine.Start_simulation(...)` invokes `block_manager.preconditioning(...)`
- **THEN** the call MUST provide the active runtime `PHY` and `Address_Mapping_Unit` instances needed to build mapping state

#### Scenario: Direct test fixture supplies explicit AMU context
- **WHEN** a unit test or standalone harness invokes `Block_Manager.preconditioning(...)` outside the full engine topology
- **THEN** the fixture MUST provide an explicit `Address_Mapping_Unit` or equivalent injected mapping context so preconditioning can complete successfully

#### Scenario: Missing AMU context fails fast with a clear error
- **WHEN** `Block_Manager.preconditioning(...)` is called without an explicit `AMU` and without an injected runtime `AMU` available from its owning topology
- **THEN** the method MUST fail with a clear caller-facing error instead of proceeding with partially initialized mapping state

### Requirement: Resumed PHY commands preserve the normal completion-event contract

When `PHY` resumes a suspended write or erase command, it MUST re-enter the simulator through the same completion-event contract used by non-suspended commands. Resume-generated completion events MUST carry `chip_id`, `die_id`, and the original `transactions` batch in a mapping payload that `PHY.execute(...)` can consume without resume-specific branching. Suspending the original command MUST invalidate the superseded in-flight completion event so the resumed command can complete exactly once.

#### Scenario: Resumed write schedules a normal completion event
- **WHEN** a suspended write command is restored on a die and `PHY` schedules its remaining execution time
- **THEN** `PHY` MUST register a `PHY_CHIP_WRITE_COMPLETE` event whose payload includes the resumed command's `chip_id`, `die_id`, and `transactions`

#### Scenario: Resumed erase schedules a normal completion event
- **WHEN** a suspended erase command is restored on a die and `PHY` schedules its remaining execution time
- **THEN** `PHY` MUST register a `PHY_CHIP_ERASE_COMPLETE` event whose payload includes the resumed command's `chip_id`, `die_id`, and `transactions`

#### Scenario: Suspend invalidates the superseded completion event
- **WHEN** a write or erase command is suspended after its original chip-completion event has already been scheduled
- **THEN** `PHY` MUST mark the superseded completion event ignored before scheduling the resumed completion event

### Requirement: Resumed GC writes preserve GC-aware chip classification

When a suspended write command is resumed, `PHY` MUST determine whether it is a GC write from the resumed transaction metadata using the same transaction-type interpretation used by the normal write path. A resumed `GC_WRITE` MUST restore `ChipStatus.GC_WRITE`, while non-GC writes MUST restore `ChipStatus.WRITE`, and the resume path MUST NOT throw a type error while performing this classification.

#### Scenario: Resume restores GC write chip status
- **WHEN** the resumed command's first transaction is a `GC_WRITE`
- **THEN** `PHY` MUST mark the chip as `ChipStatus.GC_WRITE` and continue scheduling completion normally

#### Scenario: Resume restores user write chip status
- **WHEN** the resumed command's first transaction is a non-GC write such as `USER_WRITE` or `MAPPING_WRITE`
- **THEN** `PHY` MUST mark the chip as `ChipStatus.WRITE` and continue scheduling completion normally
### Requirement: Finite CMT misses use NAND mapping pages and bounded departing state

When ideal mapping is disabled, the FTL SHALL model a finite CMT. A clean CMT victim MUST be discarded without generating a mapping write and MUST NOT be retained as an unlimited GMT hit. A dirty victim MAY remain in a temporary departing table only until its mapping-page write completes. A normal CMT miss MUST resolve through the GTD and a physical `MAPPING_READ`; concurrent misses to the same MVPN MUST join the in-flight read rather than issuing duplicate NAND reads.

#### Scenario: Clean CMT victim is revisited

- **WHEN** a clean entry is evicted from a full finite CMT and the same LPA is accessed later
- **THEN** the later access MUST miss the CMT and resolve through its physical mapping page instead of receiving a zero-delay GMT hit

#### Scenario: Dirty CMT victim is written back

- **WHEN** a dirty entry is evicted
- **THEN** the entry MUST remain temporarily accessible while its mapping write is in flight, and MUST leave departing state when that write completes

#### Scenario: Multiple misses share one MVPN read

- **WHEN** multiple waiting transactions miss entries belonging to an MVPN whose mapping read is already in flight
- **THEN** they MUST join that mapping read and continue only after its result arrives
