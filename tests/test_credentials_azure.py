"""Tests for credential delegation — Azure Key Vault and Managed Identity."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from cortiva.core.credentials import CredentialConfig, CredentialProvider


class TestResolveKeyvault:
    def test_resolve_keyvault_success(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="https://vault.azure.net",
            agents={"dev": {"TOKEN": "secret/token"}},
        )
        provider = CredentialProvider(config)

        mock_secret = MagicMock()
        mock_secret.value = "vault-secret-value"

        mock_client = MagicMock()
        mock_client.get_secret.return_value = mock_secret
        provider._vault_client = mock_client

        env = provider.get_env("dev")
        assert env["TOKEN"] == "vault-secret-value"
        mock_client.get_secret.assert_called_once_with("secret/token")

    def test_resolve_keyvault_no_url(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="",
            agents={"dev": {"TOKEN": "secret/token"}},
        )
        provider = CredentialProvider(config)

        env = provider.get_env("dev")
        assert "TOKEN" not in env

    def test_resolve_keyvault_exception(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="https://vault.azure.net",
            agents={"dev": {"TOKEN": "secret/token"}},
        )
        provider = CredentialProvider(config)

        mock_client = MagicMock()
        mock_client.get_secret.side_effect = Exception("vault unreachable")
        provider._vault_client = mock_client

        env = provider.get_env("dev")
        assert "TOKEN" not in env

    def test_resolve_keyvault_caches_result(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="https://vault.azure.net",
            agents={"dev": {"TOKEN": "secret/token"}},
        )
        provider = CredentialProvider(config)

        mock_secret = MagicMock()
        mock_secret.value = "cached-val"

        mock_client = MagicMock()
        mock_client.get_secret.return_value = mock_secret
        provider._vault_client = mock_client

        env1 = provider.get_env("dev")
        env2 = provider.get_env("dev")
        # get_secret should only be called once due to caching
        mock_client.get_secret.assert_called_once()
        assert env1["TOKEN"] == env2["TOKEN"]


class TestResolveManagedIdentity:
    def test_resolve_managed_identity_from_env(self) -> None:
        config = CredentialConfig(
            provider="azure-managed-identity",
            agents={"dev": {"MI_SECRET": "MI_SECRET"}},
        )
        provider = CredentialProvider(config)

        os.environ["MI_SECRET"] = "mi-value"
        try:
            env = provider.get_env("dev")
            assert env["MI_SECRET"] == "mi-value"
        finally:
            del os.environ["MI_SECRET"]

    def test_resolve_managed_identity_missing(self) -> None:
        config = CredentialConfig(
            provider="azure-managed-identity",
            agents={"dev": {"MISSING": "MISSING"}},
        )
        provider = CredentialProvider(config)

        env = provider.get_env("dev")
        assert "MISSING" not in env


class TestGetVaultClient:
    def test_get_vault_client_creates_lazily(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="https://vault.azure.net",
        )
        provider = CredentialProvider(config)

        mock_credential = MagicMock()
        mock_secret_client = MagicMock()

        with (
            patch(
                "cortiva.core.credentials.DefaultAzureCredential",
                return_value=mock_credential,
                create=True,
            ),
            patch(
                "cortiva.core.credentials.SecretClient",
                return_value=mock_secret_client,
                create=True,
            ),
        ):
            # Patch the imports inside _get_vault_client
            with patch.dict("sys.modules", {
                "azure": MagicMock(),
                "azure.identity": MagicMock(
                    DefaultAzureCredential=MagicMock(return_value=mock_credential)
                ),
                "azure.keyvault": MagicMock(),
                "azure.keyvault.secrets": MagicMock(
                    SecretClient=MagicMock(return_value=mock_secret_client)
                ),
            }):
                client = provider._get_vault_client()
                assert client is not None
                # Second call should return cached
                client2 = provider._get_vault_client()
                assert client2 is client

    def test_get_vault_client_raises_without_azure(self) -> None:
        config = CredentialConfig(
            provider="azure-keyvault",
            key_vault_url="https://vault.azure.net",
        )
        provider = CredentialProvider(config)

        with patch.dict("sys.modules", {
            "azure": None,
            "azure.identity": None,
            "azure.keyvault": None,
            "azure.keyvault.secrets": None,
        }):
            with pytest.raises(ImportError, match="Azure Key Vault"):
                provider._get_vault_client()


class TestGetToken:
    def test_get_token_returns_none_for_env_provider(self) -> None:
        config = CredentialConfig(provider="env")
        provider = CredentialProvider(config)

        result = provider.get_token()
        assert result is None

    def test_get_token_with_managed_identity(self) -> None:
        config = CredentialConfig(provider="azure-managed-identity")
        provider = CredentialProvider(config)

        mock_token = MagicMock()
        mock_token.token = "azure-access-token"

        mock_credential = MagicMock()
        mock_credential.get_token.return_value = mock_token

        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.identity": MagicMock(
                DefaultAzureCredential=MagicMock(return_value=mock_credential)
            ),
        }):
            result = provider.get_token("https://management.azure.com")
            assert result == "azure-access-token"

    def test_get_token_with_keyvault_provider(self) -> None:
        config = CredentialConfig(provider="azure-keyvault")
        provider = CredentialProvider(config)

        mock_token = MagicMock()
        mock_token.token = "kv-token"

        mock_credential = MagicMock()
        mock_credential.get_token.return_value = mock_token

        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.identity": MagicMock(
                DefaultAzureCredential=MagicMock(return_value=mock_credential)
            ),
        }):
            result = provider.get_token()
            assert result == "kv-token"

    def test_get_token_exception_returns_none(self) -> None:
        config = CredentialConfig(provider="azure-managed-identity")
        provider = CredentialProvider(config)

        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.identity": MagicMock(
                DefaultAzureCredential=MagicMock(
                    side_effect=Exception("auth failed")
                )
            ),
        }):
            result = provider.get_token()
            assert result is None

    def test_get_token_custom_resource(self) -> None:
        config = CredentialConfig(provider="azure-managed-identity")
        provider = CredentialProvider(config)

        mock_token = MagicMock()
        mock_token.token = "graph-token"

        mock_credential = MagicMock()
        mock_credential.get_token.return_value = mock_token

        with patch.dict("sys.modules", {
            "azure": MagicMock(),
            "azure.identity": MagicMock(
                DefaultAzureCredential=MagicMock(return_value=mock_credential)
            ),
        }):
            result = provider.get_token("https://graph.microsoft.com")
            assert result == "graph-token"


class TestCredentialConfigEdgeCases:
    def test_from_dict_none_input(self) -> None:
        config = CredentialConfig.from_dict(None)
        assert config.provider == "env"

    def test_from_dict_agents_with_non_dict_value(self) -> None:
        config = CredentialConfig.from_dict({
            "provider": "env",
            "agents": {
                "dev": "not-a-dict",
            },
        })
        # Non-dict agent entries are skipped
        assert "dev" not in config.agents

    def test_clear_cache(self) -> None:
        config = CredentialConfig(
            provider="env",
            agents={"dev": {"TOKEN": "TOKEN"}},
        )
        provider = CredentialProvider(config)
        provider._cache["some-key"] = "some-value"
        provider.clear_cache()
        assert provider._cache == {}
