"""The heartbeat must fit one link-layer frame (~1.4 KB, DESIGN 2.8): with best effort, a message
split into n frames is lost with probability 1 - (1 - p)^n.

Sizes are computed from the .msg files as DDS serializes them (CDR: 4-byte encapsulation header,
primitives aligned to their size, sequences and strings prefixed by a 4-byte length), with bounded
arrays filled as given, so the test needs no ROS and fails as soon as a field makes the message grow.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
SIZES = {"bool": 1, "byte": 1, "char": 1, "int8": 1, "uint8": 1, "int16": 2, "uint16": 2, "int32": 4,
         "uint32": 4, "float32": 4, "int64": 8, "uint64": 8, "float64": 8}
FRAME = 1400                 # bytes of payload in one link-layer frame, after RTPS/UDP/IP headers


def fields(name, pkg="cccp_cluster_interfaces"):
    text = (SRC / pkg / "msg" / ("%s.msg" % name)).read_text()
    result = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or re.match(r"^\S+\s+[A-Z][A-Z0-9_]*\s*=", line):     # blank, or a constant
            continue
        typ, field = line.split()[:2]
        shape = re.match(r"^(.+?)(?:\[(<=)?(\d*)\])?$", typ)
        result.append((field, shape.group(1), typ.endswith("]"), shape.group(2), shape.group(3)))
    return result


def align(offset, size):
    return (offset + size - 1) // size * size


def serialize(name, fill, offset=0):
    """End offset of message `name` written at offset. fill: field -> items in a bounded array, or a
    nested fill dict for a struct field; a bounded array not mentioned is filled to its bound."""
    for field, base, is_array, bounded, size in fields(name):
        if is_array:
            count = int(size) if size and not bounded else fill.get(field, int(size) if size else 0)
            if not size or bounded:
                offset = align(offset, 4) + 4          # sequence length
        else:
            count = None
        for _ in range(1 if count is None else count):
            if base in SIZES:
                offset = align(offset, SIZES[base]) + SIZES[base]
            elif base.startswith("string"):
                offset = align(offset, 4) + 4 + fill.get(field + ".length", 0) + 1
            else:
                nested = fill.get(field) if isinstance(fill.get(field), dict) else {}
                offset = serialize(base, nested, offset)
    return offset


def heartbeat(tasks, slow, peers=15):
    return 4 + serialize("NodeStatus", {"tasks": tasks, "load": 1 if slow else 0, "links": peers if slow else 0})


def test_a_full_heartbeat_fits_one_frame():
    full = heartbeat(tasks=8, slow=True)
    assert full < FRAME, "NodeStatus with 8 tasks and 15 links is %d bytes" % full


def test_typical_heartbeats_are_small():
    """DESIGN 3.2 estimates about 170 B for 4 tasks, and 290 B with the slow part (11 links)."""
    assert heartbeat(tasks=4, slow=False) <= 200
    assert heartbeat(tasks=4, slow=True, peers=11) <= 320


def test_the_calculation_counts_every_part():
    """Guards the guard: each task summary is at least its 25 bytes of fields, each link 7."""
    assert heartbeat(tasks=1, slow=False) - heartbeat(tasks=0, slow=False) >= 25
    assert heartbeat(tasks=0, slow=True, peers=1) - heartbeat(tasks=0, slow=True, peers=0) >= 7
