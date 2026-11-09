"""How stream algorithms reach their engines: services by name (algorithms/*.py declares them).

LocalBackend calls the engines of the same process directly (the default in the data-plane
process), so no image is serialized per frame. RosServiceBackend calls <ns>/algorithms/*
services, for algorithms running in another process or written in another language.

Both return futures with add_done_callback(fn(future)) and result(), the interface of rclpy
service futures.
"""
from concurrent.futures import ThreadPoolExecutor


class LocalBackend:
    def __init__(self, handlers=None, workers=2):
        """handlers: service name -> engine method(request, response) (StreamAlgorithm.handlers)."""
        self._handlers = dict(handlers or {})
        self._types = {}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inference")

    def declare(self, service, srv_type):
        self._types[service] = srv_type

    def ready(self, service):
        return service in self._handlers

    def wait_ready(self, service, timeout_sec):
        """'' if the service can be called, else a reason."""
        return "" if self.ready(service) else "%s is not loaded in this process" % service

    def call(self, service, request):
        return self._pool.submit(self._handlers[service], request, self._types[service].Response())

    def shutdown(self):
        self._pool.shutdown(wait=False)


class RosServiceBackend:
    def __init__(self, node):
        self._node = node
        self._clients = {}

    def declare(self, service, srv_type):
        self._clients[service] = self._node.create_client(srv_type, service)

    def ready(self, service):
        return self._clients[service].service_is_ready()

    def wait_ready(self, service, timeout_sec):
        client = self._clients[service]
        if not client.wait_for_service(timeout_sec=timeout_sec):
            return "algorithm service %s is not available" % client.srv_name
        return ""

    def call(self, service, request):
        return self._clients[service].call_async(request)

    def shutdown(self):
        pass
