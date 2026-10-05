from __future__ import annotations

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


class AuthenticatedClientMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=500)
        self._sniffer: AsyncSniffer | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._patcher_pid: int | None = None
        self._classic_pid: int | None = None
        self._patcher_path: str | None = None
        self._status = "stopped"
        self._message = "Not running"

    def _set_status(self, status: str, message: str):
        with self._lock:
            self._status = status
            self._message = message

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

            event = {
                "timestamp": time.time(),
                "stage": RO_PORTS[server_port],
                "direction": "server_to_client" if ip.src == SERVER_IP else "client_to_server",
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
                    f"Classic.exe detected (PID {classic_pid}). Monitoring authenticated Ragnarok traffic.",
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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            counts = {"login": 0, "character": 0, "map": 0}
            for event in events:
                stage = event.get("stage")
                if stage in counts:
                    counts[stage] += 1
            return {
                "status": self._status,
                "message": self._message,
                "patcher_path": self._patcher_path,
                "patcher_pid": self._patcher_pid,
                "classic_pid": self._classic_pid,
                "server_ip": SERVER_IP,
                "traffic_counts": counts,
                "events": events[-100:],
                "note": (
                    "Observer mode only. It does not alter packets, bypass OTP, "
                    "or modify the patcher/server configuration."
                ),
            }


authenticated_client_monitor = AuthenticatedClientMonitor()
