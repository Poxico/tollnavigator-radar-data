#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pobiera fotoradary stacjonarne i odcinkowe pomiary prędkości z OpenStreetMap.

Najważniejsze różnice względem prostych generatorów:
- pobiera surowe elementy relacji OSM (node/way), a nie współrzędny "center";
- dla OPP respektuje role relacji: from, to, section i device;
- nie tworzy punktu końcowego przez kopiowanie punktu początkowego;
- próbuje odbudować brakujący koniec z geometrii section/device;
- odrzuca pary zerowe, absurdalnie krótkie i niekompletne;
- zapisuje raport pominiętych relacji do osobnego pliku;
- usuwa z listy stacjonarnych urządzenia będące początkiem lub końcem poprawnego OPP.

Domyślny obszar odpowiada dotychczasowej bazie użytkownika:
49.0,14.0,55.0,24.5 (południe,zachód,północ,wschód).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import requests

DEFAULT_BBOX = (49.0, 14.0, 55.0, 24.5)
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
MIN_OPP_LENGTH_M = 50.0
MAX_OPP_LENGTH_M = 100_000.0
OPP_DUPLICATE_RADIUS_M = 25.0

COMPASS_DIRECTIONS = {
    "N": 0,
    "NNE": 22,
    "NE": 45,
    "ENE": 67,
    "E": 90,
    "ESE": 112,
    "SE": 135,
    "SSE": 157,
    "S": 180,
    "SSW": 202,
    "SW": 225,
    "WSW": 247,
    "W": 270,
    "WNW": 292,
    "NW": 315,
    "NNW": 337,
}


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"-?\d+", str(value))
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


def parse_direction(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if ";" in text or "," in text or "-" in text[1:]:
        return None
    if text in COMPASS_DIRECTIONS:
        return COMPASS_DIRECTIONS[text]
    try:
        return int(round(float(text))) % 360
    except ValueError:
        return None


def build_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    return f"""
[out:json][timeout:900];
(
  node({south},{west},{north},{east})[\"highway\"=\"speed_camera\"];
  relation({south},{west},{north},{east})[\"type\"=\"enforcement\"][\"enforcement\"=\"average_speed\"];
);
out body;
>;
out body qt;
""".strip()


def download_overpass(query: str, timeout_s: int = 960) -> dict[str, Any]:
    errors: list[str] = []
    headers = {"User-Agent": "TollNavigator-SpeedCameraGenerator/3.0"}

    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, 4):
            try:
                print(f"Pobieranie z {endpoint} (próba {attempt}/3)...")
                response = requests.post(
                    endpoint,
                    data={"data": query},
                    headers=headers,
                    timeout=timeout_s,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload.get("elements"), list):
                    raise RuntimeError("Odpowiedź nie zawiera listy elements")
                return payload
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                errors.append(f"{endpoint}, próba {attempt}: {exc}")
                time.sleep(3 * attempt)

    raise RuntimeError("Nie udało się pobrać danych z Overpass:\n" + "\n".join(errors))


def point_from_node(node: dict[str, Any] | None) -> tuple[float, float] | None:
    if not node:
        return None
    lat = node.get("lat")
    lon = node.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def unique_points(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()
    for lat, lon in points:
        key = (round(lat * 10_000_000), round(lon * 10_000_000))
        if key not in seen:
            seen.add(key)
            result.append((lat, lon))
    return result


def farthest_valid_pair(
    starts: Iterable[tuple[float, float]],
    ends: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float], float] | None:
    best: tuple[tuple[float, float], tuple[float, float], float] | None = None
    for start in unique_points(starts):
        for end in unique_points(ends):
            distance = haversine_m(start, end)
            if not (MIN_OPP_LENGTH_M <= distance <= MAX_OPP_LENGTH_M):
                continue
            if best is None or distance > best[2]:
                best = (start, end, distance)
    return best


