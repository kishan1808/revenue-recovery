"""PostgreSQL test database safety guard and identity validation.

Ensures that destructive integration tests can only ever execute against
a dedicated, isolated test database and never against the application's
configured development or production database.

This module is strictly computational: it parses and normalizes database
connection strings in-memory and NEVER connects to any database.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from dotenv import dotenv_values
from sqlalchemy.engine.url import make_url

__all__ = [
    "DESTRUCTIVE_OPT_IN_ENV_VAR",
    "DESTRUCTIVE_OPT_IN_REQUIRED_VALUE",
    "FORBIDDEN_PG_ENV_VARS",
    "FORBIDDEN_TARGET_PARAMS",
    "DatabaseIdentity",
    "DatabaseSafetyError",
    "bootstrap_test_database_environment",
    "canonicalize_host",
    "check_environment_database_safety",
    "normalize_database_url",
    "resolve_application_database_url",
    "validate_test_database_safety",
]

DESTRUCTIVE_OPT_IN_ENV_VAR: Final[str] = "ALLOW_DESTRUCTIVE_POSTGRES_TESTS"
DESTRUCTIVE_OPT_IN_REQUIRED_VALUE: Final[str] = "true"

# Localhost hostname aliases that identify the local machine
_LOCAL_HOSTNAME_ALIASES: Final[frozenset[str]] = frozenset(
    {"localhost", "local"}
)

# Connection parameters that can override, redirect, or alias the effective
# connection target or server identity in libpq, psycopg, or SQLAlchemy.
# For destructive test execution, any URL containing these parameters is
# rejected fail-closed because local URL parsing cannot guarantee that the
# effective target does not collide with the application database.
_FORBIDDEN_TARGET_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "database",
        "service",
        "servicefile",
        "target_session_attrs",
        "load_balance_hosts",
        "options",
        "dsn",
    }
)
FORBIDDEN_TARGET_PARAMS = _FORBIDDEN_TARGET_PARAMS

# PostgreSQL ambient environment variables that can alter connection target,
# route, service identity, or backend schema/session options in libpq or
# psycopg. When running destructive tests, having any of these set in the
# ambient environment makes the effective target ambiguous or altered
# out-of-band.
_FORBIDDEN_PG_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
        "PGTARGETSESSIONATTRS",
        "PGLOADBALANCEHOSTS",
    }
)
FORBIDDEN_PG_ENV_VARS = _FORBIDDEN_PG_ENV_VARS


class DatabaseSafetyError(Exception):
    """Raised when test database safety requirements are not met.

    Error messages intentionally omit URLs, credentials, hostnames,
    and database names to prevent secret leakage.
    """


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    """Normalized database connection identity for collision detection."""

    backend: str
    host: str
    port: int
    database: str


def canonicalize_host(raw_host: str | None) -> str:
    """Canonicalize a database hostname or IP literal for collision detection.

    Rules:
    - Rejects missing, empty, or whitespace-only hostnames.
    - Strips outer RFC 3986 brackets (for IPv6 literals) and trailing dots.
    - Rejects embedded brackets, commas, or unbracketed colons.
    - Normalizes known local hostname aliases ("localhost", "local") to "localhost".
    - Parses IPv4 and IPv6 literals using standard library ipaddress.ip_address():
        - Loopback addresses (ip.is_loopback) and unspecified addresses
          (ip.is_unspecified, e.g. 0.0.0.0, ::) map conservatively to "localhost".
        - Other valid IP addresses are canonicalized to ip.compressed.
    - Validates hostname syntax and rejects ambiguous non-IP formats
      (e.g. malformed IPv4-like numeric strings or unbracketed colons).
    """
    if not isinstance(raw_host, str):
        raise DatabaseSafetyError("Database URL must explicitly specify a hostname.")

    cleaned = raw_host.strip().lower().rstrip(".")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1].strip().lower().rstrip(".")

    if not cleaned:
        raise DatabaseSafetyError("Database URL must explicitly specify a hostname.")

    if "[" in cleaned or "]" in cleaned:
        raise DatabaseSafetyError(
            "Database URL contains an invalid or ambiguous hostname format."
        )

    if "," in cleaned:
        raise DatabaseSafetyError(
            "Multi-host database URLs are ambiguous and forbidden for test safety."
        )

    if cleaned in _LOCAL_HOSTNAME_ALIASES:
        return "localhost"

    try:
        ip = ipaddress.ip_address(cleaned)
        if ip.is_loopback or ip.is_unspecified:
            return "localhost"
        return ip.compressed
    except ValueError:
        pass

    if ":" in cleaned:
        raise DatabaseSafetyError(
            "Database URL contains an invalid or unbracketed IPv6 literal."
        )

    if all(c.isdigit() or c == "." for c in cleaned):
        raise DatabaseSafetyError(
            "Database URL contains an invalid or ambiguous IPv4 literal."
        )

    return cleaned


def check_environment_database_safety(
    env: Mapping[str, str] | None = None,
) -> None:
    """Validate that the environment has no target-altering PG variables.

    Checks for the presence of variables that libpq or psycopg could use to
    override or redirect connection targets, hosts, ports, services,
    or schema/session options.

    Does not print the values of any environment variables.
    """
    target_env = os.environ if env is None else env
    found_forbidden = [
        var
        for var in sorted(_FORBIDDEN_PG_ENV_VARS)
        if var in target_env and str(target_env[var]).strip()
    ]
    if found_forbidden:
        vars_str = ", ".join(found_forbidden)
        raise DatabaseSafetyError(
            f"PostgreSQL target-altering environment variable(s) detected: {vars_str}. "
            "Ambient target-altering environment variables are forbidden during "
            "destructive test validation to ensure unambiguous database isolation."
        )


def normalize_database_url(url_str: str) -> DatabaseIdentity:
    """Parse and normalize a database URL into a comparable identity.

    Normalizes:
    - Dialect / driver to the canonical backend name (e.g. 'postgresql').
    - Hostname to canonical form via canonicalize_host(), mapping all loopback
      IP variants (IPv4 127.0.0.0/8, IPv6 ::1, IPv4-mapped loopbacks) to 'localhost'.
    - Port to explicit integer between 1 and 65535.
    - Database name stripped of slashes and whitespace.

    Fails closed on:
    - Non-string or empty inputs.
    - Whitespace-only strings or leading/trailing whitespace.
    - Missing explicit hostname (will NOT default to localhost).
    - Missing explicit port (will NOT default to 5432).
    - Invalid port numbers.
    - Non-PostgreSQL dialects.
    - Multi-host URLs (ambiguous target resolution).
    - URLs with target-overriding or service-based query parameters.
    - URLs with unexpected fragments.

    Standard operational query parameters (such as sslmode, channel_binding,
    connect_timeout) are permitted and ignored during identity comparison.

    Raises DatabaseSafetyError without leaking credentials or chaining
    lower-level parsing exceptions into __cause__ or __context__.
    """
    if not isinstance(url_str, str) or not url_str:
        raise DatabaseSafetyError("Database URL must be a non-empty string.")

    if not url_str.strip():
        raise DatabaseSafetyError("Database URL cannot be a whitespace-only string.")

    if url_str != url_str.strip():
        raise DatabaseSafetyError(
            "Database URL contains forbidden leading or trailing whitespace."
        )

    cleaned_url = url_str

    # Pre-parse using urlsplit to detect raw query params, fragments, and multi-hosts.
    # We trap any exception and raise outside the except block to guarantee that
    # __cause__ and __context__ are None, eliminating error-chain credential leakage.
    is_split_error = False
    raw_split = None
    try:
        raw_split = urlsplit(cleaned_url)
    except Exception:
        is_split_error = True

    if is_split_error or raw_split is None:
        raise DatabaseSafetyError(
            "Database URL is malformed and cannot be parsed."
        )

    if raw_split.fragment:
        raise DatabaseSafetyError(
            "Database URL contains an unexpected fragment and cannot be "
            "validated safely."
        )

    # Reject multi-host connection URLs (e.g. host1,host2:5432 or host1:5432,host2:5433)
    if "," in raw_split.netloc:
        raise DatabaseSafetyError(
            "Multi-host database URLs are ambiguous and forbidden for test safety."
        )

    # Reject target-overriding query parameters from raw query string
    is_query_error = False
    query_params: list[tuple[str, str]] = []
    try:
        query_params = parse_qsl(raw_split.query, keep_blank_values=True)
    except Exception:
        is_query_error = True

    if is_query_error:
        raise DatabaseSafetyError(
            "Database URL is malformed and cannot be parsed."
        )

    for param_name, _ in query_params:
        normalized_param = param_name.strip().lower()
        if normalized_param in _FORBIDDEN_TARGET_PARAMS:
            raise DatabaseSafetyError(
                f"Target-overriding query parameter '{normalized_param}' is forbidden "
                "in database URLs for test safety."
            )

    is_make_url_error = False
    parsed = None
    try:
        parsed = make_url(cleaned_url)
    except Exception:
        is_make_url_error = True

    if is_make_url_error or parsed is None:
        raise DatabaseSafetyError(
            "Database URL is malformed and cannot be parsed."
        )

    # Secondary check on SQLAlchemy's parsed query dict
    for param_key in parsed.query:
        normalized_key = str(param_key).strip().lower()
        if normalized_key in _FORBIDDEN_TARGET_PARAMS:
            raise DatabaseSafetyError(
                f"Target-overriding query parameter '{normalized_key}' is forbidden "
                "in database URLs for test safety."
            )

    backend = (parsed.get_backend_name() or "").lower()
    if not backend:
        raise DatabaseSafetyError("Database URL has no identifiable database dialect.")
    if backend != "postgresql":
        raise DatabaseSafetyError(
            "Database URL must specify a PostgreSQL dialect."
        )

    # Validate explicit host with canonical IP/hostname normalization
    normalized_host = canonicalize_host(parsed.host)

    # Validate explicit port (never default to 5432 when omitted)
    if parsed.port is None:
        raise DatabaseSafetyError(
            "Database URL must explicitly specify a port number."
        )
    if not isinstance(parsed.port, int) or not (1 <= parsed.port <= 65535):
        raise DatabaseSafetyError("Database URL specifies an invalid port number.")
    port = parsed.port

    # Normalize database name
    database = (parsed.database or "").strip().lstrip("/")
    if not database:
        raise DatabaseSafetyError("Database URL must specify a database name.")

    return DatabaseIdentity(
        backend=backend,
        host=normalized_host,
        port=port,
        database=database,
    )


def resolve_application_database_url(project_root: Path | None = None) -> str:
    """Resolve the application's configured DATABASE_URL before test overrides.

    Checks:
    1. The process environment variable DATABASE_URL.
    2. The project's .env file parsed via dotenv_values (compatible with
       pydantic-settings), without importing app.database or initializing engines.

    Does not print the resolved URL.
    """
    ambient = os.environ.get("DATABASE_URL", "").strip()
    if ambient:
        return ambient

    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent

    env_path = project_root / ".env"
    if env_path.is_file():
        try:
            values = dotenv_values(env_path, encoding="utf-8")
            # pydantic-settings matches fields case-insensitively by default
            for key, val in values.items():
                if key and key.upper() == "DATABASE_URL" and val:
                    val_str = str(val).strip()
                    if val_str:
                        return val_str
        except Exception:  # noqa: S110
            pass

    return ""


def validate_test_database_safety(
    *,
    test_url: str | None,
    app_url: str,
    opt_in: str | None,
) -> str:
    """Fail-closed validation for PostgreSQL integration test database selection.

    Validates that:
    1. If test_url is unset (None) or empty (""), returns "" cleanly
       (integration tests will skip).
    2. test_url must not contain leading, trailing, or only whitespace.
    3. Destructive opt-in is active (ALLOW_DESTRUCTIVE_POSTGRES_TESTS=true).
    4. Ambient environment contains no target-altering PG* variables.
    5. test_url uses a PostgreSQL dialect and explicitly specifies host and port.
    6. Application URL can be resolved from environment or .env.
    7. test_url and app_url do not resolve to the same database identity.

    Returns the validated test URL string on success.
    Raises DatabaseSafetyError on any violation without leaking credentials.
    """
    if test_url is None or test_url == "":
        # Genuinely unset or empty: integration tests skip cleanly.
        return ""

    if not isinstance(test_url, str):
        raise DatabaseSafetyError("TEST_DATABASE_URL must be a string.")

    if not test_url.strip():
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL cannot be a whitespace-only string."
        )

    if test_url != test_url.strip():
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL contains forbidden leading or trailing whitespace."
        )

    # 1. Require explicit destructive test opt-in
    normalized_opt_in = (opt_in or "").strip().lower()
    if normalized_opt_in not in ("true", "1", "yes"):
        raise DatabaseSafetyError(
            f"Destructive PostgreSQL integration tests require explicit opt-in. "
            f"Set {DESTRUCTIVE_OPT_IN_ENV_VAR}={DESTRUCTIVE_OPT_IN_REQUIRED_VALUE} "
            f"in your environment to proceed."
        )

    # 2. Check ambient environment for target-altering PG* variables
    check_environment_database_safety()

    # 3. Verify dialect is PostgreSQL before detailed target parsing
    is_dialect_err = False
    test_backend = ""
    try:
        test_backend = (make_url(test_url).get_backend_name() or "").lower()
    except Exception:
        is_dialect_err = True

    if is_dialect_err:
        raise DatabaseSafetyError(
            "Database URL is malformed and cannot be parsed."
        )

    if test_backend != "postgresql":
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL must be a PostgreSQL connection string "
            "(postgresql+psycopg://...). Non-PostgreSQL dialects are rejected."
        )

    test_identity = normalize_database_url(test_url)

    # 4. Resolve and validate application database identity
    if not app_url or not app_url.strip():
        raise DatabaseSafetyError(
            "Application database configuration (DATABASE_URL) could not be "
            "resolved from environment or .env for safety comparison. "
            "Refusing to execute tests against unverified target."
        )

    is_app_dialect_err = False
    app_backend = ""
    try:
        app_backend = (make_url(app_url.strip()).get_backend_name() or "").lower()
    except Exception:
        is_app_dialect_err = True

    if is_app_dialect_err:
        raise DatabaseSafetyError(
            "Database URL is malformed and cannot be parsed."
        )

    if app_backend != "postgresql":
        raise DatabaseSafetyError(
            "Application DATABASE_URL must be a PostgreSQL connection string."
        )

    app_identity = normalize_database_url(app_url)

    # 5. Identity collision check
    if (
        test_identity.backend == app_identity.backend
        and test_identity.host == app_identity.host
        and test_identity.port == app_identity.port
        and test_identity.database == app_identity.database
    ):
        raise DatabaseSafetyError(
            "SAFETY VIOLATION: TEST_DATABASE_URL targets the same database instance "
            "and name as the application's configured DATABASE_URL. "
            "Destructive test execution aborted to protect development data."
        )

    return test_url


def bootstrap_test_database_environment(
    raw_test_url: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Bootstrap and validate the test database environment before test collection.

    If raw_test_url is unset (None) or empty (""), returns "" cleanly so that
    PostgreSQL integration tests are skipped.

    If raw_test_url is whitespace-only, has leading/trailing whitespace, or
    fails any safety guard, raises DatabaseSafetyError to abort collection
    before any application database engine or test suite initializes.

    When valid, synchronizes os.environ["DATABASE_URL"] and
    os.environ["TEST_DATABASE_URL"] with the validated URL and returns it.
    """
    if raw_test_url is None:
        raw_test_url = os.environ.get("TEST_DATABASE_URL", None)

    if raw_test_url is None or raw_test_url == "":
        # Genuinely unset or empty: integration tests skip cleanly.
        return ""

    if not isinstance(raw_test_url, str):
        raise DatabaseSafetyError("TEST_DATABASE_URL must be a string.")

    if not raw_test_url.strip():
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL cannot be a whitespace-only string."
        )

    if raw_test_url != raw_test_url.strip():
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL contains forbidden leading or trailing whitespace."
        )

    app_db_url = resolve_application_database_url(project_root)
    opt_in = os.environ.get(DESTRUCTIVE_OPT_IN_ENV_VAR)

    validated_url = validate_test_database_safety(
        test_url=raw_test_url,
        app_url=app_db_url,
        opt_in=opt_in,
    )

    os.environ["DATABASE_URL"] = validated_url
    os.environ["TEST_DATABASE_URL"] = validated_url
    return validated_url
