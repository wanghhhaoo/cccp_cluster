"""In-process cluster simulation for controller and gateway tests.

Each SimNode stands in for one node's executor (with the real ExecutionGuard)
and its heartbeat and TaskList. SimNetwork delivers the leader's commands to
the nodes they address, so term and generation fencing behave as on real
executors; their ExecEvents reach the controller with the next publish.
"""
from cccp_node.control import protocol as p
from cccp_node.control.protocol import ExecEvent
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import ClusterView, NodeView, TaskListView, TaskStateView
from cccp_node.core.election import Leadership
from cccp_node.core.execution_guard import REJECT, RESTART, START, STOP, ExecutionGuard
from cccp_node.core.generation import Generation


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
        """A new election result: claimed at this term if this node won."""
        self.state = Leadership(leader, term, leader == self.me, term if leader == self.me else 0)

    def raise_term(self, term):
        """The leader keeps leading and raises its term past a lone claim (DESIGN 5.3)."""
        self.state = Leadership(self.state.leader, term, self.state.is_self, self.state.claimed_term)


class SimNode:
    def __init__(self, name, **view):
        self.name = name
        self.guard = ExecutionGuard()
        self.starts = {}                # task_id -> ExecCommand of the running session
        self.alive = True
        self.fail_start = None          # error_code to refuse the next starts with
        self.deaf = False               # alive (heartbeats) but its commands never arrive
        self.view = dict(inference_ready=True, algorithms_ready=("PKCI", "PKCT"), cpu_cores=8,
                         cpu_usage=20.0, mem_total_mb=8192, mem_usage=20.0)
        self.view.update(view)
        self.seq = 0
        self.event_seq = 0
        self.events = []

    def role(self, task_id):
        return c.ROLE_PRIMARY if self.starts[task_id].primary == self.name else c.ROLE_HELPER

    def running(self):
        return tuple(
            TaskStateView(t, g.epoch, self.role(t), self.starts[t].primary, self.starts[t].helpers,
                          self.starts[t].idempotency_key, self.starts[t].name, self.starts[t].spec.to_json(),
                          g.assign_term)
            for t, g in sorted(self.guard.running().items()))

    def status(self):
        """Heartbeat and TaskList; tasks_version simply follows seq here."""
        self.seq += 1
        running = self.running()
        return (NodeView(self.name, incarnation=1, seq=self.seq, tasks_version=self.seq, running_tasks=running,
                         **self.view),
                TaskListView(self.name, 1, self.seq, running, self.guard.tombstones()))

    def report(self, task_id, generation, kind, error_code="", command=None):
        self.event_seq += 1
        term, request_id = (command.term, command.request_id) if command else (0, 0)
        self.events.append(ExecEvent(self.name, 1, self.event_seq, term, request_id, task_id, Generation(*generation),
                                     kind, error_code))

    def handle(self, command):
        """What an executor does with a command addressed to it (DESIGN 3.8)."""
        if command.op == p.START:
            if self.fail_start:
                self.report(command.task_id, command.generation, p.REJECTED, self.fail_start, command)
                return
            decision = self.guard.on_start(command.term, command.task_id, command.generation)
            if decision.action in (START, RESTART):
                self.guard.started(command.task_id, command.generation)
                self.starts[command.task_id] = command
            self.report(command.task_id, command.generation,
                        p.REJECTED if decision.action == REJECT else p.RUNNING, decision.error_code, command)
        else:
            decision = self.guard.on_stop(command.term, command.task_id, command.generation,
                                          cancel=command.stop_kind == c.STOP_CANCEL)
            if decision.action == STOP:
                self.guard.stopped(command.task_id)
            self.report(command.task_id, command.generation,
                        p.REJECTED if decision.action == REJECT else p.STOPPED, decision.error_code, command)


class SimNetwork:
    """Delivers commands at once to the live nodes they address; records what was sent."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.commands = []
        self.starts = []                # (node, task_id, generation, term, role) per target
        self.stops = []                 # (node, task_id, generation, reason) per target

    def send(self, command):
        self.commands.append(command)
        for node in command.targets:
            if command.op == p.START:
                role = c.ROLE_PRIMARY if node == command.primary else c.ROLE_HELPER
                self.starts.append((node, command.task_id, command.generation, command.term, role))
            else:
                self.stops.append((node, command.task_id, command.generation, command.reason))
            sim = self.nodes.get(node)
            if sim is not None and sim.alive and not sim.deaf:
                sim.handle(command)


class SimWorld:
    def __init__(self, names, clock, offline_timeout_s=5.0):
        self.clock = clock
        self.nodes = {n: SimNode(n) for n in names}
        self.cluster = ClusterView(offline_timeout_s)
        self.network = SimNetwork(self.nodes)

    def publish(self, controller=None):
        """Every live node publishes its heartbeat and TaskList; its ExecEvents go to the controller."""
        for node in self.nodes.values():
            if node.alive:
                beat, task_list = node.status()
                self.cluster.update(beat, self.clock())
                self.cluster.update_task_list(task_list, self.clock())
                if controller is not None:
                    for event in node.events:
                        controller.on_event(event)
                node.events.clear()

    def run(self, controller, seconds, dt=0.5):
        """Advance simulated time, publishing and stepping the controller every dt."""
        steps = int(round(seconds / dt))
        for _ in range(steps):
            self.clock.advance(dt)
            self.publish(controller)
            controller.step()
