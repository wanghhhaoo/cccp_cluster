# /cluster/task_list：本节点所有任务的完整 TaskState 和墓碑（设计 0.3 第 3.2 节、附录 C.3）。
# 只在内容变化时发布。每个节点都缓存所有节点的最新一份，离线节点的保留 offline_retain_s，
# 新集群主节点据此接收任务（包括已经离线的旧集群主节点自己执行的任务）。
# QoS：RELIABLE + TRANSIENT_LOCAL；发布端 KEEP_LAST(1)，订阅端 KEEP_LAST(32)。
# 订阅端也必须声明 TRANSIENT_LOCAL，否则收不到历史消息；深度要开大，因为订阅端的历史槽位是所有发布者共用的。

uint8 node_id
string<=32 node_name              # 节点编号 → 名字的对应（命名空间、日志、ros:// 地址用的都是名字）
uint64 incarnation
uint32 tasks_version              # 本节点任务列表或墓碑每变化一次 +1
TaskState[<=8] tasks
Tombstone[<=16] tombstones
