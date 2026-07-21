# Flash-Sim 使用说明

Flash-Sim 是一个周期精确的 3D NAND Flash 模拟器，支持传统存储操作（读、写、擦除）和存内计算操作（搜索、计算），可配置 SLC/MLC/TLC 闪存技术。

> 当前 MQSim 对齐口径（2026-07-12）：Flash-Sim 与 `../MQSim-test`
> 均使用 64 B sector。最终 read-aligned 结果位于
> `validation/mqsim_flash/results/read_mapping_aligned/flashsim-event-small-finite-cmt-aligned/exchange_pressure_20k_mapping_aligned/`，
> 使用方法和当前 write 限制见 `validation/mqsim_flash/report.md`。

---

## 1. 项目架构

```
flash_sim/
├── __init__.py        # 包入口，导出主要类
├── config.py          # 配置类：FlashConfig, FlashGeometry, TimingConfig, RuntimeConfig
├── common.py          # 公共常量、事件系统、Request/Transaction/FlashAddress 定义
├── engine.py          # 事件驱动引擎 Engine（离散事件模拟）
├── Device.py          # SSD 设备顶层（HIL + FTL + PHY）
├── HIL.py             # 主机接口层（写缓存管理、流调度）
├── FTL.py             # 闪存转换层（地址映射、CMT/GTD、Block Manager、GC/WL、TSU）
├── PHY.py             # 物理层（通道传输、芯片命令调度）
├── chip.py            # Flash 芯片定时模型（延迟计算）
├── simulator.py       # 独立模拟器（命令级延迟计算，无事件循环）
├── parser.py          # JSON trace 解析与校验
├── cli.py             # 命令行界面
├── PCIe_link.py       # PCIe 链路建模
├── Host.py            # 主机端（SQ/CQ 提交完成）
└── utils.py           # 工具函数

validation/
└── mqsim_flash/       # MQSim 对标验证工具
    ├── run_validation.py          # 验证主脚本（多 profile 多 case）
    ├── run_test_matrix_latest.py  # 批量跑 test.md 矩阵
    └── traces/run_test/           # 固定的 compact-normalized 测试 trace

test_case/             # 引擎端 trace 示例
test_script/           # 测试脚本
```

---

## 2. 两个模拟路径

| 特性 | 独立模拟器 (Standalone) | 事件驱动引擎 (Event-Driven) |
|------|------------------------|----------------------------|
| CLI 子命令 | `flash-sim run` | `flash-sim run-engine` |
| 核心类 | `FlashSimulator` | `Engine` |
| 模拟粒度 | 单条命令延迟（无时序依赖） | 离散事件调度（全局时钟、并发、排队） |
| 写缓存 | 无 | 有（HIL Cache Manager） |
| GC / 磨损均衡 | 无 | 有（GC WL Manager） |
| 地址映射 | 简单 LBA→物理地址 | 完整 CMT/GTD/Block Manager |
| 通道/芯片级并行 | 无 | 有（TSU + PHY） |
| 适用场景 | 快速评估延迟、LBA 解析 | 完整 SSD 系统行为验证 |
| 配置方式 | `-c config.json`（完整参数） | `-c config.json`（runtime、ONFI、CIM 参数）+ 环境变量（结构 geometry） |

---

## 3. 安装

```bash
cd flash-sim

# 方式一：pip 安装
pip install -e .

# 方式二：直接设 PYTHONPATH
export PYTHONPATH=/path/to/flash-sim:$PYTHONPATH
```

---

## 4. CLI 使用

### 4.1 独立模拟器 `flash-sim run`

```bash
flash-sim run trace.json [-c config.json] [-o output.json] [--summary]
```

- 使用 `FlashSimulator`，逐条计算命令延迟
- 支持 read/write/erase/search/compute
- `--summary` 打印总延迟和成功/失败统计

### 4.2 事件驱动引擎 `flash-sim run-engine`

