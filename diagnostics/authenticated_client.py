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

from core.openkore_data import item_name
from core.skill_catalog import skill_name
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
    0x00B0: 8,   # stat_info (legacy variant)
    0x00B1: 8,   # exp_zeny/stat_info
    0x07F6: 14,  # exp gained/lost (32-bit)
    0x0ACC: 18,  # exp gained/lost (64-bit)
    0x010E: 11,  # legacy skill_update
    0x0111: 39,  # legacy skill_add
    0x0196: 9,   # actor_status_active
    0x043F: 25,  # modern actor_status_active
    0x0B31: 17,  # modern skill_add
    0x0B33: 17,  # modern skill_update
    0x0AC7: 156, # modern map_changed (kRO 2021 family)
    0x0ADD: 24,  # modern floor item appeared
}
VARIABLE_PACKET_OPCODES = {0x09FD, 0x09FE, 0x09FF}
SKILL_LIST_OPCODES = {0x010F: 37, 0x0B32: 15}

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

# Confirmed modern item-list layouts from OpenKore ServerType0.
# 0B09: header = len(v), type(C), then 34-byte stackable records.
# 0B39: header = len(v), type(C), then 68-byte non-stackable type9 records.
# Incremental item packets to validate against this client. OpenKore has
# multiple historical layouts for some opcodes, so these are traced with
# strict structural checks before they are allowed to mutate live state.
INCREMENTAL_ITEM_CANDIDATES = {
    # Inventory additions across OpenKore packet generations.
    0x00A0: {"name": "inventory_item_added", "lengths": {23}},
    0x029A: {"name": "inventory_item_added", "lengths": {27}},
    0x0A0C: {"name": "inventory_item_added", "lengths": {61}},
    0x0A37: {"name": "inventory_item_added", "lengths": {57, 59, 69}},
    # Inventory removal.
    0x00AF: {"name": "inventory_item_removed", "lengths": {6}},
    # Storage additions across OpenKore packet generations.
    0x00F4: {"name": "storage_item_added", "lengths": {21}},
    0x01C4: {"name": "storage_item_added", "lengths": {22}},
    0x0A0A: {"name": "storage_item_added", "lengths": {52, 57}},
    # Storage removal.
    0x00F6: {"name": "storage_item_removed", "lengths": {8}},
}


CONFIRMED_ITEM_LIST_LAYOUTS = {
    0x0B09: {
        "name": "item_list_stackable",
        "record_len": 34,
        "kind": "stackable",
    },
    0x0B39: {
        "name": "item_list_nonstackable_v9",
        "record_len": 68,
        "kind": "nonstackable",
    },
}


