"""Consensus layer: delivers task table commands to every replica in the same order.

Business code depends only on the Consensus protocol in base.py (CONVENTIONS
6.2); pysyncobj_backend.py is the only module that imports PySyncObj.
"""
from .base import Consensus, Member  # noqa: F401
