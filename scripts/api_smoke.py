"""End-to-end check of the REST API (no browser involved).

python scripts/api_smoke.py
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")
DEMO_QUERY = (
    'TYPE:"cm:content" AND cm:title:"*S3*" AND cm:description:"*APPROVED*" '
    'AND PATH:"/app:company_home/cm:Engineering//*"'
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    # Blank: the service resolves Alfresco with its own configured address.
    # Sending the host address breaks once the Curator runs in a container,
    # where localhost is the container itself.
    acs = os.getenv("CURATOR_TARGET_BASE_URL", "")
    admin = os.getenv("ALFRESCO_ADMIN_USER", "admin")
    password = os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin")

    with httpx.Client(base_url=BASE, timeout=180.0) as http:
        print("Health and auth gate")
        health = http.get("/api/health").json()
        check("health reports the llm", bool(health["llm"]["model"]), health["llm"]["model"])
        check(
            "search is refused before connecting",
            http.post("/api/search", json={"prompt": "anything"}).status_code == 401,
        )

        print("\nConnect")
        resp = http.post(
            "/api/connect",
            json={"base_url": acs, "username": admin, "password": password, "role": "search"},
        )
        check("connect succeeds", resp.status_code == 200, resp.text[:120])
        check("status reports the identity", resp.json().get("searchingAs") == admin)
        check(
            "bad credentials are rejected",
            http.post(
                "/api/connect",
                json={"base_url": acs, "username": admin, "password": "wrong", "role": "search"},
            ).status_code
            == 400,
        )

        print("\nRaw search")
        raw = http.post("/api/search/raw", json={"query": DEMO_QUERY}).json()
        if "results" not in raw:
            check("raw search returned results", False, str(raw)[:160])
            return 1
        names = [r["name"] for r in raw["results"]]
        check(
            "demo query returns the approved spec",
            "S3_Connector_Spec_2026.pdf" in names,
            str(names),
        )
        check(
            "malformed query is rejected with a reason",
            http.post("/api/search/raw", json={"query": 'cm:name:"unterminated'}).status_code
            == 400,
        )
        check(
            "empty query is rejected",
            http.post("/api/search/raw", json={"query": "   "}).status_code == 400,
        )

        print("\nZero-hit self diagnosis")
        over_constrained = (
            'TYPE:"cm:content" AND TEXT:"approved S3 connector specifications" '
            'AND PATH:"/app:company_home/cm:Engineering//*"'
        )
        empty = http.post("/api/search/raw", json={"query": over_constrained}).json()
        check("query really returns nothing", empty["total"] == 0)
        counts = empty.get("clauseCounts", [])
        check("every clause was measured", len(counts) == 3, str(len(counts)))
        for entry in counts:
            print(f"      {entry['hits']!s:>5}  {entry['clause']}")
        dead = [c["clause"] for c in counts if c["hits"] == 0]
        check(
            "the phrase clause is identified as the blocker",
            len(dead) == 1 and "TEXT:" in dead[0],
            str(dead),
        )

        print("\nDiagnose")
        drafts = http.post(
            "/api/search/raw", json={"query": 'cm:name:"S3_Connector_Spec_Draft.pdf"'}
        ).json()["results"]
        draft = next(
            (r["id"] for r in drafts),
            None,
        )
        check("draft node located", bool(draft), str(draft))
        if draft:
            diag = http.post("/api/diagnose", json={"query": DEMO_QUERY, "node": draft})
            check("diagnose returns 200", diag.status_code == 200, diag.text[:160])
            if diag.status_code == 200:
                body = diag.json()
                failing = body["facts"]["failing_clauses"]
                check("exactly one failing clause", len(failing) == 1, str(failing))
                check("report is non-trivial", len(body["report"]) > 200)

        check(
            "injection in the node field is rejected",
            http.post(
                "/api/diagnose", json={"query": DEMO_QUERY, "node": 'x" OR ID:"y'}
            ).status_code
            == 400,
        )
        check(
            "unknown node is rejected cleanly",
            http.post(
                "/api/diagnose", json={"query": DEMO_QUERY, "node": "/no/such/path.pdf"}
            ).status_code
            == 400,
        )

        print("\nDisconnect")
        http.post("/api/disconnect")
        check(
            "search is refused after disconnect",
            http.post("/api/search", json={"prompt": "anything"}).status_code == 401,
        )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All API checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
