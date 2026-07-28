#!/usr/bin/env python3
"""Generate a durable, human-readable report for the TollNavigator MOP update.

The report intentionally distinguishes:
- raw objects returned by Overpass,
- normalized fresh records produced by the OSM parser,
- the final database after conservative merging with the previous safe database.

Matching figures are diagnostic estimates. The merge script remains the source of truth
for the final database.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

IMPORTANT_FIELDS = (
    "name",
    "motorway",
    "km",
    "lat",
    "lon",
    "direction",
    "travelBearing",
    "category",
    "hasFuel",
    "hasRestaurant",
    "hasHotel",
    "hasEV",
    "hasToilet",
    "hasShower",
    "parkingCar",
    "parkingTruck",
    "brands",
    "fuelBrands",
)
SERVICE_FIELDS = (
    "hasFuel",
    "hasRestaurant",
    "hasHotel",
    "hasEV",
    "hasToilet",
    "hasShower",
)


def load_json(path: str | None) -> Any | None:
    if not path:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}"}


def records(payload: Any | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("rest_areas", "restAreas", "items", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def stable_id(item: dict[str, Any]) -> str | None:
    for key in ("id", "restAreaId", "rest_area_id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def osm_identity(item: dict[str, Any]) -> str | None:
    candidates: list[Any] = []
    for key in ("osmId", "osm_id", "sourceId", "source_id", "osmElementId"):
        candidates.append(item.get(key))
    source = item.get("source")
    if isinstance(source, dict):
        candidates.extend(source.get(key) for key in ("osmId", "osm_id", "id", "elementId"))
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip().lower()
        if text:
            return text
    return None


def number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = item.get(key)
        try:
            if value is not None and str(value).strip() != "":
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def coordinates(item: dict[str, Any]) -> tuple[float, float] | None:
    lat = number(item, "lat", "latitude")
    lon = number(item, "lon", "lng", "longitude")
    if lat is None or lon is None:
        center = item.get("center")
        if isinstance(center, dict):
            lat = number(center, "lat", "latitude")
            lon = number(center, "lon", "lng", "longitude")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def road(item: dict[str, Any]) -> str:
    value = item.get("motorway") or item.get("road") or item.get("ref") or ""
    return re.sub(r"\s+", "", str(value).upper())


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6_371_000.0
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def comparable(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    if isinstance(value, float):
        return round(value, 7)
    return value


def diff_fields(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for field in IMPORTANT_FIELDS:
        old = comparable(before.get(field))
        new = comparable(after.get(field))
        if old != new:
            changes[field] = {"before": old, "after": new}
    return changes


def match_fresh_to_previous(
    previous: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[int], set[int]]:
    prev_by_id = {stable_id(item): index for index, item in enumerate(previous) if stable_id(item)}
    prev_by_osm = {osm_identity(item): index for index, item in enumerate(previous) if osm_identity(item)}
    used_prev: set[int] = set()
    used_fresh: set[int] = set()
    matches: list[dict[str, Any]] = []

    for fresh_index, fresh_item in enumerate(fresh):
        candidate: int | None = None
        method = ""
        item_id = stable_id(fresh_item)
        item_osm = osm_identity(fresh_item)
        if item_id and item_id in prev_by_id and prev_by_id[item_id] not in used_prev:
            candidate = prev_by_id[item_id]
            method = "id"
        elif item_osm and item_osm in prev_by_osm and prev_by_osm[item_osm] not in used_prev:
            candidate = prev_by_osm[item_osm]
            method = "osm_id"

        if candidate is None:
            point = coordinates(fresh_item)
            if point:
                best: tuple[float, int, float] | None = None
                fresh_road = road(fresh_item)
                fresh_name = normalize_text(fresh_item.get("name"))
                for prev_index, prev_item in enumerate(previous):
                    if prev_index in used_prev:
                        continue
                    prev_point = coordinates(prev_item)
                    if not prev_point:
                        continue
                    prev_road = road(prev_item)
                    if fresh_road and prev_road and fresh_road != prev_road:
                        continue
                    metres = distance_m(point, prev_point)
                    if metres > 1_200:
                        continue
                    similarity = SequenceMatcher(
                        None, fresh_name, normalize_text(prev_item.get("name"))
                    ).ratio() if fresh_name else 0.0
                    # Very close coordinates may match despite absent/generic OSM names.
                    if metres > 350 and similarity < 0.45:
                        continue
                    score = metres - similarity * 250
                    if best is None or score < best[0]:
                        best = (score, prev_index, metres)
                if best:
                    candidate = best[1]
                    method = "nearby"
                    distance = round(best[2], 1)
                else:
                    distance = None
            else:
                distance = None
        else:
            distance = None

        if candidate is not None:
            used_prev.add(candidate)
            used_fresh.add(fresh_index)
            matches.append(
                {
                    "method": method,
                    "distanceMeters": distance,
                    "previousId": stable_id(previous[candidate]),
                    "freshId": stable_id(fresh_item),
                    "previousName": previous[candidate].get("name"),
                    "freshName": fresh_item.get("name"),
                    "road": road(fresh_item) or road(previous[candidate]),
                }
            )

    return matches, used_prev, used_fresh


def raw_osm_stats(payload: Any | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"available": False}
    elements = payload.get("elements") if isinstance(payload.get("elements"), list) else []
    rest_types = Counter()
    services = Counter()
    for item in elements:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
        highway = tags.get("highway")
        if highway in {"services", "rest_area"}:
            rest_types[str(highway)] += 1
        amenity = tags.get("amenity")
        tourism = tags.get("tourism")
        if amenity in {
            "fuel", "restaurant", "fast_food", "charging_station", "toilets", "shower", "parking"
        }:
            services[str(amenity)] += 1
        if tourism in {"hotel", "motel"}:
            services[str(tourism)] += 1
    return {
        "available": True,
        "generator": payload.get("generator"),
        "tileCount": payload.get("tileCount"),
        "elementCount": len(elements),
        "restObjectCountDeclared": payload.get("restObjectCount"),
        "restObjectsByType": dict(sorted(rest_types.items())),
        "serviceObjectsByType": dict(sorted(services.items())),
    }


def write_summary(report: dict[str, Any]) -> None:
    database = report["database"]
    matching = report["matching"]
    changes = report["changes"]
    raw = report["rawOsm"]
    lines = [
        "## TollNavigator — raport aktualizacji MOP-ów",
        "",
        "| Kontrola | Wynik |",
        "|---|---:|",
        f"| Surowe obiekty OSM | {raw.get('elementCount', 0)} |",
        f"| Surowe obiekty MOP OSM | {sum(raw.get('restObjectsByType', {}).values())} |",
        f"| Rekordy po parserze | {database['freshCount']} |",
        f"| Poprzednia dobra baza | {database['previousCount']} |",
        f"| Końcowa baza | {database['currentCount']} |",
        f"| Dopasowane do starej bazy | {matching['matchedCount']} |",
        f"| Nowe kandydaty OSM | {matching['unmatchedFreshCount']} |",
        f"| Zachowane stare bez dopasowania OSM | {matching['unmatchedPreviousCount']} |",
        f"| Dodane do końcowej bazy | {changes['addedToFinalCount']} |",
        f"| Usunięte z końcowej bazy | {changes['removedFromFinalCount']} |",
        f"| Zmienione rekordy końcowe | {changes['modifiedFinalCount']} |",
        "",
        f"**Status:** `{report['status']}`",
        "",
        "> Liczby dopasowań są diagnostycznym oszacowaniem (ID OSM / ID aplikacji / bliskość). "
        "Końcowa baza jest wynikiem osobnego, konserwatywnego skryptu scalającego.",
    ]
    summary = "\n".join(lines) + "\n"
    print(summary)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fresh")
    parser.add_argument("--current", required=True)
    parser.add_argument("--raw-osm")
    parser.add_argument("--fetch-ok", default="false")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    previous_payload = load_json(args.previous)
    fresh_payload = load_json(args.fresh)
    current_payload = load_json(args.current)
    raw_payload = load_json(args.raw_osm)

    previous = records(previous_payload)
    fresh = records(fresh_payload)
    current = records(current_payload)

    matches, used_prev, used_fresh = match_fresh_to_previous(previous, fresh)
    methods = Counter(item["method"] for item in matches)

    previous_by_id = {stable_id(item): item for item in previous if stable_id(item)}
    current_by_id = {stable_id(item): item for item in current if stable_id(item)}
    previous_ids = set(previous_by_id)
    current_ids = set(current_by_id)
    added_ids = sorted(current_ids - previous_ids)
    removed_ids = sorted(previous_ids - current_ids)

    modified: list[dict[str, Any]] = []
    service_gains = Counter()
    service_losses = Counter()
    for item_id in sorted(previous_ids & current_ids):
        changes = diff_fields(previous_by_id[item_id], current_by_id[item_id])
        if changes:
            modified.append({"id": item_id, "name": current_by_id[item_id].get("name"), "fields": changes})
        for field in SERVICE_FIELDS:
            old = bool(previous_by_id[item_id].get(field))
            new = bool(current_by_id[item_id].get(field))
            if not old and new:
                service_gains[field] += 1
            elif old and not new:
                service_losses[field] += 1

    fetch_ok = str(args.fetch_ok).strip().lower() == "true"
    raw_stats = raw_osm_stats(raw_payload)
    status = "osm_merged" if fetch_ok and fresh else "fallback_or_no_fresh_data"

    report = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "rawOsm": raw_stats,
        "database": {
            "previousCount": len(previous),
            "freshCount": len(fresh),
            "currentCount": len(current),
            "previousUniqueIds": len(previous_ids),
            "currentUniqueIds": len(current_ids),
        },
        "matching": {
            "matchedCount": len(matches),
            "byMethod": dict(sorted(methods.items())),
            "unmatchedFreshCount": len(fresh) - len(used_fresh),
            "unmatchedPreviousCount": len(previous) - len(used_prev),
            "methodNote": "Diagnostic estimate using application ID, OSM ID and conservative nearby matching.",
        },
        "changes": {
            "addedToFinalCount": len(added_ids),
            "removedFromFinalCount": len(removed_ids),
            "modifiedFinalCount": len(modified),
            "serviceGains": dict(sorted(service_gains.items())),
            "serviceLosses": dict(sorted(service_losses.items())),
        },
        "details": {
            "addedFinalIds": added_ids[:200],
            "removedFinalIds": removed_ids[:200],
            "modifiedFinalRecords": modified[:200],
            "sampleMatches": matches[:200],
            "unmatchedFresh": [
                {
                    "id": stable_id(item),
                    "osmId": osm_identity(item),
                    "name": item.get("name"),
                    "motorway": item.get("motorway"),
                    "lat": number(item, "lat", "latitude"),
                    "lon": number(item, "lon", "lng", "longitude"),
                }
                for index, item in enumerate(fresh)
                if index not in used_fresh
            ][:200],
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_summary(report)


if __name__ == "__main__":
    main()
