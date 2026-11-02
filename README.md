# cccp_cluster

CCCP 分布式集群，一个去中心化的协同推理平台：12 个对等节点通过 NodeStatus 广播选出集群主节点，由它负责调度和任务管理；所有节点都执行推理任务。前身是 CCCP 复用包（中心化后端版本）。

## 文档

| 文档 | 回答的问题 | 什么时候看 |
|---|---|---|
| [docs/DESIGN.md](docs/DESIGN.md)（先读这个） | 系统**为什么**这样设计，**怎么运转**，每种故障下会发生什么 | 第一次接触项目；准备改架构；评审一个跨模块的改动 |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | **必须**怎么做：命名、QoS、标识、一致性规则、参数、错误码 | 写代码和评审代码时逐条对照 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 做到了哪一步、验证到什么程度、还有哪些待办 | 接手工作时 |

设计文档和开发规范在规则上有重叠，两者冲突时**以开发规范为准**，并修正设计文档。

设计文档的阅读顺序：

- 只想知道系统长什么样：第 0、2 章。
- 要改控制面（选主、任务表、对账、调度）：第 3、4、5、6、8 章。
- 要改执行器或接入新算法：第 2.5、4.3、4.4、7 章。
- 想知道某个设计为什么不是另一种样子：第 9 章。

## 运行测试

```bash
# 没有 ROS 也能跑的测试（Python 3.10）
pip install pytest numpy pillow
cd ros2_ws/src/cccp_node && python -m pytest test
python3 tools/check_interfaces.py ros2_ws/src     # 在仓库根目录运行
```
