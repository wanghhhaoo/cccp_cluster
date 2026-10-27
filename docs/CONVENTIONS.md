# CCCP 分布式集群 开发规范

版本 0.1（草案）｜2026-10-26

本文是平台内部的开发规范，规定命名、通信、标识、一致性规则、代码组织、错误处理、接口变更和测试。

"必须"是代码评审的硬性要求；"应"是推荐做法，不这样做时需要在评审中说明原因。

---

## 1 术语

"主节点"在本系统里有三种不同的含义。**文档、代码和日志中都禁止只写"主节点"**，必须用下表左列的全称。

| 术语 | 含义 | 代码中的名字 |
|---|---|---|
| **集群主节点** | 由 Raft 选出的节点，负责运行控制面（节点管理、调度、任务管理）。主节点故障才换 | `leader` |
| **任期** | Raft term。每选出一次新的集群主节点就 +1 | `term` |
| **任务主节点** | 执行某个任务、并负责协调辅助节点的节点（原架构图中的节点 A） | `primary` |
| **辅助节点** | 协同任务中由任务主节点调用的其他节点 | `helper` |
| **主视角** | PKCT 协同跟踪选举（`ElectPrimary`）选出的设备，属于算法层的概念 | `primary_drone_id` |
| **执行代次** | 任务每重新调度一次就 +1，用来隔离新旧执行会话 | `epoch` |
| **工作单元** | 算法处理的最小单位，目前是一帧 | `frame_id` |
| **任务表** | 用 Raft 复制到所有节点的任务状态，替代原来的数据库 | `TaskTable` |
| **控制面 / 数据面** | 控制面只在集群主节点上运行；数据面（执行器和推理服务）在所有节点上运行 | — |

`term` 和 `epoch` 的区别：`term` 防止**过期的集群主节点**继续发出命令；`epoch` 防止**过期的执行会话**继续上报结果。例如任务主节点掉线后任务被重新调度，`epoch` 加 1，但 `term` 不变。

协同模式（`CreateTask.mode`）：

| 模式 | 含义 | 结果 |
|---|---|---|
| `local` | 只有任务主节点执行 | 本节点的结果 |
| `redundant` | 任务主节点和 `helper_count` 个辅助节点处理同一份输入 | 最先返回的有效结果；之后到达的结果不能覆盖它 |
| `fusion` | 各节点处理各自的视角，由任务主节点汇总 | 融合结果，例如 PKCI 的 `fuse`、PKCT 的 `elect` |

`min_participants` 是最少有效参与数（含任务主节点）。达不到时，任务主节点报告 `INSUFFICIENT_PEERS`。原架构图中的“模型并行”暂不支持，需要时再新增模式。

---

## 2 命名

### 2.1 包和节点名

- ROS 2 包名必须以 `cccp_` 开头，例如 `cccp_cluster_interfaces`、`cccp_task_interfaces`。从复用包原样带过来的 `collab_inference*` 保持原名。
- 节点名 `NODE_NAME` 由部署配置决定，并且必须出现在集群成员表中（见 3.1）。
- 命名空间 `<ns>` 由 `NODE_NAME` 推导，规则沿用复用包的 `naming.py`：非 `[A-Za-z0-9_]` 字符替换为 `_`，连续的 `_` 合并为一个，空串或以数字开头时在前面加 `n`。例如 `edge-01` 推导为 `/edge_01`。

### 2.2 通信端点

