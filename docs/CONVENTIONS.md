# CCCP 分布式集群 开发规范

版本 0.2（草案）｜2026-10-29

本文是平台内部的开发规范，规定命名、通信、标识、一致性规则、代码组织、错误处理、接口变更和测试。

"必须"是代码评审的硬性要求；"应"是推荐做法，不这样做时需要在评审中说明原因。

> 0.2 相对 0.1 的主要变化：去掉 Raft，改为基于 NodeStatus 广播选主；任务表只保存在集群主节点的内存里，执行者是“实际在跑什么”的唯一来源，换主后由新集群主节点根据各节点的上报重建任务表；task_id 改为 UUID 字符串；协议版本升为 2。对应的取舍见 5.0。

---

## 1 术语

"主节点"在本系统里有三种不同的含义。**文档、代码和日志中都禁止只写"主节点"**，必须用下表左列的全称。

| 术语 | 含义 | 代码中的名字 |
|---|---|---|
| **集群主节点** | 通过广播选主选出的节点（5.1），负责运行控制面：调度、对账、任务表、对外的任务接口 | `leader` |
| **任期** | 每选出一次新的集群主节点就加 1 | `term` |
| **任务主节点** | 执行某个任务、并负责协调辅助节点的节点（原架构图中的节点 A） | `primary` |
| **辅助节点** | 协同任务中由任务主节点调用的其他节点 | `helper` |
| **主视角** | PKCT 协同跟踪选举（`ElectPrimary`）选出的设备，属于算法层的概念 | `primary_drone_id` |
| **执行代次** | `(epoch, assign_term)`，按字典序比较，用来隔离新旧执行会话。`epoch` 在任务每次（重新）调度时加 1；`assign_term` 是做这次调度的集群主节点的任期 | `epoch`、`assign_term`（代码中合称 `Generation`） |
| **工作单元** | 算法处理的最小单位，目前是一帧 | `frame_id` |
| **任务表** | 集群主节点内存中的任务清单：应该有哪些任务、由谁执行（5.2） | `TaskTable` |
| **控制面 / 数据面** | 控制面只在集群主节点上运行；数据面（执行器和推理服务）在所有节点上运行 | — |

`term` 和执行代次的区别：`term` 防止**过期的集群主节点**继续发出命令；执行代次防止**过期的执行会话**继续上报结果。例如任务主节点掉线后任务被重新调度，`epoch` 加 1，但 `term` 不变。执行代次带上 `assign_term`，是因为两个集群主节点短暂并存、都重新调度了同一个任务时，只比 `epoch` 会得到两份都叫 epoch 2 的副本；带上任期后是 `(2, 4)` 和 `(2, 5)`，新旧可分。

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
- 节点名 `NODE_NAME` 由部署配置决定，**在集群内必须唯一**。不需要配置成员名单：节点之间通过 DDS 自动发现。
- 命名空间 `<ns>` 由 `NODE_NAME` 推导，规则沿用复用包的 `naming.py`：非 `[A-Za-z0-9_]` 字符替换为 `_`，连续的 `_` 合并为一个，空串或以数字开头时在前面加 `n`。例如 `edge-01` 推导为 `/edge_01`。

### 2.2 通信端点

| 名称 | 类型 | 方向 | 说明 |
|---|---|---|---|
| `/cluster/node_status` | `NodeStatus` topic | 所有节点 → 所有节点 | 资源、心跳、选主声明、链路、正在运行的任务 |
| `/cluster/task_report` | `TaskReport` topic | 执行节点 → 集群主节点 | 执行状态变化 |
| `/cluster/task_status` | `TaskStatus` topic | 集群主节点 → 所有节点 | 任务表变化，供网关生成事件流 |
| `<ns>/api/task/create\|destroy\|query` | `CreateTask` 等 srv | 任何调用方 → 本节点网关 | 对外入口；不是集群主节点时转发给它 |
| `<ns>/leader/task/create\|destroy\|query` | 同上 | 其他节点的网关 → 集群主节点 | 只由集群主节点处理，**不再转发**，避免请求在节点之间来回转发 |
| `<ns>/exec/task/start\|stop` | `StartTask`/`StopTask` srv | 集群主节点 → 执行节点 | 内部接口，其他程序不得调用 |
| `<ns>/exec/status` | `ExecutorStatus` topic | 数据面进程 → 本节点控制进程 | 正在运行的任务、算法是否就绪 |
| `<ns>/algorithms/pkci/*`、`pkct/*` | 复用包原有 srv | 执行节点 → 推理服务 | 不改 |
| `<ns>/camera/image` | `sensor_msgs/Image` | 取图 → 执行节点 | 不改 |
| `<ns>/input/airsim/<vehicle>/cam_<camera>` | `sensor_msgs/Image` | 按需取图 → 本节点执行器 | 有任务使用 `airsim://` 输入时才存在（4.4） |
| `<ns>/<topic>/compressed` | `sensor_msgs/CompressedImage` | 数据所在节点 → 其他参与节点 | JPEG；只在有订阅者时才编码（4.4） |
| `<ns>/task/result`、`result_image` | `TaskResult` / `Image` | 执行节点 → 汇总方、网关 | `TaskResult` 新增了字段 |
| `http://<节点>:18090/api/ue/*` | HTTP/JSON | UE → 任意节点 | 沿用复用包的 `ue_api.py`；task_id 改为 UUID 字符串 |

