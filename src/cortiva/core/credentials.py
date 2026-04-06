"""
Credential delegation — secure access to customer resources.

When Cortiva runs on a customer's Azure node (deployed by Cortiva HQ),
agents need access to corporate apps (Azure DevOps, SharePoint,
Dynamics, etc.) without Cortiva HQ holding the customer's credentials.

Three credential providers:

- **env**: Environment variables (development only).
- **azure-managed-identity**: Azure Managed Identity — the node
  authenticates as itself, no secrets stored.  The customer grants
  the Managed Identity access to their resources via Azure RBAC.
- **azure-keyvault**: Secrets retrieved from the customer's Azure
  Key Vault at runtime.  The node uses Managed Identity to access
  the vault.

Config::

    credentials:
      provider: azure-managed-identity
      # Or:
      provider: azure-keyvault
      key_vault_url: https://customer-vault.vault.azure.net
      # Or:
      provider: env

Per-agent credentials can override the default::

    credentials:
      provider: azure-keyvault
      key_vault_url: https://customer-vault.vault.azure.net
      agents:
        dev-cortiva:
          AZURE_DEVOPS_PAT: secret/devops-pat
          GITHUB_TOKEN: secret/github-token
        pm-cortiva:
          LINEAR_API_KEY: secret/linear-key
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("cortiva.credentials")


@dataclass
class CredentialConfig:
    """Parsed ``credentials`` config section."""

    provider: str = "env"
    """``env``, ``azure-managed-identity``, or ``azure-keyvault``."""

    key_vault_url: str = ""
    """Azure Key Vault URL (for ``azure-keyvault`` provider)."""

    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    """Per-agent secret mappings: agent_id → {env_var: secret_name}."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialConfig:
        if not data:
            return cls()
        agents: dict[str, dict[str, str]] = {}
        for agent_id, secrets in (data.get("agents") or {}).items():
            if isinstance(secrets, dict):
                agents[agent_id] = {str(k): str(v) for k, v in secrets.items()}
        return cls(
            provider=data.get("provider", "env"),
            key_vault_url=data.get("key_vault_url", ""),
            agents=agents,
        )


class CredentialProvider:
    """Resolves credentials for agents at runtime.

    Each agent can request its credentials via ``get_env(agent_id)``,
    which returns a dict of environment variables to inject into
    the agent's subprocess.
    """

    def __init__(self, config: CredentialConfig) -> None:
        self._config = config
        self._cache: dict[str, str] = {}
        self._vault_client: Any = None

    def get_env(self, agent_id: str) -> dict[str, str]:
        """Get environment variables for an agent.

        Resolves secrets from the configured provider and returns
        them as a dict suitable for subprocess env injection.
        """
        agent_secrets = self._config.agents.get(agent_id, {})
        if not agent_secrets:
            return {}

        result: dict[str, str] = {}
        for env_var, secret_ref in agent_secrets.items():
            value = self._resolve(secret_ref)
            if value:
                result[env_var] = value
            else:
                logger.warning(
                    "Could not resolve credential %s for agent %s",
                    secret_ref, agent_id,
                )
        return result

    def _resolve(self, secret_ref: str) -> str | None:
        """Resolve a single secret reference."""
        # Check cache first
        if secret_ref in self._cache:
            return self._cache[secret_ref]

        value = None
        if self._config.provider == "env":
            value = self._resolve_env(secret_ref)
        elif self._config.provider == "azure-keyvault":
            value = self._resolve_keyvault(secret_ref)
        elif self._config.provider == "azure-managed-identity":
            value = self._resolve_managed_identity(secret_ref)

        if value:
            self._cache[secret_ref] = value
        return value

    def _resolve_env(self, secret_ref: str) -> str | None:
        """Resolve from environment variables."""
        return os.environ.get(secret_ref)

    def _resolve_keyvault(self, secret_ref: str) -> str | None:
        """Resolve from Azure Key Vault."""
        if not self._config.key_vault_url:
            logger.error("No key_vault_url configured")
            return None

        try:
            client = self._get_vault_client()
            secret = client.get_secret(secret_ref)
            return secret.value
        except Exception as exc:
            logger.error("Failed to get secret %s from Key Vault: %s", secret_ref, exc)
            return None

    def _resolve_managed_identity(self, secret_ref: str) -> str | None:
        """Resolve via Azure Managed Identity.

        For Managed Identity, the secret_ref is typically an env var
        name that the Managed Identity has access to via the node's
        configuration.  Falls back to env vars.
        """
        # Managed Identity doesn't directly store secrets — it provides
        # authentication tokens.  For app secrets, use Key Vault with MI.
        return os.environ.get(secret_ref)

    def _get_vault_client(self) -> Any:
        """Lazily create the Azure Key Vault SecretClient."""
        if self._vault_client is not None:
            return self._vault_client

        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError:
            raise ImportError(
                "Azure Key Vault support requires: "
                "pip install azure-identity azure-keyvault-secrets"
            )

        credential = DefaultAzureCredential()
        self._vault_client = SecretClient(
            vault_url=self._config.key_vault_url,
            credential=credential,
        )
        return self._vault_client

    def get_token(self, resource: str = "https://management.azure.com") -> str | None:
        """Get an Azure access token via Managed Identity.

        This allows agents to call Azure APIs (DevOps, Graph, etc.)
        without storing any credentials.
        """
        if self._config.provider not in ("azure-managed-identity", "azure-keyvault"):
            return None

        try:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            token = credential.get_token(resource)
            return token.token
        except Exception as exc:
            logger.error("Failed to get Azure token for %s: %s", resource, exc)
            return None

    def clear_cache(self) -> None:
        """Clear the credential cache."""
        self._cache.clear()