```bash
# 基本用法
flash-sim run-engine test_case/gc_test.json -c config.json

# 高级选项
flash-sim run-engine trace.json \
  -c flashsim_config.json \
  --pre-trace precondition.json \      # 预条件 trace
  --cache-bypass                       # 绕过写缓存
  --plane-allocation CWDP              # 分配策略：PAGE_LEVEL | CWDP
  --static-wl on                       # 静态磨损均衡：on | off
  --static-wl-threshold 16             # 静态 WL 触发阈值
  --quiet                              # 只输出关键信息
  --no-timeline                        # 关闭时间线记录
  --no-viz                             # 不生成 HTML 可视化
```

事件驱动引擎的 die/plane/block/SL/SSL 等结构 geometry 通过环境变量传递，
需要在调用前设好：

```bash
FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256 \
FLASHSIM_EVENT_RUNTIME_LAYERS_PER_BLOCK=128 \
  python -m flash_sim.cli run-engine trace.json -c config.json
```

两种方式等价（包路径 / 全局安装）：

```bash
# 方式 A：通过 pip -e . 安装后的入口
flash-sim run-engine trace.json ...

# 方式 B：直接运行模块
python -m flash_sim.cli run-engine trace.json ...
```

### 4.3 其他子命令

```bash
# 查看当前 geometry 信息
flash-sim info

# LBA -> 物理地址转换
flash-sim lba 1024

# 物理地址 -> LBA
flash-sim addr --die 0 --plane 0 --block 1 --layer 0 --sub-block 0 --page 0

# 交互式模式
flash-sim interactive

# 基准测试
flash-sim bench --ops 5000
```

---

## 5. 配置详解

### 5.1 FlashGeometry —— SSD 三维结构

从顶层到底层的层次：

```
Channel (8)
  └── Chip (4 per channel)
       └── Die (4 per chip)
            └── Plane (4 per die)
                 └── Block (1024 per plane)
                      └── Layer (128 per block)
                           └── Sub-Block = SL × SSL (1 per SL, 4 SSL/SL = 4)
                                └── Page (1 page per sub-block, per layer)
```

