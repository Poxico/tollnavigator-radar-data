#!/usr/bin/env python3
"""Reconcile legacy OSM enrichment before comparing the GDDKiA MOP workbook.

The bundled TollNavigator database was built from GDDKiA core fields and later enriched
with OSM brands. Some legacy records therefore contain fuelBrands/brands while their
hasFuel/hasRestaurant flag is still false. Those are data-consistency fixes, not newly
opened services, and must be reported separately.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import compare_gddkia_mop_database as comparator
import compare_gddkia_mop_database_v4  # noqa: F401  (installs all previous safety patches)

_original_effective_match = comparator.match_items


def reconciled_match_items(
    source_items: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reconciled_previous: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()

    for old in previous:
        normalized = dict(old)
        fixed: dict[str, Any] = {}

        fuel_brands = [str(value).strip() for value in (old.get("fuelBrands") or []) if str(value).strip()]
        restaurant_brands = [str(value).strip() for value in (old.get("brands") or []) if str(value).strip()]

        if fuel_brands and not bool(old.get("hasFuel")):
            normalized["hasFuel"] = True
            fixed["hasFuel"] = {
                "old": False,
                "effective": True,
                "reason": "existing OSM fuel brand already confirmed the station",
                "evidence": fuel_brands,
            }
            field_counts["hasFuel"] += 1

        if restaurant_brands and not bool(old.get("hasRestaurant")):
            normalized["hasRestaurant"] = True
            fixed["hasRestaurant"] = {
                "old": False,
                "effective": True,
                "reason": "existing OSM restaurant brand already confirmed gastronomy",
                "evidence": restaurant_brands,
            }
            field_counts["hasRestaurant"] += 1

        if fixed:
            reconciliations.append({
                "id": old.get("id"),
                "name": old.get("name"),
                "reconciledFields": fixed,
            })

        reconciled_previous.append(normalized)

    candidates, diagnostics = _original_effective_match(source_items, reconciled_previous)
    diagnostics["legacyOsmEnrichmentReconciliationCount"] = len(reconciliations)
    diagnostics["legacyOsmEnrichmentFieldCounts"] = dict(sorted(field_counts.items()))
    diagnostics["legacyOsmEnrichmentReconciliations"] = reconciliations
    diagnostics["serviceChangeMeaning"] = {
        "legacy_reconciliation": "service was already known from preserved OSM brand data",
        "effective_false_to_true": "GDDKiA reports a service not previously confirmed by app flags or preserved brands",
        "true_to_false": "blocked automatically and retained from the previous good database",
    }
    return candidates, diagnostics


comparator.match_items = reconciled_match_items

if __name__ == "__main__":
    comparator.main()
