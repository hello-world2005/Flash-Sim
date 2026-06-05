# NAND Flash 功耗参考数据

收集日期: 2026-06-05

## 摘要

Flash-Sim 采用 NANDFlashSim (ACM TOS 2016) 的 per-stage 能耗模型。
功耗参数基于 Micron NAND datasheet (2018+, 64Gb+ MLC/TLC, 1.8V VCC)。

## Per-Stage 能耗模型 (NANDFlashSim)

```
E_read   = P_ARRAY × tR      + P_IF × PHY_DATA_OUT_TIME  (+ CLE/ALE, 忽略)
E_prog   = P_IF × PHY_DATA_IN_TIME + P_ARRAY × tPROG     (+ CLE + status, 忽略)
E_erase  = P_ARRAY × tBERS                                (+ CLE + status, 忽略)
E_search = P_SEARCH_ARRAY × tSEARCH  + P_IF × PHY_DATA_OUT_TIME
E_compute= P_COMPUTE_ARRAY × tCOMPUTE + P_IF × PHY_DATA_OUT_TIME
```

### 各阶段含义

| 阶段 | 硬件组件 | 功耗来源 |
|------|---------|---------|
| CLE/ALE | 命令/地址寄存器 (Latch) | 可忽略 (<1%) |
| TIR/TOR | Flash I/O 接口 + 数据寄存器 | P_IF × data_time |
| TON | NAND cell array (读出) | P_ARRAY × tR |
| TIN | NAND cell array (写入) | P_ARRAY × tPROG |
| BER | NAND cell array (擦除) | P_ARRAY × tBERS |

## Flash-Sim 当前参数

来源: Micron NAND datasheet (2018+, 1.8V VCC), 适配 Flash-Sim 的 16KB page

| 参数 | 值 | 来源 |
|------|-----|------|
| VCC | 1.8V | Micron 64Gb+ datasheet |
| P_ARRAY | 45 mW (1.8V × 25mA) | ICC1/ICC2/ICC3 (Array active current) |
| P_IF | 18 mW (1.8V × 10mA) | ICC4R/ICC4W (I/O burst current) |
| PHY_DATA_IN_TIME | 5,000 ns | Flash-Sim common.py |
| PHY_DATA_OUT_TIME | 5,000 ns | Flash-Sim common.py |
| P_SEARCH_ARRAY | 54 mW | 多条WL同时激活, 估算值, 详见 CIM Search 章节 |
| P_COMPUTE_ARRAY | 72 mW | 多block并行激活, 估算值, 详见 CIM Compute 章节 |

## 计算示例 (16KB page, SLC timing)

以 Flash-Sim 默认时序为例:

```
READ (LSB, tR=75μs):
  E = 45mW × 75μs + 18mW × 5μs = 3.375 + 0.09 = 3.465 μJ

READ (MSB, tR=150μs):
  E = 45mW × 150μs + 18mW × 5μs = 6.75 + 0.09 = 6.84 μJ

WRITE (LSB, tPROG=750μs):
  E = 18mW × 5μs + 45mW × 750μs = 0.09 + 33.75 = 33.84 μJ

WRITE (MSB, tPROG=1500μs):
  E = 18mW × 5μs + 45mW × 1500μs = 0.09 + 67.5 = 67.59 μJ

ERASE (tBERS=3.8ms):
  E = 45mW × 3800μs = 171 μJ

SEARCH (tSEARCH=200μs):
  E = 54mW × 200μs + 18mW × 5μs = 10.8 + 0.09 = 10.89 μJ

COMPUTE (tCOMPUTE=500μs):
  E = 72mW × 500μs + 18mW × 5μs = 36.0 + 0.09 = 36.09 μJ
```

## CIM Search 功耗

来源: "An ultra-high-density and energy-efficient content addressable memory design based on 3D-NAND flash"
Peking University, Science China Information Sciences (2023)

| 指标 | 数值 |
|------|------|
| Search 能量 | 0.196 fJ/bit/search |
| 单元结构 | 2 Flash transistor per CAM cell |
| 密度 | 157× SRAM-based CAM |

换算：16KB page (128K bits) 全页并行 ≈ 128K × 0.196 fJ ≈ 25.1 nJ
(与当前 P_SEARCH_ARRAY=54mW 计算的 10.89 μJ 有差异，因为 0.196 fJ/bit 是 cell 级，
10.89 μJ 是 chip 级含外围电路，后续可细化)

## CIM Compute (MAC) 功耗

暂无 Flash NAND 的精确实验数据，当前使用 72mW 估算值。

## 数据来源

1. NANDFlashSim (ACM Trans. Storage, 2016): per-stage 模型框架
2. Micron NAND datasheet (2018+, 64Gb+): VCC=1.8V, ICC array=25mA, ICC I/O=10mA
3. 3D-NAND Flash CAM (Sci. China, 2023): search energy 0.196 fJ/bit