| 名称 | 类型 | 方向 | 说明 |
|---|---|---|---|
| `/cluster/node_status` | `NodeStatus` topic | 所有节点 → 所有节点 | 资源、心跳、链路、对账 |
| `/cluster/task_report` | `TaskReport` topic | 执行节点 → 集群主节点 | 执行状态变化 |
| `/cluster/task_status` | `TaskStatus` topic | 集群主节点 → 所有节点 | 任务表变化，供网关生成事件流 |
| `<ns>/api/task/create\|destroy\|query` | `CreateTask` 等 srv | 任何调用方 → 本节点网关 | 对外入口 |
| `<ns>/exec/task/start\|stop` | `StartTask`/`StopTask` srv | 集群主节点 → 执行节点 | 内部接口，其他程序不得调用 |
| `<ns>/exec/status` | `ExecutorStatus` topic | 数据面进程 → 本节点控制进程 | 正在运行的任务、算法是否就绪 |
| `<ns>/algorithms/pkci/*`、`pkct/*` | 复用包原有 srv | 执行节点 → 推理服务 | 不改 |
| `<ns>/camera/image` | `sensor_msgs/Image` | 取图 → 执行节点 | 不改 |
| `<ns>/task/result`、`result_image` | `TaskResult` / `Image` | 执行节点 → 汇总方、网关 | `TaskResult` 新增了字段 |
| `http://<节点>:18090/api/ue/*` | HTTP/JSON | UE → 任意节点 | 沿用复用包的 `ue_api.py` |

### 2.3 字段和常量

- 字段名必须用 `snake_case`，常量必须用 `UPPER_SNAKE`（这是 ROS 2 的语法要求）。
- 枚举值必须在接口文件中定义为常量，代码里通过常量引用，不要手写字符串。例如写 `TaskStatus.RUNNING`，不要写 `"Running"`。
- 纯逻辑模块（`cccp_node/core/`）不能 import 接口包，所以这些常量在 `core/constants.py` 中镜像定义一份。`test_constants.py` 会直接读取接口文件，两边不一致时测试失败。
- 任务状态的取值沿用复用包的 `Pending`/`Scheduled`/`Running`/`Finished`/`Failed`/`Cancelled`，以保证 UE 侧兼容。

---

## 3 通信

### 3.1 三种通信方式的分工

| 通信方式 | 用途 | 说明 |
|---|---|---|
| Raft（独立 TCP） | 选举集群主节点、复制任务表 | 端口由 `RAFT_PORT` 配置（建议 7480）；成员表是静态的，写在 `cluster.yaml` 里 |
| ROS 2 | 节点之间的其他所有通信 | 见 2.2 |
| HTTP | 只用于 UE 与节点之间 | 节点之间禁止使用 HTTP |

`cluster.yaml` 示例：

```yaml
protocol_version: 1
raft_port: 7480
members:            # 12 个节点全部参与投票
  - {name: edge-01, ip: 10.0.0.1}
  - {name: edge-02, ip: 10.0.0.2}
  # ...
```

Raft 必须事先知道完整的成员表，才能计算多数派，所以这里**不使用 ROS 2 的自动发现**。

### 3.2 服务名必须唯一

**整个 ROS domain 中，同一个服务名只能有一个服务端。** 因此服务一律放在 `<ns>` 下，全局名字（`/cluster/*`）只用于 topic。

原因：同名服务有多个服务端时，ROS 2 没有定义会发生什么，实际上请求通常会被**每个服务端都执行一遍**。如果把下发接口命名为全局的 `/cluster/start`，换主的过渡期里新旧两个集群主节点都会收到请求，同一个任务就会被执行两次。

### 3.3 QoS

| 端点 | Reliability | History | Durability | 原因 |
|---|---|---|---|---|
| `/cluster/node_status` | BEST_EFFORT | KEEP_LAST(1) | VOLATILE | 只有最新值有用，丢一条下一秒就补上 |
| `<ns>/exec/status` | BEST_EFFORT | KEEP_LAST(1) | VOLATILE | 同上 |
| `/cluster/task_report` | RELIABLE | KEEP_LAST(100) | VOLATILE | 状态变化不应丢失；即使丢了，也有对账兜底 |
| `/cluster/task_status` | RELIABLE | KEEP_LAST(100) | VOLATILE | 事件流 |
| `<ns>/camera/image` | sensor data 预设 | — | — | 沿用复用包 |
| `<ns>/task/result` | RELIABLE | KEEP_LAST(10) | VOLATILE | 沿用复用包 |

