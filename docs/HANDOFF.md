# 交接说明：MVP 第一版代码

2026-10-27｜面向：平台组三位成员

> **10-29 更新：架构已从 Raft 改为“广播选主 + 执行者为事实来源”，见第十三节。** 第一～十二节中凡是涉及 Raft、PySyncObj、`cluster.yaml`、整数 task_id 的内容都已过时，保留它们只是为了记录决策过程。

## 一、结论

按《开发规范》写完了 MVP 的全部代码：任务表、调度、对账、共识层、集群主节点控制器、网关、UE 接口、资源采集、执行器改造、部署脚本。**本机能验证的部分都验证过了**，共 141 个测试全部通过。其中包括一个“仿 ROS”的集成测试：3 个控制节点、3 个执行器，Raft 用真实的 PySyncObj，覆盖任务从创建到运行再到集群主节点挂掉、任务被接管的全过程。

**本机没有 ROS 2 Humble，也没有 docker 权限，所以 `colcon build`、真实 DDS、真实推理模型都还没跑过。** 这是下一步要做的第一件事。

最需要你们先看的三件事：

1. **第七节第 1 条：任务迁移后，输入数据其实变了。** 这是架构层面的问题，会影响“任务主节点挂了就迁移”这条设计是否成立。
2. **第六节：我自行做的 12 个决定**，需要逐条确认。
3. 实现过程中发现并修复的两个换主问题，都已写进规范 5.6：
   - 新主节点上任后必须先提交 `Barrier` 命令，否则会把正在运行的任务全部停掉。这是 12 节点实测发现的。
   - 换主期间如果执行节点的 `Running` 报告丢失，任务会永远停在 Scheduled，虽然实际上已经在运行。现在对账时会根据 `NodeStatus` 补交这条报告。这是自查代码时发现的。

---

## 二、怎么运行

**没有 ROS 的机器上（Python 3.10）：**

```bash
pip install pytest pysyncobj==0.3.17 pyyaml numpy pillow
cd ros2_ws/src/cccp_node            # 必须在这个目录下运行
python -m pytest test               # 141 个测试，约 30 秒
python3 ../../../tools/check_interfaces.py ../   # 检查所有 .msg/.srv 文件的语法
```

ROS 层的集成测试（`test_ros_integration.py`）会自动安装 `test/fake_ros.py` 作为 rclpy 的替身。如果机器上装了真实的 rclpy，这个测试会自动跳过。

**有 Humble 的机器上（还没验证）：**

```bash
cd ros2_ws && colcon build --packages-up-to cccp_node cccp_task_executor collab_inference
colcon test --packages-select cccp_node cccp_task_executor && colcon test-result --verbose
```

**单节点启动（还没验证）：** 参照 `deploy/Dockerfile` 开头的注释，把 `deploy/cluster.example.yaml` 改成实际的成员表后挂载进容器，再设置 `NODE_NAME`。

---

## 三、完成清单

| 模块 | 文件 | 对应规范 | 验证方式 |
|---|---|---|---|
| 任务表状态机 | `core/task_table.py` | 5.1、5.2、5.5 | 单元测试，含 5000 步随机历史测试；故意制造的 5 种 bug 都能被测试发现 |
| 任务规格与校验 | `core/task_spec.py` | 1、4.2 | 单元测试 |
| 调度器（从 Java 移植） | `core/scheduler.py` | 5.7 | 单元测试；用 Java 公式手算的数值做对照 |
| 对账器 | `core/reconciler.py` | 5.6 | 单元测试 |
| 执行节点的 term/epoch 检查 | `core/execution_guard.py` | 5.4 | 单元测试 + 集成测试 |
| 集群视图、链路估计 | `core/cluster_view.py`、`core/link_estimator.py` | 4.3 | 单元测试（RTT 回显、丢包、节点重启） |
| 共识层 | `consensus/`（接口、单机实现、PySyncObj 实现） | 3.1、6.2 | 3 节点真实 Raft 测试，连跑 5 次都通过；12 节点换主实测 |
| 集群主节点控制器 | `control/controller.py` | 5.3、5.6 | 仿真集群测试 14 项 + 集成测试 |
| 网关、UE 后端 | `control/gateway.py`、`control/ue_backend.py`、`ue_api.py` | 2.2 | 单元测试，真实 HTTP 请求 |
| 资源采集 | `monitor/probes.py` | — | 单元测试；在本机（RTX 4090）实测采集正常 |
| ROS 适配层 | `ros/control_node.py`、`dispatcher.py`、`convert.py`、`qos.py` | 3.3 | 仿 ROS 集成测试 + ruff 静态检查 |
| 执行器改造 | `cccp_task_executor/executor_node.py`、`data_plane.py` | 5.4、6.1 | 仿 ROS 集成测试；真实推理还没跑过 |
| 接口 | `cccp_cluster_interfaces`（新增 `ExecutorStatus`）、`cccp_task_interfaces` | 8 | `tools/check_interfaces.py` |
| 部署 | `deploy/`（Dockerfile ×2、`start_node.sh`、`cluster.example.yaml`） | 6.1 | 只做了 shell 语法检查；镜像还没构建过 |

---

## 四、验证到什么程度

| 级别 | 覆盖了什么 |
|---|---|
| ✅ 单元测试 | 第三节所有的纯逻辑模块 |
| ✅ 真实 Raft | 3 节点：选举、从节点转发、幂等、各副本事件一致、换主、少数派无法写入；12 节点：换主耗时（见第五节） |
| ✅ 仿 ROS 集成 | 3 个控制节点 + 3 个执行器 + 假的算法服务 + 真实 PySyncObj。覆盖：创建任务 → 运行 → 逐帧结果送到另一个节点的 UE 后端 → 杀掉集群主节点后只有一个节点在跑这个任务 → 杀掉任务主节点后任务迁移，epoch 变为 2，PKCT 会话 ID 跟着变 → 销毁任务。消息类按 `.msg` 文件生成，字段名和类型都像 rosidl 一样严格检查 |
| ⚠️ 没验证 | `colcon build`、真实 DDS（发现、QoS、丢包）、在 `data_plane` 组合进程里加载真实模型、Jetson 上的 GPU sysfs 路径（`monitor/probes.py` 里的 `JETSON_LOAD_PATHS` 需要在 Orin 上确认）、Docker 镜像构建、UE 联调 |

**关于偶发失败**：集成测试大约每 30 次会失败 1 次，日志抓到后查明是**测试本身的竞态**，不是业务逻辑的问题。`TaskStatus` 事件是异步送达的，而测试在事件到达之前就检查了事件流；仿 ROS 环境里每个节点每 0.2 秒还要调用一次 `nvidia-smi`，进一步拖慢了事件的发布。现在测试改为等待事件到达，并把资源采集换成固定值。修复后的循环测试结果见第十节。

**仿 ROS 不能代替真实 ROS**：它同步、无丢包地投递消息，也不模拟 QoS。它能发现的是字段名写错、类型不对、组件之间接线错误、线程交接方面的问题；DDS 层面的问题它发现不了。

---

## 五、实测数据

数据来自本机 localhost，不是自组网，只能作为下限参考。

| 项目 | 结果 | 条件 |
|---|---|---|
| 首次选主 | 约 1.6 s | 12 节点，生产参数（心跳 0.3 s，选举超时 1.5～3 s） |
| 换主（选出新的集群主节点） | 中位数 3.3 s，最长 4.2 s | 12 节点，连续杀 5 次集群主节点 |
| 换主后追上任务表（Barrier 提交完成） | 比选出新主再多约 0.3 s | 同上 |
| 一次提交的延迟 | 约 316 ms | PySyncObj 会把命令攒到下一次心跳再发送，所以延迟约等于心跳周期 |
| 新主节点刚当选时能否查到已提交的任务 | 5 次里有 1 次**查不到** | 这就是必须先提交 Barrier 的原因 |

---

## 六、我自行做的决定（请评审）

