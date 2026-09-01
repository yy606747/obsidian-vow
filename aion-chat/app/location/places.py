import json
from dataclasses import dataclass
from pathlib import Path

from .geo import haversine_m


@dataclass
class Place:
    id: str
    name: str
    kind: str
    lat: float
    lng: float
    enter_m: float
    exit_m: float


def load_places(path) -> list[Place]:
    place_path = Path(path)
    if not place_path.exists():
        return []
    raw = json.loads(place_path.read_text(encoding="utf-8"))
    items = raw["places"] if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("places must be a list")
    places = []
    for item in items:
        place = Place(
            id=str(item["id"]).strip(),
            name=str(item["name"]).strip(),
            kind=str(item["kind"]).strip(),
            lat=float(item["lat"]),
            lng=float(item["lng"]),
            enter_m=float(item["enter_m"]),
            exit_m=float(item["exit_m"]),
        )
        if not place.id or not place.name or not place.kind:
            raise ValueError("place id, name and kind are required")
        if place.exit_m <= place.enter_m:
            raise ValueError("place exit_m must be greater than enter_m")
        places.append(place)
    return sorted(places, key=lambda place: place.enter_m)


def match_place(lat: float, lng: float, places: list[Place], current_place_id: str | None) -> str | None:
    candidates = []
    for place in places:
        distance = haversine_m(lat, lng, place.lat, place.lng)
        radius = place.exit_m if place.id == current_place_id else place.enter_m
        if distance <= radius:
            candidates.append(place)
    if not candidates:
        return None
    return min(candidates, key=lambda place: place.enter_m).id
