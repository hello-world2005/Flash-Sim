## Why

在 `fix-host-sq-flow-deadlock` 修复后，test_trace 完成率达到 489/489，但批量运行全部 15 个 trace 时发现 3 个预存测试失败和 2 个工具链问题。

## What Changes

- 修复 `test_search_compute.json` 中 2 条 SEARCH 请求永不完成的问题
- 修复 `test_read_error_cases.json` 中 1 条 READ 请求永不完成的问题
- 修复 `test_multi_write.json` 中 `_count_new_ready_lines` 按 sector 而非 page 计数导致 cache 容量检查失败
- `main.py` 的 `INPUT_JSON` 改为接受命令行参数或环境变量，而非硬编码 `test_trace.json`
- CSV 报告中增加 `status` 列，使 ERROR 请求在表格中可见

## Capabilities

### Modified Capabilities
- `host-device-request-flow`: SEARCH/READ 请求路径
- `simulator-tooling`: main.py 命令行接口、CSV 报告格式

## Impact

- 测试：`test_search_compute.json`、`test_read_error_cases.json`、`test_multi_write.json`
- 工具：`flash_sim/main.py`、`flash_sim/request_latency_report.py`

## Non-goals

- 不改变 TSU 调度或 cache 策略
- 不引入新的 spec capability
