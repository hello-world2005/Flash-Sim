## 1. Baseline Inventory

- [x] 1.1 审阅事件驱动主路径相关模块，确认 Engine、Host、PCIe、HIL、FTL、PHY 的当前职责与交互边界
- [x] 1.2 识别并记录当前实现中的简化点、限制和与理想模型的偏差，避免在规格中误写成已承诺能力

## 2. Capability Authoring

- [x] 2.1 编写 `proposal.md`，明确这次 change 的目标是描述当前仿真器特性而非新增功能
- [x] 2.2 编写 `design.md`，说明为什么要按现状建基线，以及为什么要把限制作为显式设计决策
- [x] 2.3 为运行时、请求路径、FTL/介质模型和辅助工具分别编写 capability spec

## 3. Review And Adoption

- [x] 3.1 运行 OpenSpec 状态检查，确认 proposal、design、specs 和 tasks 已全部生成并达到 apply-ready
- [ ] 3.2 与仿真器维护者一起审阅这份基线，决定后续变更应基于哪些 capability 做增量修改
