"""Idempotency key formats (CONVENTIONS 4.2).

A key must be derived from the *cause* of a submission, so that a retry of
the same cause produces the same key and therefore the same task.
"""
from __future__ import annotations


def manual(client_id: str, request_uuid: str) -> str:
    return "manual:%s:%s" % (client_id, request_uuid)


def schedule(rule_id: str, planned_fire_unix_ms: int) -> str:
    return "schedule:%s:%d" % (rule_id, planned_fire_unix_ms)


def chain(parent_task_id: int, rule: str, frame_id: int | None = None) -> str:
    key = "chain:%d:%s" % (parent_task_id, rule)
    return key if frame_id is None else "%s:%d" % (key, frame_id)


def event(node_name: str, source: str, event_seq: int) -> str:
    return "event:%s:%s:%d" % (node_name, source, event_seq)


def anonymous(request_uuid: str) -> str:
    return "anon:%s" % request_uuid
