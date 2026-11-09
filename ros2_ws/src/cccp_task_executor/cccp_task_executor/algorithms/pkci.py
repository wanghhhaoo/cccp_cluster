"""PKCI: per-frame classification (collab_inference PkciEngine)."""
from collab_inference_interfaces.srv import Classify

from cccp_node.core import constants as c

from .. import annotate
from .base import Output, Step, StreamAlgorithm

CLASSIFY = "algorithms/pkci/classify"


class Pkci(StreamAlgorithm):
    name = c.PKCI
    services = {CLASSIFY: Classify}

    @staticmethod
    def handlers(engine):
        return {CLASSIFY: engine.classify}

    @staticmethod
    def serve():
        from collab_inference_ros.pkci_server import PkciServer
        return PkciServer()

    def step(self, task, image):
        task.frames += 1
        request = Classify.Request(image=image, drone_id=task.drone_id, frame_id=task.frames, level=task.level)
        return Step(self.backend.call(CLASSIFY, request), _output)

    def annotate(self, rgb, result):
        return annotate.label(rgb, "%s %.2f" % (result.class_name, result.score))


def _output(reply):
    return Output(
        fields=dict(label=reply.label, class_name=reply.class_name, score=reply.score,
                    confidence=reply.confidence, log_probs=list(reply.log_probs)),
        latency_ms=reply.latency_ms, payload=reply.increment,
        summary="%s (%.2f)" % (reply.class_name, reply.score))
