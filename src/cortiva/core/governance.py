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


_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "is", "it", "that", "this", "with", "from", "by", "as", "be",
    "can", "may", "i", "my", "we", "our", "do", "not", "no", "should",
})

# Matches negative authority statements like "may NOT merge"
_NEGATIVE_RE = re.compile(r"\bmay\s+not\b|\bcannot\b|\bmust\s+not\b|\bdo\s+not\b", re.IGNORECASE)


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from a text string."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    return words - _STOP_WORDS


def _keyword_overlap(text_keywords: set[str], rule_keywords: set[str]) -> float:
    """Compute overlap ratio between two keyword sets.

    Returns the fraction of *rule_keywords* matched by *text_keywords*.
    Returns 0.0 if either set is empty.
    """
    if not text_keywords or not rule_keywords:
        return 0.0
    return len(text_keywords & rule_keywords) / len(rule_keywords)


# Minimum keyword overlap to consider a match.
# Uses a relatively low threshold because action descriptions tend to be
# short while responsibility rules contain qualifying words.
_MATCH_THRESHOLD = 0.3


class AuthorityValidator:
    """Validates proposed actions against an agent's authority boundaries.

    Uses keyword-overlap matching to classify actions against the parsed
    responsibilities.md rules.  The algorithm:

    1. Check negative authority statements first (``"may NOT ..."``).
    2. Check escalation topics.
    3. Check secondary (approval-required) rules.
    4. Check primary (unilateral) rules.
    5. If no match, return ``UNKNOWN``.

    Usage::

        validator = AuthorityValidator(boundaries)
        result = validator.validate_action("merge PR to main")
        if result.tier == AuthorityTier.ESCALATION:
            # agent should escalate
            ...
    """

    def __init__(self, boundaries: AuthorityBoundaries) -> None:
        self.boundaries = boundaries
        # Pre-compile keyword sets for each rule
        self._primary_kw = [
            (rule, _extract_keywords(rule)) for rule in boundaries.primary
        ]
        self._secondary_kw = [
            (rule, _extract_keywords(rule)) for rule in boundaries.secondary
        ]
        self._escalation_kw: list[tuple[str, set[str], str]] = []
        for target in boundaries.escalation_targets:
            for topic in target.topics:
                self._escalation_kw.append(
                    (topic, _extract_keywords(topic), target.target_agent)
                )
        self._negative_kw = [
            (stmt, _extract_keywords(stmt))
            for stmt in boundaries.authority_statements
            if _NEGATIVE_RE.search(stmt)
        ]

    def validate_action(self, action_description: str) -> ValidationResult:
        """Classify an action against authority boundaries.

        Returns a :class:`ValidationResult` with the matched tier, rule,
        and (for escalations) the target agent.
        """
        action_kw = _extract_keywords(action_description)

        if not action_kw:
            return ValidationResult(
                tier=AuthorityTier.UNKNOWN,
                matched_rule=None,
                escalation_target=None,
                reason="No actionable keywords in description.",
            )

        # 1. Check negative authority statements (explicit prohibitions)
        for stmt, kw in self._negative_kw:
            if _keyword_overlap(action_kw, kw) >= _MATCH_THRESHOLD:
                return ValidationResult(
                    tier=AuthorityTier.ESCALATION,
                    matched_rule=stmt,
                    escalation_target="human",
                    reason=f"Action matches negative authority: {stmt!r}",
                )

        # 2. Check escalation topics
        best_esc: tuple[float, str, str] | None = None
        for topic, kw, target in self._escalation_kw:
            score = _keyword_overlap(action_kw, kw)
            if score >= _MATCH_THRESHOLD:
                if best_esc is None or score > best_esc[0]:
                    best_esc = (score, topic, target)
        if best_esc:
            return ValidationResult(
                tier=AuthorityTier.ESCALATION,
                matched_rule=best_esc[1],
                escalation_target=best_esc[2],
                reason=f"Action matches escalation topic: {best_esc[1]!r}",
            )

        # 3. Check secondary rules (require approval)
        best_sec: tuple[float, str] | None = None
        for rule, kw in self._secondary_kw:
            score = _keyword_overlap(action_kw, kw)
            if score >= _MATCH_THRESHOLD:
                if best_sec is None or score > best_sec[0]:
                    best_sec = (score, rule)
        if best_sec:
            return ValidationResult(
                tier=AuthorityTier.SECONDARY,
                matched_rule=best_sec[1],
                escalation_target=None,
                reason=f"Action matches secondary rule: {best_sec[1]!r}",
            )

        # 4. Check primary rules (unilateral)
        best_pri: tuple[float, str] | None = None
        for rule, kw in self._primary_kw:
            score = _keyword_overlap(action_kw, kw)
            if score >= _MATCH_THRESHOLD:
                if best_pri is None or score > best_pri[0]:
                    best_pri = (score, rule)
        if best_pri:
            return ValidationResult(
                tier=AuthorityTier.PRIMARY,
                matched_rule=best_pri[1],
                escalation_target=None,
                reason=f"Action matches primary rule: {best_pri[1]!r}",
            )

        # 5. No match
        return ValidationResult(
            tier=AuthorityTier.UNKNOWN,
            matched_rule=None,
            escalation_target=None,
            reason="Action did not match any known authority boundaries.",
        )


@dataclass
class ValidationResult:
    """Result of validating an action against authority boundaries."""

    tier: AuthorityTier
    matched_rule: str | None
    escalation_target: str | None
    reason: str
