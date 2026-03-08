"""
Portal authentication — local auth with bcrypt + JWT.

Provides user management, JWT token issuance, role-based permissions,
and activity audit logging. Uses SQLite for zero-dependency storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

# JWT is implemented with stdlib hmac+json to avoid a PyJWT dependency.
# For production, swap to PyJWT or python-jose.

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'observer',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    refresh_token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    key_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'observer',
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Role(Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    OBSERVER = "observer"

    @property
    def level(self) -> int:
        return {"owner": 4, "admin": 3, "manager": 2, "observer": 1}[self.value]

    def can_do(self, required: Role) -> bool:
        return self.level >= required.level


@dataclass
class User:
    id: str
    email: str
    name: str
    role: Role
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role.value,
            "created_at": self.created_at,
        }


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int  # seconds


def _hash_password(password: str) -> str:
    """Hash a password with a random salt using SHA-256.

    For production, use bcrypt. This avoids the bcrypt C dependency for v1.
    """
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${hashed.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    salt, hashed_hex = stored.split("$", 1)
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(computed.hex(), hashed_hex)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class JWTError(Exception):
    pass


class AuthDB:
    """SQLite-backed user and session store."""

    def __init__(self, db_path: str | Path = ".cortiva/portal.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = os.environ.get("CORTIVA_JWT_SECRET", secrets.token_hex(32))
        self._access_ttl = 900  # 15 minutes
        self._refresh_ttl = 604_800  # 7 days
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DB_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    # ----- User management -----

    def has_users(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            return row[0] > 0

    def create_user(
        self, email: str, name: str, password: str, role: str = "observer"
    ) -> User:
        user_id = secrets.token_hex(8)
        now = datetime.now(tz=UTC).isoformat()
        pw_hash = _hash_password(password)

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, role, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, name, pw_hash, role, now, now),
            )

        return User(id=user_id, email=email, name=name, role=Role(role), created_at=now)

    def get_user(self, user_id: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, name, role, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return User(id=row[0], email=row[1], name=row[2], role=Role(row[3]), created_at=row[4])

    def get_user_by_email(self, email: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, name, role, created_at FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return User(id=row[0], email=row[1], name=row[2], role=Role(row[3]), created_at=row[4])

    def list_users(self) -> list[User]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, email, name, role, created_at FROM users ORDER BY created_at"
            ).fetchall()
        return [
            User(id=r[0], email=r[1], name=r[2], role=Role(r[3]), created_at=r[4])
            for r in rows
        ]

    def verify_credentials(self, email: str, password: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, name, role, created_at, password_hash FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None:
            return None
        if not _verify_password(password, row[5]):
            return None
        return User(id=row[0], email=row[1], name=row[2], role=Role(row[3]), created_at=row[4])

    # ----- JWT tokens -----

    def _encode_jwt(self, payload: dict[str, Any]) -> str:
        import base64

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        body = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).rstrip(b"=").decode()
        msg = f"{header}.{body}"
        sig = hmac.new(self._secret.encode(), msg.encode(), hashlib.sha256).digest()
        sig_str = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        return f"{msg}.{sig_str}"

    def _decode_jwt(self, token: str) -> dict[str, Any]:
        import base64

        parts = token.split(".")
        if len(parts) != 3:
            raise JWTError("Invalid token format")

        header_b, body_b, sig_b = parts
        msg = f"{header_b}.{body_b}"
        expected_sig = hmac.new(self._secret.encode(), msg.encode(), hashlib.sha256).digest()

        # Pad base64
        sig_padded = sig_b + "=" * (4 - len(sig_b) % 4)
        actual_sig = base64.urlsafe_b64decode(sig_padded)

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise JWTError("Invalid signature")

        body_padded = body_b + "=" * (4 - len(body_b) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body_padded))

        if payload.get("exp", 0) < time.time():
            raise JWTError("Token expired")

        return payload

    def issue_tokens(self, user: User) -> TokenPair:
        now = time.time()
        access_payload = {
            "sub": user.id,
            "email": user.email,
            "role": user.role.value,
            "exp": now + self._access_ttl,
            "iat": now,
            "type": "access",
        }
        refresh_payload = {
            "sub": user.id,
            "exp": now + self._refresh_ttl,
            "iat": now,
            "type": "refresh",
        }

        access = self._encode_jwt(access_payload)
        refresh = self._encode_jwt(refresh_payload)

        # Store refresh token hash in sessions
        session_id = secrets.token_hex(8)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, refresh_token_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    user.id,
                    _hash_token(refresh),
                    datetime.now(tz=UTC).isoformat(),
                    datetime.fromtimestamp(now + self._refresh_ttl, tz=UTC).isoformat(),
                ),
            )

        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._access_ttl,
        )

    def verify_access_token(self, token: str) -> User | None:
        try:
            payload = self._decode_jwt(token)
        except JWTError:
            return None
        if payload.get("type") != "access":
            return None
        return self.get_user(payload["sub"])

    def refresh_access_token(self, refresh_token: str) -> TokenPair | None:
        try:
            payload = self._decode_jwt(refresh_token)
        except JWTError:
            return None
        if payload.get("type") != "refresh":
            return None

        # Check session is still valid
        token_hash = _hash_token(refresh_token)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, user_id FROM sessions "
                "WHERE refresh_token_hash = ? AND revoked = 0",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None

        user = self.get_user(payload["sub"])
        if user is None:
            return None

        # Revoke old session, issue new tokens
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET revoked = 1 WHERE id = ?", (row[0],))

        return self.issue_tokens(user)

    def revoke_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE id = ?", (session_id,)
            )
            return cursor.rowcount > 0

    # ----- Audit log -----

    def audit(
        self, user_id: str | None, action: str, target: str | None = None, details: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, action, target, details, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, action, target, details, datetime.now(tz=UTC).isoformat()),
            )

    def get_audit_log(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, user_id, action, target, details, timestamp "
                "FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            {
                "id": r[0],
                "user_id": r[1],
                "action": r[2],
                "target": r[3],
                "details": r[4],
                "timestamp": r[5],
            }
            for r in rows
        ]

    # ----- Org settings -----

    def set_org_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_org_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM org_settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def bootstrap_complete(self) -> bool:
        return self.get_org_setting("bootstrap_complete") == "true"

    def mark_bootstrap_complete(self) -> None:
        self.set_org_setting("bootstrap_complete", "true")