| # | 决定 | 原因 | 不同意的话要改哪里 |
|---|---|---|---|
| 1 | 任务表只有一个写入口 `apply(command)`，命令序列化后就是 Raft 日志条目 | 换 Raft 库或转 C++ 时，日志格式和业务逻辑都不用动 | `core/task_table.py` |
| 2 | `Assign`/`Requeue`/`MarkFailed` 都带 `expected_epoch`，按“比较后再修改”的方式执行 | 基于同一份快照做出的两次决策，最多只有一次生效 | 同上 |
| 3 | 新主节点上任后先提交 `Barrier`，再等待 5 s 预热，然后才开始对账 | 见第五节最后一行 | `control/controller.py` |
| 4 | 下发失败时：永久错误直接判定 Failed；其他错误执行 Requeue，并在 60 s 内避开这个节点 | CCCP 原来是一失败就整个任务失败，在自组网里太脆弱 | `controller._start` |
| 5 | `request_node` 是硬性指定，被指定的节点挂掉后任务不迁移 | 见第七节第 1 条 | `core/scheduler.py` 的 `schedule()` |
| 6 | 每个已分配的任务扣 5 分，每个节点最多 4 个任务 | CPU 使用率要过几秒才反映出新任务，这期间所有任务都会被派到同一个节点 | `SchedulerConfig` |
| 7 | 网络分以 `input_node` 为参照点，没填时以集群主节点为参照点；时延、抖动、丢包三项惩罚都乘以 `latency_sensitivity`；带宽项去掉 | `CreateTask` 只有一个敏感度字段，带宽也没有测量 | `core/scheduler.py` |
| 8 | 新增 `ExecutorStatus` 消息，用于数据面进程把正在运行的任务告诉控制进程 | 两个进程之间需要传递这个信息 | `cccp_cluster_interfaces` |
| 9 | Jetson 的 `gpu_mem_total_mb` 填 0（CCCP 原来填的是系统内存大小） | 调度器靠这个字段区分独立显卡和共享内存 GPU | `monitor/probes.py` |
| 10 | 网关只有在 UE 问到某个任务时，才订阅它的逐帧结果 | 如果每个节点都订阅全部结果，无线链路上的流量会增加约 8 Mbps | `control/ue_backend.py` |
| 11 | `CreateTask` 响应里的 `candidate_nodes` 暂时为空 | 候选列表是集群主节点算出来的，没有写进任务表 | `controller._place` |
| 12 | `TaskResult.valid` 暂时直接等于 `success` | 算法层还没有定义“有效性”的判断规则 | `executor_node.py` |

---

## 七、待决策和已知问题

1. **任务迁移后，输入数据其实变了（最重要）。** 每块板卡读的是自己那架无人机的相机（`AIRSIM_VEHICLE` 按板卡配置）。任务主节点挂掉后，任务迁移到另一块板卡，处理的就是另一架无人机的画面。对 PKCT 跟踪来说，原来的目标框在新画面里没有意义。需要决定：
   - 是否只有“输入不依赖具体节点”的任务才允许迁移；
   - 还是输入改为按 `input` 字段统一取（比如任何节点都能通过 AirSim RPC 取指定无人机的画面）。

   目前的折中是：`request_node` 指定的任务不迁移，不指定的任务会迁移。
2. **PySyncObj 不支持 PreVote**：节点短暂断网再恢复后，可能会引发一次不必要的换主。只能在 MVP 阶段接受，转 NuRaft 时解决。
3. **一次提交约 0.3 s**：创建一个任务要经过 Submit、Assign、ReportStatus 三次提交，加起来约 1 s。如果嫌慢，可以把 PySyncObj 的 `appendEntriesUseBatch` 设为 False 来降低延迟，代价是网络包变多。
4. **执行器重启后，记录的最大 term 会归零**：重启后的短暂窗口里，过期的集群主节点发来的命令可能会被接受。概率很低，暂不处理。
5. **还没实现的功能**：辅助节点替换、协同结果汇总（任务主节点按 `frame_id` 收集各节点的载荷后调用 fuse/elect）、定时触发和事件触发、模型并行。
6. **Raft 持久化**：Docker 镜像里默认把 `data_dir` 设为 `/var/lib/cccp`（需要挂载卷）。全集群重启后，任务表里的任务会恢复成 Running 状态，然后由对账器重新调度。需要确认这是不是想要的行为（另一种选择是重启后全部清空）。
7. **12 个节点全部参与投票**：每次提交需要 7 个节点确认。要在实际组网中测一下延迟和换主频率。
8. **UE 只配置了一个节点地址**：UE 侧需要支持配置多个节点地址，连不上一个就换下一个。所有节点的 HTTP 接口都一样。
9. **换主期间 `TaskStatus` 事件可能缺几条**：只有集群主节点发布 `TaskStatus`。旧主节点已应用、但还没来得及发布就挂掉的那几条状态变化不会被补发（`seq` 会出现跳号）。UE 可以通过查询接口拿到最新状态。如果需要不丢事件，可以让新主节点上任时补发 `seq` 缺口之后的状态。
10. **`nvidia-smi` 每秒调用一次**：每次耗时几十毫秒，期间会挡住同一回调组里 `NodeStatus` 的收发（处理时间在 100 ms 以内，影响不大）。以后可以改用 NVML（pynvml）直接读取。Jetson 走 sysfs，没有这个问题。

---

## 八、下一步分工（建议）

| | A：状态与一致性 | B：决策逻辑 | C：执行面、入口与部署 |
|---|---|---|---|
| 第一件事 | 评审第六节第 1～3 条，以及规范第 5 节的改动 | 评审第六节第 5～7 条，以及第七节第 1 条 | 找一台有 Humble 的机器：`colcon build` + `colcon test` |
| 接着做 | 在真实网络上测换主（可参考 `tools/raft_failover_bench.py`），调整超时参数 | 自组网摸底；在 Orin 上确认 GPU sysfs 路径；调整调度权重 | 构建 Docker 镜像，在单机上起 3 个容器跑通；然后接 UE |
| 之后 | 定时触发；PreVote 方案 | 辅助节点替换 | 协同结果汇总（fuse/elect） |

---

## 九、Git 状态

所有改动都**没有提交**，仓库里只有 10-26 23:18 那次提交的接口和规范。工作区的情况：

- 修改了已提交的文件：`README.md`（保留了原标题）、`docs/CONVENTIONS.md`、`NodeStatus.msg`（只改了注释）、`ErrorCodes.msg`、`TaskResult.msg`、`CreateTask.srv`（这三个只改了注释，去掉了对《算法接入规范》的引用）、`cccp_cluster_interfaces/CMakeLists.txt`（加入 `ExecutorStatus`）。
- 新增：`ros2_ws/src/` 下的 `cccp_node`、`cccp_task_executor`、`collab_inference`（含 43 MB 模型权重）、`collab_inference_interfaces`，以及 `ExecutorStatus.msg`、`deploy/`、`tools/`、`docs/HANDOFF.md`。

注意：模型权重 `collab_inference/weights/*.pt`（约 43 MB）直接放进 git 不太合适，建议改用 Git LFS，或者在构建镜像时从别处下载。

## 十、后台循环测试的结果

集成测试（4 个用例）的循环测试结果：

| 阶段 | 次数 | 结果 |
|---|---|---|
| 修复前 | 1～33 次 | 失败 1 次，没有保留日志 |
| 修复前 | 后台循环 31 次 | 第 31 次失败，抓到日志，查明是测试的竞态（见第四节） |
| 修复后 | 后台循环 40 次 | **40 次全部通过** |


## 十一、10-28 追加：本地推理改为直接调用函数，并首次用真实权重跑通

- `collab_inference_ros/engines.py`：把 PKCI/PKCT 的推理逻辑从 ROS 回调中抽出来，形成不依赖 ROS 的引擎类。ROS 服务和本地调用共用同一个引擎实例，模型只加载一份。
- `cccp_task_executor/inference.py`：执行器通过 `InferenceBackend` 访问算法，有两种实现：`LocalBackend` 在进程内直接调用（默认），`RosServiceBackend` 走原来的服务调用。用环境变量 `INFERENCE_BACKEND` 切换。
- 集成测试对两种方式各跑一遍，全部通过。`cccp_node` 共 144 项测试，`collab_inference` 共 9 项测试。
- **第一次用真实权重跑通了 PKCI/PKCT**（`collab_inference/test/test_engines_real.py`）。下表是单帧耗时，测量工具为 `tools/inference_bench.py`，输入是 640×480 的合成图像：

| 设备 | PKCI P50 / P95 | PKCT P50 / P95 |
|---|---|---|
| x86 CPU，8 线程 | 20.5 / 24.9 ms | 24.4 / 26.2 ms |
| x86 CPU，不限制线程（本机有 144 核） | 约 102 ms | 约 147 ms |
| RTX 4090（空闲） | 10.2 / 11.0 ms | 12.1 / 12.9 ms |

两点观察：
- **CPU 线程数必须限制。** 在多核机器上不限制线程，反而会慢 5 倍。Orin 的核数不多，影响应该小一些，但启动脚本里最好显式设置 `OMP_NUM_THREADS`。
- **在 4090 上，GPU 只比 CPU 快一倍左右。** 这说明在强 GPU 上，剩下约 10 ms 的耗时主要花在 CPU 侧的前后处理（裁剪、缩放、编码），而不是模型计算本身。所以在 4090 上上 TensorRT 意义不大。**Orin 的情况必须实测**：它的 GPU 比 4090 弱得多，而且复用包文档提到，NVIDIA 为 Jetson 提供的 torch 2.1 在 **CPU** 上跑 EfficientNet 会输出 NaN，所以 Jetson 上必须使用 `cuda:0`。

