"""Per-peer RTT, jitter and loss from NodeStatus traffic alone (see LinkQuality.msg).

RTT works like an RTCP receiver report: each node echoes, per peer, the last
seq it received from that peer and how long it held it. The original sender
subtracts the hold time from the time since it sent that seq. Both ends only
ever read their own monotonic clock, so clock skew between nodes is harmless.

Loss is measured on the receive side from gaps in the peer's seq numbers.
All times are local monotonic seconds supplied by the caller.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field

RTT_GAIN = 1 / 8        # smoothing as for TCP SRTT (RFC 6298)
JITTER_GAIN = 1 / 16    # smoothing as for RTP interarrival jitter (RFC 3550)


@dataclass(frozen=True)
class LinkReport:
    """One LinkQuality entry of this node's next NodeStatus."""

    peer_name: str
    rtt_ms: float
    jitter_ms: float
    loss_rate: float
    echo_seq: int
    echo_delay_ms: int


@dataclass
class _Peer:
    incarnation: int = 0
    last_seq: int = 0            # highest seq received
    last_seq_at: float = 0.0     # when it arrived
    received: deque = field(default_factory=deque)  # seqs received inside the loss window
    first_seq: int = 0
    rtt_ms: float = -1.0
    jitter_ms: float = -1.0
    last_sample_ms: float = -1.0
    last_echo_used: int = 0


class LinkEstimator:
    def __init__(self, loss_window: int = 30, peer_timeout_s: float = 10.0, send_history: int = 64):
        self._loss_window = loss_window
        self._peer_timeout_s = peer_timeout_s
        self._send_history = send_history
        self._sent: OrderedDict[int, float] = OrderedDict()   # own seq -> send time
        self._peers: dict[str, _Peer] = {}

    # --------------------------------------------------------- own messages
    def on_publish(self, seq: int, now: float) -> None:
        """Call right before publishing this node's NodeStatus with this seq."""
        self._sent[seq] = now
        while len(self._sent) > self._send_history:
            self._sent.popitem(last=False)

    # --------------------------------------------------------- peer messages
    def on_peer_status(self, peer: str, incarnation: int, seq: int, now: float) -> None:
        """Call for every NodeStatus received from another node."""
        state = self._peers.get(peer)
        if state is None or incarnation != state.incarnation:
            if state is not None and incarnation < state.incarnation:
                return
            state = self._peers[peer] = _Peer(incarnation=incarnation, first_seq=seq)
        if seq <= state.last_seq:
            return
        state.last_seq, state.last_seq_at = seq, now
        state.received.append(seq)
        while state.received and state.received[0] <= seq - self._loss_window:
            state.received.popleft()

    def on_peer_echo(self, peer: str, echo_seq: int, echo_delay_ms: int, now: float) -> None:
        """Call with the LinkQuality entry about *this* node found in a peer's NodeStatus."""
        state = self._peers.get(peer)
        sent_at = self._sent.get(echo_seq)
        if state is None or sent_at is None or echo_seq == state.last_echo_used:
            return
        state.last_echo_used = echo_seq
        sample = (now - sent_at) * 1000.0 - echo_delay_ms
        if sample < 0:
            return
        if state.rtt_ms < 0:
            state.rtt_ms, state.jitter_ms = sample, 0.0
        else:
            state.rtt_ms += RTT_GAIN * (sample - state.rtt_ms)
            state.jitter_ms += JITTER_GAIN * (abs(sample - state.last_sample_ms) - state.jitter_ms)
        state.last_sample_ms = sample

    # --------------------------------------------------------------- output
    def loss_rate(self, peer: str) -> float:
        state = self._peers.get(peer)
        if state is None or not state.received:
            return -1.0
        expected = min(self._loss_window, state.last_seq - state.first_seq + 1)
        return max(0.0, 1.0 - len(state.received) / expected)

    def reports(self, now: float) -> list[LinkReport]:
        """LinkQuality entries for the next NodeStatus; forgets silent peers."""
        for peer in [p for p, s in self._peers.items() if now - s.last_seq_at > self._peer_timeout_s]:
            del self._peers[peer]
        return [
            LinkReport(
                peer_name=peer,
                rtt_ms=state.rtt_ms,
                jitter_ms=state.jitter_ms,
                loss_rate=self.loss_rate(peer),
                echo_seq=state.last_seq,
                echo_delay_ms=int(round((now - state.last_seq_at) * 1000.0)),
            )
            for peer, state in sorted(self._peers.items())
        ]
