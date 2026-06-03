## MODIFIED Requirements

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
