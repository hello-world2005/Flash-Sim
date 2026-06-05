## Implementation

- [x] 1. HIL 新增 `_ack_request_received()` 并在 receive_pcie_message 中调用
  - 文件: `flash_sim/HIL.py`
  - READ_REQ: 收到后立即发送 `READ_REQ_RECEIVED`
  - WRITE_DATA/SEARCH_DATA/COMPUTE_DATA/STATIC_WRITE_DATA: 收到数据后发送对应 `*_DATA_RECEIVED`
- [x] 2. Host 修复 `send_next_req` 中的 flow 状态管理
  - 文件: `flash_sim/Host.py`
  - 设置 `flow.busy = True`、`flow.current_req = next_req`
  - 调用 `recorder.note_host_sent()`
  - 补充 `STATIC_WRITE` 消息类型支持
- [x] 3. Host 修复 SQ 空满判断
  - 文件: `flash_sim/Host.py`
  - `Queue_ptrs.__init__` 接受 `sq_entries` 引用
  - `is_sq_empty`/`is_sq_full` 改用 `len(sq_entries[queue_id])`
  - `Host.__init__` 调整 Memory 和 Queue_ptrs 的初始化顺序
- [x] 4. Host 修复 `send_req` 双重调度
  - 文件: `flash_sim/Host.py`
  - `submit_req` 中 flow 可用时先 `sq_pop(target_sq_id)` 再 `send_req()`
- [x] 5. 修复 dataclass __eq__ 循环递归
  - 文件: `flash_sim/common.py`
  - `SimEvent.__eq__`: 只比较 time、type、target 身份，不遍历 param
  - `Request.__eq__`: 只比较 type、sq_id、lha_start、size、trace_index，不遍历 transaction_list
- [x] 6. 加固 PageData.valid_bitmap 空列表保护
  - 文件: `flash_sim/FTL.py`、`flash_sim/PHY.py`
  - FTL `generate_mapping_write_transaction`: 检查 `len(_gpd_bitmap) > 0`
  - FTL `translate_and_submit`: 检查 `len(_pd.valid_bitmap) > 0`
  - PHY `_read_from_storage`: 空页面返回全空 MAPPING 页面
- [x] 7. 幂等化 gmt.pop 和 _mark_invalid
  - 文件: `flash_sim/FTL.py`
  - `gmt.pop(lpa)` → `gmt.pop(lpa, None)`
  - `_mark_invalid` 对已无效页面 return 而非 raise

## Verification

- [x] 1. test_trace.json (489 条请求) 全部完成，退出码 0
- [x] 2. 持久化能耗一致：所有 WRITE 均为 11.34 μJ（不再出现 22.68 μJ）
- [x] 3. 全部 15 个 trace 测试通过
- [x] 4. 3 个 cache hit 结果正确：WRITE 后紧随 READ 同一 LPA 的命中缓存
- [x] 5. TSU 长时间等待与静态 LPA→plane 映射一致（手动计算验证）
- [x] 6. 无 RecursionError，仿真完整运行不中途崩溃
