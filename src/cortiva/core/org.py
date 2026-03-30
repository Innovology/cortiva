"""
Organisational model — departments, reporting lines, and roles.

Loaded from the ``org:`` section of ``cortiva.yaml``.  When present,
the Fabric uses it to:

- Inject org position into agent planning context
- Validate delegation authority (managers → subordinates)
- Route approval requests to the correct approver
- Render org-awareness in ``cortiva org status``

When the ``org:`` section is absent, all org features are silently
disabled and the system behaves as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Department:
    """A department within the organisation."""

    name: str
    lead: str = ""
    """Agent ID of the department lead."""

    members: list[str] = field(default_factory=list)
    """Agent IDs belonging to this department."""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lead": self.lead, "members": self.members}


@dataclass
class RoleDefinition:
    """Authority level and capabilities for a named role."""

    authority_level: int = 0
    """0 = individual contributor, 1 = lead, 2 = director, 3 = exec."""

    can_delegate: bool = False
    """Whether this role may create work assignments for others."""

    can_approve: bool = False
    """Whether this role may approve tasks for others."""


@dataclass
class OrgModel:
    """Complete organisational structure, parsed from cortiva.yaml."""

    name: str = "Cortiva"
    departments: dict[str, Department] = field(default_factory=dict)
    reporting: dict[str, str] = field(default_factory=dict)
    """Maps agent_id → manager_agent_id."""

    roles: dict[str, RoleDefinition] = field(default_factory=dict)
    """Per-agent role overrides.  Inferred from reporting if absent."""

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def manager_of(self, agent_id: str) -> str | None:
        """Return the manager agent_id, or None."""
        return self.reporting.get(agent_id)

    def subordinates_of(self, agent_id: str) -> list[str]:
        """Return all agents who report directly to *agent_id*."""
        return [a for a, m in self.reporting.items() if m == agent_id]

    def department_of(self, agent_id: str) -> Department | None:
        """Return the department an agent belongs to."""
        for dept in self.departments.values():
            if agent_id in dept.members:
                return dept
        return None

    def peers_of(self, agent_id: str) -> list[str]:
        """Return agents in the same department, excluding self."""
        dept = self.department_of(agent_id)
        if dept is None:
            return []
        return [m for m in dept.members if m != agent_id]

    def is_manager(self, agent_id: str) -> bool:
        """True if any agent reports to *agent_id*."""
        return any(m == agent_id for m in self.reporting.values())

    def can_delegate_to(self, from_agent: str, to_agent: str) -> bool:
        """True if *from_agent* may delegate work to *to_agent*.

        Allowed when *from_agent* is *to_agent*'s manager (direct
        reporting line) or when *from_agent* has explicit
        ``can_delegate`` in its role definition.
        """
        # Check explicit role permission
        role = self.roles.get(from_agent)
        if role and role.can_delegate:
            # Managers with can_delegate can delegate to their reports
            if self.reporting.get(to_agent) == from_agent:
                return True

        # Default: direct reporting line
        return self.reporting.get(to_agent) == from_agent

    def approver_for(self, agent_id: str) -> str:
        """Return the agent who should approve work for *agent_id*.

        Defaults to the agent's manager.  Falls back to ``'human'``.
        """
        manager = self.manager_of(agent_id)
        if manager:
            role = self.roles.get(manager)
            if role and not role.can_approve:
                return "human"
            return manager
        return "human"

    # ------------------------------------------------------------------
    # Context rendering
    # ------------------------------------------------------------------

    def org_context_for(self, agent_id: str) -> str:
        """Render a markdown section describing this agent's org position.

        Injected into the LLM planning/execution context.
        """
        lines = ["## Org Position\n"]

        dept = self.department_of(agent_id)
        if dept:
            lines.append(f"Department: {dept.name}")
            if dept.lead == agent_id:
                lines.append(f"Role: Department lead")
            lines.append(f"Team: {', '.join(dept.members)}")

        manager = self.manager_of(agent_id)
        if manager:
            lines.append(f"Manager: {manager}")
            lines.append(
                "Delegated tasks from your manager take priority "
                "over self-planned tasks."
            )

        subs = self.subordinates_of(agent_id)
        if subs:
            lines.append(f"Direct reports: {', '.join(subs)}")
            lines.append(
                "You may delegate work to your reports via the "
                "delegate field in your reflection."
            )

        peers = self.peers_of(agent_id)
        if peers:
            lines.append(f"Peers: {', '.join(peers)}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "departments": {
                name: dept.to_dict() for name, dept in self.departments.items()
            },
            "reporting": dict(self.reporting),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrgModel:
        """Parse from a config dict."""
        departments: dict[str, Department] = {}
        for name, dept_data in (data.get("departments") or {}).items():
            if isinstance(dept_data, dict):
                departments[name] = Department(
                    name=name,
                    lead=dept_data.get("lead", ""),
                    members=dept_data.get("members", []),
                )

        reporting = dict(data.get("reporting") or {})

        roles: dict[str, RoleDefinition] = {}
        for agent_id, role_data in (data.get("roles") or {}).items():
            if isinstance(role_data, dict):
                roles[agent_id] = RoleDefinition(
                    authority_level=role_data.get("authority_level", 0),
                    can_delegate=role_data.get("can_delegate", False),
                    can_approve=role_data.get("can_approve", False),
                )

        # Auto-infer can_delegate/can_approve for managers not in roles
        for agent_id in set(reporting.values()):
            if agent_id not in roles:
                roles[agent_id] = RoleDefinition(
                    authority_level=1,
                    can_delegate=True,
                    can_approve=True,
                )

        return cls(
            name=data.get("name", "Cortiva"),
            departments=departments,
            reporting=reporting,
            roles=roles,
        )


def parse_org_config(config: dict[str, Any] | None) -> OrgModel | None:
    """Parse the ``org`` section of cortiva.yaml.

    Returns ``None`` if the section is absent or empty.
    """
    if not config or not isinstance(config, dict):
        return None
    return OrgModel.from_dict(config)
