"""Control process of a cluster node (CONVENTIONS 6.1).

Hosts the Raft replica, the leader controller (active only on the leader),
the task gateway (ROS services + UE HTTP), and the NodeStatus publisher that
also carries link measurements and this node's running tasks.

Threads: rclpy MultiThreadedExecutor callbacks, the PySyncObj tick thread,
the leader controller loop and the UE HTTP server. Cross-thread hand-offs go
through queues drained by a ROS timer, so ROS entities are only created and
used from executor threads (except blocking service calls in the dispatcher).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import replace

import rclpy
import yaml
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image

from cccp_cluster_interfaces.msg import ExecutorStatus, LinkQuality, NodeStatus, RunningTask
from cccp_task_interfaces.msg import TaskReport, TaskResult, TaskStatus
from cccp_task_interfaces.srv import CreateTask, DestroyTask, QueryTask

from .. import names
from ..consensus.pysyncobj_backend import PySyncObjConsensus, RaftTiming, load_members
from ..control.controller import ControllerConfig, LeaderController
from ..control.gateway import Gateway, GatewayConfig
from ..control.ue_backend import UeBackend
from ..core import constants as c
from ..core.cluster_view import ClusterView, LinkView, NodeView, RunningTaskView
from ..core.link_estimator import LinkEstimator
from ..core.reconciler import ReconcileConfig
from ..core.scheduler import SchedulerConfig
from ..core.task_table import StatusEvent
from ..monitor.probes import SystemProbe
from ..ue_api import UeApi
from . import convert, qos
from .dispatcher import RosDispatcher


class ControlNode(Node):
    def __init__(self):
        super().__init__("cccp_control")
        p = self._declare_parameters()
        self.node_name = p["node_name"]
        with open(p["cluster_config"], encoding="utf-8") as handle:
            cluster_config = yaml.safe_load(handle)
        protocol = int(cluster_config.get("protocol_version", 1))
        if protocol != NodeStatus.PROTOCOL_VERSION:
            raise RuntimeError("cluster.yaml protocol_version %d does not match interfaces %d"
                               % (protocol, NodeStatus.PROTOCOL_VERSION))

        self._incarnation = int(time.time() * 1000)
        self._seq = 0
        self._status_period = p["status_period"]
        self._executor_timeout = p["executor_timeout"]
        self._battery = p["battery"]
        self._executor_status = None
        self._executor_seen = 0.0
        self._outbox: queue.Queue = queue.Queue()        # (kind, payload) handed to ROS threads
        self._watched = set()
        self._stop = threading.Event()

        self._state_group = MutuallyExclusiveCallbackGroup()
        self._api_group = ReentrantCallbackGroup()
        self._client_group = ReentrantCallbackGroup()

        timing = RaftTiming(p["raft_heartbeat"], p["raft_election_min"], p["raft_election_max"],
                            p["raft_leader_fallback"])
        self.consensus = PySyncObjConsensus(self.node_name, load_members(cluster_config), timing,
                                            p["data_dir"] or None)
        self.cluster = ClusterView(p["offline_timeout"])
        self.links = LinkEstimator()
        self.probe = SystemProbe()
        self.gateway = Gateway(self.consensus, GatewayConfig(task_types=tuple(p["task_types"])))
        controller_config = ControllerConfig(
            scheduler=SchedulerConfig(protocol_version=protocol),
            reconcile=ReconcileConfig(),
            warmup_s=p["offline_timeout"],
        )
        self.controller = LeaderController(self.node_name, self.consensus, self.cluster,
                                           RosDispatcher(self, self._client_group, p["dispatch_timeout"]),
                                           controller_config)
        self.ue = UeBackend(self.node_name, self.gateway, self.cluster,
                            watch=lambda node, images: self._outbox.put(("watch", (node, images))),
                            leader_name=self.consensus.leader_name)
        self.consensus.add_event_listener(self._on_table_events)
        self.consensus.add_leader_listener(self._on_leadership)

        self._node_status_pub = self.create_publisher(NodeStatus, names.NODE_STATUS_TOPIC, qos.NODE_STATUS)
        self._task_status_pub = self.create_publisher(TaskStatus, names.TASK_STATUS_TOPIC, qos.TASK_STATUS)
        self.create_subscription(NodeStatus, names.NODE_STATUS_TOPIC, self._on_node_status, qos.NODE_STATUS,
                                 callback_group=self._state_group)
        self.create_subscription(ExecutorStatus, names.EXEC_STATUS_TOPIC, self._on_executor_status,
                                 qos.EXECUTOR_STATUS, callback_group=self._state_group)
        self.create_subscription(TaskReport, names.TASK_REPORT_TOPIC,
                                 lambda msg: self.controller.report(convert.report_from_msg(msg)), qos.TASK_REPORT)
        self.create_subscription(TaskStatus, names.TASK_STATUS_TOPIC, self._on_task_status, qos.TASK_STATUS)
        self.create_service(CreateTask, names.API_CREATE_SERVICE, self._on_create, callback_group=self._api_group)
        self.create_service(DestroyTask, names.API_DESTROY_SERVICE, self._on_destroy,
                            callback_group=self._api_group)
        self.create_service(QueryTask, names.API_QUERY_SERVICE, self._on_query, callback_group=self._api_group)
        self.create_timer(self._status_period, self._publish_node_status, callback_group=self._state_group)
        self.create_timer(0.05, self._drain_outbox)

        self._controller_thread = threading.Thread(target=self.controller.run, args=(self._stop,), daemon=True)
        self._controller_thread.start()
        self._ue_api = None
        if p["ue_http_port"]:
            self._ue_api = UeApi(self.ue, p["ue_http_host"], p["ue_http_port"], self.get_logger())
        self.get_logger().info("control node %s ready (raft %s, ue_http=%s)" % (
            self.node_name, len(cluster_config["members"]), p["ue_http_port"] or "off"))

    def _declare_parameters(self) -> dict:
        defaults = {
            "node_name": os.environ.get("NODE_NAME", "node-1"),
            "cluster_config": os.environ.get("CLUSTER_CONFIG", "/etc/cccp/cluster.yaml"),
            "data_dir": os.environ.get("CCCP_DATA_DIR", ""),
            "status_period": 1.0,
            "offline_timeout": 5.0,
            "executor_timeout": 3.0,
            "dispatch_timeout": 5.0,
            "battery": -1.0,
            "task_types": ["PKCI", "PKCT"],
            "raft_heartbeat": 0.3,
            "raft_election_min": 1.5,
            "raft_election_max": 3.0,
            "raft_leader_fallback": 6.0,
            "ue_http_host": "0.0.0.0",
            "ue_http_port": 18090,
        }
        return {name: self.declare_parameter(name, value).value for name, value in defaults.items()}

    # ------------------------------------------------------------ NodeStatus
    def _publish_node_status(self):
        now = time.monotonic()
        self._seq += 1
        hw, usage = self.probe.hardware, self.probe.sample()
        executor = self._executor_status if now - self._executor_seen < self._executor_timeout else None
        reports = self.links.reports(now)
        view = NodeView(
            name=self.node_name,
            protocol_version=NodeStatus.PROTOCOL_VERSION,
            incarnation=self._incarnation,
            seq=self._seq,
            leader_name=self.consensus.leader_name(),
            term=self.consensus.term(),
            is_leader=self.consensus.is_leader(),
            inference_ready=bool(executor and executor.inference_ready),
            algorithms_ready=tuple(executor.algorithms_ready) if executor else (),
            cpu_model=hw.cpu_model, cpu_cores=hw.cpu_cores, mem_total_mb=hw.mem_total_mb,
            gpu_model=hw.gpu_model, gpu_mem_total_mb=hw.gpu_mem_total_mb,
            cpu_usage=usage.cpu_usage, mem_usage=usage.mem_usage,
            gpu_usage=usage.gpu_usage, gpu_mem_usage=usage.gpu_mem_usage,
            battery=self._battery,
            running_tasks=tuple(RunningTaskView(t.task_id, t.epoch, t.role) for t in executor.running_tasks)
            if executor else (),
        )
        msg = convert.fill_node_status(NodeStatus(), view, reports, LinkQuality, RunningTask)
        msg.stamp = self.get_clock().now().to_msg()
        self.links.on_publish(self._seq, now)
        self._node_status_pub.publish(msg)
        own_links = tuple(LinkView(r.peer_name, r.rtt_ms, r.jitter_ms, r.loss_rate) for r in reports)
        self.cluster.update(replace(view, links=own_links), now)

    def _on_node_status(self, msg):
        if msg.node_name == self.node_name:
            return
        now = time.monotonic()
        self.links.on_peer_status(msg.node_name, msg.incarnation, msg.seq, now)
        echo = convert.echo_for(msg, self.node_name)
        if echo:
            self.links.on_peer_echo(msg.node_name, echo[0], echo[1], now)
        self.cluster.update(convert.node_view_from_msg(msg), now)

    def _on_executor_status(self, msg):
        if msg.node_name == self.node_name:
            self._executor_status, self._executor_seen = msg, time.monotonic()

    # ------------------------------------------------------------ task events
    def _on_table_events(self, events):
        """Consensus thread: only the leader publishes TaskStatus."""
        if self.consensus.is_leader():
            self._outbox.put(("status", (events, self.consensus.term())))

    def _on_leadership(self, leader, term):
        self.get_logger().info("%s for term %d" % ("leader" if leader else "follower", term))

    def _on_task_status(self, msg):
        self.ue.on_status(StatusEvent(msg.seq, msg.task_id, msg.name, msg.task_type, msg.status, msg.epoch,
                                      msg.node, msg.message))

    def _drain_outbox(self):
        while True:
            try:
                kind, payload = self._outbox.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                events, term = payload
                stamp = self.get_clock().now().to_msg()
                for event in events:
                    self._task_status_pub.publish(convert.fill_task_status(TaskStatus(), event, term, stamp))
            elif kind == "watch":
                self._watch(*payload)

    def _watch(self, node_name, images):
        """Subscribe to a node's per-frame results (and result images) for the UE API."""
        if (node_name, False) not in self._watched:
            self._watched.add((node_name, False))
            self.create_subscription(TaskResult, names.on_node(node_name, names.TASK_RESULT_TOPIC),
                                     lambda msg: self.ue.on_result(convert.result_to_dict(msg)), qos.TASK_RESULT)
        if images and (node_name, True) not in self._watched:
            self._watched.add((node_name, True))
            self.create_subscription(Image, names.on_node(node_name, names.TASK_RESULT_IMAGE_TOPIC),
                                     self._on_result_image, qos.TASK_RESULT_IMAGE)

    def _on_result_image(self, msg):
        if msg.header.frame_id.startswith("task_"):
            try:
                self.ue.on_result_image(int(msg.header.frame_id[len("task_"):]), msg)
            except ValueError:
                pass

    # ------------------------------------------------------------ services
    def _on_create(self, request, response):
        reply = self.gateway.create(convert.spec_from_request(request), request.idempotency_key)
        return convert.fill_create_response(response, reply)

    def _on_destroy(self, request, response):
        reply = self.gateway.destroy(int(request.task_id), request.reason)
        response.success, response.error_code = reply.success, reply.error_code
        response.message, response.final_status = reply.message, reply.final_status
        return response

    def _on_query(self, request, response):
        return convert.fill_query_response(response, self.gateway.query(int(request.task_id)),
                                           int(request.task_id), c.NOT_FOUND)

    def shutdown(self):
        self._stop.set()
        if self._ue_api is not None:
            self._ue_api.shutdown()
        self.consensus.close()
        self._controller_thread.join(timeout=2.0)


def main(args=None):
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] [%(name)s] %(message)s")
    rclpy.init(args=args)
    node = None
    try:
        node = ControlNode()
        executor = MultiThreadedExecutor(num_threads=8)
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.shutdown()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
