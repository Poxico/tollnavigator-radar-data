#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bezpieczne scalanie bazy radarów TollNavigator.

Cel:
- świeży plik z OSM może dodawać i aktualizować radary,
- ale nie może przypadkowo skasować istotnych radarów z poprzedniej dobrej bazy,
- brakujące elementy są zachowywane ze starej bazy jako carry-over,
- brakujące elementy ze świeżego OSM są zachowywane ze starej bazy,
- większe zniknięcia są raportowane jako ostrzeżenie, ale nie blokują publikacji,
  bo celem jest niedopuszczenie do przypadkowego skasowania działających radarów.

Progi ostrzeżeń:
- OPP: więcej niż 1 brakująca para OPP względem poprzedniej bazy,
- stacjonarne: więcej niż 3 brakujące fotoradary stacjonarne względem poprzedniej bazy.

Workflow zatrzymujemy tylko wtedy, gdy świeży plik wygląda ewidentnie na uszkodzony,
np. ma mniej niż MIN_FRESH_CAMERA_COUNT kamer.

Ręczne usuwanie:
- opcjonalny plik manual_removed_speed_cameras.json pozwala świadomie usunąć wpisy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


MAX_MISSING_OPP_PAIRS = 1
MAX_MISSING_FIXED_CAMERAS = 3
MIN_FRESH_CAMERA_COUNT = 1000


def fail(message: str) -> None:
    print(f"BŁĄD SCALANIA: {message}", file=sys.stderr)
    sys.exit(1)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any], pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")


def camera_id(camera: dict[str, Any]) -> str:
    return str(camera.get("id") or "")


def is_average(camera: dict[str, Any]) -> bool:
    return bool(camera.get("isAverage", False))


def is_fixed(camera: dict[str, Any]) -> bool:
    return not is_average(camera)


def opp_pair_id(camera: dict[str, Any]) -> str | None:
    value = camera.get("oppPairId")
    return str(value) if value is not None and str(value).strip() else None


def load_manual_remove(path: Path | None) -> tuple[set[str], set[str]]:
    if not path or not path.exists():
        return set(), set()

    root = load_json(path)
    remove_ids = {
        str(x).strip()
        for x in root.get("remove_ids", [])
        if str(x).strip()
    }
    remove_opp_pair_ids = {
        str(x).strip()
        for x in root.get("remove_opp_pair_ids", [])
        if str(x).strip()
    }
    return remove_ids, remove_opp_pair_ids


