"""How the task executor reaches the PKCI/PKCT algorithms.

LocalBackend calls the engines of the same process directly (the default in
the data-plane process): no image serialization per frame.
RosServiceBackend calls <ns>/algorithms/* services, for algorithms running in
another process or written in another language.

Both return futures with add_done_callback(fn(future)) and result(), the
interface the executor already used with rclpy service futures.
"""
from concurrent.futures import ThreadPoolExecutor

from collab_inference_interfaces.srv import Classify, InitTracker, ResetTracker, Track


class LocalBackend:
    def __init__(self, pkci_engine=None, pkct_engine=None, workers=2):
        self._pkci = pkci_engine
        self._pkct = pkct_engine
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inference")

    def algorithms_ready(self):
        return [name for name, engine in (("PKCI", self._pkci), ("PKCT", self._pkct)) if engine is not None]

    def wait_ready(self, task_type, timeout_sec):
        """'' if the algorithm can run, else a reason."""
        return "" if task_type in self.algorithms_ready() else "%s is not loaded in this process" % task_type

    def classify(self, request):
        return self._pool.submit(self._pkci.classify, request, Classify.Response())

    def init_tracker(self, request):
        return self._pool.submit(self._pkct.init_tracker, request, InitTracker.Response())

    def track(self, request):
        return self._pool.submit(self._pkct.track, request, Track.Response())

    def reset(self, request):
        if self._pkct is not None:
            self._pool.submit(self._pkct.reset, request, ResetTracker.Response())

    def shutdown(self):
        self._pool.shutdown(wait=False)


class RosServiceBackend:
    def __init__(self, node):
        self._classify = node.create_client(Classify, "algorithms/pkci/classify")
        self._init = node.create_client(InitTracker, "algorithms/pkct/init")
        self._track = node.create_client(Track, "algorithms/pkct/track")
        self._reset = node.create_client(ResetTracker, "algorithms/pkct/reset")

    def _needed(self, task_type):
        return [self._classify] if task_type == "PKCI" else [self._init, self._track]

    def algorithms_ready(self):
        return [t for t in ("PKCI", "PKCT") if all(c.service_is_ready() for c in self._needed(t))]

    def wait_ready(self, task_type, timeout_sec):
        for client in self._needed(task_type):
            if not client.wait_for_service(timeout_sec=timeout_sec):
                return "algorithm service %s is not available" % client.srv_name
        return ""

    def classify(self, request):
        return self._classify.call_async(request)

    def init_tracker(self, request):
        return self._init.call_async(request)

    def track(self, request):
        return self._track.call_async(request)

    def reset(self, request):
        if self._reset.service_is_ready():
            self._reset.call_async(request)

    def shutdown(self):
        pass
