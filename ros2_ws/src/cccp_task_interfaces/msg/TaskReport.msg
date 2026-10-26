# /cluster/task_report：执行节点向集群主节点报告某个任务在本节点上的状态变化。
# 所有节点都订阅，只有集群主节点处理（写入任务表）；不需要知道谁是主节点。
# 这条消息丢了也不会出错：NodeStatus.running_tasks 每秒全量对账一次，作为兜底。

string STATE_RUNNING="Running"
string STATE_FINISHED="Finished"
string STATE_FAILED="Failed"
# 收到 StopTask 后已清理完毕
string STATE_STOPPED="Stopped"

string node_name
int64 task_id
uint32 epoch                      # 主节点会丢弃 epoch 小于任务当前 epoch 的报告
string state
string error_code                 # 见 ErrorCodes.msg；成功时为空
string message
uint64 incarnation                # 与 NodeStatus.incarnation 相同
uint64 seq                        # 本节点的报告序号；(node_name, incarnation, seq) 用于去重
builtin_interfaces/Time stamp
