"""Tests for the governance / R&R enforcement spec."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortiva.core.governance import (
    AuthorityTier,
    AuthorityValidator,
    ValidationResult,
    parse_responsibilities,
)

# ---------------------------------------------------------------------------
# parse_responsibilities tests
# ---------------------------------------------------------------------------


SAMPLE_RESPONSIBILITIES = """\
# Dev-Cortiva — Responsibilities

## Primary

- Implement features and fixes from the backlog as prioritised by PM-Cortiva.
- Write tests for all new functionality.
- Keep the test suite green; fix regressions immediately.
- Create feature branches and open PRs for review by QA-Cortiva.

## Secondary

- Review technical feasibility of backlog items when asked by PM-Cortiva.
- Propose architectural improvements when patterns emerge.
- Update skills.md and procedures.md as the codebase evolves.

## Escalation

- **To PM-Cortiva**: Scope changes, new dependencies, architectural decisions
- **To QA-Cortiva**: Request review before merging any PR
- **To Human**: Security concerns, breaking changes to the public API

## Authority Boundaries

- I may create branches, write code, and open PRs.
- I may NOT merge to `main` without QA-Cortiva approval.
- I may NOT add new runtime dependencies without PM-Cortiva sign-off.
"""


class TestParseResponsibilities:
    def test_primary_items(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        assert len(b.primary) == 4
        assert "Implement features" in b.primary[0]
        assert "Write tests" in b.primary[1]

    def test_secondary_items(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        assert len(b.secondary) == 3
        assert "Review technical feasibility" in b.secondary[0]

    def test_escalation_targets(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        assert len(b.escalation_targets) == 3

        pm = b.escalation_targets[0]
        assert pm.target_agent == "PM-Cortiva"
        assert "Scope changes" in pm.topics

        qa = b.escalation_targets[1]
        assert qa.target_agent == "QA-Cortiva"

        human = b.escalation_targets[2]
        assert human.target_agent == "Human"
        assert "Security concerns" in human.topics[0]

    def test_authority_statements(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        assert len(b.authority_statements) == 3
        assert "create branches" in b.authority_statements[0]
        assert "NOT merge" in b.authority_statements[1]

    def test_empty_content(self) -> None:
        b = parse_responsibilities("")
        assert b.primary == []
        assert b.secondary == []
        assert b.escalation_targets == []
        assert b.authority_statements == []

    def test_no_sections(self) -> None:
        b = parse_responsibilities("Just some text without sections.\n- A list item\n")
        assert b.primary == []

    def test_to_dict(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        d = b.to_dict()
        assert len(d["primary"]) == 4
        assert len(d["secondary"]) == 3
        assert len(d["escalation_targets"]) == 3
        assert d["escalation_targets"][0]["target_agent"] == "PM-Cortiva"

    def test_real_template(self) -> None:
        """Parse the actual dev-cortiva template file."""
        template_path = (
            Path(__file__).parent.parent
            / "src/cortiva/templates/dev-cortiva/identity/responsibilities.md"
        )
        if not template_path.exists():
            pytest.skip("Template file not found")
        content = template_path.read_text()
        b = parse_responsibilities(content)
        assert len(b.primary) >= 3
        assert len(b.escalation_targets) >= 2


# ---------------------------------------------------------------------------
# AuthorityValidator tests
# ---------------------------------------------------------------------------


class TestAuthorityValidator:
    def test_validate_action_matches_negative_authority(self) -> None:
        """'merge PR to main' matches negative authority 'may NOT merge'."""
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        validator = AuthorityValidator(b)
        result = validator.validate_action("merge PR to main")
        assert result.tier == AuthorityTier.ESCALATION
        assert result.matched_rule is not None
        assert "merge" in result.matched_rule.lower()

    def test_validate_action_matches_primary(self) -> None:
        """'write tests for all functionality' should match a primary rule."""
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        validator = AuthorityValidator(b)
        result = validator.validate_action("write tests for all functionality")
        assert result.tier == AuthorityTier.PRIMARY

    def test_validate_action_matches_escalation(self) -> None:
        """'scope changes and new dependencies' should match escalation."""
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        validator = AuthorityValidator(b)
        result = validator.validate_action("scope changes and new dependencies")
        assert result.tier == AuthorityTier.ESCALATION
        assert result.escalation_target == "PM-Cortiva"

    def test_validate_action_unknown_for_unrelated(self) -> None:
        """Unrelated action returns UNKNOWN."""
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        validator = AuthorityValidator(b)
        result = validator.validate_action("cook breakfast")
        assert result.tier == AuthorityTier.UNKNOWN

    def test_validator_has_boundaries(self) -> None:
        b = parse_responsibilities(SAMPLE_RESPONSIBILITIES)
        validator = AuthorityValidator(b)
        assert validator.boundaries is b
        assert len(validator.boundaries.primary) == 4


# ---------------------------------------------------------------------------
# AuthorityTier tests
# ---------------------------------------------------------------------------


class TestAuthorityTier:
    def test_enum_values(self) -> None:
        assert AuthorityTier.PRIMARY.value == "primary"
        assert AuthorityTier.SECONDARY.value == "secondary"
        assert AuthorityTier.ESCALATION.value == "escalation"
        assert AuthorityTier.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# ValidationResult tests
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_fields(self) -> None:
        r = ValidationResult(
            tier=AuthorityTier.PRIMARY,
            matched_rule="I may create branches",
            escalation_target=None,
            reason="Matches primary authority",
        )
        assert r.tier == AuthorityTier.PRIMARY
        assert r.matched_rule == "I may create branches"
        assert r.escalation_target is None


# ---------------------------------------------------------------------------
# Context integration test
# ---------------------------------------------------------------------------


class TestResponsibilitiesInPlanContext:
    @pytest.mark.asyncio
    async def test_plan_context_includes_responsibilities(self) -> None:
        """Responsibilities should appear in planning context."""
        from cortiva.adapters.memory.inmemory import InMemoryAdapter
        from cortiva.core.agent import Agent, AgentState
        from cortiva.core.context import ContextBuilder

        memory = InMemoryAdapter()
        builder = ContextBuilder(memory=memory)
        agent = Agent(
            id="test-agent",
            directory=Path("/tmp/test-agent"),
            state=AgentState.PLANNING,
        )
        identity = {
            "identity": "# Test Agent",
            "soul": "",
            "skills": "",
            "responsibilities": SAMPLE_RESPONSIBILITIES,
            "procedures": "",
            "plan": "",
        }
        context = await builder.build_plan_context(agent, identity, messages=[])
        assert "Responsibilities" in context
        assert "Implement features" in context
