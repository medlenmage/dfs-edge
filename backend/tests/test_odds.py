"""
Offline unit tests for the pure odds-conversion helpers.

Run it with:
    cd backend
    .venv/bin/python -m tests.test_odds
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.odds import american_to_probability  # noqa: E402

PASS, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    print("\nOdds conversion test (american_to_probability)\n" + "=" * 60)

    check("positive underdog odds convert correctly",
          american_to_probability(150) == 40.0,
          str(american_to_probability(150)))
    check("negative favorite odds convert correctly",
          american_to_probability(-150) == 60.0,
          str(american_to_probability(-150)))
    check("even odds convert to 50%",
          american_to_probability(100) == 50.0,
          str(american_to_probability(100)))
    check("non-numeric price returns None",
          american_to_probability("bad") is None)
    check("missing price returns None",
          american_to_probability(None) is None)

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
