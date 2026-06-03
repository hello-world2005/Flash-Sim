# GC_Unit模块功能扩展
当前的GC模块中，只完成了垃圾回收功能，没有实现磨损均衡。请你帮我修改这个类，将类名改成GC_WL_Unit，并在其中实现磨损均衡功能。可以参考以下对../MQSim中实现GC_WL_Unit的功能描述：

这版代码，GC_WL_Unit 的 WL 其实分两层：dynamic WL 负责“以后把新写入分配到哪个 free block”，static WL 才会真的像 GC 一样搬页+擦块。整体入口主要在 GC_and_WL_Unit_Page_Level.cpp (line 43) 和 GC_and_WL_Unit_Base.cpp (line 250)。
机制
dynamic WL 不会单独发起一次 WL 操作。它的实现是在 free block pool 上：pool 是 multimap<erase_count, block*>，每次 Get_a_free_block() 都拿擦写次数最小的 free block；而块被擦除回收时，如果开启 dynamic WL，就按当前 Erase_count 放回池子。所以它的“触发条件”其实是“块擦完回池、以后再次分配 free block 时生效”，目的就是优先消耗冷块，摊平擦写。
普通 GC 的触发是按 plane 看 free block pool：当 free_block_pool_size < block_pool_gc_threshold 时进入 Check_gc_required() 选 victim GC_and_WL_Unit_Base.cpp (line 22) GC_and_WL_Unit_Page_Level.cpp (line 43)。这个检查发生在写前沿块写满、需要换新块时，包括用户写、GC 搬迁写、翻译页写 Flash_Block_Manager.cpp (line 20) Flash_Block_Manager.cpp (line 39) Flash_Block_Manager.cpp (line 91)。
GC victim 选择策略都在 GC_and_WL_Unit_Page_Level.cpp (line 53)：GREEDY：选“已写满且安全”的块里 Invalid_page_count 最大的。
RGA：随机抽 log2(block_no_per_plane) 个安全块，再从中选 invalid pages 最多的满块 GC_and_WL_Unit_Page_Level.cpp (line 21)。
RANDOM / RANDOM_P / RANDOM_PP：分别是随机安全块、随机满块、随机满块且 invalid pages 至少达到 rho * pages_per_block。
FIFO：按块成为写前沿的先后顺序选 Flash_Block_Manager_Base.cpp (line 115)。

“安全块”的定义在 GC_and_WL_Unit_Base.cpp (line 222)：不能是 Data_wf/Translation_wf/GC_wf，不能有 ongoing user program，不能已经在做 GC/WL。
注意它没直接排除 ongoing read；如果块上还有用户事务，GC/WL 会先打标记，等最后一个用户事务结束后再真正启动页搬迁 GC_and_WL_Unit_Base.cpp (line 61)。
static WL 的触发点是在任意一次 GC_WL 的 ERASE 完成之后：先把擦好的块回收到 pool，再检查本 plane 是否需要 static WL GC_and_WL_Unit_Base.cpp (line 153)。如果需要，就选本 plane Erase_count 最小的块，也就是“最冷块”，再走一遍和 GC 类似的“锁块 -> 搬有效页 -> 擦原块”的流程 GC_and_WL_Unit_Base.cpp (line 245) Flash_Block_Manager_Base.cpp (line 164)。
搬迁时的并发保护是先锁整块里所有有效页对应的 LPA/MVPN，新的请求会挂到 barrier 后面；等对应 GC write 完成后再解锁并放行 Address_Mapping_Unit_Page_Level.cpp (line 1768) Address_Mapping_Unit_Page_Level.cpp (line 1797)。