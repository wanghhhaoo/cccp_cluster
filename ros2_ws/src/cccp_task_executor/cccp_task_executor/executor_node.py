"""Board-side task executor.

Receives tasks from the cluster leader (<ns>/exec/task/start, <ns>/exec/task/stop),
feeds frames from an image topic to the local PKCI/PKCT services, publishes
per-frame results and composited images, reports state changes on
/cluster/task_report and publishes its running tasks on <ns>/exec/status.

Every command passes the ExecutionGuard first (CONVENTIONS 5.4): commands from
an older term are rejected, a repeated start of the same epoch is a no-op, and
a newer epoch replaces the running session.

Frames that arrive while a task's previous frame is still being processed are
skipped, so each task always works on the latest frame and PKCT receives its
frames strictly in order.
"""
import json
import time
from dataclasses import dataclass

import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import Image

from cccp_cluster_interfaces.msg import ExecutorStatus, RunningTask
from cccp_task_interfaces.msg import TaskReport, TaskResult
from cccp_task_interfaces.srv import StartTask, StopTask
from collab_inference_interfaces.srv import Classify, InitTracker, ResetTracker, Track

from cccp_node import names
from cccp_node.core import constants as c
from cccp_node.core.execution_guard import NOOP, REJECT, RESTART, STOP, ExecutionGuard
from cccp_node.ros import qos

from .annotate import annotate

TASK_TYPES = ("PKCI", "PKCT")


def parse_params(task_type, params_json):
    """Validate StartTask.params_json; returns the decoded dict."""
    params = json.loads(params_json) if params_json.strip() else {}
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON object")
    if task_type == "PKCT":
        bbox = params.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("PKCT requires params_json {\"bbox\": [x, y, width, height]}")
        bbox = [float(v) for v in bbox]
        if bbox[2] <= 0 or bbox[3] <= 0:
            raise ValueError("PKCT bbox width and height must be positive")
        params["bbox"] = bbox
    return params


def tracker_id(task_id, epoch):
    """PKCT sessions are isolated per task and epoch (CONVENTIONS 4.1)."""
    return "task-%d-%d" % (task_id, epoch)


@dataclass
class ActiveTask:
    task_id: int
    epoch: int
    role: str
    task_type: str
    drone_id: int
    level: str
    image_topic: str
    params: dict
    tracker_id: str = ""
    initialized: bool = False
    busy: bool = False
    frames: int = 0
    failures: int = 0
    reported_first_result: bool = False


