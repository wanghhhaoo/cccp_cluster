#!/usr/bin/env python3
"""Cluster health as every gateway sees it (deploy/README.md). Standard library only.

usage: smoke_check.py [--expect N] [--max-loss R] [--task JSON] host[:port] ...

Asks each node's UE HTTP API (/api/ue/health, /api/ue/nodes) and checks that:
  * every gateway answers and sees the expected number of nodes online;
  * all of them follow the same leader, exactly one node claims leadership, all at one term;
  * every online node's executor is alive (inference not ready only warns);
  * no measured link loses more than --max-loss (warning: links are measured over 30 beats).
With --task, also creates that task through the first gateway, waits for Running and destroys it.
Exit status 0 when every check passes, 1 otherwise.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PREFIX = "/api/ue"


def call(gateway, method, path, body=None, timeout=20.0):
    url = "http://%s%s%s" % (gateway, PREFIX, path)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:        # 4xx/5xx still carry the API's JSON
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def call_retrying(gateway, method, path, body=None, for_s=15.0):
    """Retryable errors (NO_LEADER, NOT_LEADER, BUSY, NOT_READY, TIMEOUT) are HTTP 503 (CONVENTIONS 7.2)."""
    deadline = time.monotonic() + for_s
    while True:
        status, reply = call(gateway, method, path, body)
        if status != 503 or time.monotonic() >= deadline:
            return status, reply
        time.sleep(1.0)


class Report:
    def __init__(self):
        self.failures, self.warnings = [], []

    def fail(self, text):
        self.failures.append(text)
        print("FAIL  " + text)

    def warn(self, text):
        self.warnings.append(text)
        print("WARN  " + text)

    @staticmethod
    def ok(text):
        print("ok    " + text)


def check_cluster(gateways, expect, max_loss, report):
    views = {}
    for gateway in gateways:
        try:
            _, health = call(gateway, "GET", "/health", timeout=5.0)
            _, nodes = call(gateway, "GET", "/nodes", timeout=5.0)
        except (OSError, ValueError) as error:
            report.fail("%s: no answer (%s)" % (gateway, error))
            continue
        views[gateway] = (health, nodes.get("nodes", []))
    if not views:
        return
    leaders = {}
    for gateway, (health, nodes) in views.items():
        online = [n for n in nodes if n["status"] == "Online"]
        offline = sorted(n["name"] for n in nodes if n["status"] != "Online")
        line = "%s (%s): %d online%s, leader %s" % (gateway, health.get("node", "?"), len(online),
                                                   ", offline: " + " ".join(offline) if offline else "",
                                                   health.get("leader") or "none")
        if len(online) != expect:
            report.fail(line + "; expected %d online" % expect)
        else:
            report.ok(line)
        leaders.setdefault(health.get("leader") or "", []).append(gateway)
    if len(leaders) != 1 or "" in leaders:
        report.fail("gateways disagree on the leader: %s" % {k or "none": v for k, v in leaders.items()})
    else:
        report.ok("every gateway follows %s" % next(iter(leaders)))

    # Node details from the first gateway that answered: what each node says in its heartbeat.
    gateway, (_, nodes) = next(iter(views.items()))
    online = [n for n in nodes if n["status"] == "Online"]
    claiming = sorted(n["name"] for n in online if n["leader"])
    terms = sorted({n["term"] for n in online})
    if len(claiming) != 1:
        report.fail("nodes claiming leadership (seen from %s): %s" % (gateway, claiming or "none"))
    elif len(terms) != 1:
        report.fail("online nodes are at different terms: %s" % terms)
    else:
        report.ok("one leader %s at term %d" % (claiming[0], terms[0]))
    dead = sorted(n["name"] for n in online if not n["executor_alive"])
    if dead:
        report.fail("executor not alive on: %s" % " ".join(dead))
    else:
        report.ok("executors alive on all %d online nodes" % len(online))
    not_ready = sorted(n["name"] for n in online if not n["inference_ready"])
    if not_ready:
        report.warn("inference not ready on: %s" % " ".join(not_ready))
    lossy, measured = [], 0
    for node in online:
        for link in node.get("links", []):
            if link["loss_rate"] >= 0:
                measured += 1
                if link["loss_rate"] > max_loss:
                    lossy.append("%s<-%s %.0f%%" % (node["name"], link["peer"], 100 * link["loss_rate"]))
    if lossy:
        report.warn("links losing more than %.0f%%: %s" % (100 * max_loss, ", ".join(sorted(lossy))))
    else:
        report.ok("%d measured links, none losing more than %.0f%%" % (measured, 100 * max_loss))


def check_task(gateway, body, report, timeout=30.0):
    body = dict(body)
    body.setdefault("idempotency_key", "smoke-%d" % int(time.time()))
    status, created = call_retrying(gateway, "POST", "/tasks", body)
    if not created.get("success"):
        report.fail("create via %s: HTTP %d %s %s" % (gateway, status, created.get("error_code"),
                                                      created.get("message")))
        return
    task_id = created["task_id"]
    report.ok("created %s on %s" % (task_id, created.get("assigned_node") or created.get("dispatched_nodes")))
    deadline, state = time.monotonic() + timeout, {}
    while time.monotonic() < deadline:
        _, state = call(gateway, "GET", "/tasks/" + task_id)
        if state.get("status") in ("Running", "Finished", "Failed", "Cancelled"):
            break
        time.sleep(1.0)
    if state.get("status") in ("Running", "Finished"):
        report.ok("task %s %s on %s" % (task_id, state["status"], state.get("node")))
    else:
        report.fail("task %s is %s after %.0f s: %s" % (task_id, state.get("status"), timeout,
                                                        state.get("error_message") or state.get("message")))
    status, destroyed = call_retrying(gateway, "DELETE", "/tasks/" + task_id)
    if destroyed.get("success"):
        report.ok("destroyed %s (%s)" % (task_id, destroyed.get("final_status")))
    else:
        report.fail("destroy %s: HTTP %d %s" % (task_id, status, destroyed.get("error_code")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("gateways", nargs="+", help="host or host:port of each node's UE HTTP API")
    parser.add_argument("--expect", type=int, help="nodes that should be online (default: one per gateway)")
    parser.add_argument("--max-loss", type=float, default=0.05, help="link loss rate that warns (default 0.05)")
    parser.add_argument("--task", help="JSON body of a task to create, wait for and destroy")
    parser.add_argument("--port", type=int, default=18090, help="port for gateways given without one")
    args = parser.parse_args(argv)
    gateways = [g if ":" in g else "%s:%d" % (g, args.port) for g in args.gateways]
    report = Report()
    check_cluster(gateways, args.expect or len(gateways), args.max_loss, report)
    if args.task:
        check_task(gateways[0], json.loads(args.task), report)
    print("\n%d failed, %d warnings" % (len(report.failures), len(report.warnings)))
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
