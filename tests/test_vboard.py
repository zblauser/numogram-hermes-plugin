"""vboard.py against a fake QEMU, so these run without Espressif's QEMU installed.

The fake listens where QEMU's UART would, says which boot mode it was started in,
and echoes what it receives, so a test can see what reached the "chip".
"""
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
from guard import WRITE_OP_CODE  # noqa: E402

FAKE_QEMU = r'''
import re, socket, sys
args = " ".join(sys.argv[1:])
port = int(re.search(r"-serial tcp:127\.0\.0\.1:(\d+),server=on,wait=on", args).group(1))
mode = "download" if "strap_mode,value=0x02" in args else "run"
assert "-nic none" in args
srv = socket.create_server(("127.0.0.1", port))
c, _ = srv.accept()
c.sendall(f"boot {mode}\n".encode())
while True:
    d = c.recv(4096)
    if not d:
        break
    c.sendall(b"echo:" + d)
'''


def _free_port_pair() -> int:
    for _ in range(50):
        with socket.socket() as a:
            a.bind(("127.0.0.1", 0))
            p = a.getsockname()[1]
        try:
            with socket.socket() as b:
                b.bind(("127.0.0.1", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture
def vb(tmp_path):
    fake = tmp_path / "qemu-system-riscv32"
    fake.write_text(f"#!{sys.executable}\n{FAKE_QEMU}")
    fake.chmod(0o755)
    dp = _free_port_pair()
    env = dict(os.environ, NUMOGRAM_QEMU=str(fake))
    p = subprocess.Popen([sys.executable, str(PLUGIN / "vboard.py"), "--data-port", str(dp),
                          "--control-port", str(dp + 1), "--workdir", str(tmp_path / "img")],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", dp + 1), 0.5).close()
            break
        except OSError:
            assert p.poll() is None, p.stdout.read().decode()
            assert time.time() < deadline
            time.sleep(0.05)
    yield dp, tmp_path / "img"
    p.terminate()
    p.wait(5)


def _ctl(dp, cmd):
    with socket.create_connection(("127.0.0.1", dp + 1), 5) as s:
        s.sendall(cmd.encode())
        return s.recv(256).decode()


def _read_until(c, want: bytes, timeout=5.0) -> bytes:
    c.settimeout(0.2)
    got, end = b"", time.time() + timeout
    while want not in got and time.time() < end:
        try:
            d = c.recv(4096)
            if not d:
                break
            got += d
        except TimeoutError:
            pass
    return got


def _slip(body: bytes) -> bytes:
    return b"\xc0" + body.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc") + b"\xc0"


def _write_reg(addr, value):
    data = struct.pack("<IIII", addr, value, 0xFFFFFFFF, 0)
    return _slip(struct.pack("<BBHI", 0, 0x09, len(data), 0) + data)


def test_images_created_with_chip_revision(vb):
    dp, img = vb
    assert (img / "flash.bin").stat().st_size == 4 << 20
    e = (img / "efuse.bin").read_bytes()
    assert len(e) == 1024 and e[38] == 0x0C and sum(e) == 0x0C


def test_status_names_the_virtual_board(vb):
    dp, _ = vb
    assert _ctl(dp, "status").startswith("ok virtual-esp32c3 mode=run blocked=0")


def test_download_then_hangup_boots_firmware_again(vb):
    dp, _ = vb
    assert _ctl(dp, "download").startswith("ok")
    with socket.create_connection(("127.0.0.1", dp)) as c:
        c.sendall(b"hello")
        got = _read_until(c, b"echo:hello")
        assert b"echo:hello" in got
    # esptool hung up after using the loader: the chip restarts from flash.
    deadline = time.time() + 5
    while "mode=run" not in _ctl(dp, "status"):
        assert time.time() < deadline
        time.sleep(0.05)
    with socket.create_connection(("127.0.0.1", dp)) as c:
        c.sendall(b"x")
        assert b"echo:x" in _read_until(c, b"echo:x")


def test_boot_text_reaches_client_connected_before_reset(vb):
    dp, _ = vb
    with socket.create_connection(("127.0.0.1", dp)) as c:
        time.sleep(0.2)
        _ctl(dp, "download")
        assert b"boot download" in _read_until(c, b"boot download")


def test_efuse_burn_is_dropped_before_the_chip(vb):
    dp, _ = vb
    _ctl(dp, "download")
    c = socket.create_connection(("127.0.0.1", dp))
    c.sendall(_write_reg(0x60008800 + 0x1CC, WRITE_OP_CODE))
    got = _read_until(c, b"never", timeout=1.5)
    c.close()
    assert b"echo:" not in got  # nothing reached the fake chip
    assert "blocked=1" in _ctl(dp, "status")


def test_read_op_passes(vb):
    dp, _ = vb
    _ctl(dp, "download")
    with socket.create_connection(("127.0.0.1", dp)) as c:
        frame = _write_reg(0x60008800 + 0x1CC, 0x5AA5)
        c.sendall(frame)
        assert b"echo:" in _read_until(c, b"echo:")
    assert "blocked=0" in _ctl(dp, "status")


def _board():
    if "numogram.board" not in sys.modules:
        import importlib.util
        spec = importlib.util.spec_from_file_location("numogram", PLUGIN / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["numogram"] = pkg
        spec.loader.exec_module(pkg)
    return sys.modules["numogram.board"]


def test_plugin_refuses_a_port_owned_by_something_else(monkeypatch):
    board = _board()
    dp = _free_port_pair()
    srv = socket.create_server(("127.0.0.1", dp + 1))
    import threading

    def answer():
        c, _ = srv.accept()
        c.recv(64)
        c.sendall(b"ok /dev/ttyACM0 blocked=0\n")
        c.close()

    threading.Thread(target=answer, daemon=True).start()
    monkeypatch.setenv("NUMOGRAM_PORT", "virtual")
    monkeypatch.setenv("NUMOGRAM_VIRTUAL_PORT", str(dp))
    monkeypatch.setattr(board, "_vboard", None)
    with pytest.raises(board.BoardError, match="isn't the numogram virtual board"):
        board.port()
    srv.close()


def test_plugin_says_how_to_get_qemu(monkeypatch):
    board = _board()
    import importlib
    vmod = importlib.import_module("numogram.vboard")
    monkeypatch.setenv("NUMOGRAM_PORT", "virtual")
    monkeypatch.setenv("NUMOGRAM_VIRTUAL_PORT", str(_free_port_pair()))
    monkeypatch.setattr(board, "_vboard", None)
    monkeypatch.setattr(vmod, "find_qemu", lambda: None)
    with pytest.raises(board.BoardError, match="idf_tools.py install qemu-riscv32"):
        board.port()
