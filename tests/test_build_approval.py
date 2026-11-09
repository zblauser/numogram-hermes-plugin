import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pkg():
    if "numogram" in sys.modules:
        return sys.modules["numogram"]
    spec = importlib.util.spec_from_file_location("numogram", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["numogram"] = mod
    spec.loader.exec_module(mod)
    return mod


GCC = """[3/9] Building C object esp-idf/main/CMakeFiles/__idf_main.dir/sensor_hub.c.obj
/work/sensor-hub/main/sensor_hub.c:51:9: error: 'chx' undeclared (first use in this function); did you mean 'ch'?
/work/sensor-hub/main/sensor_hub.c:51:9: error: 'chx' undeclared (first use in this function); did you mean 'ch'?
/work/sensor-hub/main/sensor_hub.c:40:12: warning: unused variable 'x' [-Wunused-variable]
ninja: build stopped: subcommand failed.
"""

SIZE = "sensor_hub.bin binary size 0x2a1b0 bytes. Smallest app partition is 0x100000 bytes. 0xd5e50 bytes (83%) free.\n"


def test_parse_errors_dedupe_and_count_warnings():
    r = _pkg().build_mod.parse(GCC)
    assert r["errors"] == [{"file": "/work/sensor-hub/main/sensor_hub.c", "line": 51, "message": "'chx' undeclared (first use in this function); did you mean 'ch'?"}]
    assert r["warnings"] == 1


def test_parse_size_line():
    s = _pkg().build_mod.parse(SIZE)["size"]
    assert s == {"bin": "sensor_hub.bin", "bytes": 0x2A1B0, "partition_bytes": 0x100000, "free_pct": 83}


def test_build_without_idf_says_how_to_fix(monkeypatch, tmp_path):
    m = _pkg().build_mod
    monkeypatch.delenv("IDF_PATH", raising=False)
    try:
        m.build(str(tmp_path))
    except m.BuildError as e:
        assert "IDF_PATH" in str(e)
    else:
        raise AssertionError("expected BuildError")


def test_project_info_reads_idf_description(tmp_path):
    (tmp_path / "app.elf").write_bytes(b"\x7fELF")
    (tmp_path / "project_description.json").write_text(json.dumps({"project_name": "sensor_hub", "target": "esp32p4", "app_elf": "app.elf"}))
    info = _pkg().build_mod.project_info(tmp_path)
    assert info == {"project": "sensor_hub", "target": "esp32p4", "elf": str(tmp_path / "app.elf")}


def test_flash_asks_for_approval_directly():
    r = _pkg()._approve(tool_name="board_flash", args={"build_dir": "/nope"})
    assert r["action"] == "approve" and "/nope" in r["message"] and r["rule_key"] == "numogram:board_flash"


def test_flash_through_tool_call_asks_too():
    f = _pkg()._approve
    calls = {"calls": [{"name": "board_log", "arguments": {}}, {"name": "board_flash", "arguments": json.dumps({"build_dir": "/b"})}]}
    assert f(tool_name="tool_call", args=calls)["action"] == "approve"


def test_build_asks_for_approval_with_its_own_rule():
    f = _pkg()._approve
    r = f(tool_name="board_build", args={"project_dir": "/p"})
    assert r["action"] == "approve" and "/p" in r["message"] and "CMake" in r["message"]
    assert r["rule_key"] == "numogram:board_build"
    calls = {"calls": [{"name": "board_build", "arguments": {"project_dir": "/q"}}]}
    assert f(tool_name="tool_call", args=calls)["rule_key"] == "numogram:board_build"


def test_build_and_flash_in_one_call_use_the_flash_rule():
    f = _pkg()._approve
    calls = {"calls": [{"name": "board_build", "arguments": {"project_dir": "/q"}},
                       {"name": "board_flash", "arguments": {"build_dir": "/q/build"}}]}
    r = f(tool_name="tool_call", args=calls)
    assert r["rule_key"] == "numogram:board_flash" and "/q/build" in r["message"] and "build /q" in r["message"]


def test_other_tools_pass_without_a_prompt():
    f = _pkg()._approve
    assert f(tool_name="board_crash", args={}) is None
    assert f(tool_name="tool_call", args={"calls": [{"name": "board_log", "arguments": {}}]}) is None
    assert f(tool_name="terminal", args={"command": "ls"}) is None


DUMP_HEAD = "I (5) sensor_hub: temperature = 23.16\nGuru Meditation Error: Core  0 panic'ed (Load access fault). Exception was unhandled.\n\n"
DUMP_REST = (
    "Core  0 register dump:\n"
    "MEPC    : 0x400075e6  RA      : 0x4000769e  SP      : 0x4ff1dc90  GP      : 0x4ff11a80\n"
    "MSTATUS : 0x00011880  MTVEC   : 0x4ff00003  MCAUSE  : 0x00000005  MTVAL   : 0x00000008\n"
    "ELF file SHA256: 316706f22\n\nRebooting...\n"
)


def test_watcher_does_not_split_a_dump(monkeypatch):
    pkg = _pkg()
    events = []
    w = pkg.board.Watcher(on_event=events.append)
    # The scan holds back the last 3000 chars. This puts that cut between the
    # panic header and its registers, as happened on the real board.
    w._append(DUMP_HEAD + DUMP_REST + "y" * (3000 - len(DUMP_REST)) + "\n")
    w._scan()
    assert events == []
    w._append("z" * 3100 + "\n")
    w._scan()
    assert len(events) == 1 and events[0]["registers"]["MEPC"] == "0x400075e6"


def test_watcher_reads_a_dump_that_never_finishes(monkeypatch):
    pkg = _pkg()
    events = []
    w = pkg.board.Watcher(on_event=events.append)
    w._append(DUMP_HEAD + DUMP_REST.split("ELF")[0] + "x" * 3100 + "\n")
    w._scan()
    assert events == []
    w._open_since -= 6
    w._scan()
    assert len(events) == 1


def test_flash_drops_alerts_about_old_firmware(monkeypatch):
    pkg = _pkg()
    pkg._pending[:] = [(0, "old alert")]
    monkeypatch.setattr(pkg.board, "flash", lambda bd: {"ok": True, "images": 3, "verified": 3, "seconds": 1})
    monkeypatch.setattr(pkg, "_home", lambda: __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    monkeypatch.setattr(pkg.watcher, "start", lambda: None)
    pkg.board_flash({"build_dir": "/b"})
    assert pkg._pending == []


class FakeCtx:
    def __init__(self):
        self.sent = []

    def inject_message(self, text):
        self.sent.append(text)
        return True


def _crash_event(pc="0x400075e6"):
    return {"type": "crash", "kind": "panic", "registers": {"MEPC": pc}, "signature": f"panic:Load access fault:{pc}"}


def _fresh(monkeypatch):
    pkg = _pkg()
    ctx = FakeCtx()
    monkeypatch.setattr(pkg, "_ctx", ctx)
    monkeypatch.setattr(pkg, "_session", None)
    monkeypatch.setattr(pkg, "_turn_started", None)
    pkg._pending.clear()
    return pkg, ctx


def test_alert_goes_out_when_agent_is_idle(monkeypatch):
    pkg, ctx = _fresh(monkeypatch)
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    pkg._on_event(_crash_event())
    assert len(ctx.sent) == 1 and "0x400075e6" in ctx.sent[0]


def test_alert_waits_while_agent_works_then_drops_if_agent_read_it(monkeypatch):
    import time as t
    pkg, ctx = _fresh(monkeypatch)
    monkeypatch.setattr(pkg, "_last_use", t.time())
    pkg._on_event(_crash_event())
    assert ctx.sent == [] and len(pkg._pending) == 1
    pkg._seen_by_agent(["panic:Load access fault:0x400075e6"])
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    pkg._flush()
    assert ctx.sent == []


def test_held_alert_about_another_crash_is_still_delivered(monkeypatch):
    import time as t
    pkg, ctx = _fresh(monkeypatch)
    monkeypatch.setattr(pkg, "_last_use", t.time())
    pkg._on_event(_crash_event("0x40001234"))
    pkg._seen_by_agent(["panic:Load access fault:0x400075e6"])
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    pkg._flush()
    assert len(ctx.sent) == 1 and "0x40001234" in ctx.sent[0]


def test_alert_held_for_whole_turn_even_when_board_tools_are_idle(monkeypatch):
    # The agent spends a long stretch reading and patching source with no board tool
    # calls. An alert injected then would be queued by Hermes and arrive after the fix.
    pkg, ctx = _fresh(monkeypatch)
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    pkg._turn_start(session_id="s", turn_id="t")
    pkg._on_event(_crash_event())
    assert ctx.sent == [] and len(pkg._pending) == 1
    pkg._turn_end(session_id="s", turn_id="t")
    pkg._flush()
    assert ctx.sent == []  # quiet window right after the answer
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    pkg._flush()
    assert len(ctx.sent) == 1


def test_turn_that_never_ends_stops_holding_alerts(monkeypatch):
    import time as t
    pkg, ctx = _fresh(monkeypatch)
    monkeypatch.setattr(pkg, "_last_use", 0.0)
    monkeypatch.setattr(pkg, "_turn_started", t.time() - pkg._TURN_MAX_S - 1)
    pkg._on_event(_crash_event())
    assert len(ctx.sent) == 1


def test_turn_hooks_registered(monkeypatch):
    pkg = _pkg()
    hooks = {}

    class Ctx:
        def register_tool(self, **k): pass
        def register_hook(self, name, fn): hooks[name] = fn
        def on_unload(self, fn): pass

    pkg.register(Ctx())
    assert {"pre_tool_call", "pre_llm_call", "post_llm_call"} <= set(hooks)


def test_child_env_is_an_allowlist(monkeypatch):
    import os
    _pkg()
    ce, bm = sys.modules["numogram.childenv"], sys.modules["numogram.build"]
    for k, v in {"OPENAI_API_KEY": "sk-x", "NOUS_TOKEN": "t", "CCACHE_DIR": "/c", "LC_ALL": "C",
                 "IDF_TOOLS_PATH": "/tools"}.items():
        monkeypatch.setenv(k, v)
    env = bm._env(Path("/idf"))
    assert "OPENAI_API_KEY" not in env and "NOUS_TOKEN" not in env
    assert env["IDF_PATH"] == "/idf" and env["IDF_TOOLS_PATH"] == "/tools"
    assert env["CCACHE_DIR"] == "/c" and env["LC_ALL"] == "C" and env["PATH"]
    allowed = set(ce.BASE) | {"PATH"} | {k for k in os.environ if k.startswith("LC_")}
    assert set(ce.minimal_env()) <= allowed


def test_data_dir_follows_the_active_hermes_profile(monkeypatch, tmp_path):
    import types
    _pkg()
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: tmp_path / "profile"
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "not-this"))
    d = sys.modules["numogram.board"].data_dir()
    assert d == tmp_path / "profile" / "numogram" and d.is_dir()


def test_esptool_child_gets_only_its_package_path(monkeypatch):
    _pkg()
    bd = sys.modules["numogram.board"]
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        class R: returncode, stdout, stderr = 0, "", ""
        return R()

    monkeypatch.setenv("NUMOGRAM_PORT", "/dev/null")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    monkeypatch.setattr(bd.subprocess, "run", fake_run)
    bd._esptool(["version"])
    import esptool
    assert "OPENAI_API_KEY" not in seen
    assert str(Path(esptool.__file__).parents[1]) in seen["PYTHONPATH"].split(":")
    assert "/somewhere/else" not in seen["PYTHONPATH"]
