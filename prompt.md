# Block Manager _preconditioning函数功能修改
目前的preconditioning模块只对block_manager中的状态维护表进行了赋值，导致在GC回收遇到preconditioning赋值的valid_page的时候会遇到无法读取lpa的报错。接下来需要进行如下修改：
1. precondition阶段由随机赋值page state改为根据输入的precondition_data.json进行赋值
    1. precondition_data.json文件的结构参考pre_data/precondition_data.json，为一个json列表，每个元素是包含lpa, valid_bitmap, data的字典
    2. _preconditioning函数读取该文件，根据lpa映射到plane的规则，将所有data分给不同的plane，然后根据每个plane中分到的page数量以及给定的每个`block`中`invalid_pag`e与`valid_page`的比例，算出需要的full_block（全为invalid/valid page）数量，计算方式为：假设一个plane中分到了num_page个pagedata，`valid_page/page_per_block = ratio`，则`num_full_block = num_page // (page_per_block * ratio)`。如果算出`num_full_block + GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD >= block_per_plane`，说明这个plane已经写得过满，报overfull错误并停止程序
    3. 在该plane中随机选取`num_full_block`个block，将其中比例为ratio的page赋值为valid，并赋值PHY.py文件中_storage数组里对应的pagedata的lpa, valid_bitmap, data字段
    4. 在剩余的free_block中随机选一个作为write_frontier_block，如果上一步中`num_page % (page_per_block * ratio) = left_page > 0`，则将write_frontier_page设置成`left_page / ratio`，并且在`page_idx < write_frontier_page`的page中随机选取left_page个page，将其对应的page_data赋值
2. 帮我写一个生成precondition_data.json文件的脚本，放在flash_sim目录下。该脚本读取common.py, config.py中关于flash的配置，在合理的lpa范围内随机生成`num_data`个preconditioning阶段需要赋值的数据