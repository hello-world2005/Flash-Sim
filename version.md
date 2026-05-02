# Version Update

## 本次目标
根据 `prompt.md` 统一 `PageData` 字段语义，并对映射写入/读取链路做一致性修正。

## 修改内容

### 1. 新增无效值常量（`flash_sim/common.py`）
新增以下常量用于统一无效语义：
- `INVALID_LPA = -1`
- `INVALID_MVPN = -1`
- `INVALID_DATA = -1`
- `INVALID_PPA = -1`

并将 `Transaction` 默认字段统一为：
- `lpa` 默认 `INVALID_LPA`
- `mvpn` 默认 `INVALID_MVPN`

### 2. 统一 `Transaction.get_response_from_transaction`（`flash_sim/common.py`）
- `MAPPING_WRITE <- MAPPING_READ`：从 `tr.response.data` 合并映射数据，不再依赖 `None`。
- `USER_READ/USER_WRITE <- MAPPING_READ`：
  - 使用 `response.valid_bitmap` + `response.data` 校验目标 lpa 是否有效；
  - 读取到的 ppa 通过本地解码转换为 `FlashAddress`。
- `USER_WRITE <- USER_READ_FOR_WRITE` 与 `GC_WRITE <- GC_READ`：改为基于 `INVALID_DATA` 判定未填充槽位。

### 3. 统一 PHY 落盘/读盘语义（`flash_sim/PHY.py`）
`PageData` 字段语义统一如下：
- `function == USER`：
  - `lpa = tr.lpa`
  - `mvpn = INVALID_MVPN`
  - `valid_bitmap` 长度 `SECTOR_PER_PAGE`
  - `data` 长度 `SECTOR_PER_PAGE`，无效槽位为 `INVALID_DATA`
- `function == MAPPING`：
  - `mvpn = tr.mvpn`
  - `lpa = INVALID_LPA`
  - `valid_bitmap` 长度 `LPA_NO_PER_MAPPING_PAGE`
  - `data` 长度 `LPA_NO_PER_MAPPING_PAGE`，无效槽位为 `INVALID_PPA`

并在 `_read_from_storage` 中按上述规则做合法性校验。

### 4. preconditioning 写入统一（`flash_sim/FTL.py`）
- USER page 预置写入：
  - 补齐/规范 `valid_bitmap` 为固定长度 `SECTOR_PER_PAGE`
  - `data` 固定长度，invalid 槽位填 `INVALID_DATA`
  - 写入 `mvpn = INVALID_MVPN`
- MAPPING page 预置写入：
  - `lpa = INVALID_LPA`
  - `data` 存储 ppa（通过 `translate_address_to_ppa`）
  - invalid 槽位填 `INVALID_PPA`

### 5. mapping read/write 链路修正（`flash_sim/FTL.py`）
- `generate_mapping_write_transaction`：映射 payload 默认值改为 `INVALID_PPA`。
- `generate_mapping_read_transaction`：
  - 普通读请求按 `trigger_tr.lpa` 读取单个映射槽位；
  - mapping write 合并场景（`lpa == INVALID_LPA`）按位图读取旧映射页中未覆盖槽位。
- `_handle_mapping_response`：
  - 使用 `tr.mvpn` 还原 lpa（修复原先按 `tr.lpa` 计算的问题）；
  - 通过 `response.valid_bitmap` + `response.data` 判定有效性；
  - 对 `source_req is None`（mapping write 内部读）场景做安全处理，避免错误访问 domain。

### 6. 其它一致性修正
- `flash_sim/HIL.py`：`_tile_data` 初始化 payload 从 `None` 改为 `INVALID_DATA`。
- `flash_sim/FTL.py`：GC 路径 `GC_WRITE` 初始 payload 改为 `INVALID_DATA`。
- `flash_sim/FTL.py`：`_lpa_for_physical_page` 判定由 `pd.lpa is not None` 改为 `pd.lpa != INVALID_LPA`。

## 验证结果
- 运行主流程：`python main.py`（在 `flash_sim` 目录）
- 结果：仿真正常结束，日志出现 `Simulation completed.`，无新增运行时异常。

## 说明
- 当前环境缺少 `pytest`，未执行 `tests/test_preconditioning.py` 自动化回归。