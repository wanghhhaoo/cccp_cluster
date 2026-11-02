# <ns>/exec/status：数据面进程（执行器）按 1 Hz 发布，只有本节点的控制进程订阅（设计 0.3 附录 C.4）。
# 控制进程据此生成心跳的任务摘要和本节点的 TaskList；任务或墓碑变化时 tasks_version +1。
# 超过 executor_timeout 没收到这条消息，控制进程把心跳的 executor_alive、inference_ready 置为 false，
# 任务列表和墓碑保持最后一次已知的值（执行器没有出声，不等于任务没了）。
# 执行器里发布它的定时器放在独立的回调组，不和推理回调抢线程。
# QoS：BEST_EFFORT + KEEP_LAST(1) + VOLATILE。

uint64 incarnation                # 执行器进程启动时的 Unix 毫秒
uint32 seq
bool inference_ready
uint8 algorithms_ready            # 位掩码，同 NodeStatus
TaskState[<=8] tasks
Tombstone[<=16] tombstones        # 墓碑由执行器产生，控制进程原样放进 TaskList
