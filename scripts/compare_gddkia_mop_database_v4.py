#!/usr/bin/env python3
"""Add final, effective-diff reporting after all GDDKiA safety protections."""
from __future__ import annotations

from collections import Counter
from typing import Any

import compare_gddkia_mop_database as comparator
import compare_gddkia_mop_database_v3  # noqa: F401  (installs safety patches)

_original_protected_match = comparator.match_items
EFFECTIVE_FIELDS = (
    "name", "motorway", "km", "lat", "lon", "direction", "category",
    "hasFuel", "hasRestaurant", "hasHotel", "hasEV", "hasToilet", "hasShower",
    "parkingCar", "parkingTruck", "brands", "fuelBrands",
)


def effective_match_items(
    source_items: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates, diagnostics = _original_protected_match(source_items, previous)
    old_by_id = {str(item.get("id")): item for item in previous}
    field_counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []

    for candidate in candidates:
        old = old_by_id.get(str(candidate.get("id")))
        if old is None:
            records.append({
                "id": candidate.get("id"),
                "name": candidate.get("name"),
                "type": "new",
                "changes": {field: {"old": None, "new": candidate.get(field)} for field in EFFECTIVE_FIELDS},
            })
            field_counts.update(EFFECTIVE_FIELDS)
            continue
        changes: dict[str, Any] = {}
        for field in EFFECTIVE_FIELDS:
            old_value = old.get(field)
            new_value = candidate.get(field)
            if old_value != new_value:
                changes[field] = {"old": old_value, "new": new_value}
                field_counts[field] += 1
        if changes:
            records.append({
                "id": candidate.get("id"),
                "name": candidate.get("name"),
                "type": "updated",
                "changes": changes,
            })

    candidate_ids = {str(item.get("id")) for item in candidates}
    removed = [
        {"id": item.get("id"), "name": item.get("name")}
        for item in previous if str(item.get("id")) not in candidate_ids
    ]
    diagnostics["effectiveChangedRecordCount"] = len(records)
    diagnostics["effectiveFieldChangeCounts"] = dict(sorted(field_counts.items()))
    diagnostics["effectiveChanges"] = records
    diagnostics["effectiveRemovedRecordCount"] = len(removed)
    diagnostics["effectiveRemovedRecords"] = removed
    return candidates, diagnostics


comparator.match_items = effective_match_items

if __name__ == "__main__":
    comparator.main()
