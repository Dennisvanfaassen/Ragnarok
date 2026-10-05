from __future__ import annotations

from pathlib import Path
import yaml

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse

from core.engine import engine
from core.models import BotProfile, ServerProfile
from core.state import app_state
from core.targeting import build_targeting_state
from network.session import RagnarokSession
from diagnostics.client_scan import scan_client
from diagnostics.handshake_proxy import handshake_proxy
from diagnostics.pcap_scan import analyze_capture
from diagnostics.authenticated_client import authenticated_client_monitor


ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Ragnarok Bot", version="0.1.0")


def load_server_profile(profile_id: str) -> ServerProfile:
    path = ROOT / "profiles" / f"{profile_id}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Unknown server profile: {profile_id}")
    return ServerProfile.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state():
    return app_state.get_runtime()


@app.get("/api/profile")
async def get_profile():
    return app_state.get_profile()


@app.put("/api/profile")
async def save_profile(profile: BotProfile):
    return app_state.set_profile(profile)


@app.get("/api/server/{profile_id}")
async def server_profile(profile_id: str):
    return load_server_profile(profile_id)


@app.post("/api/server/{profile_id}/probe")
async def probe_server(profile_id: str):
    profile = load_server_profile(profile_id)
    result = await RagnarokSession(profile).probe()
    return {"reachable": result.reachable, "message": result.message}


@app.post("/api/server/{profile_id}/login-probe")
async def login_probe(profile_id: str, payload: dict):
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    client_hash = str(payload.get("client_hash", "")).strip() or None

    profile = load_server_profile(profile_id)
    try:
        result = await RagnarokSession(profile).login_probe(
            username,
            password,
            client_hash_hex=client_hash,
        )
        return {
            "connected": result.connected,
            "accepted": result.accepted,
            "response_opcode": result.response_opcode,
            "response_length": result.response_length,
            "message": result.message,
            "details": result.details,
            "credentials_saved": False,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/bot/start")
async def start_bot():
    engine.start()
    return app_state.get_runtime()


@app.post("/api/bot/stop")
async def stop_bot():
    engine.stop()
    return app_state.get_runtime()


@app.post("/api/diagnostics/client")
async def client_diagnostics(payload: dict):
    exe_path = str(payload.get("exe_path", "")).strip()
    if not exe_path:
        raise HTTPException(400, "exe_path is required")
    try:
        return scan_client(exe_path)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/diagnostics/handshake/start")
async def start_handshake_proxy():
    return await handshake_proxy.start()


@app.post("/api/diagnostics/handshake/stop")
async def stop_handshake_proxy():
    return await handshake_proxy.stop()


@app.post("/api/diagnostics/handshake/clear")
async def clear_handshake_proxy():
    handshake_proxy.clear()
    return handshake_proxy.snapshot()


@app.get("/api/diagnostics/handshake")
async def handshake_state():
    return handshake_proxy.snapshot()


@app.post("/api/diagnostics/pcap")
async def analyze_pcap(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    if not (name.endswith(".pcap") or name.endswith(".pcapng")):
        raise HTTPException(400, "Upload a .pcap or .pcapng file.")

    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(400, "Capture is too large. Keep it under 100 MB.")

    try:
        return analyze_capture(data)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/diagnostics/authenticated-client/start")
async def start_authenticated_client(payload: dict):
    patcher_path = str(payload.get("patcher_path", "")).strip()
    if not patcher_path:
        raise HTTPException(400, "patcher_path is required")
    try:
        return authenticated_client_monitor.start(patcher_path)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/diagnostics/authenticated-client/stop")
async def stop_authenticated_client():
    return authenticated_client_monitor.stop()


@app.post("/api/diagnostics/authenticated-client/clear")
async def clear_authenticated_client():
    authenticated_client_monitor.clear()
    return authenticated_client_monitor.snapshot()


@app.get("/api/diagnostics/authenticated-client")
async def authenticated_client_state():
    return authenticated_client_monitor.snapshot()


@app.get("/api/targeting/live")
async def live_targeting():
    snapshot = authenticated_client_monitor.snapshot()
    profile = app_state.get_profile()
    targeting = build_targeting_state(snapshot, profile.hunt.monsters)

    selected = targeting.get("selected")
    world = (snapshot.get("live_state") or {}).get("world") or {}

    app_state.patch_runtime(
        map=world.get("map"),
        x=world.get("x"),
        y=world.get("y"),
        target=(
            f"{selected.get('name')} @ {selected.get('x')},{selected.get('y')}"
            if selected else None
        ),
        current_action=(
            "Target acquired" if selected else "Searching for target"
        ),
    )

    return targeting
