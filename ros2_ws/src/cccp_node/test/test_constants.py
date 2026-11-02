"""Constants mirrored outside the interface files must match them: core/constants.py for the
string constants, and ros/convert.py's tables for the uint8 enums of protocol 3."""
import re
from pathlib import Path

import pytest

from cccp_node.control import protocol as p
from cccp_node.core import constants
from cccp_node.ros import convert

SRC = Path(__file__).resolve().parents[2]

# interface file -> how its constant names map onto names in core/constants.py
SOURCES = {
    "cccp_task_interfaces/msg/TaskStatus.msg": lambda name: name,
    "cccp_task_interfaces/srv/CreateTask.srv": lambda name: name,
    "cccp_task_interfaces/msg/ErrorCodes.msg": lambda name: "CANCELLED_ERROR" if name == "CANCELLED" else name,
}


def string_constants(path):
    pattern = re.compile(r'^string\s+([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"\s*$')
    return [m.groups() for m in map(pattern.match, path.read_text().splitlines()) if m]


def int_constants(relative, prefix):
    pattern = re.compile(r"^u?int\d+\s+%s([A-Z0-9_]+)\s*=\s*(\d+)" % prefix)
    path = SRC / "cccp_cluster_interfaces" / "msg" / relative
    return {m.group(1): int(m.group(2)) for m in map(pattern.match, path.read_text().splitlines()) if m}


@pytest.mark.parametrize("relative", sorted(SOURCES))
def test_constants_match_interfaces(relative):
    declared = string_constants(SRC / relative)
    assert declared, "no string constants found in %s" % relative
    for name, value in declared:
        mirrored = SOURCES[relative](name)
        assert getattr(constants, mirrored, None) == value, "%s.%s" % (relative, name)


def by_name(table, rename=lambda key: key.upper()):
    """{"cancel": 1} -> {"CANCEL": 1}; "" (none) and missing entries are left out."""
    return {rename(key): value for key, value in table.items() if key}


@pytest.mark.parametrize("relative, prefix, mirrored", [
    ("Command.msg", "OP_", by_name(convert.OPS)),
    ("Command.msg", "STOP_", by_name(convert.STOP_KINDS)),
    ("ExecEvent.msg", "KIND_", by_name(convert.EVENT_KINDS)),
    ("ExecEvent.msg", "ERR_", dict(by_name(convert.EXEC_ERRORS), NONE=0)),
    ("Request.msg", "OP_", by_name(convert.REQUEST_OPS)),
    ("Request.msg", "ORIGIN_", by_name(convert.ORIGINS)),
    ("Reply.msg", "RES_", dict(by_name(convert.RESULTS), OK=0, TASK_FAILED=convert.RES_TASK_FAILED)),
    ("Reply.msg", "STATUS_", {s.upper(): i for i, s in enumerate(convert.STATUSES)}),
    ("TaskState.msg", "ROLE_", {r.upper(): i for i, r in enumerate(convert.ROLES)}),
    ("TaskBrief.msg", "ROLE_", {r.upper(): i for i, r in enumerate(convert.ROLES)}),
])
def test_enum_tables_match_interfaces(relative, prefix, mirrored):
    assert int_constants(relative, prefix) == mirrored


def test_protocol_version():
    assert int_constants("NodeStatus.msg", "")["PROTOCOL_VERSION"] == constants.PROTOCOL_VERSION


def test_event_kinds_cover_the_protocol():
    assert set(convert.EVENT_KINDS) == {p.RUNNING, p.REJECTED, p.FAILED, p.STOPPED, p.FINISHED}
