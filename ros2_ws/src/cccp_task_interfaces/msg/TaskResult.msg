# <ns>/task/result：执行节点每处理完一个工作单元（当前为一帧）发布一条结果。
# 相比复用包 v0.1：新增 node_name、epoch、valid、error_code，其余字段不变。
# success 表示计算过程完成，valid 表示结果满足算法的有效性规则，
# 两者含义不同（见《算法接入规范》第 3 节）。

int64 task_id
uint32 epoch                      # 汇总方丢弃旧代次的结果
string node_name                  # 产生这条结果的节点
string task_type                  # PKCI | PKCT
uint8 drone_id
uint32 frame_id                   # 工作单元编号；当前为任务内的帧计数
builtin_interfaces/Time source_stamp  # 输入帧的 header.stamp
bool success
bool valid
string error_code                 # 见 ErrorCodes.msg
string message
float32 latency_ms

# PKCI
int32 label
string class_name
float32 score
float32[] log_probs

# PKCT
float32 x
float32 y
float32 width
float32 height
float32 confidence

# 协同载荷（PKCI 为 increment，PKCT 为 identity）；转发时不得修改
uint8[] payload
