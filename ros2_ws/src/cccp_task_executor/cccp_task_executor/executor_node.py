"""Board-side task executor: the interface between the control plane and the data plane (DESIGN 2.7).

Takes commands from the cluster leader on /cluster/command, runs each task through
its algorithm's adapter (algorithms/): feeds frames from an image topic to a stream
algorithm, which reaches its engine through an inference backend (in-process calls or
ROS services, see inference.py), or starts a batch algorithm's process (batch.py).
Publishes per-frame results and composited images,
answers and reports on /cluster/exec_event, and publishes its running tasks and
tombstones on <ns>/exec/status (from its own callback group, so busy inference
callbacks do not starve it and make the node look stalled). That status also goes
out at once whenever the task set changes, before the event that reports the
change: the leader answers a create as soon as RUNNING arrives, and a leader
running the task itself may die right after; its TaskList, built from this status,
must already be on its way, or no node is left knowing the task (DESIGN 6.2).

Every command passes the ExecutionGuard first (DESIGN 3.8, CONVENTIONS 5.4):
commands for other nodes are ignored, commands from an older term are refused,
a repeated start of the same generation (epoch, assign_term) is answered
RUNNING again, a newer generation replaces the running session, a start beyond
max_tasks_per_node is refused BUSY, and a stop for a destroyed task leaves a
tombstone. Every command addressed here gets exactly one ExecEvent in return.

Frames that arrive while a task's previous frame is still being processed are
skipped, so each task always works on the latest frame and a stateful algorithm
(PKCT) receives its frames strictly in order. A primary that receives no frame at all for
input_timeout seconds reports the task Failed with NOT_READY: its input is
unreachable from this node, and the leader may move it (CONVENTIONS 5.5).
Helpers do not check, since only the primary's reports drive the task state.
"""
import threading
import time
from dataclasses import dataclass, field

import rclpy
from cv_bridge import CvBridge
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, Image

from cccp_cluster_interfaces.msg import Command, ExecEvent, ExecutorStatus, TaskState, Tombstone
from cccp_task_interfaces.msg import TaskResult

from cccp_node import names
from cccp_node.control import protocol as p
from cccp_node.core import constants as c
from cccp_node.core.execution_guard import IGNORE, NOOP, REJECT, RESTART, STOP, ExecutionGuard
from cccp_node.core.generation import Generation
from cccp_node.core.input_source import FETCH_EACH, parse_input, plan_input
from cccp_node.core.naming import default_node_id
from cccp_node.core.task_spec import TaskSpec
from cccp_node.ros import convert, qos

from . import algorithms
from .airsim_common import AirSimClient
from .airsim_feeds import AirSimFeeds
from .algorithms.base import parse_params
from .images import decompress
from .inference import RosServiceBackend
from .relay import ImageRelay
from . import batch

@dataclass
class ActiveTask:
    task_id: str
    epoch: int
    role: str
    task_type: str
    drone_id: int
    level: str
    image_topic: str             # topic actually subscribed (see input_source.plan_input)
    params: dict
    algorithm: object = None     # its adapter (algorithms/)
    state: dict = field(default_factory=dict)   # the adapter's per-session state (StreamAlgorithm.open)
    primary: str = ""            # name of the task primary (its namespace serves relayed input)
    primary_id: int = 0
    helper_ids: tuple = ()
    idempotency_key: str = ""
    name: str = ""
    spec_json: str = ""          # reported back in TaskState so a new leader can rebuild its table
    input: str = ""
    compressed: bool = False
    fetch: tuple = None          # (vehicle, camera) of the AirSim feed this task holds
    busy: bool = False
    frames: int = 0
    failures: int = 0
    job: object = None
    reported_first_result: bool = False
    last_input: float = 0.0      # time.monotonic() of the start or of the last frame received
    assign_term: int = 0         # with epoch, the generation of this session

    @property
    def generation(self):
        return Generation(self.epoch, self.assign_term)


