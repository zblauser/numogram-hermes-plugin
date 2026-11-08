"""Turn raw ESP32 console text into structured events. No I/O, no Hermes imports."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_HEX = r"0x[0-9a-fA-F]+"
_REG = re.compile(r"\b(MEPC|RA|MTVAL|MCAUSE|PC|EXCCAUSE|EXCVADDR)\s*:\s*(" + _HEX + r")")
_PANIC = re.compile(r"Guru Meditation Error: Core\s+(\d+) panic'ed \(([^)]*)\)")
_ASSERT = re.compile(r"assert(?:ion)? failed:\s*(\S+)\s+([^\s:]+):(\d+)\s*\((.*?)\)")
_BOOT = re.compile(r"rst:(0x[0-9a-fA-F]+) \(([^)]*)\),boot:(0x[0-9a-fA-F]+) \(([^)]*)\)")
_BACKTRACE = re.compile(r"^Backtrace:\s*(.*)$", re.M)
_ELF = re.compile(r"ELF file SHA256:\s*([0-9a-fA-F]{6,64})")
_PROJECT = re.compile(r"Project name:\s*(\S+)")
_IDF = re.compile(r"ESP-IDF:\s*(\S+)")
_REV = re.compile(r"chip revision: (v[\d.]+)")
_HOSTED_FW = re.compile(r'"t":"c5_fw","major":(\d+),"minor":(\d+),"patch":(\d+)')


@dataclass
class Crash:
    kind: str  # "panic" or "assert"
    reason: str
    core: int | None = None
    registers: dict[str, str] = field(default_factory=dict)
    backtrace: list[str] = field(default_factory=list)
    elf_sha: str | None = None

    @property
    def signature(self) -> str:
        """Stable across reboots: same bug, same signature."""
        pc = self.registers.get("MEPC") or self.registers.get("PC") or ""
        return f"{self.kind}:{self.reason}:{pc}"

    def to_dict(self) -> dict:
        return {**asdict(self), "signature": self.signature}


@dataclass
class Scan:
    boots: int = 0
    last_reset: str | None = None
    boot_mode: str | None = None
    state: str = "unknown"
    crashes: list[Crash] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    project: str | None = None
    idf: str | None = None
    chip_rev: str | None = None
    elf_sha: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["crashes"] = [c.to_dict() for c in self.crashes]
        return d


# Fixed advice only. Nothing here echoes device output, so firmware text can't steer the agent.
CONDITIONS = {
    "blank_flash": ("invalid header: 0xffffffff", "Flash holds no valid image at the boot offset. Flash firmware."),
    "coprocessor_lost": (
        "Not able to connect with ESP-Hosted slave device",
        "Host can't reach the Wi-Fi/BLE coprocessor. If it follows crashes or reset loops, ask a person to power cycle the board.",
    ),
    "rpc_timeout": ("Response not received for", "Coprocessor RPC timed out. Check for a long blocking call, such as a blocking Wi-Fi scan."),
}


def scan(text: str) -> Scan:
    out = Scan()
    boots = list(_BOOT.finditer(text))
    out.boots = len(boots)
    if boots:
        last = boots[-1]
        out.last_reset = last.group(2)
        out.boot_mode = last.group(4)
        out.state = "download_mode" if "DOWNLOAD" in last.group(4) else "running"

    for name, (needle, _) in CONDITIONS.items():
        if needle in text:
            out.conditions.append(name)
    if "blank_flash" in out.conditions:
        out.state = "no_firmware"

    for attr, rx in (("project", _PROJECT), ("idf", _IDF), ("chip_rev", _REV), ("elf_sha", _ELF)):
        m = None
        for m in rx.finditer(text):
            pass
        if m:
            setattr(out, attr, m.group(1))

    out.crashes = _crashes(text, out.elf_sha)
    if out.crashes and out.state == "running":
        out.state = "crashing"
    return out


def _crashes(text: str, elf_sha: str | None) -> list[Crash]:
    found: list[tuple[int, Crash]] = []
    for m in _PANIC.finditer(text):
        tail = text[m.end(): m.end() + 2500]
        c = Crash("panic", m.group(2), core=int(m.group(1)), elf_sha=elf_sha)
        for r in _REG.finditer(tail):
            c.registers.setdefault(r.group(1), r.group(2))
        bt = _BACKTRACE.search(tail)
        if bt:
            c.backtrace = re.findall(_HEX + r"(?=:" + _HEX + r")", bt.group(1))
        found.append((m.start(), c))
    for m in _ASSERT.finditer(text):
        c = Crash("assert", f"{m.group(1)} {m.group(2)}:{m.group(3)} ({m.group(4)})", elf_sha=elf_sha)
        tail = text[m.end(): m.end() + 1500]
        for r in _REG.finditer(tail):
            c.registers.setdefault(r.group(1), r.group(2))
        found.append((m.start(), c))
    return [c for _, c in sorted(found, key=lambda x: x[0])]


_DUMP_END = re.compile(r"Rebooting\.\.\.|ELF file SHA256:|rst:0x")


def open_dump_start(text: str) -> int | None:
    """Where the last crash dump starts, if it hasn't finished printing yet."""
    starts = [m.start() for rx in (_PANIC, _ASSERT) for m in rx.finditer(text)]
    if not starts:
        return None
    s = max(starts)
    return None if _DUMP_END.search(text, s + 1) else s


def advice(conditions: list[str]) -> list[str]:
    return [CONDITIONS[c][1] for c in conditions if c in CONDITIONS]
