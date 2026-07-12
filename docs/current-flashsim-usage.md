# Flash-Sim Pre-modern 当前使用说明

本文记录当前 `Flash-Sim` 的推荐使用方式，便于和他人讨论、对接实验结果。这里的“当前”指本仓库现有事件引擎、FTL/GC/WL、CWDP、precondition 和 validation harness 的组合状态。

## 一句话结论

当前代码已经通过：

- `pytest test_script -q`：292 个测试全部通过。
- `validation/mqsim_flash` 的 small-profile deterministic cases：`write_stream`、`flush_then_read`、`rich_aligned`、`parallel_cwdp`、`overwrite_mapping`、`gc_pressure`、`hot_gc_backpressure`、`wear_leveling` 全部通过。
- Exchange 10k external replay：Flash-Sim `10387/10387` success，`errors=0`。
- Exchange 30k compact trace + 50% capacity-fill + 256 blocks/plane：`cache_bypass=true` 和 `cache_bypass=false,data_cache_capacity=64KB` 两种路径都已通过。

日常 correctness/smoke test 推荐使用 `flashsim-event-small` profile；大 geometry/50k 实验需要显式设置 geometry，不能直接把 small profile 的耗时结果当作性能结论。

## 入口

推荐使用虚拟环境里的 Python：

```bash
export FLASHSIM_ROOT=.
PY=python3
```

直接运行 Flash-Sim event engine：

```bash
$PY -m flash_sim.cli run-engine TRACE.json \
  --quiet \
  --no-timeline \
  --no-viz
```

常用参数：

- `--pre-trace PRE.json`：使用已有 precondition 文件。
- `-c CONFIG.json`：读取 runtime 配置，例如 `precondition_fill_ratio`、`cache_bypass`、`data_cache_capacity`、`plane_allocation`。
- `--cache-bypass`：绕过 host-visible write cache，常用于和 MQSim media path 对齐。
- `--plane-allocation PAGE_LEVEL|CWDP`：用户写入的 plane 分配策略。
- `--quiet`：关闭普通初始化输出。
- `--no-timeline`：大 trace 强烈建议打开，避免生成 timeline 事件。
- `--fast-report`：validation 兼容开关；当前大 trace 仍会生成 request latency report。

输出默认在：

```text
report/<trace_stem>_request_latency.json
report/<trace_stem>_request_latency.csv
```

## Geometry/Profile

当前有三类常用设置：

| 场景 | 推荐方式 | 说明 |
|---|---|---|
| 日常单元测试 | 默认配置 | 快，覆盖细节逻辑。 |
| validation smoke/correctness | `flashsim-event-small` | 64 blocks/plane，8 pages/block，适合 GC/WL/CWDP correctness。 |
| 50k/性能实验 | 显式设置 `FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256` 或更大 | small geometry 容量压力过大，50k 会被 GC/backpressure 放大。 |

不要把 `flashsim-event` 大 profile 当默认 smoke test。当前 Python PHY storage 是 eager allocation，大 geometry 初始化成本很高。

## Precondition

有两种方式：

### 1. 自动 precondition

在 config 中设置：

```json
{
  "runtime": {
    "precondition_fill_ratio": 0.5,
    "precondition_mode": "capacity-fill",
    "precondition_seed": 42,
    "cache_bypass": true,
    "plane_allocation": "CWDP"
  }
}
```

然后不传 `--pre-trace`，engine 会从 trace 自动生成 precondition。推荐的 `capacity-fill` 模式会先保留“首次访问就是 read/search/compute”的 LPA，避免 invalid-read；随后从 trace write LPA 中补充，最后生成 deterministic filler LPA，直到接近 `precondition_fill_ratio` 指定的可预填充 user-page 容量。当前默认生成的是 valid-only 初始数据，FTL 会按 plane 紧凑放置这些页，避免 50% fill 被人为打散到几乎所有 block 后导致 GC/backpressure 被严重放大。

如果需要复现实验早期的旧语义，可以设置：

```json
{
  "runtime": {
    "precondition_fill_ratio": 0.5,
    "precondition_mode": "trace-cover"
  }
}
```

