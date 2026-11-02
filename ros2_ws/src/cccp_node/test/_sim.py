"""In-process cluster simulation for controller and gateway tests.

Each SimNode stands in for one node's executor (with the real ExecutionGuard)
and its NodeStatus publisher. SimDispatcher routes StartTask/StopTask to the
target SimNode, so term and generation fencing behave as on real executors.
"""
from cccp_node.control.controller import DispatchResult, TaskReportView
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import ClusterView, NodeView, TaskStateView
from cccp_node.core.election import Leadership
from cccp_node.core.execution_guard import NOOP, REJECT, RESTART, START, STOP, ExecutionGuard


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeElection:
    """Election result set by the test."""

    def __init__(self, leader="n1", term=1, me="n1"):
        self.me = me
        self.set(leader, term)

    def set(self, leader, term):
        self.state = Leadership(leader, term, leader == self.me)


class SimNode:
    def __init__(self, name, **view):
        self.name = name
        self.guard = ExecutionGuard()
        self.starts = {}                # task_id -> StartRequest of the running session
        self.alive = True
        self.fail_start = None          # error_code to return from the next starts
        self.view = dict(inference_ready=True, algorithms_ready=("PKCI", "PKCT"), cpu_cores=8,
                         cpu_usage=20.0, mem_total_mb=8192, mem_usage=20.0)
        self.view.update(view)
        self.seq = 0
        self.report_seq = 0
        self.reports = []

    def status(self):
        self.seq += 1
        running = tuple(
            TaskStateView(t, g.epoch, self.starts[t].role, self.starts[t].primary_node, self.starts[t].helper_nodes,
                          self.starts[t].idempotency_key, self.starts[t].name, self.starts[t].spec.to_json(),
                          g.assign_term)
            for t, g in sorted(self.guard.running().items()))
        return NodeView(self.name, incarnation=1, seq=self.seq, running_tasks=running, **self.view)

    def report(self, task_id, generation, state, error_code=""):
        self.report_seq += 1
        self.reports.append(TaskReportView(self.name, task_id, generation[0], state, error_code,
                                           incarnation=1, seq=self.report_seq, assign_term=generation[1]))


class SimDispatcher:
    def __init__(self, nodes):
        self.nodes = nodes
        self.starts = []
        self.stops = []

    def start(self, node, request):
        self.starts.append((node, request.task_id, request.generation, request.term, request.role))
        sim = self.nodes.get(node)
        if sim is None or not sim.alive:
            return DispatchResult(False, c.TIMEOUT, "%s unreachable" % node)
        if sim.fail_start:
            return DispatchResult(False, sim.fail_start, "simulated failure")
        decision = sim.guard.on_start(request.term, request.task_id, request.generation)
        if decision.action == REJECT:
            return DispatchResult(False, decision.error_code, decision.message)
        if decision.action in (START, RESTART):
            sim.guard.started(request.task_id, request.generation)
            sim.starts[request.task_id] = request
            if request.role == c.ROLE_PRIMARY:
                sim.report(request.task_id, request.generation, c.REPORT_RUNNING)
        assert decision.action in (START, RESTART, NOOP)
        return DispatchResult(True, message=decision.action)

    def stop(self, node, term, task_id, generation, reason):
        self.stops.append((node, task_id, generation, reason))
        sim = self.nodes.get(node)
        if sim is None or not sim.alive:
            return DispatchResult(False, c.TIMEOUT, "%s unreachable" % node)
        decision = sim.guard.on_stop(term, task_id, generation)
        if decision.action == STOP:
            sim.guard.stopped(task_id)
        return DispatchResult(decision.success, decision.error_code, decision.message)


class SimWorld:
    def __init__(self, names, clock, offline_timeout_s=5.0):
        self.clock = clock
        self.nodes = {n: SimNode(n) for n in names}
        self.cluster = ClusterView(offline_timeout_s)
        self.dispatcher = SimDispatcher(self.nodes)

    def publish(self, controller=None):
        """Every live node publishes NodeStatus; pending TaskReports go to the controller."""
        for node in self.nodes.values():
            if node.alive:
                self.cluster.update(node.status(), self.clock())
                if controller is not None:
                    for report in node.reports:
                        controller.report(report)
                node.reports.clear()

    def run(self, controller, seconds, dt=0.5):
        """Advance simulated time, publishing and stepping the controller every dt."""
        steps = int(round(seconds / dt))
        for _ in range(steps):
            self.clock.advance(dt)
            self.publish(controller)
            controller.step()
