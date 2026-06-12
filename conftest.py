"""Root conftest.py — ensures the project root is on sys.path.

pytest's default (prepend) import mode inserts the directory that contains
each test file into sys.path[0].  For tests/ that means only the tests/
directory itself is importable, not the top-level ``tests`` package.

Adding the project root here means ``tests.test_plugins`` (and similar
dotted paths) are resolvable by importlib, which is required by
load_plugins_from_config when tests reference plugin classes by their
fully-qualified name.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Insert the repo root (the directory that contains tests/) so that
# ``tests`` is discoverable as a namespace package.
_repo_root = str(Path(__file__).parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
