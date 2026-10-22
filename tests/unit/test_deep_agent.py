"""The deep agent is the exploratory mode; the graph stays the reproducible one."""

from __future__ import annotations

from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from alfresco_curator.agent.deep import build_deep_agent, run_deep_agent


def client() -> Mock:
    stub = Mock()
    stub.detect_query_language.return_value = "afts"
    stub.username = "admin"
    return stub


class TestBuildDeepAgent:
    def test_it_compiles_into_a_runnable_graph(self):
        agent = build_deep_agent(client())
        assert hasattr(agent, "invoke")
        assert "tools" in agent.get_graph().nodes

    def test_the_probes_are_offered_as_tools(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        names = {tool.name for tool in create.call_args.kwargs["tools"]}
        assert {"search_repository", "clause_reach", "trace_pipeline"} <= names

    def test_the_scratchpad_tools_are_disowned(self):
        """deepagents supplies ls/read_file/write_file/delete/execute against a
        private in-memory workspace. Asked what it could do, the agent listed
        them as repository capabilities - offering to delete and edit files it
        has no such power over."""
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        prompt = create.call_args.kwargs["system_prompt"]
        assert "private scratchpad" in prompt
        assert "never Alfresco" in prompt

    def test_a_read_only_turn_still_knows_the_agent_can_act(self):
        """Asked what it could do on a read-only turn, the agent listed
        "modify the repository" under what it cannot do - true of the turn,
        false of the product, and it talks the user out of the capability."""
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), allow_writes=False)
        prompt = create.call_args.kwargs["system_prompt"]
        assert "property of this turn" in prompt
        assert "Never tell the user that changing the repository is beyond you" in prompt

    def test_an_acting_turn_is_given_the_action_rules_instead(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client(), allow_writes=True)
        prompt = create.call_args.kwargs["system_prompt"]
        assert "Find before you change" in prompt
        assert "property of this turn" not in prompt

    def test_investigation_is_split_across_subagents(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        subagents = {sub["name"]: sub for sub in create.call_args.kwargs["subagents"]}

        assert set(subagents) == {"forensics", "infrastructure"}
        forensic_tools = {tool.name for tool in subagents["forensics"]["tools"]}
        assert "trace_pipeline" not in forensic_tools, (
            "the expensive sweep must stay behind its own sub-agent"
        )

    def test_memory_persists_across_turns(self):
        with patch("alfresco_curator.agent.deep.create_deep_agent") as create:
            build_deep_agent(client())
        assert create.call_args.kwargs["checkpointer"] is not None


class TestRunDeepAgent:
    def test_the_final_answer_is_taken_from_the_last_spoken_message(self):
        agent = Mock()
        agent.invoke.return_value = {
            "messages": [
                HumanMessage(content="where is the spec"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "search_repository", "args": {"query": "x"}, "id": "1"}],
                ),
                ToolMessage(content="{}", tool_call_id="1"),
                AIMessage(content="Found it in Engineering."),
            ]
        }
        out = run_deep_agent(agent, "where is the spec")
        assert out["report"] == "Found it in Engineering."

    def test_each_tool_call_is_reported_as_a_step(self):
        agent = Mock()
        agent.invoke.return_value = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "search_repository", "args": {"query": "a"}, "id": "1"},
                        {"name": "clause_reach", "args": {"query": "a"}, "id": "2"},
                    ],
                ),
                AIMessage(content="done"),
            ]
        }
        out = run_deep_agent(agent, "prompt")
        assert [step["node"] for step in out["steps"]] == ["search_repository", "clause_reach"]

    def test_the_thread_id_is_passed_so_turns_accumulate(self):
        agent = Mock()
        agent.invoke.return_value = {"messages": [AIMessage(content="ok")]}
        run_deep_agent(agent, "prompt", thread_id="abc")
        assert agent.invoke.call_args.kwargs["config"]["configurable"]["thread_id"] == "abc"

    def test_a_run_with_no_spoken_answer_is_empty_not_a_crash(self):
        agent = Mock()
        agent.invoke.return_value = {"messages": []}
        assert run_deep_agent(agent, "prompt")["report"] == ""
