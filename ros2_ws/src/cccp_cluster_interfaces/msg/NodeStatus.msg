# /cluster/node_status：心跳。每个节点按 1 Hz 发布，所有节点都订阅（设计 0.3 第 3.2 节、附录 C.2）。
# 每条都自包含：收到任意一条，就知道这个节点此刻在不在、认谁当集群主节点、在跑哪些任务（摘要）。
# 必须装进一个链路层帧（约 1.4 KB）：BEST_EFFORT 下一条消息拆成 n 帧，丢失率就从 p 变成 1-(1-p)^n。
# 完整的任务信息在 TaskList 里，只在变化时可靠发布。
# QoS：BEST_EFFORT + KEEP_LAST(1) + VOLATILE。

# 接口协议版本。cccp_* 接口包有不兼容修改时 +1；12 个节点必须一起升级。
# 3：心跳拆成 NodeStatus + TaskList；节点编号代替名字；跨节点的 Service 改为 topic；执行代次 (epoch, assign_term)。
# 4：algorithms_ready 增加 bit2～bit4（批处理任务 FASTLIVO、HETEROMAP、RSSMAP），字段类型不变。
uint8 PROTOCOL_VERSION=4
uint8 protocol_version

# ---- 身份与序号 ----
uint8 node_id                     # 部署时配置的节点编号，1~254；名字见 TaskList.node_name
uint64 incarnation                # 控制进程启动时的 Unix 毫秒；变化说明节点重启过
uint32 seq                        # 每次发布 +1，从 1 开始；incarnation 变化后重新计数

# ---- 选主（每条都带）：每个节点都广播自己认可的集群主节点 ----
uint8 leader_id                   # 0 表示还没有跟随任何集群主节点
uint32 term                       # 本节点已知的最大任期
bool is_leader                    # true 即本节点声明自己是集群主节点；同时也是它的续约
uint8 visible_peers               # 本节点判定在线的对端数，用于选主候选排序

# ---- 就绪（每条都带）----
bool executor_alive               # false：本机执行器超过 executor_timeout 没有出声；tasks 是最后一次已知的值
bool inference_ready
uint8 algorithms_ready            # 位掩码：bit0 = PKCI，bit1 = PKCT，bit2 = FASTLIVO，bit3 = HETEROMAP，bit4 = RSSMAP

# ---- 任务摘要（每条都带）----
TaskBrief[<=8] tasks
uint32 tasks_version              # 与 TaskList.tasks_version 对应；比缓存的 TaskList 新说明新版本还在路上

# ---- 慢变部分：每 slow_fields_every 条附带一次，其余时候为空数组 ----
NodeLoad[<=1] load
LinkQuality[<=15] links
