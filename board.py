"""Talk to one board: serial watcher plus a fixed set of esptool operations.

NUMOGRAM_PORT is a serial path (/dev/ttyACM0, COM3), socket://host:port for the
numogram bridge (Docker on a Mac can't see USB), or "virtual" for an emulated
ESP32-C3 (vboard.py, started here on first use). Commands here are fixed lists.
Nothing passes model text to esptool, and no eFuse write operation exists.
"""
from __future__ import annotations

import atexit
import collections
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import serial

from . import logscan
from .childenv import minimal_env

TIMEOUT_S = 300
_FLASH_FLAG = re.compile(r"^--flash[_-](mode|freq|size)$")
_OFFSET = re.compile(r"^0x[0-9a-fA-F]+$")


class BoardError(RuntimeError):
    pass


VIRTUAL_PORT = 4410  # data; control is the next port
_vboard: subprocess.Popen | None = None
_vboard_lock = threading.Lock()


def data_dir() -> Path:
    """numogram's state, under the active Hermes profile's home."""
    from hermes_constants import get_hermes_home
    d = Path(get_hermes_home()) / "numogram"
    d.mkdir(parents=True, exist_ok=True)
    return d


def port() -> str:
    p = os.environ.get("NUMOGRAM_PORT", "").strip()
    if not p:
        raise BoardError('NUMOGRAM_PORT is not set. Use a serial path, socket://host:port with the numogram bridge, or "virtual".')
    if p == "virtual":
        return _virtual()
    return p


def is_virtual() -> bool:
    return os.environ.get("NUMOGRAM_PORT", "").strip() == "virtual"


def _vboard_status(data_port: int) -> str | None:
    try:
        with socket.create_connection(("127.0.0.1", data_port + 1), 1) as s:
            s.sendall(b"status")
            return s.recv(256).decode(errors="replace")
    except OSError:
        return None


def _virtual() -> str:
    """Start the emulated board once per process, or reuse one already running."""
    global _vboard
    dp = int(os.environ.get("NUMOGRAM_VIRTUAL_PORT", VIRTUAL_PORT))
    url = f"socket://127.0.0.1:{dp}"
    with _vboard_lock:
        if _vboard is not None and _vboard.poll() is None:
            return url
        st = _vboard_status(dp)
        if st is not None:
            if "virtual-esp32c3" in st:
                return url
            raise BoardError(f"Port {dp} is taken by something that isn't the numogram virtual board. Set NUMOGRAM_VIRTUAL_PORT.")
        from . import vboard
        if not vboard.find_qemu():
            raise BoardError(vboard.NO_QEMU)
        work = data_dir() / "vboard"
        work.mkdir(parents=True, exist_ok=True)
        log = work / "vboard.log"
        cmd = [sys.executable, "-I", vboard.__file__, "--data-port", str(dp), "--control-port", str(dp + 1),
               "--workdir", str(work)]
        with open(log, "w") as out:
            _vboard = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                       start_new_session=True,
                                       env=minimal_env("NUMOGRAM_QEMU", "IDF_TOOLS_PATH", "IDF_PATH"))
        deadline = time.time() + 15
        while _vboard_status(dp) is None:
            if _vboard.poll() is not None:
                err = log.read_text(errors="replace")[-400:]
                _vboard = None
                raise BoardError(f"The virtual board didn't start: {err}")
            if time.time() > deadline:
                raise BoardError("The virtual board didn't start within 15 s.")
            time.sleep(0.1)
        atexit.register(_stop_virtual)
        return url


def _stop_virtual() -> None:
    if _vboard is not None and _vboard.poll() is None:
        _vboard.terminate()


def _is_bridge(p: str) -> bool:
    return p.startswith("socket://")  # includes the virtual board, which speaks the bridge protocol


def _control(cmd: str) -> str:
    u = urlparse(port())
    ctl = int(os.environ.get("NUMOGRAM_CONTROL_PORT", (u.port or 4000) + 1))
    try:
        with socket.create_connection((u.hostname, ctl), 5) as s:
            s.sendall(cmd.encode())
            return s.recv(256).decode(errors="replace").strip()
    except OSError as e:
        raise BoardError(f"Can't reach the numogram bridge at {u.hostname}:{ctl}: {e}") from e


