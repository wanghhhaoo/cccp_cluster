"""Algorithm adapters, one per task type in core/task_types.py (CONVENTIONS 6.6).

To add a type: register it in core/task_types.py, write its adapter module here, and list
the class below. test_algorithms.py checks that both lists agree.
"""
from cccp_node.core import task_types

from .base import BatchAlgorithm, StreamAlgorithm
from .fastlivo import FastLivo
from .heteromap import HeteroMap
from .pkci import Pkci
from .pkct import Pkct
from .rssmap import RssMap

CLASSES = {cls.name: cls for cls in (Pkci, Pkct, FastLivo, HeteroMap, RssMap)}
STREAM = tuple(cls for cls in CLASSES.values() if issubclass(cls, StreamAlgorithm))


def create(backend):
    """name -> adapter, in task_types.ALGORITHMS order; stream adapters reach their engines through backend."""
    return {name: CLASSES[name](backend) for name in task_types.ALGORITHMS}


def local_handlers(**engines):
    """LocalBackend handlers for in-process engines, e.g. local_handlers(PKCI=engine)."""
    handlers = {}
    for name, engine in engines.items():
        handlers.update(CLASSES[name].handlers(engine))
    return handlers


__all__ = ["BatchAlgorithm", "CLASSES", "STREAM", "StreamAlgorithm", "create", "local_handlers"]
