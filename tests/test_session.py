"""Tests for agent session management."""

from __future__ import annotations

import pytest

from cortiva.core.session import Session, SessionManager


class TestSession:
    def test_add_turn(self) -> None:
        s = Session(agent_id="agent-1")
        s.add_turn("user", "hello", call_type="plan")
        s.add_turn("assistant", "world", call_type="plan")
        assert len(s.turns) == 2
        assert s.turns[0].role == "user"
        assert s.turns[1].content == "world"

    def test_evict_by_count(self) -> None:
        s = Session(agent_id="agent-1", max_turns=4)
        for i in range(6):
            s.add_turn("user", f"msg-{i}")
        assert len(s.turns) == 4
        assert s.turns[0].content == "msg-2"

    def test_evict_by_tokens(self) -> None:
        s = Session(agent_id="agent-1", max_tokens=100)
        # 100 tokens * 4 chars = 400 chars budget
        for i in range(10):
            s.add_turn("user", "x" * 200)  # 200 chars each
        assert len(s.turns) <= 3  # should evict to fit budget

    def test_render_empty(self) -> None:
        s = Session(agent_id="agent-1")
        assert s.render() == ""

    def test_render_single_turn(self) -> None:
        s = Session(agent_id="agent-1")
        s.add_turn("user", "hello")
        # Need at least 2 turns
        assert s.render() == ""

    def test_render_with_turns(self) -> None:
        s = Session(agent_id="agent-1")
        s.add_turn("user", "plan my day", call_type="plan")
        s.add_turn("assistant", "here is the plan", call_type="plan")
        rendered = s.render()
        assert "Conversation Today" in rendered
        assert "plan" in rendered
        assert "here is the plan" in rendered

    def test_render_truncates_long_content(self) -> None:
        s = Session(agent_id="agent-1")
        s.add_turn("user", "x" * 1000, call_type="execute")
        s.add_turn("assistant", "y" * 1000, call_type="execute")
        rendered = s.render()
        # Content should be truncated with ellipsis
        assert "…" in rendered

    def test_clear(self) -> None:
        s = Session(agent_id="agent-1")
        s.add_turn("user", "hello")
        s.add_turn("assistant", "world")
        s.clear()
        assert len(s.turns) == 0


class TestSessionManager:
    def test_start_and_get(self) -> None:
        mgr = SessionManager()
        session = mgr.start("agent-1")
        assert session.agent_id == "agent-1"
        assert mgr.get("agent-1") is session

    def test_get_nonexistent(self) -> None:
        mgr = SessionManager()
        assert mgr.get("agent-1") is None

    def test_record(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.record("agent-1", "prompt", "response", call_type="plan")
        session = mgr.get("agent-1")
        assert session is not None
        assert len(session.turns) == 2

    def test_record_without_session(self) -> None:
        mgr = SessionManager()
        # Should not raise
        mgr.record("agent-1", "prompt", "response")

    def test_render(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.record("agent-1", "plan", "ok", call_type="plan")
        rendered = mgr.render("agent-1")
        assert "Conversation Today" in rendered

    def test_render_no_session(self) -> None:
        mgr = SessionManager()
        assert mgr.render("agent-1") == ""

    def test_end(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.record("agent-1", "hello", "world")
        mgr.end("agent-1")
        assert mgr.get("agent-1") is None

    def test_end_nonexistent(self) -> None:
        mgr = SessionManager()
        mgr.end("agent-1")  # should not raise

    def test_validate_agent_clean(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.start("agent-2")
        # Context for agent-1 with agent-1's identity — should pass
        mgr.validate_agent("agent-1", "# agent-1 — Identity\nI am agent 1.")

    def test_validate_agent_detects_cross_contamination(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.start("agent-2")
        # Context claims to be for agent-1 but contains agent-2's identity header
        with pytest.raises(ValueError, match="cross-contamination"):
            mgr.validate_agent("agent-1", "# agent-2 — Identity\nI am agent 2.")

    def test_validate_agent_allows_mention_outside_header(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.start("agent-2")
        # Mentioning agent-2 deep in the context (past the 500-char header) is fine
        context = "# agent-1 — Identity\n" + ("x" * 500) + "\n# agent-2 mentioned here"
        mgr.validate_agent("agent-1", context)

    def test_validate_no_session(self) -> None:
        mgr = SessionManager()
        # No sessions — should not raise
        mgr.validate_agent("agent-1", "any context")

    def test_sessions_are_independent(self) -> None:
        mgr = SessionManager()
        mgr.start("agent-1")
        mgr.start("agent-2")
        mgr.record("agent-1", "prompt-1", "response-1", call_type="plan")
        mgr.record("agent-2", "prompt-2", "response-2", call_type="execute")

        s1 = mgr.get("agent-1")
        s2 = mgr.get("agent-2")
        assert s1 is not None and s2 is not None
        assert len(s1.turns) == 2
        assert len(s2.turns) == 2
        assert s1.turns[0].content == "prompt-1"
        assert s2.turns[0].content == "prompt-2"
