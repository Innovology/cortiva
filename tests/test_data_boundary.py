"""Tests for data boundary enforcement."""

from __future__ import annotations

from cortiva.core.data_boundary import DataBoundaryConfig, DataBoundaryEnforcer


class TestDataBoundaryEnforcer:
    def test_allow_all_by_default(self) -> None:
        config = DataBoundaryConfig()
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.validate_llm_endpoint("https://api.openai.com/v1") is True

    def test_deny_list(self) -> None:
        config = DataBoundaryConfig(
            region="UK South",
            denied_llm_endpoints=["https://api.openai.com", "https://api.anthropic.com"],
        )
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.validate_llm_endpoint("https://api.openai.com/v1") is False
        assert enforcer.validate_llm_endpoint("https://api.anthropic.com/v1") is False
        assert enforcer.validate_llm_endpoint("https://my-azure.openai.azure.com") is True

    def test_allow_list(self) -> None:
        config = DataBoundaryConfig(
            region="UK South",
            allowed_llm_endpoints=["https://my-azure.openai.azure.com"],
        )
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.validate_llm_endpoint("https://my-azure.openai.azure.com/v1") is True
        assert enforcer.validate_llm_endpoint("https://api.openai.com/v1") is False

    def test_deny_overrides_allow(self) -> None:
        config = DataBoundaryConfig(
            allowed_llm_endpoints=["https://api.openai.com"],
            denied_llm_endpoints=["https://api.openai.com"],
        )
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.validate_llm_endpoint("https://api.openai.com/v1") is False

    def test_filter_platform_telemetry(self) -> None:
        config = DataBoundaryConfig()
        enforcer = DataBoundaryEnforcer(config)

        full_data = {
            "agent_count": 3,
            "uptime": 3600,
            "version": "0.1.0",
            "agent_tasks": [{"agent": "dev", "tasks": 5}],
            "agent_memories": ["sensitive data"],
            "secret_key": "sk-abc123",
        }
        filtered = enforcer.filter_platform_telemetry(full_data)
        assert "agent_count" in filtered
        assert "uptime" in filtered
        assert "version" in filtered
        assert "agent_tasks" not in filtered
        assert "agent_memories" not in filtered
        assert "secret_key" not in filtered

    def test_telemetry_sinks(self) -> None:
        config = DataBoundaryConfig.from_dict({
            "telemetry": {
                "customer_sink": "azure-monitor",
                "platform_sink": "https://telemetry.cortivahq.com",
            },
        })
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.customer_telemetry_sink() == "azure-monitor"
        assert enforcer.platform_telemetry_sink() == "https://telemetry.cortivahq.com"
        assert enforcer.should_send_to_platform() is True

    def test_no_platform_sink(self) -> None:
        config = DataBoundaryConfig()
        enforcer = DataBoundaryEnforcer(config)
        assert enforcer.should_send_to_platform() is False


class TestDataBoundaryConfig:
    def test_from_dict_empty(self) -> None:
        config = DataBoundaryConfig.from_dict({})
        assert config.region == ""
        assert config.allowed_llm_endpoints == []

    def test_from_dict_full(self) -> None:
        config = DataBoundaryConfig.from_dict({
            "region": "UK South",
            "allowed_llm_endpoints": ["https://uk.openai.azure.com"],
            "denied_llm_endpoints": ["https://api.openai.com"],
            "telemetry": {
                "customer_sink": "azure-monitor",
                "platform_sink": "https://cortivahq.com/telemetry",
                "platform_fields": ["agent_count", "version"],
            },
        })
        assert config.region == "UK South"
        assert len(config.allowed_llm_endpoints) == 1
        assert config.telemetry.platform_fields == ["agent_count", "version"]
