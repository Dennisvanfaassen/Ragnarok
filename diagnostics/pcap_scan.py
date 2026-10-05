from __future__ import annotations

import hashlib
import io
import socket
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import dpkt


LOGIN_PACKETS = {
    0x0064: {"name": "master_login", "length": 55, "version_off": 2, "master_off": 54},
    0x01DD: {"name": "master_login_md5", "length": 47, "version_off": 2, "master_off": 46},
    0x0987: {"name": "master_login_md5_hex", "length": 63, "version_off": 2, "master_off": 62},
    0x0AAC: {"name": "master_login_hex", "length": 69, "version_off": 2, "master_off": 68},
}

KNOWN_FIXED = {
    0x0064: 55,
    0x0065: 17,
    0x0066: 3,
    0x0204: 18,
    0x0205: 26,
    0x02CA: 3,
    0x083E: 26,
    0x0ACD: 3,
    0x0AE0: 30,
}


@dataclass
class TcpPayload:
    ts: float
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    data: bytes


def _ip(raw: bytes) -> str:
    try:
        return socket.inet_ntop(socket.AF_INET, raw)
    except Exception:
        return "?"


def _reader(data: bytes):
    bio = io.BytesIO(data)
    try:
        return dpkt.pcapng.Reader(bio)
    except Exception:
        bio.seek(0)
        return dpkt.pcap.Reader(bio)


def _iter_tcp(data: bytes):
    for ts, buf in _reader(data):
        try:
            eth = dpkt.ethernet.Ethernet(buf)
            ip = eth.data
            if not isinstance(ip, dpkt.ip.IP):
                continue
            tcp = ip.data
            if not isinstance(tcp, dpkt.tcp.TCP) or not tcp.data:
                continue
            yield TcpPayload(
                ts=float(ts),
                src_ip=_ip(ip.src),
                src_port=int(tcp.sport),
                dst_ip=_ip(ip.dst),
                dst_port=int(tcp.dport),
                data=bytes(tcp.data),
            )
        except Exception:
            continue


def _opcode(data: bytes) -> int | None:
    if len(data) < 2:
        return None
    return struct.unpack_from("<H", data, 0)[0]


def _decode_known(data: bytes) -> dict[str, Any]:
    op = _opcode(data)
    if op is None:
        return {}

    result: dict[str, Any] = {
        "opcode": f"0x{op:04X}",
        "tcp_payload_length": len(data),
    }

    if op in LOGIN_PACKETS:
        meta = LOGIN_PACKETS[op]
        result["name"] = meta["name"]
        if len(data) >= meta["length"]:
            result["version"] = struct.unpack_from("<I", data, meta["version_off"])[0]
            result["master_version"] = data[meta["master_off"]]
            result["credential_fields"] = "redacted"

    elif op == 0x0825:
        result["name"] = "token_login"
        # OpenKore kRO/Sakray format: v len, V version, C master_version...
        if len(data) >= 9:
            declared = struct.unpack_from("<H", data, 2)[0]
            result["declared_length"] = declared
            result["version"] = struct.unpack_from("<I", data, 4)[0]
            result["master_version"] = data[8]
            result["credential_fields"] = "redacted"

    elif op == 0x0204:
        result["name"] = "CA_EXE_HASHCHECK"
        result["client_hash"] = "present_redacted"

    elif op == 0x083E and len(data) >= 26:
        result["name"] = "AC_REFUSE_LOGIN_R2"
        result["login_error_type"] = struct.unpack_from("<I", data, 2)[0]

    return result


def analyze_capture(data: bytes, server_ip: str = "88.214.58.232") -> dict[str, Any]:
    if not data:
        raise ValueError("Capture file is empty.")

    packets = list(_iter_tcp(data))
    if not packets:
        raise ValueError("No IPv4 TCP payloads found in capture.")

    conversations = Counter()
    for p in packets:
        a = f"{p.src_ip}:{p.src_port}"
        b = f"{p.dst_ip}:{p.dst_port}"
        key = " <-> ".join(sorted([a, b]))
        conversations[key] += 1

    relevant = [
        p for p in packets
        if p.src_ip == server_ip or p.dst_ip == server_ip
    ]

    # If the configured server IP is absent, keep a compact overview so the
    # user can spot an OTP/redirect endpoint without exposing payload data.
    chosen = relevant if relevant else packets

    events = []
    for p in chosen[:500]:
        decoded = _decode_known(p.data)
        events.append({
            "timestamp": p.ts,
            "direction": (
                "server_to_client" if p.src_ip == server_ip
                else "client_to_server" if p.dst_ip == server_ip
                else "other"
            ),
            "src": f"{p.src_ip}:{p.src_port}",
            "dst": f"{p.dst_ip}:{p.dst_port}",
            "length": len(p.data),
            "opcode": (
                f"0x{_opcode(p.data):04X}" if _opcode(p.data) is not None else None
            ),
            "sha256": hashlib.sha256(p.data).hexdigest(),
            "decoded": decoded,
        })

    discovered = []
    for event in events:
        d = event["decoded"]
        if d.get("name") in {
            "master_login",
            "master_login_md5",
            "master_login_md5_hex",
            "master_login_hex",
            "token_login",
        }:
            discovered.append({
                "name": d["name"],
                "opcode": d.get("opcode"),
                "version": d.get("version"),
                "master_version": d.get("master_version"),
                "declared_length": d.get("declared_length"),
            })

    return {
        "capture_size": len(data),
        "tcp_payload_count": len(packets),
        "matched_server_ip": bool(relevant),
        "server_ip": server_ip,
        "top_conversations": [
            {"conversation": key, "payload_segments": count}
            for key, count in conversations.most_common(20)
        ],
        "login_candidates": discovered,
        "events": events,
        "privacy": (
            "Raw payload bytes, usernames, passwords and OTP/token fields are "
            "not returned. Only metadata and explicitly non-secret protocol "
            "fields are surfaced."
        ),
    }
