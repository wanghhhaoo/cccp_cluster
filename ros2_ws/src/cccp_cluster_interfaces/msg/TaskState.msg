# 一个任务在本节点上的完整执行信息，由执行者上报（ExecutorStatus.tasks → TaskList.tasks）。
# 执行者是“实际在跑什么”的唯一来源：新的集群主节点上任后，靠各节点的 TaskList 重建任务表
# （开发规范 5.3），所以这里带着重建所需的全部信息，而不只是 task_id。

uint8 ROLE_PRIMARY=0
uint8 ROLE_HELPER=1

uint8[16] task_id                 # 二进制 UUID
uint32 epoch                      # 执行代次 (epoch, assign_term)
uint32 assign_term                # 做这次调度的集群主节点的任期；换主接收时不变
uint8 role
uint8 primary_node                # 这一代的任务主节点（节点编号）
uint8[<=15] helper_nodes          # 这一代的辅助节点（节点编号）
string idempotency_key
string<=64 name
string spec_json                  # TaskSpec 的 JSON，含 priority、preemption_policy
