#!/usr/bin/env python3
"""Run the GDDKiA comparator with legacy-road and metadata protection."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import compare_gddkia_mop_database as comparator


def public_road(value: object) -> str:
    match = re.match(r"^\s*([AS])\s*(\d+)", comparator.text(value), re.IGNORECASE)
    return f"{match.group(1).upper()}{int(match.group(2))}" if match else comparator.text(value).upper()


def normalized_road(road_class: object, road_number: object) -> str:
    klass = re.sub(r"[^A-Za-z]", "", comparator.text(road_class)).upper()
    match = re.search(r"\d+", comparator.text(road_number))
    if not klass or not match:
        return ""
    return f"{klass}{int(match.group())}"


def generic_name(value: object) -> bool:
    name = comparator.folded(value)
    return not name or name in {
        "oour", "mop", "parking", "parking prywatny", "miejsce obslugi podroznych"
    }


_original_match_items = comparator.match_items


def protected_match_items(
    source_items: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Normalize internal GDDKiA section suffixes in the old baseline for matching.
    matching_previous = []
    original_by_id: dict[str, dict[str, Any]] = {}
    for old in previous:
        original = dict(old)
        original_by_id[str(original.get("id"))] = original
        normalized = dict(old)
        normalized["motorway"] = public_road(old.get("motorway"))
        matching_previous.append(normalized)

    candidates, diagnostics = _original_match_items(source_items, matching_previous)
    blocked_metadata: list[dict[str, Any]] = []

    for candidate in candidates:
        old = original_by_id.get(str(candidate.get("id")))
        if not old:
            continue
        blocked: dict[str, Any] = {}

        if generic_name(candidate.get("name")) and not generic_name(old.get("name")):
            blocked["name"] = {
                "old": old.get("name"),
                "source": candidate.get("name"),
                "applied": False,
                "reason": "source name is generic and less informative",
            }
            candidate["name"] = old.get("name")

        old_direction = comparator.text(old.get("direction"))
        new_direction = comparator.text(candidate.get("direction"))
        if old_direction and new_direction and comparator.folded(old_direction) != comparator.folded(new_direction):
            blocked["direction"] = {
                "old": old_direction,
                "source": new_direction,
                "applied": False,
                "reason": "direction changes require review",
            }
            candidate["direction"] = old_direction

        if blocked:
            blocked_metadata.append({
                "id": candidate.get("id"),
                "name": candidate.get("name"),
                "blockedChanges": blocked,
            })

    field_counts: Counter[str] = Counter()
    for record in diagnostics.get("changes", []):
        field_counts.update((record.get("changes") or {}).keys())
    blocked_service_counts: Counter[str] = Counter()
    for record in diagnostics.get("blockedDestructiveChanges", []):
        blocked_service_counts.update((record.get("blockedChanges") or {}).keys())
    blocked_metadata_counts: Counter[str] = Counter()
    for record in blocked_metadata:
        blocked_metadata_counts.update((record.get("blockedChanges") or {}).keys())

    diagnostics["fieldChangeCounts"] = dict(sorted(field_counts.items()))
    diagnostics["blockedServiceChangeCounts"] = dict(sorted(blocked_service_counts.items()))
    diagnostics["blockedMetadataChangeCounts"] = dict(sorted(blocked_metadata_counts.items()))
    diagnostics["blockedMetadataChanges"] = blocked_metadata
    diagnostics["blockedMetadataRecordCount"] = len(blocked_metadata)
    return candidates, diagnostics


comparator.road = normalized_road
comparator.match_items = protected_match_items

if __name__ == "__main__":
    comparator.main()
