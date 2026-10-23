"""Small environment helpers for local command-line tooling."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """Load KEY=VALUE lines from the repo-local .env file if it exists.

    Existing environment variables win by default, so shell/conda-provided
    secrets are not accidentally replaced by blank template values.
    """
    dotenv_path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / ".env"
    if not dotenv_path.exists():
        return None

    for raw_line in dotenv_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value

    return dotenv_path
