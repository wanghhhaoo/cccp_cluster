"""A small stand-in for rclpy and the generated interface packages, for machines without ROS.

Message classes are generated from the .msg/.srv files in this workspace and,
like rosidl, reject unknown fields and wrongly typed values (a float field
does not accept an int). An in-process bus connects publishers to
subscriptions and clients to services by fully resolved name, so several
nodes can run in one test process. Not a simulation of DDS: delivery is
synchronous and lossless, and QoS is ignored.

install() is a no-op when the real rclpy is importable.
"""
import array
import logging
import re
import sys
import threading
import time
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
PACKAGES = ("cccp_cluster_interfaces", "cccp_task_interfaces", "collab_inference_interfaces")
INLINE = {
    "builtin_interfaces/Time": "int32 sec\nuint32 nanosec\n",
    "std_msgs/Header": "builtin_interfaces/Time stamp\nstring frame_id\n",
    "sensor_msgs/Image": ("std_msgs/Header header\nuint32 height\nuint32 width\nstring encoding\n"
                          "uint8 is_bigendian\nuint32 step\nuint8[] data\n"),
    "sensor_msgs/CompressedImage": "std_msgs/Header header\nstring format\nuint8[] data\n",
}
INTS = {"byte", "char", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"}
FLOATS = {"float32", "float64"}
REGISTRY = {}     # "pkg/Name" -> class


# ---------------------------------------------------------------- messages

def _parse(text, pkg):
    fields, constants = {}, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        const = re.match(r'^(\S+)\s+([A-Z][A-Z0-9_]*)\s*=\s*(.+)$', line)
        if const:
            typ, name, value = const.groups()
            constants[name] = value.strip().strip('"') if typ == "string" else int(value.split("#")[0])
            continue
        typ, name = line.split("#")[0].split()[:2]
        array_field = typ.endswith("[]")
        base = typ[:-2] if array_field else typ
        if base not in INTS | FLOATS | {"bool", "string"} and "/" not in base:
            base = "%s/%s" % (pkg, base)
        fields[name] = (base, array_field)
    return fields, constants


def _scalar_ok(base, value):
    if base in INTS:
        return isinstance(value, int) and (not base.startswith("u") or value >= 0)
    if base in FLOATS:
        return isinstance(value, float)
    if base == "bool":
        return isinstance(value, bool)
    if base == "string":
        return isinstance(value, str)
    return isinstance(value, REGISTRY[base])


def _default(base, array_field):
    if array_field:
        return []
    if base in INTS:
        return 0
    if base in FLOATS:
        return 0.0
    if base == "bool":
        return False
    if base == "string":
        return ""
    return REGISTRY[base]()


def _make(full_name, text):
    pkg, name = full_name.split("/")
    fields, constants = _parse(text, pkg)

    def __init__(self, **values):
        for field, (base, array_field) in fields.items():
            object.__setattr__(self, field, _default(base, array_field))
        for key, value in values.items():
            setattr(self, key, value)

    def __setattr__(self, key, value):
        if key not in fields:
            raise AttributeError("%s has no field %r" % (full_name, key))
        base, array_field = fields[key]
        if array_field:
            if base == "uint8" and isinstance(value, (bytes, bytearray)):
                value = list(value)
            if not isinstance(value, (list, tuple, array.array)) or not all(_scalar_ok(base, v) for v in value):
                raise AssertionError("%s.%s must be a sequence of %s" % (full_name, key, base))
            value = list(value)
        elif not _scalar_ok(base, value):
            raise AssertionError("%s.%s must be of type %s, got %r" % (full_name, key, base, value))
        object.__setattr__(self, key, value)

    def __repr__(self):
        return "%s(%s)" % (name, ", ".join("%s=%r" % (f, getattr(self, f)) for f in fields))

    cls = type(name, (), dict(constants, __slots__=tuple(fields), __init__=__init__, __setattr__=__setattr__,
                              __repr__=__repr__, _fields=fields))
    REGISTRY[full_name] = cls
    return cls


def _module(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _build_interfaces():
    for full_name, text in INLINE.items():
        _make(full_name, text)
    for pkg in PACKAGES:
        msgs = {}
        for path in sorted((SRC / pkg / "msg").glob("*.msg")):
            msgs[path.stem] = path.read_text()
        pending = dict(msgs)
        while pending:     # create nested types first
            for stem, text in list(pending.items()):
                deps = re.findall(r"^(\w+)(?:\[\])?\s", text, re.M)
                if all(d in INTS | FLOATS | {"bool", "string"} or d not in pending or d == stem for d in deps):
                    _make("%s/%s" % (pkg, stem), text)
                    del pending[stem]
        srvs = {}
        for path in sorted((SRC / pkg / "srv").glob("*.srv")):
            request, response = path.read_text().split("\n---\n")
            srvs[path.stem] = type(path.stem, (), {
                "Request": _make("%s/%s_Request" % (pkg, path.stem), request),
                "Response": _make("%s/%s_Response" % (pkg, path.stem), response),
            })
        _module(pkg)
        _module(pkg + ".msg", **{s: REGISTRY["%s/%s" % (pkg, s)] for s in msgs})
        _module(pkg + ".srv", **srvs)
    _module("builtin_interfaces")
    _module("builtin_interfaces.msg", Time=REGISTRY["builtin_interfaces/Time"])
    _module("std_msgs")
    _module("std_msgs.msg", Header=REGISTRY["std_msgs/Header"])
    _module("sensor_msgs")
    _module("sensor_msgs.msg", Image=REGISTRY["sensor_msgs/Image"],
            CompressedImage=REGISTRY["sensor_msgs/CompressedImage"])


# --------------------------------------------------------------------- bus

class Bus:
    def __init__(self):
        self.lock = threading.RLock()
        self.subscriptions = {}    # topic -> [Subscription]
        self.services = {}         # name -> Service
        self.timers = []
        self.namespace = "/"
        self.params = {}

    def reset(self):
        with self.lock:
            self.subscriptions.clear()
            self.services.clear()
            self.timers.clear()

    def run_timers(self, stop, step=0.01):
        """Fire every live timer at its period until stop is set (one thread for all nodes)."""
        while not stop.is_set():
            now = time.monotonic()
            with self.lock:
                due = [t for t in self.timers if t.alive and now >= t.next_at]
            for timer in due:
                timer.next_at = now + timer.period
                try:
                    timer.callback()
                except Exception:
                    logging.getLogger("fake_ros").exception("timer callback failed")
            stop.wait(step)


BUS = Bus()


class _Entity:
    alive = True


class Publisher(_Entity):
    def __init__(self, node, msg_type, topic):
        self.node, self.msg_type, self.topic = node, msg_type, topic

    @property
    def topic_name(self):
        return self.topic

    def publish(self, msg):
        assert isinstance(msg, self.msg_type), "publishing %r on %s" % (type(msg), self.topic)
        if not (self.alive and self.node.alive):
            return
        with BUS.lock:
            subscribers = [s for s in BUS.subscriptions.get(self.topic, []) if s.alive and s.node.alive]
        for subscription in subscribers:
            subscription.callback(msg)

    def get_subscription_count(self):
        with BUS.lock:
            return len([s for s in BUS.subscriptions.get(self.topic, []) if s.alive and s.node.alive])


class Subscription(_Entity):
    def __init__(self, node, msg_type, topic, callback):
        self.node, self.msg_type, self.topic, self.callback = node, msg_type, topic, callback


class Service(_Entity):
    def __init__(self, node, srv_type, name, callback):
        self.node, self.srv_type, self.name, self.callback = node, srv_type, name, callback


class Future:
    def __init__(self):
        self._done = threading.Event()
        self._result = None
        self._callbacks = []
        self._lock = threading.Lock()

    def set_result(self, result):
        with self._lock:
            self._result = result
            self._done.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)

    def add_done_callback(self, callback):
        with self._lock:
            if not self._done.is_set():
                self._callbacks.append(callback)
                return
        callback(self)

    def done(self):
        return self._done.is_set()

    def result(self):
        return self._result


class Client(_Entity):
    def __init__(self, node, srv_type, name):
        self.node, self.srv_type, self.srv_name = node, srv_type, name

    def _service(self):
        with BUS.lock:
            service = BUS.services.get(self.srv_name)
        return service if service and service.alive and service.node.alive else None

    def service_is_ready(self):
        return self._service() is not None

    def wait_for_service(self, timeout_sec=None):
        deadline = time.monotonic() + (timeout_sec or 0)
        while time.monotonic() <= deadline:
            if self.service_is_ready():
                return True
            time.sleep(0.01)
        return self.service_is_ready()

    def call_async(self, request):
        assert isinstance(request, self.srv_type.Request)
        future = Future()
        service = self._service()
        if service is not None:
            def serve():
                response = service.callback(request, self.srv_type.Response())
                if service.node.alive:
                    future.set_result(response)
            threading.Thread(target=serve, daemon=True).start()
        return future

    def remove_pending_request(self, future):
        pass


class Timer(_Entity):
    def __init__(self, node, period, callback):
        self.node, self.period, self.callback = node, period, callback
        self.next_at = time.monotonic() + period

    @property
    def alive(self):
        return self.node.alive


class _Parameter:
    def __init__(self, value):
        self.value = value


class _Logger:
    def __init__(self, name):
        self._log = logging.getLogger(name)

    def _emit(self, level, msg, **_):
        self._log.log(level, msg)

    def debug(self, msg, **kw):
        self._emit(logging.DEBUG, msg, **kw)

    def info(self, msg, **kw):
        self._emit(logging.INFO, msg, **kw)

    def warning(self, msg, **kw):
        self._emit(logging.WARNING, msg, **kw)

    warn = warning

    def error(self, msg, **kw):
        self._emit(logging.ERROR, msg, **kw)


class _Time:
    def __init__(self, seconds):
        self._seconds = seconds

    def to_msg(self):
        return REGISTRY["builtin_interfaces/Time"](sec=int(self._seconds), nanosec=int(self._seconds % 1 * 1e9))

    @property
    def nanoseconds(self):
        return int(self._seconds * 1e9)


class _Clock:
    def now(self):
        return _Time(time.time())


class Node:
    def __init__(self, node_name, namespace=None, **_):
        self._name = node_name
        self._namespace = (namespace or BUS.namespace).rstrip("/")
        self._params = dict(BUS.params)
        self.alive = True

    # names
    def resolve_topic_name(self, name):
        return name if name.startswith("/") else "%s/%s" % (self._namespace, name)

    resolve_service_name = resolve_topic_name

    def get_name(self):
        return self._name

    def get_namespace(self):
        return self._namespace or "/"

    # parameters / utilities
    def declare_parameter(self, name, value=None, *_, **__):
        self._params.setdefault(name, value)
        return _Parameter(self._params[name])

    def get_parameter(self, name):
        return _Parameter(self._params[name])

    def get_logger(self):
        return _Logger("%s%s" % (self._namespace, "/" + self._name))

    def get_clock(self):
        return _Clock()

    # entities
    def create_publisher(self, msg_type, topic, qos, **_):
        return Publisher(self, msg_type, self.resolve_topic_name(topic))

    def create_subscription(self, msg_type, topic, callback, qos, **_):
        subscription = Subscription(self, msg_type, self.resolve_topic_name(topic), callback)
        with BUS.lock:
            BUS.subscriptions.setdefault(subscription.topic, []).append(subscription)
        return subscription

    def destroy_subscription(self, subscription):
        subscription.alive = False

    def destroy_publisher(self, publisher):
        publisher.alive = False

    def create_service(self, srv_type, name, callback, **_):
        service = Service(self, srv_type, self.resolve_topic_name(name), callback)
        with BUS.lock:
            existing = BUS.services.get(service.name)
            assert existing is None or not (existing.alive and existing.node.alive), \
                "two live servers for %s (CONVENTIONS 3.2)" % service.name
            BUS.services[service.name] = service
        return service

    def create_client(self, srv_type, name, **_):
        return Client(self, srv_type, self.resolve_topic_name(name))

    def create_timer(self, period, callback, **_):
        timer = Timer(self, period, callback)
        with BUS.lock:
            BUS.timers.append(timer)
        return timer

    def destroy_node(self):
        self.alive = False


# ------------------------------------------------------------------ rclpy

class _Enum:
    def __init__(self, *names):
        for name in names:
            setattr(self, name, name)


class QoSProfile:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _CallbackGroup:
    pass


class MultiThreadedExecutor:
    def __init__(self, *_, **__):
        self.nodes = []

    def add_node(self, node):
        self.nodes.append(node)

    def spin(self):
        raise RuntimeError("fake executor does not spin; drive timers with BUS.run_timers")


class CvBridge:
    pass


def install():
    try:
        import rclpy  # noqa: F401
        return False
    except ImportError:
        pass
    _build_interfaces()
    _module("rclpy", init=lambda *a, **k: None, shutdown=lambda *a, **k: None, ok=lambda: True)
    _module("rclpy.node", Node=Node)
    _module("rclpy.qos", QoSProfile=QoSProfile, qos_profile_sensor_data=QoSProfile(depth=5),
            ReliabilityPolicy=_Enum("RELIABLE", "BEST_EFFORT"), HistoryPolicy=_Enum("KEEP_LAST", "KEEP_ALL"),
            DurabilityPolicy=_Enum("VOLATILE", "TRANSIENT_LOCAL"))
    _module("rclpy.callback_groups", MutuallyExclusiveCallbackGroup=_CallbackGroup,
            ReentrantCallbackGroup=_CallbackGroup)
    _module("rclpy.executors", MultiThreadedExecutor=MultiThreadedExecutor)
    _module("cv_bridge", CvBridge=CvBridge)
    return True
