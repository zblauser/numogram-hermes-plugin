"""Run ESP-IDF builds where Hermes runs. One fixed command; paths go in as arguments.

A build runs the project's own CMake scripts, so it can run any code the project
contains, the same as running idf.py yourself. Only build projects you trust.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 900
MAX_ERRORS = 20

_DIAG = re.compile(
    r"^(?P<file>[^\s:][^:\n]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<kind>fatal error|error|warning):\s*(?P<msg>.+)$",
    re.M,
)
_SIZE = re.compile(
    r"(?P<bin>\S+\.bin) binary size (?P<size>0x[0-9a-f]+) bytes\. Smallest app partition is "
    r"(?P<part>0x[0-9a-f]+) bytes\. (?P<free>0x[0-9a-f]+) bytes \((?P<pct>\d+)%\) free",
    re.I,
)
# export.sh needs IDF_PATH. "$1" and "$2" are the project and build dirs, never spliced into the script.
_SCRIPT = '. "$IDF_PATH/export.sh" >/dev/null 2>&1 || { echo "ESP-IDF export failed" >&2; exit 97; }; exec idf.py -C "$1" -B "$2" build'


class BuildError(Exception):
    pass


def _env(idf: Path) -> dict:
    """ESP-IDF picks its Python from PATH. Hide the venv Hermes runs in, or export.sh refuses to run."""
    env = dict(os.environ)
    venv = os.environ.get("VIRTUAL_ENV") or (sys.prefix if sys.prefix != sys.base_prefix else None)
    for k in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    if venv:
        bad = str(Path(venv) / "bin")
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p.rstrip("/") != bad)
    env["IDF_PATH"] = str(idf)
    return env


def idf_path() -> Path | None:
    p = os.environ.get("IDF_PATH")
    if p and (Path(p) / "export.sh").is_file():
        return Path(p)
    return None


def project_info(build_dir: str | Path) -> dict:
    """What the last build in this dir produced, from ESP-IDF's own description file."""
    bd = Path(build_dir)
    try:
        d = json.loads((bd / "project_description.json").read_text())
    except (OSError, ValueError):
        return {}
    elf = bd / d["app_elf"] if d.get("app_elf") else None
    return {
        "project": d.get("project_name"),
        "target": d.get("target"),
        "elf": str(elf) if elf and elf.is_file() else None,
    }


def parse(output: str) -> dict:
    errors, seen, warnings = [], set(), 0
    for m in _DIAG.finditer(output):
        if m["kind"] == "warning":
            warnings += 1
            continue
        key = (m["file"], m["line"], m["msg"])
        if key in seen:
            continue
        seen.add(key)
        if len(errors) < MAX_ERRORS:
            errors.append({"file": m["file"], "line": int(m["line"]), "message": m["msg"].strip()})
    res: dict = {"errors": errors, "warnings": warnings}
    if s := _SIZE.search(output):
        res["size"] = {"bin": s["bin"], "bytes": int(s["size"], 16), "partition_bytes": int(s["part"], 16), "free_pct": int(s["pct"])}
    return res


def build(project_dir: str, build_dir: str | None = None) -> dict:
    idf = idf_path()
    if idf is None:
        raise BuildError(
            "ESP-IDF isn't installed where Hermes runs (IDF_PATH not set). "
            "Install ESP-IDF and set IDF_PATH, or build yourself and pass the build dir to board_flash."
        )
    proj = Path(project_dir).expanduser().resolve()
    if not (proj / "CMakeLists.txt").is_file():
        raise BuildError(f"{proj} has no CMakeLists.txt, so it isn't an ESP-IDF project.")
    bd = Path(build_dir).expanduser().resolve() if build_dir else proj / "build"
    sh = shutil.which("bash") or "sh"
    t0 = time.time()
    try:
        p = subprocess.run(
            [sh, "-c", _SCRIPT, "numogram-build", str(proj), str(bd)],
            capture_output=True, text=True, timeout=TIMEOUT_S, env=_env(idf),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Build took longer than {TIMEOUT_S} s and was stopped.", "build_dir": str(bd)}
    out = (p.stdout or "") + (p.stderr or "")
    res = {"ok": p.returncode == 0, "seconds": round(time.time() - t0, 1), "build_dir": str(bd), **parse(out)}
    if res["ok"]:
        res.update(project_info(bd))
    else:
        res["exit_code"] = p.returncode
        # Compiler output quotes source lines: it's the user's code, but still data, not instructions.
        res["build_output_tail"] = out[-3000:]
    return res
