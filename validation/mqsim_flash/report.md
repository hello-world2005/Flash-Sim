# Flash-Sim / MQSim 对齐报告

更新日期：2026-07-12。本文只把能够由当前代码和归档结果复现的数据列为“已对齐”；历史中间结果保留在其他 result 目录，但不作为最终结论。

## 最终 read 对齐结果

最终 read 行为对应的归档目录是：

`validation/mqsim_flash/results/read_mapping_aligned/flashsim-event-small-finite-cmt-aligned/exchange_pressure_20k_mapping_aligned/`

测试使用 Exchange trace 中连续 20,000 个读请求的最高压力窗口。两边均采用 64 B sector、4 KiB page、5 us page read、small64 geometry、64-entry CMT、non-ideal mapping table、相同 PCIe/ONFI 参数；47,195 个涉及的逻辑页在测试前有效。测量窗口内 GC、WL、erase 均为 0。

| 指标 | Flash-Sim | MQSim | Flash − MQ |
|---|---:|---:|---:|
| 请求数 | 20,000 | 20,000 | 0 |
| 平均延迟 | 16,154.495 ns | 16,148.747 ns | 5.748 ns |
| p50 | 14,748 ns | 14,728 ns | 20 ns |
| p95 | 26,346 ns | 26,248 ns | 98 ns |
| p99 | 33,912 ns | 33,736 ns | 176 ns |
| 最小值 | 7,556 ns | 7,546 ns | 10 ns |
| 最大值 | 95,934 ns | 105,988 ns | −10,054 ns |

### Read mapping 行为计数

| 行为 | Flash-Sim | MQSim | 说明 |
|---|---:|---:|---|
| 用户 page read | 80,264 | 80,264 | 完全一致 |
| CMT hit | 1,776 | 1,756 | 相差 20 次，来自初始 LRU 内容/同到达批次顺序 |
| CMT miss | 78,488 | 78,508 | 与 hit 差额守恒 |
| 物理 mapping-page read | 20,131 | 20,131 | 完全一致 |
| MQSim `Arriving_MVPN` join | 对应 pending-MVPN 合并 | 58,377 | 不新建 NAND read，等待已有 mapping read |
| MQSim `Departing_MVPN` bypass | GMT 仅保留 dirty departing entry | 0 | 正式读阶段没有 GMT 零延迟旁路 |
| MQSim `NO_MPPN` | 不适用 | 0 | 正式读阶段没有未物化 mapping page |
| GC / WL / erase | 0 / 0 / 0 | 0 / 0 / 0 | 纯读测量窗口 |

为得到上述结果，Flash-Sim 修正了 clean CMT eviction、dirty departing GMT 和 pending MVPN 合并；MQSim-test 修正了 mapping-write queue 从不被 service 选择，以及 mapping read/write 缺少反向依赖的问题。修复后 MQSim 的 1,362 个 mapping write 全部入队并全部出队。

## PCIe 与单页基础延迟

两边 host sector 都是 64 B，PCIe bandwidth 为 4 B/ns，128 B max payload，TLP overhead 28 B。4 KiB read 的关键路径为：submission 41 ns、NAND command/array/data-out、4 KiB payload 1,248 ns、CQ 11 ns。Flash-Sim 与 MQSim 的单页无竞争结果只保留约 10 ns ONFI 建模差额。

## Write 对齐现状

PCIe 和 NAND program 的组成公式已经对齐：4 KiB bypass write 的理论关键路径为 253,205 ns。

| write 阶段 | Flash-Sim / MQSim 对齐值 |
|---|---:|
| NVMe submission | 41 ns |
| write-data request | 8 ns |
| 4 KiB PCIe payload | 1,248 ns |
| ONFI program command | 361 ns |
| ONFI data-in | 1,536 ns |
| NAND program | 250,000 ns |
| CQ | 11 ns |
| 合计 | 253,205 ns |

但 write 的端到端行为仍未通过最终验证：修复 mapping-write 调度后重新运行 `results/write_mapping_fixed`，MQSim 生成 2 个请求但 serviced count 仍为 0。因此当前只能声明“PCIe/NAND 阶段公式对齐”，不能声明“host write completion 已对齐”。Flash-Sim 还区分 controller-cache host completion 与后续 persistence，MQSim 当前失败点需要单独继续定位。

## 如何复现

从仓库根目录执行：

```bash
export FLASHSIM_ROOT=.
export MQSIM_ROOT=../MQSim-test
PY=python3
cd "$FLASHSIM_ROOT"

# 最终 Exchange 20k read 对齐测试
$PY validation/mqsim_flash/run_exchange_all_reads_small64.py \
  --pressure-window 20000 \
  --case-dir validation/mqsim_flash/out/exchange_pressure_20k_small64_mapping_aligned \
  --timeout 300

# 基础 write 诊断
$PY validation/mqsim_flash/run_validation.py \
  --profile flashsim-event-small --case write_stream \
  --requests 2 --gap-ns 300000 \
  --output-dir validation/mqsim_flash/results/write_mapping_fixed \
  --skip-build --timeout 300
```

请求级结果：

- `.../flashsim_per_req.csv`：最终 Flash-Sim read CSV。
- `.../mqsim_per_req.csv`：最终 MQSim read CSV，只包含正式读阶段，不含预填充写。
- `.../mqsim_report.xml`：带 mapping miss 分支计数器的 MQSim XML。
- `.../summary.json`：最终延迟和行为计数的机器可读摘要。
- `.../analysis.md`：该 result 的详细分析。

完整验证工具说明见 [README.md](README.md)。
