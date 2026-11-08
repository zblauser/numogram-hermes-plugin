"""Read-only security audit of a device you own. Reports and suggests. Never changes anything."""
from __future__ import annotations

import re
from pathlib import Path

_FUSE = re.compile(r"^(\w+)\s+\(BLOCK\d+\).*?=\s*(\S+(?: \S+)?)\s+[R-][/-][W-]\s", re.M)

_FUSE_CHECKS = [
    ("SECURE_BOOT_EN", lambda v: v == "False", "secure_boot_off", "Any firmware can run on this chip.", "CONFIG_SECURE_BOOT"),
    ("SPI_BOOT_CRYPT_CNT", lambda v: v.startswith("Disable"), "flash_encryption_off", "Anyone with the board can read flash, including secrets.", "CONFIG_SECURE_FLASH_ENC_ENABLED"),
    ("DIS_PAD_JTAG", lambda v: v == "False", "jtag_pads_open", "JTAG over pins is available.", None),
    ("DIS_USB_JTAG", lambda v: v == "False", "usb_jtag_open", "JTAG over USB is available.", None),
    ("DIS_DOWNLOAD_MODE", lambda v: v == "False", "download_mode_open", "ROM download mode can read and write flash.", None),
]

_NOTE = "Enabling these burns eFuses, which is permanent. numogram never does this. Decide before production, not during development."


def audit_efuse(summary: str) -> dict:
    vals = {m.group(1): m.group(2).strip() for m in _FUSE.finditer(summary)}
    findings, unknown = [], []
    for fuse, is_open, fid, why, cfg in _FUSE_CHECKS:
        if fuse not in vals:
            unknown.append(fuse)
            continue
        if is_open(vals[fuse]):
            f = {"id": fid, "fuse": fuse, "value": vals[fuse], "why": why}
            if cfg:
                f["config"] = cfg
            findings.append(f)
    return {"findings": findings, "not_reported_by_chip": unknown, "note": _NOTE}


_SECRET_PATTERNS = [
    ("private_key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("api_key_sk", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("url_with_password", re.compile(rb"\b[a-z][a-z0-9+.-]{1,10}://[^\s:/@\x00]{1,64}:[^\s:/@\x00]{1,64}@[^\s\x00]{3,}")),
    ("assigned_password", re.compile(rb"(?i)(?:passw(?:or)?d|psk|api_?key|secret|token)\s*[=:]\s*[\"']?[^\s\"'\x00%]{6,}")),
]


def _mask(raw: bytes) -> str:
    s = raw.decode("latin-1")
    return f"{s[:4]}…({len(s)} chars)" if len(s) > 4 else "…"


def scan_image(path: str | Path, limit: int = 20) -> dict:
    """Secrets are masked in the result, so an audit doesn't copy them into the model's context."""
    data = Path(path).read_bytes()
    hits, seen = [], set()
    for kind, rx in _SECRET_PATTERNS:
        for m in rx.finditer(data):
            if m.group(0) in seen:
                continue
            seen.add(m.group(0))
            hits.append({"kind": kind, "offset": hex(m.start()), "preview": _mask(m.group(0))})
            if len(hits) >= limit:
                return {"bytes": len(data), "findings": hits, "truncated": True}
    return {"bytes": len(data), "findings": hits, "truncated": False}


_CONFIG_RISKS = {
    "CONFIG_ESP_TLS_INSECURE": "TLS checks can be weakened.",
    "CONFIG_ESP_TLS_SKIP_SERVER_CERT_VERIFY": "TLS accepts any server certificate.",
    "CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP": "Firmware updates can arrive over plain HTTP.",
    "CONFIG_BOOTLOADER_LOG_LEVEL_VERBOSE": "Verbose bootloader log.",
    "CONFIG_LOG_DEFAULT_LEVEL_VERBOSE": "Verbose app log can leak runtime data.",
    "CONFIG_LOG_DEFAULT_LEVEL_DEBUG": "Debug app log can leak runtime data.",
    "CONFIG_ESP_SYSTEM_PANIC_GDBSTUB": "A crash opens a debugger on the console.",
}


def audit_sdkconfig(text: str) -> dict:
    on = {l.split("=", 1)[0] for l in text.splitlines() if l.startswith("CONFIG_") and l.rstrip().endswith("=y")}
    return {"findings": [{"config": k, "why": v} for k, v in _CONFIG_RISKS.items() if k in on]}
