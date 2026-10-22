import pytest

from alfresco_curator.alfresco import afts


class TestNodeRefValidation:
    def test_uuid_is_accepted(self):
        assert afts.node_ref("aee2bef2-57da-49a1-a2be-f257dab9a1b3") == (
            "workspace://SpacesStore/aee2bef2-57da-49a1-a2be-f257dab9a1b3"
        )

    def test_surrounding_whitespace_is_trimmed(self):
        assert afts.node_ref("  abc-123  ").endswith("/abc-123")

    @pytest.mark.parametrize(
        "malicious",
        [
            'abc" OR TYPE:"cm:content',
            "abc' OR '1'='1",
            "abc\\",
            "abc 123",
            "abc/def",
            "",
            "   ",
            None,
        ],
    )
    def test_injection_attempts_are_rejected(self, malicious):
        with pytest.raises(ValueError):
            afts.node_ref(malicious)

    def test_scope_to_node_rejects_injection(self):
        with pytest.raises(ValueError):
            afts.scope_to_node('TYPE:"cm:content"', 'x" OR ID:"y')
