"""Run natural language prompts through the Curator and print what each produced.

Useful for rehearsing a demo script: it shows the generated AFTS, the hit
count, and - when nothing matched - which term was responsible.

    python scripts/dev/try_prompts.py
    python scripts/dev/try_prompts.py "unpaid invoices" "signed contracts"
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")

DEFAULT_PROMPTS = [
    "approved S3 connector specifications in the Engineering folder",
    "unpaid invoices",
    "invoices for Northwind Traders",
    "signed contracts",
    "draft legal agreements",
    "documents written by platform.ops",
    "runbooks about reindexing Elasticsearch",
    "the incident postmortem about the search outage",
    "spreadsheets and CSV files",
    "meeting notes from the architecture review",
    "everything in the Marketing folder",
    "documents that have no title set",
    "expenses policy",
    "anything mentioning OIDC role assumption",
    "folders called archive",
]


def main() -> int:
    prompts = sys.argv[1:] or DEFAULT_PROMPTS

    with httpx.Client(base_url=BASE, timeout=240.0) as http:
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

        for prompt in prompts:
            response = http.post("/api/search", json={"prompt": prompt})
            if response.status_code != 200:
                print(f"\n{prompt}\n  ERROR {response.status_code}: {response.text[:140]}")
                continue

            body = response.json()
            print(f"\n{prompt}")
            print(f"  AFTS : {body['aftsQuery']}")
            print(f"  hits : {body['total']}{'' if body.get('totalIsExact', True) else '+'}")
            for hit in body["results"][:5]:
                print(f"         {hit['name']}  ({hit.get('path') or ''})")

            for warning in body.get("warnings", []):
                print(f"  WARN : {warning[:110]}")

            for step in body.get("pathReach", []):
                print(f"  PATH : {step['hits']:>5} <- {step['path']}")

            dead = [c["clause"] for c in body.get("clauseCounts", []) if c["hits"] == 0]
            if dead:
                print(f"  dead : {', '.join(dead)}")
            elif body["total"] == 0 and body.get("clauseCounts"):
                print("  dead : none - every term matches, but not together")

            for index, step in enumerate(body.get("conflict", [])):
                prefix = "" if index == 0 else "+ "
                print(f"  narrow {step['hits']:>5}  {prefix}{step['clause']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
