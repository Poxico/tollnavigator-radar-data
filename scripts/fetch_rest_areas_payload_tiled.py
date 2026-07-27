#!/usr/bin/env python3
"""Fetch Polish OSM rest areas and A/S road geometry in nine bounded tiles."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
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
)
LAT = (49.0, 51.0, 53.0, 55.25)
LON = (14.0, 17.5, 21.0, 24.5)
ROAD_REF = r"(^|;|,| )([AS][ ]?[0-9]{1,2}[A-Z]?)(;|,| |$)"


def query(bounds: tuple[float, float, float, float]) -> str:
    s, w, n, e = bounds
    return f'''[out:json][timeout:90][maxsize:268435456];
(
  nwr["highway"~"^(services|rest_area)$"]({s},{w},{n},{e});
  way["highway"~"^(motorway|trunk)$"]["ref"~"{ROAD_REF}"]({s-0.2},{w-0.3},{n+0.2},{e+0.3});
);
out body geom;'''


def fetch(index: int, bounds: tuple[float, float, float, float]) -> dict[str, Any]:
    body = urllib.parse.urlencode({"data": query(bounds)}).encode("utf-8")
    errors: list[str] = []
    for attempt in range(2):
        endpoint = ENDPOINTS[(index + attempt) % len(ENDPOINTS)]
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "User-Agent": "TollNavigator-rest-area-updater/3.0",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            print(f"tile {index + 1}/9 attempt {attempt + 1}/2 via {endpoint}", flush=True)
            with urllib.request.urlopen(request, timeout=105) as response:
                result = json.loads(response.read().decode("utf-8"))
            print(f"tile {index + 1}/9: {len(result.get('elements') or [])} elements", flush=True)
            return result
        except urllib.error.HTTPError as exc:
            errors.append(f"HTTP {exc.code}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if attempt == 0:
            time.sleep(8)
    raise RuntimeError(f"tile {index + 1}/9 failed: " + " | ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    tiles = [
        (LAT[y], LON[x], LAT[y + 1], LON[x + 1])
        for y in range(len(LAT) - 1)
        for x in range(len(LON) - 1)
    ]
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch, i, tile): i for i, tile in enumerate(tiles)}
        for future in concurrent.futures.as_completed(futures):
            try:
                payloads.append(future.result())
            except Exception as exc:
                errors.append(str(exc))
                print(f"ERROR: {exc}", flush=True)

    if errors:
        raise SystemExit("Incomplete OSM tile set; preserving previous database. " + " | ".join(errors))

    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for payload in payloads:
        for element in payload.get("elements") or []:
            if isinstance(element.get("id"), int):
                unique[(str(element.get("type")), int(element["id"]))] = element

    elements = list(unique.values())
    rest_count = sum(
        1 for item in elements
        if (item.get("tags") or {}).get("highway") in {"services", "rest_area"}
    )
    if rest_count < 100:
        raise SystemExit(f"Suspiciously small OSM rest-area index: {rest_count}")

    output = {
        "version": 0.6,
        "generator": "TollNavigator tiled Overpass fetch",
        "tileCount": len(tiles),
        "restObjectCount": rest_count,
        "elements": elements,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(elements)} unique elements; rest objects: {rest_count}", flush=True)


if __name__ == "__main__":
    main()
