# 交接说明：MVP 第一版代码

2026-10-27｜面向：平台组三位成员

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

