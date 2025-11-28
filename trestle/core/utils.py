"""Shared helpers for trestle core module."""

from __future__ import annotations

import pathlib

from trestle.common import file_utils

# Default trestle workspace root relative to this file.
# Assumes the trestle package is installed from within an oscal workspace.
DEFAULT_TRESTLE_ROOT = pathlib.Path.cwd() / "build"


def find_trestle_root(preferred_root: pathlib.Path = DEFAULT_TRESTLE_ROOT) -> pathlib.Path:
    """Locate a trestle workspace, preferring the given root directory."""
    candidates = (
        file_utils.extract_trestle_project_root(preferred_root),
        file_utils.extract_trestle_project_root(pathlib.Path.cwd()),
    )
    for candidate in candidates:
        if candidate:
            return candidate
    raise RuntimeError("No trestle workspace found (checked preferred root and parents of CWD).")