### 2.3 字段和常量

- 字段名必须用 `snake_case`，常量必须用 `UPPER_SNAKE`（这是 ROS 2 的语法要求）。
- 枚举值必须在接口文件中定义为常量，代码里通过常量引用，不要手写字符串。例如写 `TaskStatus.RUNNING`，不要写 `"Running"`。
- 纯逻辑模块（`cccp_node/core/`）不能 import 接口包，所以这些常量在 `core/constants.py` 中镜像定义一份。`test_constants.py` 会直接读取接口文件，两边不一致时测试失败。
- 任务状态的取值沿用复用包的 `Pending`/`Scheduled`/`Running`/`Finished`/`Failed`/`Cancelled`，以保证 UE 侧兼容。

---

## 3 通信

### 3.1 两种通信方式的分工

| 通信方式 | 用途 |
|---|---|
| ROS 2 | 节点之间的全部通信，包括选主（见 2.2） |
| HTTP | 只用于 UE 与节点之间；节点之间禁止使用 HTTP |

没有独立的一致性协议，也没有节点之间两两建立的 TCP 连接。对网络的唯一要求是 DDS 能在节点之间正常发现和通信。

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
| `.../input/airsim/...`、`.../compressed` | sensor data 预设 | — | — | 图像流丢一帧没关系，后面的帧会补上 |
| `<ns>/task/result` | RELIABLE | KEEP_LAST(10) | VOLATILE | 沿用复用包 |

QoS 的配置集中定义在 `cccp_node/ros/qos.py`，端点名称集中定义在 `cccp_node/names.py`。各处必须统一引用这两个模块，不允许在各自的代码里临时创建 QoS 或手写端点名。

---

## 4 标识与时间

### 4.1 标识

| 标识 | 生成方式 | 规则 |
|---|---|---|
| `task_id` | 集群主节点在接受任务时生成 UUID（字符串） | 全局唯一，不依赖计数器，换主后也不会重复 |
| `epoch` | 每次（重新）调度时加 1 | 首次调度时为 1；换主后从执行者上报的值继续 |
| `assign_term` | 每次（重新）调度时设为当前任期 | 换主后接收（Adopt）任务时**不变**：接收只是认领，改了执行器会当成新会话而重启任务 |
| `term` | 选主时取“已知最大 term 加 1” | 只增不减 |
| `TaskStatus.seq` | 集群主节点在本任期内的变更序号 | 订阅方按 `(term, seq)` 去重和排序；换主后从 1 重新开始 |
| `incarnation` | 进程启动时的 Unix 毫秒 | 用来判断节点是否重启过 |
| PKCT `tracker_id` | `task-<task_id>-<epoch>-<assign_term>` | 按 `task_id` + 执行代次隔离会话 |

### 4.2 幂等键格式

| 来源 | 格式 | 由谁生成 |
|---|---|---|
| manual | `manual:<客户端ID>:<UUID>` | UE 或前端在每次用户操作时生成一次，重试时沿用同一个键 |
| schedule | `schedule:<规则ID>:<计划触发时刻的 Unix 毫秒>` | 集群主节点上的定时器 |
| chain | `chain:<父任务ID>:<规则名>[:<frame_id>]` | 父任务的任务主节点 |
| event | `event:<节点名>:<事件源>:<事件序号>` | 产生事件的节点 |
| 为空 | `anon:<UUID>` | 网关自动生成（这种情况下无法做到重试去重） |

**在新的一致性模型下（5.3），幂等键是调用方重试时避免重复执行的唯一手段**，调用方必须为每个请求生成键，并在重试时沿用同一个键。

### 4.3 时间

- 超时和耗时必须使用**本地单调时钟**（`time.monotonic()`）计算。
- 消息里的 `stamp` 只能用于显示和记日志，**不得拿不同节点的时间戳做减法**。复用包 `monitor_node` 用接收时间减发送时间来算时延，就违反了这条规则，必须改为 `LinkQuality` 中的回显方式。
- 所有节点仍然应该部署 chrony 做时间同步，这只是为了方便对照各节点的日志，正确性不依赖它。
- 在 JSON 中，纳秒级时间戳必须写成十进制字符串。

### 4.4 任务输入：数据源地址

任务的输入用**数据源地址**（`CreateTask.input`，`StartTask.spec_json` 中的 `input`）表示，由平台按地址取数据。平台不关心数据是什么类型（图像、点云、特征），只关心它在哪里。代码在 `core/input_source.py`。

| 地址 | 含义 | 位置属性 | 参与节点怎么拿到数据 |
|---|---|---|---|
| 空 | 执行节点自己的默认相机（**就是引入地址之前的行为**） | 绑定到本节点 | 订阅本节点的 `camera/image` |
| `airsim://<vehicle>/<camera>` | AirSim 中的某个相机 | **共享**：任何节点都能直接取 | `each` 模式（默认）：每个参与节点各自通过 RPC 取图；`relay` 模式：只有任务主节点取图，辅助节点订阅它转发的压缩流 |
| `ros://<node>/<topic>` | 某个节点上的 topic（真实传感器） | **绑定到该节点** | 该节点直接读本地 topic；其他节点订阅它的 `<topic>/compressed` |

