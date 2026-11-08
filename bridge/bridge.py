#!/usr/bin/env python3
"""Share one local USB serial port with Docker, on loopback only.

Docker on a Mac can't see USB. Data port: raw bytes both ways, used as
socket://host.docker.internal:4000. Control port: reset commands, run here
because DTR/RTS timing does not survive a network hop.

Native USB Serial/JTAG only for now (baud is ignored on that link).
Control lines never follow the client, so connecting never resets the board.
eFuse burns are dropped before they reach the board; see guard.py.
"""
import argparse
import socket
import sys
import threading
import time
from pathlib import Path

import serial
from esp_pylib.serial_reset import usb_jtag_bootloader_reset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # guard.py lives at the repo root
from guard import EfuseGuard  # noqa: E402


class Link:
    def __init__(self, port: str):
        self.port = port
        self.lock = threading.Lock()
        self.ser = None
        self.blocked = 0

    def get(self):
        with self.lock:
            if self.ser is None:
                s = serial.Serial()
                s.port = self.port
                s.baudrate = 115200
                s.timeout = 0.05
                s.write_timeout = 1
                s.open()  # macOS raises DTR and RTS on open; that pair does not reset
                s.rts = False  # RTS first: dropping DTR alone would pulse reset
                s.dtr = False
                self.ser = s
            return self.ser

    def drop(self):
        with self.lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except OSError:
                    pass
                self.ser = None

    def enter_download(self):
        usb_jtag_bootloader_reset(self.get())


def data_server(link: Link, port: int) -> None:
    srv = socket.create_server(("127.0.0.1", port))
    while True:
        c, _ = srv.accept()
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c.settimeout(0.02)
        print("data: client on", flush=True)
        guard = EfuseGuard()
        try:
            while True:
                try:
                    d = c.recv(4096)
                    if not d:
                        break
                    d, blocked = guard.feed(d)
                    if blocked:
                        # Hang up instead of answering, so the tool fails loudly.
                        link.blocked += len(blocked)
                        for why in blocked:
                            print(f"data: BLOCKED {why}", flush=True)
                        break
                    if d:
                        link.get().write(d)
                except TimeoutError:
                    pass
                try:
                    out = link.get().read(4096)
                except (OSError, serial.SerialException):
                    link.drop()
                    time.sleep(0.2)
                    continue
                if out:
                    c.sendall(out)
        except OSError:
            pass
        finally:
            c.close()
            print("data: client off", flush=True)


def control_server(link: Link, port: int) -> None:
    srv = socket.create_server(("127.0.0.1", port))
    while True:
        c, _ = srv.accept()
        with c:
            cmd = c.recv(64).decode(errors="replace").strip()
            try:
                if cmd == "download":
                    link.enter_download()
                    reply = "ok"
                elif cmd == "status":
                    link.get()
                    reply = f"ok {link.port} blocked={link.blocked}"
                else:
                    reply = "err unknown command"
            except (OSError, serial.SerialException) as e:
                link.drop()
                reply = f"err {e}"
            c.sendall((reply + "\n").encode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--data-port", type=int, default=4000)
    ap.add_argument("--control-port", type=int, default=4001)
    a = ap.parse_args()
    link = Link(a.port)
    threading.Thread(target=control_server, args=(link, a.control_port), daemon=True).start()
    print(f"bridge: {a.port} data 127.0.0.1:{a.data_port} control 127.0.0.1:{a.control_port}", flush=True)
    data_server(link, a.data_port)


if __name__ == "__main__":
    main()
