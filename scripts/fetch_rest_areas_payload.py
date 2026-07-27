#!/usr/bin/env python3
"""Buduje odpowiedź OSM dla generatora MOP-ów, dzieląc ciężkie zapytanie na małe partie."""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
)

REST_QUERY = '''
[out:json][timeout:180];
area["ISO3166-1"="PL"][admin_level=2]->.pl;
nwr(area.pl)["highway"~"^(services|rest_area)$"];
out body center;
'''.strip()

ROAD_REF = r"(^|;|,| )([AS][ ]?[0-9]{1,2}[A-Z]?)(;|,| |$)"


def center(element: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return float(element["lat"]), float(element["lon"])
    value = element.get("center") or {}
    if "lat" in value and "lon" in value:
        return float(value["lat"]), float(value["lon"])
    return None


def request(query: str, label: str, attempts: int = 6) -> dict[str, Any]:
    data = urllib.parse.urlencode({"data": query}).encode()
    errors: list[str] = []
    for attempt in range(attempts):
        endpoint = ENDPOINTS[attempt % len(ENDPOINTS)]
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "User-Agent": "TollNavigator-rest-area-updater/2.0",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            print(f"{label}: attempt {attempt + 1}/{attempts} via {endpoint}", flush=True)
            with urllib.request.urlopen(req, timeout=240) as response:
                payload = json.loads(response.read().decode("utf-8"))
            print(f"{label}: {len(payload.get('elements') or [])} elements", flush=True)
            return payload
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(300).decode("utf-8", "replace").replace("\n", " ")
            except Exception:
                detail = ""
            errors.append(f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if attempt + 1 < attempts:
            time.sleep(min(35, 5 + attempt * 5))
    raise RuntimeError(f"{label} failed: " + " | ".join(errors[-3:]))


def context_query(points: list[tuple[float, float]]) -> str:
    clauses: list[str] = []
    for lat, lon in points:
        point = f"{lat:.7f},{lon:.7f}"
        clauses += [
            f'way(around:2200,{point})["highway"~"^(motorway|trunk)$"]["ref"~"{ROAD_REF}"];',
            f'nwr(around:850,{point})["amenity"~"^(fuel|restaurant|fast_food|charging_station|toilets|shower|parking)$"];',
            f'nwr(around:850,{point})["tourism"~"^(hotel|motel)$"];',
        ]
    return "\n".join([
        "[out:json][timeout:180][maxsize:536870912];",
        "(", *clauses, ");", "out body geom;",
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    rest_payload = request(REST_QUERY, "rest index", attempts=8)
    rests = [
        item for item in (rest_payload.get("elements") or [])
        if (item.get("tags") or {}).get("highway") in {"services", "rest_area"} and center(item)
    ]
    if len(rests) < 100:
        raise SystemExit(f"Suspiciously small rest index: {len(rests)}")
    print(f"Rest index contains {len(rests)} objects", flush=True)

    found: dict[tuple[str, int], dict[str, Any]] = {}
    for item in rests:
        if isinstance(item.get("id"), int):
            found[(str(item.get("type")), int(item["id"]))] = item

    points = [center(item) for item in rests]
    points = [item for item in points if item]

    def fetch_group(group: list[tuple[float, float]], label: str) -> None:
        try:
            payload = request(context_query(group), label)
        except RuntimeError as exc:
            if len(group) > 1:
                middle = len(group) // 2
                print(f"{exc}; splitting {label}", flush=True)
                fetch_group(group[:middle], label + "a")
                fetch_group(group[middle:], label + "b")
                return
            print(f"Skipping one context point: {exc}", flush=True)
            return
        for item in payload.get("elements") or []:
            if isinstance(item.get("id"), int):
                found[(str(item.get("type")), int(item["id"]))] = item

    total = math.ceil(len(points) / args.batch_size)
    for start in range(0, len(points), args.batch_size):
        fetch_group(points[start:start + args.batch_size], f"context {start // args.batch_size + 1}/{total}")

    output = {"version": 0.6, "generator": "TollNavigator batched Overpass fetch", "elements": list(found.values())}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(output['elements'])} unique OSM elements to {path}")


if __name__ == "__main__":
    main()
