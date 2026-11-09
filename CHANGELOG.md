# Changelog

## [v0.1.2] - 2026-11-09

Polish.

- Tool schemas mark required arguments and describe every argument, so models get calls right the first time.
- A bad argument (`seconds: "soon"`) comes back as a plain error, not a traceback. `on: "false"` now means false.
- README: the tests section says what the tests prove, not a count that goes stale.

## [v0.1.1] - 2026-11-09

Catalog review fixes.

- Programs numogram starts (`idf.py`, `esptool`, the virtual board and QEMU) get an allowlist of
  environment variables, never Hermes's own. A build runs project scripts, so they can't see API keys.
- State lives under the active Hermes profile (`get_hermes_home() / "numogram"`), not a raw `HERMES_HOME` read.
- `vboard.py` run by hand now needs `--workdir`.

## [v0.1.0] - 2026-11-08

First release.

- Firmware loop for ESP32 boards: build, flash, watch serial, decode crashes to file and line.
- Crash alerts wake the Hermes session, held until the current turn ends.
- Builds and flashes go through Hermes's approval prompt.
- `board_pins`: firmware GPIO use checked against a KiCad netlist, pin rules for 11 chips.
- `board_audit`: read-only eFuse, sdkconfig and secrets report.
- Virtual board (`NUMOGRAM_PORT=virtual`): an emulated ESP32-C3 in Espressif's QEMU.
- macOS USB bridge for Hermes in Docker. Bridge and virtual board drop eFuse write commands.
- Demo Docker image with an example project that has a hidden bug.