## 十二、10-28 追加：任务输入改为数据源地址

背景：之前每块板卡只读自己的相机，所以任务迁移后输入会变；冗余并行也无法保证“各节点处理同一份输入”。现在任务的输入用地址表示，平台按地址取数据，规则见规范 4.4。

| 地址 | 适用场景 | 迁移后输入是否改变 |
|---|---|---|
| 空 | 和以前完全一样，读本节点的相机 | 会变（和以前一样） |
| `airsim://uav_5/0` | 仿真：任何节点都能通过 RPC 取同一路画面 | **不变** |
| `ros://edge-03/camera/image` | 真实部署：传感器接在 edge-03 上，其他节点订阅它的压缩流 | 数据所在节点挂了，数据也就没了，任务会等待，超时后判定失败 |

**改动：**
- 新增 `core/input_source.py`：地址解析，以及决定每个节点订阅什么、是否自己取图。
- 调度器：数据绑定到节点时，优先把任务放在该节点上；该节点离线时，任务无法放置。
- 执行器：按地址订阅输入；遇到 `airsim://` 地址时按需启动取图线程（`airsim_feeds.py`）；需要时把本地图像压缩转发给其他节点（`relay.py`）；收到的压缩帧先解压再推理（`images.py`，只用 PIL，不依赖 cv_bridge）。
- **接口改动**：`StartTask` 新增 `input` 字段；`CreateTask.input` 的含义从“仅作记录”改为数据源地址，字段本身没变。
- 启动脚本：新增 `AIRSIM_FETCH_MODE`（`each` 或 `relay`）。设置了 `AIRSIM_HOST` 后，就可以使用 `airsim://` 输入。

**验证**：在仿 ROS 的 3 节点集成测试里新增了 5 个场景，两种推理调用方式下都通过：
- 绑定到节点的数据源能以压缩流的形式送到辅助节点；
- `each` 模式下每个参与节点各自取图，任务销毁后取图线程会停止；
- `relay` 模式下只有任务主节点取图；
- 任务迁移后，新节点取的仍然是同一个相机；
- 数据所在节点离线时，任务停在 Pending。

我还故意注入了两种 bug（relay 模式下辅助节点也去取图、执行器不解压收到的帧），测试都能发现。`cccp_node` 共 171 项测试全部通过。

**这次改动对第七节第 1 条的影响**：用 `airsim://` 时，问题已经解决；用 `ros://` 时，“数据所在节点挂了，任务就无法继续”是物理事实，平台的处理方式是让任务等待，超时后判定失败。

**仍需你们实测**：AirSim 的 RPC 服务能否支持多个客户端同时取图，以及会不会拖慢 UE 的帧率。如果 `each` 模式下 UE 负载太大，就把 `AIRSIM_FETCH_MODE` 改成 `relay`，代码不用改。

## 十三、10-29：去掉 Raft，改为广播选主，执行者是事实来源

**决策**（三人讨论后确定）：接受“故障时可能短暂重复执行，几秒内自动清理”，换取去掉 Raft。系统的承诺写在规范 5.0 节。

**架构变化：**

| | 之前（Raft） | 现在 |
|---|---|---|
| 选主 | PySyncObj，节点之间两两 TCP 直连，需要静态成员表 | NodeStatus 广播（`core/election.py`），只需要 DDS，节点自动发现 |
| 任务表 | 复制到全部 12 个节点 | 只保存在集群主节点内存里；换主后根据各节点上报的 `TaskState` 重建 |
| task_id | 复制计数器生成的 int64 | UUID 字符串 |
| 非集群主节点收到请求 | 由 Raft 转发 | 网关通过 `leader/task/*` 服务转发，只转发一次 |
| 任务何时算被接受 | 写入多数节点即可 | **必须跑起来才算成功**；否则返回失败和 task_id，调用方用同一个幂等键重试 |
| 部署 | `cluster.yaml`、7480 端口、持久化卷 | 只需要设置 `NODE_NAME` |

**选主规则的一处改进**：多个声明之间先比较支持者数量，再比较 term。这样一来，被隔离后独自宣布为主的节点，恢复连接后无法抢走集群主节点的位置（这在 Raft 中需要 PreVote 才能解决）；网络分裂恢复后，节点多的一侧胜出。这个问题是我写规范时发现的，已修复并加了测试。

**主要改动：**
- 新增：`core/election.py`、`control/task_store.py`、`ros/forwarder.py`、接口 `TaskState.msg`
- 删除：`consensus/`（PySyncObj）、`test_pysyncobj_consensus.py`、`tools/raft_failover_bench.py`、`deploy/cluster.example.yaml`
- 任务表新增 `Adopt` 命令；对账器把未知的、或 epoch 更新的运行中任务**接收进来**，而不是停掉；对幂等键重复的任务，判定为重复并停掉
- 接口：协议版本升为 2；所有 `task_id` 改为字符串；`StartTask` 的规格字段合并为 `spec_json`；`NodeStatus.running_tasks` 改为 `tasks`（`TaskState`）
- **UE 接口有变化**：`task_id` 从整数变为 UUID 字符串（`/api/ue/tasks/<uuid>`），需要通知 UE 侧

**验证：**
- `cccp_node` 共 176 项测试全部通过，其中仿 ROS 的 3 节点集成测试覆盖了：
  - 请求被转发给集群主节点；
  - 集群主节点挂掉后，新集群主节点根据上报重建任务表，任务原地继续运行、没有被重启，用原来的幂等键重试能查到原任务；
  - 任务主节点挂掉后，任务迁移；
  - 集群主节点自己运行的任务随它丢失，用同一个键重新提交后成功；
  - 数据源地址相关的各种场景。
- 新增两个控制器测试，覆盖两个集群主节点同时存在的情况：任期较高的一方把重复副本停掉，任期较低的一方发出的停止命令被执行器拒绝。
- 我对新逻辑注入了 4 种 bug（不接收未知任务、主节点不让位、不检查重复幂等键、比较声明时不看支持者数量），测试都能发现。

**需要你们确认或注意：**
1. UE 侧需要适配 UUID 形式的 task_id，并且**重试时必须带上同一个幂等键**。
2. 集群主节点自己运行的任务会随它一起丢失。`leader_self_penalty` 能减少任务落在集群主节点上的情况，但在节点不够用时无法避免。
3. 换主期间的历史记录（已结束任务的结果）会丢失，需要的话由 UE 或前端保存。

## 十四、10-29：设计文档；UE 接口状态码修正

- 新增 `docs/DESIGN.md`：架构、状态模型、关键流程、选主、故障处理、任务输入、调度、设计决策记录（含 Raft 的来龙去脉）、已知限制。
- **UE 接口状态码修正**：写文档时发现，没有集群主节点时创建任务返回的是 400（因为响应里没有 task_id），会让 UE 误以为请求有错而放弃重试。现在 `NO_LEADER`、`BUSY`、`NOT_READY`、`TIMEOUT` 一律返回 **503**（创建、查询、销毁都一样）。注意：“任务 10 秒内没跑起来”（`TIMEOUT`）原来返回 502，现在也是 503。规则写进了规范 7.2。需要通知 UE 侧：**收到 503 就用同一个幂等键退避重试**。
- 新增测试 `test_no_leader_returns_503_so_ue_retries`，`cccp_node` 共 177 项测试全部通过。
- 写文档时发现的新问题（已记入 DESIGN 10.1 第 13 条）：输入长时间没有帧时（AirSim 不可用、`ros://` 数据所在节点的 topic 停了），任务一直显示 Running，但没有结果，也不会报告失败。

## 十五、10-29：输入停了就换节点，换不动再判定失败

解决了第十四节最后一条（DESIGN 10.1 第 13 条）：以前输入长时间没有帧时，任务一直显示 Running，却没有结果，也不报告失败。

**行为：**
- 执行器：任务主节点从启动或上一帧起超过 `input_timeout`（默认 10 秒）收不到一帧，就报告 `Failed`（`NOT_READY`）并停掉任务。只看帧有没有到达，推理慢不会触发。辅助节点不检查，因为只有任务主节点的报告会改变任务状态。
- 集群主节点：收到这种报告后不记为终态，而是 `Requeue`，60 秒内避开这个节点（复用下发失败的避让表）。同一个任务累计 3 个任务主节点（`max_input_stalls`）都没有输入，才判定 Failed（`NOT_READY`）；用 `request_node` 指定了节点的任务换不了节点，直接判定 Failed。
- 不改接口：`NOT_READY` 本来就在 `ErrorCodes.msg` 里，按规范 7.2 属于可重试。

**各种输入下的效果：**

