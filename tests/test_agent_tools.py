"""Native tool-calling: schema offering, overlay, and adapter parsing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cortiva.core.agent_tools import (
    OPTIMIZE_SCHEDULE_TOOL,
    apply_tool_calls_to_suffix,
    tools_for_agent,
)
from cortiva.core.reflection import ReflectionSuffix


class TestToolsForAgent:
    def test_scheduling_authorised_gets_the_optimiser_tool(self) -> None:
        tools = tools_for_agent("ar-scheduler", scheduling_authorised={"ar-scheduler"})
        assert any(t["function"]["name"] == "optimize_schedule" for t in tools)

    def test_unauthorised_agent_gets_only_universal_tools(self) -> None:
        # Authority-scoped tools (rota, etc.) are withheld, but every agent gets
        # the universal ones: email (reach a human), commitment tracking, and
        # coffee (pull overtime).
        tools = tools_for_agent("dev-1", scheduling_authorised={"ar-scheduler"})
        assert [t["function"]["name"] for t in tools] == [
            "send_email",
            "register_commitment",
            "update_commitment",
            "drink_coffee",
        ]

    def test_schema_shape_is_valid_openai_function(self) -> None:
        fn = OPTIMIZE_SCHEDULE_TOOL["function"]
        assert OPTIMIZE_SCHEDULE_TOOL["type"] == "function"
        assert fn["name"] == "optimize_schedule"
        assert "capacity_ceiling" in fn["parameters"]["properties"]
        assert fn["parameters"]["required"] == ["capacity_ceiling"]


class TestOverlay:
    def test_tool_call_overlays_onto_suffix(self) -> None:
        suffix = ReflectionSuffix()
        apply_tool_calls_to_suffix(suffix, [
            {"name": "optimize_schedule",
             "arguments": {"capacity_ceiling": 200, "apply": True}},
        ])
        assert suffix.optimize_schedule == {"capacity_ceiling": 200, "apply": True}

    def test_tool_call_takes_precedence_over_prose(self) -> None:
        suffix = ReflectionSuffix(optimize_schedule={"capacity_ceiling": 10})
        apply_tool_calls_to_suffix(suffix, [
            {"name": "optimize_schedule", "arguments": {"capacity_ceiling": 200}},
        ])
        assert suffix.optimize_schedule == {"capacity_ceiling": 200}

    def test_unknown_tool_ignored(self) -> None:
        suffix = ReflectionSuffix()
        apply_tool_calls_to_suffix(suffix, [{"name": "frobnicate", "arguments": {}}])
        assert suffix.optimize_schedule is None


class TestOpenAICompatToolParsing:
    @pytest.mark.asyncio
    async def test_passes_tools_and_parses_tool_calls(self, monkeypatch) -> None:
        from cortiva.adapters.consciousness.openai_compat import (
            OpenAICompatibleAdapter,
        )

        captured: dict = {}

        class _Msg:
            content = "I will optimise the rota."
            tool_calls = [
                SimpleNamespace(function=SimpleNamespace(
                    name="optimize_schedule",
                    arguments='{"capacity_ceiling": 130, "apply": true}',
                ))
            ]

        class _Resp:
            choices = [SimpleNamespace(message=_Msg())]
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _Resp()

        class _Client:
            chat = SimpleNamespace(completions=_Completions())

        adapter = OpenAICompatibleAdapter(model="qwen", base_url="http://x/v1")
        monkeypatch.setattr(adapter, "_get_client", lambda agent_id="": _Client())

        resp = await adapter.think(
            agent_id="ar-scheduler", context="ctx", prompt="do it",
            tools=[OPTIMIZE_SCHEDULE_TOOL],
        )
        # tools were forwarded to the API
        assert "tools" in captured and captured["tool_choice"] == "auto"
        # tool_calls parsed into structured form
        assert resp.tool_calls == [
            {"name": "optimize_schedule",
             "arguments": {"capacity_ceiling": 130, "apply": True}},
        ]

    @pytest.mark.asyncio
    async def test_no_tools_means_no_tools_param(self, monkeypatch) -> None:
        from cortiva.adapters.consciousness.openai_compat import (
            OpenAICompatibleAdapter,
        )
        captured: dict = {}

        class _Msg:
            content = "done"
            tool_calls = None

        class _Resp:
            choices = [SimpleNamespace(message=_Msg())]
            usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _Resp()

        class _Client:
            chat = SimpleNamespace(completions=_Completions())

        adapter = OpenAICompatibleAdapter(model="qwen", base_url="http://x/v1")
        monkeypatch.setattr(adapter, "_get_client", lambda agent_id="": _Client())

        resp = await adapter.think(agent_id="a", context="c", prompt="p")
        assert "tools" not in captured
        assert resp.tool_calls == []


# ---------------------------------------------------------------------------
# Native send_email tool (#282 follow-up) — reliable dispatch, not prose suffix
# ---------------------------------------------------------------------------


def test_send_email_offered_to_every_agent():
    from cortiva.core.agent_tools import tools_for_agent
    names = lambda ts: [t["function"]["name"] for t in ts]
    # Plain agent with no special authority still gets send_email.
    tools = tools_for_agent("nobody-special", scheduling_authorised=set())
    assert "send_email" in names(tools)
    # Scheduling agent gets it too, alongside the rota tools.
    tools2 = tools_for_agent("sched", scheduling_authorised={"sched"})
    assert "send_email" in names(tools2)
    assert "optimize_schedule" in names(tools2)


def test_send_email_call_maps_to_email_suffix():
    from cortiva.core.agent_tools import apply_tool_calls_to_suffix
    from cortiva.core.reflection import ReflectionSuffix

    suffix = ReflectionSuffix()
    apply_tool_calls_to_suffix(suffix, [{
        "name": "send_email",
        "arguments": {"to": "alex@x.io", "subject": "Status", "body": "Done."},
    }])
    assert suffix.email == {"to": "alex@x.io", "subject": "Status", "body": "Done."}


def test_send_email_schema_requires_core_fields():
    from cortiva.core.agent_tools import SEND_EMAIL_TOOL
    req = SEND_EMAIL_TOOL["function"]["parameters"]["required"]
    assert set(req) == {"to", "subject", "body"}