def member_points(
    member: dict[str, Any],
    nodes: dict[int, dict[str, Any]],
    ways: dict[int, dict[str, Any]],
) -> list[tuple[float, float]]:
    member_type = member.get("type")
    ref = member.get("ref")
    if not isinstance(ref, int):
        return []

    if member_type == "node":
        point = point_from_node(nodes.get(ref))
        return [point] if point else []

    if member_type == "way":
        way = ways.get(ref)
        if not way:
            return []
        node_ids = way.get("nodes") or []
        points = [point_from_node(nodes.get(node_id)) for node_id in node_ids]
        points = [p for p in points if p is not None]
        if not points:
            return []
        # Dla roli from/to na way używamy WYŁĄCZNIE końców linii, nigdy środka/bbox center.
        return unique_points([points[0], points[-1]])

    return []


def section_endpoints(
    section_members: list[dict[str, Any]],
    nodes: dict[int, dict[str, Any]],
    ways: dict[int, dict[str, Any]],
) -> list[tuple[float, float]]:
    degree: Counter[int] = Counter()
    all_node_ids: set[int] = set()

    for member in section_members:
        if member.get("type") != "way" or not isinstance(member.get("ref"), int):
            continue
        way = ways.get(member["ref"])
        if not way:
            continue
        ids = [node_id for node_id in (way.get("nodes") or []) if node_id in nodes]
        all_node_ids.update(ids)
        for left, right in zip(ids, ids[1:]):
            if left == right:
                continue
            degree[left] += 1
            degree[right] += 1

    endpoint_ids = [node_id for node_id, count in degree.items() if count == 1]
    endpoint_points = [point_from_node(nodes.get(node_id)) for node_id in endpoint_ids]
    endpoint_points = [p for p in endpoint_points if p is not None]

    if len(endpoint_points) >= 2:
        return unique_points(endpoint_points)

    # Awaryjnie: w splątanej geometrii wybierz skrajne punkty spośród wszystkich węzłów.
    all_points = [point_from_node(nodes.get(node_id)) for node_id in all_node_ids]
    all_points = [p for p in all_points if p is not None]
    if len(all_points) < 2:
        return []

    best = farthest_valid_pair(all_points, all_points)
    if best is None:
        return []
    return [best[0], best[1]]


def get_tags(element: dict[str, Any] | None) -> dict[str, str]:
    tags = (element or {}).get("tags") or {}
    return tags if isinstance(tags, dict) else {}


