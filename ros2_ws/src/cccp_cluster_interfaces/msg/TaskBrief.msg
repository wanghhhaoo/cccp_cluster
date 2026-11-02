# NodeStatus.tasks 的一项：本节点正在运行的一个任务的摘要，约 28 B（设计 0.3 附录 C.1）。
# 对账判断“谁在跑哪个任务的哪个执行代次”只需要这些；完整信息在 TaskList 里。

uint8 ROLE_PRIMARY=0
uint8 ROLE_HELPER=1

uint8[16] task_id                 # 二进制 UUID
uint32 epoch                      # 执行代次 (epoch, assign_term)，按字典序比较
uint32 assign_term
uint8 role