规则：
- **请求里只放地址，不放大块数据。** 大块数据只通过 topic 跨节点传输：发布一次，可以有多个订阅者，并且压缩后再发。小数据（参数、bbox、协同载荷，几 KB 以内）可以直接放进请求。
- **共享数据源**：任务迁移后输入不变，因为新节点按地址自己去取。`airsim://` 取图只在有任务使用时运行，没有任务使用就停止。
- **绑定到节点的数据源**：调度器会给数据所在节点加分（`input_locality_bonus`），优先把任务主节点放在那里；如果数据所在节点离线，任务无法放置。
- 压缩转发只有在其他节点订阅时才进行，否则不会解码，也不会编码。本节点需要对外提供的传感器 topic 由执行器参数 `relay_topics` 配置，默认是 `camera/image`；AirSim 取图的 topic 会自动提供转发。
- 新增一种数据类型时，由算法声明自己需要的消息类型；平台的取数逻辑不用改。目前只实现了图像（`Image` 和 `CompressedImage`）。

---

## 5 一致性规则（必须）

### 5.0 系统的承诺

| | 保证 | 不保证 |
|---|---|---|
| 重复执行 | 同一任务的重复执行**只会短暂存在**，并会被自动清理：执行器拒绝来自旧任期的命令，对账时停掉多余的副本 | 不保证任何时刻都不重复。网络分裂、出现两个集群主节点时，可能短暂存在两份 |
| 正在运行的任务 | 集群主节点挂掉不影响它们，新集群主节点会接手，执行代次保持不变 | 只在集群主节点上运行的任务，会随它一起丢失 |
| 还没跑起来的任务 | 对调用方的回复如实反映结果：返回成功时任务一定已经跑起来了 | 集群主节点挂掉时，还在排队或下发途中的任务会丢失，调用方需要**用同一个幂等键重试** |
| 历史 | 集群主节点在任期内保留终态任务，默认 1 小时 | 换主后历史记录会丢失，需要的话由 UE 或前端保存 |

评审时必须逐条检查 5.1～5.6，任何一条被违反，上面的保证就不成立。

### 5.1 选主

代码在 `core/election.py`。每个节点在 NodeStatus 中广播 `leader_name`、`term`、`is_leader`，集群主节点的每条 NodeStatus 同时也是一次续约。每个节点都按自己看到的视图执行下面的规则：

1. 多个声明之间按 **(支持者数量, term, 节点编号更小)** 依次比较（没有编号时比较名字）。支持者数量 = 在 NodeStatus 中把它报为 `leader_name` 的在线节点数，加上它自己。
2. 集群主节点看到更好的声明时，**立即让位**；看到某个节点上报的 term 比自己大、但对方的声明并不比自己好（例如一个被隔离后独自宣布的节点），就把自己的 term 提升到它之上，**继续担任**。这样执行器的 term 检查仍然认它的命令。
3. 连续 `leader_timeout_s`（默认 1 秒）没有看到任何声明时，候选排名第一的在线节点宣布自己为集群主节点，term = 已知最大 term + 1；其他节点等它宣布。候选按以下顺序排名：① 有资格（推理就绪，电量未知或不低于 `leader_min_battery`）；如果没有任何在线节点有资格，就忽略这一条，避免集群没有集群主节点；② 心跳里上报的 `visible_peers`（该节点判定在线的对端数）更多；③ 节点编号更小。链路良好时大家的 `visible_peers` 相同，规则退化为按编号选。注意，失效的集群主节点要先经过 `node_offline_timeout_s`（5 秒）才会被判定离线，这一步本身就相当于租约到期，所以换主总耗时大约是 6 秒。
4. 已经在任的集群主节点**不会被抢位**：排名更靠前（编号更小、`visible_peers` 更多）的节点后来加入或恢复，不会替换它；一个被隔离过、在隔离期间独自宣布为主的节点，恢复后因为只有自己一个支持者，也会让位。这解决了 Raft 里需要 PreVote 才能解决的问题。
5. 网络分裂恢复后，**节点更多的一侧的集群主节点胜出**；另一侧在分裂期间接受的任务会被接收（5.6），重复的会被停掉。
6. 节点启动后要先等待 `startup_grace_s`，听一下当前有没有集群主节点，再决定是否宣布。

支持者数量是每个节点根据自己的视图计算的，不同节点的视图可能略有差别，所以短时间内可能出现两个集群主节点。5.4 的 term 检查和 5.6 的重复清理，就是为这种情况准备的。

### 5.2 任务表