`trace-cover` 只从 trace 涉及的 LPA 中选页，不会向全盘补 filler；当 10k trace touched pages 少于 25% 容量时，25/50/75% 会退化成相同初始状态。

### 2. 手动 precondition

如果已经有 precondition JSON：

```bash
$PY -m flash_sim.cli run-engine TRACE.json \
  --pre-trace PRECOND.json \
  --quiet --no-timeline --no-viz
```

## Cache

推荐把 cache 实验分成两类明确命名：

- `cache_bypass=true`：绕过控制器 write cache，写请求进入 NAND/FTL media path，适合和 MQSim media-side 行为对齐。
- `cache_bypass=false`：启用控制器 write cache，host-visible 完成点是 cache 接收边界；持久化路径会继续在后台 flush。

cache 容量通过 runtime config 设置，默认是 `262144` 字节。下面三个键是等价别名，推荐新配置使用 `data_cache_capacity`：

```json
{
  "runtime": {
    "cache_bypass": false,
    "data_cache_capacity": 65536
  }
}
```

也可以写成 `cache_capacity` 或 `cache_cap`。64KB cache 对应 `65536` 字节，当前 cache 以 64B sector line 计数，所以 64KB 是 1024 lines。

## Validation

默认 validation 已改为 small profile：

```bash
$PY validation/mqsim_flash/run_validation.py \
  --skip-build \
  --timeout 300
```

跑指定 case：

```bash
$PY validation/mqsim_flash/run_validation.py \
  --profile flashsim-event-small \
  --case parallel_cwdp \
  --skip-build \
  --timeout 300
```

输出在：

```text
validation/mqsim_flash/out/<profile>/<case>/
```

重点文件：

- `summary.json`：机器可读汇总。
- `analysis.md`：文字分析。
- `report.html`：可视化报告。
- `flashsim_stdout.log` / `mqsim_stdout.log`：两边原始输出。

注意：external trace 带 MQSim warmup prefix 时，MQSim stdout 可能出现 generated 和 serviced count 不一致。当前 harness 将这个作为 diagnostic note，不作为 Flash-Sim correctness failure；Flash-Sim 的 `request_count/success_count/error_count` 仍是硬 gate。

## 示例：Exchange 10k validation

```bash
$PY validation/mqsim_flash/run_validation.py \
  --profile flashsim-event-small \
  --external-flash-trace validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_flashsim.json \
  --external-mqsim-trace validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_mqsim.trace \
  --external-name exchange_disk0_page_10k_small \
  --external-max-requests 10000 \
  --external-address-mode compact \
  --external-precondition read-pages \
  --flashsim-no-timeline \
  --flashsim-fast-report \
  --skip-build \
  --timeout 300
```

当前已验证结果：

```text
Flash-Sim 10387/10387 success, errors=0
validation PASS
```

## 示例：50% precondition + 50k trace

如果要做“50% precondition + 50k Exchange trace”的 Flash-Sim 实验，推荐分两步：

1. 先把 public Exchange trace compact-normalize 到当前 Flash-Sim 地址空间。
2. 再用 `precondition_fill_ratio=0.5` 和 `precondition_mode=capacity-fill` 跑 Flash-Sim。

### 第一步：生成 compact normalized 50k trace

```bash
$PY - <<'PY'
from pathlib import Path
from validation.mqsim_flash.run_validation import (
    PROFILES,
    build_external_trace_case,
    write_case_inputs,
)

profile = PROFILES["flashsim-event-small"]
case = build_external_trace_case(
    profile,
    Path("validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_flashsim.json"),
    Path("validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_mqsim.trace"),
    name="exchange_disk0_page_50k_compact",
    max_requests=50000,
    address_mode="compact",
    precondition_mode="none",
    mqsim_preconditioning=False,
    mqsim_initial_occupancy_percentage=0,
)
paths = write_case_inputs(
    case,
    profile,
    Path("validation/mqsim_flash/out/flashsim-event-small/exchange_disk0_page_50k_compact"),
)
print(paths["flash_trace"])
PY
```

这会生成：

```text
validation/mqsim_flash/out/flashsim-event-small/exchange_disk0_page_50k_compact/validation_flashsim-event-small_exchange_disk0_page_50k_compact_flashsim_trace.json
```

