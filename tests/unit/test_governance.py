import json

import pytest

from alfresco_curator.config import GovernanceSettings, LLMSettings, Settings
from alfresco_curator.governance import policy
from alfresco_curator.governance.ledger import GENESIS, Ledger
from alfresco_curator.governance.policy import PolicyError


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")


class TestLedger:
    def test_disabled_without_a_path(self):
        assert Ledger(None).enabled is False

    def test_first_record_chains_from_genesis(self, ledger):
        record = ledger.append("ask", "admin", {"prompt": "find invoices"})
        assert record["previous"] == GENESIS
        assert record["hash"] != GENESIS

    def test_each_record_chains_to_the_one_before(self, ledger):
        first = ledger.append("ask", "admin", {"a": 1})
        second = ledger.append("ask", "admin", {"b": 2})
        assert second["previous"] == first["hash"]

    def test_survives_a_new_instance_on_the_same_file(self, ledger):
        first = ledger.append("ask", "admin", {"a": 1})
        # A restarted container must continue the chain, not start a new one.
        reopened = Ledger(ledger.path)
        assert reopened.append("ask", "admin", {"b": 2})["previous"] == first["hash"]

    def test_verifies_an_untouched_chain(self, ledger):
        for index in range(5):
            ledger.append("decision", "admin", {"n": index})
        assert ledger.verify() == {"ok": True, "checked": 5, "brokenAt": None}

    def test_detects_an_edited_record(self, ledger):
        ledger.append("decision", "admin", {"action": "delete_node"})
        ledger.append("decision", "admin", {"action": "add_tag"})

        lines = ledger.path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["detail"]["action"] = "create_folder"
        lines[0] = json.dumps(tampered)
        ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert ledger.verify() == {"ok": False, "checked": 1, "brokenAt": 1}

    def test_detects_a_removed_record(self, ledger):
        for index in range(3):
            ledger.append("decision", "admin", {"n": index})

        lines = ledger.path.read_text(encoding="utf-8").splitlines()
        del lines[1]
        ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # The survivor's `previous` no longer matches the line before it.
        assert ledger.verify()["ok"] is False

    def test_a_write_failure_does_not_take_the_request_down(self, tmp_path):
        # The ledger path is occupied by a directory, so opening it fails.
        blocked = tmp_path / "occupied"
        blocked.mkdir()
        assert Ledger(blocked).append("ask", "admin", {"a": 1})["kind"] == "ask"


class TestEndpointAllowlist:
    def _settings(self, hosts, base_url="https://api.vendor.example/v1"):
        # Built explicitly rather than from a dict: the nested models are
        # BaseSettings, so anything not passed here is read from the real .env.
        return Settings(
            _env_file=None,
            llm=LLMSettings(_env_file=None, LLM_PROVIDER="openai", OPENAI_BASE_URL=base_url),
            governance=GovernanceSettings(_env_file=None, allowed_model_hosts=hosts),
        )

    def test_empty_allowlist_permits_anything(self):
        policy.assert_endpoint_allowed("openai", self._settings([]))

    def test_permits_a_listed_host(self):
        policy.assert_endpoint_allowed("openai", self._settings(["api.vendor.example"]))

    def test_refuses_an_unlisted_host(self):
        with pytest.raises(PolicyError, match="not in GOVERNANCE_ALLOWED_MODEL_HOSTS"):
            policy.assert_endpoint_allowed("openai", self._settings(["llm.internal.lan"]))

    def test_permits_a_sovereign_local_endpoint(self):
        settings = Settings(
            _env_file=None,
            llm=LLMSettings(
                _env_file=None,
                LLM_PROVIDER="ollama",
                OLLAMA_BASE_URL="http://ollama.internal:11434",
            ),
            governance=GovernanceSettings(_env_file=None, allowed_model_hosts=["ollama.internal"]),
        )
        policy.assert_endpoint_allowed("ollama", settings)


class TestMinimisation:
    def _settings(self, enabled):
        return Settings(
            _env_file=None,
            governance=GovernanceSettings(_env_file=None, minimise_metadata=enabled),
        )

    def test_passes_everything_through_when_disabled(self):
        record = {"name": "a.pdf", "title": "T", "description": "D", "path": "/x"}
        assert policy.minimise(record, self._settings(False)) == record

    def test_withholds_free_text_when_enabled(self):
        record = {"name": "a.pdf", "title": "T", "description": "D", "path": "/x"}
        assert policy.minimise(record, self._settings(True)) == {"name": "a.pdf", "path": "/x"}

    def test_keeps_what_identifies_the_document(self):
        # An answer that cannot name the document is not an answer, so these
        # survive minimisation by design.
        minimised = policy.minimise({"name": "a.pdf", "path": "/x"}, self._settings(True))
        assert minimised == {"name": "a.pdf", "path": "/x"}