- 任务表（`core/task_table.py`）只保存在**当前集群主节点的内存里**，由 `control/task_store.py` 加锁保护。**唯一的写入口是 `apply(command)`**，记录不可变。
- 每次以新的 term 当选时，任务表从空开始，在预热期结束后通过 `Adopt` 重建（5.3）。
- 只有集群主节点的控制器可以写入任务表。每个节点的网关都把创建、销毁、查询做成一条请求（`Request`），写明由哪个集群主节点处理（`target_leader`），然后等对应的回复（`Reply`）：自己是预热完毕的集群主节点就交给本机控制器，否则交给它跟随的集群主节点。收到请求的节点不是 `target_leader` 就忽略；是 `target_leader` 但已经不是集群主节点，立即回复 `NOT_LEADER`，**不再转发**。请求在等结果期间只占集群主节点上的一条等待记录，不占线程。
- `Assign`、`Requeue`、`MarkFailed` 都带期望的执行代次 `expected`：只有任务仍处于该代次时才生效，否则返回 `STALE_EPOCH`。这样基于同一份视图做出的两次决策，最多只有一次能成功。`Assign`、`Requeue` 把代次改为 `(epoch + 1, 当前任期)`。
- `Adopt` 只接收比任务表中更新（执行代次更大）的版本，并保持上报的 `assign_term` 不变；如果幂等键已经被另一个任务占用，就拒绝接收。

### 5.3 执行者是事实来源

- **执行者通过 `NodeStatus.tasks`（`TaskState`）上报自己正在运行的每个任务**，包括 task_id、执行代次（`epoch`、`assign_term`）、角色、任务主节点、辅助节点、幂等键、名称和完整的任务规格（`spec_json`）。这是“实际在跑什么”的唯一来源。
- **新集群主节点重建任务表**：等待 `warmup_s`（等于 `node_offline_timeout_s`）收齐所有存活节点的 NodeStatus，然后在第一次对账时接收所有正在运行的任务。预热期间网关返回 `NO_LEADER`，调用方稍后重试。
- **任务何时算被接受**：网关只有在任务已经在执行者上跑起来之后，才返回成功。如果在 `create_wait_timeout_s` 内没有跑起来（还在排队、下发被暂时拒绝后重试中，或者中途换主），就返回失败（`TIMEOUT` / `NO_LEADER` / `NOT_LEADER`），能带 task_id 时带上。网关在 `request_timeout_s` 内等不到回复（集群主节点挂了、回复丢了）也返回 `TIMEOUT`。调用方用同一个幂等键重试；如果任务其实已经跑起来了，重试会直接返回原来的任务（`duplicate=true`）。
- **新任务在下一个控制周期（约 0.1 秒）就调度**，不等每秒一次的对账；对账仍然兜底。
- **先写任务表，后下发**：集群主节点必须先把调度结果写入任务表，再发出启动命令。一条启动命令同时发给任务主节点和所有辅助节点。下发不等待执行器：结果由执行器的事件（`RUNNING`、`REJECTED` 等）异步送回，事件丢了由心跳和对账兜底。这样在当前任期内，任务表总是领先于实际执行情况，对账才有依据。
- **销毁以执行者确认为准**：Cancel 之后立即向当前代次的所有参与者发送停止命令（原因 `cancel`）。当前代次的**在线**参与者都确认停止（停止事件，或者心跳里已经没有这个任务）才回复成功；超过 `destroy_wait_timeout_s` 仍未全部确认，回复 `TIMEOUT`，调用方重试销毁（任务已是 Cancelled，重试会再发一次停止并等待确认）。离线的参与者不等，回来后由对账停掉。销毁已经 Finished/Failed 的任务返回 `INVALID_STATE`，不存在的返回 `NOT_FOUND`。

### 5.4 执行节点检查 `term` 和执行代次

执行节点必须记录自己见过的最大 `term`，以及每个任务当前的执行代次 `(epoch, assign_term)`，并按下表处理（代次按字典序比较）：

| 收到的命令 | 处理 |
|---|---|
| 命令的目标里没有本节点 | 忽略，不回复，也不记录其中的 `term` |
| `term` < 已知的最大 `term` | 拒绝，返回 `STALE_TERM` |
| `StartTask`：同一 `task_id`、同一代次，任务已经在运行 | 返回**成功**（幂等） |
| `StartTask`：同一 `task_id`、更大的代次 | 先停掉旧会话，再按新代次启动 |
| `StartTask`：同一 `task_id`、更小的代次 | 拒绝，返回 `STALE_EPOCH` |
| `StartTask`：本节点**实际**运行的其他任务数（不含同一 `task_id` 的旧会话）已达 `max_tasks_per_node` | 拒绝，返回 `BUSY` |
| `StopTask`：本节点运行的代次不大于命令里的代次 | 停止 |
| `StopTask`：本节点运行的代次比命令里的新 | 拒绝，返回 `STALE_EPOCH`，不停新会话 |
| `StopTask`：任务本来就不在运行 | 返回成功（幂等） |
| `StopTask` 的原因是销毁（cancel），且没有被拒绝 | 无论任务是否在运行，都留一条墓碑：保留 `tombstone_ttl_s`（600 秒），最多 16 条 |

执行器自己做准入检查，是因为调度器按任务表统计的任务数在两个集群主节点并存、刚换主、或有残留副本时可能是错的；执行器按实际运行的任务数检查，是最后一道防线。墓碑让新集群主节点把已被销毁的任务接收为 Cancelled，而不是让它复活。

