"""RSSMAP: radio propagation map from the node's data folder (params: tx, score)."""
import os
from pathlib import Path

from cccp_node.core import constants as c

from .base import BatchAlgorithm, Launch

DATA_FILES = ("config/config.json", "weights/newdata.pt", "field/gaussians.npz", "field/voxel_grid.npz")


class RssMap(BatchAlgorithm):
    name = c.RSSMAP

    def ready(self):
        data = Path(os.getenv("RSSMAP_DATA", "/nonexistent"))
        return os.access(os.getenv("RSSMAP_PYTHON", ""), os.X_OK) and all((data / f).is_file() for f in DATA_FILES)

    def launch(self, task, run, role, output):
        cmd = [os.environ["RSSMAP_PYTHON"], "-m", "propagation_model.demo",
               "--data", os.environ["RSSMAP_DATA"], "--out", str(output / "rss.npz")]
        if "tx" in task.params:
            cmd.extend(["--tx", str(task.params["tx"])])
        if task.params.get("score") is True:
            cmd.append("--score")
        return Launch(cmd)
