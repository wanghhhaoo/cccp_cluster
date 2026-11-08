from cccp_node.core.cluster_view import ClusterView, LinkView, NodeView, TaskListView, TaskStateView


def test_liveness_uses_local_arrival_time():
    view = ClusterView(offline_timeout_s=5.0)
    view.update(NodeView("a", seq=1), now=100.0)
    assert view.is_online("a", 104.9) and not view.is_online("a", 105.0)
    assert not view.is_online("ghost", 100.0)
    assert list(view.online(104.0)) == ["a"] and view.online(106.0) == {}
    assert view.age("a", 102.5) == 2.5 and view.age("ghost", 1.0) is None


def test_old_or_reordered_messages_are_ignored():
    view = ClusterView(offline_timeout_s=5.0)
    assert view.update(NodeView("a", incarnation=2, seq=10), now=1.0)
    assert not view.update(NodeView("a", incarnation=2, seq=9), now=2.0)
    assert not view.update(NodeView("a", incarnation=1, seq=99), now=2.0)   # from before a restart
    assert view.get("a").seq == 10 and view.age("a", 2.0) == 1.0
    assert view.update(NodeView("a", incarnation=3, seq=1), now=3.0)        # restarted
    assert view.get("a").incarnation == 3


def test_node_view_helpers():
    node = NodeView("a", running_tasks=(TaskStateView("t7", 2, "primary", assign_term=3),),
                    links=(LinkView("b", rtt_ms=12.0),))
    assert node.runs("t7", (2, 3)) and not node.runs("t7", (2, 2)) and not node.runs("t7", (1, 3))
    assert node.link_to("b").measured and node.link_to("c") is None
    assert not LinkView("x").measured


def beat(name="a", seq=1, incarnation=1, **kw):
    return NodeView(name, incarnation=incarnation, seq=seq, **kw)


def listed(name="a", version=1, incarnation=1, tasks=(), tombstones=()):
    return TaskListView(name, incarnation, version, tuple(tasks), tuple(tombstones))


def test_slow_fields_are_kept_from_the_last_beat_that_carried_them():
    view = ClusterView(offline_timeout_s=5.0)
    view.update(beat(seq=1, cpu_usage=40.0, cpu_cores=8, links=(LinkView("b", rtt_ms=9.0),)), now=1.0)
    view.update(beat(seq=2, slow=False), now=2.0)                  # empty slow part
    assert (view.get("a").cpu_usage, view.get("a").cpu_cores, view.get("a").link_to("b").rtt_ms) == (40.0, 8, 9.0)
    view.update(beat(seq=3, cpu_usage=70.0), now=3.0)
    assert view.get("a").cpu_usage == 70.0 and view.get("a").links == ()


def test_task_list_is_used_only_once_it_caught_up_with_the_heartbeat():
    view = ClusterView(offline_timeout_s=5.0)
    task = TaskStateView("t1", 1, "primary", assign_term=2)
    view.update(beat(tasks_version=3), now=1.0)
    assert view.task_list("a") is None
    assert view.update_task_list(listed(version=2, tasks=[task]), now=1.0)
    assert view.task_list("a") is None                             # version 3 is still on its way
    assert view.update_task_list(listed(version=3, tasks=[task], tombstones=["t0"]), now=1.1)
    assert view.task_list("a").tasks == (task,) and view.task_list("a").tombstones == ("t0",)
    assert not view.update_task_list(listed(version=2), now=1.2)  # a replay of an older one
    assert view.task_list("a").tasks_version == 3
    assert view.update_task_list(listed(version=4), now=1.3)      # ahead of the heartbeat: fine
    assert view.task_list("a").tasks_version == 4


def test_restart_drops_the_old_task_list():
    view = ClusterView(offline_timeout_s=5.0)
    view.update(beat(incarnation=1, tasks_version=1), now=1.0)
    view.update_task_list(listed(incarnation=1, version=1, tasks=[TaskStateView("t1", 1, "primary")]), now=1.0)
    view.update(beat(incarnation=2, seq=1), now=2.0)
    assert view.task_list("a") is None
    assert not view.update_task_list(listed(incarnation=1, version=5), now=2.1)   # from before the restart
    assert view.update_task_list(listed(incarnation=2, version=0), now=2.2)
    assert view.task_list("a").incarnation == 2


def test_offline_nodes_are_retained_then_forgotten():
    view = ClusterView(offline_timeout_s=5.0, offline_retain_s=300.0)
    view.update(beat("a"), now=0.0)
    view.update_task_list(listed("a", version=0), now=0.0)
    view.update_task_list(listed("ghost", version=1), now=0.0)    # never heard beating
    view.update(beat("b"), now=10.0)
    assert list(view.online(10.0)) == ["b"] and list(view.retained(10.0)) == ["a"]
    assert view.task_list("a") is not None and view.task_list("ghost") is not None
    assert sorted(view.retained(299.9)) == ["a", "b"]                # b went quiet too
    view.update(beat("b", seq=2), now=300.0)
    assert view.retained(300.0) == {} and view.get("a") is None
    assert view.task_list("a") is None and view.task_list("ghost") is None


def test_node_ids_map_to_names_and_visible_peers_count_online_others():
    view = ClusterView(offline_timeout_s=5.0)
    view.update(beat("edge-01", node_id=1), now=0.0)
    view.update_task_list(TaskListView("edge-02", 1, 0, node_id=2), now=0.0)
    view.update(beat("edge-03"), now=4.0)
    assert (view.name_of(1), view.name_of(2), view.name_of(3)) == ("edge-01", "edge-02", None)
    assert view.visible_peers("edge-03", 4.0) == 1 and view.visible_peers("edge-03", 5.0) == 0


def test_changes_report_nodes_going_offline_and_back_with_the_heartbeats_lost():
    view = ClusterView(offline_timeout_s=5.0)
    view.update(beat("a", seq=1), 0.0)
    assert [(ch.name, ch.online, ch.silent_s) for ch in view.changes(0.0)] == [("a", True, 0.0)]
    view.update(beat("a", seq=4), 1.0)                     # seq 2 and 3 were lost
    assert view.changes(1.0) == []
    [gone] = view.changes(6.0)
    assert (gone.name, gone.online, gone.silent_s, gone.missed) == ("a", False, 5.0, 2)
    assert view.changes(7.0) == []                         # reported once
    view.update(beat("a", seq=24), 21.0)                   # kept sending while unheard: a loss in transit
    [back] = view.changes(21.0)
    assert (back.online, back.silent_s, back.missed, back.restarted) == (True, 20.0, 19, False)
    view.changes(30.0)
    view.update(beat("a", seq=1, incarnation=2), 40.0)     # restarted: its seq starts again
    [again] = view.changes(40.0)
    assert again.online and again.restarted and again.missed == 0