| 输入 | 停了以后 |
|---|---|
| `airsim://`，某个节点连不上 AirSim | 换到能连上的节点，继续跑 |
| `airsim://`，AirSim 整个挂了 | 换 3 次节点，约 30 秒后判定失败 |
| `ros://edge-03/...`，edge-03 在线但 topic 停了 | 其他节点经 edge-03 的压缩流同样没有帧，约 30 秒后判定失败 |
| 空（本机相机） | 换到别的节点，读的是**那个节点**的相机（与迁移的现有语义一致，见第七节第 1 条） |

**验证：** 新增 3 个控制器测试（换节点、处处没有输入时判定失败、指定节点时直接失败）和 1 个仿 ROS 集成测试（有帧时不动，停帧后换节点，最终 `NOT_READY` 失败，两种推理后端各跑一遍）。我注入了 5 种 bug（不换节点、不设上限、不避开原节点、指定节点的任务也换、收到帧不刷新时间），测试都能发现。`cccp_node` 共 182 项测试全部通过。

**需要你们确认：**
1. 10 秒和 3 次这两个值是拍的。AirSim 重启通常要多久？如果超过 10 秒，可以调大 `input_timeout`。
2. 本机相机输入（空 `input`）的任务停帧后也会迁移，换到别的节点的相机。如果不希望这样，可以让空输入的任务停帧时直接判定失败，改 `controller._requeue_stalled` 一处即可。
3. 停帧判定失败的计数只存在集群主节点内存里，换主后从 0 重新计，最多多换几次节点。

## 十六、11-02：按设计 0.3 改控制面（进行中，分支 `p3-control-plane`）

设计文档 0.3（`DESIGN3.md`，控制面已定稿）按附录 D 分阶段实施，每个阶段结束时测试全部通过：

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 核心逻辑（`core/`，不涉及 ROS）：执行代次、执行栅栏、集群视图、选主、对账、调度过滤、链路量化 | **完成** |
| 2 | 控制器和网关：`Command`/`ExecEvent` 异步下发、Submit 后下一周期调度、销毁等确认、`Request`/`Reply` | **完成** |
| 3 | 接口包按附录 C 重写（协议 3）；`convert`、`control_node`、`dispatcher`、`forwarder`、`qos`、`names`、`fake_ros` | **完成** |
| 4 | 执行器：订阅 `Command`、发 `ExecEvent`、墓碑上报、`ExecutorStatus` 独立回调组 | **完成**（和阶段 3 一起做：删掉旧接口后两边必须同时改） |
| 5 | 接入层、部署脚本；`docs/DESIGN.md` 换成 0.3 并合入第十五节的无输入检测 | **完成**（接入层和部署随阶段 3 改完；设计文档已换成 0.3，合入了无输入检测和下面列的全部调整，附录 C 直接取自 `.msg` 文件） |
| 6 | 端到端测试：杀执行任务的集群主节点、销毁后立即换主不复活；心跳大小测试 | 未开始 |

**过渡做法**：阶段 1、2 的过渡层（旧消息上的 `assign_term`、用心跳充当 `TaskList`、`StartTask`/`StopTask`/`leader/task/*` 服务）已在阶段 3 全部删除。现在节点之间只有附录 C 的 6 个 topic。

**阶段 3 的实现要点**：
- 内部仍用节点名；只有消息里用编号。`ClusterView` 兼作编号 ↔ 名字的对照表，名字来自各节点的 `TaskList`（启动时先发一份空的）。还不知道名字的节点，心跳先忽略；`TaskList` 里出现未知编号时用 `#7` 这样的占位名，它不匹配任何在线节点。
- `ExecutorStatus` 里的任务分工是编号，执行器从 `Command` 里原样拿到；控制进程转成名字。
- `test/fake_ros.py` 现在检查有界数组、定长数组、有界字符串，并且只在**两端都**是 TRANSIENT_LOCAL 时给晚到的订阅者补发历史消息。`tools/check_interfaces.py` 也认识这些类型。
- `test_constants.py` 检查 `convert.py` 里所有 uint8 枚举表和 `.msg` 里的常量一致；`test_message_size.py` 按 CDR 规则从 `.msg` 文件算心跳大小：带满 8 个任务、15 条链路时 419 B，普通 168 B，带慢变部分 275 B（设计估算约 170 B / 290 B）。
- 控制进程和执行器创建节点时关闭了参数服务和 rosout（带宽措施 A）。真机上还需要用 `ros2 node list`、`ros2 topic list`、`ros2 service list` 核对。
- UE 的 `GET /nodes` 不再有 `cpu_model`、`gpu_model`（心跳里没有了），新增 `node_id`、`executor_alive`。调度器改为按 `gpu_mem_total_mb > 0` 或 `gpu_usage >= 0` 判断有没有 GPU。
- 部署：`start_node.sh` 新增 `NODE_ID`（默认从名字推导）、`REQUEST_TIMEOUT`；镜像标签改为 `p3-`。

**与 DESIGN3 评审稿不同的地方**（已回填到 `docs/DESIGN.md` 的“实现时的调整”一节和第 10.1 节第 20、21 条）：
1. 链路量化：设计写“255 表示未知或 ≥ 1 秒”。实现里 255 只表示未知，测量值最大取 254（约 1 秒）。否则 1 秒以上的坏链路会被当成“未测量”，调度器按默认值（30 ms）给它打高分。回显延迟饱和时按未知处理，不用来算 RTT。
2. 对账：任务表不知道、这一轮又没能接收的副本（它所在节点的 `TaskList` 还没追上心跳）不再当作“unknown task”停掉，而是等下一轮。0.2 会直接停，0.3 的“tasks_version 不一致时跳过”要求等待。如果某个节点的 `TaskList` 一直不来，这个副本会一直没人管，阶段 3 接上真实 `TaskList` 后要观察。
3. 大面积离线保护的退出条件按“触发时掉线的那批节点回来了多少”计算，而不是按 10 秒窗口内的掉线数（后者 10 秒后自然归零，保护会提前结束）。
4. `Reply` 补了查询用的 `name`、`spec_json`、`error_message`、`last_event`（附录 C.8 只有任务快照，查询接口还要这些）。
5. `Request` 补了 `string<=64 reason`（销毁原因）。
6. `Command` 补了 `string<=32 primary_name`：辅助节点在压缩转发（relay）模式下要按名字找任务主节点的命名空间，而执行器没有编号 → 名字的对照表。
7. `ExecEvent` 的错误码补了 `ERR_FUSION_ERROR=9`、`ERR_CANCELLED=10`：`Reply` 的 `RES_TASK_FAILED` 用 `exec_error` 说明原因，任务在等待期间被销毁或融合失败时需要这两个值。
8. 设计 7.3 里“`TaskState.name` 为 `string<=64`”意味着任务名最长 64 个字符；网关现在校验这一点（`INVALID_INPUT`）。

**阶段 2 的行为变化**：
- 销毁一个已经 Cancelled 的任务，不再返回 409，而是再发一次停止、等确认后返回 200（设计 4.5 的“重复销毁”）。销毁 Finished/Failed 的任务仍返回 409。
- `test_ros_integration.py` 里“集群主节点自己跑的任务随它丢失”的测试换成了 0.3 的行为：新集群主节点从缓存的 `TaskList` 接收它，在别处以新代次重启，`task_id` 不变。

**已修：在任者提升 term 时不再清空任务表**（设计 10.1 第 20 条）。在任的集群主节点看到更高的 term 时会把自己的 term 提上去继续担任（设计 5.3），原来控制器把“term 变了”一律当成新当选，清空任务表并重新预热 5 秒，排队中的任务会丢，期间请求返回 `NO_LEADER`。
- 选主结果新增 `Leadership.claimed_term`：本次在任以哪个任期声明。term 变了但 `claimed_term` 没变，说明一直在任：控制器保留任务表、等待中的请求和就绪状态，只把之后的命令改用新任期。卸任后再当选，`claimed_term` 会变，仍然从空表开始。
- 安全性：原来的“清空重建”顺带处理了一种情况——双主期间另一个集群主节点销毁了某个任务，本节点的任务表里它还是 Running。保留任务表后，本节点会把它当成“任务丢失”而迁移，任务就复活了。所以对账增加了一条：任何节点有这个任务的墓碑、而任务表里它还没结束，就改为 Cancelled。墓碑只在销毁时产生，`task_id` 不会重复使用，所以这条规则不会误伤。
- 测试：提升 term 后任务表、排队中的任务、等待中的创建请求都保留，新命令用新任期并被执行器接受；卸任后再当选、以及两次控制周期之间发生的重新当选，仍然从空表开始；双主期间被另一方销毁的任务不会复活。

## 十七、11-08：接入三类批处理任务，评审修复（协议 4）