### 第二步：使用 50% precondition config

仓库里提供了示例配置：

```text
examples/flashsim_50pct_cwdp_config.json
```

内容如下：

```json
{
  "runtime": {
    "precondition_fill_ratio": 0.5,
    "precondition_mode": "capacity-fill",
    "precondition_seed": 42,
    "cache_bypass": true,
    "plane_allocation": "CWDP",
    "gc_low_watermark": 3,
    "stop_servicing_writes_threshold": 1,
    "gc_reserve_blocks": 1
  }
}
```

### 第三步：运行 50k

推荐先使用 256 blocks/plane 的中等 geometry；small geometry 对 50k 压力过大，容易把时间花在 GC/backpressure 上。

```bash
timeout 300s env \
  FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256 \
  $PY -m flash_sim.cli run-engine \
  validation/mqsim_flash/out/flashsim-event-small/exchange_disk0_page_50k_compact/validation_flashsim-event-small_exchange_disk0_page_50k_compact_flashsim_trace.json \
  -c examples/flashsim_50pct_cwdp_config.json \
  --quiet \
  --no-timeline \
  --no-viz \
  --fast-report
```

如果希望更接近 MQSim validation 的外部 replay 流程，而不是自动 50% fill，可以使用 harness 的 `read-pages` precondition：

```bash
timeout 300s $PY validation/mqsim_flash/run_validation.py \
  --profile flashsim-event-small \
  --external-flash-trace validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_flashsim.json \
  --external-mqsim-trace validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_mqsim.trace \
  --external-name exchange_disk0_page_50k_small \
  --external-max-requests 50000 \
  --external-address-mode compact \
  --external-precondition read-pages \
  --flashsim-no-timeline \
  --flashsim-fast-report \
  --skip-build \
  --timeout 300
```

这条命令验证的是“读页覆盖 precondition + external replay”，不是 50% fill ratio。

## 读结果

快速查看 Flash-Sim 成功数和错误数：

```bash
$PY - <<'PY'
import json
from pathlib import Path

path = Path("report/TRACE_STEM_request_latency.json")
data = json.loads(path.read_text())
reqs = data["requests"]
print("requests", len(reqs))
print("success", sum(r["status"] == "SUCCESS" for r in reqs))
print("errors", sum(r["status"] == "ERROR" for r in reqs))
print("maintenance", data["meta"]["maintenance"])
PY
```

常看字段：

- `requests[*].status`：请求是否成功。
- `requests[*].error_message`：错误原因。
- `requests[*].breakdown`：host-visible latency 分解。
- `requests[*].persistence_*`：写入持久化路径。
- `meta.maintenance.gc_count`：GC 次数。
- `meta.maintenance.gc_relocated_pages`：GC 搬迁页数。
- `meta.maintenance.gc_erased_blocks`：GC erase 次数。
- `meta.maintenance.write_amplification`：写放大。

## Debug

普通运行保持 quiet：

```bash
--quiet
```

需要打开 `debug_info()`：

```bash
FLASHSIM_DEBUG=1 $PY -m flash_sim.cli run-engine TRACE.json
```

需要查看 pending 状态：

```bash
FLASHSIM_DUMP_PENDING=1 $PY -m flash_sim.cli run-engine TRACE.json
```

大 trace 不建议打开 debug，否则输出会严重拖慢仿真。

## 对外讨论时的建议表述

可以这样描述当前状态：

> 当前 Flash-Sim pre-modern 的 event engine、CWDP allocation、capacity-fill preconditioning、GC/WL/backpressure 路径已经通过单元测试和 small-profile MQSim validation。日常 correctness 使用 `flashsim-event-small`；50k/性能实验需要显式设置更大的 runtime geometry，例如 `FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256`。50% precondition 通过 `runtime.precondition_fill_ratio=0.5` 和 `runtime.precondition_mode=capacity-fill` 生成；当前 valid-only precondition 会按 plane 紧凑放置，避免人为制造严重 GC 碎片。cache 实验用 `runtime.cache_bypass` 区分 bypass/media path 和 controller-cache path，用 `runtime.data_cache_capacity` 指定 cache 容量。external public trace 需要先 compact-normalize。
