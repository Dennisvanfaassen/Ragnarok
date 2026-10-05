from __future__ import annotations

from pathlib import Path
import yaml

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse

from core.engine import engine
from core.models import BotProfile, ServerProfile
from core.state import app_state
from core.targeting import build_targeting_state
from core.pathing import build_pathing_state, nav_repository
from core.active_control import active_hunt_controller
from core.hotkey import hunting_hotkey
from core.world_route import world_route_planner
from core.town_travel import town_travel_controller
from core.map_dashboard import map_grid_payload, map_live_overlay
from core.hunt_routes import hunt_route_store
from core.game_actions import game_actions
from core.full_automation import full_automation_controller
from core.town_services import town_service_registry
from network.session import RagnarokSession
from diagnostics.client_scan import scan_client
from diagnostics.handshake_proxy import handshake_proxy
from diagnostics.pcap_scan import analyze_capture
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.hunt_recorder import hunting_diagnostic_recorder
from diagnostics.native_action_bridge import native_action_bridge


ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Ragnarok Bot", version="0.1.0")
hunting_hotkey.start()


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


@app.get("/api/diagnostics/authenticated-client/action-trace")
async def authenticated_client_action_trace():
    return authenticated_client_monitor.client_action_trace_snapshot()


@app.post("/api/diagnostics/authenticated-client/action-trace/clear")
async def clear_authenticated_client_action_trace():
    return authenticated_client_monitor.clear_client_action_trace()


@app.get("/api/diagnostics/authenticated-client/item-packet-trace")
async def authenticated_client_item_packet_trace():
    return authenticated_client_monitor.item_packet_trace_snapshot()


@app.post("/api/diagnostics/authenticated-client/item-packet-trace/clear")
async def clear_authenticated_client_item_packet_trace():
    return authenticated_client_monitor.clear_item_packet_trace()


@app.get("/api/items/live")
async def live_items():
    return authenticated_client_monitor.item_state_snapshot()


@app.post("/api/native-action/start")
async def start_native_action_bridge():
    try:
        return native_action_bridge.start()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/native-action/stop")
async def stop_native_action_bridge():
    return native_action_bridge.stop()


@app.get("/api/native-action")
async def native_action_state():
    return native_action_bridge.snapshot()


@app.post("/api/native-action/attack")
async def native_action_attack(payload: dict):
    try:
        actor_id = int(payload.get("actor_id"))
    except Exception:
        raise HTTPException(400, "actor_id is required")
    result = native_action_bridge.attack(actor_id)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return result


@app.post("/api/native-action/move")
async def native_action_move(payload: dict):
    try:
        x = int(payload.get("x"))
        y = int(payload.get("y"))
    except Exception:
        raise HTTPException(400, "x and y are required")
    result = native_action_bridge.move(x, y)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return result


@app.post("/api/native-action/storage/add")
async def native_action_storage_add(payload: dict):
    try:
        inventory_index = int(payload.get("inventory_index"))
        amount = int(payload.get("amount"))
    except Exception:
        raise HTTPException(400, "inventory_index and amount are required")
    result = native_action_bridge.storage_add(inventory_index, amount)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return result


@app.post("/api/native-action/interaction-trace/start")
async def native_action_interaction_trace_start(payload: dict | None = None):
    payload = payload or {}
    return native_action_bridge.start_interaction_trace(
        str(payload.get("label") or "interaction")
    )


@app.get("/api/native-action/interaction-trace")
async def native_action_interaction_trace():
    return native_action_bridge.interaction_trace()


@app.get("/api/game-actions")
async def game_actions_state():
    return game_actions.snapshot()


@app.get("/api/game-actions/direct-probe")
async def direct_action_probe():
    return game_actions.direct_action_probe()


@app.post("/api/game-actions/direct-probe/attack")
async def direct_action_attack_probe(payload: dict):
    try:
        actor_id = int(payload.get("actor_id"))
    except Exception:
        raise HTTPException(400, "actor_id is required")
    result = game_actions.dry_run_attack(actor_id)
    if not result.get("ok"):
        raise HTTPException(404, result.get("reason") or "Actor not visible")
    return result


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


