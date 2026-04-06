"""Tests for session management."""

from __future__ import annotations

import pytest

from cortiva.core.session import Session, SessionManager, Turn


class TestTurn:
    def test_token_estimate(self):
        turn = Turn(role="agent", phase="plan", content="a" * 400)
        assert turn.token_estimate == 100

    def test_explicit_token_estimate(self):
        turn = Turn(role="agent", phase="plan", content="hello", token_estimate=42)
        assert turn.token_estimate == 42

    def test_timestamp_auto(self):
        turn = Turn(role="agent", phase="execute", content="work")
        assert turn.timestamp  # non-empty


class TestSession:
    def test_add_turn(self):
        session = Session(agent_id="agent-001")
        turn = session.add_turn("agent", "plan", "build the widget")
        assert turn.role == "agent"
        assert turn.phase == "plan"
        assert session.turn_count == 1

    def test_turn_accumulation(self):
        session = Session(agent_id="agent-001")
        session.add_turn("agent", "plan", "step one")
        session.add_turn("agent", "execute", "doing step one")
        session.add_turn("agent", "reflect", "step one went well")
        assert session.turn_count == 3

    def test_max_turns_eviction(self):
        session = Session(agent_id="agent-001", max_turns=3, max_tokens=999_999)
        for i in range(5):
            session.add_turn("agent", "execute", f"task {i}")
        assert session.turn_count == 3
        # Oldest turns should have been evicted
        assert session.turns[0].content == "task 2"

    def test_max_tokens_eviction(self):
        session = Session(agent_id="agent-001", max_turns=100, max_tokens=100)
        # Each turn ~25 tokens (100 chars / 4)
        for i in range(10):
            session.add_turn("agent", "execute", "x" * 100)
        assert session.total_tokens <= 100
        assert session.turn_count < 10

    def test_eviction_keeps_at_least_one(self):
        session = Session(agent_id="agent-001", max_turns=100, max_tokens=10)
        session.add_turn("agent", "plan", "x" * 1000)
        # Even though it exceeds token budget, we keep at least 1 turn
        assert session.turn_count == 1

    def test_validate_agent_ok(self):
        session = Session(agent_id="agent-001")
        session.validate_agent("agent-001")  # Should not raise

    def test_validate_agent_mismatch(self):
        session = Session(agent_id="agent-001")
        with pytest.raises(ValueError, match="cross-contamination"):
            session.validate_agent("agent-002")

    def test_to_context_string_empty(self):
        session = Session(agent_id="agent-001")
        assert session.to_context_string() == ""

    def test_to_context_string(self):
        session = Session(agent_id="agent-001")
        session.add_turn("agent", "plan", "build feature X")
        session.add_turn("system", "execute", "executing task 1")
        ctx = session.to_context_string()
        assert "## Conversation History" in ctx
        assert "[agent/plan]" in ctx
        assert "[system/execute]" in ctx

    def test_end(self):
        session = Session(agent_id="agent-001")
        assert session.ended_at is None
        session.end()
        assert session.ended_at is not None

    def test_total_tokens(self):
        session = Session(agent_id="agent-001")
        session.add_turn("agent", "plan", "a" * 400)  # ~100 tokens
        session.add_turn("agent", "plan", "b" * 200)  # ~50 tokens
        assert session.total_tokens == 150


class TestSessionManager:
    def test_start_and_get(self):
        mgr = SessionManager()
        session = mgr.start_session("agent-001")
        assert session.agent_id == "agent-001"
        assert mgr.get_session("agent-001") is session

    def test_get_nonexistent(self):
        mgr = SessionManager()
        assert mgr.get_session("nope") is None

    def test_end_session(self):
        mgr = SessionManager()
        mgr.start_session("agent-001")
        ended = mgr.end_session("agent-001")
        assert ended is not None
        assert ended.ended_at is not None
        assert mgr.get_session("agent-001") is None

    def test_end_nonexistent(self):
        mgr = SessionManager()
        assert mgr.end_session("nope") is None

    def test_start_replaces_existing(self):
        mgr = SessionManager()
        s1 = mgr.start_session("agent-001")
        s2 = mgr.start_session("agent-001")
        assert s1 is not s2
        assert mgr.get_session("agent-001") is s2

    def test_add_turn(self):
        mgr = SessionManager()
        mgr.start_session("agent-001")
        turn = mgr.add_turn("agent-001", "agent", "plan", "do stuff")
        assert turn is not None
        assert turn.content == "do stuff"

    def test_add_turn_no_session(self):
        mgr = SessionManager()
        assert mgr.add_turn("agent-001", "agent", "plan", "do stuff") is None

    def test_active_sessions(self):
        mgr = SessionManager()
        mgr.start_session("agent-001")
        mgr.start_session("agent-002")
        assert sorted(mgr.active_sessions) == ["agent-001", "agent-002"]
        mgr.end_session("agent-001")
        assert mgr.active_sessions == ["agent-002"]

    def test_default_limits(self):
        mgr = SessionManager(default_max_turns=10, default_max_tokens=5000)
        session = mgr.start_session("agent-001")
        assert session.max_turns == 10
        assert session.max_tokens == 5000

    def test_cross_contamination_guard(self):
        mgr = SessionManager()
        mgr.start_session("agent-001")
        # Internally add_turn validates agent_id
        turn = mgr.add_turn("agent-001", "agent", "plan", "safe")
        assert turn is not None
