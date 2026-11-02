"""Execution generation (epoch, assign_term): tells execution sessions of one task apart (DESIGN 3.8).

epoch grows by one each time the task is (re)scheduled; assign_term is the term of
the leader that did it. Comparison is lexicographic, which a tuple gives for free:
two leaders that coexist briefly and both reschedule a task produce (2, 4) and
(2, 5) instead of two sessions both called epoch 2.
"""
from __future__ import annotations

from typing import NamedTuple


class Generation(NamedTuple):
    epoch: int = 0
    assign_term: int = 0

    def next(self, term: int) -> Generation:
        """The generation a (re)schedule by the leader of term moves to."""
        return Generation(self.epoch + 1, term)

    def __str__(self) -> str:
        return "(%d,%d)" % self     # no space: log lines are key=value (CONVENTIONS 7.3)

    # A NamedTuple's repr is "Generation(epoch=1, assign_term=4)": too long for every log line.
    __repr__ = __str__


NONE = Generation()