QoS 的配置集中定义在 `cccp_node/ros/qos.py`，端点名称集中定义在 `cccp_node/names.py`。各处必须统一引用这两个模块，不允许在各自的代码里临时创建 QoS 或手写端点名。

---

## 4 标识与时间

### 4.1 标识

| 标识 | 生成方式 | 规则 |
|---|---|---|
| `task_id` | 任务表中的复制计数器，在复制方法 `submit` 内部 +1 | 从 1 开始，永不复用；保持 int64，与 UE 接口兼容 |
| `epoch` | 任务表在每次调度时 +1 | 首次调度时为 1 |
| `TaskStatus.seq` | 任务表的复制计数器，每次状态变化 +1 | 全局单调；换主后依然连续 |
| `incarnation` | 进程启动时的 Unix 毫秒 | 用来判断节点是否重启过 |
| PKCT `tracker_id` | `task-<task_id>-<epoch>` | 按 `task_id` + `epoch` 隔离会话（复用包原来用的是 `task-<task_id>`，需要改） |

### 4.2 幂等键格式

| 来源 | 格式 | 由谁生成 |
|---|---|---|
| manual | `manual:<客户端ID>:<UUID>` | UE 或前端在每次用户操作时生成一次，重试时沿用同一个键 |
| schedule | `schedule:<规则ID>:<计划触发时刻的 Unix 毫秒>` | 集群主节点上的定时器 |
| chain | `chain:<父任务ID>:<规则名>[:<frame_id>]` | 父任务的任务主节点 |
| event | `event:<节点名>:<事件源>:<事件序号>` | 产生事件的节点 |
| 为空 | `anon:<UUID>` | 网关自动生成 |

### 4.3 时间

- 超时和耗时必须使用**本地单调时钟**（`time.monotonic()`）计算。
- 消息里的 `stamp` 只能用于显示和记日志，**不得拿不同节点的时间戳做减法**。复用包 `monitor_node` 用接收时间减发送时间来算时延，就违反了这条规则，必须改为 `LinkQuality` 中的回显方式。
- 所有节点仍然应该部署 chrony 做时间同步，这只是为了方便对照各节点的日志，正确性不依赖它。
- 在 JSON 中，纳秒级时间戳必须写成十进制字符串。

---

## 5 一致性规则（必须）

这一节是"同一个任务不会被两个节点重复执行"这条保证的基础。违反任何一条都会破坏这条保证，评审时必须逐条检查。

### 5.1 任务表只能通过 Raft 写入

- 任务表（`core/task_table.py`）是一个确定性状态机，**唯一的写入口是 `apply(command)`**。命令是不可变的 dataclass，用 `command_to_dict()` 序列化后作为 Raft 日志条目。共识层只负责让各节点按相同顺序调用 `apply()`；换 Raft 库时，只需要改共识层。
- 禁止绕过 `apply()` 修改任务表。表中的记录是不可变的，读取方拿到的记录无法被修改。
- 网关可以在任意节点上提交 `Submit` 和 `Cancel`，由共识层转发给集群主节点。PySyncObj 的转发行为已用 3 节点测试验证（`test_pysyncobj_consensus.py`）。
- `Assign`、`Requeue`、`MarkFailed`、`ReportStatus`、`Prune`、`Barrier` 只能由集群主节点上的控制面提交。其中 `Assign`、`Requeue`、`MarkFailed` 都带 `expected_epoch`：只有任务仍处于该 epoch 时才生效，否则返回 `STALE_EPOCH`。这样基于同一份快照做出的两次决策，最多只有一次能成功。

### 5.2 复制方法必须是确定性的

同一个复制方法会在 12 个节点上各执行一遍，所以：

