# NodeStatus.links 的一项：发布方（记为 Y）对邻居 peer（记为 X）的链路测量。
#
# RTT 不需要额外的探测消息，做法与 RTCP 接收报告相同：
#   1. Y 收到 X 的 NodeStatus 后，记下它的 seq 和收到时刻（Y 的单调时钟）；
#   2. Y 下次发布 NodeStatus 时，在这一项里带上 echo_seq 和 echo_delay_ms；
#   3. X 收到后计算 RTT = 当前时刻 - 自己发出 echo_seq 那条消息的时刻 - echo_delay_ms。
# 两端都只用自己的时钟，不受节点间时钟偏差影响。

string peer_name

# Y 对 X 的测量结果；还没有样本时填 -1
float32 rtt_ms                    # 平滑后的 RTT
float32 jitter_ms                 # RTT 抖动，按 RFC 3550 的方式平滑
float32 loss_rate                 # 最近窗口内 X -> Y 方向 NodeStatus 的丢失比例，0~1

# 供 X 计算 RTT 的回显字段；还没收到过 X 的消息时 echo_seq 为 0
uint64 echo_seq                   # Y 最近收到的 X 的 NodeStatus.seq
uint32 echo_delay_ms              # 从 Y 收到那条消息到 Y 本次发布经过的毫秒数
