"""Tests for encryption at rest."""

from __future__ import annotations

from pathlib import Path

from cortiva.core.encryption import EncryptionConfig, EncryptionVault, _MAGIC


class TestEncryptionVault:
    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path) -> None:
        vault = EncryptionVault(key=b"a" * 32)
        plaintext = b"Hello, this is sensitive agent data."
        ciphertext = vault.encrypt(plaintext)
        assert ciphertext != plaintext
        assert ciphertext.startswith(_MAGIC)

        decrypted = vault.decrypt(ciphertext)
        assert decrypted == plaintext

    def test_encrypt_file(self, tmp_path: Path) -> None:
        vault = EncryptionVault(key=b"b" * 32)
        file_path = tmp_path / "identity.md"
        file_path.write_text("# Secret Agent Identity")

        enc_path = vault.encrypt_file(file_path)
        assert enc_path.suffix == ".enc"
        assert enc_path.exists()
        assert not file_path.exists()

        # Encrypted file should have magic header
        assert vault.is_encrypted(enc_path)

    def test_decrypt_file(self, tmp_path: Path) -> None:
        vault = EncryptionVault(key=b"c" * 32)
        file_path = tmp_path / "identity.md"
        original_content = "# Top Secret"
        file_path.write_text(original_content)

        enc_path = vault.encrypt_file(file_path)
        dec_path = vault.decrypt_file(enc_path)

        assert dec_path == file_path
        assert dec_path.read_text() == original_content

    def test_is_encrypted(self, tmp_path: Path) -> None:
        vault = EncryptionVault(key=b"d" * 32)

        plain_file = tmp_path / "plain.txt"
        plain_file.write_text("not encrypted")
        assert vault.is_encrypted(plain_file) is False

        enc_file = tmp_path / "encrypted.txt"
        enc_file.write_bytes(vault.encrypt(b"secret"))
        assert vault.is_encrypted(enc_file) is True

    def test_from_local_key_generates(self, tmp_path: Path) -> None:
        key_path = tmp_path / "encryption.key"
        vault = EncryptionVault.from_local_key(key_path)
        assert key_path.exists()

        # Second call loads the existing key
        vault2 = EncryptionVault.from_local_key(key_path)
        # Both should decrypt the same ciphertext
        ct = vault.encrypt(b"test")
        assert vault2.decrypt(ct) == b"test"

    def test_decrypt_invalid_header(self) -> None:
        vault = EncryptionVault(key=b"e" * 32)
        import pytest
        with pytest.raises(ValueError, match="missing magic header"):
            vault.decrypt(b"not encrypted data")


class TestEncryptionConfig:
    def test_from_dict_defaults(self) -> None:
        config = EncryptionConfig.from_dict({})
        assert config.enabled is False
        assert config.provider == "local"

    def test_from_dict_azure(self) -> None:
        config = EncryptionConfig.from_dict({
            "enabled": True,
            "provider": "azure-keyvault",
            "key_vault_url": "https://vault.azure.net",
            "key_name": "my-key",
        })
        assert config.enabled is True
        assert config.provider == "azure-keyvault"
        assert config.key_vault_url == "https://vault.azure.net"

    def test_from_config_disabled(self, tmp_path: Path) -> None:
        config = EncryptionConfig(enabled=False)
        vault = EncryptionVault.from_config(config, tmp_path)
        assert vault is None

    def test_from_config_local(self, tmp_path: Path) -> None:
        config = EncryptionConfig(enabled=True, provider="local")
        vault = EncryptionVault.from_config(config, tmp_path)
        assert vault is not None