集群主节点处理 `TaskReport` 时，必须丢弃代次与任务当前代次不同的报告（更小的返回 `STALE_EPOCH`）。汇总方处理 `TaskResult` 时同样按 `(epoch, assign_term)` 丢弃旧代次的结果。

term 检查是出现两个集群主节点时的第一道防线：只要任期较高的集群主节点向某个执行器发过命令，任期较低的集群主节点就再也无法对这个执行器下发或停止任务。

### 5.5 状态机

```
            ┌─────────────── requeue(代次+1) ◄───────────────┐  下发暂时失败，或重新调度时没有可用节点
            ▼                                                │
Pending ──assign(代次+1)───► Scheduled ──report Running──► Running ──report Finished──► Finished
   │                            │  ▲                         │
   │                            │  └──assign(代次+1)─────────┤  任务主节点离线 / 丢失任务 / 迟迟不启动
   ▼                            ▼                            ▼
 Failed                       Failed                       Failed
（Pending 超时，              （下发时返回 INVALID_INPUT     （任务主节点报告 Failed）
  NO_FEASIBLE_NODE）           等永久错误：DISPATCH_FAILED）

（任务表不知道的运行中任务）──adopt──► Running        任何非终态 ──destroy──► Cancelled
终态：Finished / Failed / Cancelled，之后不再变化
```

- 定义状态序：Pending=0，Scheduled=1，Running=2，终态=3。`(epoch, assign_term, 状态序)` 按字典序**只增不减**；如果一次更新会让它变小，任务表必须忽略这次更新。
- 重新调度和 `Requeue` 都会让状态“往回走”，但它们同时会把 `epoch` 加 1，所以整体仍然是增大的。
- 待调度的任务按 `priority`（任务规格里的预留字段，0～255，越大越重要，默认 0）从高到低、相同时按提交先后排列。当前只影响这个顺序，不影响过滤、评分，也不抢占。
- 下发失败的处理：`INVALID_INPUT`、`UNSUPPORTED_VERSION` 属于永久错误，直接判定 Failed；其他错误（超时、`BUSY`、`NOT_READY`）执行 `Requeue`，并在 60 秒内避开这个节点；收到 `STALE_TERM` 说明自己已经不是集群主节点，什么也不做。
- **运行中没有输入**：任务主节点超过 `input_timeout`（10 秒）收不到一帧，执行器报告 `Failed`（`NOT_READY`）并停掉任务。集群主节点不把它记为终态，而是执行 `Requeue`，并在 60 秒内避开这个节点（换个节点也许能取到数据）；同一个任务累计 `max_input_stalls`（3）个任务主节点都没有输入，或者任务用 `request_node` 指定了节点，才判定 Failed（`NOT_READY`）。辅助节点不做这项检查。计数只在集群主节点内存里，换主后重新计。
- Pending 只存在于集群主节点的内存里，`pending_timeout_s`（8 秒）比网关的等待时间（10 秒）短，这样调用方总能得到明确的结果。
- **`request_node` 是硬性指定**：被指定的节点挂掉后，任务不会迁移到其他节点。不指定节点的任务则会迁移。

### 5.6 对账（集群主节点每秒执行一次）

对账规则（`core/reconciler.py`）：

| 观察到的情况 | 动作 |
|---|---|
| 某节点运行着任务表**不知道**的任务，或者执行代次**比任务表新**。“某节点”包括在线节点，以及 `offline_retain_s` 内离线节点最后一份 `TaskList` | **接收**（`Adopt`），完整规格取自该节点的 `TaskList`；`TaskList` 还没追上心跳的节点这一轮跳过（任务表不知道的副本也先不停）。同一任务有多份副本时，取代次最大的；代次相同时，优先取任务主节点上的那份；再相同就取节点名最小的。任何节点的 `TaskList` 里有这个任务的墓碑，就接收为 Cancelled。其余副本在下一轮被停掉 |
| 同上，但这个任务的幂等键已经属于任务表里的另一个任务 | 判定为重复，向该节点发送 `StopTask`（原因 `duplicate`） |
| Pending | 按优先级从高到低调度；超过 `pending_timeout_s` 仍无可用节点，判定 Failed |
| 任务主节点离线（超过 `node_offline_timeout_s` 没有收到它的 `NodeStatus`） | 重新调度：代次变为 `(epoch + 1, 当前任期)`。**大面积离线保护生效时暂停**（见下文） |
| 任务主节点在线，但 `executor_alive=false` 已超过 `executor_stall_grace_s`（10 秒） | 重新调度。执行器卡住期间心跳保留最后已知的任务列表，所以下面“丢失任务”那一行不会因此触发 |
| 节点在线，上报的任务里有它且执行代次一致 | 正常；如果任务表里还是 Scheduled，说明 `Running` 报告丢了，由集群主节点代为提交 `ReportStatus(Running)` |
| 节点（主或辅）在线但没有这个任务，任务状态是 Scheduled | 每隔 `dispatch_retry_s` 用同一个代次重发 `StartTask`（幂等）；超过 `start_timeout_s` 仍未启动，重新调度 |
| 任务主节点在线但没有这个任务，任务状态是 Running | 先等待 `missing_grace_s`（`NodeStatus` 可能比 `TaskReport` 晚到）；仍然没有就重新调度 |
| 某节点运行的任务在任务表中是 Cancelled | 每隔 `stop_retry_s` 发送 `StopTask`（原因 `cancel`，执行器留墓碑） |
| 某节点运行的任务已经是其他终态、执行代次比任务表旧，或者并没有分配给它 | 每隔 `stop_retry_s` 发送 `StopTask`（原因 `stale` 或 `unassigned`） |

