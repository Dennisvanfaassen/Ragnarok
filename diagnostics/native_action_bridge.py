from __future__ import annotations

import threading
import time
from typing import Any

from diagnostics.authenticated_client import authenticated_client_monitor


_AGENT_SOURCE = r"""
'use strict';

let mapSocket = null;
let lastObserved = null;
const counters = {
    send: 0,
    sendto: 0,
    WSASend: 0,
    WSASendTo: 0,
    actor_actions: 0,
    move_actions: 0
};
const hooked = [];
const recentCalls = [];

function exportPtr(name) {
    const attempts = [];

    // Frida 17+ prefers the global resolver; older builds exposed the static
    // Module.getExportByName(module, name) helper.
    try {
        if (typeof Module.getGlobalExportByName === 'function') {
            const p = Module.getGlobalExportByName(name);
            if (p) {
                hooked.push(name);
                return p;
            }
        }
    } catch (e) {
        attempts.push('global:' + String(e));
    }

    try {
        if (typeof Module.getExportByName === 'function') {
            const p = Module.getExportByName('ws2_32.dll', name);
            if (p) {
                hooked.push(name);
                return p;
            }
        }
    } catch (e) {
        attempts.push('static-ws2_32:' + String(e));
    }

    // Module-instance API works on current Frida and also lets us support
    // older clients that still import winsock through wsock32.dll.
    const modules = Process.enumerateModules();
    for (const module of modules) {
        const lower = module.name.toLowerCase();
        if (lower !== 'ws2_32.dll' && lower !== 'wsock32.dll') continue;
        try {
            if (typeof module.getExportByName === 'function') {
                const p = module.getExportByName(name);
                if (p) {
                    hooked.push(name);
                    return p;
                }
            }
        } catch (e) {
            attempts.push(module.name + ':' + String(e));
        }
    }

    send({
        event: 'hook_missing',
        api: name,
        attempts: attempts,
        loaded_socket_modules: modules
            .map(m => m.name)
            .filter(n => {
                const x = n.toLowerCase();
                return x.indexOf('ws2') >= 0 || x.indexOf('wsock') >= 0;
            })
    });
    return null;
}

function readByte(ptr, offset) {
    return ptr.add(offset).readU8();
}

function rememberCall(socket, buf, length, api) {
    if (buf.isNull() || length <= 0) return;
    try {
        const sampleLen = Math.min(length, 32);
        const bytes = new Uint8Array(buf.readByteArray(sampleLen));
        const hex = Array.from(bytes)
            .map(b => ('0' + b.toString(16)).slice(-2))
            .join(' ');
        recentCalls.push({
            api: api,
            socket: socket.toString(),
            length: length,
            first_bytes_hex: hex,
            timestamp_ms: Date.now()
        });
        while (recentCalls.length > 40) recentCalls.shift();
    } catch (e) {
        send({event: 'buffer_sample_error', api: api, error: String(e)});
    }
}

function inspectBuffer(socket, buf, length, api) {
    if (buf.isNull()) return;

    rememberCall(socket, buf, length, api);

    try {
        if (
            length === 5
            && readByte(buf, 0) === 0x5f
            && readByte(buf, 1) === 0x03
        ) {
            const b0 = readByte(buf, 2);
            const b1 = readByte(buf, 3);
            const b2 = readByte(buf, 4);
            const x = (b0 << 2) | (b1 >> 6);
            const y = ((b1 & 0x3f) << 4) | (b2 >> 4);

            mapSocket = socket;
            counters.move_actions += 1;
            send({
                event: 'move_action_observed',
                data: {
                    x: x,
                    y: y,
                    socket: mapSocket.toString(),
                    api: api,
                    packet_hex: [
                        b0, b1, b2
                    ].map(b => ('0' + b.toString(16)).slice(-2)).join(' '),
                    timestamp_ms: Date.now()
                }
            });
            return;
        }

        if (length !== 7) return;
        if (readByte(buf, 0) !== 0x37 || readByte(buf, 1) !== 0x04) return;

        const type = readByte(buf, 6);
        const target =
            readByte(buf, 2) |
            (readByte(buf, 3) << 8) |
            (readByte(buf, 4) << 16) |
            (readByte(buf, 5) << 24);

        mapSocket = socket;
        counters.actor_actions += 1;
        lastObserved = {
            target_id: target >>> 0,
            type: type,
            socket: mapSocket.toString(),
            api: api,
            timestamp_ms: Date.now()
        };
        send({event: 'actor_action_observed', data: lastObserved});
    } catch (e) {
        send({event: 'agent_error', api: api, error: String(e)});
    }
}

const sendPtr = exportPtr('send');
const sendtoPtr = exportPtr('sendto');
const wsaSendPtr = exportPtr('WSASend');
const wsaSendToPtr = exportPtr('WSASendTo');

let sendFn = null;
if (sendPtr !== null) {
    sendFn = new NativeFunction(
        sendPtr,
        'int',
        ['pointer', 'pointer', 'int', 'int']
    );

    Interceptor.attach(sendPtr, {
        onEnter(args) {
            counters.send += 1;
            inspectBuffer(args[0], args[1], args[2].toInt32(), 'send');
        }
    });
}

if (sendtoPtr !== null) {
    Interceptor.attach(sendtoPtr, {
        onEnter(args) {
            counters.sendto += 1;
            inspectBuffer(args[0], args[1], args[2].toInt32(), 'sendto');
        }
    });
}

function inspectWsabufs(socket, wsabufs, count, api) {
    if (wsabufs.isNull() || count <= 0 || count > 64) return;

    const pointerSize = Process.pointerSize;
    const stride = pointerSize === 8 ? 16 : 8;
    const bufOffset = pointerSize === 8 ? 8 : 4;

    for (let i = 0; i < count; i++) {
        const entry = wsabufs.add(i * stride);
        try {
            const len = entry.readU32();
            const buf = entry.add(bufOffset).readPointer();
            inspectBuffer(socket, buf, len, api);
        } catch (e) {
            send({event: 'wsabuf_error', api: api, error: String(e)});
        }
    }
}

if (wsaSendPtr !== null) {
    Interceptor.attach(wsaSendPtr, {
        onEnter(args) {
            counters.WSASend += 1;
            inspectWsabufs(
                args[0],
                args[1],
                args[2].toInt32(),
                'WSASend'
            );
        }
    });
}

if (wsaSendToPtr !== null) {
    Interceptor.attach(wsaSendToPtr, {
        onEnter(args) {
            counters.WSASendTo += 1;
            inspectWsabufs(
                args[0],
                args[1],
                args[2].toInt32(),
                'WSASendTo'
            );
        }
    });
}

send({
    event: 'hooks_ready',
    hooked: hooked,
    pointer_size: Process.pointerSize
});

rpc.exports = {
    status() {
        return {
            socket_learned: mapSocket !== null,
            socket: mapSocket ? mapSocket.toString() : null,
            last_observed: lastObserved,
            counters: counters,
            hooked_apis: hooked,
            pointer_size: Process.pointerSize,
            recent_calls: recentCalls
        };
    },

    attack(actorId) {
        if (mapSocket === null) {
            return {
                ok: false,
                reason: 'map_socket_not_learned'
            };
        }
        if (sendFn === null) {
            return {
                ok: false,
                reason: 'send_export_unavailable'
            };
        }

        const id = Number(actorId) >>> 0;
        const learnedType = (
            lastObserved !== null
            && (lastObserved.type === 0 || lastObserved.type === 7)
        ) ? lastObserved.type : 7;
        const bytes = [
            0x37, 0x04,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff,
            learnedType
        ];

        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);

        return {
            ok: result === 7,
            bytes_sent: result,
            target_id: id,
            action_type: learnedType,
            packet_hex: bytes.map(b => ('0' + b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
    },

    pickupFloorItem(itemId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(itemId) >>> 0;
        // 0362 pTakeItem: opcode + floor item runtime ID (u32).
        const bytes = [
            0x62, 0x03,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff
        ];
        const packet = Memory.alloc(6);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 6, 0);
        return {
            ok: result === 6,
            bytes_sent: result,
            floor_item_id: id,
            packet_hex: bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
    },

    move(x, y) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};

        const px = Math.max(0, Math.min(1023, Number(x) | 0));
        const py = Math.max(0, Math.min(1023, Number(y) | 0));
        const b0 = (px >> 2) & 0xff;
        const b1 = ((px & 0x03) << 6) | ((py >> 4) & 0x3f);
        const b2 = (py & 0x0f) << 4;
        const bytes = [0x5f, 0x03, b0, b1, b2];

        const packet = Memory.alloc(5);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 5, 0);

        return {
            ok: result === 5,
            bytes_sent: result,
            x: px,
            y: py,
            packet_hex: bytes.map(b => ('0' + b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
    },

    talkNpc(actorId, type) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        const talkType = Number(type === undefined ? 1 : type) & 0xff;
        const bytes = [
            0x90, 0x00,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff,
            talkType
        ];
        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);
        return {
            ok: result === 7,
            bytes_sent: result,
            actor_id: id,
            type: talkType,
            packet_hex: bytes.map(b => ('0' + b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
    },

    continueNpc(actorId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        const bytes = [
            0xb9, 0x00,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff
        ];
        const packet = Memory.alloc(6);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 6, 0);
        return {ok:result===6, bytes_sent:result, actor_id:id, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    chooseNpcOption(actorId, option) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        const response = Number(option) & 0xff;
        const bytes = [
            0xb8, 0x00,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff,
            response
        ];
        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);
        return {ok:result===7, bytes_sent:result, actor_id:id, option:response, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    closeNpc(actorId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        const bytes = [
            0x46, 0x01,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff
        ];
        const packet = Memory.alloc(6);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 6, 0);
        return {ok:result===6, bytes_sent:result, actor_id:id, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    storageAdd(index, amount) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};

        const itemIndex = Math.max(0, Math.min(65535, Number(index) | 0));
        const qty = Math.max(0, Number(amount) >>> 0);
        const bytes = [
            0x64, 0x03,
            itemIndex & 0xff,
            (itemIndex >>> 8) & 0xff,
            qty & 0xff,
            (qty >>> 8) & 0xff,
            (qty >>> 16) & 0xff,
            (qty >>> 24) & 0xff
        ];

        const packet = Memory.alloc(8);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 8, 0);

        return {
            ok: result === 8,
            bytes_sent: result,
            inventory_index: itemIndex,
            amount: qty,
            packet_hex: bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
    },

    storageClose() {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};

        // Standard Ragnarok/OpenKore storage_close for this protocol family.
        // The server acknowledges with ZC_CLOSE_STORE (00F8).
        const bytes = [0xf7, 0x00];
        const packet = Memory.alloc(2);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 2, 0);

        return {
            ok: result === 2,
            bytes_sent: result,
            packet_hex: 'f7 00',
            socket: mapSocket.toString()
        };
    },

    itemUse(index, targetId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const itemIndex = Math.max(0, Math.min(65535, Number(index) | 0));
        const target = Number(targetId) >>> 0;
        const bytes = [
            0x39, 0x04,
            itemIndex & 0xff,
            (itemIndex >>> 8) & 0xff,
            target & 0xff,
            (target >>> 8) & 0xff,
            (target >>> 16) & 0xff,
            (target >>> 24) & 0xff
        ];
        const packet = Memory.alloc(8);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 8, 0);
        return {ok:result===8, bytes_sent:result, inventory_index:itemIndex, target_id:target, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    requestNpcBuy(actorId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        const bytes = [0xc5,0x00,id&0xff,(id>>>8)&0xff,(id>>>16)&0xff,(id>>>24)&0xff,0x00];
        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);
        return {ok:result===7, bytes_sent:result, actor_id:id, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    requestNpcSell(actorId) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const id = Number(actorId) >>> 0;
        // 00C5 type 1 requests the NPC sell list.
        const bytes = [0xc5,0x00,id&0xff,(id>>>8)&0xff,(id>>>16)&0xff,(id>>>24)&0xff,0x01];
        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);
        return {ok:result===7, bytes_sent:result, actor_id:id, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    buyBulk(items) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const rows = Array.isArray(items) ? items : [];
        if (!rows.length) return {ok:false, reason:'empty_buy_list'};
        const length = 4 + rows.length * 4;
        const bytes = [0xc8,0x00,length&0xff,(length>>>8)&0xff];
        for (const row of rows) {
            const amount = Math.max(1, Math.min(65535, Number(row.amount) | 0));
            const itemId = Math.max(0, Math.min(65535, Number(row.item_id) | 0));
            bytes.push(amount&0xff,(amount>>>8)&0xff,itemId&0xff,(itemId>>>8)&0xff);
        }
        const packet = Memory.alloc(length);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, length, 0);
        return {ok:result===length, bytes_sent:result, length:length, items:rows, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    },

    sellBulk(items) {
        if (mapSocket === null) return {ok:false, reason:'map_socket_not_learned'};
        if (sendFn === null) return {ok:false, reason:'send_export_unavailable'};
        const rows = Array.isArray(items) ? items : [];
        if (!rows.length) return {ok:false, reason:'empty_sell_list'};
        // 00C9: len + repeated inventory index (u16) + amount (u16).
        const length = 4 + rows.length * 4;
        const bytes = [0xc9,0x00,length&0xff,(length>>>8)&0xff];
        for (const row of rows) {
            const index = Math.max(0, Math.min(65535, Number(row.index) | 0));
            const amount = Math.max(1, Math.min(65535, Number(row.amount) | 0));
            bytes.push(index&0xff,(index>>>8)&0xff,amount&0xff,(amount>>>8)&0xff);
        }
        const packet = Memory.alloc(length);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, length, 0);
        return {ok:result===length, bytes_sent:result, length:length, items:rows, packet_hex:bytes.map(b=>('0'+b.toString(16)).slice(-2)).join(' '), socket:mapSocket.toString()};
    }
};
"""


