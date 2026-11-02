# /cluster/exec_event：执行器 → 集群主节点（设计 0.3 第 3.5 节、附录 C.6）。
# 既通知执行状态变化，也是命令的回执。它是“快速通道”：丢了由心跳和对账兜底。
# 所有执行器一直有发布端；只有集群主节点在当选时创建订阅端。
# QoS：RELIABLE + KEEP_LAST(32) + VOLATILE。

uint8 KIND_RUNNING=1              # 已在运行；也是 START 的成功回执，重复 START 时再回一次
uint8 KIND_REJECTED=2             # 拒绝了命令，原因见 error_code
uint8 KIND_FAILED=3               # 运行中失败
uint8 KIND_STOPPED=4              # 已停止；也是 STOP 的回执，任务本来就没在跑也回这个
uint8 KIND_FINISHED=5             # 预留：给以后有终态的任务

uint8 ERR_NONE=0
uint8 ERR_STALE_TERM=1
uint8 ERR_STALE_EPOCH=2
uint8 ERR_BUSY=3
uint8 ERR_NOT_READY=4             # 运行中失败时表示没有输入帧（开发规范 5.5）
uint8 ERR_INVALID_INPUT=5
uint8 ERR_UNSUPPORTED_VERSION=6
uint8 ERR_INFERENCE_ERROR=7
uint8 ERR_INSUFFICIENT_PEERS=8
uint8 ERR_FUSION_ERROR=9
uint8 ERR_CANCELLED=10            # 只用在 Reply.exec_error：等任务跑起来时它被销毁了

uint8 node_id
uint64 incarnation                # 执行器进程的；与 seq 一起用于去重
uint32 seq

# 回执时原样带回命令的 term 和 request_id；主动上报（FAILED 等）时 request_id 填 0
uint32 term
uint32 request_id

uint8[16] task_id
uint32 epoch
uint32 assign_term
uint8 kind
uint8 error_code
string<=128 message