- 方法内部禁止调用 `uuid4()`、`time.time()`、`random`，也禁止读取本地文件或环境变量。这类值必须由调用方生成后作为参数传进去。
- 方法内部禁止发起 I/O、ROS 调用和日志以外的任何副作用。
- 方法内部禁止依赖 `dict`/`set` 迭代顺序以外的任何本地状态。

### 5.3 先提交，后下发

集群主节点必须**先把调度结果提交到任务表，确认提交成功后，再调用 `StartTask`**。这样新上任的集群主节点只要看任务表，就能知道哪些任务可能已经下发出去了。

### 5.4 执行节点检查 `term` 和 `epoch`

执行节点必须记录自己见过的最大 `term`，以及每个任务当前的 `epoch`，并按下表处理：

| 收到的命令 | 处理 |
|---|---|
| `term` < 已知的最大 `term` | 拒绝，返回 `STALE_TERM` |
| `StartTask`：同一 `task_id`、同一 `epoch`，任务已经在运行 | 返回**成功**（幂等） |
| `StartTask`：同一 `task_id`、更大的 `epoch` | 先停掉旧会话，再按新 `epoch` 启动 |
| `StartTask`：同一 `task_id`、更小的 `epoch` | 拒绝，返回 `STALE_EPOCH` |
| `StopTask`：任务本来就不在运行 | 返回成功（幂等） |

集群主节点处理 `TaskReport` 时，必须丢弃 `epoch` 小于任务当前 `epoch` 的报告。汇总方处理 `TaskResult` 时同样如此。

### 5.5 状态机

```
            ┌─────────────── requeue(epoch+1) ◄──────────────┐  下发暂时失败，或重新调度时没有可用节点
            ▼                                                │
Pending ──assign(epoch+1)──► Scheduled ──report Running──► Running ──report Finished──► Finished
   │                            │  ▲                         │
   │                            │  └──assign(epoch+1)────────┤  任务主节点离线 / 丢失任务 / 迟迟不启动
   ▼                            ▼                            ▼
 Failed                       Failed                       Failed
（Pending 超时，              （下发时返回 INVALID_INPUT     （任务主节点报告 Failed）
  NO_FEASIBLE_NODE）           等永久错误：DISPATCH_FAILED）

任何非终态 ──destroy──► Cancelled        终态：Finished / Failed / Cancelled，之后不再变化
```

- 定义状态序：Pending=0，Scheduled=1，Running=2，终态=3。`(epoch, 状态序)` 按字典序**只增不减**；如果一次更新会让它变小，复制方法必须忽略这次更新。
- 重新调度和 `Requeue` 都会让状态“往回走”，但它们同时会把 `epoch` 加 1，所以 `(epoch, 状态序)` 仍然是增大的。
- 下发失败的处理：`INVALID_INPUT`、`UNSUPPORTED_VERSION` 属于永久错误，直接判定 Failed；其他错误（超时、`BUSY`、`NOT_READY`）执行 `Requeue`，并在 60 秒内避开这个节点；收到 `STALE_TERM` 说明自己已经不是集群主节点，什么也不做。
- **`request_node` 是硬性指定（待定，见 HANDOFF）**：被指定的节点挂掉后，任务回到 Pending，不会迁移到其他节点，超过 `pending_timeout_s` 后判定 Failed。不指定节点的任务则会迁移。

### 5.6 对账（集群主节点每秒执行一次）

新当选的集群主节点在开始对账之前，必须依次完成两件事：

1. **提交一条 `Barrier` 命令，并等它应用完毕。** Raft 的新主节点要在自己的任期内提交一条日志之后，才能确认并应用前任期的记录。在这之前，它的本地任务表可能缺任务。12 节点实测中，第一次换主时新主节点刚当选就查不到已提交的任务。这时如果直接对账，会把正在运行的任务当成孤儿全部停掉。
2. **再等待 `node_offline_timeout_s`**，让资源视图收齐所有存活节点的 `NodeStatus`。

对账规则（`core/reconciler.py`）：

