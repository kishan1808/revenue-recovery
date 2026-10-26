"""Pytest configuration for the revenue-recovery test suite."""

import os
import sys
from pathlib import Path

# Add project root to Python path so `app` package is discoverable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Dedicated opt-in target for PostgreSQL integration tests.
#
# When TEST_DATABASE_URL is provided, it is validated by a fail-closed
# safety guard (tests.database_safety) to guarantee that it is a valid
# PostgreSQL target, does NOT target the application's configured
# database, has no ambiguous whitespace or query options, and has explicit
# destructive-test opt-in.
#
# When safety checks pass, it OVERRIDES any ambient or .env DATABASE_URL
# for the whole test process and synchronizes os.environ["TEST_DATABASE_URL"].
#
# When TEST_DATABASE_URL is genuinely unset or empty, every integration
# test module skips itself cleanly instead of falling back to another database.
# Whitespace-only or malformed configurations fail closed immediately.
from tests.database_safety import (  # noqa: E402
    bootstrap_test_database_environment,
)

RAW_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", None)

TEST_DATABASE_URL = bootstrap_test_database_environment(
    raw_test_url=RAW_TEST_DATABASE_URL,
    project_root=PROJECT_ROOT,
)
