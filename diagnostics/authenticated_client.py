from __future__ import annotations

import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import psutil
from scapy.all import AsyncSniffer, IP, TCP, Raw


SERVER_IP = "88.214.58.232"
RO_PORTS = {6900: "login", 6121: "character", 5121: "map"}

# Packet sizes used by Live Game State v1. Variable actor packets carry their
# own little-endian length at bytes 2..3.
FIXED_PACKET_LENGTHS = {
    0x007F: 6,   # received_sync
    0x0080: 7,   # actor_died_or_disappeared
    0x0087: 12,  # character_moves
    0x008A: 29,  # actor_action
    0x0091: 22,  # map_change
    0x009D: 19,  # floor item exists
    0x009E: 17,  # legacy floor item appeared
    0x00A1: 6,   # floor item disappeared
    0x00B0: 8,   # stat_info
    0x0ADD: 24,  # modern floor item appeared
}
VARIABLE_PACKET_OPCODES = {0x09FD, 0x09FE, 0x09FF}

# rAthena/OpenKore SP_* values carried by 00B0.
STAT_NAMES = {
    5: "hp",
    6: "hp_max",
    7: "sp",
    8: "sp_max",
    9: "status_points",
    11: "base_level",
    12: "skill_points",
}


def _coords3(raw: bytes) -> tuple[int, int] | None:
    if len(raw) < 3:
        return None
    x = (raw[0] << 2) | (raw[1] >> 6)
    y = ((raw[1] & 0x3F) << 4) | (raw[2] >> 4)
    return x, y


def _coords6(raw: bytes) -> tuple[tuple[int, int], tuple[int, int]] | None:
    if len(raw) < 6:
        return None
    x0 = (raw[0] << 2) | (raw[1] >> 6)
    y0 = ((raw[1] & 0x3F) << 4) | (raw[2] >> 4)
    x1 = ((raw[2] & 0x0F) << 6) | (raw[3] >> 2)
    y1 = ((raw[3] & 0x03) << 8) | raw[4]
    return (x0, y0), (x1, y1)


def _clean_text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


def _actor_kind(object_type: int) -> str:
    # Object type 5 is the standard monster type. Type 0 is a player actor.
    # Other values are intentionally left generic until we observe/verify them.
    if object_type == 5:
        return "monster"
    if object_type == 0:
        return "player"
    return "other"


