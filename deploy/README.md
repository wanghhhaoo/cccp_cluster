# 部署与真机测试

每块板卡跑一个容器（`--network host`），容器里由 `start_node.sh` 拉起 3 个进程：控制、数据面、取图（CONVENTIONS 6.1）。节点之间靠 DDS 发现，不需要成员列表，只要求 `NODE_NAME` 唯一。

| 文件 | 用途 |
|---|---|
| `Dockerfile`、`Dockerfile.arm64` | x86_64 / Jetson 镜像 |
| `start_node.sh` | 容器入口，启动 3 个进程 |
| `cyclonedds.xml` | DDS 网卡和组播设置，`start_node.sh` 在设置了 `CCCP_DDS_INTERFACE` 时启用 |
| `node.env.example` | 一块板卡的配置样例 |
| `run_node.sh` | 在板卡上启动（或替换）本节点的容器 |
| `smoke_check.py` | 从地面站检查集群状态，只用标准库 |

## 1 板卡准备

| 项目 | 要求 |
|---|---|
| 系统 | JetPack，Docker 和 `nvidia-container-runtime` |
| 网络 | 12 块板卡在**同一个二层广播域**并允许组播；DDS 默认的组播发现跨不过三层路由（DESIGN 10.5） |
| 防火墙 | 放行 UDP 7400 起的 DDS 端口段（域 `d` 用 7400 + 250·d 起的一段），以及 UE HTTP 的 18090/TCP |
| 时钟 | 建议用 chrony 同步到地面站。控制逻辑只用单调时钟，时钟回退也不影响（DESIGN“实现时的调整”第 23 条），但多块板卡的日志和抓包要靠墙钟对齐 |
| 存储 | 容器日志按 50 MB × 5 循环（`run_node.sh` 已设置） |

## 2 构建和分发镜像

Jetson 镜像在 Jetson 上构建，torch 必须是与 JetPack 版本匹配的 NVIDIA wheel：

```bash
docker build -f deploy/Dockerfile.arm64 --build-arg TORCH_WHEEL_URL=<torch wheel> --build-arg TORCHVISION_WHEEL_URL=<torchvision wheel> -t cccp-node:p4-$(git rev-parse --short HEAD)-arm64 .
```

```bash
docker save cccp-node:p4-<hash>-arm64 | gzip > cccp-node.tar.gz
```

在其他板卡上执行 `docker load -i cccp-node.tar.gz`。**12 块板卡必须是同一个标签**（协议版本不同的节点不能混跑，CONVENTIONS 8）。

## 3 配置和启动

```bash
cp deploy/node.env.example deploy/node.env
```

每块板卡至少要改 `NODE_NAME`、`IMAGE`、`CCCP_DDS_INTERFACE`（集群网络的网卡，用 `ip -br link` 查看）。然后：

```bash
deploy/run_node.sh deploy/node.env
```

- 容器用 `--restart unless-stopped`，板卡重启后自动起来。
- 启动日志（`docker logs cccp-<NODE_NAME>`）第二行会打印 DDS 网卡、组播设置和 `CYCLONEDDS_URI`。网卡名写错时 `start_node.sh` 直接退出并报错。
- 想看 Cyclone 实际选了什么，在 `node.env` 里设 `CCCP_DDS_TRACE=config`，启动时会打印完整的生效配置。
- 第一轮只验证控制面和 PKCI/PKCT，**先不配三类批处理任务**（FASTLIVO 的镜像只有 amd64，还需要在容器里调用 Docker；HETEROMAP 需要共享盘，见 `docs/推理任务集成.md`）。

### 组播还是单播（`CCCP_DDS_MULTICAST`）

Cyclone 的默认值 `default` 在无线网卡上只让发现报文（SPDP）走组播，数据走单播：每条心跳给每个订阅者各发一份（12 个节点时是 11 份），但有 802.11 的确认和重传。设成 `true` 则心跳只发一份组播，但 802.11 组播没有重传，并且按最低基础速率发送。`false` 不能用：没有配置 Peers 时，发现必须靠组播，每个节点都只看得到自己、各自当选（远端实测），`run_node.sh` 会拒绝这个值。哪个更好要看现场抓包（DESIGN 2.8 的措施 B）：两种设置各跑一轮，比较 `tools/rtps_loss.py` 打出的丢失率和每秒包数；它也会打印每个 writer 实际发往哪些目的地址，可以用来确认当前用的是组播还是单播。

## 4 冒烟检查

在地面站上对所有节点执行：

```bash
python3 deploy/smoke_check.py 192.168.1.101 192.168.1.102 192.168.1.103
```

它检查：每个网关都能连上、看到预期数量的在线节点；所有网关跟随同一个集群主节点，只有一个节点声明，term 一致；执行器都活着；链路丢失率是否超过 `--max-loss`（默认 5%，只告警）。加上 `--task '<创建任务的 JSON>'` 还会建一个任务、等它进入 Running 再销毁。有失败时退出码为 1，可以放进 `watch -n 5` 里盯着。

## 5 测试顺序

1. **单机多容器**（已在 10.160.4.17 上做过，见 HANDOFF 第十七～十九节）：在 `node.env` 里加 `DOCKER_NETWORK=<bridge 网络名>`、`CCCP_DDS_INTERFACE=eth0`，一台机器可以起多个节点。每次改控制面后都可以拿它做回归。
2. **2～3 块板卡冒烟**：`smoke_check.py` 全部通过；杀掉集群主节点的容器（`docker kill cccp-<名字>`），约 6 秒内换主、任务没有被重复执行；`docker restart` 一个节点，它回来后在其他节点那里显示为 restarted。重点看 HANDOFF 里列的三项：`TaskList` 的历史补发、当选后新建的端点能否在 5 秒预热内完成匹配、关闭参数服务后的端点数（`docker exec` 进容器用 `ros2 node list`、`ros2 topic info -v`）。
3. **12 块板卡**：
   - 抓包：空闲 60 秒，每块板卡上执行 `tcpdump -i <网卡> -w edge-XX.pcap udp`。把各板卡的 pcap 收到一个目录，加一个 `addresses` 文件（每行 `<节点名> <IP>`），然后运行 `python3 tools/rtps_loss.py <目录>`。
   - 故障注入：按 DESIGN 10.2 的验收表逐条执行，丢包和延迟用 `tc qdisc add dev <网卡> root netem loss 30%` 这类命令注入，结果回填附录 A。

## 6 常见问题

| 现象 | 先查什么 |
|---|---|
| 节点之间互相看不到 | 所有板卡的 `ROS_DOMAIN_ID` 是否一致；`CCCP_DDS_INTERFACE` 是否是集群网络那块网卡；交换机或 AP 是否过滤组播（SPDP 发现一定要组播）；防火墙 |
| 某个节点时断时续 | `/api/ue/nodes` 里各节点对它的 `loss_rate`；日志里 `node=<名字> offline ... missed=` 的 seq 缺口（缺口大说明它一直在发，是途中丢的）；抓包确认包到没到接收方网卡（HANDOFF 第十九节的查法） |
| 出现两个集群主节点 | `smoke_check.py` 会报 “gateways disagree”；看日志里的选主间接证据（`not heard here: not claiming`），HANDOFF 第十八节 |
| `inference not ready` | 数据面进程的日志：torch 能否导入、`COLLAB_DEVICE`、模型权重 |
