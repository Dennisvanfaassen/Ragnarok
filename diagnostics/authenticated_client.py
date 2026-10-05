from __future__ import annotations

import re
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
    0x0088: 10,  # actor_movement_interrupted
    0x008A: 29,  # actor_action
    0x0091: 22,  # legacy map_change
    0x0092: 28,  # legacy map_changed/server move
    0x009D: 19,  # floor item exists
    0x009E: 17,  # legacy floor item appeared
    0x00A1: 6,   # floor item disappeared
    0x00B0: 8,   # stat_info
    0x0AC7: 156, # modern map_changed (kRO 2021 family)
    0x0ADD: 24,  # modern floor item appeared
}
VARIABLE_PACKET_OPCODES = {0x09FD, 0x09FE, 0x09FF}

# OpenKore-compatible inventory/storage packet families. Different Ragexe
# generations use different list opcodes, so we trace them first and only
# enable a concrete decoder after observing the real Classic.exe traffic.
ITEM_PACKET_CANDIDATES = {
    0x00A0: "inventory_item_added_legacy",
    0x029A: "inventory_item_added_v2",
    0x0A37: "inventory_item_added_modern",
    0x0A0A: "storage_item_added",
    0x00AF: "inventory_item_removed",
    0x00F4: "storage_item_removed",
    0x00A3: "inventory_items_stackable",
    0x01EE: "inventory_items_stackable_v2",
    0x02E8: "inventory_items_stackable_v3",
    0x0900: "inventory_items_stackable_v5",
    0x0991: "inventory_items_stackable_v6",
    0x0B09: "item_list_stackable",
    0x00A4: "inventory_items_nonstackable",
    0x0295: "inventory_items_nonstackable_v2",
    0x02D0: "inventory_items_nonstackable_v3",
    0x0901: "inventory_items_nonstackable_v5",
    0x0992: "inventory_items_nonstackable_v6",
    0x0A0D: "inventory_items_nonstackable_v7",
    0x0B0A: "item_list_nonstackable",
    0x0B39: "item_list_nonstackable_v9",
    0x00A5: "storage_items_stackable",
    0x01F0: "storage_items_stackable_v2",
    0x02EA: "storage_items_stackable_v3",
    0x0975: "storage_items_stackable_v5",
    0x0995: "storage_items_stackable_v6",
    0x00A6: "storage_items_nonstackable",
    0x0296: "storage_items_nonstackable_v2",
    0x02D1: "storage_items_nonstackable_v3",
    0x0976: "storage_items_nonstackable_v5",
    0x0996: "storage_items_nonstackable_v6",
    0x0A10: "storage_items_nonstackable_v7",
}

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


_MAP_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+(?:\.(?:gat|rsw))?$")


