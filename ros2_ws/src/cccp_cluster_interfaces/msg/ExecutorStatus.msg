# <ns>/exec/status：数据面进程（执行器）按 1 Hz 发布，只有本节点的控制进程订阅。
# 控制进程据此填写 NodeStatus 的 inference_ready、algorithms_ready 和 running_tasks。
# 控制进程超过 3 秒没收到这条消息，就认为执行器不可用（inference_ready=false，running_tasks 为空）。
# QoS：BEST_EFFORT + KEEP_LAST(1) + VOLATILE。

string node_name
uint64 incarnation                # 执行器进程启动时的 Unix 毫秒
uint64 seq
bool inference_ready              # 至少一个算法服务可用
string[] algorithms_ready         # 服务已就绪的算法 ID
RunningTask[] running_tasks
uint64 max_term                   # 执行器见过的最大 term，便于排查过期的集群主节点
