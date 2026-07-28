#!/usr/bin/env python3
"""Fetch Polish OSM rest areas and A/S road geometry in bounded tiles.

The script always writes a partial raw payload and a per-tile diagnostic report.
It exits with a non-zero code when any tile is missing, so the existing safe
MOP database is never replaced with an incomplete OSM result.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    return f'''[out:json][timeout:75][maxsize:268435456];
(
  nwr["highway"~"^(services|rest_area)$"]({s},{w},{n},{e});
  way["highway"~"^(motorway|trunk)$"]["ref"~"{ROAD_REF}"]({s-0.15},{w-0.2},{n+0.15},{e+0.2});
);
out body geom;'''


def fetch(index: int, bounds: tuple[float, float, float, float]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    body = urllib.parse.urlencode({"data": query(bounds)}).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()

    for attempt in range(2):
        endpoint = ENDPOINTS[(index + attempt) % len(ENDPOINTS)]
        attempt_started = time.monotonic()
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "User-Agent": "TollNavigator-rest-area-updater/3.1",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            print(f"tile {index + 1}/9 attempt {attempt + 1}/2 via {endpoint}", flush=True)
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            element_count = len(result.get("elements") or [])
            elapsed = round(time.monotonic() - attempt_started, 2)
            attempts.append({
                "attempt": attempt + 1,
                "endpoint": endpoint,
                "status": "success",
                "seconds": elapsed,
                "elementCount": element_count,
            })
            print(f"tile {index + 1}/9: {element_count} elements in {elapsed}s", flush=True)
            return result, {
                "tile": index + 1,
                "bounds": list(bounds),
                "status": "success",
                "seconds": round(time.monotonic() - started, 2),
                "elementCount": element_count,
                "attempts": attempts,
            }
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(300).decode("utf-8", "replace").replace("\n", " ")
            except Exception:
                pass
            error = f"HTTP {exc.code}: {detail}".strip()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        attempts.append({
            "attempt": attempt + 1,
            "endpoint": endpoint,
            "status": "error",
            "seconds": round(time.monotonic() - attempt_started, 2),
            "error": error,
        })
        print(f"tile {index + 1}/9 error: {error}", flush=True)
        if attempt == 0:
            time.sleep(5)

    return None, {
        "tile": index + 1,
        "bounds": list(bounds),
        "status": "error",
        "seconds": round(time.monotonic() - started, 2),
        "attempts": attempts,
        "error": attempts[-1].get("error") if attempts else "unknown error",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics")
    args = parser.parse_args()

    tiles = [
        (LAT[y], LON[x], LAT[y + 1], LON[x + 1])
        for y in range(len(LAT) - 1)
        for x in range(len(LON) - 1)
    ]
    payloads: list[dict[str, Any]] = []
    tile_results: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch, i, tile): i for i, tile in enumerate(tiles)}
        for future in concurrent.futures.as_completed(futures):
            payload, diagnostic = future.result()
            tile_results.append(diagnostic)
            if payload is not None:
                payloads.append(payload)

    tile_results.sort(key=lambda item: int(item["tile"]))
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
    failed_tiles = [item for item in tile_results if item.get("status") != "success"]
    complete = not failed_tiles and rest_count >= 100

    output = {
        "version": 0.6,
        "generator": "TollNavigator tiled Overpass fetch",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "complete": complete,
        "tileCount": len(tiles),
        "successfulTileCount": len(tiles) - len(failed_tiles),
        "failedTileCount": len(failed_tiles),
        "restObjectCount": rest_count,
        "tileDiagnostics": tile_results,
        "elements": elements,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    diagnostic = {
        "generatedAt": output["generatedAt"],
        "complete": complete,
        "tileCount": len(tiles),
        "successfulTileCount": output["successfulTileCount"],
        "failedTileCount": output["failedTileCount"],
        "elementCount": len(elements),
        "restObjectCount": rest_count,
        "tiles": tile_results,
    }
    if args.diagnostics:
        diagnostic_path = Path(args.diagnostics)
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"Saved {len(elements)} unique elements; rest objects: {rest_count}; "
        f"successful tiles: {output['successfulTileCount']}/{len(tiles)}",
        flush=True,
    )

    if failed_tiles:
        failed_numbers = ", ".join(str(item["tile"]) for item in failed_tiles)
        raise SystemExit(f"Incomplete OSM tile set (failed tiles: {failed_numbers}); preserving previous database.")
    if rest_count < 100:
        raise SystemExit(f"Suspiciously small OSM rest-area index: {rest_count}; preserving previous database.")


if __name__ == "__main__":
    main()
