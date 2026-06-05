## Context

Host 与 Device 之间通过 PCIe 传递请求和数据。Host 维护 8 个 Submission Queue (SQ) 和 8 个 IO Flow，每个 Flow 绑定一个 SQ。Flow busy 时新请求排队在 SQ 中，等待 Flow 释放后由 `send_next_req()` 调度。Flow 的释放依赖于 Device 发回的 `WRITE_DATA_RECEIVED` / `READ_REQ_RECEIVED` / `SEARCH_DATA_RECEIVED` / `COMPUTE_DATA_RECEIVED` 四种 ACK 消息。修复前这四种消息从未被任何模块发送。

## Goals / Non-Goals

**Goals:**
- 修复 Host SQ/IO Flow 死锁，使 trace 中所有请求均能被调度和完成
- 修复 SQ 指针漂移导致排队的请求被遗留在 SQ 中
- 修复 send_req/send_next_req 双重调度导致缓存数据被重复写入 NAND
- 修复 dataclass __eq__ 循环递归导致的 RecursionError
- 加固 FTL/PHY 对未初始化页面的鲁棒性

**Non-Goals:**
- 不改变 NVMe 协议模型（如 doorbell 寄存器、DMA）
- 不改变 TSU 调度优先级或 cache 策略
- 不添加新的 spec capability

## Decisions

### 1. HIL 发送 ACK 的时机

**决定**: 在 `HIL.receive_pcie_message()` 中，READ_REQ 到达后立即发送 `READ_REQ_RECEIVED`；WRITE_DATA/SEARCH_DATA/COMPUTE_DATA/STATIC_WRITE_DATA 到达后、在 `cache_write` 之前发送对应的 `*_DATA_RECEIVED`。

**理由**: ACK 必须在 cache_write（可能触发 write_flush）之前发送，确保 Host 侧 Flow 及时释放。ACK 仅携带 `sq_id`，开销极小。

### 2. SQ 空满判断改用 sq_entries 实际长度

**决定**: `Queue_ptrs.is_sq_empty()` 和 `is_sq_full()` 改用 `len(self._sq_entries[queue_id])` 替代 `head == tail` 对比。

**理由**: head/tail 循环队列模型要求 push/pop/remove 三者严格同步，但 `sq_pop`（由 `send_next_req` 调用）移除条目时不更新任何指针。head 和 tail 会逐渐漂移，最终 head == tail 但 `sq_entries` 仍非空。直接以 list 长度为准是最简单可靠的修复。

**权衡**: 需要 `Queue_ptrs` 持有 `sq_entries` 的引用（初始化时注入）。

### 3. send_req 前从 sq_entries 移除

**决定**: `submit_req()` 中 flow 可用时，先调用 `sq_pop(target_sq_id)` 从 sq_entries 中移除请求，再调用 `send_req()`。

**理由**: `send_req` 直接发送，不经过 `sq_pop`。若不移除，后续 `send_next_req` 会再次 pop 同一请求并重复发送，导致同一 WRITE 被 cache 两次、持久化能耗翻倍。

### 4. SimEvent/Request __eq__

**决定**: 为 `SimEvent` 和 `Request` 分别添加自定义 `__eq__`。`SimEvent` 仅比较 `time`、`type` 和 `target is` 身份。`Request` 仅比较 `type`、`sq_id`、`lha_start`、`size`、`trace_index`。

**理由**: dataclass 自动生成的 `__eq__` 遍历所有字段，沿 `param → Transaction → exec_event → SimEvent` 以及 `transaction_list → Transaction → source_req → Request` 形成无限递归。更多活跃请求 → 更多事件在队列中共存 → 比较操作更频繁 → 递归更容易触发。

### 5. PageData 空页面保护

**决定**: FTL 中访问 `_gpd.valid_bitmap` 前检查 `len(bitmap) > 0`；PHY 中 `_read_from_storage` 对 `function is None` 的页面返回全空 MAPPING 页面；内部 mapping read（无 source_req）返回空数据而不抛异常。

**理由**: `PageData()` 默认 `valid_bitmap = []`。cache flush 频率增大后，mapping write 频繁触发的 read-modify-write 模式更易访问未写入页面。对用户请求保持 RequestFailure 报错；对内部操作静默返回空数据。

### 6. gmt.pop 和 _mark_invalid 防御

**决定**: `gmt.pop(lpa)` → `gmt.pop(lpa, None)`；`_mark_invalid` 对已无效页面 return 而非 raise。

**理由**: 更多写操作产生更多 mapping 更新和物理页失效，同一 LPA 或物理页可能被重复处理。幂等化处理避免不必要的崩溃。

## Risks / Trade-offs

- `is_sq_empty`/`is_sq_full` 改用 list 长度后，`get_sq_for_request` 中的 occupancy 计算（`sq_tails - sq_heads`）仍使用旧指针，可能影响 SQ 选择的最优性但不影响正确性。
- `_read_from_storage` 对内部 mapping read 返回空数据，可能掩盖某些 FTL mapping 逻辑错误。建议后续添加更严格的 mapping 一致性校验。