@app.get("/api/pathing/live")
async def live_pathing():
    snapshot = authenticated_client_monitor.snapshot()
    profile = app_state.get_profile()
    targeting = build_targeting_state(snapshot, profile.hunt.monsters)
    pathing = build_pathing_state(snapshot, targeting, nav_repository)

    if pathing.get("path_found"):
        next_waypoint = pathing.get("next_waypoint")
        message = (
            f"Path ready: {pathing.get('path_steps')} steps"
            + (
                f", next waypoint {next_waypoint.get('x')},{next_waypoint.get('y')}"
                if next_waypoint else ""
            )
        )
        app_state.patch_runtime(
            current_action="Path ready",
            message=message,
        )

    return pathing


@app.post("/api/active-hunt/start")
async def start_active_hunt(payload: dict):
    try:
        return active_hunt_controller.start(payload)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/active-hunt/stop")
async def stop_active_hunt():
    return active_hunt_controller.stop()


@app.get("/api/active-hunt")
async def active_hunt_state():
    return active_hunt_controller.snapshot()


@app.post("/api/active-hunt/calibrate")
async def calibrate_active_hunt():
    try:
        return active_hunt_controller.start_calibration()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/active-hunt/calibration")
async def active_hunt_calibration():
    return active_hunt_controller.calibration_snapshot()


@app.post("/api/active-hunt/calibration/clear")
async def clear_active_hunt_calibration():
    return active_hunt_controller.clear_calibration()


@app.get("/api/active-hunt/hotkey")
async def active_hunt_hotkey():
    return hunting_hotkey.snapshot()


@app.get("/api/world-route/town")
async def world_route_to_town():
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    current_map = str(world.get("map") or "")
    profile = app_state.get_profile()
    preferred = str(profile.town.storage_map or "").strip() or None
    return world_route_planner.route_to_town(
        current_map,
        preferred_town=preferred,
    )


@app.get("/api/world-route")
async def world_route_state():
    return world_route_planner.snapshot()


@app.post("/api/town-travel/start")
async def start_town_travel():
    try:
        if active_hunt_controller.snapshot().get("running"):
            active_hunt_controller.stop()
        return town_travel_controller.start()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/town-travel/stop")
async def stop_town_travel():
    return town_travel_controller.stop()


@app.get("/api/town-travel")
async def town_travel_state():
    return town_travel_controller.snapshot()


@app.post("/api/full-automation/start")
async def start_full_automation():
    try:
        return full_automation_controller.start()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/full-automation/stop")
async def stop_full_automation():
    return full_automation_controller.stop()


@app.post("/api/full-automation/force-town-cycle")
async def force_full_automation_town_cycle():
    try:
        return full_automation_controller.force_town_cycle()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/full-automation")
async def full_automation_state():
    return full_automation_controller.snapshot()


@app.get("/api/town-services")
async def town_services_state():
    town_service_registry.observe_live(authenticated_client_monitor.snapshot())
    return town_service_registry.snapshot()


@app.get("/api/world-route/hunt")
async def world_route_to_hunt():
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    current_map = str(world.get("map") or "")
    target_map = str(app_state.get_profile().hunt.map or "")
    return world_route_planner.route_to_map(current_map, target_map)


@app.get("/api/map/grid")
async def live_map_grid():
    return map_grid_payload()


@app.get("/api/map/live")
async def live_map_overlay():
    return map_live_overlay()


@app.get("/api/hunt-route/current")
async def current_hunt_route():
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    return hunt_route_store.get(world.get("map"))


@app.put("/api/hunt-route/current")
async def save_current_hunt_route(payload: dict):
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    map_name = str(world.get("map") or "").strip()
    if not map_name:
        raise HTTPException(400, "Current map is not known yet.")
    try:
        return hunt_route_store.save(
            map_name,
            list(payload.get("waypoints") or []),
            mode=str(payload.get("mode") or "loop"),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/hunt-route/current")
async def delete_current_hunt_route():
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    return hunt_route_store.delete(world.get("map"))


@app.post("/api/diagnostics/hunting/start")
async def start_hunting_diagnostic():
    try:
        return hunting_diagnostic_recorder.start()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/diagnostics/hunting/stop")
async def stop_hunting_diagnostic():
    try:
        return hunting_diagnostic_recorder.stop()
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/diagnostics/hunting")
async def hunting_diagnostic_state():
    return hunting_diagnostic_recorder.snapshot()


@app.get("/api/diagnostics/hunting/download")
async def download_hunting_diagnostic():
    path = hunting_diagnostic_recorder.download_path()
    if path is None:
        raise HTTPException(404, "No diagnostic ZIP is ready yet.")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/zip",
    )
