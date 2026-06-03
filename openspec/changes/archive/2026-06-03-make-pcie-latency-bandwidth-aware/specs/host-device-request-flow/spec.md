## MODIFIED Requirements

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
