from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import pefile


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text_guess(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "euc-kr", "cp949", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("latin-1", errors="replace")


def _version_info(pe: pefile.PE) -> dict:
    result = {}
    try:
        for fileinfo in getattr(pe, "FileInfo", []) or []:
            for entry in fileinfo:
                if getattr(entry, "Key", b"") == b"StringFileInfo":
                    for st in entry.StringTable:
                        for k, v in st.entries.items():
                            key = k.decode(errors="ignore") if isinstance(k, bytes) else str(k)
                            val = v.decode(errors="ignore") if isinstance(v, bytes) else str(v)
                            result[key] = val
    except Exception:
        pass
    return result


def _parse_data_ini(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = _read_text_guess(path)
    found = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+\s*=\s*(.+?\.grf)\s*$", line, flags=re.I)
        if m:
            found.append(m.group(1).strip())
    return found


def _parse_clientinfo(path: Path) -> dict:
    if not path.exists():
        return {}

    text = _read_text_guess(path)
    try:
        root = ElementTree.fromstring(text)
    except Exception:
        return {"parse_error": True}

    conn = root.find(".//connection")
    if conn is None:
        return {}

    fields = {}
    for key in (
        "display",
        "address",
        "port",
        "version",
        "langtype",
        "registrationweb",
    ):
        node = conn.find(key)
        if node is not None and node.text is not None:
            fields[key] = node.text.strip()

    for key in ("servicetype", "servertype"):
        node = root.find(key)
        if node is not None and node.text is not None:
            fields[key] = node.text.strip()

    return fields


def scan_client(exe_path: str) -> dict:
    exe = Path(exe_path).expanduser().resolve()
    if not exe.exists():
        raise FileNotFoundError(f"Client executable not found: {exe}")
    if not exe.is_file():
        raise ValueError(f"Not a file: {exe}")

    pe = pefile.PE(str(exe), fast_load=False)
    timestamp = int(pe.FILE_HEADER.TimeDateStamp)
    version_info = _version_info(pe)

    client_dir = exe.parent
    data_ini = client_dir / "DATA.ini"
    if not data_ini.exists():
        alt = client_dir / "data.ini"
        if alt.exists():
            data_ini = alt

    clientinfo_candidates = [
        client_dir / "data" / "clientinfo.xml",
        client_dir / "clientinfo.xml",
    ]
    clientinfo_path = next((p for p in clientinfo_candidates if p.exists()), None)

    report = {
        "exe_path": str(exe),
        "exe_name": exe.name,
        "exe_size": exe.stat().st_size,
        "sha256": _sha256(exe),
        "pe_timestamp_unix": timestamp,
        "pe_timestamp_utc": datetime.fromtimestamp(
            timestamp, tz=timezone.utc
        ).isoformat(),
        "machine": hex(pe.FILE_HEADER.Machine),
        "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
        "subsystem": int(pe.OPTIONAL_HEADER.Subsystem),
        "version_info": version_info,
        "grf_load_order": _parse_data_ini(data_ini),
        "data_ini_path": str(data_ini) if data_ini.exists() else None,
        "clientinfo_path": str(clientinfo_path) if clientinfo_path else None,
        "clientinfo": _parse_clientinfo(clientinfo_path) if clientinfo_path else {},
        "protocol": {
            "login_version_known": bool(
                clientinfo_path
                and _parse_clientinfo(clientinfo_path).get("version")
            ),
            "packet_profile_known": False,
            "packet_profile": None,
            "status": (
                "Client metadata collected. Packet profile still needs "
                "identification from executable/protocol characteristics."
            ),
        },
    }

    return report
