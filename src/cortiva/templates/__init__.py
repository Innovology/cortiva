"""
Cortiva agent templates.

Templates are bundled inside the package so they ship with ``pip install``.
Each template is a directory containing the standard identity files that
seed a new agent with a ready-made persona.
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path

_PACKAGE = "cortiva.templates"


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

    Creates *target_dir* (and a ``journal/`` sub-directory) if they don't
    exist yet.  Returns the list of files written.
    """
    src = get_template_path(name)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "journal").mkdir(exist_ok=True)

    written: list[str] = []
    for item in sorted(src.iterdir()):
        if item.name.startswith("_"):
            continue
        dest = target_dir / item.name
        shutil.copy2(item, dest)
        written.append(item.name)
    return written
