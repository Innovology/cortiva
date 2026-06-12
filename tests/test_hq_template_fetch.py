"""Tests for cortiva agent create -t hq://<slug>.

Two layers:
1. ``apply_hq_template`` — the function. Mocks urllib so we exercise
   every failure mode (404, 500, network error, malformed JSON,
   unsafe filenames) and the happy path.
2. ``parse_hq_slug`` / ``is_hq_template`` — the URI helpers.

We don't test the CLI shell-out end-to-end here because that path is
in ``cmd_agent_create`` which mixes a lot of side-effecting code; the
integration test would be ``cortiva agent create cpo -t hq://cpo``
on a real node which the operator runs.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from cortiva.templates import (
    HqFetchError,
    apply_hq_template,
    is_hq_template,
    parse_hq_slug,
)

# ---------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------


class TestUriParsing:
    @pytest.mark.parametrize(
        "arg,expected",
        [
            ("hq://cpo", True),
            ("hq://po-marketmesh", True),
            ("pm-cortiva", False),
            ("local-template", False),
            ("", False),
        ],
    )
    def test_is_hq_template(self, arg: str, expected: bool) -> None:
        assert is_hq_template(arg) is expected

    def test_parse_hq_slug_basic(self) -> None:
        assert parse_hq_slug("hq://cpo") == "cpo"
        assert parse_hq_slug("hq://po-marketmesh") == "po-marketmesh"

    def test_parse_hq_slug_rejects_non_hq(self) -> None:
        with pytest.raises(ValueError, match="hq://"):
            parse_hq_slug("local-name")

    @pytest.mark.parametrize(
        "bad_slug",
        [
            "hq://",
            "hq://path/traversal",
            "hq://../escape",
        ],
    )
    def test_parse_hq_slug_rejects_path_traversal(
        self,
        bad_slug: str,
    ) -> None:
        with pytest.raises(ValueError):
            parse_hq_slug(bad_slug)


# ---------------------------------------------------------------------
# apply_hq_template — fetch and materialise
# ---------------------------------------------------------------------


def _mock_urlopen(payload: dict, status: int = 200):
    """Build a context manager that returns ``payload`` as JSON when
    used as ``urllib.request.urlopen()``'s return value."""
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        status = 200

        def __init__(self) -> None:
            self.status = status
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    return _Resp()


class TestApplyHqTemplate:
    def test_happy_path_writes_deploy_and_identity_files(
        self,
        tmp_path: Path,
    ) -> None:
        payload = {
            "slug": "cpo",
            "deploy": {
                "agent": {
                    "name": "CPO",
                    "role": "Chief Product Officer",
                    "reports_to": "human-founder",
                },
            },
            "identity_files": {
                "identity.md": "# CPO\n",
                "soul.md": "---\nfoo: 1\n---\n# soul\n",
            },
        }
        target = tmp_path / "cpo"
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            return_value=_mock_urlopen(payload),
        ):
            written = apply_hq_template(
                "cpo",
                target,
                hq_url="https://hq.example.com",
            )

        deploy_path = target / "deploy.yaml"
        assert deploy_path.exists()
        loaded = yaml.safe_load(deploy_path.read_text())
        assert loaded["agent"]["name"] == "CPO"

        assert (target / "identity" / "identity.md").read_text() == "# CPO\n"
        assert (target / "identity" / "soul.md").read_text().startswith("---")
        assert "deploy.yaml" in written
        assert "identity/identity.md" in written

    def test_404_raises_actionable_message(self, tmp_path: Path) -> None:
        err = urllib.error.HTTPError(
            url="x",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"Agent \'nope\' not found"}'),
        )
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(HqFetchError, match="no agent named"):
                apply_hq_template("nope", tmp_path / "x", "https://hq.example.com")

    def test_500_includes_hq_detail(self, tmp_path: Path) -> None:
        err = urllib.error.HTTPError(
            url="x",
            code=500,
            msg="Server Error",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"deploy.yaml malformed: missing colon"}'),
        )
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(HqFetchError, match="malformed"):
                apply_hq_template("x", tmp_path / "x", "https://hq.example.com")

    def test_network_error_mentions_config_path(self, tmp_path: Path) -> None:
        """Operator needs to know *where to look* — the URLError on its
        own is just a connection refused. Wrap it with cortiva.yaml
        guidance."""
        err = urllib.error.URLError("Connection refused")
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(HqFetchError, match="cortiva.yaml"):
                apply_hq_template("x", tmp_path / "x", "https://hq.example.com")

    def test_malformed_json_response(self, tmp_path: Path) -> None:
        class _Resp:
            status = 200

            def read(self):
                return b"this is not json"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with patch(
            "cortiva.templates.urllib.request.urlopen",
            return_value=_Resp(),
        ):
            with pytest.raises(HqFetchError, match="JSON"):
                apply_hq_template("x", tmp_path / "x", "https://hq.example.com")

    def test_payload_missing_deploy_object(self, tmp_path: Path) -> None:
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            return_value=_mock_urlopen({"identity_files": {}}),
        ):
            with pytest.raises(HqFetchError, match="deploy"):
                apply_hq_template("x", tmp_path / "x", "https://hq.example.com")

    def test_rejects_identity_filename_with_traversal(
        self,
        tmp_path: Path,
    ) -> None:
        """Defensive: a compromised or buggy HQ could send a filename
        that escapes the identity dir. The framework must refuse."""
        payload = {
            "slug": "x",
            "deploy": {"agent": {"name": "X"}},
            "identity_files": {
                "../../../etc/passwd": "pwned\n",
            },
        }
        with patch(
            "cortiva.templates.urllib.request.urlopen",
            return_value=_mock_urlopen(payload),
        ):
            with pytest.raises(HqFetchError, match="unsafe"):
                apply_hq_template("x", tmp_path / "x", "https://hq.example.com")

    def test_trailing_slash_in_hq_url_is_stripped(
        self,
        tmp_path: Path,
    ) -> None:
        """Operator typo defence: ``hq.portal_url`` may or may not have
        a trailing slash. The fetcher should produce the same URL
        either way."""
        captured: dict[str, str] = {}

        def _check(url, timeout):  # noqa: ARG001
            captured["url"] = url
            return _mock_urlopen(
                {
                    "slug": "x",
                    "deploy": {"agent": {}},
                    "identity_files": {},
                }
            )

        with patch(
            "cortiva.templates.urllib.request.urlopen",
            side_effect=_check,
        ):
            apply_hq_template(
                "x",
                tmp_path / "x",
                "https://hq.example.com/",
            )
        assert captured["url"] == "https://hq.example.com/api/agents/definitions/x"
