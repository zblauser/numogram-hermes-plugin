#!/usr/bin/env python3
"""A virtual ESP32-C3 for numogram, for people without a board.

Runs Espressif's QEMU and speaks the same two ports as bridge.py, so the plugin
can't tell the difference: set NUMOGRAM_PORT=socket://127.0.0.1:4000.

Data port: the emulated UART, raw bytes both ways.
Control port: "download" restarts the chip in its ROM loader, so esptool can
write the emulated flash. When that esptool client hangs up, the chip restarts
from flash, as a real board does after flashing. "status" reports the state.

Flash and eFuse images live in --workdir and survive restarts. eFuse burns are
dropped by the same guard as the real bridge, so the demo behaves like the board.
QEMU runs with no network (-nic none). Everything binds to loopback.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from .childenv import minimal_env
    from .guard import EfuseGuard
except ImportError:  # run as a script, possibly with python -I
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from childenv import minimal_env
    from guard import EfuseGuard

FLASH_BYTES = 4 << 20
# Default eFuse image from ESP-IDF's idf.py qemu: zeros except chip revision v0.3.
EFUSE_BYTES = 1024
EFUSE_REV_OFFSET, EFUSE_REV_VALUE = 38, 0x0C
STRAP_DOWNLOAD = "0x02"  # GPIO9 low at reset: ROM serial loader
DOWNLOAD_IDLE_S = 60  # back to firmware if nobody uses the loader
NO_QEMU = ("Can't find qemu-system-riscv32 for the virtual board. Install it with "
           "python $IDF_PATH/tools/idf_tools.py install qemu-riscv32, or set NUMOGRAM_QEMU to its path.")


def find_qemu() -> str | None:
    exe = os.environ.get("NUMOGRAM_QEMU") or shutil.which("qemu-system-riscv32")
    if exe:
        return exe
    roots = [os.environ.get("IDF_TOOLS_PATH", ""), os.path.expanduser("~/.espressif")]
    for r in filter(None, roots):
        hits = sorted(glob.glob(f"{r}/tools/qemu-riscv32/*/qemu/bin/qemu-system-riscv32"))
        if hits:
            return hits[-1]
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Chip:
    """One QEMU process at a time, restarted to change boot mode."""

    def __init__(self, qemu: str, workdir: Path, verbose: bool = False):
        self.qemu = qemu
        self.dir = workdir
        self.verbose = verbose
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.uart: socket.socket | None = None
        self.mode = "off"
        self.since = 0.0
        self.blocked = 0
        self.client: socket.socket | None = None
        self.used_loader = False
        workdir.mkdir(parents=True, exist_ok=True)
        self.flash = workdir / "flash.bin"
        self.efuse = workdir / "efuse.bin"
        if not self.flash.exists():
            self.flash.write_bytes(b"\xff" * FLASH_BYTES)
        if not self.efuse.exists():
            e = bytearray(EFUSE_BYTES)
            e[EFUSE_REV_OFFSET] = EFUSE_REV_VALUE
            self.efuse.write_bytes(bytes(e))

    def start(self, mode: str) -> None:
        with self.lock:
            self._stop()
            port = free_port()
            args = [
                self.qemu, "-nographic", "-machine", "esp32c3", "-nic", "none", "-monitor", "none",
                "-drive", f"file={self.flash},if=mtd,format=raw",
                "-drive", f"file={self.efuse},if=none,format=raw,id=efuse",
                "-global", "driver=nvram.esp32c3.efuse,property=drive,value=efuse",
                "-global", "driver=timer.esp32c3.timg,property=wdt_disable,value=true",
                # wait=on: the CPU starts only once we're attached, so no boot text is lost.
                "-serial", f"tcp:127.0.0.1:{port},server=on,wait=on",
            ]
            if mode == "download":
                args += ["-global", f"driver=esp32c3.gpio,property=strap_mode,value={STRAP_DOWNLOAD}"]
            out = None if self.verbose else subprocess.DEVNULL
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=minimal_env())
            deadline = time.time() + 10
            while True:
                try:
                    self.uart = socket.create_connection(("127.0.0.1", port), 1)
                    break
                except OSError:
                    if self.proc.poll() is not None or time.time() > deadline:
                        raise RuntimeError(f"QEMU didn't start (exit {self.proc.poll()})")
                    time.sleep(0.05)
            self.uart.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.uart.settimeout(None)  # the connect timeout must not apply to reads: flash writes go quiet for seconds
            self.mode = mode
            self.since = time.time()
            self.used_loader = False
            threading.Thread(target=self._pump, args=(self.uart,), daemon=True).start()
            print(f"vboard: chip started ({mode})", flush=True)

    def _stop(self) -> None:
        if self.uart is not None:
            try:
                self.uart.close()
            except OSError:
                pass
            self.uart = None
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc = None
        self.mode = "off"

    def stop(self) -> None:
        with self.lock:
            self._stop()

    def _pump(self, uart: socket.socket) -> None:
        """UART output goes to whoever is connected. With nobody there it's lost, like a real UART."""
        while True:
            try:
                d = uart.recv(4096)
            except OSError:
                return
            if not d:
                return
            c = self.client
            if c is not None:
                try:
                    c.sendall(d)
                except OSError:
                    pass

    def write(self, d: bytes) -> None:
        with self.lock:
            if self.uart is not None:
                if self.mode == "download":
                    self.used_loader = True
                self.uart.sendall(d)

    def watchdog(self) -> None:
        """A real board left in its loader stays there; here, go back to firmware after a minute idle."""
        while True:
            time.sleep(1)
            with self.lock:
                if self.mode == "download" and self.client is None and time.time() - self.since > DOWNLOAD_IDLE_S:
                    self.start("run")


