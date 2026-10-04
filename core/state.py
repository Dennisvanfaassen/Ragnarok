from __future__ import annotations

from threading import RLock
from core.models import BotProfile, RuntimeState


class AppState:
    def __init__(self) -> None:
        self._lock = RLock()
        self.runtime = RuntimeState()
        self.profile = BotProfile()

    def get_runtime(self) -> RuntimeState:
        with self._lock:
            return self.runtime.model_copy(deep=True)

    def get_profile(self) -> BotProfile:
        with self._lock:
            return self.profile.model_copy(deep=True)

    def set_profile(self, profile: BotProfile) -> BotProfile:
        with self._lock:
            self.profile = profile
            return self.profile.model_copy(deep=True)

    def patch_runtime(self, **values) -> RuntimeState:
        with self._lock:
            self.runtime = self.runtime.model_copy(update=values)
            return self.runtime.model_copy(deep=True)


app_state = AppState()
