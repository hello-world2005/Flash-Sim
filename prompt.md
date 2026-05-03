# 添加Data Cache
帮我完成一个由HIL管理的Data Cache，要求实现如下功能：
1. 用cache line进行管理，每个cache line中可以存储`cache_line_size`的数据，`cache_line_size`默认取64B. cache的容量上限记为DATA_CACHE_CAP，必须为`cache_line_size`的整数倍。
2. Data Cache作为user write req和user static write req的数据缓存区，在用户的写请求到来时，若写请求access的地址是Data Cache里面没有的，且Data Cache能够容纳到来的写数据，则将到来的数据切分为若干cache line，存放到Data cache中；若写请求access了新的地址且Data cache无法容纳这个写请求的数据，则触发write_flush；若写请求access的地址在cache line中有存储，则直接更新cache line内的数据
3. 当触发write_flush时，调用FIL.Address_mapping_unit生成transactions并提交给TSU，将所有的cache line中的数据全部写入flash array中，并将所有的cache line清空
4. 当HIL收到读请求的时候，首先查看data cache里面有没有对应的数据，如果有，则直接用这个数据响应读请求即可