class TaskExecutor(Node):
    def __init__(self, backend=None, airsim_client_factory=None):
        """backend: how stream algorithms reach their engines (inference.py); default calls their ROS services.
        airsim_client_factory: makes AirSim clients for airsim:// inputs; default uses airsim_host."""
        super().__init__("task_executor", start_parameter_services=False, enable_rosout=False)
        self.declare_parameter("node_name", "node-1")
        self.declare_parameter("node_id", 0)                  # 0: from the number node_name ends with
        self.declare_parameter("image_topic", "camera/image")
        self.declare_parameter("publish_annotated", True)
        self.declare_parameter("max_consecutive_failures", 10)
        self.declare_parameter("input_timeout", 10.0)         # no frame this long: report NOT_READY; 0 disables
        self.declare_parameter("max_tasks_per_node", 4)       # admission limit; must match the leader's scheduler
        self.declare_parameter("tombstone_ttl", 600.0)
        self.declare_parameter("service_wait_timeout", 2.0)
        self.declare_parameter("status_period", 1.0)
        self.declare_parameter("airsim_host", "")             # empty: airsim:// inputs are not served here
        self.declare_parameter("airsim_port", 41451)
        self.declare_parameter("airsim_fps", 10.0)
        self.declare_parameter("airsim_raw_encoding", "bgr8")
        self.declare_parameter("airsim_fetch_mode", FETCH_EACH)   # each | relay (input_source.py)
        self.declare_parameter("relay_topics", ["camera/image"])  # local sensors offered to other nodes
        self.declare_parameter("jpeg_quality", 80)

        self._node_name = self.get_parameter("node_name").value
        self._node_id = int(self.get_parameter("node_id").value) or default_node_id(self._node_name)
        self._default_topic = self.get_parameter("image_topic").value
        self._publish_annotated = bool(self.get_parameter("publish_annotated").value)
        self._max_failures = int(self.get_parameter("max_consecutive_failures").value)
        self._input_timeout = float(self.get_parameter("input_timeout").value)
        self._wait_timeout = float(self.get_parameter("service_wait_timeout").value)
        self._incarnation = int(time.time() * 1000)
        self._status_seq = 0
        self._status_lock = threading.Lock()    # the timer and command callbacks both publish: keep seq order
        self._event_seq = 0
        self._guard = ExecutionGuard(me=self._node_id, max_tasks=int(self.get_parameter("max_tasks_per_node").value),
                                     tombstone_ttl_s=float(self.get_parameter("tombstone_ttl").value))
        self._bridge = CvBridge()
        self._tasks = {}
        self._stopping = []          # batch.Job: stopped, not yet gone (batch.reap)
        self.create_timer(0.5, self._poll_jobs)
        self._subscriptions_by_topic = {}

        self._backend = backend if backend is not None else RosServiceBackend(self)
        self._algorithms = algorithms.create(self._backend)
        get = lambda name: self.get_parameter(name).value  # noqa: E731
        self._airsim_mode = get("airsim_fetch_mode")
        self._relay = ImageRelay(self, int(get("jpeg_quality")))
        for topic in get("relay_topics"):
            if topic:
                self._relay.watch(topic)
        if airsim_client_factory is None and get("airsim_host"):
            host, port = get("airsim_host"), int(get("airsim_port"))
            airsim_client_factory = lambda: AirSimClient(host, port)  # noqa: E731
        self._feeds = None
        if airsim_client_factory is not None:
            self._feeds = AirSimFeeds(self, airsim_client_factory, float(get("airsim_fps")),
                                      get("airsim_raw_encoding"), self._relay)

        self._results = self.create_publisher(TaskResult, names.TASK_RESULT_TOPIC, qos.TASK_RESULT)
        self._result_images = self.create_publisher(Image, names.TASK_RESULT_IMAGE_TOPIC, qos.TASK_RESULT_IMAGE)
        self._events = self.create_publisher(ExecEvent, names.EXEC_EVENT_TOPIC, qos.EXEC_EVENT)
        self._status = self.create_publisher(ExecutorStatus, names.EXEC_STATUS_TOPIC, qos.EXECUTOR_STATUS)
        self.create_subscription(Command, names.COMMAND_TOPIC, self._on_command, qos.COMMAND)
        self.create_timer(float(self.get_parameter("status_period").value), self._publish_status,
                          callback_group=MutuallyExclusiveCallbackGroup())
        if self._input_timeout > 0:
            self.create_timer(min(1.0, self._input_timeout / 2), self._check_inputs)
        self.get_logger().info("task executor ready (node_name=%s, node_id=%d, image_topic=%s)"
                               % (self._node_name, self._node_id, self._default_topic))

    # ------------------------------------------------------------------ status
    def _algorithms_ready(self):
        return [name for name, algorithm in self._algorithms.items() if algorithm.ready()]

    def _publish_status(self):
        with self._status_lock:
            self._publish_status_locked()

    def _publish_status_locked(self):
        self._status_seq += 1
        msg = ExecutorStatus()
        msg.incarnation = self._incarnation
        msg.seq = self._status_seq
        algorithms = self._algorithms_ready()
        msg.algorithms_ready = convert.algorithms_mask(algorithms)
        msg.inference_ready = bool(algorithms)
        msg.tasks = [TaskState(task_id=convert.uuid_bytes(t.task_id), epoch=t.epoch, assign_term=t.assign_term,
                               role=convert.ROLES.index(t.role), primary_node=t.primary_id,
                               helper_nodes=list(t.helper_ids), idempotency_key=t.idempotency_key,
                               name=convert.clip(t.name, convert.NAME_LENGTH), spec_json=t.spec_json)
                     for t in sorted(self._tasks.values(), key=lambda t: t.task_id)]
        msg.tombstones = [Tombstone(task_id=convert.uuid_bytes(t)) for t in self._guard.tombstones()]
        self._status.publish(msg)

    def _report(self, task_id, generation, kind, error_code="", message="", command=None):
        """One ExecEvent: the receipt for command, or a report of its own (request_id 0). Anything but a
        refusal may follow a change of the task set: the status goes first (see the module docstring)."""
        if kind != p.REJECTED:
            self._publish_status()
        self._event_seq += 1
        term, request_id = (command.term, command.request_id) if command is not None else (0, 0)
        self._events.publish(convert.fill_exec_event(
            ExecEvent(), self._node_id, self._incarnation, self._event_seq, term, request_id, task_id, generation,
            kind, error_code, message))

    # ------------------------------------------------------------------ commands
    def _on_command(self, msg):
        task_id = convert.uuid_str(msg.task_id)
        generation = Generation(msg.epoch, msg.assign_term)
        if msg.op == Command.OP_START:
            decision = self._guard.on_start(msg.term, task_id, generation, targets=msg.targets)
        else:
            decision = self._guard.on_stop(msg.term, task_id, generation, targets=msg.targets,
                                           cancel=convert.stop_kind(msg) == c.STOP_CANCEL)
        if decision.action == IGNORE:
            return
        if decision.action == REJECT:
            self.get_logger().info("task=%s gen=%s term=%d refused: %s" % (task_id, generation, msg.term,
                                                                          decision.message))
            self._report(task_id, generation, p.REJECTED, decision.error_code, decision.message, msg)
        elif msg.op == Command.OP_START:
            if decision.action == NOOP:
                self._report(task_id, generation, p.RUNNING, message=decision.message, command=msg)
            else:
                self._start(msg, task_id, generation, decision.action == RESTART)
        else:
            task = self._tasks.get(task_id)
            if decision.action == STOP and task is not None:
                self.get_logger().info("task=%s gen=%s stopped: %s (%s)" % (
                    task_id, task.generation, convert.stop_kind(msg), msg.reason))
                self._remove(task, p.STOPPED, msg.reason or "stopped by leader", command=msg)
            else:
                self._report(task_id, generation, p.STOPPED, message=decision.message, command=msg)

    def _start(self, msg, task_id, generation, restart):
        """Checks what the guard cannot (algorithms ready, input reachable), then starts the session."""
        try:
            spec = TaskSpec.from_json(msg.spec_json)
            task_type = spec.task_type.strip().upper()
            algorithm = self._algorithms.get(task_type)
            if algorithm is None:
                raise ValueError("unsupported algorithm: " + task_type)
            if generation.epoch <= 0:
                raise ValueError("epoch must be positive")
            params = parse_params(algorithm, spec.params_json)
            unavailable = algorithm.wait_ready(self._wait_timeout)
            if unavailable:
                self._report(task_id, generation, p.REJECTED, c.NOT_READY, unavailable, msg)
                return
            role = c.ROLE_PRIMARY if msg.primary_node == self._node_id else c.ROLE_HELPER
            plan = plan_input(parse_input(spec.input), node_name=self._node_name,
                              default_topic=self._default_topic, image_topic=spec.image_topic.strip(),
                              role=role, primary_node=msg.primary_name, airsim_mode=self._airsim_mode)
            if plan.fetch and self._feeds is None:
                self._report(task_id, generation, p.REJECTED, c.NOT_READY,
                             "%s cannot read %s: airsim_host is not set" % (self._node_name, spec.input), msg)
                return
            if restart:
                self._remove(self._tasks[task_id], p.STOPPED, "replaced by %s" % (generation,))
            task = ActiveTask(
                task_id=task_id,
                epoch=generation.epoch,
                assign_term=generation.assign_term,
                role=role,
                task_type=task_type,
                drone_id=int(spec.drone_id),
                level=spec.level or "full",
                image_topic=plan.topic,
                params=params,
                algorithm=algorithm,
                primary=msg.primary_name,
                primary_id=msg.primary_node,
                helper_ids=tuple(msg.helper_nodes),
                idempotency_key=msg.idempotency_key,
                name=msg.name,
                spec_json=msg.spec_json,
                input=spec.input,
                compressed=plan.compressed,
                fetch=plan.fetch,
                last_input=time.monotonic(),
            )
            if isinstance(algorithm, algorithms.BatchAlgorithm):
                try:
                    task.job = batch.start(task, self._node_id, algorithm)
                except FileExistsError:
                    # This generation already ran here and its session was lost (executor restart).
                    # Not permanent: the leader moves the task to a new generation.
                    self._report(task_id, generation, p.REJECTED, c.NOT_READY,
                                 "%s already ran %s %s" % (self._node_name, task_type, generation), msg)
                    return
                task.fetch = None
            else:
                algorithm.open(task)
            if task.fetch:
                self._feeds.acquire(*task.fetch)
            self._tasks[task.task_id] = task
            self._guard.started(task.task_id, task.generation)
            if task.job is None:
                self._ensure_subscription(task.image_topic, task.compressed)
            message = "%s started %s %s as %s, input %s via %s" % (
                self._node_name, task_type, task.generation, task.role, task.input or "local camera",
                self.resolve_topic_name(task.image_topic))
            if task.job is not None:
                message = f"{task_type} running on {self._node_name} as {task.role}"
            self._report(task_id, generation, p.RUNNING, message=message, command=msg)
            self.get_logger().info("task=%s gen=%s term=%d %s" % (task_id, task.generation, msg.term, message))
        except Exception as exc:
            self._report(task_id, generation, p.REJECTED, c.INVALID_INPUT, str(exc), msg)
            self.get_logger().error("task=%s start failed: %s" % (task_id, exc))

    # ------------------------------------------------------------------ frames
    def _ensure_subscription(self, topic, compressed=False):
        if topic not in self._subscriptions_by_topic:
            self._subscriptions_by_topic[topic] = self.create_subscription(
                CompressedImage if compressed else Image, topic,
                lambda msg, t=topic: self._on_frame(t, msg), qos.CAMERA,
            )

    def _release_subscription(self, topic):
        if any(t.image_topic == topic for t in self._tasks.values()):
            return
        subscription = self._subscriptions_by_topic.pop(topic, None)
        if subscription is not None:
            self.destroy_subscription(subscription)

    def _on_frame(self, topic, msg):
        if isinstance(msg, CompressedImage):    # frames streamed from another node
            try:
                msg = decompress(msg, Image)
            except Exception as exc:
                self.get_logger().warning("bad compressed frame on %s: %s" % (topic, exc),
                                          throttle_duration_sec=5.0)
                return
        now = time.monotonic()
        for task in list(self._tasks.values()):
            if task.job is not None or task.image_topic != topic:
                continue
            task.last_input = now
            if task.busy:
                continue
            task.busy = True
            step = task.algorithm.step(task, msg)
            step.future.add_done_callback(lambda f, task=task, step=step: self._on_step(task, msg, step, f))

    def destroy_node(self):
        batch.shutdown([t.job for t in self._tasks.values() if t.job is not None] + self._stopping)
        return super().destroy_node()

    def _poll_jobs(self):
        self._stopping = batch.reap(self._stopping)
        for task in list(self._tasks.values()):
            job = task.job
            if job is None or job.exited:
                continue
            code = job.process.poll()
            if code is None:
                continue
            job.exited = True
            if code:
                with (job.output / "run.log").open("rb") as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 1024))
                    lines = stream.read().decode(errors="replace").strip().splitlines()
                reason = lines[-1] if lines else "see run.log"
                self._remove(task, p.FAILED, f"{task.task_type} exit {code}: {reason}", c.INFERENCE_ERROR)
            elif task.role == c.ROLE_HELPER:
                # Keep the finished session until the leader stops it, which it does once the primary
                # finishes. Dropping it now would look like "not started yet" to reconciliation, which
                # would send START again (DESIGN, implementation adjustment 16).
                self.get_logger().info("task=%s gen=%s helper part finished, waiting for the primary"
                                       % (task.task_id, task.generation))
            else:
                self._remove(task, p.FINISHED, str(job.output))

    def _check_inputs(self):
        now = time.monotonic()
        for task in list(self._tasks.values()):
            idle = now - task.last_input
            if task.job is not None or task.role != c.ROLE_PRIMARY or idle < self._input_timeout:
                continue
            reason = "no input frames for %.0f s from %s (%s)" % (
                idle, task.input or "local camera", self.resolve_topic_name(task.image_topic))
            self.get_logger().error("task=%s gen=%s failed: %s" % (task.task_id, task.generation, reason))
            self._remove(task, p.FAILED, reason, c.NOT_READY)

    # ------------------------------------------------------------------ results
    def _new_result(self, task, msg):
        result = TaskResult()
        result.task_id = task.task_id
        result.epoch = task.epoch
        result.assign_term = task.assign_term
        result.node_name = self._node_name
        result.task_type = task.task_type
        result.drone_id = task.drone_id
        result.frame_id = task.frames
        result.source_stamp = msg.header.stamp
        return result

    def _reply(self, task, future, fatal):
        """Common completion handling; returns the service reply or None."""
        task.busy = False
        if self._tasks.get(task.task_id) is not task:
            return None  # stopped or replaced while the call was in flight
        try:
            reply = future.result()
        except Exception as exc:
            self._failure(task, "service call failed: %s" % exc, fatal)
            return None
        if not reply.success:
            self._failure(task, reply.message, fatal)
            return None
        task.failures = 0
        return reply

    def _on_step(self, task, msg, step, future):
        reply = self._reply(task, future, step.fatal)
        if reply is None:
            return
        output = step.finish(reply)
        result = self._new_result(task, msg)
        result.success = result.valid = True
        result.message = output.message
        result.latency_ms = output.latency_ms
        for name, value in output.fields.items():
            setattr(result, name, value)
        result.payload = output.payload
        self._publish(task, msg, result, output.summary)

    def _publish(self, task, msg, result, summary):
        self._results.publish(result)
        if self._publish_annotated and self._result_images.get_subscription_count() > 0:
            try:
                rgb = self._bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
                image = self._bridge.cv2_to_imgmsg(task.algorithm.annotate(rgb, result), encoding="rgb8")
                image.header.stamp = msg.header.stamp
                # frame_id identifies the task: several tasks may share one board.
                image.header.frame_id = "task_%s" % task.task_id
                self._result_images.publish(image)
            except Exception as exc:
                self.get_logger().warning("result image for task %s failed: %s" % (task.task_id, exc))
        if summary and not task.reported_first_result:
            task.reported_first_result = True
            self.get_logger().info("task=%s gen=%s first result: %s" % (task.task_id, task.generation, summary))

    def _failure(self, task, reason, fatal=""):
        """fatal: a failure of this step ends the task at once (Step.fatal), with this prefix."""
        task.failures += 1
        self.get_logger().warning(
            "task=%s gen=%s frame failed (%d/%d): %s"
            % (task.task_id, task.generation, task.failures, self._max_failures, reason)
        )
        if fatal:
            reason = fatal + reason
        elif task.failures < self._max_failures:
            return
        self._remove(task, p.FAILED, reason, c.INFERENCE_ERROR)
        self.get_logger().error("task=%s gen=%s failed: %s" % (task.task_id, task.generation, reason))

    def _remove(self, task, kind, message, error_code="", command=None):
        if task.job is not None and batch.stop(task.job):
            self._stopping.append(task.job)
        self._tasks.pop(task.task_id, None)
        self._guard.stopped(task.task_id)
        self._release_subscription(task.image_topic)
        if task.fetch and self._feeds is not None:
            self._feeds.release(*task.fetch)
        if task.job is None:
            task.algorithm.close(task)
        self._report(task.task_id, task.generation, kind, error_code, message, command)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = TaskExecutor()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
