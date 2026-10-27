"""core/constants.py must mirror the constants declared in the interface files."""
import re
from pathlib import Path

import pytest

from cccp_node.core import constants

SRC = Path(__file__).resolve().parents[2]

# interface file -> how its constant names map onto names in core/constants.py
SOURCES = {
    "cccp_task_interfaces/msg/TaskStatus.msg": lambda name: name,
    "cccp_task_interfaces/msg/TaskReport.msg": lambda name: name.replace("STATE_", "REPORT_"),
    "cccp_task_interfaces/srv/CreateTask.srv": lambda name: name,
    "cccp_task_interfaces/srv/StartTask.srv": lambda name: name,
    "cccp_cluster_interfaces/msg/RunningTask.msg": lambda name: name,
    "cccp_task_interfaces/msg/ErrorCodes.msg": lambda name: "CANCELLED_ERROR" if name == "CANCELLED" else name,
}


def string_constants(path):
    pattern = re.compile(r'^string\s+([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"\s*$')
    return [m.groups() for m in map(pattern.match, path.read_text().splitlines()) if m]


@pytest.mark.parametrize("relative", sorted(SOURCES))
def test_constants_match_interfaces(relative):
    declared = string_constants(SRC / relative)
    assert declared, "no string constants found in %s" % relative
    for name, value in declared:
        mirrored = SOURCES[relative](name)
        assert getattr(constants, mirrored, None) == value, "%s.%s" % (relative, name)
