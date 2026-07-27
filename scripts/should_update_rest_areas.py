#!/usr/bin/env python3
"""Ustawia output run=true raz na 14 dni przy workflow uruchamianym co tydzień."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", required=True)
    parser.add_argument("--min-days", type=int, default=13)
    args = parser.parse_args()

    run = True
    reason = "meta missing"
    path = Path(args.meta)
    if path.exists():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            generated = meta.get("generated") or meta.get("meta_generated")
            if generated:
                age_days = (datetime.now(timezone.utc) - parse_date(generated)).total_seconds() / 86400
                run = age_days >= args.min_days
                reason = f"age={age_days:.1f} days"
        except Exception as exc:
            reason = f"invalid meta: {exc}"

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if run else 'false'}\n")
            handle.write(f"reason={reason}\n")
    print(f"run={run}: {reason}")


if __name__ == "__main__":
    main()
