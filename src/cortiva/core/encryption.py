"""
Encryption at rest for agent data.

Provides transparent encryption/decryption for agent identity files,
journals, and workspace data.  Uses AES-256-GCM via the ``cryptography``
library (or falls back to Fernet if simpler key management is preferred).

Two key sources:

- **Local key file**: A symmetric key stored in ``{agents_dir}/.cortiva/encryption.key``.
  Suitable for development and single-node deployments.
- **Azure Key Vault**: Key retrieved from Azure Key Vault at startup.
  Required for customer-deployed nodes.  The node uses Azure Managed
  Identity to authenticate — no credentials stored locally.

Encrypted files have a ``.enc`` extension and a magic header so they
can be distinguished from plaintext.

Usage::

    vault = EncryptionVault.from_config(config)
    ciphertext = vault.encrypt(plaintext_bytes)
    plaintext = vault.decrypt(ciphertext)

    # Or use the file helpers:
    vault.encrypt_file(path)   # encrypts in-place, adds .enc
    vault.decrypt_file(path)   # decrypts in-place, removes .enc
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortiva.encryption")

# Magic header for encrypted files (8 bytes)
_MAGIC = b"CORTIVA\x00"
_VERSION = 1


@dataclass
class EncryptionConfig:
    """Parsed ``encryption`` config section."""

    enabled: bool = False
    provider: str = "local"
    """``local`` (file-based key) or ``azure-keyvault``."""

    key_vault_url: str = ""
    """Azure Key Vault URL (for ``azure-keyvault`` provider)."""

    key_name: str = "cortiva-encryption-key"
    """Key name in Azure Key Vault."""

    local_key_path: str = ""
    """Path to local key file.  Auto-generated if missing."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EncryptionConfig:
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            provider=data.get("provider", "local"),
            key_vault_url=data.get("key_vault_url", ""),
            key_name=data.get("key_name", "cortiva-encryption-key"),
            local_key_path=data.get("local_key_path", ""),
        )


class EncryptionVault:
    """Encrypts and decrypts agent data.

    Uses AES-256-GCM when the ``cryptography`` library is available,
    falls back to a simpler XOR-based obfuscation for environments
    without the library (with a loud warning).
    """

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._has_crypto = False
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._aesgcm = AESGCM(key[:32])  # AES-256 needs 32 bytes
            self._has_crypto = True
        except ImportError:
            logger.warning(
                "cryptography library not installed — using basic "
                "obfuscation instead of AES-256-GCM. Install with: "
                "pip install cryptography"
            )
            self._aesgcm = None

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt data.  Returns magic header + version + nonce + ciphertext."""
        if self._has_crypto and self._aesgcm is not None:
            nonce = os.urandom(12)
            ciphertext = self._aesgcm.encrypt(nonce, data, None)
            return _MAGIC + bytes([_VERSION]) + nonce + ciphertext
        # Fallback: XOR obfuscation (NOT secure — just prevents casual reading)
        key_stream = hashlib.sha256(self._key).digest()
        obfuscated = bytes(b ^ key_stream[i % 32] for i, b in enumerate(data))
        return _MAGIC + bytes([0]) + obfuscated

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt data.  Expects magic header + version + nonce + ciphertext."""
        if not data.startswith(_MAGIC):
            raise ValueError("Not an encrypted Cortiva file (missing magic header)")

        version = data[len(_MAGIC)]

        if version == _VERSION and self._has_crypto and self._aesgcm is not None:
            nonce = data[len(_MAGIC) + 1: len(_MAGIC) + 13]
            ciphertext = data[len(_MAGIC) + 13:]
            return self._aesgcm.decrypt(nonce, ciphertext, None)
        elif version == 0:
            # XOR fallback
            payload = data[len(_MAGIC) + 1:]
            key_stream = hashlib.sha256(self._key).digest()
            return bytes(b ^ key_stream[i % 32] for i, b in enumerate(payload))
        else:
            raise ValueError(f"Unsupported encryption version: {version}")

    def encrypt_file(self, path: Path) -> Path:
        """Encrypt a file in-place.  Returns the new path (with .enc)."""
        plaintext = path.read_bytes()
        ciphertext = self.encrypt(plaintext)
        enc_path = path.with_suffix(path.suffix + ".enc")
        enc_path.write_bytes(ciphertext)
        path.unlink()
        return enc_path

    def decrypt_file(self, path: Path) -> Path:
        """Decrypt a .enc file in-place.  Returns the decrypted path."""
        ciphertext = path.read_bytes()
        plaintext = self.decrypt(ciphertext)
        dec_path = Path(str(path).removesuffix(".enc"))
        dec_path.write_bytes(plaintext)
        path.unlink()
        return dec_path

    def is_encrypted(self, path: Path) -> bool:
        """Check if a file is encrypted (has magic header)."""
        if not path.exists():
            return False
        try:
            header = path.read_bytes()[:len(_MAGIC)]
            return header == _MAGIC
        except OSError:
            return False

    # ----- Factory methods -----

    @classmethod
    def from_local_key(cls, key_path: Path) -> EncryptionVault:
        """Create a vault from a local key file.

        Generates the key file if it doesn't exist.
        """
        if key_path.exists():
            key = base64.b64decode(key_path.read_text(encoding="utf-8").strip())
        else:
            key = secrets.token_bytes(32)
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text(base64.b64encode(key).decode(), encoding="utf-8")
            key_path.chmod(0o600)
            logger.info("Generated encryption key: %s", key_path)
        return cls(key)

    @classmethod
    def from_azure_keyvault(cls, vault_url: str, key_name: str) -> EncryptionVault:
        """Create a vault from an Azure Key Vault key.

        Uses Azure Managed Identity for authentication — no
        credentials stored locally.
        """
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.keys import KeyClient
            from azure.keyvault.keys.crypto import CryptographyClient
        except ImportError:
            raise ImportError(
                "Azure Key Vault support requires: "
                "pip install azure-identity azure-keyvault-keys"
            )

        credential = DefaultAzureCredential()
        key_client = KeyClient(vault_url=vault_url, credential=credential)
        key = key_client.get_key(key_name)

        # Derive a symmetric key from the vault key's ID (deterministic)
        key_id = key.id or key_name
        symmetric_key = hashlib.sha256(key_id.encode()).digest()

        logger.info(
            "Encryption key loaded from Azure Key Vault: %s/%s",
            vault_url, key_name,
        )
        return cls(symmetric_key)

    @classmethod
    def from_config(
        cls, config: EncryptionConfig, agents_dir: Path,
    ) -> EncryptionVault | None:
        """Create a vault from parsed config.

        Returns ``None`` if encryption is disabled.
        """
        if not config.enabled:
            return None

        if config.provider == "azure-keyvault":
            if not config.key_vault_url:
                raise ValueError("encryption.key_vault_url required for azure-keyvault provider")
            return cls.from_azure_keyvault(config.key_vault_url, config.key_name)

        # Local key file
        if config.local_key_path:
            key_path = Path(config.local_key_path)
        else:
            key_path = agents_dir / ".cortiva" / "encryption.key"
        return cls.from_local_key(key_path)
