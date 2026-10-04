from __future__ import annotations

import hashlib
import json
import re
import math
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



def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    total = len(data)
    value = 0.0
    for count in counts:
        if not count:
            continue
        p = count / total
        value -= p * math.log2(p)
    return round(value, 4)


def _extract_strings(data: bytes, min_len: int = 5) -> list[str]:
    ascii_strings = re.findall(rb"[\x20-\x7e]{%d,}" % min_len, data)

    utf16_strings = []
    pattern = rb"(?:[\x20-\x7e]\x00){%d,}" % min_len
    for raw in re.findall(pattern, data):
        try:
            utf16_strings.append(raw.decode("utf-16le"))
        except Exception:
            pass

    output = []
    seen = set()

    for raw in ascii_strings:
        value = raw.decode("latin-1", errors="ignore")
        if value not in seen:
            seen.add(value)
            output.append(value)

    for value in utf16_strings:
        if value not in seen:
            seen.add(value)
            output.append(value)

    return output


def _interesting_strings(strings: list[str]) -> dict:
    groups = {
        "client_build_markers": [],
        "ragnarok_markers": [],
        "network_markers": [],
        "protection_markers": [],
    }

    client_patterns = (
        r"ragexe", r"sakexe", r"ragexere", r"ragnarok",
        r"20\d{2}[-_/]\d{2}[-_/]\d{2}",
        r"20\d{6}",
    )
    network_patterns = (
        r"packet", r"login", r"charserver", r"mapserver",
        r"connect", r"winsock", r"ws2_32",
    )
    protection_patterns = (
        r"gepard", r"gameguard", r"easyanticheat", r"nprotect",
    )

    for value in strings:
        low = value.lower()

        if any(re.search(p, low) for p in client_patterns):
            if len(groups["client_build_markers"]) < 100:
                groups["client_build_markers"].append(value)

        if "ragnarok" in low or "gravity" in low:
            if len(groups["ragnarok_markers"]) < 100:
                groups["ragnarok_markers"].append(value)

        if any(re.search(p, low) for p in network_patterns):
            if len(groups["network_markers"]) < 100:
                groups["network_markers"].append(value)

        if any(re.search(p, low) for p in protection_patterns):
            if len(groups["protection_markers"]) < 50:
                groups["protection_markers"].append(value)

    return groups


def _pe_details(pe: pefile.PE, exe_bytes: bytes) -> dict:
    sections = []
    for section in pe.sections:
        name = section.Name.rstrip(b"\x00").decode("latin-1", errors="replace")
        raw = section.get_data()
        sections.append({
            "name": name,
            "virtual_address": hex(section.VirtualAddress),
            "virtual_size": int(section.Misc_VirtualSize),
            "raw_size": int(section.SizeOfRawData),
            "entropy": _entropy(raw),
            "characteristics": hex(section.Characteristics),
        })

    imports = []
    try:
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
            dll = entry.dll.decode("latin-1", errors="replace")
            names = []
            for imp in entry.imports:
                if imp.name:
                    names.append(imp.name.decode("latin-1", errors="replace"))
            imports.append({
                "dll": dll,
                "functions": names[:80],
            })
    except Exception:
        pass

    strings = _extract_strings(exe_bytes)

    return {
        "entry_point": hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint),
        "sections": sections,
        "imports": imports,
        "interesting_strings": _interesting_strings(strings),
        "string_count": len(strings),
    }


def scan_client(exe_path: str) -> dict:
    exe = Path(exe_path).expanduser().resolve()
    if not exe.exists():
        raise FileNotFoundError(f"Client executable not found: {exe}")
    if not exe.is_file():
        raise ValueError(f"Not a file: {exe}")

    exe_bytes = exe.read_bytes()
    pe = pefile.PE(data=exe_bytes, fast_load=False)
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
        "pe_details": _pe_details(pe, exe_bytes),
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
