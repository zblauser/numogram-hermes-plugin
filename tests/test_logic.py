import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


logscan = _load("logscan")
audit = _load("audit")

PANIC = """ESP-ROM:esp32p4-eco2-20240710
rst:0x7 (HP_SYS_HP_WDT_RESET),boot:0x31d (SPI_FAST_FLASH_BOOT)
I (258) app_init: Project name:     radio_probe
I (267) app_init: ELF file SHA256:  0be7a1e10...
I (271) app_init: ESP-IDF:          v5.5.5
Guru Meditation Error: Core  0 panic'ed (Load access fault). Exception was unhandled.

Core  0 register dump:
MEPC    : 0x4ff032a8  RA      : 0x40027010  SP      : 0x4ff1dc90  GP      : 0x4ff11a80
MSTATUS : 0x00011880  MTVEC   : 0x4ff00003  MCAUSE  : 0x00000005  MTVAL   : 0x00000052
"""


def test_panic():
    s = logscan.scan(PANIC)
    assert s.state == "crashing"
    assert s.project == "radio_probe" and s.idf == "v5.5.5"
    c = s.crashes[0]
    assert c.reason == "Load access fault" and c.core == 0
    assert c.registers["MEPC"] == "0x4ff032a8" and c.registers["RA"] == "0x40027010"
    assert c.elf_sha == "0be7a1e10"


def test_signature_stable_across_reboots():
    s = logscan.scan(PANIC + PANIC)
    assert s.boots == 2 and len(s.crashes) == 2
    assert s.crashes[0].signature == s.crashes[1].signature


def test_xtensa_backtrace():
    t = "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.\n" \
        "PC      : 0x400d1234  PS      : 0x00060030\nBacktrace: 0x400d1234:0x3ffb1f00 0x400d5678:0x3ffb1f20\n"
    c = logscan.scan(t).crashes[0]
    assert c.registers["PC"] == "0x400d1234" and c.backtrace == ["0x400d1234", "0x400d5678"]


def test_assert():
    t = "assert failed: hosted_memcpy os_wrapper.c:75 (src)\n"
    c = logscan.scan(t).crashes[0]
    assert c.kind == "assert" and "os_wrapper.c:75" in c.reason


def test_blank_flash():
    t = "rst:0x17 (CHIP_USB_UART_RESET),boot:0x31c (SPI_FAST_FLASH_BOOT)\ninvalid header: 0xffffffff\n"
    s = logscan.scan(t)
    assert s.state == "no_firmware" and "blank_flash" in s.conditions


def test_download_mode():
    t = "rst:0x17 (CHIP_USB_UART_RESET),boot:0x304 (DOWNLOAD(USB/UART0/SPI))\nwaiting for download\n"
    assert logscan.scan(t).state == "download_mode"


def test_coprocessor_lost_has_fixed_advice():
    s = logscan.scan("E (10978) transport: Not able to connect with ESP-Hosted slave device\n")
    assert s.conditions == ["coprocessor_lost"]
    assert "power cycle" in logscan.advice(s.conditions)[0]


FUSES = """
SOFT_DIS_JTAG (BLOCK0)                             Represents whether JTAG is dis = 0 R/W (0b000)
DIS_PAD_JTAG (BLOCK0)                              Represents whether JTAG is dis = False R/W (0b0)
DIS_USB_JTAG (BLOCK0)                              Represents whether the functio = False R/W (0b0)
SPI_BOOT_CRYPT_CNT (BLOCK0)                        Enables flash encryption when  = Disable R/W (0b000)
SECURE_BOOT_EN (BLOCK0)                            Represents whether secure boot = False R/W (0b0)
DIS_DOWNLOAD_MODE (BLOCK0)                         Represents whether Download mo = False R/W (0b0)
"""


def test_efuse_open_board():
    r = audit.audit_efuse(FUSES)
    assert {f["id"] for f in r["findings"]} == {
        "secure_boot_off", "flash_encryption_off", "jtag_pads_open", "usb_jtag_open", "download_mode_open"}
    assert "never" in r["note"]


def test_efuse_locked_board_and_unknown_not_reported_as_safe():
    locked = FUSES.replace("SECURE_BOOT_EN (BLOCK0)                            Represents whether secure boot = False R/- (0b1)", "")
    locked = locked.replace("= False R/W (0b0)", "= True R/- (0b1)").replace("= Disable R/W (0b000)", "= Enable R/- (0b001)")
    assert audit.audit_efuse(locked)["findings"] == []
    assert audit.audit_efuse("")["not_reported_by_chip"]


def test_image_scan_masks_secrets(tmp_path):
    p = tmp_path / "fw.bin"
    p.write_bytes(b"\x00\x01wifi_password=SuperSecret123\x00mqtt://bob:hunter22@broker.local\x00" + b"AKIA" + b"A" * 16 + b"\xff")
    r = audit.scan_image(p)
    kinds = {f["kind"] for f in r["findings"]}
    assert {"url_with_password", "aws_access_key", "assigned_password"} <= kinds
    blob = str(r)
    assert "SuperSecret123" not in blob and "hunter22" not in blob


def test_image_scan_clean(tmp_path):
    p = tmp_path / "fw.bin"
    p.write_bytes(b"\x00hello world\x00%s password: %d\x00")
    assert audit.scan_image(p)["findings"] == []


def test_sdkconfig():
    r = audit.audit_sdkconfig("CONFIG_ESP_TLS_SKIP_SERVER_CERT_VERIFY=y\n# CONFIG_LOG_DEFAULT_LEVEL_DEBUG is not set\n")
    assert [f["config"] for f in r["findings"]] == ["CONFIG_ESP_TLS_SKIP_SERVER_CERT_VERIFY"]


def test_decode_name_dwarf4_dir_index_is_one_based():
    from types import SimpleNamespace as NS
    from decode import Decoder
    files = [NS(name=b"crash_demo.c", dir_index=1), NS(name=b"local.c", dir_index=0)]
    dirs = [b"/proj/main", b"/toolchain/include"]
    assert Decoder._name(files, dirs, 0, 4) == "/proj/main/crash_demo.c"
    assert Decoder._name(files, dirs, 1, 4) == "local.c"


def test_decode_name_dwarf5_dir_index_is_zero_based():
    from types import SimpleNamespace as NS
    from decode import Decoder
    files = [NS(name=b"a.c", dir_index=1)]
    assert Decoder._name(files, [b"/cwd", b"/src"], 0, 5) == "/src/a.c"