**大面积离线保护**：集群主节点自己的链路变差时，“看不到很多节点”和“很多节点挂了”在它看来是一样的，照常迁移会把任务塞进少数还看得见的节点，而原副本还在正常运行。所以在 `mass_offline_window_s`（10 秒）内新判定离线的节点数超过窗口开始时在线节点数的 `mass_offline_ratio`（1/3）时，暂停“任务主节点离线 → 重新调度”这一条，其他动作照常。这些节点回来到阈值以下时退出；持续 `mass_offline_hold_s`（30 秒）仍未恢复也退出，恢复迁移。进入和退出都记 WARNING 日志。

- 只有任务主节点的副本时，接收的任务就以它为准；如果任务主节点已经挂了、只剩辅助节点的副本，也会照样接收，下一轮对账发现任务主节点离线，再重新调度。这样，迁移途中的任务不会因为换主而丢失。
- “节点在线”和各种计时一律使用集群主节点本地的单调时钟，只保存在当前集群主节点的内存里；换主后会从头开始计时，结果只是决策晚一点，不会导致非幂等的操作被重复执行。
- MVP 阶段不做辅助节点的替换。辅助节点失效后，按 `min_participants` 的规定降质继续运行，或者判定任务失败。

### 5.7 默认参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `node_status_period_s` | 1.0 | NodeStatus 发布周期，也就是集群主节点的续约周期 |
| `node_offline_timeout_s` | 5.0 | 连续这么久收不到 NodeStatus，就判定节点离线；也用作选主的启动等待时间和集群主节点的预热时间 |
| `leader_timeout_s` | 1.0 | 集群主节点被判定离线后，再过这么久，由下一个节点宣布接任 |
| `dispatch_timeout_s` | 5.0 | 协议 2 过渡期：网关向集群主节点发请求前等待其服务出现的时间（协议 3 删除） |
| `dispatch_retry_s` | 5.0 | 未启动的任务多久重发一次 `StartTask` |
| `start_timeout_s` | 15.0 | Scheduled 这么久仍未在任务主节点上启动，就重新调度 |
| `missing_grace_s` | 3.0 | Running 的任务从任务主节点的上报中消失多久后重新调度 |
| `stop_retry_s` | 5.0 | 多余的副本多久重发一次 `StopTask` |
| `create_wait_timeout_s` | 10.0 | 集群主节点为创建请求等待任务跑起来的最长时间 |
| `destroy_wait_timeout_s` | 10.0 | 集群主节点为销毁请求等待参与者确认停止的最长时间 |
| `request_timeout_s` | 15.0 | 网关等待回复的最长时间（等待时间 10 秒加 5 秒余量） |
| `pending_timeout_s` | 8.0 | Pending 持续这么久仍无可用节点，就判定 Failed；必须小于 `create_wait_timeout_s` |
| `avoid_failed_node_s` | 60.0 | 某节点启动任务失败（或运行中没有输入）后，在这段时间内调度该任务时避开它 |
| `max_input_stalls` | 3 | 累计这么多个任务主节点报告没有输入后，判定任务 Failed（`NOT_READY`） |
| `input_timeout`（执行器） | 10.0 | 任务主节点多久收不到一帧就报告 `NOT_READY`；0 表示不检查 |
| `prune_terminal_after_s` | 3600 | 终态任务保留这么久后从任务表删除 |
| `max_tasks_per_node` | 4 | 单节点同时运行的任务上限（与 PKCT 的 `MAX_TRACKERS` 一致）；调度器过滤和执行器准入（执行器参数同名）都用它，两边必须一致 |
| `offline_retain_s` | 300 | 离线节点在集群视图里的条目（含 `TaskList`）保留多久，供新集群主节点接收它的任务 |
| `executor_stall_grace_s` | 10.0 | 任务主节点的 `executor_alive=false` 持续这么久，才迁移它的任务 |
| `mass_offline_window_s` / `mass_offline_ratio` / `mass_offline_hold_s` | 10.0 / 1/3 / 30.0 | 大面积离线保护（5.6） |
| `leader_min_battery` | 0（不启用） | 选主候选的电量下限（5.1），取值待定 |
| `tombstone_ttl_s`（执行器） | 600 | 墓碑保留多久；最多 16 条，超出时丢弃最旧的 |
| `max_chain_depth` | 5 | 链式触发的最大深度 |
| `leader_self_penalty` | 10 | 集群主节点给自己的调度评分扣分（满分 100）。另一个作用是减少“任务只跑在集群主节点上、随它一起丢失”的情况 |
| `per_task_penalty` | 5 | 节点上每个已分配任务的调度扣分，避免 CPU 使用率还没反映出新任务时，任务全部挤到同一个节点 |
| `input_locality_bonus` | 15 | 数据绑定到某个节点时，给该节点的调度加分 |

