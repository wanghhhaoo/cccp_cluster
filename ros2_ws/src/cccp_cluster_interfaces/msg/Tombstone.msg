# 墓碑：本节点因“销毁”而停止的任务（设计 4.5）。保留 tombstone_ttl_s，最多 16 条。
# 新集群主节点在任何节点的 TaskList 里看到某任务的墓碑，就把它接收为 Cancelled，不让它复活。

uint8[16] task_id
