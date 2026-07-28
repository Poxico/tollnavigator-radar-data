#!/usr/bin/env python3
"""Run the GDDKiA comparator with road-section suffix normalization.

GDDKiA values such as 61f, 8n or 1a identify internal road sections. TollNavigator
uses the public road number (S61, S8, A1), so the suffix must not become part of it.
"""
from __future__ import annotations

import re

import compare_gddkia_mop_database as comparator


def normalized_road(road_class: object, road_number: object) -> str:
    klass = re.sub(r"[^A-Za-z]", "", comparator.text(road_class)).upper()
    match = re.search(r"\d+", comparator.text(road_number))
    if not klass or not match:
        return ""
    return f"{klass}{int(match.group())}"


comparator.road = normalized_road

if __name__ == "__main__":
    comparator.main()
