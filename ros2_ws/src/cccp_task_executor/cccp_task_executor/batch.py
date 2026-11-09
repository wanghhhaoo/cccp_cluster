"""Launch node-configured algorithms. Request data never names an executable.

Each batch task runs as its own process group. Stopping one never blocks the
executor thread: stop() sends SIGTERM and returns, and reap(), called from the
executor's poll timer, sends SIGKILL to groups still alive after STOP_GRACE_S.
A FASTLIVO container belongs to the Docker daemon, not to the process group,
so after a SIGKILL its container is removed by name.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time

from cccp_node.core import constants as c
from cccp_node.core.task_types import BATCH_TYPES as TYPES  # noqa: F401  (executor_node uses batch.TYPES)

HERE = Path(__file__).resolve().parent
STOP_GRACE_S = 5.0


@dataclass
class Job:
    process: subprocess.Popen
    output: Path
    container: str = ""              # FASTLIVO: the Docker container the script runs
    kill_at: float = None            # stop requested: SIGKILL after this time.monotonic()
    cleanup: subprocess.Popen = None  # `docker rm -f` started after a SIGKILL
    exited: bool = False             # the executor has handled the exit


def ready():
    enabled = []
    if (shutil.which("docker") and os.getenv("FASTLIVO_BAG")
            and Path(os.environ["FASTLIVO_BAG"]).is_file()):
        enabled.append(c.FASTLIVO)
    data = Path(os.getenv("RSSMAP_DATA", "/nonexistent"))
    if (os.access(os.getenv("RSSMAP_PYTHON", ""), os.X_OK)
            and all((data / f).is_file() for f in ("config/config.json", "weights/newdata.pt",
                                                   "field/gaussians.npz", "field/voxel_grid.npz"))):
        enabled.append(c.RSSMAP)
    root = Path(os.getenv("HETEROMAP_ROOT", "/nonexistent"))
    if (os.access(os.getenv("HETEROMAP_PYTHON", ""), os.X_OK)
            and os.access(os.getenv("CCCP_JOB_STORE", ""), os.W_OK)
            and (root / "weights/collaboration_best.pth").is_file()
            and all((root / f"weights/id{i}_bev_best.pth").is_file() for i in range(5))):
        enabled.append(c.HETEROMAP)
    return enabled


def start(task, node_id):
    """Raises FileExistsError when this generation already ran here (an earlier session was lost)."""
    root = Path(os.getenv("CCCP_JOB_STORE", str(Path.home() / "cccp_jobs"))).resolve()
    run = root / task.task_id / f"{task.epoch}-{task.assign_term}"
    role = "primary" if task.role == c.ROLE_PRIMARY else f"id_{task.helper_ids.index(node_id)}"
    output = run / role
    output.mkdir(parents=True, exist_ok=False)
    env, container = None, ""
    if task.task_type == c.HETEROMAP:
        cmd = [os.environ["HETEROMAP_PYTHON"], str(HERE / "bev_job.py"), str(run), role]
    elif task.task_type == c.FASTLIVO:
        container = f"cccp-fastlivo-{task.task_id}-{task.epoch}-{task.assign_term}"
        env = dict(os.environ, CCCP_FASTLIVO_CONTAINER=container)
        cmd = ["bash", str(HERE / "fastlivo.sh"), str(output)]
    else:
        cmd = [os.environ["RSSMAP_PYTHON"], "-m", "propagation_model.demo",
               "--data", os.environ["RSSMAP_DATA"], "--out", str(output / "rss.npz")]
        if "tx" in task.params:
            cmd.extend(["--tx", str(task.params["tx"])])
        if task.params.get("score") is True:
            cmd.append("--score")
    with (output / "run.log").open("wb") as log:
        # A crashed executor must not leave a model or ROS bag consuming resources.
        process = subprocess.Popen(["setpriv", "--pdeathsig", "TERM", *cmd], stdout=log, env=env,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    return Job(process, output, container)


def _signal(job, sig):
    try:
        os.killpg(job.process.pid, sig)
    except ProcessLookupError:
        pass


def _remove_container(job):
    if job.container and shutil.which("docker"):
        job.cleanup = subprocess.Popen(["docker", "rm", "-f", job.container], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)


def stop(job, now=None):
    """Asks the job to stop without waiting. Returns True while it may still be running:
    the caller then keeps it and passes it to reap() until reap() drops it."""
    if job.process.poll() is not None:
        return False
    if job.kill_at is None:
        _signal(job, signal.SIGTERM)
        job.kill_at = (time.monotonic() if now is None else now) + STOP_GRACE_S
    return True


def reap(jobs, now=None):
    """Sends SIGKILL to stopped jobs past their grace period. Returns the jobs not yet gone."""
    now = time.monotonic() if now is None else now
    alive = []
    for job in jobs:
        if job.process.poll() is None:
            if now >= job.kill_at and job.cleanup is None:
                _signal(job, signal.SIGKILL)
                _remove_container(job)
            alive.append(job)
        elif job.cleanup is not None and job.cleanup.poll() is None:
            alive.append(job)
    return alive


def shutdown(jobs, timeout=STOP_GRACE_S):
    """Executor shutdown: stop every job and wait for them (blocking is fine here)."""
    jobs = [job for job in jobs if stop(job)]
    deadline = time.monotonic() + timeout
    while jobs and time.monotonic() < deadline:
        jobs = [job for job in jobs if job.process.poll() is None]
        time.sleep(0.05)
    for job in reap(jobs, now=float("inf")):
        try:
            job.process.wait(timeout=1.0)
            if job.cleanup is not None:
                job.cleanup.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pass
