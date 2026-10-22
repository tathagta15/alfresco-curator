"""Exercise the pipeline tracer against the live stack.

python scripts/demo/trace_demo.py [nodeIdOrPath] [--explain]
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.telemetry import Telemetry
from alfresco_curator.diagnostics.tracer import trace_node

load_dotenv()

ICON = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reference = args[0] if args else "/Engineering/S3-Connectors/S3_Connector_Spec_Draft.pdf"
    query = args[1] if len(args) > 1 else 'TYPE:"cm:content" AND cm:title:"*S3*"'

    client = AlfrescoClient(os.getenv("ALFRESCO_BASE_URL", "http://localhost:8080/alfresco"))
    client.login(
        os.getenv("ALFRESCO_ADMIN_USER", "admin"),
        os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin"),
    )

    telemetry = Telemetry()
    result = trace_node(client, reference, telemetry, query)

    print(f"\nNode {result['nodeId']}  (searching as {result['searchingAs']})\n")
    for stage in result["stages"]:
        print(f"  [{ICON[stage['status']]}] {stage['stage']:<28} {stage['headline']}")
        for key, value in stage["detail"].items():
            if value in (None, {}, [], ""):
                continue
            rendered = json.dumps(value, default=str)
            print(f"           {key}: {rendered[:150]}")
        print()

    verdict = result["verdict"]
    print(f"VERDICT [{verdict['status'].upper()}] {verdict['summary']}")
    if verdict["brokenAt"]:
        print(f"         first stage at fault: {verdict['brokenAt']}")

    if "--explain" in sys.argv:
        from alfresco_curator.diagnostics.narrator import narrate

        print("\n" + "=" * 72)
        print(narrate(result))

    telemetry.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
