from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

from core.models import BotProfile, RuntimeState


ROOT = Path(__file__).resolve().parent.parent
USER_DATA_DIR = ROOT / "user_data"
ACTIVE_PROFILE_PATH = USER_DATA_DIR / "active_profile.json"


class AppState:
    def __init__(self) -> None:
        self._lock = RLock()
        self.runtime = RuntimeState()
        self.profile = self._load_persisted_profile()

    def _load_persisted_profile(self) -> BotProfile:
        try:
            if ACTIVE_PROFILE_PATH.exists():
                data = json.loads(ACTIVE_PROFILE_PATH.read_text(encoding="utf-8"))
                return BotProfile.model_validate(data)
        except Exception:
            # Never prevent RO Control from starting because a local backup is
            # malformed. The UI can overwrite it with a valid profile later.
            pass
        return BotProfile()

    def _persist_profile(self, profile: BotProfile) -> None:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ACTIVE_PROFILE_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(ACTIVE_PROFILE_PATH)

    def get_runtime(self) -> RuntimeState:
        with self._lock:
            return self.runtime.model_copy(deep=True)

    def get_profile(self) -> BotProfile:
        with self._lock:
            return self.profile.model_copy(deep=True)

    def set_profile(self, profile: BotProfile) -> BotProfile:
        with self._lock:
            self.profile = profile
            self._persist_profile(profile)
            return self.profile.model_copy(deep=True)

    def patch_runtime(self, **values) -> RuntimeState:
        with self._lock:
            self.runtime = self.runtime.model_copy(update=values)
            return self.runtime.model_copy(deep=True)


app_state = AppState()
