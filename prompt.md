# Openspec init change
当前代码库已经完成了Flash-sim仿真器的基本框架，现在我需要你propose一个change，来完成对仿真器当前特性的描述。
## 整体特性
从整体上来说，当前的仿真器是事件驱动的，每个需要考虑延时的事件都会在event priority queue中register一个新的event，这个event的时间代表动作结束的时间。仿真器的engine不断从event queue队首取出事件并执行对应动作，如此推动仿真，直至事件队列为空。
## 文件说明
下面是各个文件的大致说明：
1. `main.py`：这是运行仿真器的top文件，里面进行了库导入目录的定义以及log文件的输出、仿真所需文件的导入（在开头）
2. `engine.py`：这个文件实现了仿真器的事件驱动引擎。开始仿真时，会由engine完成模块构建和验证，另外在初始化的时候调用了self.device.ftl.block_manager.preconditioning函数，来完成初始状态的赋值
3. `Host.py`：这个文件实现了对Host端行为的仿真。目前Host端的行为只是将req直接发送给PCIe接口，暂时没有实现host memory。SQ, CQ队列应该有对应的数据结构但是不是基于host memory实现的，请帮我检查
4. `Device.py`：这个是SSD Device的顶层，实现了device中各个模块的调用和连接
5. `HIL.py`：SSD的接口层，实现了以下功能：
    - 接收PCIe message，将host发来的请求进行segmentation
    - 访问挂载的data_cache，如果有cache hit的transaction直接用cache中的数据返回
    - 将未命中的transaction发给amu进行地址映射
6. `FTL.py`：SSD的核心模块，实现了以下内容：
    1. AMU子模块：
        - 进行地址映射操作，管理cmt, gdt, gmt
        - 如果需要读写mapping page，生成对应的transaction
        - 注意：FLASH中的更新操作采用异地更新，会在一个新的physical page address(ppa)写上同一个logical page address(lpa)中的新内容，同时更改mapping info
    2. TSU子模块：
        - 管理各个die（或者是chip？有点忘了，检查一下）的transaction队列
        - 采用轮询的方式，挑选合适的transaction进行issue
        - 将issue的指令打包发给PHY电路
    3. Block Manager
        - 负责管理block的状态信息
    4. GC_Unit
        - 进行垃圾回收的单元
        - 当free block数量低于阈值后会触发垃圾回收：选择一个victim block将其中的valid page转移到free block中，invalid page不管，然后将整个victim block全部擦除
7. `PHY.py`：SSD的阵列控制电路模块，完成对阵列的读、写、擦、CIM、CAM操作的模拟，并调用相应的参数计算延时
8. `pcie_link.py`：这个文件建模了PCIe传输的行为
9. `common.py`：这个文件中是一些公用的类、常量定义
除了这些主要的部分之外，另外还有例如parser.py，util.py这类完成辅助功能的文件，请根据调用方式和文件内容自行判断功能，并在spec中酌情描述