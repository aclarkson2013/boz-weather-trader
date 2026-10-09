"""Guard: the backend image's dependency layer must not depend on VERSION.

Copying VERSION before `pip install` busts the Docker layer cache on every
release, reinstalling all packages (~30 min on the homelab VM). See
docs/ALGO_CHANGELOG.md v1.14.3.
"""

from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile.backend"


def test_version_is_copied_after_dependency_install() -> None:
    lines = [ln.strip() for ln in DOCKERFILE.read_text().splitlines()]
    pip_idx = next(i for i, ln in enumerate(lines) if "pip install" in ln)
    copies_before = [ln for ln in lines[:pip_idx] if ln.startswith("COPY")]
    assert all("VERSION" not in ln for ln in copies_before), copies_before
    assert any(ln.startswith("COPY") and "VERSION" in ln for ln in lines[pip_idx:])