def group_opp_pairs(cameras: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    pairs: dict[str, list[dict[str, Any]]] = {}
    for cam in cameras:
        if not is_average(cam):
            continue
        pair_id = opp_pair_id(cam)
        if not pair_id:
            continue
        pairs.setdefault(pair_id, []).append(cam)
    return pairs


def complete_opp_pair_ids(cameras: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for pair_id, items in group_opp_pairs(cameras).items():
        starts = sum(1 for c in items if bool(c.get("isStart", True)))
        ends = sum(1 for c in items if not bool(c.get("isStart", True)))
        if starts == 1 and ends == 1:
            result.add(pair_id)
    return result


def fixed_ids(cameras: list[dict[str, Any]]) -> set[str]:
    return {camera_id(c) for c in cameras if is_fixed(c) and camera_id(c)}


def filter_removed(
    cameras: list[dict[str, Any]],
    remove_ids: set[str],
    remove_opp_pair_ids: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cam in cameras:
        cid = camera_id(cam)
        pair_id = opp_pair_id(cam)
        if cid in remove_ids:
            continue
        if pair_id and pair_id in remove_opp_pair_ids:
            continue
        result.append(cam)
    return result


def sort_cameras(cameras: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fixed = [c for c in cameras if is_fixed(c)]
    average = [c for c in cameras if is_average(c)]

    fixed.sort(key=lambda x: camera_id(x))
    average.sort(key=lambda x: (opp_pair_id(x) or "", not bool(x.get("isStart", False)), camera_id(x)))

    return fixed + average


def recalc_counts(root: dict[str, Any], cameras: list[dict[str, Any]]) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    fixed_count = sum(1 for c in cameras if is_fixed(c))
    average_count = sum(1 for c in cameras if is_average(c))

    out = dict(root)
    out["version"] = now.date().isoformat()
    out["generated"] = now.isoformat()
    out["count"] = len(cameras)
    out["count_fixed"] = fixed_count
    out["count_average"] = average_count
    out["cameras"] = cameras
    return out


def validate_no_duplicate_ids(cameras: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for cam in cameras:
        cid = camera_id(cam)
        if not cid:
            fail("kamera bez id po scaleniu")
        if cid in seen:
            duplicates.append(cid)
        seen.add(cid)
    if duplicates:
        fail(f"duplikaty id po scaleniu, przykłady: {duplicates[:10]}")


def merge_databases(
    fresh: dict[str, Any],
    previous: dict[str, Any] | None,
    remove_ids: set[str],
    remove_opp_pair_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    fresh_cameras_raw = fresh.get("cameras")
    if not isinstance(fresh_cameras_raw, list):
        fail("świeża baza nie ma tablicy cameras")

    fresh_cameras = filter_removed(
        [c for c in fresh_cameras_raw if isinstance(c, dict)],
        remove_ids,
        remove_opp_pair_ids,
    )

    if len(fresh_cameras) < MIN_FRESH_CAMERA_COUNT:
        fail(
            "świeża baza wygląda na uszkodzoną: "
            f"{len(fresh_cameras)} kamer < {MIN_FRESH_CAMERA_COUNT}"
        )

    if not previous:
        merged = recalc_counts(fresh, sort_cameras(fresh_cameras))
        report = {
            "merge_enabled": False,
            "reason": "brak poprzedniej bazy",
            "fresh_count": merged["count"],
            "fresh_fixed": merged["count_fixed"],
            "fresh_average": merged["count_average"],
            "preserved_fixed": 0,
            "preserved_opp_pairs": 0,
        }
        return merged, report

    previous_cameras_raw = previous.get("cameras")
    if not isinstance(previous_cameras_raw, list):
        fail("poprzednia baza nie ma tablicy cameras")

    previous_cameras = filter_removed(
        [c for c in previous_cameras_raw if isinstance(c, dict)],
        remove_ids,
        remove_opp_pair_ids,
    )

    fresh_fixed_ids = fixed_ids(fresh_cameras)
    previous_fixed_ids = fixed_ids(previous_cameras)
    missing_fixed_ids = sorted(previous_fixed_ids - fresh_fixed_ids)

    fresh_opp_pairs = complete_opp_pair_ids(fresh_cameras)
    previous_opp_pairs = complete_opp_pair_ids(previous_cameras)
    missing_opp_pairs = sorted(previous_opp_pairs - fresh_opp_pairs)

    warnings: list[str] = []

    if len(missing_opp_pairs) > MAX_MISSING_OPP_PAIRS:
        warnings.append(
            "zniknęło dużo par OPP ze świeżego OSM: "
            f"{len(missing_opp_pairs)} > {MAX_MISSING_OPP_PAIRS}; "
            f"zostaną zachowane ze starej bazy; przykłady: {missing_opp_pairs[:10]}"
        )

    if len(missing_fixed_ids) > MAX_MISSING_FIXED_CAMERAS:
        warnings.append(
            "zniknęło dużo fotoradarów stacjonarnych ze świeżego OSM: "
            f"{len(missing_fixed_ids)} > {MAX_MISSING_FIXED_CAMERAS}; "
            f"zostaną zachowane ze starej bazy; przykłady: {missing_fixed_ids[:10]}"
        )

    # Budujemy wynik: świeża baza + brakujące elementy ze starej dobrej bazy.
    # Brakujących elementów nie kasujemy automatycznie.
    merged_by_id: dict[str, dict[str, Any]] = {}
    for cam in fresh_cameras:
        cid = camera_id(cam)
        if cid:
            merged_by_id[cid] = cam

    previous_by_id = {camera_id(c): c for c in previous_cameras if camera_id(c)}
    preserved_fixed: list[str] = []

    for cid in missing_fixed_ids:
        old_cam = previous_by_id.get(cid)
        if old_cam:
            merged_by_id[cid] = old_cam
            preserved_fixed.append(cid)

    previous_pairs = group_opp_pairs(previous_cameras)
    preserved_opp_pairs: list[str] = []

    for pair_id in missing_opp_pairs:
        old_items = previous_pairs.get(pair_id, [])
        if not old_items:
            continue
        for old_cam in old_items:
            cid = camera_id(old_cam)
            if cid:
                merged_by_id[cid] = old_cam
        preserved_opp_pairs.append(pair_id)

    merged_cameras = sort_cameras(list(merged_by_id.values()))
    validate_no_duplicate_ids(merged_cameras)

    merged = recalc_counts(fresh, merged_cameras)

    report = {
        "merge_enabled": True,
        "max_missing_opp_pairs": MAX_MISSING_OPP_PAIRS,
        "max_missing_fixed_cameras": MAX_MISSING_FIXED_CAMERAS,
        "fresh_count": len(fresh_cameras),
        "fresh_fixed": len([c for c in fresh_cameras if is_fixed(c)]),
        "fresh_average": len([c for c in fresh_cameras if is_average(c)]),
        "previous_count": len(previous_cameras),
        "previous_fixed": len([c for c in previous_cameras if is_fixed(c)]),
        "previous_average": len([c for c in previous_cameras if is_average(c)]),
        "missing_fixed_from_fresh": len(missing_fixed_ids),
        "missing_opp_pairs_from_fresh": len(missing_opp_pairs),
        "too_many_missing_fixed_warning": len(missing_fixed_ids) > MAX_MISSING_FIXED_CAMERAS,
        "too_many_missing_opp_warning": len(missing_opp_pairs) > MAX_MISSING_OPP_PAIRS,
        "warnings": warnings,
        "preserved_fixed": len(preserved_fixed),
        "preserved_opp_pairs": len(preserved_opp_pairs),
        "preserved_fixed_ids": preserved_fixed,
        "preserved_opp_pair_ids": preserved_opp_pairs,
        "manual_removed_ids": sorted(remove_ids),
        "manual_removed_opp_pair_ids": sorted(remove_opp_pair_ids),
        "result_count": merged["count"],
        "result_fixed": merged["count_fixed"],
        "result_average": merged["count_average"],
        "result_opp_pairs": merged["count_average"] // 2,
    }

    return merged, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bezpieczne scalanie świeżej bazy radarów z poprzednią dobrą bazą")
    parser.add_argument("--new", required=True, help="Świeży plik wygenerowany z OSM, np. tmp/speed_cameras_fresh.json")
    parser.add_argument("--previous", required=False, help="Poprzednia dobra baza, np. public/tollnavigator/speed_cameras.json")
    parser.add_argument("--out", required=True, help="Finalny plik wynikowy speed_cameras.json")
    parser.add_argument("--report", required=True, help="Raport scalania JSON")
    parser.add_argument("--manual-remove", required=False, help="Opcjonalny plik manual_removed_speed_cameras.json")
    args = parser.parse_args()

    fresh_path = Path(args.new)
    previous_path = Path(args.previous) if args.previous else None
    out_path = Path(args.out)
    report_path = Path(args.report)
    manual_remove_path = Path(args.manual_remove) if args.manual_remove else None

    if not fresh_path.exists():
        fail(f"brak świeżej bazy: {fresh_path}")

    fresh = load_json(fresh_path)
    previous = load_json(previous_path) if previous_path and previous_path.exists() else None
    remove_ids, remove_opp_pair_ids = load_manual_remove(manual_remove_path)

    merged, report = merge_databases(fresh, previous, remove_ids, remove_opp_pair_ids)

    write_json(out_path, merged, pretty=False)
    write_json(report_path, report, pretty=True)

    print("Scalanie OK")
    print(f"Finalna baza: {out_path.resolve()}")
    print(f"Raport:       {report_path.resolve()}")
    print(f"Radary: {merged['count']} | stacjonarne: {merged['count_fixed']} | OPP: {merged['count_average']} ({merged['count_average'] // 2} par)")
    if report.get("merge_enabled"):
        print(f"Zachowane ze starej bazy: fixed={report['preserved_fixed']}, OPP pary={report['preserved_opp_pairs']}")
        for warning in report.get("warnings", []):
            print(f"OSTRZEŻENIE SCALANIA: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
