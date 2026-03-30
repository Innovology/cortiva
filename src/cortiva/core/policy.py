"""
Agent execution policy — declarative YAML-based permissions.

Defines what each agent is allowed to do, what requires approval,
and what is blocked entirely.  Policies are loaded from the
``policies`` section of ``cortiva.yaml`` or from per-agent
``identity/policy.yaml`` files.

Example (in cortiva.yaml)::

    policies:
      defaults:
        tools:
          allowed: [Read, Write, Edit, Glob, Grep]
          denied: [Bash]
        execution:
          auto_approve:
            - "write tests*"
            - "create branch*"
          require_approval:
            - "merge*"
            - "deploy*"
            - "delete*"
          deny:
            - "drop database*"
            - "rm -rf*"
        filesystem:
          workspace_only: true
          allowed_paths: []
          denied_paths: ["/etc", "/var", "/usr"]

      dev-cortiva:
        tools:
          allowed: [Read, Write, Edit, Bash, Glob, Grep]
        execution:
          auto_approve:
            - "implement*"
            - "refactor*"
            - "write tests*"
          require_approval:
            - "merge to main*"
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.policy")


class Decision(Enum):
    """Outcome of a policy check."""

    ALLOW = "allow"
    """Action is auto-approved."""

    REQUIRE_APPROVAL = "require_approval"
    """Action needs human or supervisor confirmation."""

    DENY = "deny"
    """Action is blocked."""

    NO_MATCH = "no_match"
    """No policy rule matched — falls through to default behavior."""


@dataclass
class PolicyResult:
    """Result of evaluating an action against a policy."""

    decision: Decision
    matched_rule: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.NO_MATCH)

    @property
    def needs_approval(self) -> bool:
        return self.decision == Decision.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.decision == Decision.DENY


@dataclass
class ToolPolicy:
    """Controls which tools an agent can use."""

    allowed: list[str] = field(default_factory=list)
    """Tools explicitly allowed.  Empty list means all tools allowed."""

    denied: list[str] = field(default_factory=list)
    """Tools explicitly denied.  Checked before allowed."""

    def check_tool(self, tool_name: str) -> PolicyResult:
        """Check if a tool is permitted."""
        # Deny list takes precedence
        for pattern in self.denied:
            if fnmatch.fnmatch(tool_name.lower(), pattern.lower()):
                return PolicyResult(
                    decision=Decision.DENY,
                    matched_rule=pattern,
                    reason=f"Tool {tool_name!r} matches deny pattern {pattern!r}",
                )
        # If allowed list is specified, tool must match
        if self.allowed:
            for pattern in self.allowed:
                if fnmatch.fnmatch(tool_name.lower(), pattern.lower()):
                    return PolicyResult(
                        decision=Decision.ALLOW,
                        matched_rule=pattern,
                    )
            return PolicyResult(
                decision=Decision.DENY,
                reason=f"Tool {tool_name!r} not in allowed list",
            )
        return PolicyResult(decision=Decision.ALLOW)

    def effective_allowed(self) -> list[str] | None:
        """Return the allowed tools list for passing to terminal adapters.

        Returns ``None`` if no restrictions (all tools allowed).
        """
        if not self.allowed and not self.denied:
            return None
        if self.allowed:
            # Filter out any that are also denied
            return [
                t for t in self.allowed
                if not any(fnmatch.fnmatch(t.lower(), d.lower()) for d in self.denied)
            ]
        return None


@dataclass
class ExecutionPolicy:
    """Controls what actions an agent can take."""

    auto_approve: list[str] = field(default_factory=list)
    """Glob patterns for actions that are auto-approved."""

    require_approval: list[str] = field(default_factory=list)
    """Glob patterns for actions that need human confirmation."""

    deny: list[str] = field(default_factory=list)
    """Glob patterns for actions that are always blocked."""

    def check_action(self, action_description: str) -> PolicyResult:
        """Evaluate an action against execution rules.

        Check order: deny → require_approval → auto_approve → no_match.
        """
        desc_lower = action_description.lower()

        for pattern in self.deny:
            if fnmatch.fnmatch(desc_lower, pattern.lower()):
                return PolicyResult(
                    decision=Decision.DENY,
                    matched_rule=pattern,
                    reason=f"Action blocked by deny rule: {pattern!r}",
                )

        for pattern in self.require_approval:
            if fnmatch.fnmatch(desc_lower, pattern.lower()):
                return PolicyResult(
                    decision=Decision.REQUIRE_APPROVAL,
                    matched_rule=pattern,
                    reason=f"Action requires approval: {pattern!r}",
                )

        for pattern in self.auto_approve:
            if fnmatch.fnmatch(desc_lower, pattern.lower()):
                return PolicyResult(
                    decision=Decision.ALLOW,
                    matched_rule=pattern,
                )

        return PolicyResult(decision=Decision.NO_MATCH)


@dataclass
class FilesystemPolicy:
    """Controls filesystem access."""

    workspace_only: bool = False
    """Restrict all file operations to the agent's workspace directory."""

    allowed_paths: list[str] = field(default_factory=list)
    """Additional paths the agent may access (glob patterns)."""

    denied_paths: list[str] = field(default_factory=list)
    """Paths the agent may never access (glob patterns)."""

    def check_path(self, path: str, workspace: str = "") -> PolicyResult:
        """Check if a file path is permitted."""
        # Deny list first
        for pattern in self.denied_paths:
            if fnmatch.fnmatch(path, pattern):
                return PolicyResult(
                    decision=Decision.DENY,
                    matched_rule=pattern,
                    reason=f"Path blocked by deny rule: {pattern!r}",
                )

        # Workspace-only check
        if self.workspace_only and workspace:
            if not path.startswith(workspace):
                # Check allowed_paths for exceptions
                for pattern in self.allowed_paths:
                    if fnmatch.fnmatch(path, pattern):
                        return PolicyResult(
                            decision=Decision.ALLOW,
                            matched_rule=pattern,
                        )
                return PolicyResult(
                    decision=Decision.DENY,
                    reason=f"Path outside workspace (workspace_only=true)",
                )

        return PolicyResult(decision=Decision.ALLOW)


