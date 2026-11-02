import pytest

from cccp_node.core.link_estimator import (
    UNKNOWN, LinkEstimator, LinkReport, byte_to_echo_delay_ms, byte_to_ms, byte_to_rate, quantize,
)


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


def test_one_byte_encoding_round_trips_within_a_unit_and_keeps_unknown_apart():
    report = LinkReport("y", rtt_ms=41.0, jitter_ms=-1.0, loss_rate=0.034, echo_seq=0x12345, echo_delay_ms=700)
    q = quantize(report)
    assert q == dict(echo_seq=0x2345, rtt_4ms=10, jitter_4ms=UNKNOWN, loss_pct=3, echo_delay_4ms=175)
    assert (byte_to_ms(q["rtt_4ms"]), byte_to_ms(q["jitter_4ms"]), byte_to_rate(q["loss_pct"])) == (40.0, -1.0, 0.03)
    slow = quantize(LinkReport("y", rtt_ms=3000.0, jitter_ms=0.0, loss_rate=1.0, echo_seq=1, echo_delay_ms=5000))
    assert byte_to_ms(slow["rtt_4ms"]) == 1016.0                  # a very slow link still reads as slow
    assert byte_to_echo_delay_ms(slow["echo_delay_4ms"]) == -1.0  # but its delay is no use for RTT
    assert byte_to_rate(slow["loss_pct"]) == 1.0 and byte_to_rate(UNKNOWN) == -1.0


def test_rtt_from_quantized_echoes_with_truncated_seq():
    x, y = LinkEstimator(), LinkEstimator()
    seq = 0x10005                                                 # past 16 bits
    x.on_peer_status("y", 1, 1, 9.0)
    x.on_publish(seq - 1, 9.0)
    x.on_publish(seq, 10.0)
    y.on_peer_status("x", 1, seq, 10.020)
    q = quantize(next(r for r in y.reports(10.720) if r.peer_name == "x"))
    x.on_peer_echo("y", q["echo_seq"], byte_to_echo_delay_ms(q["echo_delay_4ms"]), 10.740)
    assert next(r for r in x.reports(11.0) if r.peer_name == "y").rtt_ms == pytest.approx(40.0, abs=4.0)


def test_echo_with_unknown_delay_is_not_a_sample():
    x = LinkEstimator()
    x.on_peer_status("y", 1, 1, 9.0)
    x.on_publish(1, 10.0)
    x.on_peer_echo("y", 1, -1.0, 12.0)
    assert next(r for r in x.reports(12.0) if r.peer_name == "y").rtt_ms == -1.0
