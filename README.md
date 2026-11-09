# numogram

**Hermes debugs your ESP32.**

Your chip crashes and screams numbers. numogram maps them to the line of code that
broke, and Hermes takes it from there: fix, build, flash, watch it run. It asks you
before it builds or flashes anything.

![Hermes finds a crash on a real ESP32-P4, fixes it, and flashes the fix](https://github.com/user-attachments/assets/d8caed4d-b43b-4260-9918-03ba14e14ad2)

*A real ESP32-P4 board, real Hermes, Claude Sonnet. Sped up 4x. Nothing staged.*

## The 30-second version

An **ESP32** is a cheap chip that runs a lot of gadgets: sensors, smart plugs,
robots, student projects. The program on it is called **firmware**. When firmware
crashes, the chip prints a wall of numbers over the USB cable and restarts, often
forever.

Normally you'd copy those numbers, look up which line of code they point to, guess
the bug, edit, rebuild, re-flash, and watch again. numogram gives Hermes the hands to
do that loop itself:

**💥 crash → read it over USB → find the exact line → fix the code → ✋ you approve → build and flash → watch it run** (and around again if it still crashes)

## What it caught

Same bug as in the GIF, from the example project you can run yourself. The board
printed this (trimmed) and rebooted every 3 seconds:

```
Guru Meditation Error: Core 0 panic'ed (Load access fault). Exception was unhandled.
MEPC    : 0x420052fc  RA      : 0x420053c4  MCAUSE  : 0x00000005  MTVAL   : 0x00000004
```

numogram turned the addresses into source lines:

```
channel_value()  main/sensor_hub.c:38   ← crashed here, reading through a NULL pointer
report()         main/sensor_hub.c:52   ← called from here
```

Hermes read the code and found a one-letter typo. The table said `"humidty"`, the
lookup asked for `"humidity"`, so the lookup came back empty and the next line used
it anyway:

```diff
 static channel_t channels[] = {
     {"temperature", 0.01f, 2315},
-    {"humidty", 0.1f, 412},
+    {"humidity", 0.1f, 412},
     {"pressure", 0.1f, 10132},
 };
```

Then it built (you approve), flashed (you approve), and read the serial output again:
temperature, humidity and pressure, every 2 seconds, no reboot.

## Try it now, no board needed

numogram can run a **virtual ESP32**: an emulated ESP32-C3 in Espressif's own QEMU. It
boots real firmware, crashes for real, and gets flashed for real. You need Docker.

```sh
docker build -f docker/Dockerfile -t numogram-demo .
docker run -it -v numogram-demo:/opt/data numogram-demo
```

The first build takes a while (it installs ESP-IDF). Sign in to a model when Hermes
asks, then type:

> Flash /opt/data/projects/sensor-hub to the board and watch it. If it crashes, find out why and fix it.

You'll approve four times (build, flash, build, flash) and watch the same bug get
found and fixed. Then try the schematic check:

> Check the pins in /opt/data/projects/sensor-hub against hardware/sensor-hub.net.

It finds that the firmware swapped the two I2C wires, and that the buzzer is set to
a pin the board uses for USB. (The example netlist was written by hand in KiCad 8's
format for this demo.)

## Use it with your own board

```sh
hermes plugins install zblauser/numogram-hermes-plugin --enable
```

Once it's in the Hermes catalog, `hermes plugins install numogram` works too.

Tell numogram where the board is with `NUMOGRAM_PORT`, set where Hermes runs:

| Your setup | `NUMOGRAM_PORT` |
|---|---|
| Linux, or Docker with the USB device passed through | the serial device, like `/dev/ttyACM0` |
| Docker on a Mac (containers can't see USB) | `socket://host.docker.internal:4000`, and run the bridge below on the Mac |
| No board | `virtual` |

The bridge shares the board's USB port with Docker, on your machine only:

```sh
python bridge/bridge.py /dev/cu.usbmodem14701
```

It works with boards that use the chip's own USB port (C3, C6, S3, P4 and others),
not yet with boards that have a separate USB-to-serial chip.

For crash alerts to wake Hermes on their own, allow it once (the demo image does this
for you):

```sh
hermes config set plugins.entries.numogram.allow_gateway_injection true
```

To let Hermes build, install ESP-IDF where Hermes runs and set `IDF_PATH`. Without it,
build yourself and ask Hermes to flash the build folder.

### Things to say to Hermes

- "My board keeps rebooting. Figure out why."
- "Watch the board and tell me if it crashes." Then go do something else.
- "Build and flash ~/projects/blinky, then check the log."
- "Check my pins against board.net before I order the PCBs."
- "Audit this board's security settings. Don't change anything."
- "Remember that this board needs the BOOT button held to flash."

## Why this is a plugin and not a skill

A skill is instructions: text that tells the model how to behave. numogram is code
that runs inside Hermes and does things text can't.

| What numogram does | Why instructions alone can't |
|---|---|
| Keeps the USB serial connection open and reads every byte, for hours | A skill only acts while the model is answering. It can't hold a port open in the background. |
| **Wakes Hermes up when the board crashes**, even if you haven't typed anything | A skill waits for you to talk. numogram's watcher thread notices the crash and starts the conversation. |
| Turns crash addresses into file and line, exactly | A model reading raw hex would guess. numogram reads the debug tables inside the ELF file. |
| **Asks before every build and flash, enforced in code** | "Please ask first" in a prompt is a request the model can skip. numogram's hook makes Hermes stop and ask you, and refuses if nobody answers. |
| **Blocks eFuse burns byte by byte** (permanent, can brick a chip) | A prompt can't inspect serial traffic. The bridge and virtual board drop eFuse write commands before they reach the chip. |
| Runs a whole emulated ESP32 | Starting and managing a QEMU process is a program's job. |
| Runs only fixed commands | A skill would have the model type shell commands. numogram's tools never turn model text into a command line. |

## All the tools

| Tool | What it does |
|---|---|
| `board_status` | Which chip, which revision, how much flash, plus notes you saved about this board |
| `board_watch` | Start or stop watching the serial port. A new crash wakes Hermes |
| `board_log` | Recent serial output, plus a summary: running, crashing, rebooting |
| `board_crash` | The latest crash, decoded to function, file and line |
| `board_build` | Build the project, with errors as file and line. Asks you first |
| `board_flash` | Put a build on the board, verify it, restart it. Asks you first |
| `board_pins` | Compare the firmware's pins with the KiCad schematic |
| `board_audit` | Security report: secure boot, flash encryption, debug ports, leaked secrets. Read-only |
| `board_note` | Remember facts about one specific board |

## What it's been tested on

| | |
|---|---|
| ESP32-P4, real board, USB | ✅ Full loop: crash, fix, build, flash, verify |
| ESP32-C3, virtual board | ✅ Full loop |
| Other RISC-V chips (C2, C5, C6, C61, H2) | Same paths, not tried on hardware yet |
| Older Xtensa chips (ESP32, S2, S3) | Flashing should work; crash reading is untested |
| Schematic pin check | Pin rules for 11 chips, from Espressif's ESP-IDF docs |

![The ESP32-P4 board from the demo, plugged in over USB-C](https://github.com/user-attachments/assets/ad8cc5ab-df89-45d3-94ad-0cf43870d64b)

*The board from the demo: a Wireless-Tag WT01P4C5-S1 (ESP32-P4), one USB-C cable.*

One board at a time. Building and flashing work with ESP-IDF projects. Crash decoding
needs only the ELF file.

## What it does on your computer

Read this before installing.

- **It asks first.** Builds and flashes go through Hermes's approval prompt. A build
  runs the project's own build scripts, which Hermes may have just edited, so it gets
  the same gate as a flash. With nobody there to answer (scheduled jobs, `-q`, an
  unattended gateway), Hermes blocks them. Turning approvals off (`--yolo` or
  `approvals.mode: off`) is your choice. numogram never approves anything itself.
- **Programs it runs**, always with fixed arguments:
  - `esptool`, to identify, flash and restart the board
  - `espefuse summary`, read-only, for the audit. The plugin has no eFuse write.
  - `idf.py build`, which runs the project's own build scripts
  - Espressif's `qemu-system-riscv32`, only for the virtual board, with networking off

  Each one gets a short allowlist of environment variables (`PATH`, `HOME`, locale,
  ESP-IDF's own), never your Hermes API keys or tokens. A build runs the project's own
  scripts, so this matters.
- **Starts conversations:** a crash alert is sent into your Hermes session as a message.
  That needs `allow_gateway_injection: true` (above), which is off until you set it.
- **Runs in the background:** a serial watcher thread while watching is on, and a QEMU
  process for the virtual board, which stops when Hermes exits.
- **Network:** none from the plugin, except connections on your own machine to the
  bridge or the virtual board. Installing downloads `esptool`, `pyserial` and
  `pyelftools`. Building the demo image downloads ESP-IDF.
- **Files:** reads the project, build folder and netlist you point it at. Writes only
  under the active Hermes profile's home, in `numogram/` (board notes, last flash,
  virtual board).
- **Board output is treated as untrusted.** A device could print anything, so serial
  text is labeled as data for the model. Crash alerts are built from fixed text and
  checked numbers, so firmware can't slip instructions into them.
- **eFuses:** burning one is permanent, and numogram can't do it. The bridge and the
  virtual board also drop eFuse write commands from any program and hang up. That
  stops accidents. It doesn't stop firmware already on the chip, or another program
  that opens the port directly.
- **Security audit:** reports and suggests, changes nothing. For boards you own.

## How it works inside

- **Watcher:** a thread reads the serial port and spots panics, failed asserts,
  reboots and blank flash. It waits for a crash report to finish printing before
  reading it, and holds the wake-up until Hermes finishes what it's doing, so an
  alert never shows up after the fix it's about.
- **Decoder:** reads the ELF's DWARF line tables with `pyelftools`. No compiler
  toolchain needed.
- **Bridge and virtual board:** both talk the same simple protocol, raw serial on
  one port and reset commands on another, so the rest of numogram can't tell them apart.
- **Approvals:** a `pre_tool_call` hook asks Hermes to get your OK for every build and
  flash, including ones the model reaches through `tool_search`.

## Tests

```sh
cd tests && python -m pytest
```

117 tests, no board needed. The virtual board tests use a fake QEMU, and the eFuse
guard is tested against espefuse's own virtual eFuse chip for 14 chip models.
