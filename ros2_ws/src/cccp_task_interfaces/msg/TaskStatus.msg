# /cluster/task_status：任务表中的状态变化提交后，由集群主节点发布。
# 各节点的网关订阅它，生成给 UE 的事件流。
# 相比复用包 v0.1：新增 epoch、term、seq 字段，状态常量改为在本文件中定义。

string PENDING="Pending"
string SCHEDULED="Scheduled"
string RUNNING="Running"
string FINISHED="Finished"
string FAILED="Failed"
string CANCELLED="Cancelled"

int64 task_id
string name
string task_type
string status
string node                       # 任务主节点
uint32 epoch                      # 任务执行代次
uint64 term                       # 发布者（集群主节点）的任期
uint64 seq                        # 任务表变更序号，全局单调递增；订阅方据此去重和排序，换主后保持连续
string message
builtin_interfaces/Time stamp
