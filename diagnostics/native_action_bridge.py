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
    actor_actions: 0
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
    if (length !== 7) return;

    try {
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
        const bytes = [
            0x37, 0x04,
            id & 0xff,
            (id >>> 8) & 0xff,
            (id >>> 16) & 0xff,
            (id >>> 24) & 0xff,
            0x07
        ];

        const packet = Memory.alloc(7);
        packet.writeByteArray(bytes);
        const result = sendFn(mapSocket, packet, 7, 0);

        return {
            ok: result === 7,
            bytes_sent: result,
            target_id: id,
            packet_hex: bytes.map(b => ('0' + b.toString(16)).slice(-2)).join(' '),
            socket: mapSocket.toString()
        };
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
