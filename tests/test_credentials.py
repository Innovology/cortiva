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
        config = CredentialConfig.from_dict(
            {
                "provider": "azure-keyvault",
                "key_vault_url": "https://vault.azure.net",
                "agents": {
                    "dev-cortiva": {
                        "GITHUB_TOKEN": "secret/github",
                        "AZURE_DEVOPS_PAT": "secret/devops",
                    },
                },
            }
        )
        assert config.provider == "azure-keyvault"
        assert "dev-cortiva" in config.agents
        assert config.agents["dev-cortiva"]["GITHUB_TOKEN"] == "secret/github"


class TestLoadAgentCredentials:
    """credentials.json in the agent dir — written by the management
    layer when integrations are granted, injected into terminal env."""

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        from cortiva.core.credentials import load_agent_credentials

        assert load_agent_credentials(tmp_path) == {}

    def test_reads_flat_mapping(self, tmp_path) -> None:
        import json

        from cortiva.core.credentials import load_agent_credentials

        (tmp_path / "credentials.json").write_text(
            json.dumps({"GH_TOKEN": "ghp_x", "GITHUB_ORG": "acme"}),
        )
        assert load_agent_credentials(tmp_path) == {
            "GH_TOKEN": "ghp_x",
            "GITHUB_ORG": "acme",
        }

    def test_bad_json_returns_empty(self, tmp_path) -> None:
        from cortiva.core.credentials import load_agent_credentials

        (tmp_path / "credentials.json").write_text("{not json")
        assert load_agent_credentials(tmp_path) == {}

    def test_non_object_returns_empty(self, tmp_path) -> None:
        from cortiva.core.credentials import load_agent_credentials

        (tmp_path / "credentials.json").write_text('["a", "b"]')
        assert load_agent_credentials(tmp_path) == {}


class TestSelfAcquiredCredentials:
    """credentials.local.json — secrets an agent acquired itself at runtime
    (e.g. a redeemed PAT). Merged under the HQ-managed file, never clobbered
    by an HQ sync."""

    def test_store_then_load_merges_with_managed(self, tmp_path) -> None:
        import json

        from cortiva.core.credentials import (
            load_agent_credentials,
            store_local_credential,
        )

        # HQ-managed cred.
        (tmp_path / "credentials.json").write_text(json.dumps({"GH_TOKEN": "ghp_x"}))
        # Agent self-acquires a PAT.
        store_local_credential(tmp_path, "HARIS_API_KEY", "k-self")

        assert load_agent_credentials(tmp_path) == {
            "GH_TOKEN": "ghp_x",
            "HARIS_API_KEY": "k-self",
        }

    def test_managed_wins_on_conflict(self, tmp_path) -> None:
        import json

        from cortiva.core.credentials import (
            load_agent_credentials,
            store_local_credential,
        )

        store_local_credential(tmp_path, "HARIS_API_KEY", "old-self")
        (tmp_path / "credentials.json").write_text(
            json.dumps({"HARIS_API_KEY": "central"}),
        )
        # Central delivery/rotation overrides a stale self-acquired value.
        assert load_agent_credentials(tmp_path)["HARIS_API_KEY"] == "central"

    def test_store_is_idempotent_upsert(self, tmp_path) -> None:
        from cortiva.core.credentials import (
            load_agent_credentials,
            store_local_credential,
        )

        store_local_credential(tmp_path, "A", "1")
        store_local_credential(tmp_path, "B", "2")
        store_local_credential(tmp_path, "A", "3")  # update in place
        assert load_agent_credentials(tmp_path) == {"A": "3", "B": "2"}

    def test_hq_sync_does_not_clobber_self_acquired(self, tmp_path) -> None:
        import json

        from cortiva.core.credentials import (
            load_agent_credentials,
            store_local_credential,
        )

        store_local_credential(tmp_path, "HARIS_API_KEY", "k-self")
        # Simulate an HQ credentials.sync rewriting ONLY credentials.json.
        (tmp_path / "credentials.json").write_text(json.dumps({"GH_TOKEN": "ghp_y"}))
        assert load_agent_credentials(tmp_path)["HARIS_API_KEY"] == "k-self"

    def test_cli_store_uses_agent_dir_env(self, tmp_path, monkeypatch) -> None:
        from cortiva.core.credentials import _main, load_agent_credentials

        monkeypatch.setenv("CORTIVA_AGENT_DIR", str(tmp_path))
        rc = _main(["store", "HARIS_API_KEY", "k-cli"])
        assert rc == 0
        assert load_agent_credentials(tmp_path) == {"HARIS_API_KEY": "k-cli"}

    def test_cli_fails_without_agent_dir(self, tmp_path, monkeypatch) -> None:
        from cortiva.core.credentials import _main

        monkeypatch.delenv("CORTIVA_AGENT_DIR", raising=False)
        assert _main(["store", "A", "1"]) == 1

    def test_cli_usage_error(self) -> None:
        from cortiva.core.credentials import _main

        assert _main(["bogus"]) == 2
