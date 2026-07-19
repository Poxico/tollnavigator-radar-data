#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

MIN_VALID_CAMERA_COUNT = 1000
MAX_COUNT_DROP_RATIO = 0.35  # jeśli nowa baza ma spadek >35% względem poprzedniej, przerywamy


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fail(message: str) -> None:
    print(f"BŁĄD WALIDACJI: {message}", file=sys.stderr)
    sys.exit(1)


def validate_database(db_path: Path, previous_path: Path | None = None) -> dict[str, Any]:
    if not db_path.exists():
        fail(f"brak pliku {db_path}")

    root = load_json(db_path)
    cameras = root.get("cameras")
    if not isinstance(cameras, list):
        fail("brak tablicy cameras")

    count = len(cameras)
    if count < MIN_VALID_CAMERA_COUNT:
        fail(f"zbyt mało radarów: {count}")

    declared_count = root.get("count")
    if declared_count is not None and int(declared_count) != count:
        fail(f"count nie zgadza się z cameras.length: count={declared_count}, cameras={count}")

    count_fixed = int(root.get("count_fixed", sum(1 for c in cameras if not c.get("isAverage", False))))
    count_average = int(root.get("count_average", sum(1 for c in cameras if c.get("isAverage", False))))
    if count_fixed + count_average != count:
        fail(f"count_fixed + count_average != count: {count_fixed}+{count_average}!={count}")
    if count_average % 2 != 0:
        fail(f"nieparzysta liczba punktów OPP: {count_average}")

    ids: set[str] = set()
    starts_by_pair: dict[str, int] = {}
    ends_by_pair: dict[str, int] = {}

    for i, cam in enumerate(cameras):
        if not isinstance(cam, dict):
            fail(f"kamera #{i} nie jest obiektem")
        cid = cam.get("id")
        if not cid:
            fail(f"kamera #{i} nie ma id")
        if cid in ids:
            fail(f"duplikat id: {cid}")
        ids.add(str(cid))

        try:
            lat = float(cam["lat"])
            lon = float(cam["lon"])
        except Exception:
            fail(f"kamera {cid} ma błędne lat/lon")
        if not (48.0 <= lat <= 56.0 and 13.0 <= lon <= 25.5):
            fail(f"kamera {cid} poza oczekiwanym obszarem PL: {lat},{lon}")

        if cam.get("isAverage", False):
            pair_id = cam.get("oppPairId")
            if not pair_id:
                fail(f"punkt OPP {cid} nie ma oppPairId")
            if cam.get("isStart", True):
                starts_by_pair[pair_id] = starts_by_pair.get(pair_id, 0) + 1
            else:
                ends_by_pair[pair_id] = ends_by_pair.get(pair_id, 0) + 1

    if starts_by_pair.keys() != ends_by_pair.keys():
        missing_ends = sorted(starts_by_pair.keys() - ends_by_pair.keys())[:5]
        missing_starts = sorted(ends_by_pair.keys() - starts_by_pair.keys())[:5]
        fail(f"niezgodne pary OPP; missing_ends={missing_ends}, missing_starts={missing_starts}")

    bad_pairs = [p for p in starts_by_pair if starts_by_pair[p] != 1 or ends_by_pair.get(p, 0) != 1]
    if bad_pairs:
        fail(f"błędne liczności par OPP, przykłady: {bad_pairs[:5]}")

    if previous_path and previous_path.exists():
        try:
            previous = load_json(previous_path)
            prev_count = len(previous.get("cameras", []))
            if prev_count >= MIN_VALID_CAMERA_COUNT:
                drop_ratio = (prev_count - count) / prev_count
                if drop_ratio > MAX_COUNT_DROP_RATIO:
                    fail(f"podejrzany spadek liczby radarów: poprzednio={prev_count}, teraz={count}")
        except Exception as exc:
            print(f"OSTRZEŻENIE: nie można porównać z poprzednią bazą: {exc}")

    return {
        "version": str(root.get("version") or dt.datetime.now(dt.timezone.utc).date().isoformat()),
        "generated": str(root.get("generated") or dt.datetime.now(dt.timezone.utc).isoformat()),
        "source": str(root.get("source") or "OpenStreetMap contributors, ODbL"),
        "count": count,
        "count_fixed": count_fixed,
        "count_average": count_average,
        "average_pairs": count_average // 2,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Walidacja bazy radarów i tworzenie speed_cameras_meta.json")
    parser.add_argument("--db", required=True, help="Ścieżka do speed_cameras.json")
    parser.add_argument("--report", required=False, help="Ścieżka do speed_cameras_report.json")
    parser.add_argument("--previous", required=False, help="Opcjonalna poprzednia baza do porównania")
    parser.add_argument("--out", required=True, help="Ścieżka wyjściowa speed_cameras_meta.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    previous_path = Path(args.previous) if args.previous else None
    meta = validate_database(db_path, previous_path=previous_path)

    if args.report and Path(args.report).exists():
        report = load_json(Path(args.report))
        meta["average_relations_total"] = report.get("average_relations_total")
        meta["average_relations_ok"] = report.get("average_relations_ok")
        meta["average_relations_skipped"] = report.get("average_relations_skipped")
        meta["fixed_duplicates_removed"] = report.get("fixed_duplicates_removed")

    meta["sha256"] = sha256_file(db_path)
    meta["meta_generated"] = dt.datetime.now(dt.timezone.utc).isoformat()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("Walidacja OK")
    print(f"Radary: {meta['count']} | stacjonarne: {meta['count_fixed']} | OPP: {meta['count_average']} ({meta['average_pairs']} par)")
    print(f"Meta: {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
