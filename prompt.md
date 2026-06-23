# resource contention实验修改
当前的request_resource_contention_experiments.py脚本中对于read-impact的实验较为简略，请帮我修改一下这个实验：
1. 对照组与实验组同时包含CMT初始化时缓存的所有read请求，并且issue的时间、顺序完全相同，每个read req的访问均target一个page
2. 实验组扫描插入compute操作的两个变量：
    1. 插入比例：定义`ratio = num_compute_req/num_read_req`，扫描ratio = 0.1 0.2 0.4 0.8时的结果。该组中每个插入的COMPUTE req的size均为128
    2. req size：固定插入比例为0.2，扫描COMPUTE req的size为8 32 128 512
3. 统计方式：统计每个实验组的参数条件下read的平均延时
4. 绘图要求：绘制柱状图，分为三组绘制在同一个chart中，不同组别沿横轴排列，纵轴是normalized latency
    1. 对照组：设置为归一化的基础，保证其值为1。该组用默认的蓝色绘制
    2. 插入比例扫描组：横轴为ratio，纵轴为归一化后的latency。该组用橙色绘制
    3. req size扫描组：横轴为size，纵轴为归一化后的latency。该组用紫色绘制
    4. 组内的bar间隔小一些，组间的bar之间留多一点空隙
    5. 在对应组别的横轴下方用文字标出各组名称
    6. 数据标签保留两位小数