上表的超时值都需要在真实自组网上实测后调整，调整结果要回填到本表。

---

## 6 进程与代码组织

### 6.1 每个节点运行三个进程

| 进程 | 包含 | 原因 |
|---|---|---|
| 控制进程 | 选主、控制面和任务表（只在集群主节点上启用）、网关（ROS 2 + UE HTTP）、资源监控 | 必须保持轻量，否则 NodeStatus 发不出去，节点可能被判定离线，引发换主 |
| 数据面进程 | 执行器、PKCI、PKCT | 推理和图像处理很耗 CPU，受 GIL 影响，不能和控制进程放在一起 |
| 取图进程 | `airsim_camera` | 沿用复用包 |

复用包在每块板卡上运行 5 个进程。改成 3 个之后，DDS 参与者的数量从 60 个降到 36 个。

- 控制进程的入口是 `ros2 run cccp_node control_node`；数据面进程的入口是 `ros2 run cccp_task_executor data_plane`，它用一个 `MultiThreadedExecutor` 同时运行执行器、PKCI、PKCT 三个节点；启动脚本是 `deploy/start_node.sh`。
- 两个进程之间通过 `<ns>/exec/status` 传递信息：控制进程根据它填写 `NodeStatus` 里的 `tasks` 和 `inference_ready`。
- 调用方式的选择：**同一进程内直接调用函数**（执行器 → PKCI/PKCT 引擎，`INFERENCE_BACKEND=local`，默认值）；**跨节点的低频调用用 ROS 2 Service**（`StartTask`/`StopTask`、转发给集群主节点的请求）；**跨节点的逐帧数据用 Topic**（`TaskResult` 和其中的协同载荷）。禁止跨节点逐帧传输图像。推理引擎仍然以 `<ns>/algorithms/*` 服务的形式对外提供，设置 `INFERENCE_BACKEND=ros` 可以让执行器改走服务调用。

### 6.2 业务逻辑与 ROS 解耦

- 选主、调度、任务表、对账、链路估计这几部分必须写成**不 import rclpy 的纯 Python 模块**（`core/`），ROS 节点只做一层薄的适配。`test_core_is_ros_free.py` 会检查这一点。
- 这样做有两个好处：单元测试不需要 ROS 环境；以后转 C++ 时可以逐个模块对照翻译。
- `control/` 层（控制器、网关、UE 后端）也不依赖 ROS：下发命令、发送请求都通过接口（`CommandSink`、`RequestTransport`）注入，时钟也是注入的；节点之间交换的消息在 `control/protocol.py` 里有不依赖 ROS 的对应类型。

### 6.3 Python

- 版本：Python 3.10（Ubuntu 22.04 / ROS 2 Humble 自带的版本），不得使用 3.11 及以上版本才有的语法。
- 公共函数必须写类型标注。
- 代码格式和静态检查应使用 `ruff format` 和 `ruff check`。

### 6.4 目录结构

```
cccp_cluster/
├── docs/CONVENTIONS.md                 本文
├── docs/DESIGN.md                      设计文档：为什么这样设计、系统怎么运转
├── docs/HANDOFF.md                     进度、验证情况、待决策事项
├── ros2_ws/src/
│   ├── cccp_cluster_interfaces/        集群层接口：NodeStatus、TaskState、LinkQuality、ExecutorStatus
│   ├── cccp_task_interfaces/           任务层接口（从复用包 v0.1 演进而来）
│   ├── collab_inference_interfaces/    从复用包原样复制
│   ├── collab_inference/               推理服务；engines.py 让 ROS 服务和进程内调用共用同一份逻辑
│   ├── cccp_task_executor/             数据面：执行器、data_plane 组合入口、AirSim 按需取图、压缩转发
│   └── cccp_node/                      控制进程
│       └── cccp_node/
│           ├── core/                   纯逻辑：选主、任务表、调度、对账、链路估计、数据源地址（禁止 import ROS）
│           ├── control/                集群主节点控制器、任务表的存储、网关、UE 后端（不依赖 ROS）
│           ├── monitor/                CPU/内存/GPU 采集
│           ├── ros/                    ROS 适配层：control_node、dispatcher、forwarder、convert、qos
│           ├── names.py                端点名称
│           └── ue_api.py               UE HTTP 接口
├── deploy/                             Dockerfile（x86/ARM64）、start_node.sh
└── tools/                              接口语法检查、推理测速
```

---

## 7 错误与日志

### 7.1 服务返回

- 服务回调中禁止把异常抛到 ROS 层，必须捕获异常，并返回 `success=false`、`error_code` 和可读的 `message`。这与复用包的做法一致。
- `error_code` 必须使用 `ErrorCodes.msg` 中定义的常量；`message` 用来给人看，程序不得解析 `message` 的内容。

### 7.2 错误码与重试

