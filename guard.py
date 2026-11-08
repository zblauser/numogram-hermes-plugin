"""Stop eFuse burns from crossing the serial bridge.

Every ESP32 family chip burns eFuses the same way: load the write op code 0x5A5A
into the eFuse controller's config register, then set the program bit. Reads load
a different op code, so espefuse summary keeps working. This drops the write op
code and leaves everything else alone.

This stops espefuse and anything else that burns through the ROM or stub loader
protocol. It can't see inside code running on the chip: firmware, or RAM code
loaded before this link existed, can still burn eFuses. Blocks accidents, and
isn't a sandbox.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

SLIP_END = 0xC0
SLIP_ESC = 0xDB
CMD_MEM_BEGIN = 0x05
CMD_WRITE_REG = 0x09
WRITE_OP_CODE = 0x5A5A
WINDOW = 0x400  # controller registers end before this on every chip
MAX_FRAME = 1 << 20

# DR_REG_EFUSE_BASE from espefuse's mem_definition files, esptool 5.6.
EFUSE_BASES = {
    "esp32": 0x3FF5A000,
    "esp32s2": 0x3F41A000,
    "esp32s3": 0x60007000,
    "esp32c2": 0x60008800,
    "esp32c3": 0x60008800,
    "esp32c5": 0x600B4800,
    "esp32c6": 0x600B0800,
    "esp32c61": 0x600B4800,
    "esp32h2": 0x600B0800,
    "esp32h21": 0x600B4000,
    "esp32h4": 0x600B1800,
    "esp32p4": 0x5012D000,
    "esp32s31": 0x20715000,
    "esp32e22": 0xC4008000,
}
_BASES = sorted(set(EFUSE_BASES.values()))


def in_efuse_window(addr: int) -> bool:
    return any(b <= addr < b + WINDOW for b in _BASES)


def _unslip(raw: bytes) -> bytes:
    return raw.replace(b"\xdb\xdc", b"\xc0").replace(b"\xdb\xdd", b"\xdb")


def why_blocked(frame: bytes) -> str | None:
    """frame is the SLIP payload, already unescaped. None means it may pass."""
    if len(frame) < 8 or frame[0] != 0x00:
        return None
    cmd = frame[1]
    data = frame[8:]
    if cmd == CMD_WRITE_REG:
        # Address, value, mask, delay. Some loaders accept several in one command.
        for i in range(0, len(data) - 15, 16):
            addr, value = struct.unpack_from("<II", data, i)
            if in_efuse_window(addr) and value & 0xFFFF == WRITE_OP_CODE:
                return f"eFuse write op code to {addr:#010x}"
    elif cmd == CMD_MEM_BEGIN and len(data) >= 16:
        size, _, _, addr = struct.unpack_from("<IIII", data)
        if any(addr < b + WINDOW and b < addr + size for b in _BASES):
            return f"RAM load over eFuse registers at {addr:#010x}"
    return None


@dataclass
class EfuseGuard:
    """Streaming filter for client-to-board bytes. Frames can split across reads."""

    blocked: list = field(default_factory=list)
    _buf: bytearray | None = None

    def feed(self, data: bytes) -> tuple[bytes, list]:
        out = bytearray()
        new = []
        for b in data:
            if self._buf is None:
                if b == SLIP_END:
                    self._buf = bytearray()
                else:
                    out.append(b)  # console input, no frame open
                continue
            if b != SLIP_END:
                self._buf.append(b)
                if len(self._buf) > MAX_FRAME:
                    # Oversized isn't a real command. Pass it on rather than stall a console.
                    out.append(SLIP_END)
                    out += self._buf
                    self._buf = None
                continue
            raw = bytes(self._buf)
            self._buf = None
            if not raw:
                # Two ends in a row: the second opens the next frame.
                out.append(SLIP_END)
                self._buf = bytearray()
                continue
            reason = why_blocked(_unslip(raw))
            if reason:
                new.append(reason)
            else:
                out.append(SLIP_END)
                out += raw
                out.append(SLIP_END)
        self.blocked += new
        return bytes(out), new
