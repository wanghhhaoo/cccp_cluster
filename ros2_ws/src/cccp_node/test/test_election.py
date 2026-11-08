import random

import pytest

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


def test_claimed_term_tells_a_raised_term_from_a_new_election():
    n1 = Election("n1", CONFIG, 0.0)
    state = n1.update(peers(peer("n2"), peer("n3")), 6.0)
    assert (state.term, state.claimed_term) == (1, 1)
    followers = (peer("n2", 1, follows="n1"), peer("n3", 1, follows="n1"))
    state = n1.update(peers(*followers, peer("n0", 7, leader=True)), 7.0)   # a lone claim rejoins
    assert (state.term, state.claimed_term, state.is_self) == (8, 1, True)   # same leadership, higher term
    state = n1.update(peers(peer("n2", 9, leader=True, follows="n2"), peer("n3", 9, follows="n2")), 8.0)
    assert not state.is_self and state.claimed_term == 0                     # stepped down
    state = n1.update({}, 20.0)                                              # alone: claims again
    assert state.is_self and state.claimed_term == state.term == 10


# --------------------------------------------- indirect evidence: one-way heartbeat loss (DESIGN 5.3)

def test_incumbent_steps_down_for_a_leader_the_others_follow_but_it_cannot_hear():
    b = Election("n2", CONFIG, 0.0, node_id=2)
    b.update({}, 6.0)                                              # claimed while it heard nobody
    followers = [peer("n%d" % i, 5, follows="n1") for i in (3, 4, 5, 6)]
    state = b.update(peers(*followers), 7.0)
    assert (state.leader, state.is_self, state.term) == ("", False, 5)   # follows nobody, keeps the term
    assert not b.update(peers(*followers), 9.0).is_self            # and does not claim again meanwhile


def test_node_does_not_claim_while_a_majority_follows_a_leader_it_cannot_hear():
    e = Election("n2", CONFIG, 0.0, node_id=2)
    followers = [peer("n%d" % i, 3, follows="n1") for i in (3, 4, 5, 6)]
    for now in (6.0, 10.0, 30.0):
        state = e.update(peers(*followers), now)
        assert (state.leader, state.is_self, state.term) == ("", False, 3)


def test_a_leader_only_a_minority_follows_does_not_stop_the_others_electing():
    e = Election("n2", CONFIG, 0.0, node_id=2)
    view = peers(peer("n3", 1, follows="n1"), peer("n4", 1), peer("n5", 1), peer("n6", 1))
    assert e.update(view, 6.0).is_self                             # 2 of 5 for n1: not a majority


def simulate(n, hears, steps, init=None):
    """n nodes, one Election.update per node per second. hears(t, a, b): does a receive b's heartbeat
    at second t; a peer stays online for a for 5 s after its last heartbeat (node_offline_timeout_s).
    hears(t, a, a) is False once a is dead. Returns [(t, sorted live claimants, highest term)]."""
    names = ["edge-%02d" % i for i in range(1, n + 1)]
    nodes = {x: Election(x, CONFIG, 0.0, node_id=i + 1) for i, x in enumerate(names)}
    for x, (leader, term) in (init or {}).items():               # a starting state, as if elected earlier
        e = nodes[x]
        e._leader, e._term, e._claim_seen = leader, term, 0.0
        e._claimed_term = term if leader == x else 0
    last, history = {}, []
    for t in range(1, steps + 1):
        views = {x: NodeView(x, node_id=i + 1, leader_name=nodes[x].state.leader, term=nodes[x].state.term,
                             is_leader=nodes[x].state.is_self, inference_ready=True, visible_peers=n - 1)
                 for i, x in enumerate(names)}
        for a in names:
            for b in names:
                if a != b and hears(t, a, b):
                    last[(a, b)] = (t, views[b])
        alive = [x for x in names if hears(t, x, x) is not False]
        for a in alive:
            seen = {b: v for (r, b), (heard, v) in last.items() if r == a and t - heard < 5}
            nodes[a].update(seen, float(t), views[a])
        history.append((t, sorted(x for x in alive if nodes[x].state.is_self),
                        max(nodes[x].state.term for x in alive)))
    return history


def deaf(pairs, since=20):
    """Everyone hears everyone, except receiver a stops hearing b for each (a, b) in pairs."""
    return lambda t, a, b: None if a == b else not (t >= since and (a, b) in pairs)


TWO_LEADERS = {"edge-01": ("edge-01", 1), "edge-02": ("edge-02", 2),
               **{"edge-%02d" % i: ("edge-01", 1) for i in range(3, 7)}}


@pytest.mark.parametrize("label, n, hears, init, leader", [
    ("02 starts deaf to 01 while both lead (seen on the server)", 6, deaf({("edge-02", "edge-01")}, 0),
     TWO_LEADERS, "edge-01"),
    ("02 goes deaf to 01 while 01 leads", 6, deaf({("edge-02", "edge-01")}), None, "edge-01"),
    ("01 and 02 deaf to each other", 6, deaf({("edge-02", "edge-01"), ("edge-01", "edge-02")}), None, "edge-01"),
    ("only 03 still hears the leader 01", 6, deaf({(x, "edge-01") for x in ("edge-02", "edge-04", "edge-05",
                                                                          "edge-06")}), None, "edge-02"),
])
def test_one_way_loss_settles_on_one_leader_with_a_stable_term(label, n, hears, init, leader):
    history = simulate(n, hears, 60, init)
    assert all(claimants == [leader] for _, claimants, _ in history[-20:]), (label, history[-20:])
    assert history[-1][2] == history[-20][2], label                # no more term raising


def test_a_dead_leader_is_replaced_and_nobody_keeps_naming_it():
    """Followers lose the dead leader at different times; none may prop it up for the others."""
    def hears(t, a, b):
        if a == b:
            return False if a == "edge-01" and t >= 20 else None
        return not (b == "edge-01" and t >= 20 + int(a[-2:]) % 3)
    history = simulate(12, hears, 50)
    new = next(t for t, claimants, _ in history if t > 20 and claimants)
    assert new - 20 <= 7                                           # node_offline_timeout_s + 2 heartbeats
    assert all(claimants == ["edge-02"] for t, claimants, _ in history if t >= new)


def test_random_one_way_losses_never_leave_two_leaders():
    """Seeded sweep: 1-3 persistent one-way losses (sometimes towards the leader) from t=20.
    The rules before indirect evidence ended with several leaders in 24 of these 300 cases."""
    for seed in range(300):
        rng = random.Random(seed)
        n = rng.randint(4, 12)
        names = ["edge-%02d" % i for i in range(1, n + 1)]
        pairs = {(rng.choice(names), rng.choice(names)) for _ in range(rng.randint(1, 3))}
        if rng.random() < 0.3:
            pairs.add((rng.choice(names[1:]), "edge-01"))
        history = simulate(n, deaf({(a, b) for a, b in pairs if a != b}), 90)
        assert len(history[-1][1]) == 1, (seed, pairs, history[-1])
        assert history[-1][2] == history[59][2], (seed, pairs)


def test_abstaining_is_logged_once_per_unheard_leader(caplog):
    e = Election("n2", CONFIG, 0.0, node_id=2)
    followers = [peer("n%d" % i, 3, follows="n1") for i in (3, 4, 5, 6)]
    with caplog.at_level("INFO", logger="cccp_node.core.election"):
        for now in (6.0, 7.0, 8.0):
            e.update(peers(*followers), now)
    [record] = caplog.records
    assert "node=n1 is followed by 4 of the 5 nodes in view (term=3)" in record.getMessage()
