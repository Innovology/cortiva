"""
Governance / R&R enforcement (design only — issue #12).

Authority boundaries live in each agent's ``responsibilities.md`` using
a tiered structure:

- **Primary** — actions the agent may take unilaterally.
- **Secondary** — actions that require approval (e.g. from the HoD agent).
- **Escalation** — actions beyond the agent's authority that must be
  escalated to another agent or a human.

The ``AuthorityValidator`` parses these boundaries and validates proposed
actions against them before execution.

Implementation is planned for v0.3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Schema types
# ---------------------------------------------------------------------------


class AuthorityTier(Enum):
    """Classification of an action relative to an agent's authority."""

    PRIMARY = "primary"
    """Agent may act unilaterally."""

    SECONDARY = "secondary"
    """Agent requires approval from a designated authority."""

    ESCALATION = "escalation"
    """Action is beyond the agent's authority — must be escalated."""

    UNKNOWN = "unknown"
    """Action could not be classified against known boundaries."""


@dataclass
class EscalationTarget:
    """Where to escalate when an action is beyond authority."""

    target_agent: str
    """Agent ID to escalate to (e.g. 'pm-cortiva', 'human')."""

    topics: list[str] = field(default_factory=list)
    """Topics / action categories covered by this escalation path."""


@dataclass
class AuthorityBoundaries:
    """Parsed R&R schema from responsibilities.md.

    Expected responsibilities.md structure::

        ## Primary
        - Action the agent may take unilaterally
        - Another unilateral action

        ## Secondary
        - Action requiring approval
        - Another action requiring approval

        ## Escalation
        - **To PM-Cortiva**: Scope changes, new dependencies
        - **To Human**: Security concerns, breaking changes

        ## Authority Boundaries
        - I may create branches and open PRs.
        - I may NOT merge without QA approval.
    """

    primary: list[str] = field(default_factory=list)
    secondary: list[str] = field(default_factory=list)
    escalation_targets: list[EscalationTarget] = field(default_factory=list)
    authority_statements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "escalation_targets": [
                {"target_agent": e.target_agent, "topics": e.topics}
                for e in self.escalation_targets
            ],
            "authority_statements": self.authority_statements,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Matches "- **To Agent-Name**: topic list"
_ESCALATION_RE = re.compile(
    r"^-\s+\*\*To\s+(.+?)\*\*:\s*(.+)$",
    re.IGNORECASE,
)


def parse_responsibilities(content: str) -> AuthorityBoundaries:
    """Parse a responsibilities.md file into structured boundaries.

    Parameters
    ----------
    content:
        Raw markdown content of the responsibilities.md file.

    Returns
    -------
    AuthorityBoundaries with lists populated from each section.
    """
    boundaries = AuthorityBoundaries()
    current_section: str | None = None

    for line in content.splitlines():
        stripped = line.strip()

        # Detect section headers
        lower = stripped.lower()
        if lower.startswith("## primary"):
            current_section = "primary"
            continue
        if lower.startswith("## secondary"):
            current_section = "secondary"
            continue
        if lower.startswith("## escalation"):
            current_section = "escalation"
            continue
        if lower.startswith("## authority"):
            current_section = "authority"
            continue
        if stripped.startswith("## "):
            current_section = None
            continue

        # Parse list items
        if not stripped.startswith("- "):
            continue

        item = stripped[2:].strip()
        if not item:
            continue

        if current_section == "primary":
            boundaries.primary.append(item)
        elif current_section == "secondary":
            boundaries.secondary.append(item)
        elif current_section == "escalation":
            m = _ESCALATION_RE.match(stripped)
            if m:
                target = m.group(1).strip()
                topics = [t.strip() for t in m.group(2).split(",") if t.strip()]
                boundaries.escalation_targets.append(
                    EscalationTarget(target_agent=target, topics=topics),
                )
            else:
                boundaries.escalation_targets.append(
                    EscalationTarget(target_agent="unknown", topics=[item]),
                )
        elif current_section == "authority":
            boundaries.authority_statements.append(item)

    return boundaries


# ---------------------------------------------------------------------------
# Validator (stub)
# ---------------------------------------------------------------------------


class AuthorityValidator:
    """Validates proposed actions against an agent's authority boundaries.

    Stub implementation for v0.1.  Full enforcement is planned for v0.3.

    Usage::

        validator = AuthorityValidator(boundaries)
        result = validator.validate_action("merge PR to main")
        if result.tier == AuthorityTier.ESCALATION:
            # agent should escalate
            ...
    """

    def __init__(self, boundaries: AuthorityBoundaries) -> None:
        self.boundaries = boundaries

    def validate_action(self, action_description: str) -> ValidationResult:
        """Classify an action against authority boundaries.

        Stub: always returns ``UNKNOWN`` tier.  Full implementation will
        use keyword matching and semantic similarity to classify actions.
        """
        return ValidationResult(
            tier=AuthorityTier.UNKNOWN,
            matched_rule=None,
            escalation_target=None,
            reason="Governance enforcement not yet implemented (v0.3).",
        )


@dataclass
class ValidationResult:
    """Result of validating an action against authority boundaries."""

    tier: AuthorityTier
    matched_rule: str | None
    escalation_target: str | None
    reason: str