**计算公式**：
```
pages_per_block = layers_per_block × sub_blocks_per_block
                  = layers_per_block × sl_per_block × ssl_per_sl

total_pages = pages_per_die × dies
            = pages_per_plane × planes_per_die × dies
            = pages_per_block × blocks_per_plane × planes_per_die × dies
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `channel_no` | 8 | 独立通道数 |
| `chip_per_channel` | 4 | 每通道芯片数 |
| `dies` | 4 | 每芯片 die 数 |
| `planes_per_die` | 4 | 每 die plane 数 |
| `blocks_per_plane` | 1024 | 每 plane block 数 |
| `layers_per_block` | 128 | 每 block NAND layer 数 |
| `sl_per_block` | 1 | 每 block String Line 数 |
| `ssl_per_sl` | 4 | 每 SL 的 SSL 数（= 每 layer 页数） |
| `sub_blocks_per_block` | 4 (= sl×ssl) | 每 block sub-block 数 |
| `sector_per_page` | 16 | 每页扇区数（1 sector = 64 B） |
| `static_chip_per_channel` | 1 | 用作 static area 的 chip 数 |
| `compute_max_parallel_sl` | 256 | 计算操作最大并行 SL 数 |
| `search_max_parallel_wl` | 256 | 搜索操作最大并行 WL 数 |
| `wl_per_string` | 128 | 每条 NAND string 的 WL 数，也是 COMPUTE `selected_wl` 的上界 |
| `bl_per_plane` | 262144 | 每 plane 的 BL 数 |
| `search_input_bits_per_wl` | 1 | SEARCH keyword 在每条 WL 上的输入位数 |
| `search_match_bits_per_bl` | 1 | SEARCH 每条 BL 返回的匹配位数 |
| `compute_input_bits_per_sl` | 8 | COMPUTE 每个活动 SL/transaction 的输入位数 |
| `compute_accumulator_bits` | 8 | COMPUTE 每条 BL 的 ADC/累加结果位数 |

上表是 `FlashGeometry` 的公开默认值。事件引擎默认使用 compact 结构
（4 die、4 plane/die、64 block/plane、2 SL/block、4 SSL/SL），但 CIM 位宽、
WL/BL 数量和 `compute_max_parallel_sl` 可以从配置 JSON 的 `geometry` 段覆盖。

### 5.2 TimingConfig —— 时序参数

| 参数 | SLC 默认值 | MLC/TLC 说明 |
|------|-----------|-------------|
| `technology` | `"slc"` | `slc` / `mlc` / `tlc` |
| `t_r_lsb` | 5,000 ns | LSB 页读延迟 |
| `t_r_csb` | 100,000 ns | CSB 页读延迟（MLC/TLC） |
| `t_r_msb` | 150,000 ns | MSB 页读延迟（MLC/TLC） |
| `t_prog_lsb` | 250,000 ns | LSB 页编程延迟 |
| `t_prog_csb` | 1,000,000 ns | CSB 页编程延迟（MLC/TLC） |
| `t_prog_msb` | 1,500,000 ns | MSB 页编程延迟（MLC/TLC） |
| `t_bers` | 10,000,000 ns | Block 擦除延迟 |
| `t_search_lsb` | 200,000 ns | 搜索延迟 |
| `t_compute_lsb` | 500,000 ns | 计算延迟 |

### 5.3 OnfiTimingConfig —— ONFI 通道参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `channel_width_bytes` | 8 | 通道位宽（字节） |
| `t_rc` | 6 ns | 读周期 |
| `t_dsc` | 6 ns | 数据选通周期 |
| `t_dbsy` | 500 ns | 数据忙时间 |
| `t_wc` | 25 ns | 写周期 |
| `t_adl` | 70 ns | 地址数据加载 |
| `t_ccs` | 300 ns | 命令周期 |
| 其余 | ... | 参考 NVDDR2 规范 |

### 5.4 RuntimeConfig —— 运行时策略

#### GC 与磨损均衡

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `gc_low_watermark` | 3 | 触发 GC 的空闲 block 低水位 |
| `gc_exec_threshold` | None | GC 执行阈值比例（0.0~1.0） |
| `gc_victim_policy` | `"greedy"` | victim 选择：`greedy` / `d-choices` / `rga` |
| `gc_d_choices` | 10 | d-choices 的抽样数 |
| `gc_min_invalid_pages` | 1 | GC 最少无效页数 |
| `gc_min_invalid_ratio` | 0.0 | GC 最小无效页比例 |
| `gc_emergency_watermark` | 1 | 紧急 GC 水位 |
| `gc_reserve_blocks` | 1 | 保留 block 数 |
| `static_wl_enabled` | True | 是否启用静态磨损均衡 |
| `static_wl_wear_gap_threshold` | 2 | 静态 WL 触发的磨损差距阈值 |

#### 写缓存

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `cache_bypass` | False | 绕过写缓存（True = bypass） |
| `data_cache_capacity` | 262144 | 写缓存容量（单位：64 B sectors） |

#### 分配策略

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `plane_allocation` | `"PAGE_LEVEL"` | 页分配策略：`PAGE_LEVEL` 或 `CWDP` |
| `write_allocation_mode` | `"lpa-affine"` | 写分配：`lpa-affine` 或 `dynamic-cwdp` |

#### 预条件（Preconditioning）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `precondition_fill_ratio` | None | 填充比例（0.0~1.0），设值即启用自动预条件 |
| `precondition_mode` | `"capacity-fill"` | 模式：`capacity-fill` / `trace-cover` |
| `precondition_seed` | 42 | 随机种子 |

---

## 6. 配置 JSON 完整模板

```json
{
  "timing": {
    "technology": "slc",
    "t_r_lsb": 5000,
    "t_r_csb": 100000,
    "t_r_msb": 150000,
    "t_prog_lsb": 250000,
    "t_prog_csb": 1000000,
    "t_prog_msb": 1500000,
    "t_bers": 10000000
  },
  "onfi": {
    "channel_width_bytes": 8,
    "t_rc": 6,
    "t_dsc": 6,
    "t_dbsy": 500,
    "t_cs": 20,
    "t_rr": 20,
    "t_wb": 100,
    "t_wc": 25,
    "t_adl": 70,
    "t_cals": 15,
    "t_dqsre": 15,
    "t_rpre": 15,
    "t_rhw": 100,
    "t_ccs": 300,
    "t_wpst": 6,
    "t_wpsth": 15
  },
  "parallel": {
    "max_parallel_wl": 64,
    "max_parallel_blocks": 8
  },
  "geometry": {
    "channel_no": 8,
    "chip_per_channel": 4,
    "dies": 4,
    "planes_per_die": 4,
    "blocks_per_plane": 1024,
    "layers_per_block": 128,
    "sl_per_block": 1,
    "ssl_per_sl": 4,
    "sub_blocks_per_block": 4,
    "sector_per_page": 16,
    "compute_max_parallel_sl": 256,
    "search_max_parallel_wl": 256,
    "wl_per_string": 128,
    "bl_per_plane": 262144,
    "search_input_bits_per_wl": 1,
    "search_match_bits_per_bl": 1,
    "compute_input_bits_per_sl": 8,
    "compute_accumulator_bits": 8,
    "static_chip_per_channel": 1
  },
  "runtime": {
    "gc_low_watermark": 3,
    "gc_exec_threshold": 0.05,
    "gc_min_invalid_pages": 1,
    "gc_victim_policy": "d-choices",
    "gc_d_choices": 6,
    "cache_bypass": false,
    "data_cache_capacity": 65536,
    "plane_allocation": "CWDP",
    "write_allocation_mode": "dynamic-cwdp",
    "static_wl_enabled": true,
    "static_wl_wear_gap_threshold": 16,
    "precondition_fill_ratio": 0.25,
    "precondition_mode": "capacity-fill"
  }
}
```

**注意**：事件驱动引擎不会用 JSON 中的 channel/chip/die/plane/block/SL/SSL
数量重建存储数组，这些结构参数仍来自 `FLASHSIM_EVENT_RUNTIME_*` 环境变量。
但是 JSON `geometry` 中的 CIM 参数（WL/BL 数量、输入/输出位宽和 COMPUTE
并行上限）会传入 HIL、TSU 和 PHY；`onfi` 段也会传入事件引擎 PHY。

---

## 7. 如何修改 SSD Geometry

### 7.1 事件驱动引擎

在调用前设置 `FLASHSIM_EVENT_RUNTIME_*` 环境变量。这些变量在
`flash_sim/config.py` 中定义，并在 `flash_sim.common` 模块加载时生效。
这一方式只负责结构 geometry；CIM 和 ONFI 参数仍通过 `-c config.json` 设置。

```bash
# 修改为 256 blocks/plane 的 small64 变体
FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256 \
FLASHSIM_EVENT_RUNTIME_DIES=4 \
FLASHSIM_EVENT_RUNTIME_PLANES_PER_DIE=4 \
  python -m flash_sim.cli run-engine trace.json
