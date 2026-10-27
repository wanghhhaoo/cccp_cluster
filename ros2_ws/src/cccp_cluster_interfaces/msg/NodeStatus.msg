# /cluster/node_status：每个节点按 1 Hz 发布，所有节点都订阅。
# 一条消息同时承担：资源上报、存活心跳、链路测量（丢包看 seq 缺口，RTT 看 links 里的回显）、
# 任务对账（running_tasks）。
# QoS：BEST_EFFORT + KEEP_LAST(1) + VOLATILE，见开发规范 3.3。

# 接口协议版本。cccp_* 接口包有不兼容修改时 +1。
# 集群主节点不向 protocol_version 与自己不同的节点派发任务。
uint16 PROTOCOL_VERSION=1
uint16 protocol_version

# ---- 身份与序号 ----
string node_name                  # 部署配置中的 NODE_NAME，命名空间由它推导
uint64 incarnation                # 本进程启动时的 Unix 毫秒；变化说明节点重启过
uint64 seq                        # 每次发布 +1，从 1 开始；incarnation 变化后重新计数
builtin_interfaces/Time stamp     # 发送方本地时间，只用于显示，不得与其他节点的时间相减

# ---- 本节点看到的集群状态 ----
string leader_name                # 本节点认为的集群主节点；未知时为空
uint64 term                       # 本节点已知的最大 Raft 任期
bool is_leader

# ---- 就绪状态 ----
bool inference_ready              # 推理服务整体可用
string[] algorithms_ready         # 已就绪的算法 ID，如 ["PKCI", "PKCT"]

# ---- 静态硬件（很少变化，每条都带，省去单独的查询接口）----
string cpu_model
uint16 cpu_cores
uint32 mem_total_mb
string gpu_model                  # 无 GPU 时为空
uint32 gpu_mem_total_mb           # 独立显存总量；Jetson 等与系统共享内存的 GPU 填 0

# ---- 动态资源；无法获取时填 -1 ----
float32 cpu_usage                 # 0~100
float32 mem_usage                 # 0~100
float32 gpu_usage                 # 0~100
float32 gpu_mem_usage             # 0~100
float32 battery                   # 0~1；非无人机节点填 -1

# ---- 任务 ----
RunningTask[] running_tasks       # 本节点当前正在执行的全部任务，集群主节点据此每秒对账

# ---- 链路：本节点对每个已知邻居的测量 ----
LinkQuality[] links