class Watcher:
    """Owns the serial link. Tools that need the link pause this first."""

    def __init__(self, on_event=None, on_tick=None, max_chars: int = 400_000):
        self.buf: collections.deque[str] = collections.deque()
        self.size = 0
        self.max_chars = max_chars
        self.on_event = on_event
        self.on_tick = on_tick
        self.lock = threading.Lock()
        self.enabled = threading.Event()
        self.paused = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.seen: set[str] = set()
        self.crash_counts: collections.Counter[str] = collections.Counter()
        self._scanned = 0
        self._thread: threading.Thread | None = None
        self.since: float | None = None  # when the current buffer started filling
        self._open_since: float | None = None  # when an unfinished crash dump was first seen

    def start(self) -> None:
        if not self.watching:
            self.since = time.time()
        self.enabled.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="numogram-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.enabled.clear()

    @property
    def watching(self) -> bool:
        return self.enabled.is_set() and self._thread is not None and self._thread.is_alive()

    def text(self) -> str:
        with self.lock:
            return "".join(self.buf)

    def clear(self) -> None:
        with self.lock:
            self.buf.clear()
            self.size = 0
            self._scanned = 0
            self.since = time.time()

    def _append(self, s: str) -> None:
        with self.lock:
            self.buf.append(s)
            self.size += len(s)
            while self.size > self.max_chars and len(self.buf) > 1:
                gone = self.buf.popleft()
                self.size -= len(gone)
                self._scanned = max(0, self._scanned - len(gone))

    def _run(self) -> None:
        link = None
        last_scan = 0.0
        while self.enabled.is_set():
            if self.paused.is_set():
                if link is not None:
                    link.close()
                    link = None
                self.idle.set()
                time.sleep(0.05)
                continue
            self.idle.clear()
            try:
                if link is None:
                    link = serial.serial_for_url(port(), timeout=0.2)
                data = link.read(4096)
                if data:
                    self._append(data.decode("utf-8", "replace"))
            except (OSError, serial.SerialException, BoardError):
                if link is not None:
                    try:
                        link.close()
                    except OSError:
                        pass
                link = None
                self.idle.set()
                time.sleep(1)
            if time.time() - last_scan > 0.5:
                last_scan = time.time()
                self._scan()
                if self.on_tick:
                    self.on_tick()
        if link is not None:
            link.close()
        self.idle.set()

    def _scan(self) -> None:
        with self.lock:
            new = "".join(self.buf)[self._scanned:]
            # Hold back the last partial region so a dump is read once, whole.
            keep = new.rfind("\n", 0, max(0, len(new) - 3000))
            if keep < 0:
                return
            chunk = new[: keep + 1]
            # Never split a crash dump: a header without its registers reads as a different crash.
            # A dump that never finishes (panic set to halt) is read whole after a few seconds.
            s = logscan.open_dump_start(chunk)
            if s is not None:
                if self._open_since is None:
                    self._open_since = time.time()
                if time.time() - self._open_since < 5:
                    chunk = chunk[: chunk.rfind("\n", 0, s) + 1]
                    if not chunk:
                        return
            else:
                self._open_since = None
            self._scanned += len(chunk)
        res = logscan.scan(chunk)
        for c in res.crashes:
            self.crash_counts[c.signature] += 1
            if c.signature not in self.seen:
                self.seen.add(c.signature)
                self._emit({"type": "crash", "kind": c.kind, "registers": c.registers, "signature": c.signature})
        for cond in res.conditions:
            if cond not in self.seen:
                self.seen.add(cond)
                self._emit({"type": "condition", "condition": cond})

    def _emit(self, ev: dict) -> None:
        if self.on_event:
            self.on_event(ev)

    class _Hold:
        def __init__(self, w):
            self.w = w

        def __enter__(self):
            self.w.paused.set()
            if self.w.watching:
                self.w.idle.wait(3)
            return self.w

        def __exit__(self, *exc):
            self.w.paused.clear()

    def hold(self):
        """Release the serial link for the length of a with block."""
        return Watcher._Hold(self)


def _dep_path() -> str:
    """The folders this process loaded esptool and pyserial from, so the child Python finds
    them too. Plugin dependencies live outside Hermes's own venv."""
    import importlib.util
    dirs: list[str] = []
    for m in ("esptool", "serial"):
        spec = importlib.util.find_spec(m)
        if spec and spec.origin:
            d = str(Path(spec.origin).parents[1])
            if d not in dirs:
                dirs.append(d)
    return os.pathsep.join(dirs)


