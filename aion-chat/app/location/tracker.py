from collections import Counter, deque
from dataclasses import dataclass

from .geo import haversine_m
from .places import Place, match_place

PROMPT_USABLE_SEC = 30 * 60
MAX_SWITCH_ACCURACY_M = 150.0
HEARTBEAT_INTERVAL_SEC = 10 * 60
WINDOW_SIZE = 3
REQUIRED_VOTES = 2
FORCE_EXIT_HOME_RADIUS_MULTIPLIER = 1.5
FORCE_EXIT_DEFAULT_RADIUS_MULTIPLIER = 3.0
FORCE_EXIT_HOME_MIN_DISTANCE_M = 1000.0
FORCE_EXIT_DEFAULT_MIN_DISTANCE_M = 5000.0


@dataclass
class LocationFix:
    lat: float
    lng: float
    accuracy_m: float
    received_at: float


@dataclass
class LocationState:
    place_id: str | None
    place_name: str | None
    place_kind: str | None
    last_fix_at: float
    state_updated_at: float
    accuracy_m: float

    def for_prompt(
        self,
        now: float,
        *,
        geofence_radius_m: float | None = None,
    ) -> str:
        if not self.place_id or float(now) - self.last_fix_at > PROMPT_USABLE_SEC:
            return ""
        return self._cautious_place_text(geofence_radius_m=geofence_radius_m)

    def for_sentinel(
        self,
        now: float,
        *,
        geofence_radius_m: float | None = None,
    ) -> str:
        if float(now) - self.last_fix_at > PROMPT_USABLE_SEC:
            return "定位数据已超过 30 分钟，不作为位置判断依据。"
        if not self.place_id:
            return f"当前未命中可信地点，定位精度约 {self.accuracy_m:.0f}m。"
        return self._cautious_place_text(geofence_radius_m=geofence_radius_m)

    def _cautious_place_text(self, *, geofence_radius_m: float | None) -> str:
        radius = ""
        if geofence_radius_m is not None and float(geofence_radius_m) > 0:
            radius = f"（配置半径约 {float(geofence_radius_m):.0f}m）"
        prefix = (
            f"设备定位落在你们标注过的「{self.place_name}」范围里{radius}，"
            f"定位精度约 {self.accuracy_m:.0f}m；"
        )
        if self.place_kind == "home" and geofence_radius_m is not None:
            return (
                prefix
                + "这个范围太大，分不出宿舍、健身房还是教室，"
                "也说明不了她在做什么，或者手机是不是在她身上。"
            )
        return (
            prefix
            + "这个名字只是一片范围的标签，说明不了她具体在哪、在做什么，"
            "也说明不了手机是不是在她身上。"
        )


class LocationTracker:
    def __init__(
        self,
        state: LocationState | None = None,
        *,
        max_switch_accuracy_m: float = MAX_SWITCH_ACCURACY_M,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
    ):
        self.state = state
        self._samples = deque(maxlen=WINDOW_SIZE)
        self._max_switch_accuracy_m = float(max_switch_accuracy_m)
        self._window_max_age_sec = float(heartbeat_interval_sec) * WINDOW_SIZE

    def process_fix(self, fix: LocationFix, places: list[Place]) -> LocationState | None:
        if self._should_force_exit_current_place(fix, places):
            self._samples.clear()
            self._samples.append((None, fix.received_at, fix.accuracy_m))
            self.state = self._state_for(None, places, fix)
            return self.state

        if fix.accuracy_m > self._max_switch_accuracy_m:
            return None

        self._drop_old_samples(fix.received_at)
        candidate_id = match_place(
            fix.lat,
            fix.lng,
            places,
            self.state.place_id if self.state else None,
        )
        self._samples.append((candidate_id, fix.received_at, fix.accuracy_m))

        if self.state is None:
            self.state = self._state_for(candidate_id, places, fix)
            return self.state

        votes = Counter(sample[0] for sample in self._samples)
        winner, count = votes.most_common(1)[0]
        if count < REQUIRED_VOTES:
            # 投票还没达成翻盘共识：地点不变，但这仍是一帧可信定位（已过精度门），
            # 所以刷新 last_fix_at，避免手机明明在持续上报、却被判成“位置过期”。
            self._refresh_state(fix)
            return None
        if winner == self.state.place_id:
            self._refresh_state(fix)
            return None

        self.state = self._state_for(winner, places, fix)
        return self.state

    def _drop_old_samples(self, now: float) -> None:
        while self._samples and now - self._samples[0][1] > self._window_max_age_sec:
            self._samples.popleft()

    def _refresh_state(self, fix: LocationFix) -> None:
        if self.state is None:
            return
        self.state.last_fix_at = fix.received_at
        self.state.accuracy_m = fix.accuracy_m

    def _should_force_exit_current_place(self, fix: LocationFix, places: list[Place]) -> bool:
        if self.state is None or not self.state.place_id:
            return False
        current = next((item for item in places if item.id == self.state.place_id), None)
        if current is None:
            return False
        distance = haversine_m(fix.lat, fix.lng, current.lat, current.lng)
        min_distance = (
            FORCE_EXIT_HOME_MIN_DISTANCE_M
            if current.kind == "home"
            else FORCE_EXIT_DEFAULT_MIN_DISTANCE_M
        )
        radius_multiplier = (
            FORCE_EXIT_HOME_RADIUS_MULTIPLIER
            if current.kind == "home"
            else FORCE_EXIT_DEFAULT_RADIUS_MULTIPLIER
        )
        threshold = max(
            current.exit_m + fix.accuracy_m,
            current.exit_m * radius_multiplier,
            min_distance,
        )
        return distance > threshold

    def _state_for(self, place_id: str | None, places: list[Place], fix: LocationFix) -> LocationState:
        place = next((item for item in places if item.id == place_id), None)
        return LocationState(
            place_id=place.id if place else None,
            place_name=place.name if place else None,
            place_kind=place.kind if place else None,
            last_fix_at=fix.received_at,
            state_updated_at=fix.received_at,
            accuracy_m=fix.accuracy_m,
        )
