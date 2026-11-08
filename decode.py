"""Map firmware addresses to source lines from an ELF. No cross toolchain needed."""
from __future__ import annotations

import bisect
import hashlib
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection


def elf_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def matches(path: str | Path, boot_sha: str | None) -> bool | None:
    """Boot logs print a prefix of the ELF hash. None means the log had no hash."""
    if not boot_sha:
        return None
    return elf_sha256(path).startswith(boot_sha.lower().rstrip("."))


class Decoder:
    def __init__(self, path: str | Path):
        self._f = open(path, "rb")
        self.elf = ELFFile(self._f)
        self._funcs = self._load_funcs()
        self._starts = [f[0] for f in self._funcs]
        self._rows = self._load_lines()
        self._addrs = [r[0] for r in self._rows]

    def close(self) -> None:
        self._f.close()

    def _load_funcs(self):
        out = []
        for sec in self.elf.iter_sections():
            if isinstance(sec, SymbolTableSection):
                for s in sec.iter_symbols():
                    if s["st_info"]["type"] == "STT_FUNC" and s["st_size"]:
                        out.append((s["st_value"] & ~1, s["st_size"], s.name))
        out.sort()
        return out

    def _load_lines(self):
        rows = []
        if not self.elf.has_dwarf_info():
            return rows
        dwarf = self.elf.get_dwarf_info()
        for cu in dwarf.iter_CUs():
            lp = dwarf.line_program_for_CU(cu)
            if lp is None:
                continue
            ver = lp.header["version"]
            files = lp.header["file_entry"]
            dirs = lp.header["include_directory"]
            base = 0 if ver >= 5 else 1
            prev = None
            for e in lp.get_entries():
                st = e.state
                if st is None:
                    continue
                if prev is not None and not prev.end_sequence and prev.address <= st.address:
                    rows.append((prev.address, st.address, self._name(files, dirs, prev.file - base, ver), prev.line))
                prev = None if st.end_sequence else st
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows

    @staticmethod
    def _name(files, dirs, idx, ver) -> str:
        try:
            fe = files[idx]
            name = fe.name.decode(errors="replace")
            if name.startswith("/"):
                return name
            # DWARF 5 numbers directories from 0. Before that, 0 means the compile
            # dir, which isn't in the table, and real entries start at 1.
            if ver < 5 and fe.dir_index == 0:
                return name
            d = dirs[fe.dir_index if ver >= 5 else fe.dir_index - 1]
            return f"{d.decode(errors='replace')}/{name}"
        except (IndexError, AttributeError):
            return "?"

    def lookup(self, addr: int) -> dict:
        res = {"addr": hex(addr), "function": None, "file": None, "line": None}
        i = bisect.bisect_right(self._starts, addr) - 1
        if i >= 0:
            start, size, name = self._funcs[i]
            if start <= addr < start + size:
                res["function"] = name
        j = bisect.bisect_right(self._addrs, addr) - 1
        # Rows can overlap across compile units, so look back a little for one that covers addr.
        while j >= 0 and j > bisect.bisect_right(self._addrs, addr) - 8:
            lo, hi, path, line = self._rows[j]
            if lo <= addr < hi:
                res["file"], res["line"] = path, line
                break
            j -= 1
        return res
