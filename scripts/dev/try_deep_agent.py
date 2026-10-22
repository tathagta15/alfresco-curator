"""Run the deep agent against the live stack and show what it chose to do.

python scripts/dev/try_deep_agent.py "where is the Kubernetes runbook"
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("CURATOR_API", "http://127.0.0.1:8600")
DEFAULT = ["the penetration test report", "where is the Kubernetes migration runbook"]


def main() -> int:
    prompts = sys.argv[1:] or DEFAULT
    with httpx.Client(base_url=BASE, timeout=600.0) as http:
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
            response = http.post(
                "/api/agent/deep", json={"prompt": prompt, "thread_id": "deep-cli"}
            )
            print(f"\n{prompt}")
            if response.status_code != 200:
                print(f"  ERROR {response.status_code}: {response.text[:300]}")
                continue

            body = response.json()
            print(f"  messages : {body['messageCount']}")
            for step in body["steps"]:
                print(f"  tool     : {step['node']}  ({step['detail']})")
            for todo in body.get("todos", []):
                print(f"  plan     : {todo}")
            for line in (body.get("report") or "").splitlines():
                if line.strip():
                    print(f"           | {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
