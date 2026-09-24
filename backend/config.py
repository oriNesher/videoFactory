"""Workspace location, layout and backend-only settings.

The workspace is application-managed local storage. It holds one directory per
project (metadata, jobs, plans, generated intermediates, exports). Source
footage is *not* stored here: it stays wherever the user recorded it and is only
referenced by absolute path.

Settings that carry credentials live in environment variables, never in project
files and never in anything the frontend can read.
"""

import os
from pathlib import Path

# backend/config.py -> backend/ -> repository root
REPO_ROOT = Path(__file__).resolve().parent.parent

WORKSPACE_ENV_VAR = "VIDEO_FACTORY_WORKSPACE"

DEFAULT_WORKSPACE = REPO_ROOT / "workspace"

ENV_FILE = REPO_ROOT / ".env"

# --- LLM settings -----------------------------------------------------------

PROVIDER_ENV_VAR = "VIDEO_FACTORY_LLM_PROVIDER"
MODEL_ENV_VAR = "VIDEO_FACTORY_LLM_MODEL"
TIMEOUT_ENV_VAR = "VIDEO_FACTORY_LLM_TIMEOUT"
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

DEFAULT_PROVIDER = "mock"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 600.0


def load_env_file(path: Path | None = None) -> None:
    """Read `KEY=VALUE` lines from `.env` into the environment.

    Deliberately minimal (no dependency): blank lines and `#` comments are
    skipped, surrounding quotes are stripped, and a variable that is already set
    in the real environment always wins — so an explicit PowerShell `$env:...`
    overrides the file, and tests are never surprised by a developer's `.env`.
    """
    env_path = path or ENV_FILE
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


def get_workspace_root() -> Path:
    """Return the configured workspace root, creating it if needed."""
    configured = os.environ.get(WORKSPACE_ENV_VAR, "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_WORKSPACE
    root = root.resolve() if root.exists() else Path(os.path.abspath(root))
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_projects_root() -> Path:
    """Return the directory that contains one sub-directory per project."""
    projects_root = get_workspace_root() / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    return projects_root


def get_llm_settings() -> dict:
    """Return the configured provider. Never returns the API key itself."""
    provider = os.environ.get(PROVIDER_ENV_VAR, "").strip().lower() or DEFAULT_PROVIDER
    model = os.environ.get(MODEL_ENV_VAR, "").strip() or DEFAULT_MODEL

    raw_timeout = os.environ.get(TIMEOUT_ENV_VAR, "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(max(timeout, 1.0), MAX_TIMEOUT_SECONDS)

    return {
        "provider": provider,
        "model": model,
        "timeout_seconds": timeout,
        # Presence only. The value never leaves this process.
        "api_key_present": bool(os.environ.get(API_KEY_ENV_VAR, "").strip()),
    }
