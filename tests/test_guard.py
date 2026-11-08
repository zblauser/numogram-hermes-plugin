"""No hardware here. Burns come from espefuse's own virtual eFuse controller."""
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import guard
from guard import EFUSE_BASES, EfuseGuard, WRITE_OP_CODE, why_blocked


def slip(payload: bytes) -> bytes:
    return b"\xc0" + payload.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc") + b"\xc0"


def command(cmd: int, data: bytes) -> bytes:
    return slip(struct.pack("<BBHI", 0, cmd, len(data), 0) + data)


def write_reg(addr: int, value: int) -> bytes:
    return command(0x09, struct.pack("<IIII", addr, value, 0xFFFFFFFF, 0))


@pytest.mark.parametrize("chip", sorted(EFUSE_BASES))
def test_write_op_code_blocked_on_every_chip(chip):
    conf = EFUSE_BASES[chip] + 0xFC
    out, blocked = EfuseGuard().feed(write_reg(conf, WRITE_OP_CODE))
    assert out == b"" and len(blocked) == 1


@pytest.mark.parametrize("chip", sorted(EFUSE_BASES))
def test_read_op_code_passes(chip):
    frame = write_reg(EFUSE_BASES[chip] + 0x1CC, 0x5AA5)
    assert EfuseGuard().feed(frame) == (frame, [])


def test_same_value_outside_efuse_registers_passes():
    frame = write_reg(0x50116000, WRITE_OP_CODE)
    assert EfuseGuard().feed(frame) == (frame, [])


def test_frame_split_byte_by_byte_still_blocked():
    g = EfuseGuard()
    out = b"".join(g.feed(bytes([b]))[0] for b in write_reg(0x5012D1CC, WRITE_OP_CODE))
    assert out == b"" and len(g.blocked) == 1


def test_good_frames_around_a_burn_survive():
    a, b = write_reg(0x1000, 1), write_reg(0x2000, 2)
    out, blocked = EfuseGuard().feed(a + write_reg(0x5012D1CC, WRITE_OP_CODE) + b)
    assert out == a + b and len(blocked) == 1


def test_escaped_bytes_are_decoded_before_checking():
    # 0x6000 88C0 has an escaped byte in its address.
    frame = write_reg(0x600088C0, WRITE_OP_CODE)
    assert b"\xdb\xdc" in frame
    assert EfuseGuard().feed(frame)[0] == b""


def test_console_bytes_pass_straight_through():
    assert EfuseGuard().feed(b"help\r\n") == (b"help\r\n", [])


def test_large_flash_frame_passes_unchanged():
    frame = command(0x03, bytes(range(256)) * 64)
    assert EfuseGuard().feed(frame) == (frame, [])


def test_ram_load_over_efuse_registers_blocked():
    data = struct.pack("<IIII", 0x200, 1, 0x200, 0x5012D000)
    assert why_blocked(struct.pack("<BBHI", 0, 0x05, 16, 0) + data)


def test_ram_load_elsewhere_passes():
    data = struct.pack("<IIII", 0x4000, 4, 0x1000, 0x4FF00000)
    assert not why_blocked(struct.pack("<BBHI", 0, 0x05, 16, 0) + data)


def _virtual_efuse_writes(monkeypatch, chip, args):
    """Run real espefuse against its emulator and frame each register write."""
    from espefuse import main as espefuse_main
    from espefuse.efuse.emulate_efuse_controller_base import EmulateEfuseControllerBase as Emu

    frames = []
    real = Emu.write_reg

    def spy(self, addr, value, *a, **k):
        frames.append(write_reg(addr, value))
        return real(self, addr, value, *a, **k)

    monkeypatch.setattr(Emu, "write_reg", spy)
    try:
        espefuse_main(["--chip", chip, "--virt", "--do-not-confirm", *args])
    except SystemExit:
        pass
    return frames


@pytest.mark.parametrize("chip", sorted(EFUSE_BASES))
def test_real_espefuse_burn_is_caught(monkeypatch, chip):
    frames = _virtual_efuse_writes(monkeypatch, chip, ["burn-bit", "BLOCK3", "0"])
    assert frames, "espefuse made no register writes"
    assert any(why_blocked(_decode(f)) for f in frames)


@pytest.mark.parametrize("chip", sorted(EFUSE_BASES))
def test_real_espefuse_summary_is_not_blocked(monkeypatch, chip):
    frames = _virtual_efuse_writes(monkeypatch, chip, ["summary"])
    assert not [f for f in frames if why_blocked(_decode(f))]


def _decode(frame: bytes) -> bytes:
    return guard._unslip(frame[1:-1])


class FakeLink:
    def __init__(self):
        self.written = bytearray()
        self.blocked = 0

    def get(self):
        return self

    def write(self, d):
        self.written += d

    def read(self, n):
        return b""

    def drop(self):
        pass


def test_bridge_hangs_up_and_writes_nothing_to_the_board():
    import bridge

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    link = FakeLink()
    threading.Thread(target=bridge.data_server, args=(link, port), daemon=True).start()
    for _ in range(100):
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            break
        except OSError:
            threading.Event().wait(0.02)
    ok = write_reg(0x1000, 1)
    c.sendall(ok)
    threading.Event().wait(0.2)
    c.sendall(write_reg(0x5012D1CC, WRITE_OP_CODE))
    assert c.recv(16) == b""  # closed by the bridge
    assert bytes(link.written) == ok
    assert link.blocked == 1
