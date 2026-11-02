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
| 2 | 控制器和网关：`Command`/`ExecEvent` 异步下发、Submit 后下一周期调度、销毁等确认、`Request`/`Reply` | 未开始 |
| 3 | 接口包按附录 C 重写（协议 3）；`convert`、`control_node`、`dispatcher`、`forwarder`、`qos`、`names`、`fake_ros` | 未开始 |
| 4 | 执行器：订阅 `Command`、发 `ExecEvent`、墓碑上报、`ExecutorStatus` 独立回调组 | 未开始 |
| 5 | 接入层、部署脚本；`docs/DESIGN.md` 换成 0.3 并合入第十五节的无输入检测 | 未开始 |
| 6 | 端到端测试：杀执行任务的集群主节点、销毁后立即换主不复活；心跳大小测试 | 未开始 |

**阶段 1 期间的过渡做法**（阶段 3 换接口时去掉）：
- 0.2 的 `TaskState`、`TaskReport`、`StartTask`、`StopTask`、`TaskResult` 临时加了 `assign_term` 字段，端到端测试因此现在就覆盖执行代次。
- 协议 2 的 `NodeStatus` 仍带完整 `TaskState`，`control_node._record` 拿它充当 `TaskList`（版本号取 `seq`），核心逻辑已按 0.3 的“心跳摘要 + TaskList”工作。
- 选主已支持 `visible_peers` 排序，但 0.2 心跳没有这个字段，控制节点暂时对自己也报 0，大家一致，结果退化为按名字选。
- STOP 的原因类别（`cancel` 等）目前写进 `StopTask.reason` 文本；执行器还不留墓碑（阶段 4）。
- `NodeView` 仍保留 `cpu_model`、`gpu_model`：调度器用 `gpu_model` 判断有没有 GPU，0.3 的心跳去掉了型号字符串，阶段 3 要换一个判断（例如 `gpu_mem_total_mb > 0` 或 `gpu_usage >= 0`）。

**与 DESIGN3 不同的地方**（需要回填设计文档）：
1. 链路量化：设计写“255 表示未知或 ≥ 1 秒”。实现里 255 只表示未知，测量值最大取 254（约 1 秒）。否则 1 秒以上的坏链路会被当成“未测量”，调度器按默认值（30 ms）给它打高分。回显延迟饱和时按未知处理，不用来算 RTT。
2. 对账：任务表不知道、这一轮又没能接收的副本（它所在节点的 `TaskList` 还没追上心跳）不再当作“unknown task”停掉，而是等下一轮。0.2 会直接停，0.3 的“tasks_version 不一致时跳过”要求等待。如果某个节点的 `TaskList` 一直不来，这个副本会一直没人管，阶段 3 接上真实 `TaskList` 后要观察。
3. 大面积离线保护的退出条件按“触发时掉线的那批节点回来了多少”计算，而不是按 10 秒窗口内的掉线数（后者 10 秒后自然归零，保护会提前结束）。