def data_server(chip: Chip, port: int) -> None:
    srv = socket.create_server(("127.0.0.1", port))
    while True:
        c, _ = srv.accept()
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        old, chip.client = chip.client, c  # newest client wins, as with the real bridge
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        threading.Thread(target=serve_client, args=(chip, c), daemon=True).start()


def serve_client(chip: Chip, c: socket.socket) -> None:
    guard = EfuseGuard()
    why = "hung up"
    try:
        while True:
            d = c.recv(4096)
            if not d:
                break
            d, blocked = guard.feed(d)
            if blocked:
                chip.blocked += len(blocked)
                for why in blocked:
                    print(f"vboard: BLOCKED {why}", flush=True)
                break
            if d:
                chip.write(d)
    except OSError as e:
        why = f"link error: {e}"
    finally:
        print(f"vboard: client off ({why})", flush=True)
        try:
            c.close()
        except OSError:
            pass
        with chip.lock:
            if chip.client is c:
                chip.client = None
            # esptool is done with the loader: boot the new firmware.
            if chip.mode == "download" and chip.used_loader:
                chip.start("run")


def control_server(chip: Chip, port: int) -> None:
    srv = socket.create_server(("127.0.0.1", port))
    while True:
        c, _ = srv.accept()
        with c:
            cmd = c.recv(64).decode(errors="replace").strip()
            try:
                if cmd == "download":
                    chip.start("download")
                    reply = "ok"
                elif cmd == "status":
                    reply = f"ok virtual-esp32c3 mode={chip.mode} blocked={chip.blocked}"
                else:
                    reply = "err unknown command"
            except (OSError, RuntimeError) as e:
                reply = f"err {e}"
            c.sendall((reply + "\n").encode())


def _on_term(*_):
    raise KeyboardInterrupt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-port", type=int, default=4000)
    ap.add_argument("--control-port", type=int, default=4001)
    ap.add_argument("--workdir", type=Path, required=True, help="where the flash and eFuse images live")
    ap.add_argument("--erase", action="store_true", help="start with blank flash")
    ap.add_argument("--verbose", action="store_true", help="show QEMU's own output")
    a = ap.parse_args()
    if a.erase:
        (a.workdir / "flash.bin").unlink(missing_ok=True)
    qemu = find_qemu()
    if not qemu:
        sys.exit(NO_QEMU)
    chip = Chip(qemu, a.workdir, a.verbose)
    signal.signal(signal.SIGTERM, _on_term)  # so the finally below stops QEMU too
    chip.start("run")
    threading.Thread(target=control_server, args=(chip, a.control_port), daemon=True).start()
    threading.Thread(target=chip.watchdog, daemon=True).start()
    print(f"vboard: virtual ESP32-C3, data 127.0.0.1:{a.data_port} control 127.0.0.1:{a.control_port}, "
          f"images in {a.workdir}", flush=True)
    try:
        data_server(chip, a.data_port)
    except KeyboardInterrupt:
        pass
    finally:
        chip.stop()


if __name__ == "__main__":
    main()
