"""Load API keys from a local env file.

Keeps secrets out of the repository and out of chat logs. Existing environment
variables always win, so a shell-level ``$env:DEEPSEEK_API_KEY`` still overrides
the file.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path, *, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines into os.environ; return the names that were set."""

    path = Path(path)
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded
