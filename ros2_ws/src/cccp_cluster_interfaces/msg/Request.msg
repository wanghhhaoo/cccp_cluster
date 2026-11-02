# /cluster/request：网关 → 集群主节点（设计 0.3 第 4.1 节、附录 C.7）。
# 必须指定 target_leader，只有它本身处理这条请求：不是集群主节点了就立即回复 NOT_LEADER，
# 不再转发，所以一个请求最多被处理一次。
# 所有网关一直有发布端；只有集群主节点在当选时创建订阅端。
# QoS：RELIABLE + KEEP_LAST(32) + VOLATILE。

uint8 OP_CREATE=1
uint8 OP_DESTROY=2
uint8 OP_QUERY=3

uint8 ORIGIN_MANUAL=1
uint8 ORIGIN_CHAIN=2
uint8 ORIGIN_SCHEDULE=3
uint8 ORIGIN_EVENT=4

# ---- 请求方 ----
uint8 origin_node
uint64 origin_incarnation         # 请求方重启后 request_id 从头开始，靠它区分
uint32 request_id

# ---- 目标 ----
uint8 target_leader
uint32 term                       # 请求方当前认为的任期，只用于日志

uint8 op

# ---- CREATE ----
uint8 origin
uint8 chain_depth
string idempotency_key
string<=64 name
string spec_json                  # 网关已经校验并规范化过

# ---- DESTROY / QUERY ----
uint8[16] task_id
string<=64 reason                 # DESTROY 的原因（整理 0.3 时补充）
