from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView
from cccp_node.core.election import Election, ElectionConfig

CONFIG = ElectionConfig(leader_timeout_s=1.0, startup_grace_s=5.0)


def peer(name, term=0, leader=False, protocol=c.PROTOCOL_VERSION, follows=""):
    return NodeView(name, protocol_version=protocol, term=term, is_leader=leader,
                    leader_name=name if leader else follows)


def peers(*views):
    return {v.name: v for v in views}


def test_a_lone_node_claims_after_the_startup_grace():
    e = Election("n1", CONFIG, now=0.0)
    assert e.update({}, 4.9).leader == ""
    state = e.update({}, 5.0)
    assert state.is_self and state.term == 1


def test_smallest_name_claims_and_others_follow():
    n1, n2 = Election("n1", CONFIG, 0.0), Election("n2", CONFIG, 0.0)
    assert not n2.update(peers(peer("n1")), 6.0).is_self          # n1 is smaller: n2 waits
    assert n1.update(peers(peer("n2")), 6.0).is_self
    follower = n2.update(peers(peer("n1", 1, leader=True)), 7.0)
    assert (follower.leader, follower.term, follower.is_self) == ("n1", 1, False)


def test_new_node_follows_the_existing_leader_even_if_its_name_is_smaller():
    n0 = Election("n0", CONFIG, 0.0)
    state = n0.update(peers(peer("n5", 4, leader=True), peer("n7", 4)), 1.0)
    assert (state.leader, state.term, state.is_self) == ("n5", 4, False)
    assert n0.update(peers(peer("n5", 4, leader=True)), 60.0).leader == "n5"     # never preempts


def test_dead_leader_is_replaced_by_the_smallest_survivor_with_a_higher_term():
    n2, n3 = Election("n2", CONFIG, 0.0), Election("n3", CONFIG, 0.0)
    for e in (n2, n3):
        e.update(peers(peer("n1", 3, leader=True)), 10.0)
    gone = peers(peer("n3", 3)), peers(peer("n2", 3))
    assert n2.update(gone[0], 10.5).leader == "n1"               # within leader_timeout: keep waiting
    state = n2.update(gone[0], 11.0)
    assert state.is_self and state.term == 4
    assert not n3.update(gone[1], 11.0).is_self                  # n3 waits for the smaller n2
    assert n3.update(peers(peer("n2", 4, leader=True)), 12.0).leader == "n2"


def test_two_claimants_converge_on_the_smaller_name_or_the_higher_term():
    n1, n2 = Election("n1", CONFIG, 0.0), Election("n2", CONFIG, 0.0)
    n1.update({}, 6.0)
    n2.update({}, 6.0)                                           # partitioned at start: both claim term 1
    assert n1.state.is_self and n2.state.is_self
    assert n1.update(peers(peer("n2", 1, leader=True)), 7.0).is_self
    state = n2.update(peers(peer("n1", 1, leader=True)), 7.0)
    assert (state.leader, state.is_self) == ("n1", False)
    n1.update(peers(peer("n2", 5, leader=True)), 8.0)            # a higher term beats a smaller name
    assert n1.state.leader == "n2" and n1.state.term == 5


def test_lone_claim_with_a_higher_term_cannot_unseat_the_followed_leader():
    """A node cut off for a while claims alone with a higher term; when it rejoins, the leader the
    others follow stays and raises its term past the lone claim (the PreVote problem)."""
    n1 = Election("n1", CONFIG, 0.0)
    n1.update(peers(peer("n2"), peer("n3")), 6.0)                 # n1 leads, term 1
    followers = (peer("n2", 1, follows="n1"), peer("n3", 1, follows="n1"))
    state = n1.update(peers(*followers, peer("n0", 7, leader=True)), 7.0)
    assert state.is_self and state.term == 8                      # kept the role, now above term 7
    n2 = Election("n2", CONFIG, 0.0)
    n2.update(peers(peer("n1", 1, leader=True), peer("n3", 1, follows="n1")), 1.0)
    state = n2.update(peers(peer("n1", 8, leader=True), peer("n3", 8, follows="n1"),
                            peer("n0", 7, leader=True)), 8.0)
    assert state.leader == "n1" and state.term == 8               # follows the supported leader
    lone = Election("n0", CONFIG, 0.0)
    lone.update({}, 6.0)                                          # claimed while alone
    state = lone.update(peers(peer("n1", 8, leader=True), peer("n2", 8, follows="n1")), 8.0)
    assert not state.is_self and state.leader == "n1"             # steps down on rejoining


