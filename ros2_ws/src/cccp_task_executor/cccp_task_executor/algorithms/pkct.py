"""PKCT: stateful single-object tracking (collab_inference PkctEngine).

A tracker session per task and generation (CONVENTIONS 4.1): initialized from params.bbox on the
first frame, advanced by one frame per step, released when the session ends.
"""
from collab_inference_interfaces.srv import InitTracker, ResetTracker, Track

from cccp_node.core import constants as c

from .. import annotate
from .base import Output, Step, StreamAlgorithm

INIT, TRACK, RESET = "algorithms/pkct/init", "algorithms/pkct/track", "algorithms/pkct/reset"


def tracker_id(task_id, generation):
    return "task-%s-%d-%d" % (task_id, *generation)


class Pkct(StreamAlgorithm):
    name = c.PKCT
    services = {INIT: InitTracker, TRACK: Track}
    optional = {RESET: ResetTracker}

    @staticmethod
    def handlers(engine):
        return {INIT: engine.init_tracker, TRACK: engine.track, RESET: engine.reset}

    @staticmethod
    def serve():
        from collab_inference_ros.pkct_server import PkctServer
        return PkctServer()

    def normalize_params(self, params):
        params["bbox"] = [float(v) for v in params["bbox"]]
        return params

    def open(self, task):
        task.state.update(tracker_id=tracker_id(task.task_id, task.generation), initialized=False)

    def step(self, task, image):
        if not task.state["initialized"]:
            x, y, width, height = task.params["bbox"]
            request = InitTracker.Request(tracker_id=task.state["tracker_id"], first_frame=image,
                                          x=x, y=y, width=width, height=height)
            return Step(self.backend.call(INIT, request), lambda reply: _initialized(task),
                        fatal="tracker init failed: ")
        task.frames += 1
        request = Track.Request(tracker_id=task.state["tracker_id"], frame=image, drone_id=task.drone_id,
                                frame_id=task.frames, level=task.level)
        return Step(self.backend.call(TRACK, request), _tracked)

    def close(self, task):
        if self.backend.ready(RESET):
            self.backend.call(RESET, ResetTracker.Request(tracker_id=task.state["tracker_id"]))

    def annotate(self, rgb, result):
        return annotate.box(rgb, result.x, result.y, result.width, result.height, "conf %.2f" % result.confidence)


def _initialized(task):
    task.state["initialized"] = True
    x, y, width, height = task.params["bbox"]
    return Output(fields=dict(x=x, y=y, width=width, height=height, confidence=1.0), message="tracker initialized")


def _tracked(reply):
    return Output(
        fields=dict(x=reply.x, y=reply.y, width=reply.width, height=reply.height, confidence=reply.confidence),
        latency_ms=reply.latency_ms, payload=reply.identity,
        summary="box (%.0f, %.0f, %.0f, %.0f) conf %.2f" % (
            reply.x, reply.y, reply.width, reply.height, reply.confidence))
