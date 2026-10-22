"""Does the model actually drive the action tools from plain English?

Verifying the tools work is not the same as verifying the agent uses them.
This runs a natural language instruction end to end, auto-approving the held
changes, then checks the repository itself rather than trusting the report.

    python scripts/dev/try_actions.py "create a folder called Q4 in Shared"
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from alfresco_curator.agent.actions import AuditLog
from alfresco_curator.agent.deep import (
    approve_all,
    build_deep_agent,
    resume_deep_agent,
    run_deep_agent,
)
from alfresco_curator.alfresco.client import AlfrescoClient

load_dotenv()

DEFAULT = (
    "In the Shared folder, create a folder named Quarterly-Reports with the "
    "title 'Quarterly Reports', then add the tag 'quarterly' to it."
)


def main() -> int:
    prompt = " ".join(sys.argv[1:]) or DEFAULT

    client = AlfrescoClient("http://localhost:8080/alfresco")
    client.login("admin", "admin")

    audit = AuditLog()
    agent = build_deep_agent(client, audit=audit, allow_writes=True)

    print(f"prompt: {prompt}\n")
    state = run_deep_agent(agent, prompt, thread_id="actions-cli")

    turn = 0
    while state.get("pending") and turn < 8:
        turn += 1
        print(f"-- approval round {turn} --")
        for held in state["pending"]:
            print(f"   HOLD {held['action']}  {held['arguments']}")
        state = resume_deep_agent(agent, approve_all(state["pending"]), thread_id="actions-cli")

    print("\ntools the model chose:")
    for step in state["steps"]:
        print(f"   {step['node']:22} {step['detail'][:76]}")

    print(f"\naudit: {len(audit.entries)} attempted, {audit.changed} applied")
    for entry in audit.entries:
        print(f"   {entry.outcome:7} {entry.action:22} {entry.arguments}")

    print("\nreport:")
    for line in (state.get("report") or "").splitlines():
        if line.strip():
            print(f"   | {line.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
