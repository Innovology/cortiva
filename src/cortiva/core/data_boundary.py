"""
Data boundary enforcement — controls where agent data can flow.

When Cortiva is deployed on a customer's Azure node, the customer
needs guarantees about where their data goes:

- **Agent data** (identity, memories, journals) stays on the node
  or in the customer's Azure region.
- **LLM calls** go to approved endpoints only (e.g., Azure OpenAI
  in UK South, not OpenAI US).
- **Telemetry** is split: operational telemetry (health, billing)
  goes to Cortiva HQ; agent activity (audit logs, task data) stays
  with the customer.

Config::

    data_boundary:
      region: "UK South"
      allowed_llm_endpoints:
        - "https://my-openai.openai.azure.com"
        - "https://my-anthropic-proxy.ukcloud.com"
      denied_llm_endpoints:
        - "https://api.openai.com"        # US endpoint
        - "https://api.anthropic.com"     # US endpoint
      telemetry:
        customer_sink: "local"             # local | azure-monitor | webhook
        platform_sink: "https://telemetry.cortivahq.com"
        platform_fields: [agent_count, uptime, version]  # only these go to HQ
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.data_boundary")


@dataclass
class TelemetryConfig:
    """Controls where telemetry data is sent."""

    customer_sink: str = "local"
    """Where agent activity data goes: ``local``, ``azure-monitor``, or a webhook URL."""

    platform_sink: str = ""
    """Where platform health data goes (Cortiva HQ endpoint)."""

    platform_fields: list[str] = field(default_factory=lambda: [
        "agent_count", "uptime", "version", "heartbeat_interval",
    ])
    """Only these fields are sent to the platform sink.  No agent data."""


@dataclass
class DataBoundaryConfig:
    """Parsed ``data_boundary`` config section."""

    region: str = ""
    """Data residency region (e.g., ``UK South``, ``West Europe``)."""

    allowed_llm_endpoints: list[str] = field(default_factory=list)
    """LLM API endpoints the agents may call.  Empty = no restriction."""

    denied_llm_endpoints: list[str] = field(default_factory=list)
    """LLM API endpoints explicitly blocked."""

    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DataBoundaryConfig:
        if not data:
            return cls()
        tel_data = data.get("telemetry", {})
        telemetry = TelemetryConfig(
            customer_sink=tel_data.get("customer_sink", "local"),
            platform_sink=tel_data.get("platform_sink", ""),
            platform_fields=tel_data.get("platform_fields", TelemetryConfig().platform_fields),
        )
        return cls(
            region=data.get("region", ""),
            allowed_llm_endpoints=data.get("allowed_llm_endpoints", []),
            denied_llm_endpoints=data.get("denied_llm_endpoints", []),
            telemetry=telemetry,
        )


class DataBoundaryEnforcer:
    """Enforces data residency and endpoint restrictions.

    Instantiated once per Fabric.  Called before every LLM API call
    and before every telemetry emission.
    """

    def __init__(self, config: DataBoundaryConfig) -> None:
        self._config = config

    @property
    def region(self) -> str:
        return self._config.region

    def validate_llm_endpoint(self, endpoint: str) -> bool:
        """Check if an LLM endpoint is allowed.

        Returns True if the endpoint passes the allow/deny lists.
        Logs a warning and returns False if blocked.
        """
        # Deny list takes precedence
        for denied in self._config.denied_llm_endpoints:
            if endpoint.startswith(denied):
                logger.warning(
                    "LLM endpoint blocked by data boundary: %s "
                    "(denied: %s, region: %s)",
                    endpoint, denied, self._config.region,
                )
                return False

        # If allow list is specified, endpoint must match
        if self._config.allowed_llm_endpoints:
            for allowed in self._config.allowed_llm_endpoints:
                if endpoint.startswith(allowed):
                    return True
            logger.warning(
                "LLM endpoint not in allow list: %s (region: %s)",
                endpoint, self._config.region,
            )
            return False

        return True

    def filter_platform_telemetry(self, data: dict[str, Any]) -> dict[str, Any]:
        """Filter telemetry data before sending to Cortiva HQ.

        Only fields listed in ``platform_fields`` are included.
        No agent-specific data ever leaves the customer boundary.
        """
        allowed = set(self._config.telemetry.platform_fields)
        return {k: v for k, v in data.items() if k in allowed}

    def should_send_to_platform(self) -> bool:
        """Check if platform telemetry is configured."""
        return bool(self._config.telemetry.platform_sink)

    def customer_telemetry_sink(self) -> str:
        """Get the customer telemetry destination."""
        return self._config.telemetry.customer_sink

    def platform_telemetry_sink(self) -> str:
        """Get the platform (Cortiva HQ) telemetry destination."""
        return self._config.telemetry.platform_sink

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self._config.region,
            "allowed_llm_endpoints": self._config.allowed_llm_endpoints,
            "denied_llm_endpoints": self._config.denied_llm_endpoints,
            "telemetry": {
                "customer_sink": self._config.telemetry.customer_sink,
                "platform_sink": self._config.telemetry.platform_sink,
                "platform_fields": self._config.telemetry.platform_fields,
            },
        }
