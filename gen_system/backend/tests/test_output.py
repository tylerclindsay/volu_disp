import socket

import numpy as np
import pytest

from test_geometry import PROTOTYPE
from test_mapping import mapping_of, with_sections
from volu_gen.geometry import load_display_config
from volu_gen.mapping import Mapping
from volu_gen.output import (MAX_PIXELS_PER_PACKET, NodeSim, Packetizer, Pixels, ProtocolError,
                             Sender, Sync, decode, encode_pixels, encode_sync, subnet_broadcast)


def random_frame(P: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (P, 3), dtype=np.uint8)


# --- protocol: fixed test vectors (docs/protocol.md) ---

def test_sync_bytes():
    assert encode_sync(0x01020304) == bytes([0xA7, 2, 2, 0, 0x04, 0x03, 0x02, 0x01])


def test_pixels_bytes():
    pkt = encode_pixels(5, 486, bytes([1, 2, 3, 4, 5, 6]), show_now=True)
    assert pkt == bytes([0xA7, 2, 1, 1, 5, 0, 0, 0, 0xE6, 0x01, 2, 0, 1, 2, 3, 4, 5, 6])
    assert decode(pkt) == Pixels(5, 486, bytes([1, 2, 3, 4, 5, 6]), True)
    assert decode(encode_sync(7)) == Sync(7)


def test_frame_counter_wraps_u32():
    assert decode(encode_sync(2**32 + 3)) == Sync(3)


def test_max_packet():
    assert MAX_PIXELS_PER_PACKET == 486
    assert len(encode_pixels(0, 0, bytes(486 * 3))) == 1470
    with pytest.raises(ProtocolError):
        encode_pixels(0, 0, bytes(487 * 3))
    with pytest.raises(ProtocolError):
        encode_pixels(0, 0, b"")


@pytest.mark.parametrize("pkt,reason", [
    (b"\xa7\x02\x02", "length"),
    (b"\xa6\x02\x02\x00\x00\x00\x00\x00", "magic"),
    (b"\xa7\x01\x02\x00\x00\x00\x00\x00", "version"),
    (b"\xa7\x02\x02\x01\x00\x00\x00\x00", "flags"),           # SHOW_NOW on SYNC
    (b"\xa7\x02\x01\x02\x00\x00\x00\x00\x00\x00\x01\x00abc", "flags"),
    (b"\xa7\x02\x01\x00\x00\x00\x00\x00\x00\x00\x02\x00abc", "length"),  # count 2, 1 pixel
    (b"\xa7\x02\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00", "count"),
])
def test_decode_rejects(pkt, reason):
    with pytest.raises(ProtocolError) as e:
        decode(pkt)
    assert e.value.reason == reason


def test_unknown_type_ignored():
    assert decode(b"\xa7\x02\x03\x00\x00\x00\x00\x00") is None
    assert decode(b"\xa7\x02\x09\x00\x00\x00\x00\x00\xff") is None


# --- packetizer ---

def deliver(mapping: Mapping, frame: np.ndarray, frame_no: int = 0) -> list[NodeSim]:
    """Packetize frame, feed each node's packets and a SYNC to a NodeSim per node."""
    sims = [NodeSim(n.buffer_len) for n in mapping.nodes]
    for sim, pkts in zip(sims, Packetizer(mapping).packetize(frame, frame_no)):
        for p in pkts:
            sim.receive(bytes(p))
        sim.receive(encode_sync(frame_no))
    return sims


@pytest.mark.parametrize("sections,size", [
    # prototype: 480 pixels per node, one packet each
    ([((0, 9), (0, 11), (0, 3)), ((0, 9), (0, 11), (4, 7))], None),
    # split across x: node buffers are gathers, not slices of the frame
    ([((0, 2), (0, 11), (0, 7)), ((3, 9), (0, 11), (0, 7))], None),
    # 64 strips × 100 pixels = 6400 per node: 14 packets, crossing strip boundaries
    ([((0, 7), (0, 99), (0, 7)), ((0, 7), (0, 99), (8, 15))], (8, 100, 16)),
])
def test_packetize_round_trip(sections, size):
    """Every node shows exactly frame[buffer_index], and the frame is complete."""
    m = mapping_of(with_sections(*sections, size=size))
    frame = random_frame(m.geometry.pixel_count)
    for sim, n in zip(deliver(m, frame), m.nodes):
        assert np.array_equal(sim.front, frame[n.buffer_index])
        assert sim.stats["shown"] == 1 and sim.stats["incomplete"] == 0
        assert sim.stats["clipped"] == 0 and not any(k.startswith("bad") for k in sim.stats)


