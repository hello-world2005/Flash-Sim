# Flash-Sim

A cycle-accurate 3D NAND Flash simulator that models storage operations (read, write, erase) and in-memory compute operations (search, compute) with configurable flash technologies (SLC/MLC/TLC).

## Features

### Flash Chip Timing Model

- **SLC/MLC/TLC** technology support with page-type-aware latencies (LSB/CSB/MSB)
- Configurable read, program, and erase timing parameters
- 3D NAND geometry modeling: Die → Plane → Block → Layer → Sub-block → Page

### Supported Operations

| Operation | Description | Typical Latency |
|-----------|-------------|-----------------|
| READ | Page read with page-type-aware latency | ~75 μs (LSB) |
| WRITE | Page program with page-type-aware latency | ~750 μs (LSB) |
| ERASE | Block erase | ~3.8 ms |
| SEARCH | Parallel word-line activation (CAM-style) | ~75 μs + WL overhead |
| COMPUTE | Multi-block MAC accumulation | ~75 μs + block overhead |

### Flash Translation Layer (FTL)

- LBA to physical address mapping
- Page status tracking (FREE / VALID / INVALID)
- Block-level PE (Program/Erase) cycle counting and wear statistics

### CLI

```bash
# Show flash geometry info
flash-sim info

# Run a trace file
flash-sim run examples/basic_trace.json --config examples/config.json --summary

# LBA to physical address lookup
flash-sim lba 1024

# Interactive mode
flash-sim interactive

# Random read benchmark
flash-sim bench --ops 1000
```

## Quick Start

### Installation

```bash
pip install -e .
```

### Python API

```python
from flash_sim import FlashSimulator, FlashConfig, parse_trace

sim = FlashSimulator(FlashConfig())
results = sim.run_trace(parse_trace([
    {"type": "read", "lba": 100},
    {"type": "write", "lba": 200},
    {"type": "search", "lba": 0, "wl_count": 16},
    {"type": "compute", "lba": 0, "block_count": 4},
]))
print(f"Total latency: {sim.get_total_latency(results)} ns")
```

### Trace Format

JSON array of commands:

```json
[
  {"type": "read", "lba": 1033},
  {"type": "write", "lba": 512},
  {"type": "erase", "lba": 0},
  {"type": "search", "lba": 0, "wl_count": 16},
  {"type": "compute", "lba": 0, "block_count": 4, "layer": 0}
]
```

### Configuration

```json
{
  "timing": {
    "technology": "slc",
    "t_r_lsb": 75000,
    "t_r_csb": 100000,
    "t_r_msb": 150000,
    "t_prog_lsb": 750000,
    "t_prog_csb": 1000000,
    "t_prog_msb": 1500000,
    "t_bers": 3800000
  },
  "parallel": {
    "max_parallel_wl": 64,
    "max_parallel_blocks": 8
  },
  "geometry": {
    "layers_per_block": 128,
    "sub_blocks_per_block": 8,
    "blocks_per_plane": 1024,
    "planes_per_die": 2,
    "dies": 1
  }
}
```

`FlashConfig()` and `FlashGeometry()` use the same default geometry as the JSON
example above: `layers_per_block=128`, `sub_blocks_per_block=4`,
`blocks_per_plane=1024`, `planes_per_die=4`, and `dies=4`.

## Architecture

```
flash_sim/
├── config.py      # Configuration, geometry, FTL, address mapping
├── chip.py        # Flash chip timing model (latency calculations)
├── simulator.py   # Main simulator engine (command execution)
├── parser.py      # Trace parsing and result formatting
└── cli.py         # Command-line interface
```

**Execution flow:**

```
JSON Trace → Parse & Validate → LBA → Physical Address (FTL) → Latency Calculation (FlashChip) → Results
```

## Comparison with MQSim

[MQSim](https://github.com/CMU-SAFARI/MQSim) (FAST'18) is a full-stack multi-queue SSD simulator with ~19,300 lines of C++. The table below summarizes what Flash-Sim currently covers and what is missing compared to MQSim.

### What Flash-Sim Has

| Component | Status |
|-----------|--------|
| Flash chip timing model (SLC/MLC/TLC, LSB/CSB/MSB latencies) | Implemented |
| 3D NAND geometry (die/plane/block/layer/sub-block hierarchy) | Implemented |
| Basic FTL with LBA-to-physical mapping | Implemented |
| Page/block status tracking and PE cycle counting | Implemented |
| In-memory compute: SEARCH (parallel WL) and COMPUTE (parallel block MAC) | Implemented (MQSim does not have this) |

### What Flash-Sim Is Missing

| Category | Missing Features | MQSim Implementation |
|----------|-----------------|---------------------|
| **Host Interface** | NVMe multi-queue protocol, SATA/NCQ, PCIe link modeling | Full NVMe SQ/CQ with 4 priority levels, SATA NCQ |
| **Multi-Flow I/O** | Multi-stream workloads, per-flow resource partitioning | Up to 8 concurrent flows with independent queues |
| **Transaction Scheduling** | I/O scheduling, out-of-order execution, priority scheduling | TSU with OUT_OF_ORDER, PRIORITY_OUT_OF_ORDER, FLIN policies |
| **Channel Parallelism** | ONFI channel modeling, multi-channel pipeline, bus contention | ONFI NVDDR2 with channel/die interleaving |
| **Die/Plane Parallelism** | Parallel operation execution, multi-plane commands | Multi-plane and die-interleaved execution |
| **DRAM Cache** | Write-back buffer, DRAM timing, per-flow cache partitioning | Configurable cache with tRCD/tCL/tRP modeling |
| **Garbage Collection** | Block selection (greedy/RGA/FIFO), preemptible GC, auto-trigger | Full GC with hard/soft thresholds and copyback |
| **Wear Leveling** | Dynamic/static wear leveling, hot/cold data separation | PE-count-based block selection with hot/cold classification |
| **Address Mapping** | Page-level/hybrid mapping, 24 plane allocation schemes, CMT | Cached mapping table with miss-triggered flash reads |
| **Command Suspension** | Program/erase suspend for urgent requests | Full suspend/resume support |
| **Workload Generation** | Synthetic workloads, arrival time modeling, address distributions | Random/sequential/hotcold distributions, bandwidth/QD modes |
| **Statistics** | IOPS, bandwidth, per-channel utilization, per-flow metrics | Comprehensive XML-based reporting with epoch-level stats |
| **Steady-State Simulation** | Preconditioning to reach steady state before measurement | Configurable preconditioning phase |
| **Event-Driven Engine** | Discrete-event simulation with global clock | Full SimEngine with event scheduling |

### Summary

Flash-Sim currently functions as a **flash chip timing calculator** with basic FTL support and unique in-memory compute capabilities (SEARCH/COMPUTE). To become a full SSD system simulator comparable to MQSim, it would need the SSD controller stack: host interface, I/O scheduling, channel-level parallelism, DRAM caching, garbage collection, and wear leveling.

## Running Tests

```bash
pytest test_script/
```