def _clean_map_name(raw: bytes) -> str | None:
    try:
        value = raw.split(b"\x00", 1)[0].decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    if not value or len(value) > 16 or not _MAP_NAME_RE.fullmatch(value):
        return None
    value = re.sub(r"\.(?:gat|rsw)$", "", value, flags=re.IGNORECASE)
    return value or None


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
        self._client_action_trace: deque[dict[str, Any]] = deque(maxlen=200)
        self._item_packet_trace: deque[dict[str, Any]] = deque(maxlen=200)
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
            "last_client_action": None,
        }
        self._actors: dict[int, dict[str, Any]] = {}
        self._self_move: dict[str, Any] | None = None
        self._floor_items: dict[int, dict[str, Any]] = {}
        self._aggressors: dict[int, float] = {}
        self._client_action_trace.clear()
        self._item_packet_trace.clear()
        self._parsed_counts: dict[str, int] = {
            "map_change": 0,
            "invalid_map_change": 0,
            "character_moves": 0,
            "movement_interrupted": 0,
            "actor_moved": 0,
            "actor_connected": 0,
            "actor_exists": 0,
            "actor_removed": 0,
            "stat_info": 0,
            "sync": 0,
            "combat": 0,
            "client_action": 0,
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
        now = time.time()
        actor.update({
            "id": actor_id,
            "char_id": char_id,
            "object_type": object_type,
            "kind": _actor_kind(object_type),
            "last_seen": now,
            "packet": f"0x{opcode:04X}",
        })

        # 09FD (actor_moved): coords a6 begin at absolute offset 67.
        if opcode == 0x09FD and len(data) >= 73:
            movement = _coords6(data[67:73])
            if movement:
                (from_x, from_y), (to_x, to_y) = movement
                walk_speed_ms = int.from_bytes(data[13:15], "little")
                tiles = max(abs(to_x - from_x), abs(to_y - from_y), 1)
                actor.update({
                    "from_x": from_x,
                    "from_y": from_y,
                    "to_x": to_x,
                    "to_y": to_y,
                    "x": from_x,
                    "y": from_y,
                    "walk_speed_ms": walk_speed_ms,
                    "move_started_at": now,
                    "move_duration": max(
                        0.05,
                        (walk_speed_ms * tiles) / 1000.0,
                    ),
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
                actor.pop("from_x", None)
                actor.pop("from_y", None)
                actor.pop("to_x", None)
                actor.pop("to_y", None)
                actor.pop("move_started_at", None)
                actor.pop("move_duration", None)
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

        if opcode in {0x0091, 0x0092, 0x0AC7}:
            minimum = {0x0091: 22, 0x0092: 28, 0x0AC7: 156}[opcode]
            if len(data) < minimum:
                return

            map_name = _clean_map_name(data[2:18])
            if map_name is None:
                self._parsed_counts["invalid_map_change"] += 1
                return

            x, y = struct.unpack_from("<HH", data, 18)
            if not (0 <= x <= 4095 and 0 <= y <= 4095):
                self._parsed_counts["invalid_map_change"] += 1
                return

            previous_map = self._world.get("map")
            previous_x = self._world.get("x")
            previous_y = self._world.get("y")
            if (
                previous_map
                and previous_map != map_name
                and previous_x is not None
                and previous_y is not None
            ):
                try:
                    from core.world_route import world_route_planner
                    world_route_planner.learn_transition(
                        str(previous_map),
                        int(previous_x),
                        int(previous_y),
                        map_name,
                        int(x),
                        int(y),
                    )
                except Exception:
                    pass

            self._world.update({"map": map_name, "x": x, "y": y})
            self._self_move = None
            self._actors.clear()
            self._floor_items.clear()
            self._aggressors.clear()
            self._parsed_counts["map_change"] += 1
            return

        if opcode == 0x0087 and len(data) >= 12:
            movement = _coords6(data[6:12])
            if movement:
                start, destination = movement
                tiles = max(
                    abs(destination[0] - start[0]),
                    abs(destination[1] - start[1]),
                    1,
                )
                self._self_move = {
                    "from_x": start[0],
                    "from_y": start[1],
                    "to_x": destination[0],
                    "to_y": destination[1],
                    "move_started_at": time.time(),
                    # Typical client walk interpolation. This can later be
                    # refined from observed server timing if needed.
                    "move_duration": max(0.05, tiles * 0.15),
                }
                self._world["x"], self._world["y"] = start
            self._parsed_counts["character_moves"] += 1
            return

        if opcode == 0x0088 and len(data) >= 10:
            actor_id = int.from_bytes(data[2:6], "little")
            x = int.from_bytes(data[6:8], "little")
            y = int.from_bytes(data[8:10], "little")

            self_ids = {
                int(v)
                for v in (
                    self._world.get("self_account_id"),
                    self._world.get("self_char_id"),
                )
                if v is not None
            }
            if actor_id in self_ids:
                self._self_move = None
                self._world["x"] = x
                self._world["y"] = y
            else:
                actor = self._actors.get(actor_id)
                if actor is not None:
                    actor["x"] = x
                    actor["y"] = y
                    actor.pop("from_x", None)
                    actor.pop("from_y", None)
                    actor.pop("to_x", None)
                    actor.pop("to_y", None)
                    actor.pop("move_started_at", None)
                    actor.pop("move_duration", None)

            self._parsed_counts["movement_interrupted"] += 1
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

    def _trace_item_candidates(self, payload: bytes):
        size = len(payload)
        for i in range(max(0, size - 1)):
            if i + 2 > size:
                break
            opcode = int.from_bytes(payload[i:i + 2], "little")
            name = ITEM_PACKET_CANDIDATES.get(opcode)
            if name is None:
                continue

            declared_length = None
            if i + 4 <= size:
                candidate = int.from_bytes(payload[i + 2:i + 4], "little")
                if 4 <= candidate <= 65535:
                    declared_length = candidate

            sample_end = min(size, i + 96)
            self._item_packet_trace.append({
                "timestamp": time.time(),
                "opcode": f"0x{opcode:04X}",
                "name": name,
                "payload_offset": i,
                "tcp_payload_length": size,
                "declared_length": declared_length,
                "sample_hex": payload[i:sample_end].hex(" "),
            })

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

    def _parse_character_payload(self, payload: bytes):
        size = len(payload)
        i = 0
        while i + 156 <= size:
            opcode = int.from_bytes(payload[i:i + 2], "little")
            if opcode == 0x0AC5:
                packet = payload[i:i + 156]
                map_name = _clean_map_name(packet[6:22])
                if map_name:
                    self._world["map"] = map_name
                i += 156
                continue
            i += 1

    def _parse_client_payload(self, payload: bytes):
        i = 0
        size = len(payload)
        payload_timestamp = time.time()
        while i + 2 <= size:
            opcode = int.from_bytes(payload[i:i + 2], "little")

            if opcode == 0x0436 and i + 23 <= size:
                self._parse_client_map_packet(payload[i:i + 23])
                i += 23
                continue

            # 0437 actor_action: targetID a4, type C.
            # This is the strongest passive confirmation that a mouse click
            # actually registered on a specific actor in Classic.exe.
            if opcode == 0x0437 and i + 7 <= size:
                packet = payload[i:i + 7]
                target_id = int.from_bytes(packet[2:6], "little")
                action_type = int(packet[6])
                now = time.time()
                action = {
                    "timestamp": now,
                    "target_id": target_id,
                    "type": action_type,
                    "opcode": "0x0437",
                }
                self._world["last_client_action"] = action

                actor = self._actors.get(target_id)
                trace = {
                    **action,
                    "packet_hex": packet.hex(" "),
                    "packet_length": len(packet),
                    "payload_offset": i,
                    "tcp_payload_length": size,
                    "tcp_payload_hex": payload.hex(" ")[:1024],
                    "payload_timestamp": payload_timestamp,
                    "actor": (
                        {
                            "id": int(actor.get("id")),
                            "name": actor.get("name"),
                            "kind": actor.get("kind"),
                            "x": actor.get("x"),
                            "y": actor.get("y"),
                        }
                        if actor is not None else None
                    ),
                    "player": {
                        "map": self._world.get("map"),
                        "x": self._world.get("x"),
                        "y": self._world.get("y"),
                    },
                }
                self._client_action_trace.append(trace)
                self._parsed_counts["client_action"] += 1
                i += 7
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

            if payload:
                with self._lock:
                    if stage == "map":
                        if direction == "server_to_client":
                            self._trace_item_candidates(payload)
                            self._parse_payload(payload)
                        else:
                            self._parse_client_payload(payload)
                    elif stage == "character" and direction == "server_to_client":
                        self._parse_character_payload(payload)

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

    def clear_client_action_trace(self) -> dict[str, Any]:
        with self._lock:
            self._client_action_trace.clear()
        return self.client_action_trace_snapshot()

    def clear_item_packet_trace(self) -> dict[str, Any]:
        with self._lock:
            self._item_packet_trace.clear()
        return self.item_packet_trace_snapshot()

    def item_packet_trace_snapshot(self) -> dict[str, Any]:
        with self._lock:
            trace = list(self._item_packet_trace)
        return {
            "count": len(trace),
            "packets": trace,
            "note": (
                "Read-only detector for OpenKore inventory/storage packet "
                "families observed in authenticated Classic.exe traffic."
            ),
        }

    def client_action_trace_snapshot(self) -> dict[str, Any]:
        with self._lock:
            trace = list(self._client_action_trace)
        return {
            "count": len(trace),
            "actions": trace,
            "note": (
                "Read-only capture of outgoing Classic.exe actor-action packets. "
                "No packets are injected or modified."
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            counts = {"login": 0, "character": 0, "map": 0}
            for event in events:
                stage = event.get("stage")
                if stage in counts:
                    counts[stage] += 1

            now = time.time()
            actors = []
            for stored in self._actors.values():
                actor = dict(stored)
                if (
                    actor.get("move_started_at") is not None
                    and actor.get("to_x") is not None
                    and actor.get("to_y") is not None
                    and actor.get("from_x") is not None
                    and actor.get("from_y") is not None
                ):
                    duration = max(
                        0.05,
                        float(actor.get("move_duration") or 0.05),
                    )
                    progress = max(
                        0.0,
                        min(
                            1.0,
                            (now - float(actor["move_started_at"])) / duration,
                        ),
                    )
                    render_x = (
                        int(actor["from_x"])
                        + (int(actor["to_x"]) - int(actor["from_x"])) * progress
                    )
                    render_y = (
                        int(actor["from_y"])
                        + (int(actor["to_y"]) - int(actor["from_y"])) * progress
                    )
                    actor["render_x"] = round(render_x, 3)
                    actor["render_y"] = round(render_y, 3)
                    actor["x"] = int(round(render_x))
                    actor["y"] = int(round(render_y))
                    actor["move_progress"] = round(progress, 3)
                actors.append(actor)

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
            if self._self_move is not None:
                move = self._self_move
                duration = max(
                    0.05,
                    float(move.get("move_duration") or 0.05),
                )
                progress = max(
                    0.0,
                    min(
                        1.0,
                        (now - float(move["move_started_at"])) / duration,
                    ),
                )
                render_x = (
                    int(move["from_x"])
                    + (int(move["to_x"]) - int(move["from_x"])) * progress
                )
                render_y = (
                    int(move["from_y"])
                    + (int(move["to_y"]) - int(move["from_y"])) * progress
                )
                world["render_x"] = round(render_x, 3)
                world["render_y"] = round(render_y, 3)
                world["x"] = int(round(render_x))
                world["y"] = int(round(render_y))
                world["move_progress"] = round(progress, 3)
                world["move_destination"] = {
                    "x": int(move["to_x"]),
                    "y": int(move["to_y"]),
                }
                if progress >= 1.0:
                    self._world["x"] = int(move["to_x"])
                    self._world["y"] = int(move["to_y"])
                    self._self_move = None

            if world.get("render_x") is None and world.get("x") is not None:
                world["render_x"] = float(world["x"])
            if world.get("render_y") is None and world.get("y") is not None:
                world["render_y"] = float(world["y"])
            for actor in actors:
                if actor.get("render_x") is None and actor.get("x") is not None:
                    actor["render_x"] = float(actor["x"])
                if actor.get("render_y") is None and actor.get("y") is not None:
                    actor["render_y"] = float(actor["y"])

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
                "client_action_trace_count": len(self._client_action_trace),
                "item_packet_trace_count": len(self._item_packet_trace),
                "note": (
                    "Observer mode only. Live Game State is decoded from the "
                    "authenticated client's normal server traffic."
                ),
            }


authenticated_client_monitor = AuthenticatedClientMonitor()