```

常用组合对应的环境变量：

| 几何 | `DIES` | `PLANES_PER_DIE` | `BLOCKS_PER_PLANE` | `LAYERS_PER_BLOCK` | `SL_PER_BLOCK` | `SSL_PER_SL` |
|------|--------|-----------------|-------------------|-------------------|---------------|-------------|
| small64 | 4 | 4 | 64 | 1 | 2 | 4 |
| modern (512 ppb) | 4 | 4 | 2048 | 128 | 1 | 4 |
| FAST'18 论文 | 2 | 2 | 2048 | 64 (256/4) | 1 | 4 |

**Python API 方式**：

```python
import os
# 必须在 import flash_sim 之前设置
os.environ["FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE"] = "256"
os.environ["FLASHSIM_EVENT_RUNTIME_LAYERS_PER_BLOCK"] = "128"

from flash_sim.common import geometry
print(geometry.blocks_per_plane)  # 256
```

### 7.2 独立模拟器

通过 `-c config.json` 传入完整配置（包含 `geometry` 段）：

```bash
flash-sim run trace.json -c my_geometry.json
```

或通过 Python API：

```python
from flash_sim import FlashConfig, FlashGeometry, FlashSimulator

geo = FlashGeometry(
    channel_no=8, chip_per_channel=4,
    dies=4, planes_per_die=4, blocks_per_plane=256,
    layers_per_block=128, sl_per_block=1, ssl_per_sl=4,
)
config = FlashConfig(geometry=geo)
sim = FlashSimulator(config)
results = sim.run_trace(trace)
```

### 7.3 Validation 验证脚本

在 `validation/mqsim_flash/run_validation.py` 中，geometry 通过 Profile 定义（`PROFILES` 字典，第 96-177 行）。Profile 的字段通过 `flashsim_event_runtime_env(profile)`（第 180-201 行）转为环境变量。

修改验证的 geometry 有两种方式：

**方式 A**：修改现有 Profile 的字段
```python
PROFILES["flashsim-event-small"] = replace(
    PROFILES["flashsim-event-small"],
    block_no_per_plane=256,
)
```

**方式 B**：新增自定义 Profile
```python
PROFILES["my-custom"] = Profile(
    name="my-custom",
    page_no_per_block=128,
    block_no_per_plane=1024,
    plane_no_per_die=4,
    die_no_per_chip=4,
    flash_channel_count=8,
    chip_no_per_channel=3,
    # ... 其他字段
)
```

**方式 C**：修改 `run_test_matrix_latest.py` 的 `make_profiles()`
```python
def make_profiles():
    base = rv.PROFILES["flashsim-event-small"]
    return {
        "my-large": replace(base, block_no_per_plane=512),
    }