**来源**：远端 `comp@10.160.4.17:~/cccp_integration_1108/cccp_cluster` 在 `e469a52` 上未提交的改动，接入了 FASTLIVO、RSSMAP、HETEROMAP 三类批处理任务（每个任务一个子进程，自己结束）。原样导入为一个提交，评审意见单独修复。远端的验收记录在 `docs/推理任务集成.md` 末尾（HETEROMAP 在单机 6 容器、真实 DDS 上跑通过）。远端 `~/cccp_cluster` 里另有 4 个旧协议的未跟踪定义（`RunningTask.msg` 等，10-26 留下的），集成副本把它们存到了 `docs/legacy-interfaces/`。

**评审修复**（都已回填 DESIGN“实现时的调整”第 12～19 条、CONVENTIONS 5.4/5.5/5.6）：
1. **HETEROMAP 辅助节点跑完后被重发 START，整个任务失败**。原来批处理进程退出就从执行器删掉会话，而 guard 对正常结束的任务不留记录。辅助节点比任务主节点早结束的那一小段时间里，对账看到“辅助节点在线但没在跑”，立刻重发 START（首次下发早已超过 5 秒）；执行器重新建产物目录时 `mkdir(exist_ok=False)` 报错，回 `INVALID_INPUT`，控制器当成永久错误判定 Failed。修复：辅助节点进程以 0 退出时保留会话、不报告，等任务主节点 Finished 后对账按“已是其他终态”发 STOP。
2. **同一代次重复 START**（执行器重启后会话丢了、产物目录还在）：改为回 `REJECTED`（`NOT_READY`），集群主节点换新代次重新调度，不再判定失败。
3. **停止时阻塞执行器、FASTLIVO 容器泄漏**：`batch.stop()` 原来在回调线程里同步等 5 秒再 SIGKILL，容器又不在进程组里。改为 SIGTERM 后立即返回，执行器的 0.5 秒定时器（`batch.reap`）在 `STOP_GRACE_S` 后 SIGKILL 进程组，并按确定的容器名（`cccp-fastlivo-<task>-<epoch>-<assign_term>`，经 `CCCP_FASTLIVO_CONTAINER` 传给 `fastlivo.sh`）`docker rm -f`。执行器退出时 `batch.shutdown` 同步收尾。
4. **换主后辅助节点顺序被打乱**：对账接收任务时原来把辅助节点按名字排序，HETEROMAP 按位置分配 id_0～id_4。改为保持胜出副本上报的顺序。
5. 任务类型改用常量（`c.FASTLIVO` 等），`"fusion"` 改用 `c.MODE_FUSION`；产物接口的 404 带 `success: false`、`error_code: NOT_FOUND`；`start_node.sh` 权限恢复为 755。
6. **协议升为 4**：`algorithms_ready` 的位从 2 个扩到 5 个，字段类型没变，但含义变了，新旧节点混跑时能力位对不上。`NodeStatus.msg` 注释、`cccp_cluster_interfaces` 的 `package.xml`（0.2.0）、镜像标签（`p4-`）一起改。

**测试**：新增 `test/test_batch_jobs.py`（6 项，用 shell 脚本冒充算法、假 `docker` 放在 PATH 里）：完成与产物下载、销毁不阻塞且 5 秒（测试里 1 秒）后 SIGKILL、辅助节点跑完后保留会话、同一代次重复 START 可重试、FASTLIVO 被 TERM 时自己删容器、被 KILL 后按名字删容器。`test_reconciler.py` 加了接收时保持辅助节点顺序，`test_constants.py` 检查能力位顺序和 `.msg` 注释一致。每一项都做了“故意改坏”检查（8 处改坏全部被发现）。

**新的已知限制**（DESIGN 10.1 第 22、23 条）：`STOPPED` 之后进程最多还活 5 秒；HETEROMAP 任务主节点 Finished 后、STOP 到达辅助节点前集群主节点刚好挂掉，新集群主节点会从辅助节点接收任务并整个重跑。

**远端复验（11-08 晚，`d63c356`）**：在 `comp@10.160.4.17:~/cccp_integration_1108/review-d63c356/` 另起一份（原集成副本和它的 6 个容器没动），容器里 colcon 构建 6 个包成功，回归 276 项通过。
- RSSMAP、FASTLIVO 真实 HTTP 测试（`test_batch_http.py`）3 项全过。注意：这台服务器上 `RSSMAP_PYTHON` 要用 `~/miniconda3/bin/python`（3.13）加 `PYTHONPATH=rsslib`；`radiomap` 环境（3.12）的 triton 是坏的，导入就报错。
- HETEROMAP：6 个容器、真实 Cyclone DDS（`ROS_DOMAIN_ID=92`），辅助节点故意不按名字排序（edge-04、02、06、03、05）。epoch 1 一次完成，372 秒，120 个文件，下载哈希一致；全局图可观测区域与参考一致 347186 / 347243（99.9836%），和原验收相同。5 个辅助节点都打出“helper part finished, waiting for the primary”，0.4～0.8 秒后收到 STOP（task finished），这正是原来会被重发 START 的窗口。
- 运行中取消：DELETE 0.11 秒返回 Cancelled，6 个 `bev_job` 进程 0.6 秒内全部退出。

**复验中发现的选主活锁（`e469a52` 就有，与批处理无关，未修）**：第一次 HETEROMAP 和 FASTLIVO 测试同时跑，约 118 秒时 edge-02 判定 edge-01 离线并以 term 2 当选；之后 edge-02 一直收不到 edge-01 的心跳，而其他节点两边都能收到。两个在任者交替“raised past a higher claim”（edge-01：1→3→5…，edge-02：2→4→6…），谁也不卸任，各自重新调度、互相 STOP 对方的代次。原因在 `core/election.py`：在任者只在**直接听到**更好的声明时才卸任；edge-02 听不到 edge-01 的声明，却从跟随者的心跳里看到 edge-01 的 term，于是一直往上抬。用纯 `Election` 类按“edge-02 听不到 edge-01、其他全通”能稳定复现（见 DESIGN 10.1 第 24 条）。这种情况下，“同一代次已经跑过 → `NOT_READY` → `Requeue`”会让任务不停换代次（原代码则直接判定 Failed），活锁一解除就不会再出现。单独重跑时只有一个集群主节点，没有复现。edge-02 为什么长时间收不到 edge-01（负载？DDS 发现？）还没查清。

## 十八、11-08：修选主活锁（间接证据），远端复现

接第十七节复验中发现的活锁（DESIGN 10.1 第 24 条），分三个阶段，每个阶段单独提交。

**阶段 1：选主规则**（`core/election.py`，不改消息，协议版本不变）。见 DESIGN 5.3“间接证据”、CONVENTIONS 5.1 第 7 条、DESIGN“实现时的调整”第 20 条。
- 先在临时脚本里比较了几种写法：跟随听不到的 X 会造成“僵尸主节点”（X 死后大家互相引用它）；不要求过半，只有少数节点听得到的集群主节点会让其他节点永远无主；过半条件只加在“不声明”上、不加在“卸任”上，刚声明的节点会被马上劝退，term 照样乱抬。
- 测试（`test_election.py` 新增 9 项）：卸任但不跟随、不声明、少数跟随不挡选举；按秒推进的多节点仿真 `simulate()`，覆盖服务器上的情况、运行中变单向、双向互相听不到、只有一个节点听得到主节点、主节点死后无僵尸（换主不超过 7 秒）；固定种子的 300 例随机单向丢失扫描（旧规则 24 例多主，已核对）。
- 故意改坏 6 处，5 处被发现。没被发现的是“无主时不重置声明计时器”：节点离线要 5 秒才判定，一条心跳丢了视图不会变，间接证据不会闪断，所以这个计时器在现有参数下不起作用（叠加 10% 随机丢包的 100 例仿真，有无计时器结果相同）。这是等价改动，保留计时器作为防御。

**阶段 2：可观测性**
- `ClusterView.changes(now)` 返回自上次调用以来上线、离线的节点（`NodeChange`），控制进程每次发心跳前打日志：离线是 WARNING `node=<名> offline: last heartbeat 5.0 s ago, missed=<n> before`，回来是 INFO `node=<名> online, unheard <秒> s, missed=<n> heartbeats`（重启的标 `(restarted)`）。`missed` 按同一 incarnation 的 seq 缺口计算：回来时缺口大，说明对方一直在发、是传输途中丢了（单向丢失）；缺口约等于 0，说明对方自己停了（例如控制进程被饿死）。阶段 3 就靠这个区分。
- 选主触发间接证据时打一次 INFO（同一个 X 只打一次）：`node=<X> is followed by <k> of the <n> nodes in view (term=<t>) but not heard here: stepping down, following nobody`（或 `not claiming`）。
- 测试 2 项，故意改坏 4 处全部被发现。