class NativeActionBridge:
    """Experimental direct action bridge for the authenticated Classic.exe.

    The bridge is intentionally explicit and non-stealthy. It attaches with
    Frida only when requested by the user. One normal actor attack teaches it
    which Winsock socket Classic.exe uses for map actions. Direct test actions
    are then sent through that same socket, preserving the TCP state owned by
    Classic.exe.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = None
        self._script = None
        self._pid: int | None = None
        self._status = "stopped"
        self._message = "Native action bridge is not running."
        self._events: list[dict[str, Any]] = []
        self._trace_started_at_ms: int | None = None
        self._trace_label: str | None = None

    def _record(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append({"time": time.time(), **event})
            self._events = self._events[-100:]

    def _on_message(self, message, data) -> None:
        if message.get("type") == "send":
            payload = message.get("payload")
            if isinstance(payload, dict):
                self._record(payload)
            return
        self._record({
            "event": "frida_message",
            "message": message,
        })

    def start(self) -> dict[str, Any]:
        snapshot = authenticated_client_monitor.snapshot()
        pid = snapshot.get("classic_pid")
        if not pid:
            raise RuntimeError(
                "Classic.exe is not detected. Start Authenticated Client Mode "
                "and log into the game first."
            )

        with self._lock:
            if self._session is not None and self._pid == int(pid):
                return self.snapshot()

        self.stop()

        try:
            import frida
        except ImportError as exc:
            raise RuntimeError(
                "Frida is not installed. Run setup.bat again or install the "
                "updated requirements before starting the native action test."
            ) from exc

        try:
            # Clear stale events before loading so hook diagnostics emitted
            # during script initialization remain visible to the user.
            with self._lock:
                self._events.clear()

            session = frida.attach(int(pid))
            script = session.create_script(_AGENT_SOURCE)
            script.on("message", self._on_message)
            script.load()
        except Exception as exc:
            raise RuntimeError(
                "Could not attach the experimental native-action bridge to "
                f"Classic.exe: {exc}"
            ) from exc

        with self._lock:
            self._session = session
            self._script = script
            self._pid = int(pid)
            self._status = "attached_waiting_for_attack"
            self._message = (
                "Attached to Classic.exe. Manually attack one monster once so "
                "the bridge can learn the authenticated map socket."
            )

        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            script = self._script
            session = self._session
            self._script = None
            self._session = None
            self._pid = None

        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass

        with self._lock:
            self._status = "stopped"
            self._message = "Native action bridge is not running."
        return self.snapshot()

    def _agent_status(self) -> dict[str, Any]:
        with self._lock:
            script = self._script
        if script is None:
            return {
                "socket_learned": False,
                "socket": None,
                "last_observed": None,
            }
        try:
            return dict(script.exports_sync.status())
        except Exception as exc:
            self._record({
                "event": "status_error",
                "error": str(exc),
            })
            return {
                "socket_learned": False,
                "socket": None,
                "last_observed": None,
                "error": str(exc),
            }

    def pickup_floor_item(self, floor_item_id: int) -> dict[str, Any]:
        floor_item_id = int(floor_item_id)
        if floor_item_id < 0:
            return {
                "ok": False,
                "executed": False,
                "reason": "floor_item_id_out_of_range",
            }
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}
        if not self._agent_status().get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}
        try:
            result = dict(script.exports_sync.pickup_floor_item(floor_item_id))
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "reason": "agent_call_failed",
                "message": str(exc),
            }
        result["executed"] = bool(result.get("ok"))
        result["command"] = "pickup_floor_item"
        self._record({"event": "direct_floor_item_pickup", "result": result})
        return result

    def move(self, x: int, y: int) -> dict[str, Any]:
        x = int(x)
        y = int(y)
        if not (0 <= x <= 1023 and 0 <= y <= 1023):
            return {
                "ok": False,
                "executed": False,
                "reason": "destination_out_of_range",
                "x": x,
                "y": y,
            }

        with self._lock:
            script = self._script
        if script is None:
            return {
                "ok": False,
                "executed": False,
                "reason": "bridge_not_running",
                "x": x,
                "y": y,
            }

        status = self._agent_status()
        if not status.get("socket_learned"):
            return {
                "ok": False,
                "executed": False,
                "reason": "map_socket_not_learned",
                "x": x,
                "y": y,
            }

        before = time.time()
        try:
            result = dict(script.exports_sync.move(x, y))
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "reason": "agent_call_failed",
                "message": str(exc),
                "x": x,
                "y": y,
            }

        result.update({
            "executed": bool(result.get("ok")),
            "sent_at": before,
            "command": "move_to",
        })
        self._record({
            "event": "direct_move_sent",
            "result": result,
        })
        return result

    def _npc_call(self, method: str, actor_id: int, *args) -> dict[str, Any]:
        actor_id = int(actor_id)
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}

        status = self._agent_status()
        if not status.get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}

        try:
            fn = getattr(script.exports_sync, method)
            result = dict(fn(actor_id, *args))
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "reason": "agent_call_failed",
                "message": str(exc),
            }

        result["executed"] = bool(result.get("ok"))
        result["command"] = method
        self._record({"event": "direct_npc_action", "result": result})
        return result

    def talk_npc(self, actor_id: int, talk_type: int = 1) -> dict[str, Any]:
        return self._npc_call("talk_npc", actor_id, int(talk_type))

    def continue_npc(self, actor_id: int) -> dict[str, Any]:
        return self._npc_call("continue_npc", actor_id)

    def choose_npc_option(self, actor_id: int, option: int) -> dict[str, Any]:
        return self._npc_call("choose_npc_option", actor_id, int(option))

    def close_npc(self, actor_id: int) -> dict[str, Any]:
        return self._npc_call("close_npc", actor_id)

    def storage_add(self, inventory_index: int, amount: int) -> dict[str, Any]:
        inventory_index = int(inventory_index)
        amount = int(amount)
        if not (0 <= inventory_index <= 65535):
            return {
                "ok": False,
                "executed": False,
                "reason": "inventory_index_out_of_range",
            }
        if amount <= 0:
            return {
                "ok": False,
                "executed": False,
                "reason": "amount_must_be_positive",
            }

        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}

        status = self._agent_status()
        if not status.get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}

        try:
            result = dict(script.exports_sync.storage_add(
                inventory_index,
                amount,
            ))
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "reason": "agent_call_failed",
                "message": str(exc),
            }

        result["executed"] = bool(result.get("ok"))
        result["command"] = "storage_add"
        self._record({"event": "direct_storage_add", "result": result})
        return result

    def storage_close(self) -> dict[str, Any]:
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}
        if not self._agent_status().get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}
        try:
            result = dict(script.exports_sync.storage_close())
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "reason": "agent_call_failed",
                "message": str(exc),
            }
        result["executed"] = bool(result.get("ok"))
        result["command"] = "storage_close"
        self._record({"event": "direct_storage_close", "result": result})
        return result

    def item_use(self, inventory_index: int, target_id: int) -> dict[str, Any]:
        inventory_index = int(inventory_index)
        target_id = int(target_id)
        if not (0 <= inventory_index <= 65535):
            return {"ok": False, "executed": False, "reason": "inventory_index_out_of_range"}
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}
        if not self._agent_status().get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}
        try:
            result = dict(script.exports_sync.item_use(inventory_index, target_id))
        except Exception as exc:
            return {"ok": False, "executed": False, "reason": "agent_call_failed", "message": str(exc)}
        result["executed"] = bool(result.get("ok"))
        result["command"] = "item_use"
        self._record({"event": "direct_item_use", "result": result})
        return result

    def request_npc_buy(self, actor_id: int) -> dict[str, Any]:
        return self._npc_call("request_npc_buy", int(actor_id))

    def request_npc_sell(self, actor_id: int) -> dict[str, Any]:
        return self._npc_call("request_npc_sell", int(actor_id))

    def buy_bulk(self, items: list[dict[str, int]]) -> dict[str, Any]:
        rows = [
            {"amount": int(row["amount"]), "item_id": int(row["item_id"])}
            for row in items
            if int(row.get("amount") or 0) > 0
        ]
        if not rows:
            return {"ok": False, "executed": False, "reason": "empty_buy_list"}
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}
        if not self._agent_status().get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}
        try:
            result = dict(script.exports_sync.buy_bulk(rows))
        except Exception as exc:
            return {"ok": False, "executed": False, "reason": "agent_call_failed", "message": str(exc)}
        result["executed"] = bool(result.get("ok"))
        result["command"] = "buy_bulk"
        self._record({"event": "direct_buy_bulk", "result": result})
        return result

    def sell_bulk(self, items: list[dict[str, int]]) -> dict[str, Any]:
        rows = [
            {"index": int(row["index"]), "amount": int(row["amount"])}
            for row in items
            if int(row.get("amount") or 0) > 0
            and int(row.get("index") or -1) >= 0
        ]
        if not rows:
            return {"ok": False, "executed": False, "reason": "empty_sell_list"}
        with self._lock:
            script = self._script
        if script is None:
            return {"ok": False, "executed": False, "reason": "bridge_not_running"}
        if not self._agent_status().get("socket_learned"):
            return {"ok": False, "executed": False, "reason": "map_socket_not_learned"}
        try:
            result = dict(script.exports_sync.sell_bulk(rows))
        except Exception as exc:
            return {"ok": False, "executed": False, "reason": "agent_call_failed", "message": str(exc)}
        result["executed"] = bool(result.get("ok"))
        result["command"] = "sell_bulk"
        self._record({"event": "direct_sell_bulk", "result": result})
        return result

    def attack(self, actor_id: int) -> dict[str, Any]:
        actor_id = int(actor_id)
        snapshot = authenticated_client_monitor.snapshot()
        live = snapshot.get("live_state") or {}
        actor = next(
            (
                entry
                for entry in (live.get("actors") or [])
                if int(entry.get("id") or -1) == actor_id
                and entry.get("kind") == "monster"
            ),
            None,
        )
        if actor is None:
            return {
                "ok": False,
                "executed": False,
                "actor_id": actor_id,
                "reason": "actor_not_visible",
            }

        with self._lock:
            script = self._script
        if script is None:
            return {
                "ok": False,
                "executed": False,
                "actor_id": actor_id,
                "reason": "bridge_not_running",
            }

        status = self._agent_status()
        if not status.get("socket_learned"):
            return {
                "ok": False,
                "executed": False,
                "actor_id": actor_id,
                "reason": "map_socket_not_learned",
                "message": "Manually attack one monster once, then retry.",
            }

        before = time.time()
        try:
            result = dict(script.exports_sync.attack(actor_id))
        except Exception as exc:
            return {
                "ok": False,
                "executed": False,
                "actor_id": actor_id,
                "reason": "agent_call_failed",
                "message": str(exc),
            }

        result.update({
            "executed": bool(result.get("ok")),
            "actor": {
                "id": actor_id,
                "name": actor.get("name"),
                "x": actor.get("x"),
                "y": actor.get("y"),
            },
            "sent_at": before,
        })
        self._record({
            "event": "direct_attack_sent",
            "result": result,
        })
        return result

    def start_interaction_trace(self, label: str = "interaction") -> dict[str, Any]:
        self._trace_started_at_ms = int(time.time() * 1000)
        self._trace_label = str(label or "interaction")
        return {
            "ok": True,
            "label": self._trace_label,
            "started_at_ms": self._trace_started_at_ms,
            "message": "Interaction trace marker set.",
        }

    def interaction_trace(self) -> dict[str, Any]:
        started = self._trace_started_at_ms
        status = self._agent_status()
        calls = list(status.get("recent_calls") or [])
        if started is not None:
            calls = [
                call for call in calls
                if int(call.get("timestamp_ms") or 0) >= started
            ]
        return {
            "label": self._trace_label,
            "started_at_ms": started,
            "calls": calls,
            "count": len(calls),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            base = {
                "status": self._status,
                "message": self._message,
                "classic_pid": self._pid,
                "attached": self._session is not None,
                "events": list(self._events[-30:]),
                "experimental": True,
                "stealth": False,
            }

        agent = self._agent_status() if base["attached"] else {
            "socket_learned": False,
            "socket": None,
            "last_observed": None,
        }
        base["agent"] = agent

        if base["attached"] and agent.get("socket_learned"):
            base["status"] = "ready"
            base["message"] = (
                "Authenticated map socket learned. Direct attack test is ready."
            )
        return base


native_action_bridge = NativeActionBridge()
