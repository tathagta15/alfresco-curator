"""Check how the repository reacts to a specific AFTS query.

python scripts/dev/try_query.py 'TYPE:"cm:content" AND cm:name:"*spec*"'
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.errors import AlfrescoError

load_dotenv()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    query = sys.argv[1]
    client = AlfrescoClient(os.getenv("ALFRESCO_BASE_URL", "http://localhost:8080/alfresco"))
    client.login(
        os.getenv("ALFRESCO_ADMIN_USER", "admin"),
        os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin"),
    )

    print(f"query: {query}")
    try:
        result = client.search_afts(query, max_items=10)
    except AlfrescoError as exc:
        print(f"ERROR {exc.status_code}: {exc.message[:300]}")
        return 1

    print(f"hits: {result['list']['pagination']['totalItems']}")
    for entry in result["list"]["entries"]:
        print(f"  {entry['entry']['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
