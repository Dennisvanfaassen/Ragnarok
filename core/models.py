from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class BotStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTING = "connecting"
    ONLINE = "online"
    PAUSED = "paused"
    ERROR = "error"


class ServerProfile(BaseModel):
    id: str
    name: str
    host: str
    port: int
    version: int | None = None
    login_version: int | None = None
    master_version: int | None = None
    service_type: str | None = None
    server_type: str | None = None
    packet_version: str | None = None
    notes: str | None = None


class MonsterLootRule(BaseModel):
    monster: str
    enabled: bool = True
    loot_mode: str = "all"  # all | selected | excluded | none
    include_items: list[str] = Field(default_factory=list)
    exclude_items: list[str] = Field(default_factory=list)


class HuntSettings(BaseModel):
    map: str = ""
    monsters: list[str] = Field(default_factory=list)
    loot_all: bool = True
    monster_rules: list[MonsterLootRule] = Field(default_factory=list)


class HealingSettings(BaseModel):
    enabled: bool = True
    item: str = ""
    hp_below_percent: int = 50
    hotkey: str = "1"
    cooldown_seconds: float = 0.9


class SupplySettings(BaseModel):
    awakening_potions: int = 4
    butterfly_wings: int = 1


class TownSettings(BaseModel):
    # Blank means: Butterfly Wing to the character's saved respawn point,
    # then discover/use the nearest town services on the map we actually land on.
    storage_map: str = ""
    storage_npc: str = ""
    return_weight_percent: int = 70
    butterfly_wing_item: str = "Butterfly Wing"
    auto_nearest_services: bool = True
    return_method: str = "butterfly_wing"
    supplies: SupplySettings = Field(default_factory=SupplySettings)


class BotProfile(BaseModel):
    name: str = "Default"
    server_profile: str = "soulbound"
    hunt: HuntSettings = Field(default_factory=HuntSettings)
    healing: HealingSettings = Field(default_factory=HealingSettings)
    town: TownSettings = Field(default_factory=TownSettings)


class RuntimeState(BaseModel):
    status: BotStatus = BotStatus.STOPPED
    message: str = "Ready"
    map: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    current_action: str = "Idle"
    target: Optional[str] = None