```

---

## 8. 如何修改其他 SSD 配置

### 8.1 运行时参数（GC、缓存、分配等）

**CLI 方式**：在 `-c config.json` 的 `runtime` 段中设置：

```json
{
  "runtime": {
    "gc_low_watermark": 5,
    "gc_victim_policy": "greedy",
    "cache_bypass": true,
    "plane_allocation": "PAGE_LEVEL",
    "precondition_fill_ratio": 0.5
  }
}
```

验证脚本中是通过 `write_flashsim_config()` 函数生成此文件（`run_test_matrix_latest.py:302-321`），直接修改该函数即可。

**Python API**（在 Engine 中通过 `apply_runtime_config` 应用）：

```python
from flash_sim.config import RuntimeConfig

runtime = RuntimeConfig(
    gc_low_watermark=5,
    gc_victim_policy="d-choices",
    gc_d_choices=6,
    cache_bypass=True,
    data_cache_capacity=65536,
    plane_allocation="CWDP",
    write_allocation_mode="dynamic-cwdp",
    static_wl_wear_gap_threshold=16,
    precondition_fill_ratio=0.25,
)
```

`RuntimeConfig` 的完整校验逻辑见 `flash_sim/config.py:626-682`，包括：
- GC 策略取别名：`greedy`、`d-choices`、`rga`、`dchoices` 都合法
- 写分配模式：`lpa-affine`、`dynamic-cwdp`、`fixed-lpa`（别名）
- plane 分配：仅 `PAGE_LEVEL` 或 `CWDP`
- 预条件模式：仅 `capacity-fill` 或 `trace-cover`

### 8.2 Timing（时序参数）

**CLI 方式**（仅独立模拟器支持 full timing 配置）：

```json
{
  "timing": {
    "technology": "mlc",
    "t_r_lsb": 75000,
    "t_r_csb": 100000,
    "t_r_msb": 150000,
    "t_prog_lsb": 750000,
    "t_prog_csb": 1000000,
    "t_prog_msb": 1500000,
    "t_bers": 3800000
  }
}
```

**事件驱动引擎**的时序常量在 `flash_sim/common.py:168-178` 硬编码：
```python
T_READ_LSB = 5_000    # 来自 TimingConfig.t_r_lsb
T_PROG     = 250_000  # 来自 TimingConfig.t_prog_lsb
T_BERS     = 10_000_000
T_SEARCH   = 200_000
T_COMPUTE  = 500_000
```

修改这些常量需要直接编辑 `common.py` 或在 `TimingConfig` 类中改默认值。

### 8.3 ONFI 通道参数

在 JSON 的 `onfi` 段设置。独立模拟器和事件驱动引擎都会读取这些参数；
事件引擎会把它们传入 PHY 的 ONFI command/data-in/data-out 时序模型：

```json
{
  "onfi": {
    "channel_width_bytes": 8,
    "t_rc": 6,
    "t_dbsy": 500,
    "t_wc": 25
  }
}
```

未提供 `onfi` 段时，事件引擎使用 `OnfiTimingConfig` 的默认值。

---

## 9. Trace 格式

### 9.1 独立模拟器 trace

```json
[
  {"type": "read", "lba": 1033},
  {"type": "write", "lba": 512},
  {"type": "erase", "lba": 0},
  {"type": "search", "lba": 0, "wl_count": 16},
  {"type": "compute", "lba": 0, "block_count": 4, "layer": 0}
]
```

### 9.2 事件驱动引擎 trace

每个命令包含 `time`、`start_lha`、`size`（单位：64 B sectors）：

```json
[
  {"type": "write", "time": 0, "start_lha": 0, "size": 64},
  {"type": "read", "time": 1000000, "start_lha": 0, "size": 64},
  {"type": "static_write", "time": 2000000, "start_lha": 12582912, "size": 8},
  {"type": "search", "time": 3000000, "start_lha": 12582912, "size": 8},
  {"type": "compute", "time": 4000000, "start_lha": 12582912, "size": 8, "selected_wl": 7}
]
```

示例中的 `12582912` 是默认 compact event-runtime geometry 的 static-area
起始 LHA。修改结构 geometry 后应以当前运行时的 `STATIC_BASE_LHA` 为准。

| 字段 | 说明 |
|------|------|
| `type` | `read` / `write` / `static_write` / `search` / `compute` |
| `time` | 到达时间（ns） |
| `start_lha` | 起始逻辑扇区地址（1 LHA = 64 B） |
| `size` | 扇区数 |
| `invalidate` | 可选，`1` 表示写时先作废 |
| `stream_id` | 可选，流 ID（默认 0） |
| `bitmap` | 可选，扇区级有效位图（search/compute） |
| `wl_bitmap` | 可选，SEARCH/COMPUTE 输入位图 |
| `data_address` / `data_size` | 可选，SEARCH/COMPUTE 输入数据地址和大小 |
| `selected_wl` | COMPUTE 必填，整数且满足 `0 <= selected_wl < wl_per_string` |

SEARCH、COMPUTE 和 STATIC_WRITE 必须完全位于 static area。一个 static LHA
对应一个 `(block, SL, SSL)` 操作单元，所以 `size` 表示 SSL 粒度的 transaction
数量；COMPUTE 不会把 `size` 重新解释成 SL 数。

### 9.3 SEARCH / COMPUTE 时序模型

事件引擎只模拟请求校验、wave 调度、固定 array delay 和 ONFI 传输，不计算
实际匹配向量、GEMV 数值、模拟电流或 ADC code。

每个 SEARCH die wave 使用一个 source request，每个 plane 最多选择一个 SSL。
不同 die 可以选择不同 request。输入 keyword 只传一次，plane-local BL 匹配结果
按 concat 计入输出量，而不是 OR：

```text
search_input_bytes = ceil(wl_per_string * search_input_bits_per_wl / 8)
search_output_bytes = participating_plane_count
                    * ceil(bl_per_plane * search_match_bits_per_bl / 8)