def test_after_a_partition_the_side_with_more_nodes_keeps_its_leader():
    big = Election("n5", CONFIG, 0.0)                             # led n5..n8 at term 3
    big.update(peers(peer("n6"), peer("n7"), peer("n8")), 6.0)
    others = [peer("n6", 3, follows="n5"), peer("n7", 3, follows="n5"), peer("n8", 3, follows="n5")]
    small = [peer("n1", 9, leader=True), peer("n2", 9, follows="n1")]     # smaller names, higher term
    state = big.update(peers(*others, *small), 7.0)
    assert state.is_self and state.term == 10


def test_incompatible_claims_are_ignored():
    n5 = Election("n5", CONFIG, 0.0)
    state = n5.update(peers(peer("n2", 6), peer("n3", 9, leader=True, protocol=1)), 20.0)
    assert state.leader == "" and state.term == 6                 # n2 is smaller and should claim


def candidate(name, node_id, visible=11, ready=True, battery=-1.0, follows="", **kw):
    kw.setdefault("leader_name", follows)
    return NodeView(name, node_id=node_id, visible_peers=visible, inference_ready=ready,
                    battery=battery, **kw)


def first_claimant(views, config=CONFIG):
    """Which of these nodes claims when nobody leads (each runs the rule on the others)."""
    claimed = []
    for me in views:
        e = Election(me.name, config, 0.0, node_id=me.node_id)
        if e.update(peers(*[v for v in views if v is not me]), 6.0, me=me).is_self:
            claimed.append(me.name)
    return claimed


def test_candidates_rank_by_visible_peers_then_smaller_id():
    assert first_claimant([candidate("a", 1), candidate("b", 2), candidate("c", 3)]) == ["a"]
    assert first_claimant([candidate("a", 1, visible=9), candidate("b", 2, visible=11),
                           candidate("c", 3, visible=11)]) == ["b"]
    assert first_claimant([candidate("z", 1), candidate("a", 2)]) == ["z"]      # the id, not the name


def test_ineligible_candidates_wait_unless_nobody_is_eligible():
    config = ElectionConfig(leader_min_battery=20.0)
    views = [candidate("a", 1, ready=False), candidate("b", 2, battery=10.0), candidate("c", 3, battery=50.0),
             candidate("d", 4)]
    assert first_claimant(views, config) == ["c"]                 # unknown battery (d) would also do
    assert first_claimant([candidate("a", 1, ready=False), candidate("b", 2, ready=False)], config) == ["a"]


def test_incumbent_keeps_the_role_against_a_better_connected_node():
    e = Election("b", CONFIG, 0.0, node_id=2)
    me = candidate("b", 2, visible=5)
    e.update(peers(candidate("c", 3, visible=5)), 6.0, me=me)
    assert e.state.is_self
    state = e.update(peers(candidate("a", 1, visible=11, follows="b")), 7.0, me=me)
    assert state.is_self and state.leader == "b"


def test_equal_claims_prefer_the_smaller_id():
    e = Election("z", CONFIG, 0.0, node_id=1)
    e.update({}, 6.0, me=candidate("z", 1))                      # both claimed term 1 while apart
    assert e.update(peers(candidate("a", 2, term=1, is_leader=True, leader_name="a")), 7.0,
                    me=candidate("z", 1)).is_self
    other = Election("a", CONFIG, 0.0, node_id=2)
    other.update({}, 6.0, me=candidate("a", 2))
    state = other.update(peers(candidate("z", 1, term=1, is_leader=True, leader_name="z")), 7.0,
                         me=candidate("a", 2))
    assert state.leader == "z" and not state.is_self
