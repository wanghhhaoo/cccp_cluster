# NodeStatus.load：资源容量和使用率，约 20 B。每 slow_fields_every 条心跳附带一次。
# 使用率和电量单位均为 %，-1 表示未知。

uint16 cpu_cores
uint32 mem_total_mb
uint32 gpu_mem_total_mb           # 独立显存总量；Jetson 等与系统共享内存的 GPU 填 0
int8 cpu_usage
int8 mem_usage
int8 gpu_usage                    # 有 GPU 的节点总能测出它（>= 0），没有 GPU 时为 -1
int8 gpu_mem_usage
int8 battery                      # 非无人机节点填 -1
