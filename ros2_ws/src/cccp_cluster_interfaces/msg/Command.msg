# /cluster/command：集群主节点 → 执行器的命令（设计 0.3 第 2.5 节、附录 C.5）。
# 所有执行器一直订阅；只有集群主节点在当选时创建发布端。一条命令可以同时发给多个节点，
# 执行器收到不是发给自己的就忽略。命令都是幂等的，丢了由对账重发。
# 执行器的检查顺序见设计 3.8 节（开发规范 5.4）。
# QoS：RELIABLE + KEEP_LAST(32) + VOLATILE。

uint8 OP_START=1
uint8 OP_STOP=2

uint8 STOP_CANCEL=1               # 任务被销毁：执行器停止后留下墓碑
uint8 STOP_STALE=2                # 旧代次的残留副本，或任务已是其他终态
uint8 STOP_DUPLICATE=3            # 与其他任务的幂等键重复
uint8 STOP_UNASSIGNED=4           # 本节点不在当前代次的分工里

# ---- 发送方与栅栏 ----
uint8 leader_id                   # 发送方；目前只用于日志
uint32 term
uint32 request_id                 # 在 (leader_id, term) 内递增；执行器在 ExecEvent 里原样带回

# ---- 目标 ----
uint8[<=15] targets

uint8 op
uint8 stop_reason                 # 只在 OP_STOP 时有意义
string<=64 reason                 # 给人看的原因，只用于日志

# ---- 任务与代次 ----
uint8[16] task_id
uint32 epoch
uint32 assign_term

# ---- 只在 OP_START 时填写 ----
uint8 primary_node
string<=32 primary_name           # 任务主节点的名字：辅助节点按名字找它的命名空间（压缩转发）
uint8[<=15] helper_nodes
string idempotency_key
string<=64 name
string spec_json
