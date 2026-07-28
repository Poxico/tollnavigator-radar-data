#!/usr/bin/env python3
"""Fetch Polish OSM rest areas plus nearby A/S road geometry safely.

Large Overpass requests are attempted once. Any failed or suspiciously empty
base tile is split into four smaller tiles. Failed subtiles are split once more.
A full payload is accepted only when every base tile has complete coverage.
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
    return f'''[out:json][timeout:70][maxsize:268435456];
nwr["highway"~"^(services|rest_area)$"]({s},{w},{n},{e})->.rests;
way(around.rests:3500)
  ["highway"~"^(motorway|trunk)$"]
  ["ref"~"{ROAD_REF}"]->.roads;
(.rests;.roads;);
out body geom;'''


def split_bounds(
    bounds: tuple[float, float, float, float]
) -> list[tuple[float, float, float, float]]:
    s, w, n, e = bounds
    mid_lat = (s + n) / 2.0
    mid_lon = (w + e) / 2.0
    return [
        (s, w, mid_lat, mid_lon),
        (s, mid_lon, mid_lat, e),
        (mid_lat, w, n, mid_lon),
        (mid_lat, mid_lon, n, e),
    ]


def fetch_leaf(
    *,
    root_tile: int,
    label: str,
    level: int,
    bounds: tuple[float, float, float, float],
    seed: int,
    attempts_count: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    body = urllib.parse.urlencode({"data": query(bounds)}).encode("utf-8")
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()

    for attempt in range(attempts_count):
        endpoint = ENDPOINTS[(seed + attempt) % len(ENDPOINTS)]
        attempt_started = time.monotonic()
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "User-Agent": "TollNavigator-rest-area-updater/4.0",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            print(
                f"{label} attempt {attempt + 1}/{attempts_count} via {endpoint}",
                flush=True,
            )
            with urllib.request.urlopen(request, timeout=85) as response:
                result = json.loads(response.read().decode("utf-8"))
            element_count = len(result.get("elements") or [])
            elapsed = round(time.monotonic() - attempt_started, 2)
            if element_count == 0:
                raise RuntimeError("suspicious empty Overpass response")
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "endpoint": endpoint,
                    "status": "success",
                    "seconds": elapsed,
                    "elementCount": element_count,
                }
            )
            print(f"{label}: {element_count} elements in {elapsed}s", flush=True)
            return result, {
                "rootTile": root_tile,
                "label": label,
                "level": level,
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

        attempts.append(
            {
                "attempt": attempt + 1,
                "endpoint": endpoint,
                "status": "error",
                "seconds": round(time.monotonic() - attempt_started, 2),
                "error": error,
            }
        )
        print(f"{label} error: {error}", flush=True)
        if attempt + 1 < attempts_count:
            time.sleep(4)

    return None, {
        "rootTile": root_tile,
        "label": label,
        "level": level,
        "bounds": list(bounds),
        "status": "error",
        "seconds": round(time.monotonic() - started, 2),
        "attempts": attempts,
        "error": attempts[-1].get("error") if attempts else "unknown error",
    }


def run_batch(
    requests_to_run: list[dict[str, Any]], max_workers: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    payloads: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_leaf, **request): request for request in requests_to_run
        }
        for future in concurrent.futures.as_completed(futures):
            request = futures[future]
            try:
                payload, diagnostic = future.result()
            except Exception as exc:
                payload = None
                diagnostic = {
                    "rootTile": request["root_tile"],
                    "label": request["label"],
                    "level": request["level"],
                    "bounds": list(request["bounds"]),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempts": [],
                }
            if payload is None:
                failures.append(diagnostic)
            else:
                payloads.append(payload)
                successes.append(diagnostic)
    return payloads, successes, failures


def child_requests(
    failures: list[dict[str, Any]], level: int
) -> list[dict[str, Any]]:
    requests_to_run: list[dict[str, Any]] = []
    for failure in failures:
        root_tile = int(failure["rootTile"])
        parent_label = str(failure["label"])
        parent_bounds = tuple(float(v) for v in failure["bounds"])
        for child_index, bounds in enumerate(split_bounds(parent_bounds), start=1):
            requests_to_run.append(
                {
                    "root_tile": root_tile,
                    "label": f"{parent_label}.{child_index}",
                    "level": level,
                    "bounds": bounds,
                    "seed": root_tile + level + child_index,
                    "attempts_count": 2,
                }
            )
    return requests_to_run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics")
    args = parser.parse_args()

    base_tiles = [
        (LAT[y], LON[x], LAT[y + 1], LON[x + 1])
        for y in range(len(LAT) - 1)
        for x in range(len(LON) - 1)
    ]
    base_requests = [
        {
            "root_tile": index + 1,
            "label": f"tile {index + 1}/9",
            "level": 0,
            "bounds": bounds,
            "seed": index,
            "attempts_count": 1,
        }
        for index, bounds in enumerate(base_tiles)
    ]

    all_payloads: list[dict[str, Any]] = []
    all_diagnostics: list[dict[str, Any]] = []

    payloads, successes, failures = run_batch(base_requests, max_workers=3)
    all_payloads.extend(payloads)
    all_diagnostics.extend(successes + failures)

    if failures:
        print(
            f"Splitting {len(failures)} failed/empty base tiles into smaller tiles.",
            flush=True,
        )
        payloads, successes, level_one_failures = run_batch(
            child_requests(failures, level=1), max_workers=4
        )
        all_payloads.extend(payloads)
        all_diagnostics.extend(successes + level_one_failures)
    else:
        level_one_failures = []

    if level_one_failures:
        print(
            f"Splitting {len(level_one_failures)} failed subtiles once more.",
            flush=True,
        )
        payloads, successes, final_failures = run_batch(
            child_requests(level_one_failures, level=2), max_workers=5
        )
        all_payloads.extend(payloads)
        all_diagnostics.extend(successes + final_failures)
    else:
        final_failures = []

    failed_roots = {int(item["rootTile"]) for item in final_failures}
    complete_roots = set(range(1, len(base_tiles) + 1)) - failed_roots

    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for payload in all_payloads:
        for element in payload.get("elements") or []:
            if isinstance(element.get("id"), int):
                unique[(str(element.get("type")), int(element["id"]))] = element

    elements = list(unique.values())
    rest_count = sum(
        1
        for item in elements
        if (item.get("tags") or {}).get("highway") in {"services", "rest_area"}
    )
    complete = len(complete_roots) == len(base_tiles) and rest_count >= 100
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    output = {
        "version": 0.7,
        "generator": "TollNavigator adaptive tiled Overpass fetch",
        "generatedAt": generated_at,
        "complete": complete,
        "tileCount": len(base_tiles),
        "successfulTileCount": len(complete_roots),
        "failedTileCount": len(failed_roots),
        "restObjectCount": rest_count,
        "tileDiagnostics": sorted(
            all_diagnostics,
            key=lambda item: (
                int(item.get("rootTile", 0)),
                int(item.get("level", 0)),
                str(item.get("label", "")),
            ),
        ),
        "elements": elements,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    diagnostic = {
        "generatedAt": generated_at,
        "complete": complete,
        "tileCount": len(base_tiles),
        "successfulTileCount": len(complete_roots),
        "failedTileCount": len(failed_roots),
        "failedRootTiles": sorted(failed_roots),
        "elementCount": len(elements),
        "restObjectCount": rest_count,
        "requestsExecuted": len(all_diagnostics),
        "tiles": output["tileDiagnostics"],
    }
    if args.diagnostics:
        diagnostic_path = Path(args.diagnostics)
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        f"Saved {len(elements)} unique elements; rest objects: {rest_count}; "
        f"complete base tiles: {len(complete_roots)}/{len(base_tiles)}",
        flush=True,
    )

    if failed_roots:
        failed_numbers = ", ".join(str(value) for value in sorted(failed_roots))
        raise SystemExit(
            f"Incomplete OSM coverage after adaptive splitting "
            f"(failed base tiles: {failed_numbers}); preserving previous database."
        )
    if rest_count < 100:
        raise SystemExit(
            f"Suspiciously small OSM rest-area index: {rest_count}; "
            "preserving previous database."
        )


if __name__ == "__main__":
    main()
