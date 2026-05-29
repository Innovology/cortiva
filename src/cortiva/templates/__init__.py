"""
Cortiva agent templates.

Two kinds of templates exist:

1. **Bundled starter templates** — shipped inside the package, applied
   by ``apply_template(name, target_dir)``. These are slow-changing
   skeletons ("PM", "Dev", "QA") that anchor common roles. Used when
   ``-t <name>`` is passed without a scheme.

2. **HQ-hosted agents** — fetched from a Cortiva HQ instance at
   ``cortiva agent create`` time, applied by
   ``apply_hq_template(slug, target_dir, hq_url)``. These are the
   customer-specific named agents (CPO, PO-MarketMesh, every agent
   built through the HQ UI). Used when ``-t hq://<slug>`` is passed.

Mixing the two storage models was the historical mistake — agents
were bundled into the cortiva-hq wheel, which meant every new agent
required a wheel rebuild + node upgrade. The wheel stops at engine.
Content (templates + agents) lives in HQ and is fetched on demand.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

_PACKAGE = "cortiva.templates"
_HQ_SCHEME = "hq://"
_HQ_FETCH_TIMEOUT_S = 30.0


class HqFetchError(RuntimeError):
    """Raised when the framework can't fetch an HQ-hosted agent
    definition. Callers should surface the operator-actionable message
    (CLI prints it; programmatic callers can str() it)."""


def list_templates() -> list[str]:
    """Return names of all bundled agent templates."""
    templates: list[str] = []
    files = importlib.resources.files(_PACKAGE)
    for item in files.iterdir():
        if item.is_dir() and not item.name.startswith("_"):
            templates.append(item.name)
    return sorted(templates)


def get_template_path(name: str) -> Path:
    """Return the on-disk path for a bundled template.

    Raises ``KeyError`` if the template does not exist.
    """
    files = importlib.resources.files(_PACKAGE)
    candidate = files.joinpath(name)
    if not candidate.is_dir():
        available = list_templates()
        raise KeyError(
            f"Unknown template: {name!r}. Available: {', '.join(available)}"
        )
    # importlib.resources.files returns a Traversable; for on-disk packages
    # it's already a Path.  Cast for the type checker.
    return Path(str(candidate))


def apply_template(name: str, target_dir: Path) -> list[str]:
    """Copy a template's files into *target_dir*.

    Creates *target_dir* and all standard workspace subdirectories if
    they don't exist yet.  Returns the list of relative paths written.
    """
    from cortiva.core.agent import WORKSPACE_DIRS

    src = get_template_path(name)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create all workspace subdirectories
    for subdir in WORKSPACE_DIRS:
        (target_dir / subdir).mkdir(exist_ok=True)

    written: list[str] = []
    for item in sorted(src.rglob("*")):
        if item.name.startswith("_") or not item.is_file():
            continue
        rel = item.relative_to(src)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        written.append(str(rel))
    return written


def is_hq_template(template_arg: str) -> bool:
    """True if ``template_arg`` is an HQ reference (``hq://<slug>``)
    rather than a bundled-template name."""
    return template_arg.startswith(_HQ_SCHEME)


def parse_hq_slug(template_arg: str) -> str:
    """Extract the slug from an ``hq://<slug>`` reference."""
    if not is_hq_template(template_arg):
        raise ValueError(
            f"Expected hq:// reference, got {template_arg!r}",
        )
    slug = template_arg[len(_HQ_SCHEME):]
    if not slug or "/" in slug or ".." in slug:
        raise ValueError(f"Invalid HQ slug: {slug!r}")
    return slug


def apply_hq_template(
    slug: str,
    target_dir: Path,
    hq_url: str,
    *,
    timeout_s: float = _HQ_FETCH_TIMEOUT_S,
) -> list[str]:
    """Fetch an agent definition from Cortiva HQ and materialise it.

    Args:
        slug: the agent's slug as stored in HQ
            (e.g. ``cpo``, ``po-marketmesh``).
        target_dir: where to write the materialised files. Will be
            created if it doesn't exist.
        hq_url: base URL of the Cortiva HQ instance. The fetch path
            is ``<hq_url>/api/agents/definitions/<slug>``.
        timeout_s: HTTP timeout. The endpoint is fast (reads from
            local FS server-side) so 30s is generous.

    Returns:
        Relative paths of every file written.

    Raises:
        HqFetchError: any failure mode — HTTP non-200, malformed
            response, write error. Message is operator-actionable.
    """
    from cortiva.core.agent import WORKSPACE_DIRS

    base = hq_url.rstrip("/")
    url = f"{base}/api/agents/definitions/{slug}"

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get(
                "detail", "",
            )
        except Exception:
            detail = ""
        if exc.code == 404:
            raise HqFetchError(
                f"HQ has no agent named {slug!r} at {url}",
            ) from exc
        raise HqFetchError(
            f"HQ returned HTTP {exc.code} for {slug!r}: {detail or exc.reason}",
        ) from exc
    except urllib.error.URLError as exc:
        raise HqFetchError(
            f"Could not reach HQ at {hq_url}: {exc.reason}. "
            "Check the `hq.portal_url` setting in cortiva.yaml and "
            "that the node has network access.",
        ) from exc

    if status != 200:
        raise HqFetchError(
            f"HQ returned HTTP {status} for {slug!r}",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HqFetchError(
            f"HQ returned malformed JSON for {slug!r}: {exc.msg}",
        ) from exc

    if not isinstance(payload, dict):
        raise HqFetchError(
            f"HQ returned non-object response for {slug!r}",
        )
    deploy = payload.get("deploy")
    identity_files = payload.get("identity_files") or {}
    if not isinstance(deploy, dict):
        raise HqFetchError(
            f"HQ response for {slug!r} missing `deploy` object",
        )
    if not isinstance(identity_files, dict):
        raise HqFetchError(
            f"HQ response for {slug!r} has non-object `identity_files`",
        )

    # Materialise.
    target_dir.mkdir(parents=True, exist_ok=True)
    for subdir in WORKSPACE_DIRS:
        (target_dir / subdir).mkdir(exist_ok=True)

    written: list[str] = []

    import yaml as _yaml  # local — keeps import-time deps off the hot path

    deploy_path = target_dir / "deploy.yaml"
    deploy_path.write_text(_yaml.safe_dump(deploy, sort_keys=False))
    written.append("deploy.yaml")

    identity_dir = target_dir / "identity"
    identity_dir.mkdir(exist_ok=True)
    for filename, content in identity_files.items():
        # Defensive: refuse anything that would escape the identity dir.
        if "/" in filename or ".." in filename or "\\" in filename:
            raise HqFetchError(
                f"HQ response contained unsafe identity filename: {filename!r}",
            )
        (identity_dir / filename).write_text(content)
        written.append(f"identity/{filename}")

    return written
