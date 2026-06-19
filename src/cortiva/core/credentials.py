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

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
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
                    secret_ref,
                    agent_id,
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


# ---------------------------------------------------------------------------
# Agent-directory credential file
# ---------------------------------------------------------------------------

CREDENTIALS_FILENAME = "credentials.json"
# Credentials an agent acquired ITSELF at runtime (e.g. a PAT it redeemed from
# an external service). Kept in a SEPARATE file so the management layer's
# ``credentials.json`` sync never clobbers them, and so a self-acquired secret
# never has to round-trip through HQ. Merged under the HQ-managed file, which
# wins on conflict (central rotation stays authoritative).
LOCAL_CREDENTIALS_FILENAME = "credentials.local.json"


def _read_cred_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object — ignoring", path)
        return {}
    return {str(k): str(v) for k, v in data.items()}


def load_agent_credentials(agent_dir: Path) -> dict[str, str]:
    """Load per-agent credentials injected into the agent's terminal env.

    Two sources, merged:

    - ``credentials.json`` — written by the management layer (Cortiva HQ's node
      client) when an agent is granted an integration.
    - ``credentials.local.json`` — written by the agent itself for a secret it
      acquired at runtime (see :func:`store_local_credential`). HQ-managed keys
      win on conflict, so central delivery/rotation always overrides a stale
      self-acquired value.

    Both files live outside ``identity/`` and ``today/`` so snapshot and clone
    pipelines (which exclude ``credentials`` paths by design) never pick them
    up. Returns an empty dict when nothing is present — credential absence must
    never break task execution.
    """
    agent_dir = Path(agent_dir)
    local = _read_cred_file(agent_dir / LOCAL_CREDENTIALS_FILENAME)
    managed = _read_cred_file(agent_dir / CREDENTIALS_FILENAME)
    return {**local, **managed}


def store_local_credential(agent_dir: Path, name: str, value: str) -> None:
    """Persist a credential the agent acquired itself, surviving HQ syncs.

    Writes/updates ``<agent_dir>/credentials.local.json`` (a flat
    ``{ENV_VAR: value}`` map) atomically. Generic by design — not specific to
    any one service — so any agent that redeems a key (a HARIS PAT, an API
    token, …) can keep it across sessions without it passing through HQ.
    """
    agent_dir = Path(agent_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / LOCAL_CREDENTIALS_FILENAME
    current = _read_cred_file(path)
    current[str(name)] = str(value)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _main(argv: list[str]) -> int:
    """Agent-facing CLI: ``python -m cortiva.core.credentials store NAME VALUE``.

    Lets an agent persist a credential it acquired at runtime, from its own
    session, with no new tool-dispatch machinery — it resolves the agent's own
    directory from ``CORTIVA_AGENT_DIR`` (set in the session env by the fabric).
    e.g. after redeeming a HARIS PAT, the agent pipes the response's apiKey to:

        python -m cortiva.core.credentials store HARIS_API_KEY "$KEY"
    """
    import sys

    if len(argv) != 3 or argv[0] != "store":
        print("usage: python -m cortiva.core.credentials store NAME VALUE", file=sys.stderr)
        return 2
    agent_dir = os.environ.get("CORTIVA_AGENT_DIR")
    if not agent_dir:
        print("CORTIVA_AGENT_DIR not set — not inside an agent session", file=sys.stderr)
        return 1
    store_local_credential(Path(agent_dir), argv[1], argv[2])
    print(f"stored {argv[1]} for this agent")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
