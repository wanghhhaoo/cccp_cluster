"""FASTLIVO: lidar mapping from the node's recorded bag, in a Docker container (fastlivo.sh)."""
import os
import shutil
from pathlib import Path

from cccp_node.core import constants as c

from .base import PACKAGE, BatchAlgorithm, Launch


class FastLivo(BatchAlgorithm):
    name = c.FASTLIVO

    def ready(self):
        return bool(shutil.which("docker") and os.getenv("FASTLIVO_BAG")
                    and Path(os.environ["FASTLIVO_BAG"]).is_file())

    def launch(self, task, run, role, output):
        # A container belongs to the Docker daemon, not to the process group: name it so that
        # batch.reap() can remove it after a SIGKILL.
        container = f"cccp-fastlivo-{task.task_id}-{task.epoch}-{task.assign_term}"
        return Launch(["bash", str(PACKAGE / "fastlivo.sh"), str(output)],
                      env=dict(os.environ, CCCP_FASTLIVO_CONTAINER=container), container=container)