def test_packets_are_full():
    """6400 pixels: 13 full packets of 486 and one of 82, at consecutive offsets."""
    m = mapping_of(with_sections(((0, 7), (0, 99), (0, 7)), ((0, 7), (0, 99), (8, 15)),
                                 size=(8, 100, 16)))
    pkts = Packetizer(m).packetize(random_frame(m.geometry.pixel_count), 9)[0]
    decoded = [decode(bytes(p)) for p in pkts]
    assert [d.count for d in decoded] == [486] * 13 + [82]
    assert [d.offset for d in decoded] == list(range(0, 6400, 486))
    assert all(d.frame == 9 and not d.show_now for d in decoded)


def test_packets_reuse_buffers_and_update_frame_no():
    m = Mapping(load_display_config(PROTOTYPE))
    p = Packetizer(m)
    a, b = random_frame(960, 1), random_frame(960, 2)
    first = p.packetize(a, 1)[0][0]
    p.packetize(b, 2)
    assert decode(bytes(first)).frame == 2   # same buffer, rewritten
    assert decode(bytes(first)).rgb == b[:480].tobytes()


def test_show_now_and_node_subset():
    m = Mapping(load_display_config(PROTOTYPE))
    out = Packetizer(m).packetize(random_frame(960), 0, show_now=True, nodes={1})
    assert out[0] is None
    assert decode(bytes(out[1][-1])).show_now


def test_packetize_checks_frame():
    p = Packetizer(Mapping(load_display_config(PROTOTYPE)))
    with pytest.raises(ValueError):
        p.packetize(np.zeros((960, 3), np.float32), 0)
    with pytest.raises(ValueError):
        p.packetize(np.zeros((959, 3), np.uint8), 0)


# --- node simulator: receive rules (docs/protocol.md → Frames) ---

def test_late_pixels_dropped():
    sim = NodeSim(4)
    sim.receive(encode_pixels(10, 0, bytes([9] * 12)))
    for old in (9, 2):           # 1 and 8 frames old
        sim.receive(encode_pixels(old, 0, bytes([1] * 12)))
    assert sim.stats["late"] == 2 and sim.back[0, 0] == 9
    sim.receive(encode_pixels(1, 0, bytes([5] * 12)))   # 9 old: taken as a restart
    assert sim.current == 1 and sim.back[0, 0] == 5


def test_late_window_across_wrap():
    sim = NodeSim(1)
    sim.receive(encode_pixels(2, 0, b"\x01\x01\x01"))
    sim.receive(encode_pixels(2**32 - 1, 0, b"\x02\x02\x02"))   # 3 frames old
    assert sim.stats["late"] == 1


def test_missing_packets_keep_previous_pixels():
    sim = NodeSim(4)
    sim.receive(encode_pixels(0, 0, bytes([7] * 12)))
    sim.receive(encode_sync(0))
    sim.receive(encode_pixels(1, 2, bytes([8] * 6)))
    sim.receive(encode_sync(1))
    assert sim.front[:, 0].tolist() == [7, 7, 8, 8]
    assert sim.stats["shown"] == 2 and sim.stats["incomplete"] == 1


def test_clipped_and_bad_counted():
    sim = NodeSim(2)
    sim.receive(encode_pixels(0, 1, bytes(9)))
    sim.receive(b"\x00" * 8)
    assert sim.stats["clipped"] == 2 and sim.stats["bad:magic"] == 1


# --- sender over loopback UDP ---

def test_sender_loopback():
    m = Mapping(load_display_config(PROTOTYPE))
    socks = []
    for _ in m.nodes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(2)
        socks.append(s)
    targets = [s.getsockname() for s in socks]
    frames = [random_frame(960, k) for k in range(3)]
    with Sender(m, targets, targets) as tx:
        tx.frame_no = 2**32 - 1       # check the counter wraps
        for f in frames:
            tx.send_frame(f)
        assert tx.frame_no == 2 and tx.stats["send_errors"] == 0
    for s, n in zip(socks, m.nodes):
        sim = NodeSim(n.buffer_len)
        for _ in range(2 * len(frames)):    # one PIXELS + one SYNC per frame
            sim.receive(s.recv(2048))
        s.close()
        assert sim.stats["shown"] == 3 and sim.stats["incomplete"] == 0
        assert sim.shown_frame == 1
        assert np.array_equal(sim.front, frames[-1][n.buffer_index])


def test_subnet_broadcast():
    m = Mapping(load_display_config(PROTOTYPE))
    assert subnet_broadcast(m) == "192.168.1.255"
    d = with_sections(((0, 9), (0, 11), (0, 3)), ((0, 9), (0, 11), (4, 7)))
    d["drive_nodes"][1]["ip"] = "192.168.2.102"
    with pytest.raises(ValueError, match="several"):
        subnet_broadcast(mapping_of(d))