def _esptool(args: list[str], *, module: str = "esptool", cwd: str | None = None, timeout: int = TIMEOUT_S):
    p = port()
    before = ["--before", "no-reset"] if _is_bridge(p) else []
    cmd = [sys.executable, "-m", module, "--port", p, *before, *args]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**minimal_env(), "PYTHONPATH": _dep_path()})
    except subprocess.TimeoutExpired as e:
        raise BoardError(f"{module} timed out after {timeout}s") from e
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _download() -> None:
    if _is_bridge(port()):
        reply = _control("download")
        if not reply.startswith("ok"):
            raise BoardError(f"Bridge couldn't reset the board: {reply}")
        time.sleep(0.3)


def _loader(args: list[str], *, module: str = "esptool", cwd: str | None = None, timeout: int = TIMEOUT_S):
    """Reset into the ROM loader and run esptool. A board mid-crash can miss the first
    reset, so try once more when esptool couldn't connect at all."""
    for attempt in (1, 2):
        _download()
        rc, out = _esptool(args, module=module, cwd=cwd, timeout=timeout)
        if "Failed to connect" not in out or attempt == 2:
            return rc, out
        time.sleep(1.0)
    return rc, out


_INFO = {
    "chip": re.compile(r"Chip type:\s*(.+)"),
    "mac": re.compile(r"MAC:\s*([0-9a-fA-F:]{17})"),
    "flash_size": re.compile(r"Detected flash size:\s*(\S+)"),
    "usb_mode": re.compile(r"USB mode:\s*(.+)"),
    "features": re.compile(r"Features:\s*(.+)"),
}


def info() -> dict:
    rc, out = _loader(["--after", "watchdog-reset", "flash-id"], timeout=60)
    res = {k: (m.group(1).strip() if (m := rx.search(out)) else None) for k, rx in _INFO.items()}
    if is_virtual():
        res["virtual"] = "Emulated ESP32-C3 in QEMU, no hardware. MAC is all zeros."
    if not res["chip"]:
        raise BoardError("No answer from the board after two tries. Check the cable and that the USB plug is fully seated, then try again.\n" + out[-400:])
    return res


def efuse_summary() -> str:
    """Read-only: `summary` is the only espefuse operation this plugin can run."""
    rc, out = _loader(["summary"], module="espefuse", timeout=60)
    if "BLOCK0" not in out:
        raise BoardError("Couldn't read eFuses.\n" + out[-400:])
    _download()
    _esptool(["--after", "watchdog-reset", "flash-id"], timeout=60)  # back to running firmware
    return out


def parse_flash_args(build_dir: Path) -> list[str]:
    """Accept only flash settings and offset/path pairs inside the build dir."""
    f = build_dir / "flash_args"
    if not f.is_file():
        raise BoardError(f"No flash_args in {build_dir}. Point at an ESP-IDF build directory.")
    toks = shlex.split(f.read_text())
    out: list[str] = []
    root = build_dir.resolve()
    i = 0
    while i < len(toks):
        t = toks[i]
        if _FLASH_FLAG.match(t) and i + 1 < len(toks):
            out += [t.replace("_", "-"), toks[i + 1]]
        elif _OFFSET.match(t) and i + 1 < len(toks):
            target = (build_dir / toks[i + 1]).resolve()
            if root not in target.parents or not target.is_file():
                raise BoardError(f"flash_args names a file outside the build dir: {toks[i + 1]}")
            out += [t, str(target)]
        else:
            raise BoardError(f"Unexpected entry in flash_args: {t}")
        i += 2
    return out


def flash(build_dir: str) -> dict:
    bd = Path(build_dir).expanduser().resolve()
    args = parse_flash_args(bd)
    images = sum(1 for a in args if _OFFSET.match(a))
    start = time.time()
    rc, out = _loader(["--after", "watchdog-reset", "write-flash", *args], cwd=str(bd))
    verified = out.count("Hash of data verified")
    # Over the bridge esptool can exit non-zero after a good flash, when boot text
    # lands in its last read, so judge by what was verified.
    ok = verified == images and images > 0
    res = {"ok": ok, "images": images, "verified": verified, "seconds": round(time.time() - start, 1)}
    if not ok:
        res["esptool_tail"] = out[-600:]
    return res
