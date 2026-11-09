"""The task type registry (core/task_types.py): wire order, consistency, and that it is the only place
that knows type names (CONVENTIONS 6.5)."""
import re
from pathlib import Path

import pytest

from cccp_node.core import constants as c
from cccp_node.core import task_types as types

PACKAGE = Path(__file__).resolve().parents[1] / "cccp_node"


def test_bit_order_is_pinned():
    """Bit i of algorithms_ready is ALGORITHMS[i] on every node: append only, never reorder or reuse."""
    assert types.ALGORITHMS == ("PKCI", "PKCT", "FASTLIVO", "HETEROMAP", "RSSMAP")


def test_names_are_unique_upper_case_and_fit_the_uint8_mask():
    assert len(set(types.ALGORITHMS)) == len(types.ALGORITHMS) <= 8
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in types.ALGORITHMS)
    assert types.BY_NAME.keys() == set(types.ALGORITHMS)
    assert types.BATCH_TYPES == ("FASTLIVO", "HETEROMAP", "RSSMAP")


@pytest.mark.parametrize("entry", types.REGISTRY, ids=lambda t: t.name)
def test_entries_are_consistent(entry):
    assert entry.kind in types.KINDS
    assert entry.modes and entry.modes <= c.MODES
    if entry.participants:
        # A fixed, ordered set of nodes is a collaboration: at least one helper, and never local mode.
        assert entry.participants >= 2 and c.MODE_LOCAL not in entry.modes
    if entry.helpers_required:
        assert entry.modes != {c.MODE_LOCAL}
    # Only batch jobs leave files in CCCP_JOB_STORE, and every batch job does.
    assert bool(entry.artifacts) == (entry.kind == types.BATCH)
    assert all(re.fullmatch(r"\.[a-z0-9]+", suffix) for suffix in entry.artifacts)
    assert callable(entry.check_params)


def test_artifact_roles_and_suffixes_cover_every_type():
    assert types.ARTIFACT_ROLES == ("primary", "id_0", "id_1", "id_2", "id_3", "id_4")
    assert types.ARTIFACT_SUFFIXES == (".npz", ".pcd")


def test_param_checks_raise_value_error():
    with pytest.raises(ValueError, match="bbox"):
        types.BY_NAME["PKCT"].check_params({})
    with pytest.raises(ValueError, match="invalid parameters for RSSMAP"):
        types.BY_NAME["RSSMAP"].check_params({"tx": 7})
    types.BY_NAME["RSSMAP"].check_params({"tx": "node_7", "score": False})
    types.BY_NAME["PKCI"].check_params({"anything": 1})


def test_unknown_type_is_not_found():
    assert types.find("YOLO") is None and types.find("PKCI") is types.REGISTRY[0]


# Type names written as code: c.PKCI, "PKCI", 'HETEROMAP'. Bare words in comments and docstrings are fine.
_NAMES = "|".join(types.ALGORITHMS)
_TYPE_REFERENCE = re.compile(r"""\bc\.(?:%s)\b|["'](?:%s)["']""" % (_NAMES, _NAMES))


def test_control_plane_logic_does_not_branch_on_type_names():
    """Per-type behaviour belongs in the registry; core/ and control/ only read TaskType fields."""
    offenders = []
    for folder in ("core", "control"):
        for path in sorted((PACKAGE / folder).glob("*.py")):
            if path.name in ("task_types.py", "constants.py"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if _TYPE_REFERENCE.search(line):
                    offenders.append("%s:%d: %s" % (path.relative_to(PACKAGE), number, line.strip()))
    assert offenders == []


def test_the_scan_finds_type_references():
    """Guards the test above against a pattern that silently matches nothing."""
    assert _TYPE_REFERENCE.search('if spec.task_type == c.HETEROMAP:')
    assert _TYPE_REFERENCE.search('PARAM_CHECKS = {"PKCT": check}')
    assert not _TYPE_REFERENCE.search("# Every HETEROMAP frame needs all five helpers")
