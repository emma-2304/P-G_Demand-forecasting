#!/usr/bin/env python3
"""
Test script for the Demand Forecasting case study.

Calls your server's API to:
  1. Setup reference data (POST /api/setup/)
  2. Ingest events (valid and invalid)
  3. Trigger family recomputation
  4. Verify resolve / reverse-resolve queries

Usage:
    python test_api.py [--base-url http://localhost:5000] [--total 100]
"""

import argparse
import json
import random
import sys
import time
from datetime import date, timedelta

import requests

# ── Defaults ──────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "http://localhost:5050"
DEFAULT_TOTAL = 100
COUNTRIES = ["PL", "DE", "US", "JP", "CN"]
CODE_TYPE_ID = "IPC"

# ── State ─────────────────────────────────────────────────────────────
_code_seq = 1_000_000
_session = requests.Session()
stats = {"pass": 0, "fail": 0}
# Track created families for verification: list of (country, [(code, date)])
_families: list[tuple[str, list[tuple[int, str]]]] = []


def next_code() -> int:
    global _code_seq
    _code_seq += 1
    return _code_seq


def rand_date_sequence(n: int) -> list[date]:
    """Return n sorted distinct random dates in 2020–2025."""
    start = date(2020, 1, 1)
    span = (date(2025, 12, 31) - start).days
    dates: set[date] = set()
    while len(dates) < n:
        dates.add(start + timedelta(days=random.randint(0, span)))
    return sorted(dates)


# ── HTTP helpers ──────────────────────────────────────────────────────
def post(path: str, data: dict, *, expect_status: int | None = None) -> requests.Response:
    r = _session.post(f"{BASE_URL}{path}", json=data)
    if expect_status is not None and r.status_code != expect_status:
        return r  # Caller will handle
    return r


def get(path: str, params: dict | None = None) -> requests.Response:
    return _session.get(f"{BASE_URL}{path}", params=params)


def check(label: str, condition: bool) -> None:
    if condition:
        stats["pass"] += 1
        print(f"  PASS  {label}")
    else:
        stats["fail"] += 1
        print(f"  FAIL  {label}")


# ── Phase 0: Setup ───────────────────────────────────────────────────
def phase_setup():
    print("\n=== PHASE 0: SETUP ===")
    r = post("/api/setup/", {})
    check("POST /api/setup/ returns 200", r.status_code == 200)


# ── Phase 1: Event Ingestion ─────────────────────────────────────────
def phase_events(total: int):
    print(f"\n=== PHASE 1: EVENT INGESTION ({total} families) ===")
    _create_families(total)
    _run_failure_cases()
    _trigger_recompute()


def _create_families(total: int):
    scenarios = [_scenario_chain3, _scenario_chain4, _scenario_split, _scenario_merge]
    for i in range(total):
        country = COUNTRIES[i % len(COUNTRIES)]
        scenario_fn = random.choice(scenarios)
        event_groups, codes_with_dates = scenario_fn()
        dates = rand_date_sequence(len(event_groups))
        all_codes_dates: list[tuple[int, str]] = []

        for step, (transitions, d) in enumerate(zip(event_groups, dates)):
            for t in transitions:
                t["date"] = str(d)
            payload = {
                "iso_country_code": country,
                "transitions_write": transitions,
            }
            r = post("/api/events/", payload)
            if r.status_code not in (200, 201):
                print(f"  WARNING: family {i+1} step {step+1}: HTTP {r.status_code}")

        # Record codes and their intro dates for later verification
        for code_idx, code in enumerate(codes_with_dates):
            intro_date = str(dates[min(code_idx, len(dates) - 1)])
            all_codes_dates.append((code, intro_date))
        _families.append((country, all_codes_dates))

    print(f"  Created {total} families ({total * 3} avg events)")


def _scenario_chain3():
    """A → B → C"""
    a, b, c = next_code(), next_code(), next_code()
    groups = [
        [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": a}],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": b, "discontinuation_code": a},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": a},
        ],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c, "discontinuation_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": b},
        ],
    ]
    return groups, [a, b, c]


def _scenario_chain4():
    """A → B → C → D"""
    a, b, c, d = next_code(), next_code(), next_code(), next_code()
    groups = [
        [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": a}],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": b, "discontinuation_code": a},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": a},
        ],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c, "discontinuation_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": b},
        ],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": d},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": d, "discontinuation_code": c},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": c},
        ],
    ]
    return groups, [a, b, c, d]


def _scenario_split():
    """A → {B, C}"""
    a, b, c = next_code(), next_code(), next_code()
    groups = [
        [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": a}],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": b, "discontinuation_code": a},
        ],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c, "discontinuation_code": a},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": a},
        ],
    ]
    return groups, [a, b, c]


