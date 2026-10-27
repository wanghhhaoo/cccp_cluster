import pytest

from cccp_node.core.link_estimator import LinkEstimator


def exchange(x, y, x_seq, t_send, one_way_s, hold_s):
    """x publishes x_seq at t_send; y receives it, holds it, then publishes its echo back."""
    x.on_publish(x_seq, t_send)
    y.on_peer_status("x", 1, x_seq, t_send + one_way_s)
    t_reply = t_send + one_way_s + hold_s
    report = next(r for r in y.reports(t_reply) if r.peer_name == "x")
    x.on_peer_echo("y", report.echo_seq, report.echo_delay_ms, t_reply + one_way_s)


def test_rtt_excludes_hold_time_and_ignores_clock_offsets():
    x, y = LinkEstimator(), LinkEstimator()
    x.on_peer_status("y", 1, 1, 9.0)            # x must know y before it can learn y's echo
    # y's clock could be anything: only differences on each node's own clock are used
    exchange(x, y, x_seq=1, t_send=10.0, one_way_s=0.020, hold_s=0.700)
    report = next(r for r in x.reports(11.0) if r.peer_name == "y")
    assert report.rtt_ms == pytest.approx(40.0, abs=1.0)
    assert report.jitter_ms == 0.0


def test_rtt_and_jitter_are_smoothed():
    x, y = LinkEstimator(), LinkEstimator()
    x.on_peer_status("y", 1, 1, 0.0)
    for i, one_way in enumerate([0.010, 0.030, 0.010, 0.030]):
        exchange(x, y, x_seq=i + 1, t_send=10.0 + i, one_way_s=one_way, hold_s=0.2)
        x.on_peer_status("y", 1, i + 2, 10.5 + i)   # keep y alive on x's side
    report = next(r for r in x.reports(15.0) if r.peer_name == "y")
    assert 20.0 < report.rtt_ms < 60.0
    assert report.jitter_ms > 0.0


def test_repeated_or_unknown_echoes_are_ignored():
    x = LinkEstimator(send_history=4)
    x.on_peer_status("y", 1, 1, 0.0)
    x.on_publish(1, 1.0)
    x.on_peer_echo("y", 1, 0, 1.050)
    x.on_peer_echo("y", 1, 0, 9.000)             # same echo again: no new sample
    x.on_peer_echo("y", 999, 0, 9.000)           # never sent
    x.on_peer_echo("z", 1, 0, 9.000)             # unknown peer
    for seq in range(2, 10):
        x.on_publish(seq, float(seq))
    x.on_peer_echo("y", 2, 0, 3.0)               # forgotten (history of 4)
    assert x.reports(9.5)[0].rtt_ms == pytest.approx(50.0)


def test_loss_rate_from_sequence_gaps():
    est = LinkEstimator(loss_window=10)
    assert est.loss_rate("y") == -1.0
    for seq in [1, 2, 3, 5, 6, 8, 9, 10]:        # 4 and 7 lost
        est.on_peer_status("y", 1, seq, float(seq))
    assert est.loss_rate("y") == pytest.approx(0.2)
    for seq in range(11, 21):                    # a clean window pushes old gaps out
        est.on_peer_status("y", 1, seq, float(seq))
    assert est.loss_rate("y") == 0.0


def test_restart_resets_peer_and_silent_peers_are_dropped():
    est = LinkEstimator(peer_timeout_s=10.0)
    for seq in (1, 5):
        est.on_peer_status("y", 1, seq, 0.0)
    est.on_peer_status("y", 2, 1, 1.0)           # new incarnation starts from seq 1
    assert est.loss_rate("y") == 0.0
    est.on_peer_status("y", 1, 50, 1.0)          # stale incarnation ignored
    report = est.reports(2.0)[0]
    assert (report.echo_seq, report.echo_delay_ms) == (1, 1000)
    assert est.reports(11.5) == []
