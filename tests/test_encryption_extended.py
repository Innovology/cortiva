"""Extended tests for encryption.py — Azure Key Vault path, unsupported
version, and from_config with azure-keyvault provider."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cortiva.core.encryption import EncryptionConfig, EncryptionVault, _MAGIC, _VERSION


class TestUnsupportedVersion:
    def test_decrypt_unsupported_version_raises(self) -> None:
        vault = EncryptionVault(key=b"x" * 32)
        # Craft a payload with version=99
        bad_data = _MAGIC + bytes([99]) + b"some_payload"
        with pytest.raises(ValueError, match="Unsupported encryption version: 99"):
            vault.decrypt(bad_data)


class TestAzureKeyVault:
    @patch("cortiva.core.encryption.EncryptionVault.from_azure_keyvault")
    def test_from_config_azure_keyvault(
        self, mock_from_azure: MagicMock, tmp_path: Path,
    ) -> None:
        mock_vault = EncryptionVault(key=b"z" * 32)
        mock_from_azure.return_value = mock_vault

        config = EncryptionConfig(
            enabled=True,
            provider="azure-keyvault",
            key_vault_url="https://my-vault.vault.azure.net",
            key_name="my-key",
        )
        result = EncryptionVault.from_config(config, tmp_path)

        assert result is mock_vault
        mock_from_azure.assert_called_once_with(
            "https://my-vault.vault.azure.net", "my-key",
        )

    def test_from_config_azure_missing_url_raises(self, tmp_path: Path) -> None:
        config = EncryptionConfig(
            enabled=True,
            provider="azure-keyvault",
            key_vault_url="",
        )
        with pytest.raises(ValueError, match="key_vault_url required"):
            EncryptionVault.from_config(config, tmp_path)

    @patch.dict("sys.modules", {
        "azure": MagicMock(),
        "azure.identity": MagicMock(),
        "azure.keyvault": MagicMock(),
        "azure.keyvault.keys": MagicMock(),
        "azure.keyvault.keys.crypto": MagicMock(),
    })
    def test_from_azure_keyvault_mocked(self) -> None:
        """Test the Azure Key Vault path with fully mocked azure modules."""
        import sys
        mock_identity = sys.modules["azure.identity"]
        mock_keys = sys.modules["azure.keyvault.keys"]

        mock_credential = MagicMock()
        mock_identity.DefaultAzureCredential.return_value = mock_credential

        mock_key = MagicMock()
        mock_key.id = "https://vault.azure.net/keys/my-key/abc123"
        mock_key_client = MagicMock()
        mock_key_client.get_key.return_value = mock_key
        mock_keys.KeyClient.return_value = mock_key_client

        vault = EncryptionVault.from_azure_keyvault(
            "https://vault.azure.net", "my-key",
        )
        assert isinstance(vault, EncryptionVault)

        # Verify it can encrypt/decrypt
        ct = vault.encrypt(b"secret data")
        assert vault.decrypt(ct) == b"secret data"

    def test_from_azure_keyvault_import_error(self) -> None:
        """Without azure libraries installed, should raise ImportError."""
        with pytest.raises(ImportError, match="Azure Key Vault support requires"):
            EncryptionVault.from_azure_keyvault(
                "https://vault.azure.net", "my-key",
            )


class TestFromConfigLocalKeyPath:
    def test_from_config_explicit_local_key_path(self, tmp_path: Path) -> None:
        key_path = tmp_path / "custom" / "my.key"
        config = EncryptionConfig(
            enabled=True,
            provider="local",
            local_key_path=str(key_path),
        )
        vault = EncryptionVault.from_config(config, tmp_path)
        assert vault is not None
        assert key_path.exists()

    def test_from_config_default_local_key_path(self, tmp_path: Path) -> None:
        config = EncryptionConfig(enabled=True, provider="local")
        vault = EncryptionVault.from_config(config, tmp_path)
        assert vault is not None
        expected_key = tmp_path / ".cortiva" / "encryption.key"
        assert expected_key.exists()


class TestXorFallback:
    def test_xor_roundtrip_no_crypto(self) -> None:
        """Test that XOR obfuscation roundtrip works when _has_crypto is False."""
        vault = EncryptionVault(key=b"k" * 32)
        # Force fallback
        vault._has_crypto = False
        vault._aesgcm = None

        plaintext = b"fallback test data"
        ct = vault.encrypt(plaintext)
        assert ct.startswith(_MAGIC)
        # Version byte should be 0 (XOR fallback)
        assert ct[len(_MAGIC)] == 0

        result = vault.decrypt(ct)
        assert result == plaintext