| 观察到的情况 | 动作 |
|---|---|
| Pending | 调度；超过 `pending_timeout_s` 仍无可用节点，判定 Failed |
| 任务主节点离线（超过 `node_offline_timeout_s` 没有收到它的 `NodeStatus`） | 重新调度：`epoch` + 1 |
| 节点在线，`running_tasks` 里有这个任务且 `epoch` 一致 | 正常，不做处理；如果任务表里还是 Scheduled，说明 `Running` 报告丢了（通常发生在换主期间），由集群主节点代为提交 `ReportStatus(Running)` |
| 节点（主或辅）在线但没有这个任务，任务状态是 Scheduled | 每隔 `dispatch_retry_s` 用同一个 `epoch` 重发 `StartTask`（幂等）；超过 `start_timeout_s` 仍未启动，重新调度 |
| 任务主节点在线但没有这个任务，任务状态是 Running | 先等待 `missing_grace_s`（`NodeStatus` 可能比 `TaskReport` 晚到）；仍然没有就重新调度 |
| 某节点的 `running_tasks` 里有任务表中不存在、已经结束、`epoch` 过期或者并没有分配给它的任务 | 每隔 `stop_retry_s` 向该节点发送 `StopTask`，清理孤儿任务 |

“节点在线”和各种计时一律使用集群主节点本地的单调时钟。这些计时只保存在当前集群主节点的内存里；换主后会从头开始计时，结果只是决策晚一点，不会导致任何非幂等的操作被重复执行。

MVP 阶段不做辅助节点的替换。辅助节点失效后，按 `min_participants` 的规定降质继续运行，或者判定任务失败。

### 5.7 默认参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `node_status_period_s` | 1.0 | NodeStatus 发布周期 |
| `node_offline_timeout_s` | 5.0 | 连续这么久收不到 NodeStatus，就判定节点离线 |
| `raft_heartbeat_s` | 0.3 | 对应 PySyncObj 的 `appendEntriesPeriod`；一次提交的延迟约等于它（实测 316 ms） |
| `raft_election_timeout_s` | 1.5 ~ 3.0 | 对应 `raftMinTimeout` / `raftMaxTimeout`；12 节点实测换主耗时中位数 3.3 s，最长 4.2 s |
| `raft_leader_fallback_s` | 6.0 | 集群主节点联系不上多数节点这么久就卸任（PySyncObj 默认 30 s，太长） |
| `dispatch_timeout_s` | 5.0 | 单次 `StartTask` 调用的超时 |
| `dispatch_retry_s` | 5.0 | 未启动的任务多久重发一次 `StartTask` |
| `start_timeout_s` | 15.0 | Scheduled 这么久仍未在任务主节点上启动，就重新调度 |
| `missing_grace_s` | 3.0 | Running 的任务从任务主节点的 `running_tasks` 中消失多久后重新调度 |
| `stop_retry_s` | 5.0 | 孤儿任务多久重发一次 `StopTask` |
| `create_wait_timeout_s` | 10.0 | `CreateTask` 等待下发完成的最长时间 |
| `pending_timeout_s` | 30.0 | Pending 状态持续这么久仍无可用节点，就判定 Failed |
| `avoid_failed_node_s` | 60.0 | 某节点启动任务失败后，在这段时间内调度该任务时避开它 |
| `prune_terminal_after_s` | 3600 | 终态任务保留这么久后从任务表删除 |
| `max_tasks_per_node` | 4 | 单节点同时运行的任务上限（与 PKCT 的 `MAX_TRACKERS` 一致） |
| `max_chain_depth` | 5 | 链式触发的最大深度 |
| `leader_self_penalty` | 10 | 集群主节点给自己的调度评分扣分（满分 100） |
| `per_task_penalty` | 5 | 节点上每个已分配任务的调度扣分，避免 CPU 使用率还没反映出新任务时，任务全部挤到同一个节点 |

上表的超时值都需要在真实自组网上实测后调整，调整结果要回填到本表。

---

## 6 进程与代码组织

