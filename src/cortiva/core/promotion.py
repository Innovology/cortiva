"""
Agent promotion engine — structured role transitions with probation.

Promotion is a first-class lifecycle event: snapshot -> R&R swap ->
probation -> assessment -> confirm/revert.  Preserves institutional
knowledge accumulated in the previous role.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cortiva.core.snapshots import create_snapshot, restore_snapshot


@dataclass
class ProbationConfig:
    """Guardrails applied during the probation period."""

    duration_days: int = 14
    approval_threshold: float = 0.5  # lower than normal -> more oversight
    escalation_ratio_target: float = 0.30  # must be below this to confirm
    decision_quality_target: float = 0.85


@dataclass
class PromotionRecord:
    """Tracks an active or completed promotion."""

    agent_id: str
    source_role: str
    target_role: str
    initiated_at: str
    pre_promotion_snapshot: str  # snapshot_id
    probation_config: ProbationConfig
    probation_end: str  # ISO datetime
    status: str = "probationary"  # "probationary" | "confirmed" | "reverted" | "extended"
    confirmed_at: str | None = None
    reverted_at: str | None = None
    backfill_agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "source_role": self.source_role,
            "target_role": self.target_role,
            "initiated_at": self.initiated_at,
            "pre_promotion_snapshot": self.pre_promotion_snapshot,
            "probation_days": self.probation_config.duration_days,
            "probation_end": self.probation_end,
            "status": self.status,
            "confirmed_at": self.confirmed_at,
            "reverted_at": self.reverted_at,
            "backfill_agent_id": self.backfill_agent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromotionRecord:
        config = ProbationConfig(
            duration_days=data.get("probation_days", 14),
        )
        return cls(
            agent_id=data["agent_id"],
            source_role=data["source_role"],
            target_role=data["target_role"],
            initiated_at=data["initiated_at"],
            pre_promotion_snapshot=data["pre_promotion_snapshot"],
            probation_config=config,
            probation_end=data["probation_end"],
            status=data.get("status", "probationary"),
            confirmed_at=data.get("confirmed_at"),
            reverted_at=data.get("reverted_at"),
            backfill_agent_id=data.get("backfill_agent_id"),
        )


def _promotion_path(agent_dir: Path) -> Path:
    return agent_dir / ".promotion.json"


def get_promotion(agent_dir: Path) -> PromotionRecord | None:
    """Load the active promotion record, if any."""
    path = _promotion_path(agent_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return PromotionRecord.from_dict(data)


def _save_promotion(agent_dir: Path, record: PromotionRecord) -> None:
    path = _promotion_path(agent_dir)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")


def initiate_promotion(
    agent_dir: Path,
    target_role_template: Path,
    probation_days: int = 14,
) -> PromotionRecord:
    """Start a promotion flow for an agent.

    1. Creates a pre-promotion snapshot
    2. Swaps responsibilities.md from the target role template
    3. Merges soul.md (keeps core personality, updates role parameters)
    4. Updates identity.md to reference the promotion
    5. Sets probation status

    The target_role_template should be a directory containing at least
    ``identity/responsibilities.md`` and optionally ``identity/soul.md``.
    """
    agent_id = agent_dir.name
    now = datetime.now(tz=UTC)

    # 1. Pre-promotion snapshot
    meta = create_snapshot(
        agent_dir,
        name=f"pre-promotion-{now.strftime('%Y%m%d')}",
        trigger="pre-edit",
    )

    # Read current role from responsibilities.md header
    resp_path = agent_dir / "identity" / "responsibilities.md"
    current_role = agent_id
    if resp_path.exists():
        first_line = resp_path.read_text(encoding="utf-8").split("\n")[0]
        if first_line.startswith("# "):
            current_role = first_line[2:].strip().split(" — ")[0]

    # Read target role name
    target_resp = target_role_template / "identity" / "responsibilities.md"
    target_role = target_role_template.name
    if target_resp.exists():
        first_line = target_resp.read_text(encoding="utf-8").split("\n")[0]
        if first_line.startswith("# "):
            target_role = first_line[2:].strip().split(" — ")[0]

    # 2. Swap responsibilities.md
    if target_resp.exists():
        content = target_resp.read_text(encoding="utf-8")
        # Replace template agent ID with actual agent ID
        content = content.replace(target_role_template.name, agent_id)
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        resp_path.write_text(content, encoding="utf-8")

    # 3. Merge soul.md — keep core personality, note role change
    soul_path = agent_dir / "identity" / "soul.md"
    target_soul = target_role_template / "identity" / "soul.md"
    if soul_path.exists() and target_soul.exists():
        current_soul = soul_path.read_text(encoding="utf-8")
        # Append promotion note
        promotion_note = (
            f"\n\n## Role Transition\n\n"
            f"Promoted from {current_role} to {target_role} on "
            f"{now.strftime('%Y-%m-%d')}. Core personality retained; "
            f"authority boundaries updated for new role.\n"
        )
        soul_path.write_text(current_soul + promotion_note, encoding="utf-8")

    # 4. Update identity.md
    identity_path = agent_dir / "identity" / "identity.md"
    if identity_path.exists():
        content = identity_path.read_text(encoding="utf-8")
        promotion_header = (
            f"\n\n## Promotion\n\n"
            f"Promoted from {current_role} to {target_role} on "
            f"{now.strftime('%Y-%m-%d')}. Currently in probation period "
            f"({probation_days} days). Bringing experience from previous role.\n"
        )
        identity_path.write_text(content + promotion_header, encoding="utf-8")

    # 5. Create promotion record
    config = ProbationConfig(duration_days=probation_days)
    probation_end = now + timedelta(days=probation_days)

    record = PromotionRecord(
        agent_id=agent_id,
        source_role=current_role,
        target_role=target_role,
        initiated_at=now.isoformat(),
        pre_promotion_snapshot=meta.snapshot_id,
        probation_config=config,
        probation_end=probation_end.isoformat(),
    )
    _save_promotion(agent_dir, record)

    return record


def confirm_promotion(agent_dir: Path) -> PromotionRecord | None:
    """Confirm a promotion — remove probation flag."""
    record = get_promotion(agent_dir)
    if record is None or record.status != "probationary":
        return None

    record.status = "confirmed"
    record.confirmed_at = datetime.now(tz=UTC).isoformat()
    _save_promotion(agent_dir, record)
    return record


def revert_promotion(agent_dir: Path) -> PromotionRecord | None:
    """Revert a promotion — restore from pre-promotion snapshot."""
    record = get_promotion(agent_dir)
    if record is None or record.status not in ("probationary", "extended"):
        return None

    # Restore identity from pre-promotion snapshot
    restore_snapshot(
        agent_dir,
        record.pre_promotion_snapshot,
        restore_journal=False,  # keep journal — the agent should remember the attempt
    )

    record.status = "reverted"
    record.reverted_at = datetime.now(tz=UTC).isoformat()
    _save_promotion(agent_dir, record)
    return record


def extend_probation(agent_dir: Path, additional_days: int = 7) -> PromotionRecord | None:
    """Extend the probation period."""
    record = get_promotion(agent_dir)
    if record is None or record.status != "probationary":
        return None

    current_end = datetime.fromisoformat(record.probation_end)
    new_end = current_end + timedelta(days=additional_days)
    record.probation_end = new_end.isoformat()
    record.probation_config.duration_days += additional_days
    record.status = "probationary"
    _save_promotion(agent_dir, record)
    return record


def is_probationary(agent_dir: Path) -> bool:
    """Check if an agent is currently in probation."""
    record = get_promotion(agent_dir)
    return record is not None and record.status == "probationary"


def probation_expired(agent_dir: Path) -> bool:
    """Check if the probation period has elapsed."""
    record = get_promotion(agent_dir)
    if record is None or record.status != "probationary":
        return False
    end = datetime.fromisoformat(record.probation_end)
    now = datetime.now(tz=UTC)
    return now >= end
