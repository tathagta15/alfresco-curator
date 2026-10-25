"""Prove the governance controls against the running service."""

import json
import urllib.request

BASE = "http://127.0.0.1:8600"


def call(method, path, payload=None, jar=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    if jar:
        request.add_header("Cookie", jar)
    with urllib.request.urlopen(request, timeout=180) as response:
        cookie = response.headers.get("Set-Cookie", "")
        return json.loads(response.read() or b"{}"), cookie.split(";")[0]


print("=== posture (before) ===")
posture, _ = call("GET", "/api/governance/posture")
print(json.dumps(posture, indent=2))

before = call("GET", "/api/governance/ledger?limit=1000")[0]["records"]
print(f"\nledger records before: {len(before)}")

_, jar = call("POST", "/api/connect", {"username": "admin", "password": "admin"})

print("\n=== one ask ===")
answer, _ = call("POST", "/api/agent/ask", {"prompt": "find the unpaid invoices"}, jar)
print("route:", answer.get("route"))
search = answer.get("search") or {}
print("hits:", len(search.get("results") or []), "state:", search.get("state"))
print("query:", search.get("aftsQuery") or search.get("query"))
print("report:", (search.get("report") or "")[:160].replace("\n", " "))

after = call("GET", "/api/governance/ledger?limit=1000")[0]["records"]
print(f"ledger records after: {len(after)}  (+{len(after) - len(before)})")

print("\n=== what was recorded ===")
for record in after[len(before) :]:
    detail = record["detail"]
    if record["kind"] == "ask":
        print(f"  ask      prompt={detail['prompt']!r} route={detail['route']}")
    elif record["kind"] == "egress":
        print(
            f"  egress   purpose={detail['purpose']} host={detail['endpoint']} "
            f"nodes={detail['nodeCount']} fields={detail['fields']}"
        )
    else:
        print(f"  {record['kind']} {detail}")

print("\n=== does any record contain document content? ===")
blob = json.dumps(after)
for probe in ("STATUS:UNPAID", "second reminder", "purchase order"):
    print(f"  {probe!r:24} present={probe in blob}")

print("\n=== chain verifies? ===")
print(json.dumps(call("GET", "/api/governance/verify")[0], indent=2))