### 6.1 每个节点运行三个进程

| 进程 | 包含 | 原因 |
|---|---|---|
| 控制进程 | Raft 副本、控制面（只在集群主节点上启用）、网关（ROS 2 + UE HTTP）、资源监控 | 必须保持轻量，否则 Raft 心跳可能被阻塞 |
| 数据面进程 | 执行器、PKCI、PKCT | 推理和图像处理很耗 CPU，受 GIL 影响，不能和控制进程放在一起 |
| 取图进程 | `airsim_camera` | 沿用复用包 |

复用包在每块板卡上运行 5 个进程。改成 3 个之后，DDS 参与者的数量从 60 个降到 36 个。

- 控制进程的入口是 `ros2 run cccp_node control_node`；数据面进程的入口是 `ros2 run cccp_task_executor data_plane`，它用一个 `MultiThreadedExecutor` 同时运行执行器、PKCI、PKCT 三个节点；启动脚本是 `deploy/start_node.sh`。
- 两个进程之间通过 `<ns>/exec/status` 传递信息：控制进程根据它填写 `NodeStatus` 里的 `running_tasks` 和 `inference_ready`。

### 6.2 业务逻辑与 ROS 解耦

- 调度、状态机、对账、链路估计这几部分必须写成**不 import rclpy 的纯 Python 模块**，ROS 节点只做一层薄的适配。复用包的 `test_bridge_logic.py` 就是这种写法。
- 这样做有两个好处：单元测试不需要 ROS 环境；以后转 C++ 时可以逐个模块对照翻译。
- 共识层必须通过 `Consensus` 接口访问，业务代码禁止直接 import `pysyncobj`。这样以后换成 NuRaft 或 etcd 时，只需要改实现。

### 6.3 Python

- 版本：Python 3.10（Ubuntu 22.04 / ROS 2 Humble 自带的版本），不得使用 3.11 及以上版本才有的语法。
- 公共函数必须写类型标注。
- 代码格式和静态检查应使用 `ruff format` 和 `ruff check`。

### 6.4 目录结构

```
cccp_cluster/
├── docs/CONVENTIONS.md                 本文
├── ros2_ws/src/
│   ├── cccp_cluster_interfaces/        集群层接口（新增）
│   ├── cccp_task_interfaces/           任务层接口（从复用包 v0.1 演进而来）
│   ├── collab_inference_interfaces/    从复用包原样复制
│   ├── collab_inference/               推理服务，从复用包原样复制
│   ├── cccp_task_executor/             数据面：执行器（已改造）+ data_plane 组合入口 + AirSim 取图
│   └── cccp_node/                      控制进程
│       └── cccp_node/
│           ├── core/                   纯逻辑，禁止 import ROS 和共识库（由测试强制检查）
│           ├── consensus/              Consensus 接口；local（单机）和 pysyncobj 两种实现
│           ├── control/                集群主节点控制器、网关、UE 后端（不依赖 ROS）
│           ├── monitor/                CPU/内存/GPU 采集
│           ├── ros/                    ROS 适配层：control_node、dispatcher、convert、qos
│           ├── names.py                端点名称
│           └── ue_api.py               UE HTTP 接口
├── deploy/                             Dockerfile（x86/ARM64）、cluster.example.yaml、start_node.sh
└── tools/check_interfaces.py           无 ROS 环境下的接口语法检查
```

---

## 7 错误与日志

### 7.1 服务返回

- 服务回调中禁止把异常抛到 ROS 层，必须捕获异常，并返回 `success=false`、`error_code` 和可读的 `message`。这与复用包的做法一致。
- `error_code` 必须使用 `ErrorCodes.msg` 中定义的常量；`message` 用来给人看，程序不得解析 `message` 的内容。

### 7.2 错误码与重试

