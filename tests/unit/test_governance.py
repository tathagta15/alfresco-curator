import json

import pytest

from alfresco_curator.agent.actions import MUTATING_TOOLS
from alfresco_curator.agent.capabilities import MUTATING_CAPABILITIES
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


class TestPolicyOverride:
    def _settings(self, path):
        return Settings(
            _env_file=None,
            llm=LLMSettings(_env_file=None, LANGSMITH_TRACING=False),
            governance=GovernanceSettings(
                _env_file=None,
                policy_path=str(path),
                allowed_model_hosts=["deployment.example.eu"],
                minimise_metadata=False,
                mutation_budget=25,
            ),
        )

    def test_persists_override_and_applies_it_without_restarting(self, tmp_path):
        settings = self._settings(tmp_path / "policy.json")
        policy.write_policy_override(
            ["ollama.internal"],
            True,
            settings,
            trace_to_third_party=True,
            mutation_budget=7,
            allow_repository_changes=False,
            allow_content_changes=False,
            allow_access_changes=False,
            allow_folder_rules=False,
            allow_repository_actions=False,
        )

        assert policy.effective_policy(settings) == {
            "allowed_model_hosts": ["ollama.internal"],
            "minimise_metadata": True,
            "trace_to_third_party": True,
            "mutation_budget": 7,
            "allow_repository_changes": False,
            "allow_content_changes": False,
            "allow_access_changes": False,
            "allow_folder_rules": False,
            "allow_repository_actions": False,
            "overridden": True,
        }
        assert policy.minimise({"title": "private", "name": "safe"}, settings) == {"name": "safe"}

    def test_reset_restores_deployment_defaults(self, tmp_path):
        settings = self._settings(tmp_path / "policy.json")
        policy.write_policy_override(
            ["ollama.internal"], True, settings, trace_to_third_party=True, mutation_budget=7
        )
        policy.reset_policy_override(settings)

        assert policy.effective_policy(settings) == {
            "allowed_model_hosts": ["deployment.example.eu"],
            "minimise_metadata": False,
            "trace_to_third_party": False,
            "mutation_budget": 25,
            "allow_repository_changes": True,
            "allow_content_changes": True,
            "allow_access_changes": True,
            "allow_folder_rules": True,
            "allow_repository_actions": True,
            "overridden": False,
        }

    @pytest.mark.parametrize("budget", [0, -1, 1001, True])
    def test_rejects_out_of_range_or_non_integer_budgets(self, tmp_path, budget):
        settings = self._settings(tmp_path / "policy.json")
        with pytest.raises(ValueError):
            policy.write_policy_override(
                ["ollama.internal"], True, settings, mutation_budget=budget
            )

    @pytest.mark.parametrize("host", ["https://llm.example", "llm.example/path", "*.example.eu"])
    def test_rejects_urls_paths_and_wildcards(self, host):
        with pytest.raises(ValueError):
            policy.normalise_hosts([host])

    def test_normalises_and_deduplicates_exact_hosts(self):
        assert policy.normalise_hosts([" LLM.Example.EU. ", "llm.example.eu"]) == ["llm.example.eu"]


class TestChangeGates:
    @pytest.mark.parametrize(
        ("action", "settings", "expected"),
        [
            ("create_folder", {"allow_repository_changes": False}, "Repository changes are paused"),
            (
                "grant_permission",
                {"allow_access_changes": False},
                "Sharing and access changes are disabled",
            ),
            (
                "run_action",
                {"allow_repository_actions": False},
                "Alfresco repository actions are disabled",
            ),
            (
                "add_tag",
                {"allow_content_changes": False},
                "Document and folder changes are disabled",
            ),
            (
                "restore_deleted_node",
                {"allow_content_changes": False},
                "Document and folder changes are disabled",
            ),
            (
                "revert_version",
                {"allow_content_changes": False},
                "Document and folder changes are disabled",
            ),
            (
                "add_comment",
                {"allow_content_changes": False},
                "Document and folder changes are disabled",
            ),
            (
                "create_folder_rule",
                {"allow_folder_rules": False},
                "Alfresco folder-rule changes are disabled",
            ),
            ("create_folder", {}, ""),
        ],
    )
    def test_reports_the_policy_gate_for_a_change(self, monkeypatch, action, settings, expected):
        monkeypatch.setattr(policy, "effective_policy", lambda: settings)

        assert policy.change_denial(action).startswith(expected)

    def test_legacy_repository_action_override_also_blocks_folder_rules(self, tmp_path):
        policy_path = tmp_path / "policy.json"
        settings = Settings(
            _env_file=None,
            governance=GovernanceSettings(_env_file=None, policy_path=str(policy_path)),
        )
        policy_path.write_text('{"allow_repository_actions": false}', encoding="utf-8")

        assert policy.effective_policy(settings)["allow_folder_rules"] is False

    def test_every_builtin_mutation_has_a_governance_category(self):
        mutation_names = set(MUTATING_TOOLS) | set(MUTATING_CAPABILITIES)
        governed_names = (
            policy.CONTENT_CHANGE_ACTIONS
            | policy.ACCESS_CHANGE_ACTIONS
            | {"create_folder_rule", "run_action"}
        )

        assert mutation_names <= governed_names
