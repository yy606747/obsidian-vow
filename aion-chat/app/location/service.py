from .geo import haversine_m, wgs84_to_gcj02
from .places import Place
from .tracker import LocationFix, LocationState, LocationTracker


class LocationService:
    def __init__(self):
        self.tracker = LocationTracker()

    def process_fix(
        self,
        *,
        lng: float,
        lat: float,
        accuracy_m: float,
        is_gcj02: bool,
        places: list[Place],
        previous_status: dict,
        now: float,
        home_place: Place | None = None,
    ) -> dict:
        if self.tracker.state is None:
            restored = state_from_payload(previous_status.get("v2_state"))
            if restored is not None:
                self.tracker = LocationTracker(state=restored)

        fix_lat, fix_lng = (float(lat), float(lng)) if is_gcj02 else wgs84_to_gcj02(lat, lng)
        fix = LocationFix(lat=fix_lat, lng=fix_lng, accuracy_m=float(accuracy_m), received_at=float(now))
        changed = self.tracker.process_fix(fix, places)
        current = self.tracker.state
        old_legacy = previous_status.get("state", "unknown")
        previous_v2 = previous_status.get("v2_state") or {}
        old_place_id = previous_v2.get("place_id")
        new_place_id = current.place_id if current else None
        new_legacy = legacy_state(current)
        legacy_changed = old_legacy != new_legacy and old_legacy != "unknown" and new_legacy != "unknown"
        distance_home = -1.0
        if home_place is not None:
            distance_home = haversine_m(fix_lat, fix_lng, home_place.lat, home_place.lng)

        status = dict(previous_status)
        status.update({
            "state": new_legacy,
            "lng": round(fix_lng, 6),
            "lat": round(fix_lat, 6),
            "accuracy": float(accuracy_m),
            "updated_at": float(now),
            "state_changed_at": float(now) if legacy_changed else previous_status.get("state_changed_at", 0),
            "distance_from_home": round(distance_home, 1) if distance_home >= 0 else -1,
            "v2_state": state_to_payload(current),
        })
        return {
            "state": new_legacy,
            "old_state": old_legacy,
            "state_changed": legacy_changed,
            "lng": round(fix_lng, 6),
            "lat": round(fix_lat, 6),
            "accuracy": float(accuracy_m),
            "distance_from_home": status["distance_from_home"],
            "home_not_set": home_place is None,
            "full_api": False,
            "moved_distance": moved_distance(previous_status, fix_lat, fix_lng),
            "v2_state_changed": changed is not None,
            "v2_fix_accepted": bool(current and current.last_fix_at == fix.received_at),
            "v2_old_place_id": old_place_id,
            "v2_new_place_id": new_place_id,
            "v2_state": status["v2_state"],
            "status": status,
        }


def state_to_payload(state: LocationState | None) -> dict | None:
    if state is None:
        return None
    return {
        "place_id": state.place_id,
        "place_name": state.place_name,
        "place_kind": state.place_kind,
        "last_fix_at": state.last_fix_at,
        "state_updated_at": state.state_updated_at,
        "accuracy_m": state.accuracy_m,
    }


def state_from_payload(payload: dict | None) -> LocationState | None:
    if not payload:
        return None
    return LocationState(
        place_id=payload.get("place_id"),
        place_name=payload.get("place_name"),
        place_kind=payload.get("place_kind"),
        last_fix_at=float(payload["last_fix_at"]),
        state_updated_at=float(payload["state_updated_at"]),
        accuracy_m=float(payload["accuracy_m"]),
    )


def legacy_state(state: LocationState | None) -> str:
    if state is None:
        return "unknown"
    if state.place_kind in ("home", "dorm"):
        return "at_home"
    if state.last_fix_at > 0:
        return "outside"
    return "unknown"


def moved_distance(previous_status: dict, lat: float, lng: float) -> float:
    old_lat = previous_status.get("lat", 0)
    old_lng = previous_status.get("lng", 0)
    if not old_lat or not old_lng:
        return -1
    return round(haversine_m(lat, lng, float(old_lat), float(old_lng)), 1)