| 错误码 | 调用方能否重试 | 说明 |
|---|---|---|
| `NO_LEADER`、`BUSY`、`NOT_READY` | 可以，退避后重试 | 临时状态 |
| `TIMEOUT` | 可以，但**必须使用同一个幂等键** | 超时不代表对方没有执行 |
| `DISPATCH_FAILED` | 由集群主节点决定 | 已经回滚 |
| `STALE_TERM`、`STALE_EPOCH` | 不可以 | 发出命令的一方已经过期，应当停止 |
| `INVALID_INPUT`、`INVALID_STATE`、`NOT_FOUND`、`CHAIN_TOO_DEEP`、`UNSUPPORTED_VERSION` | 不可以 | 请求本身有问题 |
| `NO_FEASIBLE_NODE`、`INSUFFICIENT_PEERS` | 由调用方决定 | 资源不足 |
| `INFERENCE_ERROR`、`FUSION_ERROR` | 不由调用方重试 | 由执行器累计失败次数，超过上限后报告 Failed |
| `CANCELLED` | 不可以 | 任务已被销毁 |

### 7.3 日志

- 与任务相关的日志行必须带上 `key=value` 格式的字段：`task=<id> epoch=<e> term=<t> node=<名称>`，有工作单元时再加 `frame=<id>`。
- 集群主节点每次上任和卸任都必须记录一条 INFO 日志，内容包括 `term` 和接手时任务表的大小。
- 高频出现的警告必须限流（`throttle_duration_sec`）。

---

## 8 接口变更流程

- 接口文件是平台组、算法组和 UE 之间的契约，任何改动都必须经过评审。
- ROS 2 Humble 不支持类型演进。**`cccp_*` 接口包的任何改动，都会让新旧版本的节点无法互通**。因此：
  1. 每次改动都要把 `NodeStatus.PROTOCOL_VERSION` 加 1，同时更新对应 `package.xml` 中的版本号；
  2. 12 个节点必须一起升级；集群主节点不会给 `protocol_version` 与自己不同的节点派发任务；
  3. 镜像标签必须带上协议版本号，例如 `cccp-node:p1-<git短哈希>`。
- 每个接口文件开头的注释里，必须写明与上一版相比改了什么。
- 不得改变已有字段的含义。如果需要新含义，就新增一个字段。

---

## 9 测试

| 层级 | 内容 | 环境 |
|---|---|---|
| 单元测试 | 状态机、调度评分、对账、幂等、链路估计 | pytest，不需要 ROS |
| 接口检查 | `.msg`/`.srv` 语法检查、`colcon build` | CI |
| 单机集成 | 3～5 个容器模拟多节点，跑完整链路 | 在同一台主机上用 Docker 网络，参考复用包的联调方式 |
| 故障注入 | 见下表 | 单机集成环境，用 `tc netem` 模拟丢包和延迟 |
| 现场测试 | 12 块板卡加真实自组网：验证 DDS 发现、Raft 超时和流量 | 不能用容器测试代替 |

故障注入的必测项：

| 场景 | 通过条件 |
|---|---|
| 杀掉集群主节点 | 选出新的集群主节点，`term` + 1；没有任务被重复执行；没有任务丢失 |
| 杀掉任务主节点 | 在 `node_offline_timeout_s` 之后，任务被重新调度，`epoch` + 1 |
| 执行进程重启 | `incarnation` 变化，对账后任务恢复 |
| 重复发送 `StartTask` | 第二次返回成功，任务不会启动两次 |
| 用旧 `term` 发送 `StartTask` | 返回 `STALE_TERM` |
| 用同一个幂等键提交两次 `CreateTask` | 返回同一个 `task_id`，并且 `duplicate=true` |
| 某个节点丢包率达到 30% | 不会被误判为离线，也不会引发频繁选举 |
| 隔离一个节点 30 秒后恢复 | 恢复后不会造成不必要的换主（PreVote 问题，见下方说明） |

PySyncObj 是否支持 PreVote 还需要查证。如果不支持，最后一项在 MVP 阶段预期会失败，需要记录在已知问题中，转 C++（NuRaft）时解决。
