"""Configuration behavior tests.

No test in this module ever prints a connection string or credential.
"""

import os
import subprocess
import sys
from pathlib import Path

from app.config import RecoverySettings, settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_settings_loads():
    """The application settings object instantiates."""
    assert settings is not None


def test_database_url_is_configured_in_this_environment():
    """A DATABASE_URL must be present for the app to be importable at all
    (app.database fails fast otherwise). Assert presence, never value."""
    assert isinstance(settings.DATABASE_URL, str)
    assert settings.DATABASE_URL != ""


def test_database_url_loaded_from_environment(monkeypatch):
    """DATABASE_URL is read from the environment when provided."""
    fake = "postgresql+psycopg://user:secret@example.test/dbname"
    monkeypatch.setenv("DATABASE_URL", fake)
    # _env_file=None ignores any local .env so the env var alone is proven.
    loaded = RecoverySettings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.DATABASE_URL == fake


def test_missing_database_url_fails_fast_with_clear_error(tmp_path):
    """With no DATABASE_URL anywhere, importing app.database must fail fast
    with an explicit, actionable message (not an obscure driver error)."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    code = "import app.database"  # engine creation triggers the guard
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),  # away from project .env
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "DATABASE_URL is not configured" in combined
