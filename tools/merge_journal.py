#!/usr/bin/env python3
"""Merge the event journals of several boards into one timeline (control/journal.py). Standard library only.

usage: merge_journal.py [--task ID] [--node NAME] [--event A,B] [--snapshots] [--json] [--utc] [--skew S] path ...

Each path is a journal file (<node>.jsonl, <node>.N.jsonl) or a directory searched for them,
e.g. one directory per board collected with `scp board:/var/lib/cccp/journal/* edge-03/`.
Entries are sorted by wall clock, so the timeline is only as good as the boards' clock sync.
That is checked: every node journals each change of leader, and all nodes see a change
within a second or two (DESIGN 5.5), so a node whose leader_changed entries sit far from
the others' has its clock off; it is reported with the estimated offset.

Filters: --task matches a task id prefix; --node keeps entries written by that node or
about it (peer, primary, nodes, by); --event keeps the named events. Snapshots are left out
unless --snapshots. --json prints the merged entries as JSON lines instead of text.
"""
import argparse
import datetime
import glob
import json
import os
import statistics
import sys
from collections import defaultdict

# Printed in the line's header, not among the fields.
HEADER = ("t", "node", "event", "term", "leader", "seq", "inc", "mono")


def journal_files(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            files.extend(sorted(glob.glob(os.path.join(path, "**", "*.jsonl"), recursive=True)))
        else:
            files.append(path)
    return files


def load(files, errors=sys.stderr):
    entries, bad = [], 0
    for name in files:
        with open(name, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    float(entry["t"])
                    entry["node"], entry["event"]
                except (ValueError, KeyError, TypeError):
                    bad += 1          # a line cut short by a power loss, most likely
                    continue
                entries.append(entry)
    if bad:
        print("skipped %d unreadable lines" % bad, file=errors)
    # Rotated files of a node overlap nothing, but two copies of one file may be given: drop repeats.
    unique = {(e["node"], e.get("inc"), e.get("seq"), e["t"]): e for e in entries}
    return sorted(unique.values(), key=lambda e: (e["t"], e["node"], e.get("seq", 0)))


def clock_offsets(entries, min_nodes=3):
    """Estimated wall clock offset of each node from the others, from the times each one
    journaled the same leader change: {node: (median offset s, number of changes compared)}.
    Needs changes seen by at least three nodes: with two, the median is their midpoint and
    both look half off (main() reports the gap of a pair instead)."""
    seen = defaultdict(dict)                    # (leader, term) -> node -> first time it followed it
    for entry in entries:
        if entry["event"] == "leader_changed" and entry.get("leader"):
            seen[(entry["leader"], entry.get("term"))].setdefault(entry["node"], entry["t"])
    offsets = defaultdict(list)
    for times in seen.values():
        if len(times) < min_nodes:
            continue
        middle = statistics.median(times.values())
        for node, t in times.items():
            offsets[node].append(t - middle)
    return {node: (statistics.median(values), len(values)) for node, values in offsets.items()}


def about(entry, node):
    if entry["node"] == node:
        return True
    for key in ("peer", "primary", "by", "reported_by", "prev_leader"):
        if entry.get(key) == node:
            return True
    return node in (entry.get("nodes") or ()) or node in (entry.get("helpers") or ())


def keep(entry, args):
    if entry["event"] == "snapshot" and not args.snapshots and not (args.event and "snapshot" in args.event):
        return False
    if args.event and entry["event"] not in args.event:
        return False
    if args.task and not str(entry.get("task", "")).startswith(args.task):
        return False
    if args.node and not about(entry, args.node):
        return False
    return True


def value_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value_text(v) for v in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    if text == "":
        return '""'
    return json.dumps(text, ensure_ascii=False) if any(ch.isspace() for ch in text) else text


def line_text(entry, utc=False):
    moment = datetime.datetime.fromtimestamp(entry["t"], datetime.timezone.utc if utc else None)
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (moment.microsecond // 1000)
    follows = "t%s/%s" % (entry.get("term", "?"), entry.get("leader") or "-")
    fields = " ".join("%s=%s" % (key, value_text(value)) for key, value in entry.items() if key not in HEADER)
    return "%s  %-10s %-14s %-18s %s" % (stamp, entry["node"], follows, entry["event"], fields)


def main(argv=None, out=sys.stdout, errors=sys.stderr):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", help="journal files or directories holding them")
    parser.add_argument("--task", help="task id or its prefix")
    parser.add_argument("--node", help="entries written by or about this node")
    parser.add_argument("--event", type=lambda text: set(text.split(",")), help="comma-separated event names")
    parser.add_argument("--snapshots", action="store_true", help="include the periodic snapshots")
    parser.add_argument("--json", action="store_true", help="print JSON lines")
    parser.add_argument("--utc", action="store_true", help="print times in UTC instead of local time")
    parser.add_argument("--skew", type=float, default=3.0, help="clock offset that is reported (default 3 s)")
    args = parser.parse_args(argv)
    files = journal_files(args.paths)
    if not files:
        print("no journal files under %s" % " ".join(args.paths), file=errors)
        return 1
    entries = load(files, errors)
    nodes = sorted({e["node"] for e in entries})
    print("%d entries from %d files, nodes: %s" % (len(entries), len(files), " ".join(nodes)), file=errors)
    offsets = clock_offsets(entries)
    for node, (offset, count) in sorted(offsets.items()):
        if abs(offset) > args.skew:
            print("clock check: %s is %+.1f s off the others (from %d leader change%s); its entries are out of "
                  "place by that much" % (node, offset, count, "" if count == 1 else "s"), file=errors)
    if len(nodes) > 1 and not offsets:
        pairs = clock_offsets(entries, min_nodes=2)
        if not pairs:
            print("clock check: no leader change journaled by two nodes; clock offsets unknown", file=errors)
        elif len(pairs) == 2 and max(abs(offset) for offset, _ in pairs.values()) * 2 > args.skew:
            (a, (offset, count)), (b, _) = sorted(pairs.items())
            print("clock check: %s and %s are %.1f s apart (from %d leader change%s); with two nodes it cannot "
                  "tell which clock is off" % (a, b, 2 * abs(offset), count, "" if count == 1 else "s"), file=errors)
    for entry in entries:
        if keep(entry, args):
            if args.json:
                print(json.dumps(entry, ensure_ascii=False, separators=(",", ":")), file=out)
            else:
                print(line_text(entry, args.utc), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
