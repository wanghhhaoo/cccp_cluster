# cccp_cluster

CCCP 分布式集群：去中心化的协同推理平台：12 个对等节点通过 Raft 选出集群主节点，由它负责调度和任务管理；所有节点都执行推理任务。前身是 CCCP 复用包（中心化后端版本）。

- 开发规范：[docs/CONVENTIONS.md](docs/CONVENTIONS.md)
- 当前进度、验证情况、待决策事项：[docs/HANDOFF.md](docs/HANDOFF.md)

```bash
# 没有 ROS 也能跑的测试（Python 3.10）
pip install pytest pysyncobj==0.3.17 pyyaml numpy pillow
cd ros2_ws/src/cccp_node && python -m pytest test
python3 tools/check_interfaces.py ros2_ws/src     # 在仓库根目录运行
```
