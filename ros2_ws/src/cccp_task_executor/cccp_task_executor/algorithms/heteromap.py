"""HETEROMAP: collaborative BEV map; the fusion node and helpers id_0..id_4 each run bev_job.py.

They exchange features through the shared CCCP_JOB_STORE (docs/推理任务集成.md).
"""
import os
from pathlib import Path

from cccp_node.core import constants as c

from .base import PACKAGE, BatchAlgorithm, Launch


class HeteroMap(BatchAlgorithm):
    name = c.HETEROMAP

    def ready(self):
        root = Path(os.getenv("HETEROMAP_ROOT", "/nonexistent"))
        return bool(os.access(os.getenv("HETEROMAP_PYTHON", ""), os.X_OK)
                    and os.access(os.getenv("CCCP_JOB_STORE", ""), os.W_OK)
                    and (root / "weights/collaboration_best.pth").is_file()
                    and all((root / f"weights/id{i}_bev_best.pth").is_file() for i in range(5)))

    def launch(self, task, run, role, output):
        return Launch([os.environ["HETEROMAP_PYTHON"], str(PACKAGE / "bev_job.py"), str(run), role])
