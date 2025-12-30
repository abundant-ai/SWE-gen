from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# The override content that enables network isolation
# Docker Compose automatically merges docker-compose.override.yaml with docker-compose.yaml
NETWORK_ISOLATION_OVERRIDE = """# Auto-generated override for network isolation testing
# This file is temporary and should be removed after validation
networks:
  default:
    internal: true
"""

OVERRIDE_FILENAME = "docker-compose.override.yaml"


@contextlib.contextmanager
def network_isolation(task_dir: Path) -> Generator[Path, None, None]:
    """Context manager that temporarily enables network isolation for a task.

    Creates docker-compose.override.yaml on entry and removes it on exit,
    ensuring cleanup even if an exception occurs.

    Args:
        task_dir: Path to the task directory containing docker-compose.yaml

    Yields:
        Path to the override file

    Example:
        with network_isolation(task_dir):
            # Run validation here - network will be isolated
            run_harbor_validation(task_dir)
        # Override file is automatically cleaned up
    """
    task_dir = Path(task_dir)
    override_path = task_dir / OVERRIDE_FILENAME

    # Check if override already exists (don't overwrite user's file)
    existing = override_path.exists()
    if existing:
        # Just yield the existing path without modifying
        yield override_path
        return

    try:
        override_path.write_text(NETWORK_ISOLATION_OVERRIDE)
        yield override_path
    finally:
        # Clean up the override file
        if override_path.exists() and not existing:
            override_path.unlink()
