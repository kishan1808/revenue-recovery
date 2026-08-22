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
# When TEST_DATABASE_URL is provided it OVERRIDES any ambient or .env
# DATABASE_URL for the whole test process. This guarantees that
# integration tests -- including destructive Alembic upgrade/downgrade
# cycles -- can only ever reach the isolated database the developer
# explicitly pointed them at (e.g. the Neon "revenue-recovery-test"
# branch), never an unreviewed default target.
#
# When it is not provided, every integration test module skips itself
# clearly instead of silently falling back to another database.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
