# NodeStatus.links 的一项：发布方（记为 Y）对对端 peer（记为 X）的链路测量，约 8 B。
#
# RTT 不需要额外的探测消息，做法与 RTCP 接收报告相同：
#   1. Y 收到 X 的心跳后，记下它的 seq 和收到时刻（Y 的单调时钟）；
#   2. Y 下次发布带慢变部分的心跳时，在这一项里带上 echo_seq 和 echo_delay_4ms；
#   3. X 收到后计算 RTT = 当前时刻 - 自己发出 echo_seq 那条心跳的时刻 - echo_delay。
# 两端都只用自己的时钟，不受节点间时钟偏差影响。
#
# 每个值占 1 字节：时间以 4 ms 为单位，丢包率以 % 为单位。255 表示未知；
# 测量值最大取 254，所以超过约 1 秒的坏链路仍然显示为“很慢”，而不是“未测量”。

uint8 UNKNOWN=255

uint16 echo_seq                   # Y 最近收到的 X 的心跳 seq 的低 16 位；还没收到过时为 0
uint8 peer                        # 对端节点编号
uint8 rtt_4ms                     # 平滑后的 RTT
uint8 jitter_4ms                  # RTT 抖动，按 RFC 3550 的方式平滑
uint8 loss_pct                    # 最近窗口内 X -> Y 方向心跳的丢失比例
uint8 echo_delay_4ms              # 从 Y 收到那条心跳到 Y 本次发布经过的时间；饱和时不能用来算 RTT
