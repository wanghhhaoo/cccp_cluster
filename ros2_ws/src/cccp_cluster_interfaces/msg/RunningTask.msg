# NodeStatus.running_tasks 的一项：本节点正在执行的一个任务。

string ROLE_PRIMARY="primary"
string ROLE_HELPER="helper"

int64 task_id
uint32 epoch
string role                       # primary（任务主节点）| helper（辅助节点）