@dataclass
class AgentPolicy:
    """Complete policy for a single agent."""

    agent_id: str
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    filesystem: FilesystemPolicy = field(default_factory=FilesystemPolicy)

    def check_tool(self, tool_name: str) -> PolicyResult:
        return self.tools.check_tool(tool_name)

    def check_action(self, action_description: str) -> PolicyResult:
        return self.execution.check_action(action_description)

    def check_path(self, path: str, workspace: str = "") -> PolicyResult:
        return self.filesystem.check_path(path, workspace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "tools": {
                "allowed": self.tools.allowed,
                "denied": self.tools.denied,
            },
            "execution": {
                "auto_approve": self.execution.auto_approve,
                "require_approval": self.execution.require_approval,
                "deny": self.execution.deny,
            },
            "filesystem": {
                "workspace_only": self.filesystem.workspace_only,
                "allowed_paths": self.filesystem.allowed_paths,
                "denied_paths": self.filesystem.denied_paths,
            },
        }


def _parse_tool_policy(data: dict[str, Any]) -> ToolPolicy:
    return ToolPolicy(
        allowed=data.get("allowed", []),
        denied=data.get("denied", []),
    )


def _parse_execution_policy(data: dict[str, Any]) -> ExecutionPolicy:
    return ExecutionPolicy(
        auto_approve=data.get("auto_approve", []),
        require_approval=data.get("require_approval", []),
        deny=data.get("deny", []),
    )


def _parse_filesystem_policy(data: dict[str, Any]) -> FilesystemPolicy:
    return FilesystemPolicy(
        workspace_only=data.get("workspace_only", False),
        allowed_paths=data.get("allowed_paths", []),
        denied_paths=data.get("denied_paths", []),
    )


def parse_agent_policy(agent_id: str, data: dict[str, Any]) -> AgentPolicy:
    """Parse a policy dict into an AgentPolicy."""
    return AgentPolicy(
        agent_id=agent_id,
        tools=_parse_tool_policy(data.get("tools", {})),
        execution=_parse_execution_policy(data.get("execution", {})),
        filesystem=_parse_filesystem_policy(data.get("filesystem", {})),
    )


class PolicyManager:
    """Manages policies for all agents.

    Loads defaults and per-agent overrides from the ``policies``
    section of cortiva.yaml.  Per-agent policies inherit from
    defaults and can override specific fields.
    """

    def __init__(self) -> None:
        self._defaults = AgentPolicy(agent_id="__defaults__")
        self._policies: dict[str, AgentPolicy] = {}

    def load(self, policies_config: dict[str, Any]) -> None:
        """Load policies from the ``policies`` config section."""
        # Parse defaults
        defaults_data = policies_config.get("defaults", {})
        if defaults_data:
            self._defaults = parse_agent_policy("__defaults__", defaults_data)

        # Parse per-agent policies (merge with defaults)
        for agent_id, agent_data in policies_config.items():
            if agent_id == "defaults":
                continue
            if isinstance(agent_data, dict):
                merged = self._merge_with_defaults(agent_id, agent_data)
                self._policies[agent_id] = merged

    def _merge_with_defaults(
        self, agent_id: str, agent_data: dict[str, Any],
    ) -> AgentPolicy:
        """Merge agent-specific config with defaults."""
        # Start with defaults
        tools_data = {
            "allowed": list(self._defaults.tools.allowed),
            "denied": list(self._defaults.tools.denied),
        }
        exec_data = {
            "auto_approve": list(self._defaults.execution.auto_approve),
            "require_approval": list(self._defaults.execution.require_approval),
            "deny": list(self._defaults.execution.deny),
        }
        fs_data = {
            "workspace_only": self._defaults.filesystem.workspace_only,
            "allowed_paths": list(self._defaults.filesystem.allowed_paths),
            "denied_paths": list(self._defaults.filesystem.denied_paths),
        }

        # Override with agent-specific values
        if "tools" in agent_data:
            for key in ("allowed", "denied"):
                if key in agent_data["tools"]:
                    tools_data[key] = agent_data["tools"][key]
        if "execution" in agent_data:
            for key in ("auto_approve", "require_approval", "deny"):
                if key in agent_data["execution"]:
                    exec_data[key] = agent_data["execution"][key]
        if "filesystem" in agent_data:
            for key in ("workspace_only", "allowed_paths", "denied_paths"):
                if key in agent_data["filesystem"]:
                    fs_data[key] = agent_data["filesystem"][key]

        return AgentPolicy(
            agent_id=agent_id,
            tools=_parse_tool_policy(tools_data),
            execution=_parse_execution_policy(exec_data),
            filesystem=_parse_filesystem_policy(fs_data),
        )

    def get(self, agent_id: str) -> AgentPolicy:
        """Get the effective policy for an agent.

        Returns the agent-specific policy if one exists, otherwise
        the defaults.
        """
        return self._policies.get(agent_id, self._defaults)

    def check_tool(self, agent_id: str, tool_name: str) -> PolicyResult:
        return self.get(agent_id).check_tool(tool_name)

    def check_action(self, agent_id: str, action: str) -> PolicyResult:
        return self.get(agent_id).check_action(action)

    def check_path(self, agent_id: str, path: str, workspace: str = "") -> PolicyResult:
        return self.get(agent_id).check_path(path, workspace)