**阶段 3：远端复现与验证**（`review-d63c356/`，6 个容器、真实 Cyclone DDS，`ROS_DOMAIN_ID=92`；日志 `logs-stage3-*`、`logs-inject-*`）
- **按第一次的条件复现（HETEROMAP 和真实 RSSMAP/FASTLIVO 测试同时跑）：没有复现**。没有任何节点离线，HETEROMAP 一次完成，3 项 HTTP 测试通过。负载很轻：平均负载 16～25（144 核），每个节点容器约 2 核。所以“控制进程被 CPU 饿死”大概率不是原因。
- **单向丢失注入**：在 edge-02 的网络命名空间里用 iptables 丢掉来自 edge-01 的包，持续 120 秒（`inject.sh`；用一个 `--net container:` + `NET_ADMIN` 的 alpine 容器，只影响测试容器）。
  - 新规则：edge-02 打出 `node=edge-01 is followed by 5 of the 5 nodes in view (term=1) but not heard here: not claiming`，始终只有一个集群主节点、term 保持 1，HETEROMAP 在 epoch 1 跑完（120 个文件、哈希一致）。最终代码 `02ca9a6` 又跑了一遍，结果相同。
  - **旧规则（`d63c356`）在同样的注入下也没有出事**：edge-02 看到的在线节点（4 个）比别人（5 个）少，候选排名靠后，就一直等别人宣布。也就是说，只有“听不到主节点”这一个条件，旧规则不会分裂；第一次出事还需要第二个条件：**edge-02 自己的心跳也时断时续**，别人看到的在线节点数降到和它一样，它按编号最小排第一而宣布。
- **意外发现：某一次运行中 edge-02 的心跳持续丢失**（注入前就开始，注入解除后还在）。5 个接收方共记了 1254 条丢失，而 edge-02 的 seq 一直在涨，说明它在发、是途中丢的。6 次运行里有 2 次 edge-02 出现异常（第一次是收不到 edge-01，这一次是别人收不到它），其余 4 次完全正常。原因未查明，需要在出现时抓包（DESIGN 10.1 第 25 条）。新规则下这种情况也没有出事：只有一个集群主节点，任务正常完成。
- **据此修了仿真和一处新问题**：原来的仿真给所有节点固定 `visible_peers = n-1`，会高估旧规则出事的频率；改为按各自视图上报后，发现“主节点只有 03 听得到”时，其他节点一直等排第一的 03 宣布，而 03 在跟随主节点、永远不会宣布。加了兜底：等候选超过 `candidate_wait_s`（10 秒）仍无声明，就不再等正在跟随别人的候选（DESIGN 5.3“等候选的兜底”）。先试过“一开始就不等跟随者”，结果主节点死时 5 个节点同时宣布（各节点超时有先后），所以改成超时兜底。新增“听不到主节点且自己心跳时断时续”的仿真测试：旧规则 100 例全部多主，新规则没有；300 例随机扫描仍是旧规则 24 例、新规则 0 例；主节点死后换主新旧都是 6 秒。故意改坏 4 处（去掉兜底、一开始就去掉跟随者、去掉间接证据、跟随时不重置等待计时）全部被发现。
- 远端留下的东西：`review-d63c356/` 下的日志、产物和 `old-d63c356` 工作树（旧代码对照用），容器和网络都已删除。

## 十九、11-08：查明偶发单向丢心跳：订阅端历史被覆盖（DESIGN 10.1 第 25 条）

**结论**：不是网络丢包，是 `NodeStatus` 订阅端 KEEP_LAST(1)。ROS 2 消息没有 key，一个订阅端只有一份历史、所有节点共用；两个节点的心跳相隔只有几毫秒时，后一条在前一条被取走前就把它覆盖了。各节点 1 Hz 定时器的相位由启动时刻决定、运行期间不变，所以同一对节点每秒都撞，先到的那个在**所有**接收方那里长期“丢心跳”，而它的 seq 一直在涨——正是第十八节看到的样子。负载变化让取走的延迟抖动，所以表现为时断时续的离线/上线。订阅端深度改为 32（DESIGN“实现时的调整”第 21 条，CONVENTIONS 3.3），发布端仍是 1。

**怎么查的**（远端 `review-d63c356/`，没有 sudo，所以用 `--net container:<节点>` + `NET_RAW` 的旁路容器在每个节点的网络命名空间里跑 tcpdump，镜像 `cccp-review-tcpdump`）：
- 先按原条件（6 节点 + HETEROMAP）抓包跑了 3 轮（`capture.sh`，`cap/run1..3`）：没复现，各节点 eth0 无接收错误、socket 无丢包，心跳走组播 239.255.0.1、线路上一条不丢；任意两个节点心跳相位的最近距离 38～90 ms。
- 回看第十八节复现的那次（inject-new）：其他节点从注入**前** 18 秒就开始丢 edge-02，注入解除后还在丢，与 iptables 无关。
- 读代码发现订阅端深度是 1（`TaskList` 的订阅端早就因为同一原因开到了 32，心跳漏了）。在真实 Cyclone DDS 上做受控实验（`collide/hb.py`）：两个发布者相隔 0.2 ms，深度 1 时先发的那个 30 条全丢、seq 照涨；深度 32 一条不丢；相隔 2 ms、20 ms 都不丢。回调本身慢（40 ms）不会丢：rclpy 先取走再执行回调，只有两次取之间来了两条才覆盖。
- 12 个空载节点各跑 90 秒（`trial.sh`，`trials/`；12 个节点有 66 对，更容易出现相位相近的一对），比较 `/api/ue/nodes` 里各节点报告的应用层丢失率和抓包算出的相位：
  - 旧代码第 1 轮：edge-06、edge-10、edge-04 的心跳在 5 ms 内依次到达；**所有节点都丢 edge-06 约 43%、丢 edge-10 约 13%、不丢 edge-04**（最后到的不会被覆盖），17 次离线。抓包显示 edge-06 的 93 条心跳全部到了每个接收方的网卡。另 3 轮最近的一对相隔 5.7～18.7 ms，不丢。
  - 修复后 8 轮：5 轮有相隔不到 2 ms 的节点对（最近 0.4 ms），**应用层丢失率全部为 0，没有离线**。
  - 12 个节点时，8 轮里有 5 轮出现相隔不到 2 ms 的一对：深度为 1 的话，真机部署几乎每次都会有节点被“隐身”。
- 测试：`test_ros_integration.py` 新增一项，检查所有“多个节点发布”的 topic 的订阅端深度不小于 12（fake_ros 同步投递，复现不了覆盖，只能检查声明；为此 fake_ros 记下了每个端点的 QoS）。故意改坏 2 处（心跳订阅用发布端的 profile、`TaskList` 订阅端深度改 8）都被发现。
- `tools/rtps_loss.py`：从各节点的 pcap 里按 SEDP 找出某个 topic 的 writer，逐对比较“发送方线路上的序号”和“接收方网卡上收到的序号”，只用标准库。阶段 6 在真机上抓包时可以直接用。

**顺带发现**（DESIGN 10.1 第 26 条，第二十节已改）：资源采样在 NVIDIA GPU 的机器上同步调用 `nvidia-smi`（远端实测 70～320 ms，超时 2 秒），而它在心跳定时器里、与心跳订阅同一个互斥回调组。深度 32 后不会因此丢心跳，但会晚处理、链路 RTT 偏大。

**接口文件没改**：`NodeStatus.msg` 头部注释（以及照抄它的 DESIGN 附录 C）仍写着 “KEEP_LAST(1)”。按 CONVENTIONS，接口包任何改动都要升协议版本、12 个节点一起升级；深度只是本地设置，不影响匹配和互通，为一行注释升协议不划算，所以留着，等下次因别的原因升协议时一并改。

远端留下 `cap/`（35 MB）、`trials/`（180 MB）的抓包和日志，`collide/` 实验脚本；容器和网络都已删除。

## 二十、11-08：资源采样移出心跳的回调组（DESIGN 10.1 第 26 条）

- `ControlNode` 新增 `_probe_group`（互斥回调组）和定时器 `_sample_resources`，周期 `status_period × slow_fields_every`（默认 5 秒）；心跳 `_publish_node_status` 只读 `self._usage`（冻结的 `Usage`，一次引用赋值，不用加锁）。第一次采样之前报 -1，调度器按默认占用率处理（DESIGN“实现时的调整”第 22 条）。
- 为什么是 5 秒：资源只随每第 5 条心跳发送（`slow_fields_every`），按 1 秒采样时有 4 次结果没人用；`nvidia-smi` 的调用次数降到五分之一，CPU 占用率变成 5 秒内的平均，正好对上发送间隔。
- 规则写进 CONVENTIONS 3.3：心跳的回调组里不做阻塞调用。
- 测试：fake_ros 记下定时器和订阅的回调组（只记录，仍然一个线程跑所有定时器）。新测试包住每个节点的 `probe.sample`，从调用栈找出是哪个定时器在采样，检查它的回调组不是心跳订阅或心跳定时器的组，并且 FixedProbe 的 20% 送到了所有节点。故意改坏 3 处（心跳里直接采样、采样定时器放进心跳的组、去掉采样定时器）都被发现。
- 没在远端真机上复测 RTT：本次只改了线程归属，心跳收发路径没变。

