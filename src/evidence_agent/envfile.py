"""Load API keys from a local env file.

Keeps secrets out of the repository and out of chat logs. Existing environment
variables always win, so a shell-level ``$env:DEEPSEEK_API_KEY`` still overrides
the file.
"""

from __future__ import annotations

import os
from pathlib import Path

#: 项目外的密钥文件位置。密钥不该躺在项目树里：项目会被打包、分享、当作品集上传。
EXTERNAL_ENV_PATHS = (
    Path.home() / ".deepseek" / "evidence-agent.env",
)


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


def default_env_files(project_root: Path) -> list[Path]:
    """按优先级列出要加载的密钥文件。

    顺序：项目内 .env.local（老位置，保留兼容）→ 环境变量 EVIDENCE_AGENT_ENV_FILE
    指定的路径 → 项目外（~/.deepseek/evidence-agent.env）。后面的文件不覆盖前面已经
    设好的变量，所以老位置仍然优先。
    """

    paths = [Path(project_root) / ".env.local"]
    override = os.environ.get("EVIDENCE_AGENT_ENV_FILE", "").strip()
    if override:
        paths.append(Path(override))
    paths.extend(EXTERNAL_ENV_PATHS)
    return paths


def load_env_files(project_root: Path, *, override: bool = False) -> list[str]:
    """按 default_env_files 的顺序加载，返回被设置的变量名。"""

    loaded: list[str] = []
    for path in default_env_files(project_root):
        for name in load_env_file(path, override=override):
            if name not in loaded:
                loaded.append(name)
    return loaded