class TaskExecutor(Node):
    def __init__(self):
        super().__init__("task_executor")
        self.declare_parameter("node_name", "node-1")
        self.declare_parameter("image_topic", "camera/image")
        self.declare_parameter("publish_annotated", True)
        self.declare_parameter("max_consecutive_failures", 10)
        self.declare_parameter("service_wait_timeout", 2.0)
        self.declare_parameter("status_period", 1.0)

        self._node_name = self.get_parameter("node_name").value
        self._default_topic = self.get_parameter("image_topic").value
        self._publish_annotated = bool(self.get_parameter("publish_annotated").value)
        self._max_failures = int(self.get_parameter("max_consecutive_failures").value)
        self._wait_timeout = float(self.get_parameter("service_wait_timeout").value)
        self._incarnation = int(time.time() * 1000)
        self._status_seq = 0
        self._report_seq = 0
        self._guard = ExecutionGuard()
        self._bridge = CvBridge()
        self._tasks = {}
        self._subscriptions_by_topic = {}

        # Relative names resolve inside this node's namespace, next to the
        # algorithm services of the same data-plane process.
        self._cli_classify = self.create_client(Classify, "algorithms/pkci/classify")
        self._cli_init = self.create_client(InitTracker, "algorithms/pkct/init")
        self._cli_track = self.create_client(Track, "algorithms/pkct/track")
        self._cli_reset = self.create_client(ResetTracker, "algorithms/pkct/reset")

        self._results = self.create_publisher(TaskResult, names.TASK_RESULT_TOPIC, qos.TASK_RESULT)
        self._result_images = self.create_publisher(Image, names.TASK_RESULT_IMAGE_TOPIC, qos.TASK_RESULT_IMAGE)
        self._reports = self.create_publisher(TaskReport, names.TASK_REPORT_TOPIC, qos.TASK_REPORT)
        self._status = self.create_publisher(ExecutorStatus, names.EXEC_STATUS_TOPIC, qos.EXECUTOR_STATUS)
        self.create_service(StartTask, names.EXEC_START_SERVICE, self._on_start)
        self.create_service(StopTask, names.EXEC_STOP_SERVICE, self._on_stop)
        self.create_timer(float(self.get_parameter("status_period").value), self._publish_status)
        self.get_logger().info("task executor ready (node_name=%s, image_topic=%s)"
                               % (self._node_name, self._default_topic))

    # ------------------------------------------------------------------ status
    def _algorithms_ready(self):
        ready = []
        if self._cli_classify.service_is_ready():
            ready.append("PKCI")
        if self._cli_init.service_is_ready() and self._cli_track.service_is_ready():
            ready.append("PKCT")
        return ready

    def _publish_status(self):
        self._status_seq += 1
        msg = ExecutorStatus()
        msg.node_name = self._node_name
        msg.incarnation = self._incarnation
        msg.seq = self._status_seq
        msg.algorithms_ready = self._algorithms_ready()
        msg.inference_ready = bool(msg.algorithms_ready)
        msg.running_tasks = [RunningTask(task_id=t.task_id, epoch=t.epoch, role=t.role)
                             for t in sorted(self._tasks.values(), key=lambda t: t.task_id)]
        msg.max_term = self._guard.max_term
        self._status.publish(msg)

    def _report(self, task, state, error_code="", message=""):
        self._report_seq += 1
        msg = TaskReport()
        msg.node_name = self._node_name
        msg.task_id = task.task_id
        msg.epoch = task.epoch
        msg.state = state
        msg.error_code = error_code
        msg.message = message
        msg.incarnation = self._incarnation
        msg.seq = self._report_seq
        msg.stamp = self.get_clock().now().to_msg()
        self._reports.publish(msg)

    # ------------------------------------------------------------------ services
    def _on_start(self, request, response):
        decision = self._guard.on_start(int(request.term), int(request.task_id), int(request.epoch))
        response.success = decision.success
        response.error_code = decision.error_code
        response.message = decision.message
        if decision.action in (REJECT, NOOP):
            return response
        try:
            task_type = request.task_type.strip().upper()
            if task_type not in TASK_TYPES:
                raise ValueError("task_type must be PKCI or PKCT")
            if request.task_id <= 0 or request.epoch <= 0:
                raise ValueError("task_id and epoch must be positive")
            params = parse_params(task_type, request.params_json)
            needed = [self._cli_classify] if task_type == "PKCI" else [self._cli_init, self._cli_track]
            for client in needed:
                if not client.wait_for_service(timeout_sec=self._wait_timeout):
                    response.success, response.error_code = False, c.NOT_READY
                    response.message = "algorithm service %s is not available" % client.srv_name
                    return response
            if decision.action == RESTART:
                self._remove(self._tasks[int(request.task_id)], c.REPORT_STOPPED, "replaced by a newer epoch")
            task = ActiveTask(
                task_id=int(request.task_id),
                epoch=int(request.epoch),
                role=request.role or c.ROLE_PRIMARY,
                task_type=task_type,
                drone_id=int(request.drone_id),
                level=request.level or "full",
                image_topic=request.image_topic.strip() or self._default_topic,
                params=params,
                tracker_id=tracker_id(int(request.task_id), int(request.epoch)),
            )
            self._tasks[task.task_id] = task
            self._guard.started(task.task_id, task.epoch)
            self._ensure_subscription(task.image_topic)
            message = "%s started %s epoch %d as %s, input %s" % (
                self._node_name, task_type, task.epoch, task.role, self.resolve_topic_name(task.image_topic))
            self._report(task, c.REPORT_RUNNING, message=message)
            response.success, response.error_code, response.message = True, "", message
            self.get_logger().info("task=%d epoch=%d term=%d %s" % (task.task_id, task.epoch, request.term, message))
        except Exception as exc:
            response.success, response.error_code, response.message = False, c.INVALID_INPUT, str(exc)
            self.get_logger().error("task=%d start failed: %s" % (request.task_id, exc))
        return response

    def _on_stop(self, request, response):
        decision = self._guard.on_stop(int(request.term), int(request.task_id), int(request.epoch))
        response.success, response.error_code, response.message = decision.success, decision.error_code, \
            decision.message
        if decision.action == STOP:
            task = self._tasks.get(int(request.task_id))
            if task is not None:
                self._remove(task, c.REPORT_STOPPED, request.reason or "stopped by leader")
                response.message = "task %d epoch %d stopped on %s" % (task.task_id, task.epoch, self._node_name)
                self.get_logger().info("task=%d epoch=%d %s (%s)" % (task.task_id, task.epoch,
                                                                     response.message, request.reason))
        return response

    # ------------------------------------------------------------------ frames
    def _ensure_subscription(self, topic):
        if topic not in self._subscriptions_by_topic:
            self._subscriptions_by_topic[topic] = self.create_subscription(
                Image, topic, lambda msg, t=topic: self._on_frame(t, msg), qos.CAMERA,
            )

    def _release_subscription(self, topic):
        if any(t.image_topic == topic for t in self._tasks.values()):
            return
        subscription = self._subscriptions_by_topic.pop(topic, None)
        if subscription is not None:
            self.destroy_subscription(subscription)

    def _on_frame(self, topic, msg):
        for task in list(self._tasks.values()):
            if task.image_topic != topic or task.busy:
                continue
            task.busy = True
            if task.task_type == "PKCI":
                self._run_classify(task, msg)
            elif not task.initialized:
                self._run_init(task, msg)
            else:
                self._run_track(task, msg)

    def _run_classify(self, task, msg):
        task.frames += 1
        request = Classify.Request(
            image=msg, drone_id=task.drone_id, frame_id=task.frames, level=task.level
        )
        future = self._cli_classify.call_async(request)
        future.add_done_callback(lambda f: self._on_classified(task, msg, f))

    def _run_init(self, task, msg):
        x, y, width, height = task.params["bbox"]
        request = InitTracker.Request(
            tracker_id=task.tracker_id, first_frame=msg, x=x, y=y, width=width, height=height
        )
        future = self._cli_init.call_async(request)
        future.add_done_callback(lambda f: self._on_initialized(task, msg, f))

    def _run_track(self, task, msg):
        task.frames += 1
        request = Track.Request(
            tracker_id=task.tracker_id, frame=msg, drone_id=task.drone_id,
            frame_id=task.frames, level=task.level,
        )
        future = self._cli_track.call_async(request)
        future.add_done_callback(lambda f: self._on_tracked(task, msg, f))

    # ------------------------------------------------------------------ results
    def _new_result(self, task, msg):
        result = TaskResult()
        result.task_id = task.task_id
        result.epoch = task.epoch
        result.node_name = self._node_name
        result.task_type = task.task_type
        result.drone_id = task.drone_id
        result.frame_id = task.frames
        result.source_stamp = msg.header.stamp
        return result

    def _reply(self, task, future):
        """Common completion handling; returns the service reply or None."""
        task.busy = False
        if self._tasks.get(task.task_id) is not task:
            return None  # stopped or replaced while the call was in flight
        try:
            reply = future.result()
        except Exception as exc:
            self._failure(task, "service call failed: %s" % exc)
            return None
        if not reply.success:
            self._failure(task, reply.message)
            return None
        task.failures = 0
        return reply

    def _on_classified(self, task, msg, future):
        reply = self._reply(task, future)
        if reply is None:
            return
        result = self._new_result(task, msg)
        result.success = result.valid = True
        result.message = "ok"
        result.latency_ms = reply.latency_ms
        result.label = reply.label
        result.class_name = reply.class_name
        result.score = reply.score
        result.confidence = reply.confidence
        result.log_probs = list(reply.log_probs)
        result.payload = reply.increment
        self._publish(task, msg, result, "%s (%.2f)" % (reply.class_name, reply.score))

    def _on_initialized(self, task, msg, future):
        reply = self._reply(task, future)
        if reply is None:
            return
        task.initialized = True
        result = self._new_result(task, msg)
        result.success = result.valid = True
        result.message = "tracker initialized"
        result.x, result.y, result.width, result.height = task.params["bbox"]
        result.confidence = 1.0
        self._publish(task, msg, result, None)

    def _on_tracked(self, task, msg, future):
        reply = self._reply(task, future)
        if reply is None:
            return
        result = self._new_result(task, msg)
        result.success = result.valid = True
        result.message = "ok"
        result.latency_ms = reply.latency_ms
        result.x, result.y = reply.x, reply.y
        result.width, result.height = reply.width, reply.height
        result.confidence = reply.confidence
        result.payload = reply.identity
        self._publish(task, msg, result, "box (%.0f, %.0f, %.0f, %.0f) conf %.2f" % (
            reply.x, reply.y, reply.width, reply.height, reply.confidence))

    def _publish(self, task, msg, result, summary):
        self._results.publish(result)
        if self._publish_annotated and self._result_images.get_subscription_count() > 0:
            try:
                rgb = self._bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
                image = self._bridge.cv2_to_imgmsg(annotate(rgb, result), encoding="rgb8")
                image.header.stamp = msg.header.stamp
                # frame_id identifies the task: several tasks may share one board.
                image.header.frame_id = "task_%d" % task.task_id
                self._result_images.publish(image)
            except Exception as exc:
                self.get_logger().warning("result image for task %d failed: %s" % (task.task_id, exc))
        if summary and not task.reported_first_result:
            task.reported_first_result = True
            self.get_logger().info("task=%d epoch=%d first result: %s" % (task.task_id, task.epoch, summary))

    def _failure(self, task, reason):
        task.failures += 1
        self.get_logger().warning(
            "task=%d epoch=%d frame failed (%d/%d): %s"
            % (task.task_id, task.epoch, task.failures, self._max_failures, reason)
        )
        if not task.initialized and task.task_type == "PKCT":
            reason = "tracker init failed: " + reason
        elif task.failures < self._max_failures:
            return
        self._remove(task, c.REPORT_FAILED, reason, c.INFERENCE_ERROR)
        self.get_logger().error("task=%d epoch=%d failed: %s" % (task.task_id, task.epoch, reason))

    def _remove(self, task, state, message, error_code=""):
        self._tasks.pop(task.task_id, None)
        self._guard.stopped(task.task_id)
        self._release_subscription(task.image_topic)
        if task.task_type == "PKCT" and self._cli_reset.service_is_ready():
            self._cli_reset.call_async(ResetTracker.Request(tracker_id=task.tracker_id))
        self._report(task, state, error_code, message)


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