## 二十一、11-08：板卡时钟回退后重启的节点不再“隐身”（DESIGN 10.1 第 27 条）

准备真机部署时读代码发现的，容器里碰不到（宿主机时钟不会倒退）。
- **问题**：`incarnation` 取进程启动时的墙钟毫秒，`ClusterView` 一律丢弃比已知值小的 `incarnation`。无人机板卡常常没有 RTC 电池，外场又没有 NTP，重启后时钟回到出厂时间，新的 `incarnation` 比旧的小：所有节点把它的心跳当成重启前迟到的消息，直到旧条目过期（`offline_retain_s`，5 分钟）；它的 `TaskList` 只在变化时发，被丢掉后不会再来。chrony 往回校时后再重启进程也会这样。
- **改法**（`core/cluster_view.py` 的 `_current`，DESIGN“实现时的调整”第 23 条、3.8 节）：
  - 节点在线时，较小的 `incarnation` 仍当作迟到的消息丢掉；节点离线后（`offline_timeout` 没收到心跳），不论大小都算新的一轮。重启通常远超 5 秒，所以实际上立即被接受；进程快速重启又碰上时钟回退时，最多晚 5 秒。
  - 被替换掉的 `incarnation` 记下来（每个节点最近 8 个），以后一律丢弃。第一版没有这条，测试发现：接受较小的之后，旧一轮迟到的一条（较大）又被当成重启，视图来回翻。
  - 新一轮的 `TaskList` 如果在旧一轮还算在线时到达，先暂存，它的心跳被接受时装上。
  - `task_list()` 改为只返回当前这一轮的 `TaskList`（原来按 `(incarnation, tasks_version)` 字典序比较，较小的 `incarnation` 会让旧一轮的 `TaskList` 被当成最新的，集群主节点会以为它还在跑重启前的任务）。
  - 没改的：`LinkEstimator` 对端沉默 10 秒后自己丢掉状态，之后接受任何 `incarnation`；执行器的 `incarnation` 和请求方的 `origin_incarnation` 只做相等比较。
- **测试**：`test_cluster_view.py` 新增两项（离线后以较小的 `incarnation` 回来、旧一轮迟到的消息不再接受；离线前到达的 `TaskList` 被暂存后装上）。故意改坏 5 处（恢复旧规则、较小的一律接受、不记被替换的、不装暂存的、`task_list()` 不看当前一轮）都被发现。
- 部署时仍然建议做时钟同步：日志和多块板卡的抓包要靠墙钟对齐。

## 二十二、11-08：真机部署脚本（`deploy/`），在远端容器上验证

**新增**（说明见 `deploy/README.md`）：
- `cyclonedds.xml`：用 `${CCCP_DDS_INTERFACE}` 指定网卡（板卡上同时有 `wlan0`、`docker0`、USB 等网卡，不指定时 Cyclone 自己选，可能选错），`${CCCP_DDS_MULTICAST:-default}` 切换 `AllowMulticast`，`${CCCP_DDS_TRACE:-warning}` 控制日志。`start_node.sh` 在设置了 `CCCP_DDS_INTERFACE` 且没有 `CYCLONEDDS_URI` 时启用它，网卡不存在就报错退出；两个 Dockerfile 把它复制到 `/ros2_ws/`。
- `node.env.example` + `run_node.sh`：一块板卡的配置和启动（`--network host`、`--restart unless-stopped`、日志 50 MB × 5、有 nvidia 运行时就加 `--runtime nvidia`、`CCCP_JOB_STORE` 内外同路径挂载）。`DOCKER_NETWORK` 可以改成 bridge 网络，在一台机器上起多个节点做回归。
- `smoke_check.py`：从各网关的 `/api/ue/health`、`/api/ue/nodes` 检查在线数、集群主节点是否一致、term、执行器、链路丢失率；`--task` 建一个任务、等 Running、再销毁（503 自动重试）。只用标准库。

**远端验证**（`review-d63c356/deploycheck/`，`docker build -f deploy/Dockerfile` 构建出 `cccp-node:p4-a7d2f2f-check`，3 个节点用 `run_node.sh` 起在 bridge 网络 `cccp-deploy-check`、域 93，关掉 PKCI/PKCT）：
- 镜像构建成功（x86_64，CPU torch）。Cyclone 0.10.5 读到了配置：`Interfaces/NetworkInterface[@name]: eth0 {1}`、`selected interfaces: eth0`；`${X:-默认值}` 的写法也会展开。
- `smoke_check.py` 全部通过（推理关掉，只有 “inference not ready” 告警）。
- 杀掉集群主节点 edge-01：6.1 秒后 edge-02 当选，term 3；edge-01 重启回来显示 `online (restarted)`，没有抢回位置。（第一次测时 edge-01 在 1 秒内就重启了，别人还没判它离线，它过了启动等待后以 term 2 重新当选，这是对的。）
- `CCCP_DDS_MULTICAST=false`：3 个节点互相看不见、各自当选，`smoke_check.py` 报 3 个网关各有一个集群主节点。没有 Peers 列表时发现必须靠组播，所以 `run_node.sh` 改为拒绝 `false`。`true` 正常。
- 网卡名写错：`start_node.sh` 打印容器里实际有的网卡并退出（容器会按重启策略反复重启，`docker logs` 里能看到原因）。
- 容器和网络都已删除，镜像 `cccp-node:p4-a7d2f2f-check` 留在远端，以后做单机回归可以直接用。

**没验证的**：arm64 镜像（要在 JetPack 上构建）、无线网卡上 Cyclone `default` 的实际行为（只让 SPDP 走组播是按 Cyclone 的说明，要在板卡上抓包确认）、`--runtime nvidia`、`--task`（远端这次没开推理，只用假网关测过）。

## 二十三、11-09：事件日志（DESIGN 2.9，“实现时的调整”第 24、25 条）

监控与本地录制分三个阶段：事件日志、Prometheus 指标、rosbag2 只录本机 topic。这次只做了第一阶段。

**为什么先做它**：外场出问题时无法登录排查，只能事后复盘。原来的日志格式不统一、没有时间戳、只在容器标准输出里（重建容器就没了），没法把 12 块板卡合并成一条时间线。

**做了什么**：
- `control/journal.py`：`Journal.record(event, **fields)` 生成一行 JSON。公共字段有墙钟 `t`、单调时钟 `mono`、`node`、`inc`、跟随的 `term`/`leader`、`seq`。最近 2000 条留在内存里，同时放进有界队列，由 `JournalWriter` 线程写入 `RotatingFile`（20 MB × 10 个文件）。每批写完 flush，fsync 最多每秒一次。写不进去时计数，每分钟告警一次，节点照常运行。
- **记录的事件**（DESIGN 2.9 有完整的表）：
  - 控制器：上任、预热结束（`leader_active`，附实际用时）、提升 term、卸任、调度（`reason` 非空即迁移）、接收、重新调度、取消（`by` 是网关节点或 `reconcile`）、停止、拒绝、无输入、大面积离线保护，以及任务表的每次状态变化（作为任务表的事件监听器）。
  - 控制进程：`start`/`stop`、节点上线和离线、跟随的 (集群主节点, term) 变化（每个节点都记，时钟检查就靠它），以及每 10 秒一份快照。
- `GET /api/ue/journal?after=&limit=&snapshots=0`。
- `tools/merge_journal.py`：多目录合并、按墙钟排序，按任务、节点、事件筛选，跳过断电截断的行，去掉重复的文件。
- **时钟检查**：同一次换主，各节点记录 `leader_changed` 的时间相差一两秒，据此估计每个节点的时钟偏差。第一版用了只有两个节点记录到的那次换主，中位数是两者的中点，两个节点都显得偏了一半，把没偏的 n2 也报了出来。现在至少 3 个节点都记录到才指认是哪个节点偏了；只有两个节点时只报两者相差多少。
- 控制进程的 Python 日志改为 rclpy 的格式 `[级别] [墙钟秒] [名字]: 内容`。
- `run_node.sh` 把宿主机的 `CCCP_DATA_DIR`（默认 `/var/lib/cccp`）以同一路径挂进容器。控制进程的 `journal_dir` 参数默认读 `$CCCP_JOURNAL_DIR`，没有就用 `$CCCP_DATA_DIR/journal`；两个都没有时只保留在内存里，测试默认就是这样。

