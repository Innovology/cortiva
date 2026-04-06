"""Tests for credential delegation."""

from __future__ import annotations

import os

from cortiva.core.credentials import CredentialConfig, CredentialProvider


class TestCredentialProvider:
    def test_env_provider(self) -> None:
        config = CredentialConfig(
            provider="env",
            agents={
                "dev-cortiva": {"GITHUB_TOKEN": "GITHUB_TOKEN"},
            },
        )
        provider = CredentialProvider(config)

        # Set env var
        os.environ["GITHUB_TOKEN"] = "ghp_test123"
        try:
            env = provider.get_env("dev-cortiva")
            assert env["GITHUB_TOKEN"] == "ghp_test123"
        finally:
            del os.environ["GITHUB_TOKEN"]

    def test_env_provider_missing(self) -> None:
        config = CredentialConfig(
            provider="env",
            agents={
                "dev-cortiva": {"MISSING_VAR": "MISSING_VAR"},
            },
        )
        provider = CredentialProvider(config)
        env = provider.get_env("dev-cortiva")
        assert "MISSING_VAR" not in env

    def test_no_agent_secrets(self) -> None:
        config = CredentialConfig(provider="env")
        provider = CredentialProvider(config)
        env = provider.get_env("unknown-agent")
        assert env == {}

    def test_cache(self) -> None:
        config = CredentialConfig(
            provider="env",
            agents={"dev": {"TOKEN": "TOKEN"}},
        )
        provider = CredentialProvider(config)
        os.environ["TOKEN"] = "cached_value"
        try:
            env1 = provider.get_env("dev")
            assert env1["TOKEN"] == "cached_value"

            # Change env var — cached value should persist
            os.environ["TOKEN"] = "new_value"
            env2 = provider.get_env("dev")
            assert env2["TOKEN"] == "cached_value"  # still cached

            # Clear cache
            provider.clear_cache()
            env3 = provider.get_env("dev")
            assert env3["TOKEN"] == "new_value"
        finally:
            del os.environ["TOKEN"]

    def test_managed_identity_falls_back_to_env(self) -> None:
        config = CredentialConfig(
            provider="azure-managed-identity",
            agents={"dev": {"AZURE_TOKEN": "AZURE_TOKEN"}},
        )
        provider = CredentialProvider(config)
        os.environ["AZURE_TOKEN"] = "mi_token"
        try:
            env = provider.get_env("dev")
            assert env["AZURE_TOKEN"] == "mi_token"
        finally:
            del os.environ["AZURE_TOKEN"]


class TestCredentialConfig:
    def test_from_dict_empty(self) -> None:
        config = CredentialConfig.from_dict({})
        assert config.provider == "env"
        assert config.agents == {}

    def test_from_dict_full(self) -> None:
        config = CredentialConfig.from_dict({
            "provider": "azure-keyvault",
            "key_vault_url": "https://vault.azure.net",
            "agents": {
                "dev-cortiva": {
                    "GITHUB_TOKEN": "secret/github",
                    "AZURE_DEVOPS_PAT": "secret/devops",
                },
            },
        })
        assert config.provider == "azure-keyvault"
        assert "dev-cortiva" in config.agents
        assert config.agents["dev-cortiva"]["GITHUB_TOKEN"] == "secret/github"