```

默认配置下，每个 SEARCH wave 输入 16 B，每个参与 plane 输出 32 KiB。

每个 COMPUTE die wave 要求 source request 和 `selected_wl` 相同。每个 plane
最多选择 `compute_max_parallel_sl` 个活动 SL，并且同一个 `(block, SL)` 中最多
选择一个 SSL；同 block 的不同 SL 可以并行，不同 die 可以独立选择 request/WL：

```text
compute_input_bytes = ceil(active_transaction_count
                         * compute_input_bits_per_sl / 8)
compute_output_bytes = participating_plane_count
                     * ceil(bl_per_plane * compute_accumulator_bits / 8)
```

默认 262,144 BL、8-bit ADC/累加结果时，每个参与 plane 的 COMPUTE 输出为
256 KiB。超出一个 wave 容量或产生 SL/请求冲突的 transaction 会留在队列中；
后续每个 wave 都会重新经历 command、data-in、固定 `T_SEARCH`/`T_COMPUTE`
和 data-out。ONFI data-in/data-out 可以被更高优先级 command 抢占并按剩余时长恢复。

可用下面的最小配置覆盖事件引擎 CIM/ONFI 参数：

```json
{
  "onfi": {"channel_width_bytes": 16},
  "geometry": {
    "wl_per_string": 64,
    "bl_per_plane": 131072,
    "search_input_bits_per_wl": 2,
    "search_match_bits_per_bl": 2,
    "compute_input_bits_per_sl": 4,
    "compute_accumulator_bits": 4,
    "compute_max_parallel_sl": 32
  }
}
```

<!-- 更完整的电路语义、并行规则和公式见 `docs/cim-cam.md`； -->
可运行的并行 trace见 `test_case/cim_parallel/`。

### 9.4 预条件 trace

事件驱动引擎支持在正式 trace 前自动生成预条件写，自动填充一定比例的 LBA 范围。由 `RuntimeConfig.precondition_fill_ratio` 触发，生成逻辑见 `engine.py:101-248`（`_generate_precondition_from_trace`）。

---

## 10. Validation 验证工具

### 10.1 Profile 定义

`validation/mqsim_flash/run_validation.py` 的 Profile 类定义了与 MQSim 对标所需的所有参数：

| Profile | 用途 | page_no_per_block | blocks_per_plane |
|---------|------|-------------------|------------------|
| `flashsim-event-small` | 快速正确性验证（small64） | 8 | 64 |
| `flashsim-event` | 现代几何 | 512 | 2048 |
| `flashsim-event-finite-cmt` | 有限 CMT 验证 | 512 | 2048 |
| `fast18-paper` | FAST'18 论文复现 | 256 | 2048 |

### 10.2 运行验证

当前最终 read 对齐结果位于
`validation/mqsim_flash/results/read_mapping_aligned/flashsim-event-small-finite-cmt-aligned/exchange_pressure_20k_mapping_aligned/`。
其中 `summary.json` 是机器可读摘要，`analysis.md` 是详细分析，两个
`*_per_req.csv` 分别是 Flash-Sim 与 MQSim 的正式读取阶段请求延迟。
完整对照表和复现口径见 `validation/mqsim_flash/report.md`。当前 read 已对齐；
write 仅对齐了 PCIe/NAND 阶段公式，MQSim host completion 尚未通过端到端验证。

```bash
# 从仓库根目录
PY=/path/to/.venv/bin/python

