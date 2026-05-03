# Version Update

## 本次目标
按 `prompt.md` 新增由 HIL 管理的 Data Cache（cache-line 机制），并接入 HIL/FTL/TSU 写入与读取路径。

## 修改内容

### 1) Data Cache 常量与约束（`flash_sim/common.py`）
- 新增：
  - `SECTOR_SIZE_BYTES = 64`
  - `DATA_CACHE_LINE_SIZE = 64`
  - `DATA_CACHE_CAP = 4096`
- HIL 数据缓存初始化时强校验：`DATA_CACHE_CAP` 必须是 `cache_line_size` 的整数倍。

### 2) HIL 新增 cache-line Data Cache（`flash_sim/HIL.py`）
- 重构原 `Cache/Cache_Manager` 为 `Data_Cache + Cache_Manager`：
  - 按 cache line（64B）存储；
  - 以 sector 级 line address 管理 user write 数据；
  - 支持 static write 缓存条目。
- 写请求（`WRITE` / `STATIC_WRITE`）数据到达后：
  - 若新地址写入超出容量，先触发 `write_flush`；
  - 若地址已存在，直接覆盖更新缓存行；
  - 数据先写入缓存，再立即完成主机请求（写回策略）。
- `write_flush`：
  - 通过 `FTL.address_mapping_unit.translate_and_submit(...)` 生成并提交 flush 事务到 TSU；
  - 将缓存中的 user/static 写条目全部下刷；
  - 刷新后清空全部 cache lines 与 pending 条目。
- 读请求（`READ`）优先查 Data Cache：
  - 命中则直接完成对应 transaction；
  - 未命中 transaction 保留并继续走 FTL。

### 3) HIL 请求流程调整（`flash_sim/HIL.py`）
- `WRITE_REQ/STATIC_WRITE_REQ`：先分段并取数据，不再在数据未到达时直接提交 FTL。
- `WRITE_DATA/STATIC_WRITE_DATA`：写入 Data Cache 并直接返回 REQ_COMP。
- `READ_REQ`：缓存命中时直接完成请求；miss 才走 FTL。
- `_tile_data` 增强 static write 数据填充逻辑。

### 4) 静态写 flush 兼容性修正（`flash_sim/FTL.py`）
- `Block_Manager._set_barrier` 新增对 `USER_STATIC_WRITE` 的 barrier 支持。
- `Block_Manager._on_transaction_serviced` 新增对 `USER_STATIC_WRITE` barrier 释放。

### 5) 新增测试（`tests/test_data_cache.py`）
- 覆盖点：
  - cache 容量与 cache line 对齐校验；
  - 写入后读命中返回缓存数据；
  - 缓存满触发 flush 并继续缓存新写入；
  - static write 可通过 flush 提交到 AMU。

## 验证说明
- 计划执行现有测试，但当前命令环境缺少 `pwsh`，无法通过 CLI 运行测试命令。
