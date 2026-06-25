# TODO List

## Important Major Change!
修改CIM和CAM的ISA与切分方式，将切分粒度完全改为X, Y, Z方向上的一条cell

## Host
1. 检查NVMe协议的多队列并发实现有没有问题。目前的trace超过8条req之后无法执行可能是由于Host侧SQ队列实现的问题导致后面的REQ都没被发送给Device

## HIL - Data Cache
1. 检查当前的write_flush逻辑。正确的write_flush行为应当如下：
    1. write_flush在写请求申请的new_line数目大于free_line数目的时候触发
    2. 当write_flush正在执行时，data_cache中的所有写数据缓存全部被打包发给TSU进行调度。每当一个line对应的写数据全部被写进阵列（即对应的write_req已经全部在PHY中经历了PHY_CHIP_WRITE_COMPLETE事件，即可将这条line释放为free的状态并清空其中数据。当所有发起write_flush时提交的line都完成阵列写操作之后，write_flush结束
    3. 在write_flush执行期间，read请求可以对data_cache中仍然处于ready状态的line进行访问，即使其中已经有部分数据被写入了阵列
    4. 给Host侧的back pressure: write_flush发起时，通过PCIe接口给Host发一个指令（需要在PCIe模块中进行一次指令传输以表现其对端口资源的占用），提示Host端目前data_cache已经满了。wirte_flush中的所有line全部写入阵列之后，再给Host发一个信号，告知主机write_flush已经完成。write_flush写回阵列期间，Host不得向HIL提交写请求，但在SQ中如果WRITE_REQ后有其它类型的req，需要越过WRITE_REQ将其正常发送
## 已完成 ✅
- [x] **Host SQ 多队列死锁** — `fix/host-sq-flow-deadlock` 分支已修复并合入 PR
- [x] **PHY 功耗评估** — per-stage 能耗模型（P_ARRAY/P_IF/P_SEARCH/P_COMPUTE）已在 PHY.py 实现，JSON/CSV 报告输出 energy_uj/persistence_energy_uj
- [x] **GC/WL correctness 与压力验证** — trigger、victim、relocation、映射、bookkeeping、overwrite、waiting queue、报告守恒和 Static WL 均已覆盖；完整 pytest 与 16-trace matrix 通过

---

## Bug 修复（历史遗留）

### 测试失败
1. **test_multi_write cache 容量检查** — `_count_new_ready_lines` 按 sector 计数，应改为按 page(line) 计数。单次 WRITE size=130 → 130 个 "line" > 64 上限
2. **test_read_error_cases** — req-0001 READ lha=106688 永不完成，需定位根因
3. **test_search_compute** — SEARCH 全部路由到 plane=0 串行执行，仿真结束前未完成

### 工具链
4. **CSV 缺少 status 列** — ERROR 请求在 CSV 中不可见
5. **CSV "是否cache命中" 列语义错误** — 回退到 CMT hit 判断，而非 data cache hit
6. **main.py INPUT_JSON 硬编码** — 应改为 argparse 接受命令行参数

---

## HIL — Data Cache

1. **write_flush 逻辑完善**（当前实现与规范有差异）：
   1. write_flush 应在申请的 new_line > free_line 时触发 ✅ 已实现
   2. 当前实现：flush 提交后**立即**释放 cache line（不等 NAND 写入完成）。规范要求：等所有 flush 的 line 完成 PHY_CHIP_WRITE_COMPLETE 后才释放
   3. 当前实现：无 PCIe 通知。规范要求：write_flush 发起时通过 PCIe 给 Host 发指令（占用端口资源）
   4. flush 期间 read 能否访问 ready line ✅ 已支持（cache_pressure_drain_mode 只翻转优先级，不阻塞 read）

---

## FTL

1. **TSU 多种调度算法** — 当前仅有 Out-of-Order 一种策略（固定优先级 + round-robin），待新增 FILN 等算法

## Deferred：GC/WL 真实性评估

- [ ] **真实性缺口评估（非当前阻塞）**
  - 记录 metadata 不参与 GC 的抽象边界
  - 记录当前 greedy victim policy 与真实 SSD policy 的差距
  - 记录当前 PHY 时序模型对 GC 成本评估的简化假设
  - 记录掉电恢复与 metadata 持久化语义暂未覆盖的范围

---

## PHY

1. **地址跳跃延时模型** — 当前所有读写操作用固定常量（T_READ_LSB=5000ns, T_PROG=250000ns），不区分地址间跳跃距离。chip.py 已有 page_type 感知（LSB/CSB/MSB），但 PHY.py 事件驱动层未接入。需让延时随地址跳跃距离变化
