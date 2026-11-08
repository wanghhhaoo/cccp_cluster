"""Per-pair delivery of DDS samples, from tcpdump captures taken on every node.

usage: rtps_loss.py <capture dir> [topic substring, default cluster/node_status]

The directory holds one `tcpdump -i eth0 -s 0 -w <node>.pcap udp` per node, named after the
node (edge-01.pcap ...), and a file `addresses` with lines "<node> <IPv4>". Topic names come
from the SEDP publications in the captures, so capture from before the nodes start.

For every writer on the topic: the RTPS sequence numbers its node put on the wire (and to
which addresses), and for every other node which of them reached its interface. A sample on
the receiver's wire that its application never saw was lost inside the receiver (HANDOFF 19).
Standard library only; no tshark needed.
"""
import collections
import struct
import sys
from pathlib import Path

SEDP_PUB = b"\x00\x00\x03\xc2"
SPDP = b"\x00\x01\x00\xc2"


def packets(path):
    data = path.read_bytes()
    magic = data[:4]
    endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    off = 24
    while off + 16 <= len(data):
        sec, frac, caplen, _ = struct.unpack_from(endian + "IIII", data, off)
        off += 16
        frame = data[off:off + caplen]
        off += caplen
        ts = sec + frac / (1e9 if nano else 1e6)
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        ihl = (frame[14] & 15) * 4
        if frame[23] != 17:
            continue
        src = ".".join(map(str, frame[26:30]))
        dst = ".".join(map(str, frame[30:34]))
        udp = 14 + ihl
        dport = struct.unpack_from(">H", frame, udp + 2)[0]
        yield ts, src, dst, dport, frame[udp + 8:]


def submessages(payload):
    """(guid prefix of the sender, [(id, little endian, body)])"""
    if payload[:4] != b"RTPS":
        return None, []
    prefix = payload[8:20]
    out, off = [], 20
    while off + 4 <= len(payload):
        sid, flags = payload[off], payload[off + 1]
        le = flags & 1
        length = struct.unpack_from("<H" if le else ">H", payload, off + 2)[0]
        body = payload[off + 4: off + 4 + length] if length else payload[off + 4:]
        out.append((sid, le, body))
        if not length:
            break
        off += 4 + length
    return prefix, out


def data_sample(le, body):
    """DATA / DATA_FRAG: (writer entity id, sn, serialized payload or None)"""
    e = "<" if le else ">"
    qos_off = struct.unpack_from(e + "H", body, 2)[0]
    writer = body[8:12]
    hi, lo = struct.unpack_from(e + "iI", body, 12)
    return writer, (hi << 32) | lo, body[4 + qos_off:]


def param_list(ser):
    """PID -> raw value, from a PL_CDR encapsulated parameter list."""
    if len(ser) < 4:
        return {}
    le = ser[1] in (3,)          # PL_CDR_LE = 0x0003
    e = "<" if le else ">"
    out, off = {}, 4
    while off + 4 <= len(ser):
        pid, length = struct.unpack_from(e + "HH", ser, off)
        if pid == 1:
            break
        out.setdefault(pid & 0x3FFF, ser[off + 4: off + 4 + length])
        off += 4 + length
    return out, e


def main():
    cap = Path(sys.argv[1])
    topic = sys.argv[2] if len(sys.argv) > 2 else "cluster/node_status"
    nodes = {}
    for line in (cap / "addresses").read_text().split("\n"):
        if line.strip():
            name, ip = line.split()[:2]
            nodes[ip] = name
    writers = {}                       # (prefix, entity) -> topic
    sent = collections.defaultdict(dict)       # writer -> sn -> (ts, set(dst))
    seen = collections.defaultdict(lambda: collections.defaultdict(dict))  # receiver -> writer -> sn -> ts
    owner = {}                          # prefix -> node name
    for pcap in sorted(cap.glob("edge-*.pcap")):
        me = pcap.stem
        for ts, src, dst, dport, payload in packets(pcap):
            prefix, subs = submessages(payload)
            if prefix is None:
                continue
            if src in nodes:
                owner.setdefault(prefix, nodes[src])
            for sid, le, body in subs:
                if sid not in (0x15, 0x16) or len(body) < 20:
                    continue
                writer, sn, ser = data_sample(le, body)
                if writer == SEDP_PUB and sid == 0x15:
                    pl = param_list(ser)
                    if pl:
                        params, e = pl
                        name = params.get(5)
                        guid = params.get(0x5A)
                        if name and guid and len(guid) == 16:
                            n = struct.unpack_from(e + "I", name)[0]
                            writers[(guid[:12], guid[12:])] = name[4:4 + n - 1].decode(errors="replace")
                    continue
                key = (prefix, writer)
                if nodes.get(src) == me:
                    t, dsts = sent[key].get(sn, (ts, set()))
                    dsts.add(dst)
                    sent[key][sn] = (t, dsts)
                else:
                    seen[me][key].setdefault(sn, ts)
    targets = [k for k, t in writers.items() if topic in t]
    print("writers on %s: %d" % (topic, len(targets)))
    for key in sorted(targets, key=lambda k: owner.get(k[0], "?")):
        node = owner.get(key[0], "?")
        sns = sent.get(key, {})
        if not sns:
            print("%s %s: nothing captured on its own interface" % (node, key[0].hex()))
            continue
        lo, hi = min(sns), max(sns)
        gaps = sorted(set(range(lo, hi + 1)) - set(sns))
        dsts = collections.Counter(d for _, ds in sns.values() for d in ds)
        print("\n%s writer %s/%s: sn %d..%d, %d on wire, missing at sender %d, dst %s"
              % (node, key[0].hex(), key[1].hex(), lo, hi, len(sns), len(gaps), dict(dsts)))
        for rx in sorted(seen):
            if rx == node:
                continue
            got = seen[rx].get(key, {})
            lost = sorted(sn for sn in sns if sn not in got)
            # runs of consecutive lost sns, with the sender's timestamps
            runs, start = [], None
            for i, sn in enumerate(lost):
                if start is None:
                    start = sn
                if i + 1 == len(lost) or lost[i + 1] != sn + 1:
                    runs.append((start, sn))
                    start = None
            big = [(a, b) for a, b in runs if b - a + 1 >= 3]
            print("  -> %s: lost %d of %d; runs>=3: %s" % (rx, len(lost), len(sns), ", ".join(
                "sn%d-%d (%d, t=%.0f..%.0f, dst %s)" % (a, b, b - a + 1, sns[a][0], sns[b][0],
                                                       "/".join(sorted(sns[a][1]))) for a, b in big[:8])))


if __name__ == "__main__":
    main()
