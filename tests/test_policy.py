"""Tests for agent execution policies."""

from __future__ import annotations

from cortiva.core.policy import (
    AgentPolicy,
    Decision,
    ExecutionPolicy,
    FilesystemPolicy,
    PolicyManager,
    ToolPolicy,
    parse_agent_policy,
)


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------


class TestToolPolicy:
    def test_allow_all_by_default(self) -> None:
        p = ToolPolicy()
        result = p.check_tool("Bash")
        assert result.allowed

    def test_deny_takes_precedence(self) -> None:
        p = ToolPolicy(allowed=["Bash", "Read"], denied=["Bash"])
        result = p.check_tool("Bash")
        assert result.denied
        assert "deny" in result.reason.lower()

    def test_allowed_list_restricts(self) -> None:
        p = ToolPolicy(allowed=["Read", "Write", "Edit"])
        assert p.check_tool("Read").allowed
        assert p.check_tool("Bash").denied
        assert "not in allowed" in p.check_tool("Bash").reason

    def test_glob_patterns(self) -> None:
        p = ToolPolicy(denied=["Bash*"])
        assert p.check_tool("Bash").denied
        assert p.check_tool("BashInteractive").denied
        assert p.check_tool("Read").allowed

    def test_case_insensitive(self) -> None:
        p = ToolPolicy(denied=["bash"])
        assert p.check_tool("Bash").denied
        assert p.check_tool("BASH").denied

    def test_effective_allowed_none(self) -> None:
        p = ToolPolicy()
        assert p.effective_allowed() is None

    def test_effective_allowed_filters_denied(self) -> None:
        p = ToolPolicy(allowed=["Read", "Write", "Bash"], denied=["Bash"])
        effective = p.effective_allowed()
        assert effective is not None
        assert "Bash" not in effective
        assert "Read" in effective
        assert "Write" in effective


# ---------------------------------------------------------------------------
# ExecutionPolicy
# ---------------------------------------------------------------------------


class TestExecutionPolicy:
    def test_auto_approve(self) -> None:
        p = ExecutionPolicy(auto_approve=["write tests*"])
        result = p.check_action("write tests for the auth module")
        assert result.decision == Decision.ALLOW

    def test_deny(self) -> None:
        p = ExecutionPolicy(deny=["drop database*"])
        result = p.check_action("drop database production")
        assert result.denied

    def test_require_approval(self) -> None:
        p = ExecutionPolicy(require_approval=["merge*", "deploy*"])
        result = p.check_action("merge PR to main")
        assert result.needs_approval

    def test_deny_over_approve(self) -> None:
        """Deny takes precedence over auto_approve."""
        p = ExecutionPolicy(
            auto_approve=["delete temporary*"],
            deny=["delete*"],
        )
        result = p.check_action("delete temporary files")
        assert result.denied

    def test_require_approval_over_auto(self) -> None:
        """Require approval takes precedence over auto_approve."""
        p = ExecutionPolicy(
            auto_approve=["merge feature*"],
            require_approval=["merge*"],
        )
        result = p.check_action("merge feature branch")
        assert result.needs_approval

    def test_no_match(self) -> None:
        p = ExecutionPolicy(auto_approve=["write*"])
        result = p.check_action("review the PR")
        assert result.decision == Decision.NO_MATCH
        assert result.allowed  # no_match is permissive

    def test_case_insensitive(self) -> None:
        p = ExecutionPolicy(deny=["DROP DATABASE*"])
        result = p.check_action("drop database users")
        assert result.denied


# ---------------------------------------------------------------------------
# FilesystemPolicy
# ---------------------------------------------------------------------------


class TestFilesystemPolicy:
    def test_allow_by_default(self) -> None:
        p = FilesystemPolicy()
        assert p.check_path("/any/path").allowed

    def test_deny_path(self) -> None:
        p = FilesystemPolicy(denied_paths=["/etc*", "/var*"])
        assert p.check_path("/etc/passwd").denied
        assert p.check_path("/home/user/file").allowed

    def test_workspace_only(self) -> None:
        p = FilesystemPolicy(workspace_only=True)
        ws = "/agents/dev-cortiva/workspace"
        assert p.check_path(f"{ws}/file.py", workspace=ws).allowed
        assert p.check_path("/etc/passwd", workspace=ws).denied

    def test_workspace_only_with_exceptions(self) -> None:
        p = FilesystemPolicy(
            workspace_only=True,
            allowed_paths=["/shared/data*"],
        )
        ws = "/agents/dev-cortiva/workspace"
        assert p.check_path("/shared/data/input.csv", workspace=ws).allowed
        assert p.check_path("/etc/passwd", workspace=ws).denied


