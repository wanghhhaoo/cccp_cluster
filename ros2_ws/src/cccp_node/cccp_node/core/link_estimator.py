"""Per-peer RTT, jitter and loss from NodeStatus traffic alone (see LinkQuality.msg).

RTT works like an RTCP receiver report: each node echoes, per peer, the last
seq it received from that peer and how long it held it. The original sender
subtracts the hold time from the time since it sent that seq. Both ends only
ever read their own monotonic clock, so clock skew between nodes is harmless.

Loss is measured on the receive side from gaps in the peer's seq numbers, on
every heartbeat. Echoes travel only in the slow part of a heartbeat (every few
beats), so RTT is sampled less often than loss.
All times are local monotonic seconds supplied by the caller.

On the wire (LinkQuality.msg, protocol 3) every value is one byte: times in
units of 4 ms, loss in percent, the echoed seq as its low 16 bits. 255 means
unknown; measured values saturate at 254, so a link slower than about one
second still reads as slow rather than as unmeasured. See quantize and the
byte_to_* functions.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field

RTT_GAIN = 1 / 8        # smoothing as for TCP SRTT (RFC 6298)
JITTER_GAIN = 1 / 16    # smoothing as for RTP interarrival jitter (RFC 3550)
UNKNOWN = 255           # one-byte "not measured"
TIME_UNIT_MS = 4.0
SEQ_MASK = 0xFFFF       # echo_seq carries the low 16 bits


@dataclass(frozen=True)
class LinkReport:
    """One LinkQuality entry of this node's next NodeStatus."""

    peer_name: str
    rtt_ms: float
    jitter_ms: float
    loss_rate: float
    echo_seq: int
    echo_delay_ms: int



def ms_to_byte(ms: float) -> int:
    return UNKNOWN if ms < 0 else min(UNKNOWN - 1, int(round(ms / TIME_UNIT_MS)))


def byte_to_ms(value: int) -> float:
    return -1.0 if value == UNKNOWN else value * TIME_UNIT_MS


def byte_to_echo_delay_ms(value: int) -> float:
    """A saturated echo delay is useless for an RTT sample: unknown (-1) as well."""
    return -1.0 if value >= UNKNOWN - 1 else value * TIME_UNIT_MS


def rate_to_byte(rate: float) -> int:
    return UNKNOWN if rate < 0 else min(100, int(round(rate * 100)))


def byte_to_rate(value: int) -> float:
    return -1.0 if value == UNKNOWN else value / 100.0


def quantize(report: LinkReport) -> dict:
    """LinkQuality fields (protocol 3) for a report; the peer is named by the caller."""
    return dict(echo_seq=report.echo_seq & SEQ_MASK, rtt_4ms=ms_to_byte(report.rtt_ms),
                jitter_4ms=ms_to_byte(report.jitter_ms), loss_pct=rate_to_byte(report.loss_rate),
                echo_delay_4ms=ms_to_byte(report.echo_delay_ms))


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

    def on_peer_echo(self, peer: str, echo_seq: int, echo_delay_ms: float, now: float) -> None:
        """Call with the LinkQuality entry about *this* node found in a peer's NodeStatus.
        echo_seq may be truncated to its low 16 bits; a negative delay means unknown."""
        state = self._peers.get(peer)
        seq = next((s for s in reversed(self._sent) if s & SEQ_MASK == echo_seq & SEQ_MASK), None)
        if state is None or seq is None or seq == state.last_echo_used or echo_delay_ms < 0:
            return
        sent_at = self._sent[seq]
        state.last_echo_used = seq
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
