## MODIFIED Requirements

### Requirement: TSU 调度必须满足 die 和 plane 约束并尊重 barrier

对同一 chip 的普通读写擦事务，`TSU` SHALL 以 die 为选择粒度，并在每个 die 内满足 plane 复用和页号一致性约束；同时 `TSU` MUST 阻止被 LPA/MVPN barrier、GC 擦除屏障，或 static wear-leveling 迁移屏障拦住的事务被下发。

#### Scenario: 事务被 GC 或 static WL barrier 阻塞

- **WHEN** 某个待调度事务依赖尚未完成的同 LPA、同 MVPN，或命中了当前正被 GC / static wear leveling 保护的块与页面
- **THEN** `TSU` MUST 跳过该事务，直到对应 barrier 被释放后才允许把事务发送到 `PHY`

### Requirement: Block Manager 和 GC 必须维护块状态并在阈值下触发回收

`Block_Manager` SHALL 维护每个 plane / block 的 `free`、`valid`、`invalid` 页面统计、写前沿与擦写次数；`GC_WL_Unit` MUST 在 free block pool 低于阈值时选择 GC victim block、提交 `GC_READ`、`GC_WRITE` 和 `GC_ERASE` 事务链，并在擦除完成后把该 block 按当前擦写次数放回可分配 free pool，同时继续检查是否需要触发 static wear leveling。

#### Scenario: Free block 数量低于 GC 阈值

- **WHEN** 某个 plane 的 `free_block_pool` 数量小于或等于 GC 阈值
- **THEN** `GC_WL_Unit` MUST 选择一个安全的 victim block，提交完整的 `GC_READ`、`GC_WRITE` 和 `GC_ERASE` 事务链，并在擦除完成后更新 block bookkeeping

#### Scenario: GC 擦除完成后块回收到 WL-aware free pool

- **WHEN** 一个 `GC_ERASE` 或 static-WL 擦除事务完成
- **THEN** `Block_Manager` 和 `GC_WL_Unit` MUST 将被擦除 block 以最新 `wl_level` 重新加入可分配 free pool，并在同一 plane 上继续评估是否需要 follow-up 的 static wear leveling

## ADDED Requirements

### Requirement: Dynamic wear leveling chooses the lowest-erase eligible free block

当 `Block_Manager` 需要为用户写、mapping 写或 GC 迁移写切换到新的 free block 时，`GC_WL_Unit` SHALL 基于块的擦写次数选择当前 plane 内擦写次数最低且满足安全条件的 free block，而不是仅按 block 编号或隐式 frontier 顺序消费 free pool。

#### Scenario: 写前沿切换时优先选择最低擦写次数 free block

- **WHEN** 同一 plane 的 free pool 中存在多个可用 free block，且它们的 `wl_level` 不同
- **THEN** 新的 write frontier 或 GC destination block MUST 选择 `wl_level` 最低的那个合格 free block

### Requirement: Static wear leveling migrates cold data after erase completion

在任意一次 GC/WL 擦除完成后，`GC_WL_Unit` SHALL 评估当前 plane 的 wear skew；当磨损差距超过策略阈值且存在安全的低磨损冷块时，系统 MUST 迁移该冷块中的有效页到更高磨损的可用块，并对原块提交擦除，完成一次 static wear-leveling 流程。

#### Scenario: 擦除完成后触发 static wear leveling

- **WHEN** 某个 plane 在 GC 擦除完成后检测到磨损差距超过 static-WL 阈值，且存在可迁移的安全冷块
- **THEN** `GC_WL_Unit` MUST 提交一条与 GC 相同结构的迁移事务链，把冷块中的有效页搬迁出去并擦除原块

### Requirement: GC and static-WL candidates must exclude unsafe blocks

`GC_WL_Unit` MUST 仅从安全块集合中选择 GC victim、static-WL source 和迁移目标块。安全块集合不得包含当前写前沿块、已处于 GC/WL 擦除保护中的块，或仍带有进行中用户 program 归属的块。

#### Scenario: 非安全块不会被选为 GC 或 static-WL 候选

- **WHEN** 一个块仍是活跃写前沿、已被 GC/WL barrier 保护，或仍有进行中的用户写归属
- **THEN** `GC_WL_Unit` MUST 不把该块选作 GC victim、static-WL source，或任何迁移目标块