def _scenario_merge():
    """{A, B} → C"""
    a, b, c = next_code(), next_code(), next_code()
    groups = [
        [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": a}],
        [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": b}],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c},
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c, "discontinuation_code": a},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": a},
        ],
        [
            {"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c, "discontinuation_code": b},
            {"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": b},
        ],
    ]
    return groups, [a, b, c]


def _run_failure_cases():
    print("\n  --- Failure cases (must all be rejected with HTTP 400) ---")
    country = "PL"

    # Helper: create a code that exists
    def intro(code, d="2025-06-01"):
        post("/api/events/", {
            "iso_country_code": country,
            "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": code, "date": d}],
        })

    def discont(code, d="2025-06-15"):
        post("/api/events/", {
            "iso_country_code": country,
            "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": code, "date": d}],
        })

    def expect_400(label, payload):
        r = post("/api/events/", payload)
        check(label, r.status_code == 400)

    # 1. Double introduction
    c1 = next_code()
    intro(c1)
    expect_400("Double introduction", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c1, "date": "2025-06-10"}],
    })

    # 2. Overlapping generation
    c2 = next_code()
    intro(c2, "2025-06-01")
    discont(c2, "2025-06-10")
    expect_400("Overlapping generation", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": c2, "date": "2025-05-01"}],
    })

    # 3. Double discontinuation
    c3 = next_code()
    intro(c3, "2025-07-01")
    discont(c3, "2025-07-15")
    expect_400("Double discontinuation", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": c3, "date": "2025-07-20"}],
    })

    # 4. Chain with non-existing discontinuation code
    c4 = next_code()
    intro(c4, "2025-08-01")
    expect_400("Chain bad discontinuation_code", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c4, "discontinuation_code": 999999999, "date": "2025-08-15"}],
    })

    # 5. Chain with non-existing introduction code
    c5 = next_code()
    intro(c5, "2025-09-01")
    expect_400("Chain bad introduction_code", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": 999999998, "discontinuation_code": c5, "date": "2025-09-15"}],
    })

    # 6. Discontinuation of never-introduced code
    expect_400("Discont never-introduced", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "discontinuation_code": 999999997, "date": "2025-10-01"}],
    })

    # 7. Chain missing discontinuation_code
    expect_400("Chain missing discontinuation_code", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": next_code(), "date": "2025-11-01"}],
    })

    # 8. Introduction missing introduction_code
    expect_400("Intro missing introduction_code", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "date": "2025-11-15"}],
    })

    # 9. Discontinuation missing discontinuation_code
    expect_400("Discont missing discontinuation_code", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "DISCONT", "date": "2025-11-20"}],
    })

    # 10. Chain where introduction_code == discontinuation_code
    c10 = next_code()
    intro(c10, "2025-12-01")
    expect_400("Chain same codes", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "chain", "introduction_code": c10, "discontinuation_code": c10, "date": "2025-12-10"}],
    })

    # 11. Invalid country code
    expect_400("Invalid country code", {
        "iso_country_code": "ZZ",
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": next_code(), "date": "2025-12-15"}],
    })

    # 12. Invalid code type
    expect_400("Invalid code type", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": "NOPE", "type": "INTRO", "introduction_code": next_code(), "date": "2025-12-20"}],
    })

    # 13. Missing date
    expect_400("Missing date", {
        "iso_country_code": country,
        "transitions_write": [{"code_type_id": CODE_TYPE_ID, "type": "INTRO", "introduction_code": next_code()}],
    })


def _trigger_recompute():
    print("\n  Triggering family recomputation...")
    r = post("/api/product-families/recompute/", {})
    check("Recompute returns 200", r.status_code == 200)


# ── Phase 2: Family Queries ──────────────────────────────────────────
def phase_queries():
    print("\n=== PHASE 2: FAMILY QUERIES ===")
    if not _families:
        print("  No families to verify.")
        return

    # Pick up to 20 random families to spot-check
    sample = random.sample(_families, min(20, len(_families)))
    for country, codes_dates in sample:
        code, intro_date = codes_dates[0]
        r = get("/api/resolve/", {
            "code": code,
            "code_type": CODE_TYPE_ID,
            "country": country,
            "date": intro_date,
        })
        if r.status_code != 200:
            check(f"Resolve code={code} country={country}", False)
            continue

        data = r.json()
        family_id = data.get("product_family_identifier") or data.get("identifier")
        check(f"Resolve code={code} → family={family_id}", family_id is not None)

        # Verify all codes in the same family resolve to the same identifier
        if family_id and len(codes_dates) > 1:
            other_code, other_date = codes_dates[-1]
            r2 = get("/api/resolve/", {
                "code": other_code,
                "code_type": CODE_TYPE_ID,
                "country": country,
                "date": other_date,
            })
            if r2.status_code == 200:
                other_family = r2.json().get("product_family_identifier") or r2.json().get("identifier")
                check(f"  Same family for code={other_code}", other_family == family_id)

        # Reverse resolve
        if family_id:
            r3 = get("/api/resolve/reverse/", {"identifier": family_id, "date": intro_date})
            check(f"  Reverse resolve family={family_id}", r3.status_code == 200)


# ── Main ──────────────────────────────────────────────────────────────
def main():
    global BASE_URL

    parser = argparse.ArgumentParser(description="Demand Forecasting API test script")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Server URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL, help=f"Number of families to create (default: {DEFAULT_TOTAL})")
    args = parser.parse_args()
    BASE_URL = args.base_url

    print(f"Demand Forecasting API Test — target: {BASE_URL}")
    print(f"Families: {args.total}  Countries: {COUNTRIES}  CodeType: {CODE_TYPE_ID}")

    phase_setup()
    phase_events(args.total)
    phase_queries()

    print("\n=== RESULTS ===")
    print(f"  Passed: {stats['pass']}")
    print(f"  Failed: {stats['fail']}")
    total = stats["pass"] + stats["fail"]
    if total > 0:
        print(f"  Score:  {stats['pass']}/{total} ({100 * stats['pass'] / total:.0f}%)")

    sys.exit(0 if stats["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