**没有做的**：控制面不用 rosbag 录。录制器订阅 `/cluster/*` 后，单播模式下发布者要给它多发一份，控制面的无线流量会翻倍；控制面的历史由事件日志负责。指标和推理结果录制留到后面的阶段（DESIGN 10.3 第 3 条）。没有改接口包，协议版本不变。

**测试**：
- 新增 `test_journal.py`（9 项）和 `test_merge_journal.py`（6 项）。
- `test_controller.py` 加了 3 项：换主和迁移、提升 term 和卸任、销毁记录发起者。
- `test_ue_api.py` 加了 1 项。
- `test_ros_integration.py` 加了 1 项：杀掉集群主节点后，每个存活节点都记了 `leader_changed` 和 `node_offline`，有快照；新集群主节点的快照带任务计数；死掉的那个节点的文件以 `start` 开头、`stop` 结尾。
- 故意改坏 18 处都被发现：不记 `leader_active`/`term_raised`/`task_stop`、丢掉迁移原因、不记销毁发起者、不用 context、轮转时删掉旧文件、重启时截断文件、close 时丢掉最后一批、快照不带链路、`snapshots=0` 无效、按两节点的换主指认偏差、`--node` 不认 `primary`、不去重、默认显示快照；控制进程不记 `leader_changed`、离线记成上线、退出时不记 `stop`。
- 全量 313 项通过、3 项跳过。
- **原来就有的不稳定测试**：`test_ros_integration.py::test_task_running_on_the_leader_is_restarted_elsewhere_after_it_dies[ros_services]` 偶尔 30 秒内等不到任务以新代次在别处运行。在未改动的提交 `2228652` 上（临时 worktree）跑 8 次失败 5 次，当时机器上同时在跑别的测试；空闲时大多能通过。与事件日志无关，原因还没查。

## 二十四、11-09：查明不稳定的迁移测试——集群主节点自己的新任务可能丢失（DESIGN 10.1 第 28 条，“实现时的调整”第 26 条）

**现象**：`test_task_running_on_the_leader_is_restarted_elsewhere_after_it_dies[ros_services]` 偶尔 30 秒内等不到任务以新代次在别处运行。在未改动的提交 `2228652` 上 8 次失败 5 次。

**查法**：
1. 用 INFO 日志复现。失败时新集群主节点只从 edge-02、edge-03 接收了 4 个任务，死掉的 edge-01 上那个任务既没有被接收，也没有被重新调度。
2. 加一个临时探针，在创建回复返回、杀掉之前，看其他节点缓存的 edge-01 的 `TaskList`：8 次里有 2 次两个跟随者都还没有这个任务。

**原因**：
- 执行器启动任务后立即发 `RUNNING`，集群主节点收到就回复“创建成功”；但执行器的 `ExecutorStatus` 只在定时器上发（真机 1 Hz，测试里和心跳共用参数，是 0.2 秒），而控制进程要收到它才发 `TaskList`。
- 测试一拿到回复就杀掉集群主节点：如果下一次状态还没发出去，没有任何节点知道这个任务。
- 真机上也一样：集群主节点给自己派了任务、回复成功后 1 秒内掉电，任务就丢了，违背 DESIGN 1.4 和 6.2 的说法。只有集群主节点自己执行的任务会这样：别的节点上的任务，集群主节点的任务表里有记录，那个节点离线后会被重新调度。

**改法**（`executor_node.py`）：`_report` 发出除 `REJECTED` 以外的事件之前，先补发一条 `ExecutorStatus`。所有任务集合的变化都经过这里：启动报 `RUNNING`，`_remove` 报 `STOPPED`/`FAILED`/`FINISHED`。另外，状态的构造和发布加锁，因为定时器和命令回调现在都会发布，要保持 seq 和内容的顺序。顺带的好处是墓碑也更快到达集群主节点，销毁确认更快。

**剩下的窗口**：本机投递加一次 `TaskList` 可靠传输，毫秒级。回复和 `TaskList` 走不同的 topic，DDS 不保证二者的先后。完全消除的办法记在 10.1 第 28 条：等跟随者心跳里的 `tasks_version` 追上之后再回复。目前没有做。

**测试**：
- 新增 `test_a_task_is_in_its_nodes_task_list_everywhere_before_its_create_is_answered`：执行器状态周期设为 3 秒（新的 `executor_status_period` marker），从每个网关创建一个任务，回复返回时检查其他节点都已经有它。去掉修复后 3 次全部失败。
- 原来不稳定的测试修复后连跑 15 次（每次两种推理后端）全部通过。
- 夹具里执行器的 `status_period` 原来和控制进程共用 `BUS.params`，现在分开设置。

**另一项只出现过一次的失败**：`test_silent_executor_keeps_its_tasks_for_the_grace_period_then_they_move[local_calls]` 在一次误跑整个集成模块时失败过：任务在 6 秒宽限内就迁移了，代次变成 2。单独连跑 10 次都通过。猜测是负载下共享的定时器线程变慢，心跳间隔超过了测试用的 `offline_timeout`（1 秒），节点被判离线后立即迁移。没有查实，再出现时看日志里有没有 `node=... offline`。

## 二十五、11-09：任务类型注册表（推理任务接入改造 阶段 1，DESIGN“实现时的调整”第 27 条）

**目标**：以后新增一种推理任务，控制面只登记一项，不再到处加分支。改造一共分 4 个阶段：
1. 控制面注册表（本节）；
2. 数据面适配器：执行器、推理后端、`batch.py`、`annotate.py`；
3. `TaskResult` 通用化，`algorithms_ready` 扩到 uint32（改接口，协议 5，要先评审）；
4. 示例算法和接入文档。

**改动**：
- 新增 `core/task_types.py`。`TaskType` 的字段有 `kind`（stream/batch）、`modes`、`participants`、`helpers_required`、`check_params`、`artifacts`；`REGISTRY` 的顺序就是能力位。`ALGORITHMS`、`BATCH_TYPES` 从 `constants.py` 移到这里，`HETEROMAP_NODES` 去掉（改为 HETEROMAP 的 `participants=6`）。`constants.py` 只留类型名字。
- `task_spec.normalize_spec`：批处理不能带输入、mode 限制、固定参与节点数、参数校验都查表；`PARAM_CHECKS` 去掉。gateway 配置里有、但没有注册的类型名，按不接受处理。
- `controller`：辅助节点 `FAILED` 时整个任务失败，条件由 `== HETEROMAP` 改为 `helpers_required`。
- `task_table.result_url`：条件由"是批处理"改为"有产物后缀"。
- `ue_api`：产物下载地址的正则由角色列表和产物后缀生成，对现有 5 种类型和原来的正则等价（`primary|id_0..id_4`，`.npz|.pcd`）。
- `gateway`、`control_node`、`convert.ALGORITHMS`、执行器的 `TASK_TYPES`、`batch.TYPES` 改为从注册表导入；执行器的逻辑没有改。
- 两条规格错误信息改为通用写法（见 DESIGN 第 27 条），没有找到依赖原文的地方。

**测试**：
- 先补了现状测试，在改代码之前都能通过，覆盖原来没有测试的批处理规则：
  - `test_task_spec.py`：批处理不能带输入、FASTLIVO/RSSMAP 只能是 local、HETEROMAP 必须 fusion 且 6 个有序节点、RSSMAP 的参数、其他批处理不能带参数、PKCI 的参数原样通过、gateway 配置限制可接受的类型；
  - `test_task_table.py`：只有 Finished 的批处理任务有 `result_url`；
  - `test_controller.py`：HETEROMAP 辅助节点失败，整个任务失败；PKCI 的不会。
- 新增 `test_task_types.py`：固定位顺序、各项自洽（有产物 ⇔ 批处理、固定节点数 ⇒ 非 local 等），扫描 `core/`、`control/` 里有没有按类型名字分支（另有一项测试确认扫描本身能匹配）。
- 全量 349 项通过、3 项跳过（原 315 项）。
- 故意改坏 11 处都被发现：
  - 注册表：关掉 `helpers_required`、`participants` 改为 5、RSSMAP 的 `score` 接受字符串、FASTLIVO 去掉产物、PKCI/PKCT 换位、FASTLIVO 允许 redundant；
  - 其他代码：去掉批处理输入检查、去掉参数校验、在调度器里按名字分支、所有 Finished 任务都给 `result_url`、gateway 忽略配置。
- 没有覆盖到的：
  - 执行器包本机跑不了自己的测试，这次只改了导入，由 `test_ros_integration.py`、`test_batch_jobs.py`（执行器跑在仿 ROS 上）覆盖；
  - 产物正则的生成没有专门的改坏测试，`test_batch_jobs.py` 的列出和下载用例覆盖了它。

