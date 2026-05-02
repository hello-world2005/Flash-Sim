# README
支持输入trace, pre_data, 仿真输出所有req的延时

## 待完善功能
__对真实数据的支持和仿真验证__
1. 添加S_DRAM作为write buffer和read data buffer
2. 完善host传入数据的机制（先考虑给host data fetch专门做一个接口，这样可以跟上层联动）
3. verification，测试数据正确性