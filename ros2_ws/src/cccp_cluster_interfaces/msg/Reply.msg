# /cluster/reply：集群主节点 → 网关（设计 0.3 附录 C.8）。
# 每个网关一直订阅，只取 origin_node 是自己的那些。
# QoS：RELIABLE + KEEP_LAST(32) + VOLATILE。

uint8 RES_OK=0
uint8 RES_NO_LEADER=1             # 正在选主，或者新集群主节点还在预热
uint8 RES_NOT_LEADER=2            # 我已经不是集群主节点了
uint8 RES_TIMEOUT=3
uint8 RES_INVALID_INPUT=4
uint8 RES_CHAIN_TOO_DEEP=5
uint8 RES_NO_FEASIBLE_NODE=6
uint8 RES_DISPATCH_FAILED=7
uint8 RES_TASK_FAILED=8           # 任务启动后失败，具体原因见 exec_error
uint8 RES_NOT_FOUND=9
uint8 RES_INVALID_STATE=10

uint8 STATUS_PENDING=0
uint8 STATUS_SCHEDULED=1
uint8 STATUS_RUNNING=2
uint8 STATUS_FINISHED=3
uint8 STATUS_FAILED=4
uint8 STATUS_CANCELLED=5

# ---- 对应哪个请求 ----
uint8 origin_node
uint64 origin_incarnation
uint32 request_id

uint8 leader_id
uint32 term
uint8 result
uint8 exec_error                  # result 为 RES_TASK_FAILED 时，填执行器的错误码（ExecEvent.ERR_*）
string<=128 message

# ---- 任务快照：只要任务已经生成就带上，即使 result 不是 OK ----
uint8[16] task_id
bool duplicate
uint8 status
uint32 epoch
uint32 assign_term
uint8 primary_node
uint8[<=15] helper_nodes

# ---- 只在 QUERY 时填写（整理 0.3 时补充：查询接口要返回这些）----
string<=64 name
string spec_json
string<=128 error_message
string<=128 last_event