def first_nonempty(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def relation_speed(
    relation: dict[str, Any],
    section_members: list[dict[str, Any]],
    nodes: dict[int, dict[str, Any]],
    ways: dict[int, dict[str, Any]],
) -> int | None:
    value = parse_int(get_tags(relation).get("maxspeed"))
    if value is not None:
        return value

    for member in section_members:
        ref = member.get("ref")
        element = ways.get(ref) if member.get("type") == "way" else nodes.get(ref)
        value = parse_int(get_tags(element).get("maxspeed"))
        if value is not None:
            return value
    return None


def extract_average_pair(
    relation: dict[str, Any],
    nodes: dict[int, dict[str, Any]],
    ways: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | tuple[None, None, dict[str, Any]]:
    relation_id = relation.get("id")
    members = relation.get("members") or []
    from_members = [m for m in members if m.get("role") == "from"]
    to_members = [m for m in members if m.get("role") == "to"]
    section_members = [m for m in members if m.get("role") == "section"]
    device_members = [m for m in members if m.get("role") == "device"]

    from_points = unique_points(
        point for member in from_members for point in member_points(member, nodes, ways)
    )
    to_points = unique_points(
        point for member in to_members for point in member_points(member, nodes, ways)
    )
    device_points = unique_points(
        point for member in device_members for point in member_points(member, nodes, ways)
    )
    endpoints = section_endpoints(section_members, nodes, ways)

    method = "explicit_from_to"
    pair = farthest_valid_pair(from_points, to_points)

    # Jeżeli from/to są brakujące albo wskazują to samo miejsce, odbuduj parę bez kopiowania punktu.
    if pair is None and from_points:
        method = "from_plus_section_or_device"
        pair = farthest_valid_pair(from_points, endpoints + device_points + to_points)

    if pair is None and to_points:
        method = "section_or_device_plus_to"
        pair = farthest_valid_pair(endpoints + device_points + from_points, to_points)

    # Gdy OSM nie ma żadnego from/to, nie zgadujemy kierunku na podstawie samej linii section.
    if pair is None:
        report = {
            "relation_id": relation_id,
            "status": "skipped",
            "reason": "Nie udało się wyznaczyć dwóch różnych punktów i pewnego kierunku OPP",
            "from_points": from_points,
            "to_points": to_points,
            "device_points": device_points,
            "section_endpoints": endpoints,
        }
        return None, None, report

    start, end, distance = pair
    tags = get_tags(relation)
    speed = relation_speed(relation, section_members, nodes, ways)
    operator = first_nonempty(tags.get("operator"), tags.get("brand"))
    ref = first_nonempty(tags.get("ref"))
    name = first_nonempty(tags.get("name"), tags.get("description"))

    base = {
        "maxspeed": speed,
        "direction": None,
        "isAverage": True,
        "oppPairId": str(relation_id),
    }
    if operator:
        base["operator"] = operator
    if ref:
        base["ref"] = ref
    if name:
        base["name"] = name

    start_item = {
        "id": f"opp_{relation_id}_start",
        "lat": round(start[0], 7),
        "lon": round(start[1], 7),
        **base,
        "isStart": True,
    }
    end_item = {
        "id": f"opp_{relation_id}_end",
        "lat": round(end[0], 7),
        "lon": round(end[1], 7),
        **base,
        "isStart": False,
    }
    report = {
        "relation_id": relation_id,
        "status": "ok",
        "method": method,
        "length_m": round(distance, 1),
    }
    return start_item, end_item, report


def extract_fixed_camera(node: dict[str, Any]) -> dict[str, Any] | None:
    tags = get_tags(node)
    if tags.get("highway") != "speed_camera":
        return None
    point = point_from_node(node)
    if not point:
        return None

    item: dict[str, Any] = {
        "id": f"sc_{node['id']}",
        "lat": round(point[0], 7),
        "lon": round(point[1], 7),
        "maxspeed": parse_int(tags.get("maxspeed")),
        "direction": parse_direction(tags.get("direction") or tags.get("camera:direction")),
        "isAverage": False,
    }
    for key in ("operator", "ref", "name"):
        value = first_nonempty(tags.get(key))
        if value:
            item[key] = value
    return item


def speeds_compatible(a: int | None, b: int | None) -> bool:
    # Brak limitu po jednej stronie nie powinien blokować rozpoznania tego samego urządzenia.
    return a is None or b is None or a == b


def is_near_average_endpoint(
    fixed: dict[str, Any],
    average_endpoints: list[dict[str, Any]],
) -> bool:
    fixed_point = (float(fixed["lat"]), float(fixed["lon"]))
    fixed_speed = fixed.get("maxspeed")

    for endpoint in average_endpoints:
        if not speeds_compatible(fixed_speed, endpoint.get("maxspeed")):
            continue
        endpoint_point = (float(endpoint["lat"]), float(endpoint["lon"]))
        if haversine_m(fixed_point, endpoint_point) <= OPP_DUPLICATE_RADIUS_M:
            return True
    return False


def successful_opp_member_node_ids(relation: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for member in relation.get("members") or []:
        if member.get("type") != "node":
            continue
        if member.get("role") not in {"from", "to", "device"}:
            continue
        ref = member.get("ref")
        if isinstance(ref, int):
            result.add(ref)
    return result


def build_database(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    elements = payload.get("elements") or []
    nodes: dict[int, dict[str, Any]] = {}
    ways: dict[int, dict[str, Any]] = {}
    relations: dict[int, dict[str, Any]] = {}

    for element in elements:
        element_id = element.get("id")
        if not isinstance(element_id, int):
            continue
        if element.get("type") == "node":
            nodes[element_id] = element
        elif element.get("type") == "way":
            ways[element_id] = element
        elif element.get("type") == "relation":
            relations[element_id] = element

    # Najpierw tworzymy OPP. Dopiero potem radary stacjonarne, aby urządzenia
    # należące do poprawnego OPP nie zostały dodane po raz drugi jako fixed.
    average: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    opp_member_node_ids: set[int] = set()

    for relation in relations.values():
        tags = get_tags(relation)
        if tags.get("type") != "enforcement" or tags.get("enforcement") != "average_speed":
            continue

        start, end, report = extract_average_pair(relation, nodes, ways)
        reports.append(report)
        if start and end:
            average.extend([start, end])
            opp_member_node_ids.update(successful_opp_member_node_ids(relation))

    fixed: list[dict[str, Any]] = []
    removed_fixed_duplicates: list[dict[str, Any]] = []

    for node_id, node in nodes.items():
        item = extract_fixed_camera(node)
        if not item:
            continue

        reason: str | None = None
        if node_id in opp_member_node_ids:
            reason = "member_of_successful_opp"
        elif is_near_average_endpoint(item, average):
            reason = "near_opp_endpoint"

        if reason:
            removed_fixed_duplicates.append(
                {
                    "id": item["id"],
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "reason": reason,
                }
            )
            continue

        fixed.append(item)

    fixed.sort(key=lambda x: x["id"])
    average.sort(key=lambda x: (x.get("oppPairId", ""), not x.get("isStart", False)))
    cameras = fixed + average

    now = dt.datetime.now(dt.timezone.utc)
    database = {
        "version": now.date().isoformat(),
        "source": "OpenStreetMap contributors, ODbL",
        "generated": now.isoformat(),
        "count": len(cameras),
        "count_fixed": len(fixed),
        "count_average": len(average),
        "cameras": cameras,
    }
    diagnostics = {
        "generated": now.isoformat(),
        "average_relations_total": len(reports),
        "average_relations_ok": sum(1 for r in reports if r["status"] == "ok"),
        "average_relations_skipped": sum(1 for r in reports if r["status"] != "ok"),
        "fixed_duplicates_removed": len(removed_fixed_duplicates),
        "removed_fixed_duplicates": removed_fixed_duplicates,
        "reports": reports,
    }
    return database, diagnostics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generator bazy radarów i OPP z OSM")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        default=DEFAULT_BBOX,
        help="Obszar pobierania; domyślnie: 49 14 55 24.5",
    )
    parser.add_argument(
        "--output",
        default="speed_cameras.json",
        help="Plik wynikowy JSON (domyślnie speed_cameras.json)",
    )
    parser.add_argument(
        "--report",
        default="speed_cameras_report.json",
        help="Raport diagnostyczny (domyślnie speed_cameras_report.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bbox = tuple(args.bbox)
    if len(bbox) != 4:
        print("Błędny bbox", file=sys.stderr)
        return 2

    query = build_query(bbox)  # type: ignore[arg-type]
    try:
        payload = download_overpass(query)
        database, diagnostics = build_database(payload)
    except Exception as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    report_path = Path(args.report)
    output_path.write_text(json.dumps(database, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    report_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nGotowe.")
    print(f"Plik bazy: {output_path.resolve()}")
    print(f"Raport:    {report_path.resolve()}")
    print(f"Stacjonarne: {database['count_fixed']}")
    print(f"Punkty OPP:  {database['count_average']} ({database['count_average'] // 2} par)")
    print(f"Pominięte relacje OPP: {diagnostics['average_relations_skipped']}")
    print(f"Duplikaty OPP usunięte ze stacjonarnych: {diagnostics['fixed_duplicates_removed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