# ---------------------------------------------------------------------------
# AgentPolicy
# ---------------------------------------------------------------------------


class TestAgentPolicy:
    def test_parse_full(self) -> None:
        data = {
            "tools": {"allowed": ["Read", "Write"], "denied": ["Bash"]},
            "execution": {
                "auto_approve": ["write*"],
                "require_approval": ["merge*"],
                "deny": ["drop*"],
            },
            "filesystem": {
                "workspace_only": True,
                "denied_paths": ["/etc*"],
            },
        }
        policy = parse_agent_policy("dev-cortiva", data)
        assert policy.agent_id == "dev-cortiva"
        assert policy.check_tool("Bash").denied
        assert policy.check_tool("Read").allowed
        assert policy.check_action("write tests").allowed
        assert policy.check_action("merge to main").needs_approval
        assert policy.check_action("drop database").denied

    def test_parse_empty(self) -> None:
        policy = parse_agent_policy("agent-1", {})
        assert policy.check_tool("Bash").allowed
        assert policy.check_action("anything").allowed

    def test_to_dict(self) -> None:
        policy = parse_agent_policy("agent-1", {
            "tools": {"allowed": ["Read"]},
            "execution": {"deny": ["rm*"]},
        })
        d = policy.to_dict()
        assert d["agent_id"] == "agent-1"
        assert d["tools"]["allowed"] == ["Read"]
        assert d["execution"]["deny"] == ["rm*"]


# ---------------------------------------------------------------------------
# PolicyManager
# ---------------------------------------------------------------------------


class TestPolicyManager:
    def test_defaults(self) -> None:
        mgr = PolicyManager()
        mgr.load({
            "defaults": {
                "tools": {"denied": ["Bash"]},
                "execution": {"deny": ["drop*"]},
            },
        })
        # Any agent should inherit defaults
        assert mgr.check_tool("unknown-agent", "Bash").denied
        assert mgr.check_action("unknown-agent", "drop database").denied

    def test_per_agent_override(self) -> None:
        mgr = PolicyManager()
        mgr.load({
            "defaults": {
                "tools": {"denied": ["Bash"]},
            },
            "dev-cortiva": {
                "tools": {"allowed": ["Read", "Write", "Edit", "Bash"]},
            },
        })
        # dev-cortiva overrides tools.allowed, but Bash is still denied
        # because denied is inherited from defaults
        assert mgr.check_tool("dev-cortiva", "Read").allowed
        # Note: denied inherited from defaults still applies
        assert mgr.check_tool("dev-cortiva", "Bash").denied

    def test_per_agent_override_clears_denied(self) -> None:
        mgr = PolicyManager()
        mgr.load({
            "defaults": {
                "tools": {"denied": ["Bash"]},
            },
            "dev-cortiva": {
                "tools": {"denied": []},  # explicitly clear denied
            },
        })
        assert mgr.check_tool("dev-cortiva", "Bash").allowed

    def test_get_unknown_returns_defaults(self) -> None:
        mgr = PolicyManager()
        mgr.load({"defaults": {"execution": {"deny": ["evil*"]}}})
        policy = mgr.get("nonexistent")
        assert policy.check_action("evil action").denied

    def test_empty_config(self) -> None:
        mgr = PolicyManager()
        mgr.load({})
        # Everything should be allowed
        assert mgr.check_tool("agent-1", "Bash").allowed
        assert mgr.check_action("agent-1", "anything").allowed

    def test_check_path(self) -> None:
        mgr = PolicyManager()
        mgr.load({
            "defaults": {
                "filesystem": {
                    "workspace_only": True,
                    "denied_paths": ["/etc*"],
                },
            },
        })
        ws = "/agents/agent-1/workspace"
        assert mgr.check_path("agent-1", f"{ws}/file.py", workspace=ws).allowed
        assert mgr.check_path("agent-1", "/etc/passwd", workspace=ws).denied
