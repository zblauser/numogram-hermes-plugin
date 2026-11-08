import shutil
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
import pins  # noqa: E402

EXAMPLE = PLUGIN / "examples" / "sensor-hub"
NET = EXAMPLE / "hardware" / "sensor-hub.net"


def _issues(res):
    return {(f["gpio"], f["issue"]) for f in res["findings"]}


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "proj"
    shutil.copytree(EXAMPLE, d)
    return d


def test_example_board_findings(proj):
    res = pins.run(NET, proj)
    assert res["chip"] == "esp32c3" and res["firmware_target"] == "esp32c3"
    got = _issues(res)
    assert {(4, "role mismatch"), (5, "role mismatch"), (18, "role mismatch"),
            (18, "USB-Serial/JTAG pin"), (8, "strapping pin"), (9, "strapping pin"),
            (10, "wired but unused")} == got
    sda = next(f for f in res["findings"] if f["gpio"] == 4)
    assert "GPIO5" in sda["why"] and sda["firmware"] == "I2C_SDA_GPIO at main/board_pins.h:4"
    assert [f["severity"] for f in res["findings"]] == sorted(
        (f["severity"] for f in res["findings"]), key=pins._ORDER.get)


def test_fixed_pins_leave_only_strapping_info(proj):
    h = proj / "main" / "board_pins.h"
    h.write_text(h.read_text().replace("SDA_GPIO    4", "SDA_GPIO    5")
                 .replace("SCL_GPIO    5", "SCL_GPIO    4").replace("BUZZER_GPIO     18", "BUZZER_GPIO     10"))
    res = pins.run(NET, proj)
    assert {f["severity"] for f in res["findings"]} == {"info"}
    assert _issues(res) == {(8, "strapping pin"), (9, "strapping pin")}


def test_wrong_chip(proj):
    (proj / "sdkconfig.defaults").write_text('CONFIG_IDF_TARGET="esp32p4"\n')
    res = pins.run(NET, proj)
    assert res["findings"][0]["issue"] == "wrong chip"


def test_not_a_pin_names_ignored(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "x.c").write_text(
        "#define LED_BRIGHTNESS 50\n#define UART_TX_BUF 64\n#define I2C_PORT 0\n#define SPI_CS_COUNT 2\n"
        "#define LED_GPIO 7\n#define PIN_SDA (GPIO_NUM_6)\nstatic const gpio_num_t BTN = GPIO_NUM_3;\n"
        "#define I2C_SDA 4 // data\n")
    got = {(u.gpio, u.name) for u in pins.firmware_uses(tmp_path)}
    assert got == {(7, "LED_GPIO"), (6, "PIN_SDA"), (4, "I2C_SDA")}


def test_struct_fields_and_kconfig(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "x.c").write_text(
        "i2c_master_bus_config_t c = { .sda_io_num = GPIO_NUM_1, .scl_io_num = 2 };\n"
        "spi_bus_config_t b = {.mosi_io_num = 11, .sclk_io_num=12};\n")
    (tmp_path / "sdkconfig").write_text("CONFIG_EXAMPLE_LED_GPIO=13\nCONFIG_FREERTOS_HZ=100\n")
    got = {(u.gpio, u.name) for u in pins.firmware_uses(tmp_path)}
    assert got == {(1, "sda_io_num"), (2, "scl_io_num"), (11, "mosi_io_num"), (12, "sclk_io_num"),
                   (13, "CONFIG_EXAMPLE_LED_GPIO")}


def test_build_and_managed_components_skipped(tmp_path):
    for d in ("build", "managed_components/x"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "a.h").write_text("#define LED_GPIO 7\n")
    assert pins.firmware_uses(tmp_path) == []


def _net(chip_value, nets):
    body = "".join(
        f'(net (code "{i}") (name "{name}") (node (ref "U1") (pin "{i}") (pinfunction "{fn}"))'
        + "".join(f'(node (ref "{r}") (pin "1"))' for r in others) + ")"
        for i, (name, fn, others) in enumerate(nets, 1))
    return (f'(export (version "E") (components (comp (ref "U1") (value "{chip_value}")) '
            f'(comp (ref "R1") (value "1k"))) (nets {body}))')


def test_classic_esp32_input_only_and_flash_and_aliases(tmp_path):
    net = _net("ESP32-WROOM-32E", [("/LED", "SENSOR_VP", ["R1"]), ("/X", "SD0", ["R1"]),
                                   ("/TX", "TXD0", ["R1"])])
    b = pins.parse_netlist(net)
    assert b.chip == "esp32" and set(b.gpio_nets) == {36, 7, 1}
    uses = [pins.Use(36, "LED_GPIO", "m.c", 1)]
    got = _issues(pins.check(b, uses))
    assert (36, "input-only pin driven") in got and (7, "flash pin wired") in got


def test_unconnected_and_bare_nets_skipped():
    net = _net("ESP32-C3-MINI-1", [("unconnected-(U1-IO2-Pad5)", "IO2", []), ("Net-(U1-IO3)", "IO3", [])])
    assert pins.parse_netlist(net).gpio_nets == {}


def test_two_roles_one_pin_and_no_such_gpio():
    b = pins.parse_netlist(_net("ESP32-C3-MINI-1", []))
    got = _issues(pins.check(b, [pins.Use(3, "LED_GPIO", "a", 1), pins.Use(3, "BUTTON_PIN", "a", 2),
                                 pins.Use(40, "CS_PIN", "a", 3)]))
    assert (3, "two roles on one pin") in got and (40, "no such GPIO") in got


def test_no_esp_part_and_bad_input():
    b = pins.parse_netlist('(export (version "E") (components (comp (ref "U1") (value "STM32F103"))) (nets))')
    assert b.ref is None and b.notes
    with pytest.raises(ValueError, match="Not a KiCad netlist"):
        pins.parse_netlist("(kicad_sch (version 1))")
    with pytest.raises(ValueError, match="unbalanced"):
        pins.parse_netlist("(export (nets")


def test_quoted_names_with_escapes():
    net = '(export (components (comp (ref "U1") (value "ESP32-S3-WROOM-1"))) (nets (net (code "1") (name "/A \\"B\\" SDA") (node (ref "U1") (pin "4") (pinfunction "IO4")) (node (ref "R1") (pin "1")))))'
    b = pins.parse_netlist(net)
    assert b.chip == "esp32s3" and b.gpio_nets[4][0] == '/A "B" SDA'


def test_every_chip_has_facts_in_the_same_shape():
    for chip, f in pins.CHIPS.items():
        assert set(f) == {"strapping", "flash", "usb_jtag", "input_only", "max", "aliases"}, chip
