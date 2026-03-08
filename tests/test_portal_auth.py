"""Tests for the portal authentication system."""

from pathlib import Path

import pytest

from cortiva.portal.auth import AuthDB, Role, User


class TestAuthDB:
    def _make_db(self, tmp_path: Path) -> AuthDB:
        return AuthDB(db_path=tmp_path / "test.db")

    def test_no_users_initially(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        assert db.has_users() is False

    def test_create_user(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        user = db.create_user("test@example.com", "Test User", "password123", role="admin")
        assert user.email == "test@example.com"
        assert user.role == Role.ADMIN
        assert db.has_users() is True

    def test_verify_credentials(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        db.create_user("test@example.com", "Test User", "password123")

        user = db.verify_credentials("test@example.com", "password123")
        assert user is not None
        assert user.email == "test@example.com"

        # Wrong password
        assert db.verify_credentials("test@example.com", "wrong") is None

        # Wrong email
        assert db.verify_credentials("nobody@example.com", "password123") is None

    def test_get_user(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        created = db.create_user("test@example.com", "Test", "pw")
        found = db.get_user(created.id)
        assert found is not None
        assert found.email == "test@example.com"

    def test_get_user_by_email(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        db.create_user("test@example.com", "Test", "pw")
        found = db.get_user_by_email("test@example.com")
        assert found is not None
        assert found.name == "Test"

    def test_list_users(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        db.create_user("a@test.com", "Alice", "pw")
        db.create_user("b@test.com", "Bob", "pw")
        users = db.list_users()
        assert len(users) == 2


class TestJWT:
    def _make_db(self, tmp_path: Path) -> AuthDB:
        return AuthDB(db_path=tmp_path / "test.db")

    def test_issue_and_verify_tokens(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        user = db.create_user("test@example.com", "Test", "pw")
        tokens = db.issue_tokens(user)

        assert tokens.access_token
        assert tokens.refresh_token
        assert tokens.expires_in == 900

        verified = db.verify_access_token(tokens.access_token)
        assert verified is not None
        assert verified.id == user.id

    def test_invalid_token_rejected(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        assert db.verify_access_token("invalid.token.here") is None

    def test_refresh_token(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        user = db.create_user("test@example.com", "Test", "pw")
        tokens = db.issue_tokens(user)

        new_tokens = db.refresh_access_token(tokens.refresh_token)
        assert new_tokens is not None
        assert new_tokens.access_token != tokens.access_token

        # Old refresh token should be revoked
        assert db.refresh_access_token(tokens.refresh_token) is None

    def test_refresh_with_access_token_fails(self, tmp_path: Path) -> None:
        db = self._make_db(tmp_path)
        user = db.create_user("test@example.com", "Test", "pw")
        tokens = db.issue_tokens(user)

        # Can't use access token as refresh token
        assert db.refresh_access_token(tokens.access_token) is None


class TestRoles:
    def test_role_hierarchy(self) -> None:
        assert Role.OWNER.can_do(Role.ADMIN) is True
        assert Role.OWNER.can_do(Role.OWNER) is True
        assert Role.ADMIN.can_do(Role.MANAGER) is True
        assert Role.ADMIN.can_do(Role.OWNER) is False
        assert Role.MANAGER.can_do(Role.OBSERVER) is True
        assert Role.MANAGER.can_do(Role.ADMIN) is False
        assert Role.OBSERVER.can_do(Role.OBSERVER) is True
        assert Role.OBSERVER.can_do(Role.MANAGER) is False


class TestAuditLog:
    def test_audit_and_retrieve(self, tmp_path: Path) -> None:
        db = AuthDB(db_path=tmp_path / "test.db")
        db.audit("user-1", "login")
        db.audit("user-1", "wake_agent", target="bookkeep-01")
        db.audit("user-2", "create_snapshot", target="dev-cortiva")

        log = db.get_audit_log()
        assert len(log) == 3
        # Newest first
        assert log[0]["action"] == "create_snapshot"
        assert log[0]["user_id"] == "user-2"

    def test_audit_pagination(self, tmp_path: Path) -> None:
        db = AuthDB(db_path=tmp_path / "test.db")
        for i in range(10):
            db.audit("user-1", f"action-{i}")

        page = db.get_audit_log(limit=3, offset=0)
        assert len(page) == 3
        assert page[0]["action"] == "action-9"


class TestOrgSettings:
    def test_set_and_get(self, tmp_path: Path) -> None:
        db = AuthDB(db_path=tmp_path / "test.db")
        db.set_org_setting("org_name", "Acme Corp")
        assert db.get_org_setting("org_name") == "Acme Corp"
        assert db.get_org_setting("nonexistent") is None

    def test_bootstrap_status(self, tmp_path: Path) -> None:
        db = AuthDB(db_path=tmp_path / "test.db")
        assert db.bootstrap_complete() is False
        db.mark_bootstrap_complete()
        assert db.bootstrap_complete() is True
