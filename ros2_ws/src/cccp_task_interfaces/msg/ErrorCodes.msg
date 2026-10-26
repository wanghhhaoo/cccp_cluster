# 稳定错误码，只定义常量。各服务响应和消息里的 error_code 字段取这里的值，成功时为空字符串。
# 每个错误码是否可以重试，见开发规范 7.2。
#
# 前 10 个与《CCCP 新增算法接入规范 v1.0》第 5 节一致：

string INVALID_INPUT="INVALID_INPUT"
string NOT_READY="NOT_READY"
string UNSUPPORTED_VERSION="UNSUPPORTED_VERSION"
string BUSY="BUSY"
string TIMEOUT="TIMEOUT"
string CANCELLED="CANCELLED"
string STALE_EPOCH="STALE_EPOCH"
string INFERENCE_ERROR="INFERENCE_ERROR"
string INSUFFICIENT_PEERS="INSUFFICIENT_PEERS"
string FUSION_ERROR="FUSION_ERROR"

# 以下为集群层新增：
# 命令来自已经过期的集群主节点（term 小于接收方已知的值）
string STALE_TERM="STALE_TERM"
# 当前没有集群主节点（例如正在选举），稍后重试
string NO_LEADER="NO_LEADER"
# 过滤后没有满足约束的节点
string NO_FEASIBLE_NODE="NO_FEASIBLE_NODE"
# 任务不存在
string NOT_FOUND="NOT_FOUND"
# 当前状态不允许该操作，例如销毁已结束的任务
string INVALID_STATE="INVALID_STATE"
# 下发到执行节点失败，已回滚
string DISPATCH_FAILED="DISPATCH_FAILED"
# 链式触发超过最大深度
string CHAIN_TOO_DEEP="CHAIN_TOO_DEEP"
