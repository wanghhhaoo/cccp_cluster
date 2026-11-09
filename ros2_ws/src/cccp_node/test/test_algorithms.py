"""The data plane's algorithm adapters (cccp_task_executor/algorithms) against the control plane's
task type registry (core/task_types.py), and that only the adapters know type names (CONVENTIONS 6.6)."""
import pytest

import fake_ros
from test_ros_integration import INSTALLED
from test_task_types import _TYPE_REFERENCE

from cccp_node.core import task_types

if INSTALLED:
    from cccp_task_executor import algorithms
    from cccp_task_executor.algorithms import BatchAlgorithm, StreamAlgorithm
    from cccp_task_executor.inference import LocalBackend

pytestmark = pytest.mark.skipif(not INSTALLED, reason="real ROS present: run the colcon tests instead")

EXECUTOR = fake_ros.SRC / "cccp_task_executor" / "cccp_task_executor"


def test_every_registered_type_has_one_adapter_of_its_kind():
    assert tuple(algorithms.CLASSES) == task_types.ALGORITHMS
    for name, cls in algorithms.CLASSES.items():
        assert cls.name == name
        kind = task_types.BY_NAME[name].kind
        assert issubclass(cls, StreamAlgorithm if kind == task_types.STREAM else BatchAlgorithm), name
    assert tuple(algorithms.create(LocalBackend())) == task_types.ALGORITHMS


@pytest.mark.parametrize("name", [n for n in task_types.ALGORITHMS if task_types.BY_NAME[n].kind == task_types.STREAM])
def test_stream_adapters_declare_their_services_and_local_handlers(name):
    cls = algorithms.CLASSES[name]
    assert cls.services, "a stream algorithm needs at least one service"
    assert all(service.startswith("algorithms/") for service in {**cls.services, **cls.optional})

    class AnyEngine:
        def __getattr__(self, attribute):
            return lambda request, response: response
    assert set(cls.handlers(AnyEngine())) == set(cls.services) | set(cls.optional)


def test_executor_modules_outside_the_adapters_do_not_branch_on_type_names():
    offenders = []
    for path in sorted(EXECUTOR.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _TYPE_REFERENCE.search(line):
                offenders.append("%s:%d: %s" % (path.name, number, line.strip()))
    assert offenders == []


# ------------------------------------------------------------------ batch adapters: command and readiness

def _task(**kw):
    from types import SimpleNamespace
    values = dict(task_id="t-1", epoch=2, assign_term=3, params={})
    values.update(kw)
    return SimpleNamespace(**values)


def _executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_rssmap_command_carries_its_params(tmp_path, monkeypatch):
    from cccp_task_executor.algorithms.rssmap import RssMap
    monkeypatch.setenv("RSSMAP_PYTHON", "/opt/rss/python")
    monkeypatch.setenv("RSSMAP_DATA", "/data/newdata")
    out = tmp_path / "primary"
    launch = RssMap().launch(_task(params={"tx": "node_7", "score": True}), tmp_path, "primary", out)
    assert launch.cmd == ["/opt/rss/python", "-m", "propagation_model.demo", "--data", "/data/newdata",
                          "--out", str(out / "rss.npz"), "--tx", "node_7", "--score"]
    assert (launch.env, launch.container) == (None, "")
    assert "--score" not in RssMap().launch(_task(params={"score": False}), tmp_path, "primary", out).cmd


def test_heteromap_runs_bev_job_with_the_generation_folder_and_role(tmp_path, monkeypatch):
    from cccp_task_executor.algorithms.base import PACKAGE
    from cccp_task_executor.algorithms.heteromap import HeteroMap
    monkeypatch.setenv("HETEROMAP_PYTHON", "/opt/bev/python")
    launch = HeteroMap().launch(_task(), tmp_path, "id_3", tmp_path / "id_3")
    assert launch.cmd == ["/opt/bev/python", str(PACKAGE / "bev_job.py"), str(tmp_path), "id_3"]
    assert (PACKAGE / "bev_job.py").is_file()


def test_fastlivo_names_its_container_per_generation(tmp_path):
    from cccp_task_executor.algorithms.base import PACKAGE
    from cccp_task_executor.algorithms.fastlivo import FastLivo
    launch = FastLivo().launch(_task(), tmp_path, "primary", tmp_path / "primary")
    assert launch.cmd == ["bash", str(PACKAGE / "fastlivo.sh"), str(tmp_path / "primary")]
    assert launch.container == "cccp-fastlivo-t-1-2-3" == launch.env["CCCP_FASTLIVO_CONTAINER"]
    assert (PACKAGE / "fastlivo.sh").is_file()


def test_heteromap_is_ready_only_with_python_writable_job_store_and_all_weights(tmp_path, monkeypatch):
    from cccp_task_executor.algorithms.heteromap import HeteroMap
    root, store = tmp_path / "bev", tmp_path / "jobs"
    (root / "weights").mkdir(parents=True)
    store.mkdir()
    for name in ["collaboration_best.pth"] + ["id%d_bev_best.pth" % i for i in range(5)]:
        (root / "weights" / name).write_bytes(b"")
    monkeypatch.setenv("HETEROMAP_ROOT", str(root))
    monkeypatch.setenv("HETEROMAP_PYTHON", str(_executable(tmp_path / "python")))
    monkeypatch.setenv("CCCP_JOB_STORE", str(store))
    assert HeteroMap().ready()
    monkeypatch.setenv("CCCP_JOB_STORE", str(tmp_path / "missing"))
    assert not HeteroMap().ready()
    monkeypatch.setenv("CCCP_JOB_STORE", str(store))
    (root / "weights" / "id4_bev_best.pth").unlink()
    assert not HeteroMap().ready()


def test_rssmap_is_ready_only_with_python_and_every_data_file(tmp_path, monkeypatch):
    from cccp_task_executor.algorithms.rssmap import DATA_FILES, RssMap
    for name in DATA_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setenv("RSSMAP_DATA", str(tmp_path))
    monkeypatch.delenv("RSSMAP_PYTHON", raising=False)
    assert not RssMap().ready()
    monkeypatch.setenv("RSSMAP_PYTHON", str(_executable(tmp_path / "python")))
    assert RssMap().ready()
    (tmp_path / DATA_FILES[-1]).unlink()
    assert not RssMap().ready()


def test_fastlivo_is_ready_only_with_docker_and_the_bag(tmp_path, monkeypatch):
    from cccp_task_executor.algorithms.fastlivo import FastLivo
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    bag = tmp_path / "input.bag"
    bag.write_bytes(b"")
    monkeypatch.setenv("FASTLIVO_BAG", str(bag))
    assert not FastLivo().ready()                         # no docker
    _executable(bin_dir / "docker")
    assert FastLivo().ready()
    monkeypatch.setenv("FASTLIVO_BAG", str(tmp_path / "other.bag"))
    assert not FastLivo().ready()