class AuthenticatedClientMonitor:
    def __init__(self):
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=500)
        self._sniffer: AsyncSniffer | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._patcher_pid: int | None = None
        self._classic_pid: int | None = None
        self._patcher_path: str | None = None
        self._status = "stopped"
        self._message = "Not running"
        self._reset_world_state()

    def _reset_world_state(self):
        self._world: dict[str, Any] = {
            "map": None,
            "x": None,
            "y": None,
            "hp": None,
            "hp_max": None,
            "sp": None,
            "sp_max": None,
            "base_level": None,
            "status_points": None,
            "skill_points": None,
            "last_sync": None,
            "self_account_id": None,
            "self_char_id": None,
            "last_combat": None,
        }
        self._actors: dict[int, dict[str, Any]] = {}
        self._floor_items: dict[int, dict[str, Any]] = {}
        self._aggressors: dict[int, float] = {}
        self._parsed_counts: dict[str, int] = {
            "map_change": 0,
            "character_moves": 0,
            "actor_moved": 0,
            "actor_connected": 0,
            "actor_exists": 0,
            "actor_removed": 0,
            "stat_info": 0,
            "sync": 0,
            "combat": 0,
            "item_seen": 0,
            "item_removed": 0,
        }

    def _set_status(self, status: str, message: str):
        with self._lock:
            self._status = status
            self._message = message

    def _parse_actor(self, opcode: int, data: bytes):
        if len(data) < 13:
            return

        object_type = data[4]
        actor_id = int.from_bytes(data[5:9], "little")
        char_id = int.from_bytes(data[9:13], "little")

        actor = self._actors.get(actor_id, {})
        actor.update({
            "id": actor_id,
            "char_id": char_id,
            "object_type": object_type,
            "kind": _actor_kind(object_type),
            "last_seen": time.time(),
            "packet": f"0x{opcode:04X}",
        })

        # 09FD (actor_moved): coords a6 begin at absolute offset 67.
        if opcode == 0x09FD and len(data) >= 73:
            movement = _coords6(data[67:73])
            if movement:
                (from_x, from_y), (to_x, to_y) = movement
                actor.update({
                    "from_x": from_x,
                    "from_y": from_y,
                    "x": to_x,
                    "y": to_y,
                })
            if len(data) > 90:
                name = _clean_text(data[90:])
                if name:
                    actor["name"] = name

        # 09FE / 09FF use a 3-byte standing coordinate. For this protocol
        # family it begins at absolute offset 63.
        elif opcode in {0x09FE, 0x09FF} and len(data) >= 66:
            pos = _coords3(data[63:66])
            if pos:
                actor["x"], actor["y"] = pos
            if len(data) > 84:
                name = _clean_text(data[84:])
                if name:
                    actor["name"] = name

        self._actors[actor_id] = actor
        key = {
            0x09FD: "actor_moved",
            0x09FE: "actor_connected",
            0x09FF: "actor_exists",
        }[opcode]
        self._parsed_counts[key] += 1

    def _parse_client_map_packet(self, data: bytes):
        if len(data) < 2:
            return

        opcode = int.from_bytes(data[:2], "little")

        # 0436 map_login: accountID, charID, sessionID, unknown, tick, sex.
        if opcode == 0x0436 and len(data) >= 23:
            self._world["self_account_id"] = int.from_bytes(data[2:6], "little")
            self._world["self_char_id"] = int.from_bytes(data[6:10], "little")

    def _parse_world_packet(self, data: bytes):
        if len(data) < 2:
            return

        opcode = int.from_bytes(data[:2], "little")

        if opcode == 0x008A and len(data) >= 29:
            source_id = int.from_bytes(data[2:6], "little")
            target_id = int.from_bytes(data[6:10], "little")
            self_ids = {
                int(v)
                for v in (
                    self._world.get("self_account_id"),
                    self._world.get("self_char_id"),
                )
                if v is not None
            }
            now = time.time()
            if source_id in self_ids:
                self._world["last_combat"] = {
                    "timestamp": now,
                    "source_id": source_id,
                    "target_id": target_id,
                }
            elif target_id in self_ids:
                actor = self._actors.get(source_id)
                if actor and actor.get("kind") == "monster":
                    self._aggressors[source_id] = now
                    actor["aggressive_to_me"] = True
                    actor["last_aggression"] = now
            self._parsed_counts["combat"] += 1
            return

        if opcode == 0x0091 and len(data) >= 22:
            map_name = _clean_text(data[2:18])
            x, y = struct.unpack_from("<HH", data, 18)
            self._world.update({"map": map_name, "x": x, "y": y})
            self._actors.clear()
            self._floor_items.clear()
            self._aggressors.clear()
            self._parsed_counts["map_change"] += 1
            return

        if opcode == 0x0087 and len(data) >= 12:
            movement = _coords6(data[6:12])
            if movement:
                _start, destination = movement
                self._world["x"], self._world["y"] = destination
            self._parsed_counts["character_moves"] += 1
            return

        if opcode == 0x00B0 and len(data) >= 8:
            stat_type = struct.unpack_from("<H", data, 2)[0]
            value = struct.unpack_from("<I", data, 4)[0]
            name = STAT_NAMES.get(stat_type)
            if name:
                self._world[name] = value
            self._parsed_counts["stat_info"] += 1
            return

        if opcode == 0x007F and len(data) >= 6:
            self._world["last_sync"] = struct.unpack_from("<I", data, 2)[0]
            self._parsed_counts["sync"] += 1
            return

        if opcode == 0x0080 and len(data) >= 7:
            actor_id = int.from_bytes(data[2:6], "little")
            self._actors.pop(actor_id, None)
            self._aggressors.pop(actor_id, None)
            self._parsed_counts["actor_removed"] += 1
            return

        if opcode == 0x009D and len(data) >= 19:
            item_id = int.from_bytes(data[2:6], "little")
            self._floor_items[item_id] = {
                "id": item_id,
                "name_id": int.from_bytes(data[6:10], "little"),
                "x": int.from_bytes(data[11:13], "little"),
                "y": int.from_bytes(data[13:15], "little"),
                "amount": int.from_bytes(data[15:17], "little"),
                "last_seen": time.time(),
            }
            self._parsed_counts["item_seen"] += 1
            return

        if opcode == 0x009E and len(data) >= 17:
            item_id = int.from_bytes(data[2:6], "little")
            self._floor_items[item_id] = {
                "id": item_id,
                "name_id": int.from_bytes(data[6:8], "little"),
                "x": int.from_bytes(data[9:11], "little"),
                "y": int.from_bytes(data[11:13], "little"),
                "amount": int.from_bytes(data[15:17], "little"),
                "last_seen": time.time(),
            }
            self._parsed_counts["item_seen"] += 1
            return

        if opcode == 0x0ADD and len(data) >= 24:
            item_id = int.from_bytes(data[2:6], "little")
            self._floor_items[item_id] = {
                "id": item_id,
                "name_id": int.from_bytes(data[6:10], "little"),
                "x": int.from_bytes(data[13:15], "little"),
                "y": int.from_bytes(data[15:17], "little"),
                "amount": int.from_bytes(data[19:21], "little"),
                "last_seen": time.time(),
            }
            self._parsed_counts["item_seen"] += 1
            return

        if opcode == 0x00A1 and len(data) >= 6:
            item_id = int.from_bytes(data[2:6], "little")
            self._floor_items.pop(item_id, None)
            self._parsed_counts["item_removed"] += 1
            return

        if opcode in VARIABLE_PACKET_OPCODES:
            self._parse_actor(opcode, data)

    def _parse_payload(self, payload: bytes):
        """Extract Live Game State packets from a TCP payload.

        TCP segment boundaries are not Ragnarok packet boundaries. For the
        observer we scan for the small set of packet types we understand and
        validate their lengths before decoding them. This is deliberately
        read-only and does not modify the client stream.
        """
        i = 0
        size = len(payload)

        while i + 2 <= size:
            opcode = int.from_bytes(payload[i:i + 2], "little")
            length = None

            if opcode in FIXED_PACKET_LENGTHS:
                candidate = FIXED_PACKET_LENGTHS[opcode]
                if i + candidate <= size:
                    length = candidate

            elif opcode in VARIABLE_PACKET_OPCODES and i + 4 <= size:
                candidate = int.from_bytes(payload[i + 2:i + 4], "little")
                # 09FD/09FE/09FF are sizeable actor packets. Requiring a
                # realistic minimum makes accidental opcode matches unlikely.
                if 80 <= candidate <= 2048 and i + candidate <= size:
                    length = candidate

            if length is None:
                i += 1
                continue

            packet = payload[i:i + length]

            # Extra sanity checks for actor packets before accepting a match.
            if opcode in VARIABLE_PACKET_OPCODES:
                object_type = packet[4] if len(packet) > 4 else 255
                if object_type > 20:
                    i += 1
                    continue

            self._parse_world_packet(packet)
            i += length

    def _parse_client_payload(self, payload: bytes):
        i = 0
        size = len(payload)
        while i + 2 <= size:
            opcode = int.from_bytes(payload[i:i + 2], "little")
            if opcode == 0x0436 and i + 23 <= size:
                self._parse_client_map_packet(payload[i:i + 23])
                i += 23
                continue
            i += 1

    def _packet(self, pkt):
        try:
            if IP not in pkt or TCP not in pkt:
                return

            ip = pkt[IP]
            tcp = pkt[TCP]

            if ip.src != SERVER_IP and ip.dst != SERVER_IP:
                return

            src_port = int(tcp.sport)
            dst_port = int(tcp.dport)
            server_port = src_port if ip.src == SERVER_IP else dst_port
            if server_port not in RO_PORTS:
                return

            payload = bytes(pkt[Raw].load) if Raw in pkt else b""
            opcode = None
            if len(payload) >= 2:
                opcode = f"0x{int.from_bytes(payload[:2], 'little'):04X}"

            stage = RO_PORTS[server_port]
            direction = "server_to_client" if ip.src == SERVER_IP else "client_to_server"

            if stage == "map" and payload:
                with self._lock:
                    if direction == "server_to_client":
                        self._parse_payload(payload)
                    else:
                        self._parse_client_payload(payload)

            event = {
                "timestamp": time.time(),
                "stage": stage,
                "direction": direction,
                "src": f"{ip.src}:{src_port}",
                "dst": f"{ip.dst}:{dst_port}",
                "payload_length": len(payload),
                "opcode": opcode,
            }
            with self._lock:
                self._events.append(event)
        except Exception:
            pass

    def _watch_processes(self):
        while not self._stop.is_set():
            classic_pid = None
            try:
                for proc in psutil.process_iter(["pid", "name", "exe"]):
                    name = (proc.info.get("name") or "").lower()
                    if name == "classic.exe":
                        classic_pid = int(proc.info["pid"])
                        break
            except Exception:
                pass

            with self._lock:
                self._classic_pid = classic_pid

            if classic_pid:
                self._set_status(
                    "classic_detected",
                    f"Classic.exe detected (PID {classic_pid}). Live Game State is active.",
                )
            elif self._patcher_pid:
                self._set_status(
                    "waiting_for_classic",
                    "SoulBound.exe launched. Waiting for patcher login and Classic.exe...",
                )

            self._stop.wait(1.0)

    def start(self, patcher_path: str) -> dict[str, Any]:
        path = Path(patcher_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Patcher not found: {path}")
        if path.name.lower() != "soulbound.exe":
            raise ValueError("Select SoulBound.exe, the Soulbound patcher executable.")

        self.stop()
        self._stop.clear()
        with self._lock:
            self._events.clear()
            self._reset_world_state()
            self._patcher_path = str(path)
            self._classic_pid = None

        try:
            proc = subprocess.Popen([str(path)], cwd=str(path.parent))
            self._patcher_pid = proc.pid
        except Exception as exc:
            self._set_status("error", f"Could not launch SoulBound.exe: {exc}")
            raise

        try:
            self._sniffer = AsyncSniffer(
                filter=f"host {SERVER_IP} and tcp and (port 6900 or port 6121 or port 5121)",
                prn=self._packet,
                store=False,
            )
            self._sniffer.start()
        except Exception as exc:
            self._set_status(
                "error",
                "SoulBound.exe launched, but live capture could not start. "
                "Make sure Npcap is installed (Wireshark normally includes it). "
                f"Details: {exc}",
            )
            return self.snapshot()

        self._watcher = threading.Thread(target=self._watch_processes, daemon=True)
        self._watcher.start()
        self._set_status(
            "waiting_for_classic",
            "SoulBound.exe launched. Sign in normally through the patcher; monitoring is active.",
        )
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None
        self._patcher_pid = None
        self._classic_pid = None
        self._set_status("stopped", "Authenticated Client Mode stopped.")
        return self.snapshot()

    def clear(self):
        with self._lock:
            self._events.clear()
            self._reset_world_state()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            counts = {"login": 0, "character": 0, "map": 0}
            for event in events:
                stage = event.get("stage")
                if stage in counts:
                    counts[stage] += 1

            actors = list(self._actors.values())
            floor_items = list(self._floor_items.values())
            aggressor_ids = [
                actor_id
                for actor_id in self._aggressors
                if actor_id in self._actors
                and self._actors[actor_id].get("kind") == "monster"
            ]
            actor_counts = {
                "total": len(actors),
                "monsters": sum(1 for a in actors if a.get("kind") == "monster"),
                "players": sum(1 for a in actors if a.get("kind") == "player"),
                "other": sum(1 for a in actors if a.get("kind") == "other"),
            }

            world = dict(self._world)
            hp = world.get("hp")
            hp_max = world.get("hp_max")
            sp = world.get("sp")
            sp_max = world.get("sp_max")
            world["hp_percent"] = (
                round(hp * 100 / hp_max, 1) if hp is not None and hp_max else None
            )
            world["sp_percent"] = (
                round(sp * 100 / sp_max, 1) if sp is not None and sp_max else None
            )

            return {
                "status": self._status,
                "message": self._message,
                "patcher_path": self._patcher_path,
                "patcher_pid": self._patcher_pid,
                "classic_pid": self._classic_pid,
                "server_ip": SERVER_IP,
                "traffic_counts": counts,
                "live_state": {
                    "world": world,
                    "actor_counts": actor_counts,
                    "actors": sorted(
                        actors,
                        key=lambda a: (a.get("kind", ""), a.get("name", ""), a.get("id", 0)),
                    )[:100],
                    "aggressor_ids": aggressor_ids,
                    "floor_items": sorted(
                        floor_items,
                        key=lambda item: (item.get("y", 0), item.get("x", 0), item.get("id", 0)),
                    )[:100],
                    "parsed_packets": dict(self._parsed_counts),
                },
                "events": events[-100:],
                "note": (
                    "Observer mode only. Live Game State is decoded from the "
                    "authenticated client's normal server traffic."
                ),
            }


authenticated_client_monitor = AuthenticatedClientMonitor()
