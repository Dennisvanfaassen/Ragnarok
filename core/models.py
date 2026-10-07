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
    behavior: str = "attack"  # attack | aggressor_only | ignore | teleport
    priority: int = 50        # 1 = highest priority
    min_distance: int = 0
    max_distance: int = 0     # 0 = unlimited
    loot_mode: str = "all"    # all | selected | excluded | none
    include_items: list[str] = Field(default_factory=list)
    exclude_items: list[str] = Field(default_factory=list)


class AvoidZone(BaseModel):
    map: str = ""
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""


class AttackSkillRule(BaseModel):
    enabled: bool = True
    skill_name: str = "Bash"
    skill_id: int = 5
    level: int = 1
    min_sp_percent: int = 50
    first_attack_only: bool = True
    cooldown_seconds: float = 0.0
    # Targeted melee openers such as Bash must be executed from melee range
    # before the normal attack command is allowed to start.
    range_tiles: int = 1
    post_skill_delay_seconds: float = 0.35
    monsters: list[str] = Field(default_factory=list)


class HuntSettings(BaseModel):
    map: str = ""
    monsters: list[str] = Field(default_factory=list)
    loot_all: bool = True
    loot_radius: int = 12
    monster_rules: list[MonsterLootRule] = Field(default_factory=list)
    attack_skills: list[AttackSkillRule] = Field(default_factory=list)
    teleport_item: str = "Fly Wing"
    emergency_hp_percent: int = 20
    emergency_action: str = "teleport"  # teleport | stop | none
    navigation_mode: str = "saved_or_explore"  # saved_or_explore | saved_only | explore_only
    avoid_zones: list[AvoidZone] = Field(default_factory=list)

    # Smart combat defaults: native game actions, threat-first decisions and
    # priority pre-emption. These keep hunting independent from screen clicks.
    native_only_actions: bool = True
    threat_first_combat: bool = True
    preempt_for_higher_priority_aggressor: bool = True
    loot_after_aggressors: bool = True
    exploration_frontier_bias: bool = True

    # Hunting pacing/recovery.
    loot_drop_delay_min: float = 0.40
    loot_drop_delay_max: float = 0.75
    loot_between_items_min: float = 0.18
    loot_between_items_max: float = 0.38
    unreachable_target_cooldown_seconds: float = 8.0
    failed_los_cooldown_seconds: float = 12.0
    combat_no_progress_timeout: float = 1.6

    # Natural pacing / decision continuity.
    normal_reaction_delay_min: float = 0.15
    normal_reaction_delay_max: float = 0.45
    aggressor_reaction_delay_min: float = 0.06
    aggressor_reaction_delay_max: float = 0.20
    post_kill_pause_chance: float = 0.72
    post_kill_pause_min: float = 0.15
    post_kill_pause_max: float = 0.50
    preempt_priority_gap: int = 2
    monster_memory_min_seconds: float = 1.0
    monster_memory_max_seconds: float = 3.0
    roam_pause_interval_min: float = 9.0
    roam_pause_interval_max: float = 20.0
    roam_pause_min: float = 0.30
    roam_pause_max: float = 0.90
    exploration_reconsider_distance: int = 12

    # OpenKore-inspired guard rails. Native actions are still used for
    # execution, but target selection/approach must pass navigation checks.
    attack_check_los: bool = True
    attack_wait_approach_finish: bool = True
    attack_max_route_time: float = 4.0
    attack_route_max_path_distance: int = 20
    move_giveup_seconds: float = 2.5
    loot_giveup_seconds: float = 1.8
    hunting_liveness_timeout: float = 3.0


class HealingSettings(BaseModel):
    enabled: bool = True
    item: str = ""
    # Legacy fixed threshold retained for older saved profiles. Humanized
    # healing uses the min/max range below for each new healing decision.
    hp_below_percent: int = 50
    hp_trigger_min_percent: int = 30
    hp_trigger_max_percent: int = 60
    burst_min_items: int = 1
    burst_max_items: int = 3
    burst_delay_seconds: float = 0.2
    hotkey: str = "1"
    cooldown_seconds: float = 0.9


class AspdSettings(BaseModel):
    enabled: bool = False
    item: str = "Awakening Potion"
    name_id: int | None = 656
    # EFST_ATTHASTE_POTION2. When status telemetry is available, the bot only
    # reuses the potion after this effect disappears.
    status_effect_id: int | None = 38
    reuse_minutes: float = 30.0
    restock_target: int = 4


class SupplySettings(BaseModel):
    awakening_potions: int = 4
    butterfly_wings: int = 1


class TownItemRule(BaseModel):
    item_name: str = ""
    name_id: int | None = None
    action: str = "store"  # keep | store | sell


class TownBuyRule(BaseModel):
    item_name: str = ""
    name_id: int | None = None
    target_quantity: int = 0


class TownSettings(BaseModel):
    auto_town_cycle: bool = False
    # Blank means: Butterfly Wing to the character's saved respawn point,
    # then discover/use the nearest town services on the map we actually land on.
    storage_map: str = ""
    storage_npc: str = ""
    return_weight_percent: int = 70
    return_when_out_of_meat: bool = True
    return_when_out_of_fly_wings: bool = True
    butterfly_wing_item: str = "Butterfly Wing"
    auto_nearest_services: bool = True
    return_method: str = "butterfly_wing"
    supplies: SupplySettings = Field(default_factory=SupplySettings)
    default_item_action: str = "store"  # keep | store
    item_rules: list[TownItemRule] = Field(default_factory=list)
    buy_rules: list[TownBuyRule] = Field(default_factory=lambda: [
        TownBuyRule(item_name="Awakening Potion", name_id=656, target_quantity=4),
        TownBuyRule(item_name="Butterfly Wing", name_id=602, target_quantity=1),
    ])


class BotProfile(BaseModel):
    name: str = "Default"
    server_profile: str = "soulbound"
    hunt: HuntSettings = Field(default_factory=HuntSettings)
    healing: HealingSettings = Field(default_factory=HealingSettings)
    aspd: AspdSettings = Field(default_factory=AspdSettings)
    town: TownSettings = Field(default_factory=TownSettings)


class RuntimeState(BaseModel):
    status: BotStatus = BotStatus.STOPPED
    message: str = "Ready"
    map: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    current_action: str = "Idle"
    target: Optional[str] = None
