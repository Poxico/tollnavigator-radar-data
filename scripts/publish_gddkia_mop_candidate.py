#!/usr/bin/env python3
"""Validate and publish a safe GDDKiA-derived TollNavigator MOP candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {
    "id", "name", "motorway", "km", "lat", "lon", "direction", "category",
    "hasFuel", "hasRestaurant", "hasHotel", "hasEV", "hasToilet", "hasShower",
    "parkingCar", "parkingTruck", "brands", "fuelBrands",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(candidate: dict[str, Any], report: dict[str, Any], previous: dict[str, Any]) -> None:
    comparison = report.get("comparison") or {}
    workbook = report.get("workbook") or {}
    areas = candidate.get("rest_areas") or []
    previous_areas = previous.get("rest_areas") or []

    errors: list[str] = []
    if not report.get("safeToConsiderPublishing"):
        errors.append("diagnostic report did not pass safety assessment")
    if workbook.get("parsedCount", 0) < 350:
        errors.append(f"too few parsed GDDKiA rows: {workbook.get('parsedCount')}")
    if workbook.get("rowIssues"):
        errors.append(f"workbook row issues: {len(workbook.get('rowIssues') or [])}")
    if comparison.get("duplicateIds"):
        errors.append(f"duplicate candidate IDs: {comparison.get('duplicateIds')}")
    if comparison.get("effectiveRemovedRecordCount", 0) != 0:
        errors.append("candidate would remove existing records")
    if len(areas) < len(previous_areas):
        errors.append(f"candidate count {len(areas)} is below previous count {len(previous_areas)}")
    if comparison.get("matchedPreviousCount", 0) < max(350, int(len(previous_areas) * 0.9)):
        errors.append(f"too few matched previous records: {comparison.get('matchedPreviousCount')}")
    if comparison.get("effectiveChangedRecordCount", 0) > max(200, int(len(previous_areas) * 0.5)):
        errors.append(f"too many effective changes: {comparison.get('effectiveChangedRecordCount')}")

    seen: set[str] = set()
    for index, item in enumerate(areas):
        missing = REQUIRED_FIELDS - set(item)
        if missing:
            errors.append(f"record {index} missing fields: {sorted(missing)}")
            continue
        item_id = str(item.get("id"))
        if item_id in seen:
            errors.append(f"duplicate ID: {item_id}")
        seen.add(item_id)
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
            km = float(item["km"])
            if not (48.5 <= lat <= 55.5 and 13.5 <= lon <= 25.0 and math.isfinite(km)):
                errors.append(f"invalid coordinates/km for {item_id}")
        except (TypeError, ValueError):
            errors.append(f"non-numeric coordinates/km for {item_id}")
        if int(item.get("parkingCar", 0)) < 0 or int(item.get("parkingTruck", 0)) < 0:
            errors.append(f"negative parking count for {item_id}")

    if errors:
        raise SystemExit("Unsafe GDDKiA MOP candidate:\n- " + "\n- ".join(errors[:50]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--public-report", required=True)
    args = parser.parse_args()

    candidate = load(args.candidate)
    report = load(args.report)
    previous = load(args.previous)
    validate(candidate, report, previous)

    generated = utc_now()
    source = report.get("source") or {}
    published = {
        "version": datetime.now(timezone.utc).date().isoformat(),
        "generated": generated,
        "source": "GDDKiA official MOP workbook",
        "count": len(candidate.get("rest_areas") or []),
        "rest_areas": candidate.get("rest_areas") or [],
    }
    output_bytes = canonical_bytes(published)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)

    meta = {
        "schemaVersion": 1,
        "version": published["version"],
        "generated": generated,
        "count": published["count"],
        "sha256": hashlib.sha256(output_bytes).hexdigest(),
        "source": "GDDKiA",
        "sourcePage": source.get("pageUrl"),
        "sourceAttachment": source.get("attachmentUrl"),
        "sourceAttachmentLabel": source.get("attachmentLabel"),
        "sourceAttachmentSha256": source.get("sha256"),
        "previousCount": len(previous.get("rest_areas") or []),
        "effectiveChangedRecordCount": (report.get("comparison") or {}).get("effectiveChangedRecordCount", 0),
        "effectiveFieldChangeCounts": (report.get("comparison") or {}).get("effectiveFieldChangeCounts", {}),
        "safety": {
            "missingOldRecordsPreserved": True,
            "genericNamesProtected": True,
            "directionDowngradesBlocked": True,
            "serviceTrueToFalseBlocked": True,
            "brandsPreserved": True,
        },
    }
    Path(args.meta).write_bytes(canonical_bytes(meta))

    public_report = dict(report)
    public_report["mode"] = "published_safe_update"
    public_report["productionDatabaseModified"] = True
    public_report["publishedAt"] = generated
    Path(args.public_report).write_bytes(canonical_bytes(public_report))

    print(
        f"Published {published['count']} GDDKiA MOP records; "
        f"effective changed records: {meta['effectiveChangedRecordCount']}"
    )


if __name__ == "__main__":
    main()