| 错误码 | 调用方能否重试 | 说明 |
|---|---|---|
| `NO_LEADER`、`NOT_LEADER`、`BUSY`、`NOT_READY` | 可以，退避后重试 | 临时状态，例如正在选主、新集群主节点还在预热，或者请求发给的集群主节点刚让位 |
| `TIMEOUT` | 可以，但**必须使用同一个幂等键** | 超时不代表对方没有执行；CreateTask 超时时，响应里会带上 task_id |
| `DISPATCH_FAILED` | 由集群主节点决定 | 已经回滚 |
| `STALE_TERM`、`STALE_EPOCH` | 不可以 | 发出命令的一方已经过期，应当停止 |
| `INVALID_INPUT`、`INVALID_STATE`、`NOT_FOUND`、`CHAIN_TOO_DEEP`、`UNSUPPORTED_VERSION` | 不可以 | 请求本身有问题 |
| `NO_FEASIBLE_NODE`、`INSUFFICIENT_PEERS` | 由调用方决定 | 资源不足 |
| `INFERENCE_ERROR`、`FUSION_ERROR` | 不由调用方重试 | 由执行器累计失败次数，超过上限后报告 Failed |
| `CANCELLED` | 不可以 | 任务已被销毁 |

UE HTTP 接口的状态码必须与上表一致（`ue_api.py`）：可以重试的错误（`NO_LEADER`、`NOT_LEADER`、`BUSY`、`NOT_READY`、`TIMEOUT`）一律返回 **503**；创建请求本身有问题（没有生成 task_id）返回 400；任务已生成但失败返回 502；任务不存在返回 404；销毁已结束的任务返回 409。不得把可以重试的错误映射成 4xx，否则 UE 会放弃重试。

### 7.3 日志

- 与任务相关的日志行必须带上 `key=value` 格式的字段：`task=<id> gen=(<epoch>,<assign_term>) term=<t> node=<名称>`（`gen` 的值里没有空格），有工作单元时再加 `frame=<id>`。
- 集群主节点每次上任和卸任都必须记录一条 INFO 日志，内容包括 `term`；接收任务（adopt）时也要记录。
- 高频出现的警告必须限流（`throttle_duration_sec`）。

---

## 8 接口变更流程

- 接口文件是平台组、算法组和 UE 之间的契约，任何改动都必须经过评审。
- ROS 2 Humble 不支持类型演进。**`cccp_*` 接口包的任何改动，都会让新旧版本的节点无法互通**。因此：
  1. 每次改动都要把 `NodeStatus.PROTOCOL_VERSION` 加 1（当前是 2），同时更新对应 `package.xml` 中的版本号；
  2. 12 个节点必须一起升级；协议版本不同的节点不参与选主，也不会被派发任务；
  3. 镜像标签必须带上协议版本号，例如 `cccp-node:p2-<git短哈希>`。
- 每个接口文件开头的注释里，必须写明与上一版相比改了什么。
- 不得改变已有字段的含义。如果需要新含义，就新增一个字段。

---

## 9 测试

| 层级 | 内容 | 环境 |
|---|---|---|
| 单元测试 | 选主、任务表、调度评分、对账、幂等、链路估计 | pytest，不需要 ROS |
| 仿 ROS 集成 | 3 个控制节点 + 3 个执行器，通过 `test/fake_ros.py` 在一个进程里运行 | pytest，不需要 ROS |
| 接口检查 | `tools/check_interfaces.py`、`colcon build` | CI |
| 单机集成 | 3～5 个容器模拟多节点，跑完整链路 | 在同一台主机上用 Docker 网络 |
| 故障注入 | 见下表 | 单机集成环境，用 `tc netem` 模拟丢包和延迟 |
| 现场测试 | 12 块板卡加真实自组网：验证 DDS 发现、选主稳定性和流量 | 不能用容器测试代替 |

故障注入的必测项：

| 场景 | 通过条件 |
|---|---|
| 杀掉集群主节点（它没有运行任务） | 约 6 秒内选出新集群主节点，`term` 加 1；正在运行的任务被接收，执行代次不变，没有被重启，也没有被重复执行 |
| 杀掉集群主节点（它正在运行任务） | 这些任务丢失；调用方用同一个幂等键重试后，任务重新跑起来 |
| 杀掉任务主节点 | 在 `node_offline_timeout_s` 之后，任务被重新调度，`epoch` 加 1 |
| 执行进程重启 | 对账后任务恢复 |
| 重复发送 `StartTask` | 第二次返回成功，任务不会启动两次 |
| 用旧 `term` 发送 `StartTask` / `StopTask` | 返回 `STALE_TERM` |
| 用同一个幂等键提交两次 `CreateTask`（包括换主前后各提交一次） | 返回同一个 `task_id`，并且 `duplicate=true` |
| 网络分裂，两边各自选出集群主节点，并接受了同一个请求 | 恢复连通后（或者高任期一方已经接触到这些执行器时），只剩一份副本 |
| 某个节点丢包率达到 30% | 不会被误判为离线，也不会引发频繁换主 |
| 隔离一个节点 30 秒后恢复 | 它不会抢回集群主节点的位置 |
