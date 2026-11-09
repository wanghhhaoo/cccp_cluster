"""What an algorithm adapter provides to the executor (CONVENTIONS 6.6).

core/task_types.py says what the control plane needs to know about a task type. Its adapter
here says how the data plane runs it. There is one module per type, and algorithms/__init__.py
lists them.

A STREAM algorithm processes the frames of an input stream through services of the
inference backend (inference.py), which are either engines called in this process or ROS
services. Per task, the executor hands it one frame at a time and skips frames that arrive
while one is in flight. The executor publishes what the algorithm returns, counts failures
and ends the task.

A BATCH algorithm names the process that runs one task. batch.py starts, stops and reaps
that process.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from cccp_node.core import task_types

PACKAGE = Path(__file__).resolve().parents[1]     # where helper scripts (fastlivo.sh, bev_job.py) live


@dataclass
class Output:
    """One frame's result: the executor puts it into a TaskResult."""
    fields: dict = field(default_factory=dict)    # this algorithm's TaskResult fields
    message: str = "ok"
    latency_ms: float = 0.0
    payload: Any = b""                            # collaboration payload, passed on unmodified
    summary: str = ""                             # logged with the task's first result; "" skips this one


@dataclass
class Step:
    """One backend call for one frame."""
    future: Any                                   # add_done_callback(fn(future)) and result()
    finish: Callable[[Any], Output]               # a successful service reply -> Output
    fatal: str = ""                               # non-empty: a failure ends the task at once, prefixed by this


@dataclass
class Launch:
    """How to run one batch task. The executor redirects output to run.log in the output folder."""
    cmd: list
    env: Optional[dict] = None                    # None: inherit the executor's environment
    container: str = ""                           # Docker container to remove by name after a SIGKILL


class Algorithm:
    name = ""                                     # a task_types.REGISTRY name

    def __init__(self, backend=None):
        self.backend = backend

    def ready(self) -> bool:
        """Whether this node can run the algorithm now; it decides the algorithms_ready bit."""
        raise NotImplementedError

    def wait_ready(self, timeout_s: float) -> str:
        """'' once the algorithm can run (waiting up to timeout_s), else why not."""
        raise NotImplementedError

    def normalize_params(self, params: dict) -> dict:
        """params passed the type's check (core/task_types.py); convert them for the algorithm."""
        return params


class StreamAlgorithm(Algorithm):
    services: dict = {}       # service name -> srv type, all needed to run
    optional: dict = {}       # used when present, e.g. to release a session

    def __init__(self, backend):
        super().__init__(backend)
        for service, srv_type in {**self.services, **self.optional}.items():
            backend.declare(service, srv_type)

    @staticmethod
    def handlers(engine) -> dict:
        """service name -> engine method(request, response), for LocalBackend."""
        raise NotImplementedError

    @staticmethod
    def serve():
        """The ROS node hosting the engine in the data_plane process; it has an `engine` attribute."""
        raise NotImplementedError

    def ready(self) -> bool:
        return all(self.backend.ready(service) for service in self.services)

    def wait_ready(self, timeout_s: float) -> str:
        for service in self.services:
            reason = self.backend.wait_ready(service, timeout_s)
            if reason:
                return reason
        return ""

    def open(self, task) -> None:
        """A session starts; per-session state goes into task.state."""

    def step(self, task, image) -> Step:
        """Process one frame. Count work units in task.frames (the result's frame_id)."""
        raise NotImplementedError

    def close(self, task) -> None:
        """The session ended (stopped, failed or replaced): release what open() and step() took."""

    def annotate(self, rgb, result):
        """Draw a TaskResult onto the input frame (HxWx3 uint8); returns a new array."""
        return rgb


class BatchAlgorithm(Algorithm):
    def wait_ready(self, timeout_s: float) -> str:
        return "" if self.ready() else "batch runtime unavailable"

    def launch(self, task, run: Path, role: str, output: Path) -> Launch:
        """run: this generation's folder in CCCP_JOB_STORE; role: "primary" or id_<i>; output: run/role."""
        raise NotImplementedError


def parse_params(algorithm: Algorithm, params_json: str) -> dict:
    """Decode and check StartTask params like the gateway did, then normalize them for the algorithm."""
    params = json.loads(params_json) if params_json.strip() else {}
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON object")
    task_types.BY_NAME[algorithm.name].check_params(params)
    return algorithm.normalize_params(params)
