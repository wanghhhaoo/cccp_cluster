from types import SimpleNamespace

import numpy as np
import pytest

from cccp_task_executor.annotate import annotate
from cccp_task_executor.executor_node import parse_params, tracker_id


def test_tracker_sessions_are_isolated_per_epoch():
    assert tracker_id(7, 1) == "task-7-1" and tracker_id(7, 2) != tracker_id(7, 1)


def test_parse_params_normalizes_pkct_bbox():
    assert parse_params("PKCT", '{"bbox": [1, 2, 3, 4]}')["bbox"] == [1.0, 2.0, 3.0, 4.0]
    assert parse_params("PKCI", "") == {}
    for bad in ("{}", '{"bbox": [0, 0, -1, 5]}', "[1]"):
        with pytest.raises(ValueError):
            parse_params("PKCT", bad)


def test_annotate_draws_box_and_keeps_shape():
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    result = SimpleNamespace(task_type="PKCT", x=20.0, y=30.0, width=40.0, height=50.0,
                             confidence=0.9, class_name="", score=0.0)
    out = annotate(rgb, result)
    assert out.shape == rgb.shape and out.dtype == np.uint8
    assert out[30, 40].any()          # top edge of the box
    assert not rgb.any()              # input untouched


def test_annotate_classification_label():
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    result = SimpleNamespace(task_type="PKCI", class_name="car", score=0.8)
    assert annotate(rgb, result)[8, 8].any()
