from cccp_node.core.cluster_view import ClusterView, LinkView, NodeView, RunningTaskView


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
    node = NodeView("a", running_tasks=(RunningTaskView(7, 2, "primary"),),
                    links=(LinkView("b", rtt_ms=12.0),))
    assert node.runs(7, 2) and not node.runs(7, 1)
    assert node.link_to("b").measured and node.link_to("c") is None
    assert not LinkView("x").measured
