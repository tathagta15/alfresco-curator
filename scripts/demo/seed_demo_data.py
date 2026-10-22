"""Seed the demo dataset used in the live walkthrough.

Creates three documents covering the three failure classes the Curator explains:

  /Company Home/Engineering/S3-Connectors            (inherited permissions)
    S3_Connector_Spec_2026.pdf    STATUS:APPROVED  -> the document that MATCHES
    S3_Connector_Spec_Draft.pdf   STATUS:DRAFT     -> metadata mismatch failure
  /Company Home/Confidential-Architecture            (inheritance broken, admins only)
    S3_OIDC_Security_Review.pdf   STATUS:APPROVED  -> ACL exclusion failure

Also creates `demo.analyst`. The ACL case is only demonstrable as a non-admin:
admin sees everything, so the exclusion would never reproduce.

    python seed_demo_data.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv

from alfresco_curator.alfresco.client import CORE_API, AlfrescoClient
from alfresco_curator.errors import AlfrescoError

load_dotenv()

DEMO_USER = "demo.analyst"
DEMO_USER_PASSWORD = "demo1234"
ADMIN_GROUP = "GROUP_ALFRESCO_ADMINISTRATORS"


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(title: str, lines: list[str]) -> bytes:
    """Minimal but well-formed PDF (valid xref) so text extraction actually works.

    Laid out like a real document - heading, rule, body - so the doclib
    rendition is a recognisable page thumbnail rather than a blank sheet.
    """
    ops = [
        "BT",
        "/F2 18 Tf",
        "72 720 Td",
        f"({_escape(title)}) Tj",
        "ET",
        # Rule under the heading.
        "0.4 w",
        "72 706 m",
        "540 706 l",
        "S",
        "BT",
        "/F1 11 Tf",
        "72 680 Td",
        "16 TL",
    ]
    for line in lines:
        ops.append(f"({_escape(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


def session(client: AlfrescoClient) -> httpx.Client:
    return client._client


def check(resp: httpx.Response) -> httpx.Response:
    AlfrescoClient._check(resp)
    return resp


def create_folder(client: AlfrescoClient, parent_id: str, name: str) -> str:
    resp = session(client).post(
        f"{CORE_API}/nodes/{parent_id}/children",
        json={"name": name, "nodeType": "cm:folder"},
    )
    if resp.status_code == 409:
        existing = check(
            session(client).get(f"{CORE_API}/nodes/{parent_id}", params={"relativePath": name})
        )
        print(f"  folder exists: {name}")
        return existing.json()["entry"]["id"]
    print(f"  created folder: {name}")
    return check(resp).json()["entry"]["id"]


def upload_document(
    client: AlfrescoClient,
    parent_id: str,
    name: str,
    title: str,
    description: str,
    body_lines: list[str],
    *,
    mime_type: str = "application/pdf",
    author: str | None = None,
    tags: list[str] | None = None,
    aspects: list[str] | None = None,
) -> str:
    content = (
        build_pdf(title, body_lines)
        if mime_type == "application/pdf"
        else ("\n".join([title, "=" * len(title), "", *body_lines]) + "\n").encode("utf-8")
    )

    resp = check(
        session(client).post(
            f"{CORE_API}/nodes/{parent_id}/children",
            files={"filedata": (name, content, mime_type)},
            data={"name": name, "nodeType": "cm:content", "overwrite": "true"},
        )
    )
    node_id = resp.json()["entry"]["id"]

    properties: dict[str, str] = {}
    if title:
        properties["cm:title"] = title
    if description:
        properties["cm:description"] = description
    if author:
        properties["cm:author"] = author

    # An empty title is a deliberate fixture, so cm:titled is only applied
    # when there is something to put in it.
    applied = list(aspects or (["cm:titled", "cm:versionable"] if title else ["cm:versionable"]))
    check(
        session(client).put(
            f"{CORE_API}/nodes/{node_id}",
            json={"aspectNames": applied, "properties": properties},
        )
    )

    for tag in tags or []:
        check(session(client).post(f"{CORE_API}/nodes/{node_id}/tags", json={"tag": tag}))

    print(f"  created document: {name} ({node_id})")
    return node_id


# A small but varied corpus, so natural language search has something real to
# work against: several departments, mixed mime types, authors, tags, and a few
# deliberate edge cases (no title, hyphenated name, near-duplicate wording).
CORPUS: list[dict[str, Any]] = [
    {
        "folder": ("Finance", "Invoices"),
        "name": "INV-2026-0142.pdf",
        "title": "Invoice 2026-0142 - Northwind Traders",
        "description": "STATUS:PAID - Cloud storage egress charges for August 2026.",
        "author": "finance.ops",
        "tags": ["invoice", "paid"],
        "body": [
            "Invoice number: INV-2026-0142",
            "Customer: Northwind Traders",
            "Period: August 2026",
            "",
            "Cloud storage egress            1,240.00 USD",
            "Support retainer                  800.00 USD",
            "Total due                       2,040.00 USD",
            "",
            "Payment received in full on 12 September 2026.",
        ],
    },
    {
        "folder": ("Finance", "Invoices"),
        "name": "INV-2026-0143.pdf",
        "title": "Invoice 2026-0143 - Contoso Ltd",
        "description": "STATUS:UNPAID - Awaiting purchase order confirmation.",
        "author": "finance.ops",
        "tags": ["invoice", "unpaid"],
        "body": [
            "Invoice number: INV-2026-0143",
            "Customer: Contoso Ltd",
            "Period: September 2026",
            "",
            "Professional services            6,500.00 USD",
            "Total due                        6,500.00 USD",
            "",
            "Overdue by 14 days. Escalate to the account manager.",
        ],
    },
    {
        "folder": ("Finance", "Budgets"),
        "name": "FY26_Budget_Forecast.csv",
        "title": "FY26 Budget Forecast",
        "description": "STATUS:DRAFT - Departmental forecast, not yet signed off.",
        "author": "finance.ops",
        "tags": ["budget", "forecast"],
        "mime_type": "text/csv",
        "body": [
            "department,quarter,forecast_usd,actual_usd",
            "Engineering,Q1,420000,411500",
            "Engineering,Q2,435000,448200",
            "Marketing,Q1,180000,176400",
            "Legal,Q1,95000,91250",
        ],
    },
    {
        "folder": ("HR", "Policies"),
        "name": "Remote_Working_Policy.pdf",
        "title": "Remote Working Policy",
        "description": "STATUS:APPROVED - Effective from 1 January 2026.",
        "author": "hr.people",
        "tags": ["policy", "remote-working"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Eligibility",
            "All permanent employees may work remotely up to three days a week,",
            "subject to their manager's agreement and team coverage.",
            "",
            "2. Equipment",
            "The company provides a laptop and monitor. Home office furniture is",
            "claimed through the expenses policy.",
            "",
            "3. Security",
            "Company data must not be stored on personal devices.",
        ],
    },
    {
        "folder": ("HR", "Policies"),
        "name": "Expenses_Policy.pdf",
        "title": "Employee Expenses Policy",
        "description": "STATUS:APPROVED - Supersedes the 2024 expenses guidance.",
        "author": "hr.people",
        "tags": ["policy", "expenses"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Travel",
            "Book economy class for flights under six hours. Rail is preferred",
            "for domestic journeys.",
            "",
            "2. Submission",
            "Submit receipts within 30 days. Claims older than 90 days are",
            "rejected automatically.",
        ],
    },
    {
        "folder": ("Legal", "Contracts"),
        "name": "MSA_Northwind_2026.pdf",
        "title": "Master Services Agreement - Northwind Traders",
        "description": "STATUS:SIGNED - Countersigned 4 February 2026, term three years.",
        "author": "legal.counsel",
        "tags": ["contract", "signed", "northwind"],
        "body": [
            "STATUS: SIGNED",
            "",
            "1. Term",
            "This agreement runs for three years from the effective date and",
            "renews annually unless terminated with ninety days notice.",
            "",
            "2. Service levels",
            "Availability of 99.9 percent measured monthly, excluding planned",
            "maintenance notified five working days in advance.",
            "",
            "3. Liability",
            "Aggregate liability is capped at the fees paid in the preceding",
            "twelve months.",
        ],
    },
    {
        "folder": ("Legal", "Contracts"),
        "name": "NDA_Contoso_Draft.pdf",
        "title": "Mutual Non-Disclosure Agreement - Contoso Ltd",
        "description": "STATUS:DRAFT - With counsel for review, do not circulate.",
        "author": "legal.counsel",
        "tags": ["contract", "nda", "confidential"],
        "body": [
            "STATUS: DRAFT",
            "",
            "1. Confidential information",
            "Each party may disclose technical and commercial information that",
            "is marked confidential or would reasonably be understood as such.",
            "",
            "2. Duration",
            "Obligations survive for five years after disclosure.",
        ],
    },
    {
        "folder": ("Engineering", "Runbooks"),
        "name": "Elasticsearch_Reindex_Runbook.pdf",
        "title": "Elasticsearch Reindex Runbook",
        "description": "STATUS:APPROVED - Operational procedure for a full reindex.",
        "author": "platform.ops",
        "tags": ["runbook", "elasticsearch", "operations"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. When to reindex",
            "Run a full reindex after a content model change, or when the",
            "batch indexer reports sustained write failures.",
            "",
            "2. Procedure",
            "Stop the live indexer, snapshot the index, then start the",
            "reindexing component with the required date range.",
            "",
            "3. Verification",
            "Compare the repository node count against the index document",
            "count before returning the system to service.",
        ],
    },
    {
        "folder": ("Engineering", "Runbooks"),
        "name": "Incident_Postmortem_2026-09.pdf",
        "title": "Incident Postmortem - Search Outage September 2026",
        "description": "STATUS:APPROVED - Root cause was a transform backlog.",
        "author": "platform.ops",
        "tags": ["incident", "postmortem", "search"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Summary",
            "Newly uploaded documents were missing from search results for",
            "roughly four hours on 18 September 2026.",
            "",
            "2. Root cause",
            "A transform backlog starved full text extraction, so metadata",
            "was indexed while document content was not.",
            "",
            "3. Actions",
            "Alert on transform queue depth and on the gap between the",
            "metadata and content watermarks.",
        ],
    },
    {
        "folder": ("Engineering", "Meeting-Notes"),
        "name": "Architecture_Review_Notes.txt",
        "title": "Architecture Review - September 2026",
        "description": "STATUS:DRAFT - Notes taken during the review, unedited.",
        "author": "platform.ops",
        "tags": ["meeting-notes", "architecture"],
        "mime_type": "text/plain",
        "body": [
            "Attendees: platform, security, delivery",
            "",
            "- Agreed to move the connector to OIDC role assumption.",
            "- Storage egress costs need a quarterly review.",
            "- Reindex runbook to be rehearsed before the next release.",
            "- Open question: retention policy for postmortems.",
        ],
    },
    {
        "folder": ("Engineering", "S3-Connectors"),
        "name": "S3-Connector-Handover-Notes.pdf",
        "title": "S3 Connector Handover Notes",
        "description": "STATUS:APPROVED - Handover from the build team to operations.",
        "author": "platform.ops",
        "tags": ["handover", "s3"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Ownership",
            "Operations owns the connector from the 2026.3 release onward.",
            "",
            "2. Known issues",
            "Multipart uploads above five gigabytes have not been exercised",
            "against the production bucket.",
        ],
    },
    {
        "folder": ("Marketing", "Campaigns"),
        "name": "Q4_Launch_Brief.pdf",
        "title": "Q4 Launch Campaign Brief",
        "description": "STATUS:APPROVED - Creative brief for the Q4 product launch.",
        "author": "marketing.team",
        "tags": ["campaign", "launch"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Objective",
            "Drive trial sign-ups for the new storage connector among existing",
            "enterprise customers.",
            "",
            "2. Channels",
            "Email to the installed base, a partner webinar, and a technical",
            "blog series.",
        ],
    },
    {
        # No title: exercises "documents with no title set" and the aspect logic.
        "folder": ("Marketing", "Campaigns"),
        "name": "untitled_campaign_scratch.txt",
        "title": "",
        "description": "",
        "author": "marketing.team",
        "tags": ["scratch"],
        "mime_type": "text/plain",
        "body": [
            "rough ideas for the launch - nothing decided yet",
            "maybe a customer story from Northwind?",
        ],
    },
    {
        "folder": ("Legal", "Contracts"),
        "name": "Services_Agreement_Fabrikam_Draft.pdf",
        "title": "Draft Services Agreement - Fabrikam Industries",
        "description": "STATUS:DRAFT - First pass, legal review not started.",
        "author": "legal.counsel",
        "tags": ["contract", "agreement", "draft", "fabrikam"],
        "body": [
            "STATUS: DRAFT",
            "",
            "This draft services agreement covers managed hosting for the",
            "Fabrikam Industries content platform.",
            "",
            "1. Scope",
            "Hosting, monitoring and quarterly capacity review.",
            "",
            "2. Open points",
            "Liability cap and data residency remain unagreed.",
        ],
    },
    {
        "folder": ("Legal", "Policies"),
        "name": "Data_Retention_Policy.pdf",
        "title": "Data Retention Policy",
        "description": "STATUS:APPROVED - Retention schedule by record class.",
        "author": "legal.counsel",
        "tags": ["policy", "retention", "compliance"],
        "body": [
            "STATUS: APPROVED",
            "",
            "1. Record classes",
            "Contracts are retained for seven years after expiry. Invoices are",
            "retained for six years. Incident postmortems are retained",
            "indefinitely.",
            "",
            "2. Disposal",
            "Records past their retention date are moved to the archive and",
            "destroyed after a final legal hold check.",
        ],
    },
    {
        "folder": ("Archive", "2024"),
        "name": "Legacy_Storage_Policy_2024.pdf",
        "title": "Legacy Storage Policy 2024",
        "description": "STATUS:ARCHIVED - Superseded by the 2026 retention policy.",
        "author": "legal.counsel",
        "tags": ["policy", "archived", "legacy"],
        "body": [
            "STATUS: ARCHIVED",
            "",
            "This policy governed on-premises storage tiers before the move to",
            "object storage. It is retained for audit reference only and must",
            "not be cited in current work.",
        ],
    },
    {
        "folder": ("Archive", "2024"),
        "name": "Decommissioned_Solr_Notes.txt",
        "title": "Decommissioned Solr Search Notes",
        "description": "STATUS:ARCHIVED - Solr was replaced by Elasticsearch.",
        "author": "platform.ops",
        "tags": ["archived", "solr", "search"],
        "mime_type": "text/plain",
        "body": [
            "Solr ran the search index until the 2026.1 upgrade.",
            "Sharding was manual and the cores drifted under load.",
            "Replaced by Elasticsearch with batch indexing.",
        ],
    },
    {
        "folder": ("Engineering", "Security"),
        "name": "CVE-2026-1337_Assessment.pdf",
        "title": "CVE-2026-1337 Impact Assessment",
        "description": "STATUS:URGENT - Exploitable in the document preview path.",
        "author": "security.team",
        "tags": ["security", "cve", "urgent"],
        "body": [
            "STATUS: URGENT",
            "",
            "1. Summary",
            "CVE-2026-1337 allows a crafted document to trigger remote code",
            "execution in the preview transformer.",
            "",
            "2. Affected versions",
            "Transform core 5.1.0 through 5.1.4 inclusive.",
            "",
            "3. Mitigation",
            "Upgrade to 5.1.5, or disable preview generation for untrusted",
            "uploads until the upgrade lands.",
        ],
    },
    {
        "folder": ("Engineering", "Security"),
        "name": "Penetration_Test_Report_2026.pdf",
        "title": "Penetration Test Report 2026",
        "description": "STATUS:APPROVED - Two medium findings, both remediated.",
        "author": "security.team",
        "tags": ["security", "pentest"],
        "body": [
            "STATUS: APPROVED",
            "",
            "Finding 1 (medium): session tickets were not rotated after a",
            "privilege change. Remediated.",
            "",
            "Finding 2 (medium): verbose error messages leaked internal paths.",
            "Remediated.",
            "",
            "No high or critical findings were identified.",
        ],
    },
    {
        "folder": ("Engineering", "Specs"),
        "name": "API_Rate_Limiting_Spec.md",
        "title": "API Rate Limiting Specification",
        "description": "STATUS:DRAFT - Awaiting sign-off from the platform guild.",
        "author": "platform.ops",
        "tags": ["spec", "api", "rate-limiting"],
        "mime_type": "text/markdown",
        "body": [
            "# API Rate Limiting",
            "",
            "## Limits",
            "- 100 requests per minute per authenticated user",
            "- 20 requests per minute for unauthenticated callers",
            "",
            "## Behaviour",
            "Exceeding a limit returns HTTP 429 with a Retry-After header.",
        ],
    },
    {
        "folder": ("Operations", "Config"),
        "name": "indexer_settings.json",
        "title": "Batch Indexer Settings",
        "description": "STATUS:APPROVED - Production batch indexing configuration.",
        "author": "platform.ops",
        "tags": ["config", "indexer"],
        "mime_type": "application/json",
        "body": [
            "{",
            '  "pollIntervalSeconds": 15,',
            '  "batchSize": 1000,',
            '  "indexName": "alfresco",',
            '  "contentExtraction": true',
            "}",
        ],
    },
    {
        "folder": ("Support", "Tickets"),
        "name": "Ticket_4821_Search_Latency.txt",
        "title": "Ticket 4821 - Search Latency Complaint",
        "description": "STATUS:OPEN - Customer reports slow search after bulk import.",
        "author": "support.desk",
        "tags": ["ticket", "search", "open"],
        "mime_type": "text/plain",
        "body": [
            "Customer: Northwind Traders",
            "Severity: 3",
            "",
            "Search results take eight seconds after their overnight bulk",
            "import. Suspect the batch indexer is still draining the backlog.",
            "Asked the customer for the indexer watermark timestamps.",
        ],
    },
    {
        "folder": ("Sales", "Proposals"),
        "name": "Proposal_Fabrikam_2026.pdf",
        "title": "Proposal - Fabrikam Industries Platform Migration",
        "description": "STATUS:SUBMITTED - Awaiting customer decision.",
        "author": "sales.team",
        "tags": ["proposal", "fabrikam"],
        "body": [
            "STATUS: SUBMITTED",
            "",
            "1. Opportunity",
            "Migrate Fabrikam Industries from their legacy document store to a",
            "managed content platform.",
            "",
            "2. Commercials",
            "Three year term, with a phased migration across four quarters.",
        ],
    },
    {
        "folder": ("Finance", "Invoices"),
        "name": "INV-2026-0144.pdf",
        "title": "Invoice 2026-0144 - Fabrikam Industries",
        "description": "STATUS:UNPAID - Second reminder sent.",
        "author": "finance.ops",
        "tags": ["invoice", "unpaid", "fabrikam"],
        "body": [
            "Invoice number: INV-2026-0144",
            "Customer: Fabrikam Industries",
            "Period: September 2026",
            "",
            "Migration consultancy          12,750.00 USD",
            "Total due                      12,750.00 USD",
            "",
            "Unpaid. Second reminder sent 28 September 2026.",
        ],
    },
    {
        "folder": ("HR", "Onboarding"),
        "name": "New_Starter_Checklist.pdf",
        "title": "New Starter Onboarding Checklist",
        "description": "STATUS:APPROVED - Day one through week four.",
        "author": "hr.people",
        "tags": ["onboarding", "checklist"],
        "body": [
            "STATUS: APPROVED",
            "",
            "Day one: accounts, laptop, building access.",
            "Week one: team introductions and the security briefing.",
            "Week two: read the expenses policy and the remote working policy.",
            "Week four: first objectives agreed with the line manager.",
        ],
    },
]


def seed_corpus(client: AlfrescoClient) -> int:
    """Create the wider corpus that makes search results look like a real repository."""
    print("\n  wider corpus")
    folders: dict[tuple[str, ...], str] = {}

    for item in CORPUS:
        path = tuple(item["folder"])
        for depth in range(1, len(path) + 1):
            branch = path[:depth]
            if branch not in folders:
                parent = folders[branch[:-1]] if depth > 1 else "-root-"
                folders[branch] = create_folder(client, parent, branch[-1])

        upload_document(
            client,
            folders[path],
            item["name"],
            item["title"],
            item["description"],
            item["body"],
            mime_type=item.get("mime_type", "application/pdf"),
            author=item.get("author"),
            tags=item.get("tags"),
        )

    return len(CORPUS)


def restrict_to_admins(client: AlfrescoClient, node_id: str) -> None:
    check(
        session(client).put(
            f"{CORE_API}/nodes/{node_id}",
            json={
                "permissions": {
                    "isInheritanceEnabled": False,
                    "locallySet": [
                        {
                            "authorityId": ADMIN_GROUP,
                            "name": "Coordinator",
                            "accessStatus": "ALLOWED",
                        }
                    ],
                }
            },
        )
    )
    print(f"  broke ACL inheritance on {node_id} (admins only)")


def ensure_demo_user(client: AlfrescoClient) -> None:
    resp = session(client).post(
        f"{CORE_API}/people",
        json={
            "id": DEMO_USER,
            "firstName": "Demo",
            "lastName": "Analyst",
            "email": f"{DEMO_USER}@example.com",
            "password": DEMO_USER_PASSWORD,
        },
    )
    if resp.status_code == 409:
        print(f"  user exists: {DEMO_USER}")
        return
    check(resp)
    print(f"  created user: {DEMO_USER} / {DEMO_USER_PASSWORD}")


def main() -> int:
    base_url = os.getenv("ALFRESCO_BASE_URL", "http://localhost:8080/alfresco")
    admin = os.getenv("ALFRESCO_ADMIN_USER", "admin")
    password = os.getenv("ALFRESCO_ADMIN_PASSWORD", "admin")

    client = AlfrescoClient(base_url)
    client.login(admin, password)
    print(f"connected to {base_url} as {admin}\n")

    ensure_demo_user(client)

    engineering = create_folder(client, "-root-", "Engineering")
    connectors = create_folder(client, engineering, "S3-Connectors")
    confidential = create_folder(client, "-root-", "Confidential-Architecture")
    restrict_to_admins(client, confidential)

    approved = upload_document(
        client,
        connectors,
        "S3_Connector_Spec_2026.pdf",
        "S3 Cloud Storage Connector Production Architecture",
        "STATUS:APPROVED - Complete production specification for the S3 storage "
        "connector with OIDC authentication.",
        [
            "STATUS: APPROVED",
            "",
            "1. Scope",
            "Architecture specification for AWS S3 direct multipart upload in",
            "Alfresco Content Services, using OIDC role assumption for access.",
            "",
            "2. Authentication",
            "The connector exchanges a short-lived OIDC token for temporary STS",
            "credentials. No long-lived IAM access keys are stored by the repository.",
            "",
            "3. Transfer",
            "Objects above the multipart threshold are uploaded in parallel parts",
            "and completed with a single CompleteMultipartUpload call.",
            "",
            "4. Failure handling",
            "Interrupted uploads are resumed from the last acknowledged part.",
            "Abandoned uploads are reaped by a lifecycle policy after seven days.",
        ],
    )
    draft = upload_document(
        client,
        connectors,
        "S3_Connector_Spec_Draft.pdf",
        "S3 Connector NextGen Staging",
        "STATUS:DRAFT - Work in progress architecture draft.",
        [
            "STATUS: DRAFT - not for review",
            "",
            "1. Purpose",
            "Draft specification capturing S3 connector benchmark results for the",
            "next generation staging environment.",
            "",
            "2. Open questions",
            "Throughput targets are unconfirmed pending a second benchmark run.",
            "Part size and concurrency remain to be tuned against production data.",
            "",
            "3. Next steps",
            "Re-run the benchmark suite, then promote this document for approval.",
        ],
    )
    restricted = upload_document(
        client,
        confidential,
        "S3_OIDC_Security_Review.pdf",
        "S3 OIDC Keycloak Security Assessment",
        "STATUS:APPROVED - Confidential vulnerability scan and IAM role assumption review.",
        [
            "STATUS: APPROVED - CONFIDENTIAL",
            "",
            "1. Assessment scope",
            "IAM role chaining and STS token exchange assessment for S3 direct",
            "transfers, covering the Keycloak realm used by the connector.",
            "",
            "2. Findings",
            "Role trust policies are correctly scoped to the connector's audience.",
            "Token lifetimes exceed the recommended maximum and should be reduced.",
            "",
            "3. Recommendation",
            "Shorten the STS session duration and enable CloudTrail data events",
            "on the target bucket before production rollout.",
        ],
        tags=["confidential", "security", "s3"],
        author="security.review",
    )

    seed_corpus(client)

    print(
        f"""
Seeding complete.

  approved   (public)     {approved}
  draft      (public)     {draft}
  restricted (admin-only) {restricted}

Demo sequence
  1. Connect as {admin}. Search: "approved S3 connector specifications in Engineering"
     -> S3_Connector_Spec_2026.pdf is returned; the draft is not.
  2. Diagnose {draft} against that query
     -> metadata mismatch: cm:description is STATUS:DRAFT, not STATUS:APPROVED.
  3. Reconnect as {DEMO_USER} / {DEMO_USER_PASSWORD}, rerun the query,
     then diagnose {restricted}
     -> ACL exclusion: inheritance broken, {ADMIN_GROUP} only.

Indexing is asynchronous. Wait a few seconds, or re-run the search, before step 1.
"""
    )
    client.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AlfrescoError as exc:
        print(f"Alfresco error: {exc}", file=sys.stderr)
        sys.exit(1)
