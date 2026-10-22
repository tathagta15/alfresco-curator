"""Run a diagnosis as any user and print the pipeline evidence with the report.

Useful for rehearsing the cases where the index, not the query, is at fault.

    python scripts/dev/diagnose_as.py demo.analyst demo1234 <node> "<afts>"
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2

    username, password, node, query = sys.argv[1:5]
    # Blank: the service resolves Alfresco itself, which differs between a
    # host process and the containerised Curator.
    acs = os.getenv("CURATOR_TARGET_BASE_URL", "")

    with httpx.Client(base_url=BASE, timeout=240.0) as http:
        connect = http.post(
            "/api/connect",
            json={"base_url": acs, "username": username, "password": password, "role": "search"},
        )
        if connect.status_code != 200:
            print(f"connect failed: {connect.text}")
            return 1

        # Elevated inspection, so an ACL-blocked node still yields its permissions.
        http.post(
            "/api/connect",
            json={
                "base_url": acs,
                "username": os.getenv("ALFRESCO_ADMIN_USER", "admin"),
                "password": os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin"),
                "role": "inspect",
            },
        )

        response = http.post("/api/diagnose", json={"query": query, "node": node})
        if response.status_code != 200:
            print(f"diagnose failed: {response.status_code} {response.text[:300]}")
            return 1

        body = response.json()
        facts = body["facts"]
        print(f"searching as   : {body['searchingAs']}")
        print(f"visible/indexed: {facts['node_visible_to_user']}")
        print(f"readable       : {facts['node_readable_by_searching_user']}")
        print(f"pipeline traced: {facts['pipeline_traced']}")

        pipeline = body.get("pipeline")
        if pipeline:
            print(
                f"\nverdict: {pipeline['verdict']['status'].upper()} "
                f"- {pipeline['verdict']['summary']}"
            )
            for stage in pipeline["stages"]:
                print(f"  [{stage['status'].upper():>4}] {stage['stage']}: {stage['headline']}")

        print("\n" + "=" * 72)
        print(body["report"])
        return 0


if __name__ == "__main__":
    sys.exit(main())
