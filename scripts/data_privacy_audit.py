#!/usr/bin/env python3
"""Verify that published scenario records are explicitly synthetic."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("materials", "delivery", "quality")
ID_PREFIXES = {
    "materials": "DEMO-MAT-",
    "delivery": "DEMO-SO-",
    "quality": "DEMO-LOT-",
}
PLACEHOLDER_PATTERNS = {
    "owner": re.compile(r"^示例负责人[A-Z]$"),
    "owner_email": re.compile(r"^owner-[a-z]@example\.invalid$"),
    "site": re.compile(r"^示例工厂[A-Z]$"),
    "supplier": re.compile(r"^合成供应商[A-Z]$"),
}
FORBIDDEN_KEYS = {
    "phone",
    "mobile",
    "address",
    "employee_id",
    "open_id",
    "union_id",
    "tenant_key",
    "chat_id",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_scenario(name: str) -> tuple[list[str], int]:
    failures: list[str] = []
    example_path = ROOT / "examples" / f"{name}.json"
    pack_path = ROOT / "packs" / f"{name}.json"
    example = load(example_path)
    pack = load(pack_path)

    if example != pack:
        failures.append(f"{name}: examples and packs differ")

    note = str(example.get("data_note", ""))
    if "全合成" not in note or "虚构" not in note:
        failures.append(f"{name}: data_note must explicitly say the data is fully synthetic and fictional")

    records = example.get("demo_records", [])
    if not records:
        failures.append(f"{name}: demo_records is empty")
        return failures, 0

    for index, record in enumerate(records):
        location = f"{name}.demo_records[{index}]"
        forbidden = FORBIDDEN_KEYS.intersection(record)
        if forbidden:
            failures.append(f"{location}: forbidden identifying fields: {sorted(forbidden)}")

        record_id = str(record.get("id", ""))
        if not record_id.startswith(ID_PREFIXES[name]):
            failures.append(f"{location}.id is not explicitly synthetic: {record_id!r}")

        for field, pattern in PLACEHOLDER_PATTERNS.items():
            value = record.get(field)
            if value in (None, "") and field == "owner_email":
                continue
            if value is not None and not pattern.fullmatch(str(value)):
                failures.append(f"{location}.{field} is not an approved placeholder: {value!r}")

        if "order_no" in record and not str(record["order_no"]).startswith("DEMO-MO-"):
            failures.append(f"{location}.order_no is not explicitly synthetic")

    return failures, len(records)


def main() -> int:
    failures: list[str] = []
    total = 0
    for scenario in SCENARIOS:
        scenario_failures, count = audit_scenario(scenario)
        failures.extend(scenario_failures)
        total += count

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}")
        print(f"data privacy audit: FAIL ({len(failures)} issue(s))")
        return 1

    print(f"PASS  {len(SCENARIOS)} synthetic scenario(s), {total} demo record(s)")
    print("data privacy audit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
