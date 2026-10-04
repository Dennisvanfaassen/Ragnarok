from __future__ import annotations

from pathlib import Path
import yaml

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from core.engine import engine
from core.models import BotProfile, ServerProfile
from core.state import app_state
from network.session import RagnarokSession
from diagnostics.client_scan import scan_client


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
