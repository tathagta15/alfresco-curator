"""Exercise zero-hit analysis shapes against the running service.

Two failure modes look identical to a user but need different advice:
a single dead term, versus terms that each match but never co-occur.

    python scripts/dev/check_zero_hit.py
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")

CASES = [
    (
        "every term matches, intersection empty",
        'cm:name:"*INV-2026-0142*" AND cm:name:"*INV-2026-0143*"',
    ),
    (
        "one dead term",
        'TYPE:"cm:content" AND cm:name:"*definitely-not-present-xyz*"',
    ),
    (
        "mixed AND/OR still measured",
        'TYPE:"cm:content" AND cm:name:"*nope-xyz*" OR cm:name:"*also-nope*"',
    ),
]


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=120.0) as http:
        connect = http.post(
            "/api/connect",
            json={
                "base_url": "",
                "username": os.getenv("ALFRESCO_ADMIN_USER", "admin"),
                "password": os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin"),
                "role": "search",
            },
        )
        if connect.status_code != 200:
            print(f"connect failed: {connect.text}")
            return 1

        failures = 0
        for label, query in CASES:
            body = http.post("/api/search/raw", json={"query": query}).json()
            counts = body.get("clauseCounts", [])
            dead = [c["clause"] for c in counts if c["hits"] == 0]

            print(f"\n{label}")
            print(f"  query : {query}")
            print(f"  total : {body.get('total')}")
            for entry in counts:
                print(f"  {entry['hits']!s:>5}  {entry['clause']}")
            verdict = "dead term(s): " + ", ".join(dead) if dead else "no single term is at fault"
            print(f"  verdict: {verdict}")

            if not counts:
                print("  FAIL: no clause reach reported")
                failures += 1

        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
