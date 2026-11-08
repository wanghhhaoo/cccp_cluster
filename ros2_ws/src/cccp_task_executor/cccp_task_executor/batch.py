"""Launch node-configured algorithms. Request data never names an executable."""
import os
from pathlib import Path
import signal
import shutil
import subprocess

from cccp_node.core.constants import BATCH_TYPES as TYPES
HERE = Path(__file__).resolve().parent


def ready():
    enabled = []
    if (shutil.which("docker") and os.getenv("FASTLIVO_BAG")
            and Path(os.environ["FASTLIVO_BAG"]).is_file()):
        enabled.append("FASTLIVO")
    data = Path(os.getenv("RSSMAP_DATA", "/nonexistent"))
    if (os.access(os.getenv("RSSMAP_PYTHON", ""), os.X_OK)
            and all((data / f).is_file() for f in ("config/config.json", "weights/newdata.pt",
                                                   "field/gaussians.npz", "field/voxel_grid.npz"))):
        enabled.append("RSSMAP")
    root = Path(os.getenv("HETEROMAP_ROOT", "/nonexistent"))
    if (os.access(os.getenv("HETEROMAP_PYTHON", ""), os.X_OK)
            and os.access(os.getenv("CCCP_JOB_STORE", ""), os.W_OK)
            and (root / "weights/collaboration_best.pth").is_file()
            and all((root / f"weights/id{i}_bev_best.pth").is_file() for i in range(5))):
        enabled.append("HETEROMAP")
    return enabled


def start(task, node_id):
    root = Path(os.getenv("CCCP_JOB_STORE", str(Path.home() / "cccp_jobs"))).resolve()
    run = root / task.task_id / f"{task.epoch}-{task.assign_term}"
    role = "primary" if task.role == "primary" else f"id_{task.helper_ids.index(node_id)}"
    output = run / role
    output.mkdir(parents=True, exist_ok=False)
    if task.task_type == "HETEROMAP":
        cmd = [os.environ["HETEROMAP_PYTHON"], str(HERE / "bev_job.py"), str(run), role]
    elif task.task_type == "FASTLIVO":
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
        return subprocess.Popen(["setpriv", "--pdeathsig", "TERM", *cmd], stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True), output


def stop(job):
    proc, _ = job
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
