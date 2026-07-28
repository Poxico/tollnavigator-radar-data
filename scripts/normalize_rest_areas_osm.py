#!/usr/bin/env python3
"""Normalize tiled Overpass data into conservative TollNavigator MOP candidates."""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROAD_RE = re.compile(r"\b([AS])\s*([0-9]{1,2})([A-Z]?)\b", re.IGNORECASE)
CELL_SIZE = 0.04
MAX_ROAD_DISTANCE_M = 2500.0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def element_center(element: dict[str, Any]) -> tuple[float, float] | None:
    try:
        if "lat" in element and "lon" in element:
            return float(element["lat"]), float(element["lon"])
        center = element.get("center") or {}
        if "lat" in center and "lon" in center:
            return float(center["lat"]), float(center["lon"])
        points = [
            (float(point["lat"]), float(point["lon"]))
            for point in (element.get("geometry") or [])
            if "lat" in point and "lon" in point
        ]
        if points:
            return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    except (TypeError, ValueError):
        return None
    return None


def normalize_road_ref(value: Any) -> str | None:
    match = ROAD_RE.search(str(value or "").upper())
    return f"{match.group(1).upper()}{match.group(2)}{match.group(3).upper()}" if match else None


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def point_segment_distance_m(lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat = math.radians((lat + lat1 + lat2) / 3.0)
    scale_x = 111_320.0 * max(0.2, math.cos(mean_lat))
    scale_y = 110_540.0
    px, py = lon * scale_x, lat * scale_y
    x1, y1 = lon1 * scale_x, lat1 * scale_y
    x2, y2 = lon2 * scale_x, lat2 * scale_y
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def int_tag(tags: dict[str, Any], *keys: str) -> int:
    for key in keys:
        match = re.search(r"\d+", str(tags.get(key, "")))
        if match:
            return int(match.group())
    return 0


def true_tag(tags: dict[str, Any], *keys: str) -> bool:
    truthy = {"yes", "true", "1", "designated", "customers"}
    return any(str(tags.get(key, "")).strip().lower() in truthy for key in keys)


def clean_name(tags: dict[str, Any], road: str, osm_type: str, osm_id: int) -> str:
    for key in ("name", "official_name", "loc_name", "operator"):
        value = str(tags.get(key, "")).strip()
        if value:
            return value
    return f"MOP {road} OSM {osm_type}/{osm_id}"


def iter_road_segments(elements: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for element in elements:
        if element.get("type") != "way":
            continue
        tags = element.get("tags") or {}
        if tags.get("highway") not in {"motorway", "trunk"}:
            continue
        road = normalize_road_ref(tags.get("ref"))
        if not road:
            continue
        points: list[tuple[float, float]] = []
        for point in element.get("geometry") or []:
            try:
                points.append((float(point["lat"]), float(point["lon"])))
            except (KeyError, TypeError, ValueError):
                continue
        reverse = str(tags.get("oneway", "")).lower() == "-1"
        reliable = tags.get("highway") == "motorway" or str(tags.get("oneway", "")).lower() in {"yes", "1", "true", "-1"}
        for first, second in zip(points, points[1:]):
            bearing = bearing_deg(first[0], first[1], second[0], second[1])
            if reverse:
                bearing = (bearing + 180.0) % 360.0
            yield {
                "road": road,
                "a": first,
                "b": second,
                "bearing": round(bearing, 1) if reliable else None,
                "min_lat": min(first[0], second[0]),
                "max_lat": max(first[0], second[0]),
                "min_lon": min(first[1], second[1]),
                "max_lon": max(first[1], second[1]),
            }


def grid_key(lat: float, lon: float) -> tuple[int, int]:
    return int(math.floor(lat / CELL_SIZE)), int(math.floor(lon / CELL_SIZE))


def build_segment_grid(segments: list[dict[str, Any]]) -> dict[tuple[int, int], list[int]]:
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    padding = 0.03
    for index, segment in enumerate(segments):
        y1, x1 = grid_key(segment["min_lat"] - padding, segment["min_lon"] - padding)
        y2, x2 = grid_key(segment["max_lat"] + padding, segment["max_lon"] + padding)
        for y in range(y1, y2 + 1):
            for x in range(x1, x2 + 1):
                grid[(y, x)].append(index)
    return grid


def nearest_road(lat: float, lon: float, segments: list[dict[str, Any]], grid: dict[tuple[int, int], list[int]]) -> tuple[str | None, float | None, float]:
    y, x = grid_key(lat, lon)
    candidate_indexes: set[int] = set()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            candidate_indexes.update(grid.get((y + dy, x + dx), ()))
    best_road: str | None = None
    best_bearing: float | None = None
    best_distance = float("inf")
    for index in candidate_indexes:
        segment = segments[index]
        distance = point_segment_distance_m(lat, lon, *segment["a"], *segment["b"])
        if distance < best_distance:
            best_road = segment["road"]
            best_bearing = segment["bearing"]
            best_distance = distance
    return best_road, best_bearing, best_distance


def make_candidate(element: dict[str, Any], road: str, bearing: float | None, distance: float) -> dict[str, Any]:
    tags = element.get("tags") or {}
    lat, lon = element_center(element) or (0.0, 0.0)
    osm_type = str(element.get("type", "node"))
    osm_id = int(element["id"])
    kind = str(tags.get("highway", ""))
    direction = str(tags.get("destination", "") or tags.get("direction", "")).strip() or None
    if direction and re.fullmatch(r"\d+(?:[.,]\d+)?", direction):
        direction = None
    return {
        "id": f"OSM_{osm_type.upper()}_{osm_id}",
        "name": clean_name(tags, road, osm_type, osm_id),
        "motorway": road,
        "km": None,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "direction": direction,
        "travelBearing": bearing,
        "category": "MOP funkcja komercyjna" if kind == "services" else "MOP funkcja podstawowa",
        "hasFuel": kind == "services" or true_tag(tags, "fuel"),
        "hasRestaurant": true_tag(tags, "restaurant", "fast_food"),
        "hasHotel": true_tag(tags, "hotel", "motel"),
        "hasEV": true_tag(tags, "charging_station", "charging"),
        "hasToilet": true_tag(tags, "toilets"),
        "hasShower": true_tag(tags, "shower"),
        "parkingCar": int_tag(tags, "capacity:car", "capacity"),
        "parkingTruck": int_tag(tags, "capacity:hgv", "capacity:truck"),
        "brands": [],
        "fuelBrands": [],
        "osmType": osm_type,
        "osmId": osm_id,
        "osmHighway": kind,
        "roadDistanceMeters": round(distance, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics", required=True)
    parser.add_argument("--min-count", type=int, default=100)
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    elements = [item for item in (payload.get("elements") or []) if isinstance(item, dict)]
    rests = [item for item in elements if (item.get("tags") or {}).get("highway") in {"services", "rest_area"} and element_center(item)]
    segments = list(iter_road_segments(elements))
    segment_grid = build_segment_grid(segments)

    areas: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    distances: list[float] = []
    road_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()

    for item in rests:
        center = element_center(item)
        if not center:
            continue
        road, bearing, distance = nearest_road(center[0], center[1], segments, segment_grid)
        if not road or distance > MAX_ROAD_DISTANCE_M:
            tags = item.get("tags") or {}
            unmapped.append({
                "osmType": item.get("type"),
                "osmId": item.get("id"),
                "name": tags.get("name") or tags.get("operator"),
                "lat": round(center[0], 7),
                "lon": round(center[1], 7),
                "nearestRoad": road,
                "distanceMeters": None if not math.isfinite(distance) else round(distance, 1),
            })
            continue
        areas.append(make_candidate(item, road, bearing, distance))
        distances.append(distance)
        road_counts[road] += 1
        type_counts[str((item.get("tags") or {}).get("highway"))] += 1

    areas.sort(key=lambda value: (value["motorway"], value["name"].casefold(), value["id"]))
    accepted = len(areas) >= args.min_count
    root = {
        "version": datetime.now(timezone.utc).date().isoformat(),
        "generated": utc_now(),
        "source": "OpenStreetMap / tiled Overpass candidate set",
        "count": len(areas),
        "rest_areas": areas,
    }
    diagnostics = {
        "generatedAt": utc_now(),
        "rawElementCount": len(elements),
        "rawRestObjectCount": len(rests),
        "roadSegmentCount": len(segments),
        "mappedCandidateCount": len(areas),
        "unmappedRestCount": len(unmapped),
        "mappedByOsmType": dict(sorted(type_counts.items())),
        "mappedByRoad": dict(sorted(road_counts.items())),
        "roadDistanceMeters": {
            "min": round(min(distances), 1) if distances else None,
            "max": round(max(distances), 1) if distances else None,
            "average": round(sum(distances) / len(distances), 1) if distances else None,
        },
        "sampleUnmapped": unmapped[:100],
        "threshold": args.min_count,
        "accepted": accepted,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.diagnostics).parent.mkdir(parents=True, exist_ok=True)
    Path(args.diagnostics).write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: diagnostics[key] for key in ("rawRestObjectCount", "roadSegmentCount", "mappedCandidateCount", "unmappedRestCount", "accepted")}, ensure_ascii=False))
    if not accepted:
        raise SystemExit(f"Suspiciously small normalized MOP candidate set: {len(areas)}")


if __name__ == "__main__":
    main()
