"""Delete the generated load-test tree, restoring the curated demo corpus.

The bulk documents sit under one folder, so removing that node removes the
whole tree in a single call.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from alfresco_curator.alfresco.client import AlfrescoClient

load_dotenv()

FOLDER = "Bulk"


def main() -> int:
    client = AlfrescoClient(os.getenv("ALFRESCO_BASE_URL", "http://localhost:8080/alfresco"))
    client.login(
        os.getenv("ALFRESCO_ADMIN_USER", "admin"),
        os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin"),
    )

    found = client.search_afts(
        f'TYPE:"cm:folder" AND cm:name:"{FOLDER}" AND PATH:"/app:company_home/*"',
        max_items=5,
    )
    entries = found.get("list", {}).get("entries", [])
    if not entries:
        print(f"No '{FOLDER}' folder found - nothing to remove.")
        return 0

    for item in entries:
        node = item["entry"]
        print(f"Deleting {node['name']} ({node['id']}) ...")
        client.delete_node(node["id"], permanent=True)
        print("  removed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