# 默认 write_stream 验证
$PY validation/mqsim_flash/run_validation.py

# 指定 profile 和 case
$PY validation/mqsim_flash/run_validation.py --profile flashsim-event-small
$PY validation/mqsim_flash/run_validation.py --case gc_pressure --gc-rounds 4

# 跑完整测试矩阵
$PY validation/mqsim_flash/run_test_matrix_latest.py

# 只重渲已有结果（不重新运行）
$PY validation/mqsim_flash/run_test_matrix_latest.py --rerender 20260705_222159
```

### 10.3 验证 Case

| Case | 说明 |
|------|------|
| `write_stream` | 连续全页写（默认） |
| `flush_then_read` | 全页写 → 大间隔 → 读回 |
| `rich_aligned` | 混合负载（含 pre-setup） |
| `parallel_cwdp` | 通道/die/plane 边界测试 |
| `overwrite_mapping` | 同一地址反复写 |
| `gc_pressure` | GC 压力测试（可指定轮数） |
| `wear_leveling` | 静态磨损均衡测试 |

---

## 11. 快速参考

### 场景：想跑一个自定义 geometry 的引擎模拟

```bash
# 1. 准备 trace（事件引擎格式）
cat > my_trace.json << 'EOF'
[
  {"type": "write", "time": 0, "start_lha": 0, "size": 64},
  {"type": "write", "time": 100000, "start_lha": 64, "size": 64},
  {"type": "read", "time": 5000000, "start_lha": 0, "size": 64}
]
EOF

