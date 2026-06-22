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

## FTL
1. 测试现有的垃圾回收、磨损均衡等是否valid（需要生成足量写操作）
2. 测试barrier功能是否正常
3. 新增TSU的多种调度算法（FILN等）

## PHY
1. 修改延时评估方式，使得不同地址跳跃对应的阵列操作延时不同
2. 增加功耗评估
