"""Run prompts through the agent and show the path it took.

python scripts/dev/try_agent.py
python scripts/dev/try_agent.py "where is the fabrikam proposal"
"""

from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")

DEFAULT_PROMPTS = [
    "the penetration test report",
    "urgent security assessments",
    "draft legal agreements",
    "documents in the Archive folder",
    "invoices for Fabrikam that are unpaid",
    "runbooks about reindexing Elasticsearch written by platform.ops",
]


def main() -> int:
    prompts = sys.argv[1:] or DEFAULT_PROMPTS

    with httpx.Client(base_url=BASE, timeout=300.0) as http:
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
            response = http.post("/api/agent", json={"prompt": prompt, "thread_id": "cli"})
            if response.status_code != 200:
                print(f"\n{prompt}\n  ERROR {response.status_code}: {response.text[:200]}")
                continue

            body = response.json()
            print(f"\n{prompt}")
            print(f"  AFTS  : {body['aftsQuery']}")
            print(f"  hits  : {body['total']}    cause: {body['cause']}")
            print(f"  path  : {' -> '.join(s['node'] for s in body['steps'])}")
            for hit in body["results"][:3]:
                print(f"          {hit['name']}  ({hit.get('path') or ''})")
                if hit.get("title"):
                    print(f"            title: {hit['title']}")
                if hit.get("description"):
                    print(f"            desc : {hit['description']}")

            for document in body.get("matchEvidence", []):
                clauses = ", ".join(document.get("matchedClauses", []))
                print(f"  why   : {document['name']} <- {clauses}")

            if body.get("relaxedQuery"):
                print(f"  relax : {body['relaxedTotal']} hit(s) without the dead clause")
                for hit in body.get("relaxedResults", [])[:3]:
                    print(f"          {hit['name']}")

            if body.get("infra"):
                verdict = body["infra"].get("verdict", {})
                print(f"  infra : {verdict.get('status')} - {verdict.get('summary')}")

            report = (body.get("report") or "").strip()
            if report:
                for line in report.splitlines():
                    if line.strip():
                        print(f"        | {line.strip()}")

            print(f"  cache : {body.get('cacheStats')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
