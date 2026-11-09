"""Run batch tasks. Request data never names an executable: the algorithm's adapter
(algorithms/*.py, BatchAlgorithm.launch) builds the command from node configuration.

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

STOP_GRACE_S = 5.0


@dataclass
class Job:
    process: subprocess.Popen
    output: Path
    container: str = ""              # Docker container the job runs in (Launch.container)
    kill_at: float = None            # stop requested: SIGKILL after this time.monotonic()
    cleanup: subprocess.Popen = None  # `docker rm -f` started after a SIGKILL
    exited: bool = False             # the executor has handled the exit


def start(task, node_id, algorithm):
    """algorithm: the task's BatchAlgorithm. Raises FileExistsError when this generation already ran
    here (an earlier session was lost)."""
    root = Path(os.getenv("CCCP_JOB_STORE", str(Path.home() / "cccp_jobs"))).resolve()
    run = root / task.task_id / f"{task.epoch}-{task.assign_term}"
    role = "primary" if task.role == c.ROLE_PRIMARY else f"id_{task.helper_ids.index(node_id)}"
    output = run / role
    output.mkdir(parents=True, exist_ok=False)
    launch = algorithm.launch(task, run, role, output)
    with (output / "run.log").open("wb") as log:
        # A crashed executor must not leave a model or ROS bag consuming resources.
        process = subprocess.Popen(["setpriv", "--pdeathsig", "TERM", *launch.cmd], stdout=log, env=launch.env,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    return Job(process, output, launch.container)


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
