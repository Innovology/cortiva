"""Tests for the organisational model."""

from __future__ import annotations

from cortiva.core.org import Department, OrgModel, RoleDefinition, parse_org_config


SAMPLE_CONFIG = {
    "name": "Cortiva Bootstrap",
    "departments": {
        "engineering": {
            "lead": "dev-cortiva",
            "members": ["dev-cortiva", "qa-cortiva"],
        },
        "management": {
            "lead": "pm-cortiva",
            "members": ["pm-cortiva"],
        },
    },
    "reporting": {
        "dev-cortiva": "pm-cortiva",
        "qa-cortiva": "pm-cortiva",
    },
}


class TestOrgModel:
    def test_from_dict(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        assert org.name == "Cortiva Bootstrap"
        assert len(org.departments) == 2
        assert "engineering" in org.departments
        assert org.departments["engineering"].lead == "dev-cortiva"

    def test_manager_of(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        assert org.manager_of("dev-cortiva") == "pm-cortiva"
        assert org.manager_of("qa-cortiva") == "pm-cortiva"
        assert org.manager_of("pm-cortiva") is None

    def test_subordinates_of(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        subs = org.subordinates_of("pm-cortiva")
        assert sorted(subs) == ["dev-cortiva", "qa-cortiva"]
        assert org.subordinates_of("dev-cortiva") == []

    def test_department_of(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        dept = org.department_of("dev-cortiva")
        assert dept is not None
        assert dept.name == "engineering"
        assert org.department_of("unknown") is None

    def test_peers_of(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        peers = org.peers_of("dev-cortiva")
        assert peers == ["qa-cortiva"]
        assert org.peers_of("pm-cortiva") == []

    def test_is_manager(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        assert org.is_manager("pm-cortiva") is True
        assert org.is_manager("dev-cortiva") is False

    def test_can_delegate_to(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        assert org.can_delegate_to("pm-cortiva", "dev-cortiva") is True
        assert org.can_delegate_to("pm-cortiva", "qa-cortiva") is True
        assert org.can_delegate_to("dev-cortiva", "pm-cortiva") is False
        assert org.can_delegate_to("dev-cortiva", "qa-cortiva") is False

    def test_approver_for(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        assert org.approver_for("dev-cortiva") == "pm-cortiva"
        assert org.approver_for("pm-cortiva") == "human"

    def test_org_context_for(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        ctx = org.org_context_for("dev-cortiva")
        assert "engineering" in ctx.lower() or "Engineering" in ctx
        assert "pm-cortiva" in ctx
        assert "Delegated tasks" in ctx

    def test_org_context_for_manager(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        ctx = org.org_context_for("pm-cortiva")
        assert "Direct reports" in ctx
        assert "delegate" in ctx.lower()

    def test_to_dict_roundtrip(self) -> None:
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        d = org.to_dict()
        assert d["name"] == "Cortiva Bootstrap"
        assert "engineering" in d["departments"]
        assert d["reporting"]["dev-cortiva"] == "pm-cortiva"

    def test_auto_infer_roles(self) -> None:
        """Managers without explicit roles get can_delegate/can_approve."""
        org = OrgModel.from_dict(SAMPLE_CONFIG)
        pm_role = org.roles.get("pm-cortiva")
        assert pm_role is not None
        assert pm_role.can_delegate is True
        assert pm_role.can_approve is True

    def test_explicit_roles(self) -> None:
        config = {
            **SAMPLE_CONFIG,
            "roles": {
                "pm-cortiva": {
                    "authority_level": 2,
                    "can_delegate": True,
                    "can_approve": False,
                },
            },
        }
        org = OrgModel.from_dict(config)
        assert org.approver_for("dev-cortiva") == "human"  # pm can't approve

    def test_empty_org(self) -> None:
        org = OrgModel.from_dict({})
        assert org.name == "Cortiva"
        assert org.departments == {}
        assert org.manager_of("anyone") is None
        assert org.subordinates_of("anyone") == []


class TestParseOrgConfig:
    def test_none_when_absent(self) -> None:
        assert parse_org_config(None) is None
        assert parse_org_config({}) is None

    def test_parses_valid(self) -> None:
        org = parse_org_config(SAMPLE_CONFIG)
        assert org is not None
        assert org.name == "Cortiva Bootstrap"
