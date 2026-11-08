"""Check firmware pin use against the board's KiCad netlist.

Reads a netlist exported from KiCad (`kicad-cli sch export netlist`, or
File > Export > Netlist in the schematic editor) and the GPIO numbers the firmware
uses, then reports:
  - pins whose firmware role doesn't match the net on the board (SDA vs SCL)
  - pins the firmware uses that go nowhere on the board
  - flash pins wired to something else
  - strapping pins, and what else sits on their net
  - USB-Serial/JTAG pins reused, which also stops flashing and logs over USB
  - input-only pins the firmware drives
  - one pin claimed by two roles

Pin facts come from ESP-IDF's GPIO docs (docs/en/api-reference/peripherals/gpio/*.inc,
v5.5). Read-only: nothing here touches the board.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CHIPS: dict[str, dict] = {
    "esp32": {"strapping": {0, 2, 5, 12, 15}, "flash": {6, 7, 8, 9, 10, 11, 16, 17}, "usb_jtag": set(),
              "input_only": {34, 35, 36, 37, 38, 39}, "max": 39, "aliases": {"TXD0": 1, "RXD0": 3, "SENSOR_VP": 36, "SENSOR_CAPP": 37,
              "SENSOR_CAPN": 38, "SENSOR_VN": 39, "CLK": 6, "SD0": 7, "SD1": 8, "SD2": 9, "SD3": 10, "CMD": 11}},
    "esp32s2": {"strapping": {0, 45, 46}, "flash": set(range(26, 33)), "usb_jtag": set(),
                "input_only": {46}, "max": 46, "aliases": {"TXD0": 43, "RXD0": 44}},
    "esp32s3": {"strapping": {0, 3, 45, 46}, "flash": set(range(26, 33)), "usb_jtag": {19, 20},
                "input_only": set(), "max": 48, "aliases": {"TXD0": 43, "RXD0": 44}},
    "esp32c2": {"strapping": {8, 9}, "flash": set(range(12, 18)), "usb_jtag": set(),
                "input_only": set(), "max": 20, "aliases": {"TXD0": 20, "RXD0": 19}},
    "esp32c3": {"strapping": {2, 8, 9}, "flash": set(range(12, 18)), "usb_jtag": {18, 19},
                "input_only": set(), "max": 21, "aliases": {"TXD0": 21, "RXD0": 20}},
    "esp32c5": {"strapping": {2, 7, 25, 27, 28}, "flash": set(range(16, 23)), "usb_jtag": {13, 14},
                "input_only": set(), "max": 28, "aliases": {}},
    "esp32c6": {"strapping": {4, 5, 8, 9, 15}, "flash": set(range(24, 31)), "usb_jtag": {12, 13},
                "input_only": set(), "max": 30, "aliases": {"TXD0": 16, "RXD0": 17}},
    "esp32c61": {"strapping": set(), "flash": {14, 15, 16, 17, 19, 20, 21}, "usb_jtag": {12, 13},
                 "input_only": set(), "max": None, "aliases": {}},
    "esp32h2": {"strapping": {2, 3, 8, 9, 25}, "flash": set(range(15, 22)), "usb_jtag": {26, 27},
                "input_only": set(), "max": 27, "aliases": {"TXD0": 24, "RXD0": 23}},
    "esp32h21": {"strapping": {8, 13, 14}, "flash": set(range(19, 26)), "usb_jtag": {17, 18},
                 "input_only": set(), "max": None, "aliases": {}},
    "esp32p4": {"strapping": {34, 35, 36, 37, 38}, "flash": set(), "usb_jtag": {24, 25},
                "input_only": set(), "max": 54, "aliases": {"TXD0": 37, "RXD0": 38}},
}

# Role words, matched as whole tokens in macro names, struct fields and net names.
ROLES = {
    "SDA": {"SDA"}, "SCL": {"SCL"}, "MOSI": {"MOSI", "SDO", "COPI"}, "MISO": {"MISO", "SDI", "CIPO"},
    "SCK": {"SCK", "SCLK", "CLK"}, "CS": {"CS", "SS", "NSS", "CSN", "SPICS"}, "TX": {"TX", "TXD"},
    "RX": {"RX", "RXD"}, "LED": {"LED"}, "BUTTON": {"BUTTON", "BTN", "SW", "KEY"},
    "BUZZER": {"BUZZER", "BEEP", "PIEZO"}, "RST": {"RST", "RESET"}, "INT": {"INT", "IRQ"},
    "DC": {"DC"}, "PWM": {"PWM"},
}
# Roles where the chip drives the pin.
OUTPUT_ROLES = {"MOSI", "SCK", "CS", "TX", "LED", "BUZZER", "RST", "DC", "PWM", "SCL"}
_TOKEN = re.compile(r"[A-Za-z]+")


def roles_in(name: str) -> set[str]:
    toks = {t.upper() for t in _TOKEN.findall(re.sub(r"([a-z])([A-Z])", r"\1_\2", name))}
    return {r for r, words in ROLES.items() if toks & words}


# --- KiCad netlist (s-expression) -------------------------------------------------

def _sexp(text: str):
    tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')
    stack: list[list] = [[]]
    for m in tok.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ) in netlist")
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", t[1:-1]))
        else:
            stack[-1].append(t)
    if len(stack) != 1:
        raise ValueError("unbalanced ( in netlist")
    return stack[0]


def _kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _val(node, name, default=None):
    k = _kids(node, name)
    return k[0][1] if k and len(k[0]) > 1 and isinstance(k[0][1], str) else default


@dataclass
class Board:
    chip: str | None
    ref: str | None
    value: str | None
    # GPIO number -> (net name, [(ref, pin, value of other parts on the net)])
    gpio_nets: dict[int, tuple[str, list[tuple[str, str, str]]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


_CHIP_IN_VALUE = re.compile(r"ESP32[-_]?(C61|C2|C3|C5|C6|H21|H2|P4|S2|S3)?", re.I)
_GPIO_IN_PIN = re.compile(r"(?:^|[^A-Za-z])(?:GPIO|IO)(\d{1,2})(?![0-9])", re.I)


def chip_from_value(value: str) -> str | None:
    m = _CHIP_IN_VALUE.search(value or "")
    if not m:
        return None
    return "esp32" + (m.group(1) or "").lower()


def parse_netlist(text: str, chip: str | None = None) -> Board:
    root = _sexp(text)
    exp = next((n for n in root if isinstance(n, list) and n and n[0] == "export"), None)
    if exp is None:
        raise ValueError("Not a KiCad netlist (no export block). Export with: kicad-cli sch export netlist")
    comps = {}
    for comps_block in _kids(exp, "components"):
        for c in _kids(comps_block, "comp"):
            comps[_val(c, "ref")] = _val(c, "value", "") or ""
    esp = [(r, v) for r, v in comps.items() if chip_from_value(v)]
    b = Board(chip=chip, ref=None, value=None)
    if not esp:
        b.notes.append("No ESP32 part found by its value field. Pass the chip, and check the netlist is from the right schematic.")
        return b
    if len(esp) > 1:
        b.notes.append(f"Several ESP32 parts ({', '.join(r for r, _ in esp)}); checked {esp[0][0]} only.")
    b.ref, b.value = esp[0]
    b.chip = chip or chip_from_value(b.value)
    aliases = CHIPS.get(b.chip, {}).get("aliases", {})
    for nets in _kids(exp, "nets"):
        for n in _kids(nets, "net"):
            name = _val(n, "name", "") or ""
            nodes = _kids(n, "node")
            mine = [x for x in nodes if _val(x, "ref") == b.ref]
            others = [(_val(x, "ref"), _val(x, "pin"), comps.get(_val(x, "ref"), "")) for x in nodes
                      if _val(x, "ref") != b.ref]
            for x in mine:
                fn = _val(x, "pinfunction", "") or ""
                m = _GPIO_IN_PIN.search(fn)
                g = int(m.group(1)) if m else aliases.get(fn.upper().split("/")[0])
                if g is None:
                    continue
                if name.startswith("unconnected-") or (not others and name.startswith("Net-(")):
                    continue
                b.gpio_nets[g] = (name, others)
    return b


# --- Firmware ---------------------------------------------------------------------

@dataclass
class Use:
    gpio: int
    name: str
    file: str
    line: int

    @property
    def roles(self) -> set[str]:
        return roles_in(self.name)


_DEFINE = re.compile(r"^\s*#\s*define\s+(\w+)\s+\(?\s*(?:\(gpio_num_t\)\s*)?(?:GPIO_NUM_)?(\d{1,2})\s*\)?\s*(?://.*|/\*.*)?$")
_FIELD = re.compile(r"\.(\w*(?:io_num|gpio_num|_pin|_io|_gpio))\s*=\s*(?:\(gpio_num_t\)\s*)?(?:GPIO_NUM_)?(\d{1,2})\b")
_CONST = re.compile(r"\b(?:static\s+)?(?:const\s+)?(?:gpio_num_t|int|uint8_t)\s+(\w+)\s*=\s*(?:GPIO_NUM_)?(\d{1,2})\s*;")
_KCONFIG = re.compile(r"^CONFIG_(\w*(?:GPIO|PIN|_IO)\w*)=(\d{1,2})$")
_PINNY = re.compile(r"PIN|GPIO|_IO\b|IO_NUM", re.I)
_SKIP_DIRS = {"build", "managed_components", ".git"}
_SIGNAL_ROLES = {"SDA", "SCL", "MOSI", "MISO", "SCK", "CS", "TX", "RX"}
_NOT_A_PIN = re.compile(r"COUNT|SIZE|LEN|BUF|MAX|MIN|DELAY|_MS\b|RATE|FREQ|HZ|PORT|HOST|CHAN|BRIGHT|TIMEOUT|NUM_\w|LEVEL|ADDR|BIT|MODE|SPEED", re.I)


def _is_pin_name(name: str) -> bool:
    """LED_GPIO, BUTTON_PIN, I2C_SDA: yes. LED_BRIGHTNESS, UART_TX_BUF, I2C_PORT: no."""
    if _NOT_A_PIN.search(name):
        return False
    return bool(_PINNY.search(name) or roles_in(name) & _SIGNAL_ROLES)


def firmware_uses(project_dir: str | Path, limit_files: int = 400) -> list[Use]:
    root = Path(project_dir)
    uses: list[Use] = []
    files = []
    for p in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and p.suffix in {".c", ".h", ".cpp", ".hpp", ".cc"}:
            files.append(p)
    for p in files[:limit_files]:
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for m in (_DEFINE.match(line), _CONST.search(line)):
                if m and _is_pin_name(m.group(1)):
                    uses.append(Use(int(m.group(2)), m.group(1), str(p.relative_to(root)), i))
            for m in _FIELD.finditer(line):
                uses.append(Use(int(m.group(2)), m.group(1), str(p.relative_to(root)), i))
    sdk = root / "sdkconfig"
    if sdk.is_file():
        for i, line in enumerate(sdk.read_text(errors="replace").splitlines(), 1):
            m = _KCONFIG.match(line.strip())
            if m:
                uses.append(Use(int(m.group(2)), "CONFIG_" + m.group(1), "sdkconfig", i))
    return uses


# --- Checks -----------------------------------------------------------------------

def check(board: Board, uses: list[Use]) -> dict:
    facts = CHIPS.get(board.chip or "")
    out: dict = {"chip": board.chip, "part": board.value and f"{board.ref} {board.value}",
                 "findings": [], "notes": list(board.notes)}
    if facts is None:
        out["notes"].append(f"No pin facts for chip {board.chip!r}. Known: {', '.join(CHIPS)}.")
        return out
    f = out["findings"]

    def add(severity, gpio, what, why, use=None, **extra):
        item = {"severity": severity, "gpio": gpio, "issue": what, "why": why}
        if use:
            item["firmware"] = f"{use.name} at {use.file}:{use.line}"
        item.update(extra)
        f.append(item)

    by_role_net: dict[str, int] = {}
    for g, (net, _) in board.gpio_nets.items():
        for r in roles_in(net):
            by_role_net.setdefault(r, g)

    seen: dict[int, Use] = {}
    for u in uses:
        g = u.gpio
        if facts["max"] is not None and g > facts["max"]:
            add("error", g, "no such GPIO", f"{board.chip} has GPIO0-{facts['max']}.", u)
            continue
        prev = seen.get(g)
        if prev and prev.roles and u.roles and not (prev.roles & u.roles):
            add("error", g, "two roles on one pin",
                f"{prev.name} ({prev.file}:{prev.line}) and {u.name} both use GPIO{g}.", u)
        seen.setdefault(g, u)
        net = board.gpio_nets.get(g)
        if g in facts["flash"]:
            add("error", g, "flash pin", "This pin drives the SPI flash or PSRAM. Using it for anything else stops the chip.", u)
        if g in facts["usb_jtag"]:
            add("warning", g, "USB-Serial/JTAG pin",
                "Reusing it as a GPIO turns off USB-Serial/JTAG: no more flashing or logs over the USB port, "
                "which is how numogram talks to the board. Flash over UART0 instead, or move this signal.", u)
        if g in facts["input_only"] and u.roles & OUTPUT_ROLES:
            add("error", g, "input-only pin driven", f"GPIO{g} can't drive output, but the firmware uses it as {', '.join(sorted(u.roles))}.", u)
        fw_roles = u.roles
        right = sorted({by_role_net[r] for r in fw_roles if r in by_role_net} - {g})
        hint = f" The board's {'/'.join(sorted(fw_roles))} net is on GPIO{right[0]}." if right else ""
        if net is None:
            if board.ref:
                add("error" if right else "warning", g, "not wired",
                    f"The firmware uses GPIO{g}, but on {board.ref} it connects to nothing.{hint}", u)
            continue
        name, others = net
        net_roles = roles_in(name)
        if fw_roles and not (fw_roles & net_roles) and (net_roles or right):
            add("error", g, "role mismatch",
                f"Firmware treats GPIO{g} as {'/'.join(sorted(fw_roles))}, but on the board it's net {name}.{hint}", u,
                net=name)
        if g in facts["strapping"]:
            parts = ", ".join(f"{r} {v}".strip() for r, _, v in others[:4]) or "nothing else"
            add("info", g, "strapping pin",
                f"GPIO{g} sets the boot mode at reset. Its net {name} also connects {parts}. "
                "Anything that pulls it the wrong way at power-up can stop the firmware booting.", u, net=name)
    for g, (name, _) in sorted(board.gpio_nets.items()):
        if g in facts["flash"]:
            add("error", g, "flash pin wired", f"GPIO{g} is a flash pin but the board connects it to net {name}.")
        elif g not in seen and roles_in(name):
            add("info", g, "wired but unused", f"The board routes {name} to GPIO{g}, and the firmware never names GPIO{g}.", net=name)
    out["checked"] = {"firmware_pins": len(seen), "board_gpio_nets": len(board.gpio_nets)}
    return out


_TARGET = re.compile(r'^CONFIG_IDF_TARGET="(\w+)"', re.M)


def project_target(project_dir: str | Path) -> str | None:
    for name in ("sdkconfig", "sdkconfig.defaults"):
        f = Path(project_dir) / name
        if f.is_file() and (m := _TARGET.search(f.read_text(errors="replace"))):
            return m.group(1)
    return None


_ORDER = {"error": 0, "warning": 1, "info": 2}


def run(netlist_path: str | Path, project_dir: str | Path, chip: str | None = None) -> dict:
    p = Path(netlist_path)
    if not p.is_file():
        raise FileNotFoundError(f"No netlist at {p}. Export one with: kicad-cli sch export netlist -o board.net board.kicad_sch")
    if not Path(project_dir).is_dir():
        raise FileNotFoundError(f"No project at {project_dir}")
    board = parse_netlist(p.read_text(errors="replace"), chip)
    res = check(board, firmware_uses(project_dir))
    target = project_target(project_dir)
    res["firmware_target"] = target
    if target and board.chip and target != board.chip:
        res["findings"].insert(0, {"severity": "error", "gpio": None, "issue": "wrong chip",
                                   "why": f"The firmware targets {target}, but the board's {board.ref} is {board.value} ({board.chip})."})
    res["findings"].sort(key=lambda x: _ORDER.get(x["severity"], 3))
    return res
