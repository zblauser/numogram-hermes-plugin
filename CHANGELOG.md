# Changelog

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