# rAthena/OpenKore SP_* values carried by 00B0.
STAT_NAMES = {
    1: "base_exp",
    2: "job_exp",
    5: "hp",
    6: "hp_max",
    7: "sp",
    8: "sp_max",
    9: "status_points",
    11: "base_level",
    12: "skill_points",
    20: "zeny",
    22: "base_exp_next",
    23: "job_exp_next",
    # Ragnarok/OpenKore SP_WEIGHT and SP_MAXWEIGHT. The wire values are
    # scaled by the client, but their ratio is still the real weight percent.
    24: "weight",
    25: "weight_max",
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


def _clean_actor_name(data: bytes, offset: int) -> str:
    """Decode actor names while tolerating the observed one-byte name shift.

    SoulBound's 09FE/09FF traffic is mostly aligned at the expected offset, but
    a small subset of packets places the first ASCII character one byte earlier.
    Example observed in diagnostics: "Hydra" was parsed as "ydra". Prefer the
    one-byte-earlier candidate only when it is a clean alphabetical prefix of
    the normal candidate, so unrelated packet metadata is never treated as part
    of the name.
    """
    normal = _clean_text(data[offset:]) if len(data) > offset else ""
    previous = _clean_text(data[offset - 1:]) if offset > 0 and len(data) >= offset else ""

    if (
        previous
        and normal
        and len(previous) == len(normal) + 1
        and previous[1:] == normal
        and previous[0].isascii()
        and previous[0].isalpha()
    ):
        return previous
    return normal


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
            "base_exp": None,
            "job_exp": None,
            "zeny": None,
            "base_exp_next": None,
            "job_exp_next": None,
            "last_sync": None,
            "self_account_id": None,
            "self_char_id": None,
            "last_combat": None,
            "last_client_action": None,
            "last_client_item_use": None,
        }
        self._actors: dict[int, dict[str, Any]] = {}
        # Session-wide monster encounter history. Unlike _actors this is not
        # pruned when a monster leaves sight, so we can diagnose skipped mobs
        # and compare runtime actor IDs/names seen while walking a map.
        self._monster_encounters: dict[int, dict[str, Any]] = {}
        self._self_move: dict[str, Any] | None = None
        self._floor_items: dict[int, dict[str, Any]] = {}
        self._aggressors: dict[int, float] = {}
        self._self_attack_targets: dict[int, float] = {}
        self._monster_kill_seq = 0
        self._last_monster_kill: dict[str, Any] | None = None
        self._inventory_gain_seq = 0
        self._last_inventory_gain: dict[str, Any] | None = None
        self._inventory_gain_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._exp_gain_seq = 0
        self._exp_gain_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._last_exp_gain: dict[str, Any] | None = None
        self._inventory_baseline_ready = False
        # Item-use requests are visible immediately in Classic.exe's outgoing
        # traffic, while this server does not consistently send the matching
        # inventory-remove update. Keep a small pending acknowledgement queue
        # per inventory index so the live stack can be adjusted immediately
        # without double-subtracting if 0x00AF arrives later.
        self._pending_inventory_consumptions: dict[int, deque[float]] = {}
        self._inventory: dict[int, dict[str, Any]] = {}
        self._storage: dict[int, dict[str, Any]] = {}
        self._skills: dict[int, dict[str, Any]] = {}
        self._skills_updated_at: float | None = None
        # Skill lists are variable-length packets and may be split across TCP
        # segments. Keep only an incomplete skill-list packet (or up to the
        # final 3 bytes that could begin its header) until the next payload.
        self._skill_list_pending = b""
        self._active_statuses: dict[int, dict[str, Any]] = {}
        self._status_packets_seen = 0
        self._item_list_updated_at: dict[str, float | None] = {
            "inventory": None,
            "storage": None,
        }
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
            "skills_list": 0,
            "skill_update": 0,
            "status_active": 0,
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
                name = _clean_actor_name(data, 90)
                if name:
                    existing = str(actor.get("name") or "")
                    # Never replace a complete name with a one-character-short
                    # suffix from a later packet.
                    if not (existing and existing.endswith(name) and len(existing) == len(name) + 1):
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
                name = _clean_actor_name(data, 84)
                if name:
                    existing = str(actor.get("name") or "")
                    if not (existing and existing.endswith(name) and len(existing) == len(name) + 1):
                        actor["name"] = name

        self._actors[actor_id] = actor

        if actor.get("kind") == "monster":
            encounter = self._monster_encounters.get(actor_id, {})
            first_seen = float(encounter.get("first_seen") or now)
            packets = set(encounter.get("packets") or [])
            packets.add(f"0x{opcode:04X}")
            encounter.update({
                "actor_id": actor_id,
                "char_id": actor.get("char_id"),
                "object_type": actor.get("object_type"),
                "kind": "monster",
                "name": actor.get("name") or encounter.get("name") or "",
                "map": self._world.get("map"),
                "first_seen": first_seen,
                "last_seen": now,
                "last_x": actor.get("x"),
                "last_y": actor.get("y"),
                "packets": sorted(packets),
                "seen_updates": int(encounter.get("seen_updates") or 0) + 1,
            })
            self._monster_encounters[actor_id] = encounter

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
                self._self_attack_targets[target_id] = now
            elif target_id in self_ids:
                actor = self._actors.get(source_id)
                if actor and actor.get("kind") == "monster":
                    self._aggressors[source_id] = now
                    actor["aggressive_to_me"] = True
                    actor["last_aggression"] = now
            else:
                source_actor = self._actors.get(source_id)
                target_actor = self._actors.get(target_id)

                # Anti-KS ownership telemetry. The 008A actor-action packet
                # tells us who is fighting whom. Mark a monster as claimed by
                # another player when a visible player attacks it, or when the
                # monster is actively fighting a visible player. HuntingAI uses
                # the timestamp with a short expiry rather than trusting a
                # permanent boolean.
                if (
                    source_actor
                    and target_actor
                    and source_actor.get("kind") == "player"
                    and target_actor.get("kind") == "monster"
                ):
                    target_actor["engaged_by_other"] = True
                    target_actor["last_other_player_combat"] = now
                    target_actor["other_player_id"] = source_id
                elif (
                    source_actor
                    and target_actor
                    and source_actor.get("kind") == "monster"
                    and target_actor.get("kind") == "player"
                ):
                    source_actor["engaged_by_other"] = True
                    source_actor["last_other_player_combat"] = now
                    source_actor["other_player_id"] = target_id
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
            self._self_attack_targets.clear()
            self._parsed_counts["map_change"] += 1
            return

        if opcode == 0x0087 and len(data) >= 12:
            movement = _coords6(data[6:12])
            if movement:
                start, destination = movement
                current_x = self._world.get("x")
                current_y = self._world.get("y")
                start_jump = (
                    max(abs(start[0] - int(current_x)), abs(start[1] - int(current_y)))
                    if current_x is not None and current_y is not None
                    else 0
                )

                # A normal self-movement packet starts close to the character's
                # current position. If a malformed/misaligned packet decodes to
                # something hundreds of cells away (observed as 214,79 -> 2,935),
                # do not create a fake interpolated trajectory from it.
                if start_jump > 32:
                    self._parsed_counts["character_move_rejected"] += 1
                    self._self_move = None
                    return

                tiles = max(
                    abs(destination[0] - start[0]),
                    abs(destination[1] - start[1]),
                    1,
                )

                # Also reject impossible single movement spans. Legitimate
                # teleports/map changes are delivered through different state
                # updates and must not be represented as a walking interpolation.
                if tiles > 96:
                    self._parsed_counts["character_move_rejected"] += 1
                    self._self_move = None
                    return

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

        if opcode in {0x010E, 0x0B33}:
            if opcode == 0x010E and len(data) >= 11:
                skill_id = int.from_bytes(data[2:4], "little")
                level = int.from_bytes(data[4:6], "little")
                sp = int.from_bytes(data[6:8], "little")
                skill_range = int.from_bytes(data[8:10], "little")
                upgradable = int(data[10])
                self._set_skill(
                    skill_id, level, sp=sp, skill_range=skill_range,
                    upgradable=upgradable,
                )
            elif opcode == 0x0B33 and len(data) >= 17:
                skill_id = int.from_bytes(data[2:4], "little")
                target_type = int.from_bytes(data[4:8], "little")
                level = int.from_bytes(data[8:10], "little")
                sp = int.from_bytes(data[10:12], "little")
                skill_range = int.from_bytes(data[12:14], "little")
                upgradable = int(data[14])
                level2 = int.from_bytes(data[15:17], "little")
                self._set_skill(
                    skill_id, level, sp=sp, skill_range=skill_range,
                    target_type=target_type, upgradable=upgradable, level2=level2,
                )
            self._parsed_counts["skill_update"] += 1
            return

        if opcode in {0x0111, 0x0B31}:
            if opcode == 0x0111 and len(data) >= 39:
                skill_id = int.from_bytes(data[2:4], "little")
                target_type = int.from_bytes(data[4:8], "little")
                level = int.from_bytes(data[8:10], "little")
                sp = int.from_bytes(data[10:12], "little")
                skill_range = int.from_bytes(data[12:14], "little")
                handle = _clean_text(data[14:38])
                upgradable = int(data[38])
                self._set_skill(
                    skill_id, level, sp=sp, skill_range=skill_range,
                    target_type=target_type, upgradable=upgradable, handle=handle,
                )
            elif opcode == 0x0B31 and len(data) >= 17:
                skill_id = int.from_bytes(data[2:4], "little")
                target_type = int.from_bytes(data[4:8], "little")
                level = int.from_bytes(data[8:10], "little")
                sp = int.from_bytes(data[10:12], "little")
                skill_range = int.from_bytes(data[12:14], "little")
                upgradable = int(data[14])
                level2 = int.from_bytes(data[15:17], "little")
                self._set_skill(
                    skill_id, level, sp=sp, skill_range=skill_range,
                    target_type=target_type, upgradable=upgradable, level2=level2,
                )
            self._parsed_counts["skill_update"] += 1
            return

        if opcode in {0x0196, 0x043F}:
            if opcode == 0x0196 and len(data) >= 9:
                status_type = int.from_bytes(data[2:4], "little")
                actor_id = int.from_bytes(data[4:8], "little")
                flag = int(data[8])
                tick = None
            else:
                status_type = int.from_bytes(data[2:4], "little")
                actor_id = int.from_bytes(data[4:8], "little")
                flag = int(data[8])
                tick = int.from_bytes(data[9:13], "little") if len(data) >= 13 else None

            self_ids = {
                int(v) for v in (
                    self._world.get("self_account_id"),
                    self._world.get("self_char_id"),
                ) if v is not None
            }
            if actor_id in self_ids:
                self._status_packets_seen += 1
                if flag:
                    self._active_statuses[status_type] = {
                        "type": status_type,
                        "active": True,
                        "tick": tick,
                        "updated_at": time.time(),
                    }
                else:
                    self._active_statuses.pop(status_type, None)
                self._parsed_counts["status_active"] += 1
            return

        if opcode in {0x00B0, 0x00B1} and len(data) >= 8:
            stat_type = struct.unpack_from("<H", data, 2)[0]
            value = struct.unpack_from("<I", data, 4)[0]
            name = STAT_NAMES.get(stat_type)
            if name:
                self._world[name] = value
            self._parsed_counts["stat_info"] += 1
            return

        if opcode in {0x07F6, 0x0ACC}:
            if opcode == 0x07F6 and len(data) >= 14:
                account_id = int.from_bytes(data[2:6], "little")
                amount = int.from_bytes(data[6:10], "little", signed=True)
                exp_type = int.from_bytes(data[10:12], "little")
                flag = int.from_bytes(data[12:14], "little")
            elif opcode == 0x0ACC and len(data) >= 18:
                account_id = int.from_bytes(data[2:6], "little")
                amount = int.from_bytes(data[6:14], "little", signed=True)
                exp_type = int.from_bytes(data[14:16], "little")
                flag = int.from_bytes(data[16:18], "little")
            else:
                return

            now = time.time()
            self._exp_gain_seq += 1
            event = {
                "seq": self._exp_gain_seq,
                "timestamp": now,
                "opcode": f"0x{opcode:04X}",
                "account_id": account_id,
                "amount": int(amount),
                "type": exp_type,
                "kind": "base" if exp_type == 1 else ("job" if exp_type == 2 else "unknown"),
                "flag": flag,
            }
            self._last_exp_gain = event
            self._exp_gain_events.append(dict(event))
            self._world["last_exp_gain"] = dict(event)
            self._parsed_counts["exp_gain"] = int(self._parsed_counts.get("exp_gain") or 0) + 1
            return

        if opcode == 0x007F and len(data) >= 6:
            self._world["last_sync"] = struct.unpack_from("<I", data, 2)[0]
            self._parsed_counts["sync"] += 1
            return

        if opcode == 0x0080 and len(data) >= 7:
            actor_id = int.from_bytes(data[2:6], "little")
            vanish_type = int(data[6])
            actor = self._actors.get(actor_id)
            now = time.time()

            # ZC_NOTIFY_VANISH type 1 is an actual death. Other values are
            # ordinary disappear/teleport/out-of-sight events and must never
            # count as kills.
            if vanish_type == 1 and actor and actor.get("kind") == "monster":
                attacked_at = self._self_attack_targets.get(actor_id)
                # Slower monsters can take well over 12 seconds to die after
                # the initial attack command. Keep ownership long enough for a
                # normal sustained fight while still expiring stale targets.
                if attacked_at is not None and now - float(attacked_at) <= 60.0:
                    self._monster_kill_seq += 1
                    self._last_monster_kill = {
                        "seq": self._monster_kill_seq,
                        "timestamp": now,
                        "actor_id": actor_id,
                        "name": actor.get("name") or "Unknown monster",
                        "map": self._world.get("map"),
                        "x": actor.get("x"),
                        "y": actor.get("y"),
                    }

            self._actors.pop(actor_id, None)
            self._aggressors.pop(actor_id, None)
            self._self_attack_targets.pop(actor_id, None)
            self._parsed_counts["actor_removed"] += 1
            if vanish_type == 1:
                self._parsed_counts["actor_died"] = (
                    int(self._parsed_counts.get("actor_died") or 0) + 1
                )
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

    @staticmethod
    def _decode_confirmed_item_record(
        opcode: int,
        record: bytes,
    ) -> dict[str, Any] | None:
        if opcode == 0x0B09 and len(record) == 34:
            # OpenKore items_stackable type7:
            # a2 V C v V a16 l C
            return {
                "index": int.from_bytes(record[0:2], "little"),
                "name_id": int.from_bytes(record[2:6], "little"),
                "item_type": int(record[6]),
                "amount": int.from_bytes(record[7:9], "little"),
                "type_equip": int.from_bytes(record[9:13], "little"),
                "cards_hex": record[13:29].hex(" "),
                "expire": int.from_bytes(record[29:33], "little", signed=True),
                "identified": bool(record[33] & 0x01),
                "stackable": True,
            }

        if opcode == 0x0B39 and len(record) == 68:
            # OpenKore items_nonstackable type9:
            # a2 V C V2 a16 l v2 C a25 C3
            return {
                "index": int.from_bytes(record[0:2], "little"),
                "name_id": int.from_bytes(record[2:6], "little"),
                "item_type": int(record[6]),
                "type_equip": int.from_bytes(record[7:11], "little"),
                "equipped": int.from_bytes(record[11:15], "little"),
                "cards_hex": record[15:31].hex(" "),
                "expire": int.from_bytes(record[31:35], "little", signed=True),
                "bind_on_equip_type": int.from_bytes(record[35:37], "little"),
                "sprite_id": int.from_bytes(record[37:39], "little"),
                "num_options": int(record[39]),
                "options_hex": record[40:65].hex(" "),
                "upgrade": int(record[65]),
                "grade": int(record[66]),
                "identified": bool(record[67] & 0x01),
                "amount": 1,
                "stackable": False,
            }
        return None

    def _trace_pickup_context(self, payload: bytes):
        """Capture raw server payload around a validated floor-item removal.

        On successful pickup the server normally removes a known floor item
        and sends an inventory-add acknowledgement close to it. Rather than
        guessing the acknowledgement opcode, use the known floor-item ID as
        an anchor and preserve the surrounding bytes for discovery.
        """
        size = len(payload)
        if size < 6:
            return

        for i in range(0, size - 5):
            if int.from_bytes(payload[i:i + 2], "little") != 0x00A1:
                continue

            item_id = int.from_bytes(payload[i + 2:i + 6], "little")
            if item_id not in self._floor_items:
                continue

            start = max(0, i - 64)
            end = min(size, i + 192)
            self._item_packet_trace.append({
                "timestamp": time.time(),
                "opcode": "0x00A1",
                "name": "pickup_context",
                "kind": "raw_pickup_discovery",
                "floor_item_id": item_id,
                "payload_offset": i,
                "tcp_payload_length": size,
                "context_start": start,
                "context_end": end,
                "context_hex": payload[start:end].hex(" "),
                "note": (
                    "Validated floor-item disappearance. Nearby raw bytes are "
                    "captured to identify this client's inventory-add packet."
                ),
            })
            break

    def _trace_incremental_item_candidates(self, payload: bytes):
        size = len(payload)
        now = time.time()

        for i in range(max(0, size - 1)):
            if i + 2 > size:
                break
            opcode = int.from_bytes(payload[i:i + 2], "little")
            spec = INCREMENTAL_ITEM_CANDIDATES.get(opcode)
            if spec is None:
                continue

            for packet_len in sorted(spec["lengths"]):
                if i + packet_len > size:
                    continue
                packet = payload[i:i + packet_len]

                decoded: dict[str, Any] | None = None
                if opcode == 0x00AF and packet_len == 6:
                    index = int.from_bytes(packet[2:4], "little")
                    amount = int.from_bytes(packet[4:6], "little")
                    if index in self._inventory and amount > 0:
                        decoded = {
                            "index": index,
                            "amount": amount,
                        }

                elif opcode == 0x00F6 and packet_len == 8:
                    index = int.from_bytes(packet[2:4], "little")
                    amount = int.from_bytes(packet[4:8], "little")
                    if index in self._storage and amount > 0:
                        decoded = {
                            "index": index,
                            "amount": amount,
                        }

                elif opcode == 0x0A37 and packet_len in {57, 69}:
                    index = int.from_bytes(packet[2:4], "little")
                    amount = int.from_bytes(packet[4:6], "little")
                    # 57-byte variants use a 16-bit nameID; 69-byte expanded
                    # item-ID variants use a 32-bit nameID.
                    if packet_len == 69:
                        name_id = int.from_bytes(packet[6:10], "little")
                        identified = int(packet[10])
                        item_type = int(packet[33])
                        fail = int(packet[34])
                    else:
                        name_id = int.from_bytes(packet[6:8], "little")
                        identified = int(packet[8])
                        # The remaining fields vary by generation; don't
                        # over-decode until this variant is observed.
                        item_type = None
                        fail = int(packet[-1]) if packet else 255

                    if (
                        index > 0
                        and amount > 0
                        and name_id > 0
                        and identified <= 7
                        and fail in {0, 1}
                    ):
                        decoded = {
                            "index": index,
                            "amount": amount,
                            "name_id": name_id,
                            "name": item_name(name_id),
                            "item_type": item_type,
                            "identified_raw": identified,
                            "fail": fail,
                        }

                elif opcode == 0x0A0A and packet_len in {52, 57}:
                    index = int.from_bytes(packet[2:4], "little")
                    amount = int.from_bytes(packet[4:8], "little")
                    if packet_len == 57:
                        name_id = int.from_bytes(packet[8:12], "little")
                        item_type = int(packet[12])
                        identified = int(packet[13])
                    else:
                        name_id = int.from_bytes(packet[8:10], "little")
                        item_type = int(packet[10])
                        identified = int(packet[11])

                    if (
                        index > 0
                        and amount > 0
                        and name_id > 0
                        and identified <= 7
                    ):
                        decoded = {
                            "index": index,
                            "amount": amount,
                            "name_id": name_id,
                            "name": item_name(name_id),
                            "item_type": item_type,
                            "identified_raw": identified,
                        }

                if decoded is None:
                    # Discovery fallback: keep a bounded raw sample for known
                    # OpenKore incremental item opcodes whose exact layout has
                    # not yet been confirmed for this client. This is marked
                    # unvalidated and never mutates live item state.
                    if opcode in {
                        0x00A0, 0x029A, 0x0A0C, 0x0A37,
                        0x00F4, 0x01C4, 0x0A0A,
                    }:
                        self._item_packet_trace.append({
                            "timestamp": now,
                            "opcode": f"0x{opcode:04X}",
                            "name": str(spec["name"]),
                            "kind": "incremental_discovery",
                            "candidate_packet_length": packet_len,
                            "payload_offset": i,
                            "tcp_payload_length": size,
                            "packet_hex": packet.hex(" "),
                            "note": "Known OpenKore item opcode; layout not yet validated for this Classic.exe.",
                        })
                        break
                    continue

                # Apply only layouts that passed the strict validation above.
                # This keeps the live inventory/storage snapshot in sync after
                # native town-cycle actions instead of leaving the original
                # full-list snapshot stale.
                if opcode == 0x0A37:
                    index = int(decoded["index"])
                    amount = int(decoded["amount"])
                    existing = self._inventory.get(index)
                    if (
                        existing is not None
                        and int(existing.get("name_id") or -1)
                        == int(decoded.get("name_id") or -2)
                    ):
                        row = dict(existing)
                        row["amount"] = int(existing.get("amount") or 0) + amount
                        row["name"] = decoded.get("name") or row.get("name")
                        self._inventory[index] = row
                    else:
                        self._inventory[index] = dict(decoded)

                    self._item_list_updated_at["inventory"] = now
                    self._inventory_gain_seq += 1
                    self._last_inventory_gain = {
                        "seq": self._inventory_gain_seq,
                        "timestamp": now,
                        "index": index,
                        "amount": amount,
                        "name_id": decoded.get("name_id"),
                        "name": decoded.get("name"),
                    }
                    self._inventory_gain_events.append(
                        dict(self._last_inventory_gain)
                    )

                elif opcode == 0x00AF:
                    index = int(decoded["index"])
                    removed = int(decoded["amount"])

                    # A matching outgoing 0x0439 item-use may already have
                    # reduced the local shadow stack. Consume those pending
                    # acknowledgements first so the same use is never applied
                    # twice when the server does send 0x00AF.
                    pending = self._pending_inventory_consumptions.get(index)
                    acknowledged = 0
                    if pending:
                        # Discard ancient entries; a delayed packet after this
                        # window should be treated as a fresh authoritative
                        # removal instead of an acknowledgement.
                        while pending and now - float(pending[0]) > 10.0:
                            pending.popleft()
                        acknowledged = min(removed, len(pending))
                        for _ in range(acknowledged):
                            pending.popleft()
                        if not pending:
                            self._pending_inventory_consumptions.pop(index, None)

                    unapplied = max(0, removed - acknowledged)
                    current = self._inventory.get(index)
                    if current is not None and unapplied > 0:
                        remaining = max(
                            0,
                            int(current.get("amount") or 0) - unapplied,
                        )
                        if remaining <= 0:
                            self._inventory.pop(index, None)
                        else:
                            current = dict(current)
                            current["amount"] = remaining
                            self._inventory[index] = current

                    if current is not None or acknowledged > 0:
                        self._item_list_updated_at["inventory"] = now

                elif opcode == 0x0A0A:
                    index = int(decoded["index"])
                    amount = int(decoded["amount"])
                    existing = self._storage.get(index)
                    if existing is not None and int(existing.get("name_id") or -1) == int(decoded.get("name_id") or -2):
                        row = dict(existing)
                        row["amount"] = int(existing.get("amount") or 0) + amount
                        self._storage[index] = row
                    else:
                        self._storage[index] = dict(decoded)
                    self._item_list_updated_at["storage"] = now

                elif opcode == 0x00F6:
                    index = int(decoded["index"])
                    removed = int(decoded["amount"])
                    current = self._storage.get(index)
                    if current is not None:
                        remaining = max(0, int(current.get("amount") or 0) - removed)
                        if remaining <= 0:
                            self._storage.pop(index, None)
                        else:
                            current = dict(current)
                            current["amount"] = remaining
                            self._storage[index] = current
                        self._item_list_updated_at["storage"] = now

                self._item_packet_trace.append({
                    "timestamp": now,
                    "opcode": f"0x{opcode:04X}",
                    "name": str(spec["name"]),
                    "kind": "incremental",
                    "packet_length": packet_len,
                    "payload_offset": i,
                    "tcp_payload_length": size,
                    "decoded": decoded,
                    "packet_hex": packet.hex(" "),
                })
                break

    def _trace_item_candidates(self, payload: bytes):
        """Trace only structurally valid modern item-list packets.

        Earlier versions scanned every byte for any historical OpenKore item
        opcode, which produced many false positives inside unrelated payload
        data. A candidate is now accepted only when its declared packet length
        fits this TCP payload and its body is exactly divisible by the OpenKore
        record size for that opcode.
        """
        size = len(payload)
        for i in range(max(0, size - 4)):
            if i + 5 > size:
                break

            opcode = int.from_bytes(payload[i:i + 2], "little")
            layout = CONFIRMED_ITEM_LIST_LAYOUTS.get(opcode)
            if layout is None:
                continue

            declared_length = int.from_bytes(payload[i + 2:i + 4], "little")
            record_len = int(layout["record_len"])
            body_len = declared_length - 5

            if declared_length < 5:
                continue
            if i + declared_length > size:
                continue
            if body_len < 0 or body_len % record_len != 0:
                continue

            packet = payload[i:i + declared_length]
            list_type = int(packet[4])
            items = []
            body = packet[5:]
            for offset in range(0, len(body), record_len):
                decoded = self._decode_confirmed_item_record(
                    opcode,
                    body[offset:offset + record_len],
                )
                if decoded is not None:
                    items.append(decoded)

            for item in items:
                item["name"] = item_name(int(item["name_id"]))

            list_name = {
                0: "inventory",
                2: "storage",
            }.get(list_type)

            if list_name == "inventory":
                now = time.time()

                # Compare the incoming inventory list with the previously known
                # quantities by item ID. Some servers refresh stack amounts via
                # a full item-list packet rather than a dedicated incremental
                # add packet. Treat positive deltas as confirmed item gains.
                previous_totals: dict[int, int] = {}
                for row in self._inventory.values():
                    try:
                        name_id = int(row.get("name_id") or 0)
                    except Exception:
                        name_id = 0
                    if name_id > 0:
                        previous_totals[name_id] = (
                            int(previous_totals.get(name_id) or 0)
                            + int(row.get("amount") or 0)
                        )

                incoming_totals: dict[int, int] = {}
                incoming_names: dict[int, str] = {}
                for item in items:
                    name_id = int(item.get("name_id") or 0)
                    if name_id <= 0:
                        continue
                    incoming_totals[name_id] = (
                        int(incoming_totals.get(name_id) or 0)
                        + int(item.get("amount") or 0)
                    )
                    incoming_names[name_id] = str(
                        item.get("name") or item_name(name_id)
                    )

                if self._inventory_baseline_ready:
                    for name_id, new_total in incoming_totals.items():
                        gained = int(new_total) - int(previous_totals.get(name_id) or 0)
                        if gained <= 0:
                            continue
                        self._inventory_gain_seq += 1
                        gain_event = {
                            "seq": self._inventory_gain_seq,
                            "timestamp": now,
                            "index": None,
                            "amount": gained,
                            "name_id": name_id,
                            "name": incoming_names.get(name_id) or item_name(name_id),
                            "source": "inventory_list_delta",
                        }
                        self._last_inventory_gain = gain_event
                        self._inventory_gain_events.append(dict(gain_event))

                for item in items:
                    self._inventory[int(item["index"])] = dict(item)
                # A complete inventory list is authoritative and supersedes
                # all optimistic item-use adjustments made since the previous
                # list. Any later 0x00AF must therefore be treated normally.
                self._pending_inventory_consumptions.clear()
                self._inventory_baseline_ready = True
                self._item_list_updated_at["inventory"] = now
            elif list_name == "storage":
                for item in items:
                    self._storage[int(item["index"])] = dict(item)
                self._item_list_updated_at["storage"] = time.time()

            self._item_packet_trace.append({
                "timestamp": time.time(),
                "opcode": f"0x{opcode:04X}",
                "name": str(layout["name"]),
                "kind": str(layout["kind"]),
                "payload_offset": i,
                "tcp_payload_length": size,
                "declared_length": declared_length,
                "list_type": list_type,
                "list_name": list_name,
                "record_length": record_len,
                "item_count": len(items),
                "items": items,
                "packet_hex_prefix": packet[:96].hex(" "),
            })

    def _set_skill(
        self,
        skill_id: int,
        level: int,
        *,
        sp: int | None = None,
        skill_range: int | None = None,
        target_type: int | None = None,
        upgradable: int | None = None,
        level2: int | None = None,
        handle: str | None = None,
    ) -> None:
        if skill_id <= 0:
            return
        row = dict(self._skills.get(int(skill_id), {}))
        row.update({
            "id": int(skill_id),
            "name": skill_name(int(skill_id)),
            "level": int(level),
        })
        if sp is not None:
            row["sp"] = int(sp)
        if skill_range is not None:
            row["range"] = int(skill_range)
        if target_type is not None:
            row["target_type"] = int(target_type)
        if upgradable is not None:
            row["upgradable"] = bool(upgradable)
        if level2 is not None:
            row["level2"] = int(level2)
        if handle:
            row["handle"] = handle
        row["updated_at"] = time.time()
        self._skills[int(skill_id)] = row
        self._skills_updated_at = time.time()

    def _parse_skill_list_packets(self, payload: bytes) -> None:
        """Parse complete skill lists, preserving packets split by TCP.

        Scapy exposes TCP payloads per captured segment, while Ragnarok's
        variable-length 010F/0B32 packet can span more than one segment. The
        previous parser dropped such packets when the declared packet length
        exceeded the current payload length. Preserve the incomplete packet
        and prepend it to the next server payload instead.
        """
        pending = self._skill_list_pending
        data = pending + payload if pending else payload
        self._skill_list_pending = b""

        size = len(data)
        i = 0
        while i + 4 <= size:
            opcode = int.from_bytes(data[i:i + 2], "little")
            record_len = SKILL_LIST_OPCODES.get(opcode)
            if record_len is None:
                i += 1
                continue

            declared = int.from_bytes(data[i + 2:i + 4], "little")
            if (
                declared < 4
                or declared > 8192
                or (declared - 4) % record_len != 0
            ):
                i += 1
                continue

            # Valid skill-list header, but the body continues in a later TCP
            # segment. Keep it intact instead of scanning the partial body as
            # unrelated packet data.
            if i + declared > size:
                self._skill_list_pending = data[i:]
                return

            packet = data[i:i + declared]
            parsed: dict[int, dict[str, Any]] = {}
            for off in range(4, declared, record_len):
                rec = packet[off:off + record_len]
                if opcode == 0x0B32:
                    # OpenKore 0B32: v V v3 C v
                    skill_id = int.from_bytes(rec[0:2], "little")
                    target_type = int.from_bytes(rec[2:6], "little")
                    level = int.from_bytes(rec[6:8], "little")
                    sp = int.from_bytes(rec[8:10], "little")
                    skill_range = int.from_bytes(rec[10:12], "little")
                    upgradable = int(rec[12])
                    level2 = int.from_bytes(rec[13:15], "little")
                    handle = None
                else:
                    # OpenKore 010F: v V v3 Z24 C
                    skill_id = int.from_bytes(rec[0:2], "little")
                    target_type = int.from_bytes(rec[2:6], "little")
                    level = int.from_bytes(rec[6:8], "little")
                    sp = int.from_bytes(rec[8:10], "little")
                    skill_range = int.from_bytes(rec[10:12], "little")
                    handle = _clean_text(rec[12:36])
                    upgradable = int(rec[36])
                    level2 = None

                if skill_id <= 0 or level < 0 or level > 200:
                    continue
                parsed[skill_id] = {
                    "id": skill_id,
                    "name": skill_name(skill_id),
                    "level": level,
                    "sp": sp,
                    "range": skill_range,
                    "target_type": target_type,
                    "upgradable": bool(upgradable),
                    "updated_at": time.time(),
                }
                if level2 is not None:
                    parsed[skill_id]["level2"] = level2
                if handle:
                    parsed[skill_id]["handle"] = handle

            if parsed:
                self._skills = parsed
                self._skills_updated_at = time.time()
                self._parsed_counts["skills_list"] += 1
            i += declared

        # A skill-list opcode or its two-byte length field can itself straddle
        # a TCP boundary. Retain only enough trailing bytes to reconstruct that
        # header on the next call. Do not retain arbitrary payload history.
        if size:
            self._skill_list_pending = data[max(0, size - 3):]

    def character_snapshot(self) -> dict[str, Any]:
        with self._lock:
            world = dict(self._world)
            skills = [
                dict(row)
                for _, row in sorted(self._skills.items())
                if int(row.get("level") or 0) > 0
            ]
            statuses = [dict(row) for _, row in sorted(self._active_statuses.items())]
            updated_at = self._skills_updated_at
            status_packets_seen = self._status_packets_seen
        return {
            "base_level": world.get("base_level"),
            "skill_points": world.get("skill_points"),
            "hp": world.get("hp"),
            "hp_max": world.get("hp_max"),
            "sp": world.get("sp"),
            "sp_max": world.get("sp_max"),
            "base_exp": world.get("base_exp"),
            "job_exp": world.get("job_exp"),
            "zeny": world.get("zeny"),
            "base_exp_next": world.get("base_exp_next"),
            "job_exp_next": world.get("job_exp_next"),
            "skills": skills,
            "skill_count": len(skills),
            "skills_updated_at": updated_at,
            "skills_packet_pending_bytes": len(self._skill_list_pending),
            "active_statuses": statuses,
            "status_packets_seen": status_packets_seen,
        }

    def status_active(self, status_type: int) -> bool | None:
        with self._lock:
            if self._status_packets_seen <= 0:
                return None
            return int(status_type) in self._active_statuses

    def _parse_payload(self, payload: bytes):
        """Extract Live Game State packets from a TCP payload.

        TCP segment boundaries are not Ragnarok packet boundaries. For the
        observer we scan for the small set of packet types we understand and
        validate their lengths before decoding them. This is deliberately
        read-only and does not modify the client stream.
        """
        self._parse_skill_list_packets(payload)

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

            # 0439 item_use: inventory index a2, targetID a4.
            # This is emitted for manual player item use as well as our native
            # assistant actions, so session telemetry can count consumables in
            # both manual and automated play.
            if opcode == 0x0439 and i + 8 <= size:
                packet = payload[i:i + 8]
                inventory_index = int.from_bytes(packet[2:4], "little")
                target_id = int.from_bytes(packet[4:8], "little")
                now = time.time()
                row = self._inventory.get(inventory_index)

                amount_before = (
                    int(row.get("amount") or 0)
                    if row is not None
                    else None
                )
                amount_after = amount_before
                shadow_adjusted = False

                if row is not None and amount_before is not None and amount_before > 0:
                    amount_after = amount_before - 1
                    updated = dict(row)
                    if amount_after <= 0:
                        self._inventory.pop(inventory_index, None)
                    else:
                        updated["amount"] = amount_after
                        self._inventory[inventory_index] = updated

                    pending = self._pending_inventory_consumptions.setdefault(
                        inventory_index,
                        deque(),
                    )
                    pending.append(now)
                    shadow_adjusted = True
                    self._item_list_updated_at["inventory"] = now

                self._world["last_client_item_use"] = {
                    "timestamp": now,
                    "inventory_index": inventory_index,
                    "target_id": target_id,
                    "name_id": int(row.get("name_id") or 0) if row else None,
                    "name": row.get("name") if row else None,
                    "amount_before": amount_before,
                    "amount_after": amount_after,
                    "shadow_adjusted": shadow_adjusted,
                }
                self._parsed_counts["client_item_use"] = (
                    int(self._parsed_counts.get("client_item_use") or 0) + 1
                )
                i += 8
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

                # Type 7 is the normal attack action. Record it as the
                # authoritative "I attacked this actor" signal for session
                # kill tracking. This works for manual play as well as bot
                # attacks because both originate from Classic.exe.
                if action_type == 7:
                    self._self_attack_targets[target_id] = now

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
                            self._trace_pickup_context(payload)
                            self._trace_item_candidates(payload)
                            self._trace_incremental_item_candidates(payload)
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

    def item_state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            inventory = [
                dict(item)
                for _, item in sorted(self._inventory.items())
            ]
            storage = [
                dict(item)
                for _, item in sorted(self._storage.items())
            ]
            updated = dict(self._item_list_updated_at)

        return {
            "inventory": inventory,
            "storage": storage,
            "counts": {
                "inventory_entries": len(inventory),
                "storage_entries": len(storage),
                "inventory_amount": sum(int(i.get("amount") or 0) for i in inventory),
                "storage_amount": sum(int(i.get("amount") or 0) for i in storage),
            },
            "updated_at": updated,
            "list_types": {
                "0": "inventory",
                "2": "storage",
            },
            "source": "authenticated Classic.exe item-list packets + OpenKore item names",
        }

    def monster_encounter_history_snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(row) for row in self._monster_encounters.values()]

        rows.sort(
            key=lambda row: (
                str(row.get("name") or "").casefold(),
                int(row.get("actor_id") or 0),
            )
        )

        by_name: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("name") or "").strip() or "<unknown>"
            key = name.casefold()
            bucket = by_name.setdefault(
                key,
                {
                    "name": name,
                    "actor_ids": [],
                    "char_ids": [],
                    "count": 0,
                    "unknown_name_count": 0,
                    "maps": [],
                },
            )
            bucket["actor_ids"].append(row.get("actor_id"))
            if row.get("char_id") is not None and row.get("char_id") not in bucket["char_ids"]:
                bucket["char_ids"].append(row.get("char_id"))
            bucket["count"] += 1
            if not str(row.get("name") or "").strip():
                bucket["unknown_name_count"] += 1
            map_name = row.get("map")
            if map_name and map_name not in bucket["maps"]:
                bucket["maps"].append(map_name)

        return {
            "count": len(rows),
            "encounters": rows,
            "by_name": sorted(by_name.values(), key=lambda row: str(row["name"]).casefold()),
            "note": (
                "actor_id is the runtime ID of one spawned monster and normally differs "
                "between individual spawns. Matching in RO Control is based on monster name, "
                "not one fixed actor_id."
            ),
        }

    def clear_monster_encounter_history(self) -> dict[str, Any]:
        with self._lock:
            self._monster_encounters.clear()
        return self.monster_encounter_history_snapshot()

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
            weight = world.get("weight")
            weight_max = world.get("weight_max")
            world["weight_percent"] = (
                round(weight * 100 / weight_max, 1)
                if weight is not None and weight_max
                else None
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
                    "monster_kill_seq": self._monster_kill_seq,
                    "last_monster_kill": (
                        dict(self._last_monster_kill)
                        if self._last_monster_kill is not None
                        else None
                    ),
                    "inventory_gain_seq": self._inventory_gain_seq,
                    "last_inventory_gain": (
                        dict(self._last_inventory_gain)
                        if self._last_inventory_gain is not None
                        else None
                    ),
                    "inventory_gain_events": [
                        dict(row) for row in self._inventory_gain_events
                    ],
                    "exp_gain_seq": self._exp_gain_seq,
                    "last_exp_gain": (
                        dict(self._last_exp_gain)
                        if self._last_exp_gain is not None
                        else None
                    ),
                    "exp_gain_events": [
                        dict(row) for row in self._exp_gain_events
                    ],
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
                    "inventory": [
                        dict(item)
                        for _, item in sorted(self._inventory.items())
                    ],
                    "storage": [
                        dict(item)
                        for _, item in sorted(self._storage.items())
                    ],
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
