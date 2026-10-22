"""Pre-demo dry run: verifies every deterministic step of the demo script.

Exercises search, clause isolation, ACL exclusion and elevated inspection
against the live repository. The LLM is not called by default, so this runs
without an API key and proves the parts the diagnostic report is grounded on.

    python scripts/demo/verify_demo.py
    python scripts/demo/verify_demo.py --with-reports   # also render the LLM reports
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

from alfresco_curator.alfresco.client import AlfrescoClient
from alfresco_curator.diagnostics.search_debugger import (
    collect_facts,
    diagnose,
    load_node,
    load_parent_acl,
)
from alfresco_curator.errors import AlfrescoError

load_dotenv()

DEMO_QUERY = (
    'TYPE:"cm:content" AND cm:title:"*S3*" AND cm:description:"*APPROVED*" '
    'AND PATH:"/app:company_home/cm:Engineering//*"'
)

failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
    if not passed:
        failures.append(label)


def wait_for_index(client: AlfrescoClient, node_id: str, timeout: int = 90) -> bool:
    """Batch indexing polls the database, so a new node is not searchable at once."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.count_matches(f'ID:"workspace://SpacesStore/{node_id}"').value > 0:
            return True
        time.sleep(5)
    return False


def find_node(client: AlfrescoClient, path: str) -> str:
    return client.resolve_node_id(path)


def main() -> int:
    base_url = os.getenv("ALFRESCO_BASE_URL", "http://localhost:8080/alfresco")
    admin_user = os.getenv("ALFRESCO_ADMIN_USER", "admin")
    admin_password = os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin")

    admin = AlfrescoClient(base_url)
    admin.login(admin_user, admin_password)

    approved = find_node(admin, "/Engineering/S3-Connectors/S3_Connector_Spec_2026.pdf")
    draft = find_node(admin, "/Engineering/S3-Connectors/S3_Connector_Spec_Draft.pdf")
    restricted = find_node(admin, "/Confidential-Architecture/S3_OIDC_Security_Review.pdf")

    print("\nWaiting for batch indexing to pick up the seeded nodes...")
    if not wait_for_index(admin, approved):
        print("  seeded content is still not indexed; aborting")
        return 1
    wait_for_index(admin, draft)
    print("  indexed\n")

    print("A. Forward search as admin")
    result = admin.search_afts(DEMO_QUERY, max_items=25)
    names = [e["entry"]["name"] for e in result["list"]["entries"]]
    check("approved spec is returned", "S3_Connector_Spec_2026.pdf" in names, str(names))
    check("draft spec is excluded", "S3_Connector_Spec_Draft.pdf" not in names)
    check(
        "restricted doc is outside the PATH clause",
        "S3_OIDC_Security_Review.pdf" not in names,
    )

    print("\nB. Clause isolation on the draft (as admin)")
    facts = collect_facts(admin, DEMO_QUERY, draft)
    check("draft is indexed and visible", facts["node_visible_to_user"] is True)
    check("whole query does not match the draft", facts["whole_query_matches_node"] is False)
    failing = facts["failing_clauses"]
    check("exactly one clause fails", len(failing) == 1, str(failing))
    check(
        "the failing clause is the APPROVED description filter",
        any("APPROVED" in c for c in facts["failing_clauses"]),
    )
    for clause in facts["clauses"]:
        print(f"      {'PASS' if clause['matches_node'] else 'FAIL'}  {clause['clause']}")

    print("\nC. ACL exclusion as demo.analyst")
    analyst = AlfrescoClient(base_url)
    try:
        analyst.login("demo.analyst", "demo1234")
    except AlfrescoError as exc:
        check("demo.analyst can log in", False, str(exc))
        return 1

    analyst_names = [
        e["entry"]["name"] for e in analyst.search_afts(DEMO_QUERY, max_items=25)["list"]["entries"]
    ]
    check("analyst still sees the approved spec", "S3_Connector_Spec_2026.pdf" in analyst_names)

    acl_facts = collect_facts(analyst, DEMO_QUERY, restricted)
    check("restricted node is invisible to the analyst", acl_facts["node_visible_to_user"] is False)

    as_analyst = load_node(analyst, restricted)
    check(
        "node read is refused for the analyst",
        as_analyst.get("_status") in (403, 404),
        f"status {as_analyst.get('_status')}",
    )

    print("\nD. Elevated inspection recovers the permission tree")
    as_admin = load_node(admin, restricted)
    check("admin can read the node", not as_admin.get("_unreadable"))

    # The document itself inherits; the restriction lives on the parent folder.
    inherited = as_admin.get("permissions", {}).get("inherited", []) or []
    check(
        "node grants access only to administrators",
        {p.get("authorityId") for p in inherited} == {"GROUP_ALFRESCO_ADMINISTRATORS"},
        str([p.get("authorityId") for p in inherited]),
    )

    parent = load_parent_acl(admin, as_admin)
    check("parent folder ACL is readable", bool(parent) and not parent.get("_unreadable"))
    parent_perms = (parent or {}).get("permissions", {})
    check(
        "inheritance is broken on the parent folder",
        parent_perms.get("isInheritanceEnabled") is False,
        f"{(parent or {}).get('name')} -> {parent_perms.get('isInheritanceEnabled')}",
    )
    locally_set = parent_perms.get("locallySet", []) or []
    check(
        "parent folder grants only administrators",
        {p.get("authorityId") for p in locally_set} == {"GROUP_ALFRESCO_ADMINISTRATORS"},
        str([p.get("authorityId") for p in locally_set]),
    )

    print(
        f"\nNode ids for the live demo:\n"
        f"  approved   {approved}\n"
        f"  draft      {draft}\n"
        f"  restricted {restricted}\n"
        f"\nDemo AFTS query:\n  {DEMO_QUERY}\n"
    )

    if "--with-reports" in sys.argv:
        from alfresco_curator import llm

        print(f"Rendering LLM reports via {llm.provider()} / {llm.model()}\n")
        for label, node_id, searcher, inspector in (
            ("B. Draft document, diagnosed as admin", draft, admin, None),
            ("C. Restricted document, diagnosed as demo.analyst", restricted, analyst, None),
            ("D. Restricted document, with elevated inspection", restricted, analyst, admin),
        ):
            print("=" * 72)
            print(label)
            print("=" * 72)
            try:
                print(diagnose(searcher, DEMO_QUERY, node_id, inspector)["report"], "\n")
            except llm.LLMError as exc:
                check(f"report for {label}", False, str(exc))

    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
