## Why

事件驱动仿真器在 trace 请求数量不超过 8 条时可以正常工作，但超过 8 条后，第 9 条及之后的所有请求均无法完成，报告中 `completion_time` 为空、全部阶段延时为零。经排查，根因是 Host 侧 IO Flow / Submission Queue 的释放机制依赖于 `WRITE_DATA_RECEIVED`、`READ_REQ_RECEIVED`、`SEARCH_DATA_RECEIVED`、`COMPUTE_DATA_RECEIVED` 四种 ACK 消息，但 HIL 从未发送这些消息。前 8 条请求恰好各占一个 SQ（0-7），通过 `send_req()` 直接发送成功；第 9 条起所有 8 个 IO Flow 均处于 busy 状态，后续请求永远排队在 SQ 中无法被 dispatch。

在修复主 bug 的过程中，还发现并修复了以下连带问题：

1. **SQ 指针漂移导致 send_next_req 失效**：`sq_pop` 仅从 `sq_entries` list 中移除条目，但不更新 `sq_tails` 指针；`is_sq_empty` 依赖 `head == tail`，二者逐步漂移后导致 SQ 中尚有等待请求时被误判为空。
2. **send_req 双重调度导致持久化能耗翻倍**：`send_req` 发送请求后未从 `sq_entries` 中移除，后续 `send_next_req` 再次 pop 同一请求并重复发送，导致同一 LPA 被 cache flush 两次，持久化能耗从 11.34 μJ 翻倍至 22.68 μJ。
3. **dataclass __eq__ 循环递归**：`SimEvent` 和 `Request` 的 dataclass 自动生成的 `__eq__` 沿 `param → Transaction → exec_event → SimEvent` 和 `transaction_list → Transaction → source_req → Request` 形成循环引用，在事件队列较大时触发 `RecursionError`。
4. **PageData.valid_bitmap 空列表 IndexError**：FTL 和 PHY 多处代码假定已写入页面的 `valid_bitmap` 长度为 `LPA_NO_PER_MAPPING_PAGE`(256)，但未写入页面为 `[]`。更多请求导致更频繁的 cache flush 和 mapping 操作，暴露了这些位置。
5. **gmt.pop() KeyError**：mapping write 完成后失效化旧 LPA 时 `gmt.pop(lpa)` 可能因 LPA 已被前序操作移除而抛出 KeyError。
6. **_mark_invalid 幂等缺失**：同一物理页可能被多次标记无效，第二次调用时应静默跳过而非抛出 ValueError。

## What Changes

- **HIL.py**: 新增 `_ack_request_received()` 方法，在收到 Host 数据后通过 PCIe 发送对应的 `*_RECEIVED` ACK 消息，使 Host 能正确释放 IO Flow 并调度下一个排队请求。
- **Host.py**: 修复 `send_next_req()` 中缺失的 flow 状态管理（`flow.busy`、`flow.current_req`）和 latency recorder 钩子；修复 `send_req()` 调用前需从 `sq_entries` 中移除条目；修改 `is_sq_empty`/`is_sq_full` 改用 `len(sq_entries)` 替代不同步的 head/tail 指针。
- **common.py**: 为 `SimEvent` 和 `Request` 添加自定义 `__eq__`，避免 dataclass 默认比较遍历循环引用字段导致递归溢出。
- **FTL.py**: 3 处 `valid_bitmap` 索引增加空列表保护；`gmt.pop(lpa)` 改为 `gmt.pop(lpa, None)`；`_mark_invalid` 对已无效页面改为静默返回。
- **PHY.py**: `_read_from_storage` 对未写入页面返回全空 MAPPING 页面；内部 mapping read（无 `source_req`）返回空数据而非抛出 ValueError。

## Capabilities

### Modified Capabilities
- `host-device-request-flow`: Host SQ/IO Flow 管理逻辑修正，HIL 增加 ACK 发送。
- `ftl-scheduling-and-media-model`: Block Manager 和 PHY 的页面状态处理增加鲁棒性。

## Impact

- 代码范围：`flash_sim/Host.py`、`flash_sim/HIL.py`、`flash_sim/common.py`、`flash_sim/FTL.py`、`flash_sim/PHY.py`
- `test_trace.json`（489 条请求）完成率从 8/489 (1.6%) 提升至 489/489 (100%)
- 其他 14 个 trace 测试全部通过（3 个预存 bug 不受影响）
- 持久化能耗值修正为统一的 11.34 μJ/页

## Non-goals

- 不修改 TSU 调度算法或 cache 策略
- 不修复预存的 test_search_compute（2 条 SEARCH 未完成）、test_read_error_cases（1 条 READ 未完成）、test_multi_write（cache 容量检查按 sector 而非 page 计数）
- 不增加 CSV 新列
