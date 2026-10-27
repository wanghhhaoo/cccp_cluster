"""HTTP/JSON interface for UE, served by every node (ported from CCCP ue_api.py).

Paths, JSON fields and status codes are unchanged, so UE needs no changes
other than being able to try another node when one is unreachable. New
optional request fields: idempotency_key, mode, helper_count,
min_participants, input_node.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core.task_spec import TaskSpec

PREFIX = "/api/ue"
_TASK = re.compile(r"^%s/tasks/(\d+)(/destroy|/result_image)?$" % PREFIX)

# UE JSON field -> TaskSpec field type.
CREATE_FIELDS = {
    "name": str, "task_type": str, "input": str, "input_node": str, "request_node": str,
    "target_nodes": list, "mode": str, "helper_count": int, "min_participants": int,
    "use_gpu": bool, "cpu_compute_required": float, "cpu_memory_required": float,
    "gpu_memory_required": float, "latency_sensitivity": float, "drone_id": int, "level": str,
    "image_topic": str,
}
UINT8_FIELDS = ("drone_id", "helper_count", "min_participants")


def spec_from_body(body) -> tuple[TaskSpec, str]:
    """UE JSON -> (TaskSpec, idempotency_key). `params` (object) or `params_json` (string)."""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    values = {}
    for key, kind in CREATE_FIELDS.items():
        if body.get(key) is None:
            continue
        value = body[key]
        if kind is list:
            if not isinstance(value, list):
                raise ValueError("%s must be a list" % key)
            value = tuple(str(v) for v in value)
        elif kind is bool:
            value = bool(value)
        else:
            value = kind(value)
        if key in UINT8_FIELDS and not 0 <= value <= 255:
            raise ValueError("%s must be 0..255" % key)
        values[key] = value
    if body.get("params") is not None:
        values["params_json"] = json.dumps(body["params"])
    elif body.get("params_json") is not None:
        values["params_json"] = str(body["params_json"])
    values.setdefault("task_type", "")
    key = str(body.get("idempotency_key") or "")
    return TaskSpec(**values), key


class EventLog:
    """Bounded, sequence-numbered event list with long-poll waiting."""

    def __init__(self, capacity=2000):
        self._events = deque(maxlen=capacity)
        self._seq = 0
        self._cond = threading.Condition()

    def add(self, kind, data):
        with self._cond:
            self._seq += 1
            self._events.append(dict(data, seq=self._seq, kind=kind, time=time.time()))
            self._cond.notify_all()

    def since(self, after, wait_s, limit=200):
        deadline = time.monotonic() + max(0.0, wait_s)
        with self._cond:
            while True:
                found = [e for e in self._events if e["seq"] > after]
                remaining = deadline - time.monotonic()
                if found or remaining <= 0:
                    return found[:limit], self._seq
                self._cond.wait(remaining)


class UeApi:
    """Owns the HTTP server; `backend` provides create/destroy/query/list_nodes/latest_*/events."""

    def __init__(self, backend, host, port, logger):
        self.backend = backend
        self.logger = logger
        handler = type("Handler", (_Handler,), {"api": self})
        self.server = ThreadingHTTPServer((host, port), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


class _Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------------- plumbing
    def _send(self, code, body=None, content_type="application/json", raw=None):
        data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if raw is None else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(text) if text.strip() else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _guard(self, handler):
        try:
            handler()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"success": False, "message": str(exc)})
        except Exception as exc:  # never kill the server thread
            self.api.logger.error("UE API %s %s failed: %s" % (self.command, self.path, exc))
            self._send(500, {"success": False, "message": str(exc)})

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def do_DELETE(self):
        self._guard(self._delete)

    def _get(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        backend = self.api.backend
        if url.path == PREFIX + "/health":
            return self._send(200, {"success": True, "message": "ok", **backend.health()})
        if url.path == PREFIX + "/nodes":
            ok, data, message = backend.list_nodes()
            return self._send(200 if ok else 502, {"success": ok, "message": message, "nodes": data})
        if url.path == PREFIX + "/events":
            after = int(query.get("after", ["0"])[0])
            wait_s = min(float(query.get("wait", ["0"])[0]), 30.0)
            events, last = backend.events.since(after, wait_s)
            return self._send(200, {"success": True, "last_seq": last, "events": events})
        match = _TASK.match(url.path)
        if match and match.group(2) is None:
            result = backend.query(int(match.group(1)))
            result["latest_result"] = backend.latest_result(int(match.group(1)))
            return self._send(200 if result["success"] else 404, result)
        if match and match.group(2) == "/result_image":
            jpeg = backend.latest_image_jpeg(int(match.group(1)))
            if jpeg is None:
                return self._send(404, {"success": False, "message": "no result image yet"})
            return self._send(200, content_type="image/jpeg", raw=jpeg)
        self._send(404, {"success": False, "message": "unknown path"})

    def _post(self):
        path = urlsplit(self.path).path
        if path == PREFIX + "/tasks":
            result = self.api.backend.create(self._json_body())
            code = 200 if result["success"] else (400 if result["task_id"] == 0 else 502)
            return self._send(code, result)
        match = _TASK.match(path)
        if match and match.group(2) == "/destroy":
            body = self._json_body()
            return self._destroy(int(match.group(1)), str(body.get("reason") or ""))
        self._send(404, {"success": False, "message": "unknown path"})

    def _delete(self):
        match = _TASK.match(urlsplit(self.path).path)
        if match and match.group(2) is None:
            return self._destroy(int(match.group(1)), "")
        self._send(404, {"success": False, "message": "unknown path"})

    def _destroy(self, task_id, reason):
        result = self.api.backend.destroy(task_id, reason)
        code = 200 if result["success"] else (404 if result.get("error_code") == "NOT_FOUND" else 409)
        self._send(code, result)


def encode_jpeg(image_msg, quality=85):
    """sensor_msgs/Image (rgb8/bgr8) -> JPEG bytes."""
    import numpy as np
    from PIL import Image as PILImage
    channels = 3
    array = np.frombuffer(bytes(image_msg.data), dtype=np.uint8)
    array = array.reshape(image_msg.height, image_msg.step)[:, :image_msg.width * channels]
    array = array.reshape(image_msg.height, image_msg.width, channels)
    if image_msg.encoding == "bgr8":
        array = array[:, :, ::-1]
    elif image_msg.encoding != "rgb8":
        raise ValueError("unsupported encoding %s" % image_msg.encoding)
    out = io.BytesIO()
    PILImage.fromarray(np.ascontiguousarray(array)).save(out, format="JPEG", quality=quality)
    return out.getvalue()
