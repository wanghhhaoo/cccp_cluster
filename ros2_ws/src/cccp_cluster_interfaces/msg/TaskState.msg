# 一个节点上正在运行的一个任务，由执行者上报（ExecutorStatus.tasks → NodeStatus.tasks）。
# 执行者是“实际在跑什么”的唯一来源：新的集群主节点上任后，靠收集各节点的 TaskState 重建任务表
# （开发规范 5.3）。所以这里带着重建所需的全部信息，而不只是 task_id。

string ROLE_PRIMARY="primary"
string ROLE_HELPER="helper"

string task_id                    # UUID
uint32 epoch
string role                       # primary（任务主节点）| helper（辅助节点）
string primary_node
string[] helper_nodes             # 任务主节点上的这一项才完整；辅助节点上可能为空
string idempotency_key
string name
string spec_json                  # TaskSpec 的 JSON（任务规格，提交后不变）