# 2. 准备 runtime 配置
cat > my_config.json << 'EOF'
{"runtime": {"cache_bypass": true, "gc_low_watermark": 3}}
EOF

# 3. 设环境变量跑（256 blocks/plane）
FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=256 \
  python -m flash_sim.cli run-engine my_trace.json -c my_config.json
```

### 场景：想跑独立模拟器对比不同 geometry

```bash
# 128 layers/block 的配置
cat > geo_a.json << 'EOF'
{
  "timing": {"technology": "slc", "t_bers": 10000000},
  "geometry": {"layers_per_block": 128, "sub_blocks_per_block": 4, "blocks_per_plane": 1024}
}
EOF

# 64 layers/block 的配置
cat > geo_b.json << 'EOF'
{
  "timing": {"technology": "slc", "t_bers": 10000000},
  "geometry": {"layers_per_block": 64, "sub_blocks_per_block": 8, "blocks_per_plane": 1024}
}
EOF

flash-sim run trace.json -c geo_a.json --summary
flash-sim run trace.json -c geo_b.json --summary
```

### 场景：Python API 快速测试

```python
from flash_sim import FlashSimulator, FlashConfig, FlashGeometry, parse_trace

# 自定义 geometry
geo = FlashGeometry(
    channel_no=8, chip_per_channel=4,
    dies=4, planes_per_die=4, blocks_per_plane=64,
    layers_per_block=1, sl_per_block=2, ssl_per_sl=4,
)
config = FlashConfig(geometry=geo)
sim = FlashSimulator(config)

# 执行命令
trace = [
    {"type": "read", "lba": 0},
    {"type": "write", "lba": 100},
    {"type": "search", "lba": 0, "wl_count": 16},
]
results = sim.run_trace(trace)
for r in results:
    print(f"{r['command']} LBA={r['lba']}: {r['latency_ns']} ns -> {r['physical_address']}")
```

---

## 12. 常见问题

**Q: 事件引擎的 geometry 明明在 JSON 里写了，为什么结构大小没变？**

A: 事件引擎的结构 geometry 来自 `FLASHSIM_EVENT_RUNTIME_*` 环境变量。
配置 JSON 中的 channel/chip/die/plane/block/SL/SSL 数量不会重建事件引擎数组；
但 `wl_per_string`、`bl_per_plane`、SEARCH/COMPUTE 位宽和
`compute_max_parallel_sl` 会生效。验证脚本中结构 geometry 通过
`flashsim_event_runtime_env(profile)` 转为环境变量传递给子进程。

**Q: 修改了 `config.py` 的默认值，为什么事件引擎的行为没变？**

A: 事件引擎使用的全局 geometry 在 `common.py:110`（`geometry = make_event_runtime_geometry()`）在模块加载时创建。`make_event_runtime_geometry()` 读 `FLASHSIM_EVENT_RUNTIME_*` 环境变量，不关心 `DEFAULT_*` 常量——但这些常量的确是默认值来源。修改后需清除 Python 缓存或重启进程。

**Q: COMPUTE trace 为什么报缺少 `selected_wl`？**

A: 事件引擎的 COMPUTE 命令必须明确选择 WL。请加入整数
`"selected_wl": N`，并确保 `0 <= N < wl_per_string`。该字段只保存在 source
request 上，派生 transaction 通过 `source_req.selected_wl` 使用它。

**Q: 怎么知道当前 geometry 有多少数据页？**

A: 用 `flash-sim info` 查看，或使用 Python：
```python
from flash_sim.config import FlashGeometry
g = FlashGeometry()
print(f"数据页: {g.total_pages}")
print(f"数据容量: {g.total_pages * 16 * 64 / 1024**3:.1f} GiB")
```
