"""Path-traversal guards.

Every media path in the app comes from the database, but a database row is not a trust
boundary — an admin-assigned path or a crafted subtitle sidecar must still not be able to
read outside the mount.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import AppError
from app.services.scanner import safe_media_path


@pytest.mark.parametrize(
    "relative",
    [
        "../../etc/passwd",
        "../secrets.txt",
        "Show/../../../../etc/hosts",
        "/etc/passwd",
        "Show/./../../outside.mkv",
    ],
)
def test_escaping_paths_are_refused(tmp_env: Path, relative: str) -> None:
    with pytest.raises(AppError) as excinfo:
        safe_media_path(relative)
    assert excinfo.value.code == "PATH_TRAVERSAL"


def test_legitimate_path_resolves(tmp_env: Path) -> None:
    from app.config import get_settings

    root = get_settings().library_root
    target = root / "Show" / "Season 01" / "ep.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")

    assert safe_media_path("Show/Season 01/ep.mkv") == target.resolve()


def test_symlink_escape_is_refused(tmp_env: Path) -> None:
    """A symlink inside the library pointing out of it is the subtle version of traversal."""
    from app.config import get_settings

    root = get_settings().library_root
    outside = tmp_env / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.mkv"
    secret.write_bytes(b"secret")

    link = root / "escape.mkv"
    link.symlink_to(secret)

    with pytest.raises(AppError) as excinfo:
        safe_media_path("escape.mkv")
    assert excinfo.value.code == "PATH_TRAVERSAL"
