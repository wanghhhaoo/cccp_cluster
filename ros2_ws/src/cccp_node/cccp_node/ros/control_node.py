"""Control process of a cluster node (CONVENTIONS 6.1, DESIGN 2.4, 2.7).

Every node runs: the heartbeat (/cluster/node_status, 1 Hz, best effort), its
TaskList (/cluster/task_list, reliable and transient-local, on change), the
cluster view and election built from everyone else's, link measurement, the
gateway (public api/* services, UE HTTP) sending Requests and reading Replies,
and the watch over the local executor (<ns>/exec/status).

The elected leader also runs the controller over its task table. Only then
does the node hold the leader's endpoints: Command and Reply publishers,
ExecEvent and Request subscriptions. They are created on election and
destroyed on stepping down, so the other nodes do not carry their reliable
traffic (DESIGN 2.5).

Threads: rclpy MultiThreadedExecutor callbacks, the controller loop and the UE
HTTP server. Cross-thread hand-offs that create ROS entities go through a
queue drained by a ROS timer, so ROS entities are only created from executor
threads. Nothing waits on another node.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import replace

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import Image

from cccp_cluster_interfaces.msg import (
    Command, ExecEvent, ExecutorStatus, LinkQuality, NodeLoad, NodeStatus, Reply, Request, TaskBrief, TaskList,
    TaskState, Tombstone,
)
from cccp_task_interfaces.msg import TaskResult, TaskStatus
from cccp_task_interfaces.srv import CreateTask, DestroyTask, QueryTask

from .. import names
from ..control.controller import ControllerConfig, LeaderController
from ..control.gateway import Gateway, GatewayConfig
from ..control.task_store import TaskStore
from ..control.ue_backend import UeBackend
from ..core import constants as c
from ..core.cluster_view import ClusterView, LinkView, NodeView, TaskListView
from ..core.election import Election, ElectionConfig
from ..core.link_estimator import LinkEstimator
from ..core.naming import default_node_id
from ..core.reconciler import ReconcileConfig
from ..core.scheduler import SchedulerConfig
from ..core.task_table import StatusEvent
from ..monitor.probes import SystemProbe
from ..ue_api import UeApi
from . import convert, qos
from .dispatcher import RosCommandSink
from .forwarder import RosRequestTransport

log = logging.getLogger(__name__)


class ControlNode(Node):
    def __init__(self):
        # Parameter services and /rosout cost DDS endpoints on every node; launch-time
        # parameter overrides still apply (bandwidth measure A, DESIGN 2.8).
        super().__init__("cccp_control", start_parameter_services=False, enable_rosout=False)
        p = self._declare_parameters()
        self.node_name = p["node_name"]
        self.node_id = p["node_id"] or default_node_id(self.node_name)
        assert NodeStatus.PROTOCOL_VERSION == c.PROTOCOL_VERSION, "core/constants.py is out of date"
        self._incarnation = int(time.time() * 1000)
        self._seq = 0
        self._slow_every = max(1, p["slow_fields_every"])
        self._executor_timeout = p["executor_timeout"]
        self._battery = p["battery"]
        self._executor = None                        # last ExecutorStatus
        self._executor_seen = 0.0
        self._own_tasks = TaskListView(self.node_name, self._incarnation, 0, node_id=self.node_id)
        self._outbox: queue.Queue = queue.Queue()     # (kind, payload) handed to ROS threads
        self._watched = set()
        self._stop = threading.Event()

        self._state_group = MutuallyExclusiveCallbackGroup()
        self._api_group = ReentrantCallbackGroup()

        self.cluster = ClusterView(p["offline_timeout"], p["offline_retain"])
        self.cluster.register(self.node_name, self.node_id)
        self.election = Election(self.node_name, ElectionConfig(startup_grace_s=p["offline_timeout"]),
                                 time.monotonic(), node_id=self.node_id)
        self.links = LinkEstimator()
        self.probe = SystemProbe()
        self.store = TaskStore()
        self._commands = RosCommandSink(self.cluster)
        self._leader_endpoints = None                 # (Reply publisher, ExecEvent and Request subscriptions)
        self.controller = LeaderController(
            self.node_name, self.store, self.election, self.cluster, self._commands,
            ControllerConfig(scheduler=SchedulerConfig(), reconcile=ReconcileConfig(
                executor_stall_grace_s=p["executor_stall_grace"]), warmup_s=p["offline_timeout"],
                create_wait_timeout_s=p["create_wait"], destroy_wait_timeout_s=p["destroy_wait"]),
            reply=self._send_reply,
        )
        request_pub = self.create_publisher(Request, names.REQUEST_TOPIC, qos.REQUEST)
        self.gateway = Gateway(
            self.node_name, lambda: self.controller.active, lambda: self.election.state.leader,
            self.controller.on_request,
            RosRequestTransport(request_pub, self.cluster, lambda reply: self.gateway.on_reply(reply)),
            GatewayConfig(task_types=tuple(p["task_types"]), request_timeout_s=p["request_timeout"]),
            term=lambda: self.election.state.term, incarnation=self._incarnation,
        )
        self.ue = UeBackend(self.node_name, self.gateway, self.cluster,
                            watch=lambda node, images: self._outbox.put(("watch", (node, images))),
                            leader_name=lambda: self.election.state.leader)
        self.store.add_event_listener(self._on_table_events)

        self._node_status_pub = self.create_publisher(NodeStatus, names.NODE_STATUS_TOPIC, qos.NODE_STATUS)
        self._task_list_pub = self.create_publisher(TaskList, names.TASK_LIST_TOPIC, qos.TASK_LIST_PUB)
        self._task_status_pub = self.create_publisher(TaskStatus, names.TASK_STATUS_TOPIC, qos.TASK_STATUS)
        self.create_subscription(NodeStatus, names.NODE_STATUS_TOPIC, self._on_node_status, qos.NODE_STATUS,
                                 callback_group=self._state_group)
        self.create_subscription(TaskList, names.TASK_LIST_TOPIC, self._on_task_list, qos.TASK_LIST_SUB,
                                 callback_group=self._state_group)
        self.create_subscription(ExecutorStatus, names.EXEC_STATUS_TOPIC, self._on_executor_status,
                                 qos.EXECUTOR_STATUS, callback_group=self._state_group)
        self.create_subscription(Reply, names.REPLY_TOPIC, self._on_reply, qos.REPLY)
        self.create_subscription(TaskStatus, names.TASK_STATUS_TOPIC, self._on_task_status, qos.TASK_STATUS)
        for name, srv, handler in (
            (names.API_CREATE_SERVICE, CreateTask, self._on_create),
            (names.API_DESTROY_SERVICE, DestroyTask, self._on_destroy),
            (names.API_QUERY_SERVICE, QueryTask, self._on_query),
        ):
            self.create_service(srv, name, handler, callback_group=self._api_group)
        self.create_timer(p["status_period"], self._publish_node_status, callback_group=self._state_group)
        self.create_timer(0.05, self._drain_outbox)
        self._publish_task_list()                     # tells everyone this node's name

        self._controller_thread = threading.Thread(target=self.controller.run, args=(self._stop,), daemon=True)
        self._controller_thread.start()
        self._ue_api = None
        if p["ue_http_port"]:
            self._ue_api = UeApi(self.ue, p["ue_http_host"], p["ue_http_port"], self.get_logger())
        self.get_logger().info("control node %s (id %d) ready (protocol %d, ue_http=%s)" % (
            self.node_name, self.node_id, c.PROTOCOL_VERSION, p["ue_http_port"] or "off"))

    def _declare_parameters(self) -> dict:
        defaults = {
            "node_name": os.environ.get("NODE_NAME", "node-1"),
            "node_id": int(os.environ.get("NODE_ID", "0") or 0),   # 0: from the number node_name ends with
            "status_period": 1.0,
            "slow_fields_every": 5,      # resources and links ride on every 5th heartbeat
            "offline_timeout": 5.0,
            "offline_retain": 300.0,
            "executor_timeout": 3.0,
            "executor_stall_grace": 10.0,
            "create_wait": 10.0,
            "destroy_wait": 10.0,
            "request_timeout": 15.0,
            "battery": -1.0,
            "task_types": list(c.ALGORITHMS),
            "ue_http_host": "0.0.0.0",
            "ue_http_port": 18090,
        }
        return {name: self.declare_parameter(name, value).value for name, value in defaults.items()}

    # ------------------------------------------------------------ heartbeat
    def _publish_node_status(self):
        now = time.monotonic()
        peers = {n: v for n, v in self.cluster.online(now).items() if n != self.node_name}
        executor = self._executor if now - self._executor_seen < self._executor_timeout else None
        inference_ready = bool(executor and executor.inference_ready)
        visible = self.cluster.visible_peers(self.node_name, now)
        me = NodeView(self.node_name, node_id=self.node_id, visible_peers=visible, inference_ready=inference_ready,
                      battery=self._battery)
        leadership = self.election.update(peers, now, me)
        self._follow_role(leadership.is_self)
        self._seq += 1
        hw, usage = self.probe.hardware, self.probe.sample()
        slow = (self._seq - 1) % self._slow_every == 0
        reports = self.links.reports(now)
        view = NodeView(
            name=self.node_name, incarnation=self._incarnation, seq=self._seq, leader_name=leadership.leader,
            term=leadership.term, is_leader=leadership.is_self, inference_ready=inference_ready,
            algorithms_ready=convert.algorithms_from_mask(executor.algorithms_ready) if executor else (),
            node_id=self.node_id, visible_peers=visible,
            executor_alive=executor is not None,      # quiet: the tasks below are the last known ones
            tasks_version=self._own_tasks.tasks_version,
            cpu_cores=hw.cpu_cores, mem_total_mb=hw.mem_total_mb, gpu_mem_total_mb=hw.gpu_mem_total_mb,
            cpu_usage=usage.cpu_usage, mem_usage=usage.mem_usage, gpu_usage=usage.gpu_usage,
            gpu_mem_usage=usage.gpu_mem_usage, battery=self._battery,
            running_tasks=self._own_tasks.tasks,
        )
        msg = convert.fill_node_status(NodeStatus(), view, self.cluster, reports if slow else None,
                                       TaskBrief, NodeLoad, LinkQuality)
        self.links.on_publish(self._seq, now)
        self._node_status_pub.publish(msg)
        own_links = tuple(LinkView(r.peer_name, r.rtt_ms, r.jitter_ms, r.loss_rate) for r in reports)
        self.cluster.update(replace(view, links=own_links), now)
        self.cluster.update_task_list(self._own_tasks, now)

    def _on_node_status(self, msg):
        if msg.node_id == self.node_id:
            return
        name = self.cluster.name_of(msg.node_id)
        if name is None:
            return                # its TaskList, which says its name, has not arrived yet
        now = time.monotonic()
        self.links.on_peer_status(name, msg.incarnation, msg.seq, now)
        echo = convert.echo_for(msg, self.node_id)
        if echo:
            self.links.on_peer_echo(name, echo[0], echo[1], now)
        self.cluster.update(convert.node_view_from_msg(msg, name, self.cluster), now)

    def _on_task_list(self, msg):
        if msg.node_id == self.node_id:
            return
        self.cluster.register(msg.node_name, msg.node_id)
        self.cluster.update_task_list(convert.task_list_from_msg(msg, self.cluster), time.monotonic())

    # ------------------------------------------------------- local executor
    def _on_executor_status(self, msg):
        """The executor's own account of its tasks. Only a change of it, never its silence,
        changes this node's TaskList (DESIGN 2.4)."""
        self._executor, self._executor_seen = msg, time.monotonic()
        tasks = tuple(convert.task_state_from_msg(t, self.cluster) for t in msg.tasks)
        tombstones = tuple(convert.uuid_str(t.task_id) for t in msg.tombstones)
        if (tasks, tombstones) != (self._own_tasks.tasks, self._own_tasks.tombstones):
            self._own_tasks = replace(self._own_tasks, tasks=tasks, tombstones=tombstones,
                                      tasks_version=self._own_tasks.tasks_version + 1)
            self._publish_task_list()

    def _publish_task_list(self):
        self._task_list_pub.publish(convert.fill_task_list(TaskList(), self._own_tasks, self.cluster,
                                                           TaskState, Tombstone))

    # ------------------------------------------------------- leader endpoints
    def _follow_role(self, leading: bool):
        """Create the leader's endpoints on election, destroy them on stepping down."""
        if leading and self._leader_endpoints is None:
            self._commands.attach(self.create_publisher(Command, names.COMMAND_TOPIC, qos.COMMAND))
            self._leader_endpoints = (
                self.create_publisher(Reply, names.REPLY_TOPIC, qos.REPLY),
                self.create_subscription(ExecEvent, names.EXEC_EVENT_TOPIC, self._on_exec_event, qos.EXEC_EVENT),
                self.create_subscription(Request, names.REQUEST_TOPIC, self._on_request, qos.REQUEST),
            )
        elif not leading and self._leader_endpoints is not None:
            reply_pub, events, requests = self._leader_endpoints
            self._leader_endpoints = None
            self.destroy_subscription(events)
            self.destroy_subscription(requests)
            self.destroy_publisher(reply_pub)
            command_pub = self._commands.detach()
            if command_pub is not None:
                self.destroy_publisher(command_pub)

    def _on_exec_event(self, msg):
        event = convert.exec_event_from_msg(msg, self.cluster)
        if event is not None:
            self.controller.on_event(event)

    def _on_request(self, msg):
        try:
            request = convert.request_from_msg(msg, self.cluster)
        except ValueError as exc:
            log.warning("unreadable request from node %d: %s", msg.origin_node, exc)
            return
        self.controller.on_request(request)

    def _send_reply(self, reply):
        """Controller thread. Replies to this node's own gateway skip the network."""
        if reply.origin_node == self.node_name:
            self.gateway.on_reply(reply)
            return
        endpoints = self._leader_endpoints
        if endpoints is not None:
            endpoints[0].publish(convert.fill_reply(Reply(), reply, self.cluster))

    def _on_reply(self, msg):
        if msg.origin_node == self.node_id:
            self.gateway.on_reply(convert.reply_from_msg(msg, self.cluster))

    # ------------------------------------------------------------ task events
    def _on_table_events(self, events):
        """Only the ready leader announces its table's changes."""
        if self.controller.active:
            self._outbox.put(("status", (events, self.controller.term)))

    def _on_task_status(self, msg):
        self.ue.on_status(StatusEvent(msg.seq, msg.task_id, msg.name, msg.task_type, msg.status, msg.epoch,
                                      msg.node, msg.message), msg.term)

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
            self.ue.on_result_image(msg.header.frame_id[len("task_"):], msg)

    # ------------------------------------------------------------ services
    def _on_create(self, request, response):
        reply = self.gateway.create(convert.spec_from_request(request), request.idempotency_key)
        return convert.fill_create_response(response, reply)

    def _on_destroy(self, request, response):
        return convert.fill_destroy_response(response, self.gateway.destroy(request.task_id, request.reason))

    def _on_query(self, request, response):
        return convert.fill_query_response(response, self.gateway.query(request.task_id))

    def shutdown(self):
        self._stop.set()
        if self._ue_api is not None:
            self._ue_api.shutdown()
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
