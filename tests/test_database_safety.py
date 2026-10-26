"""Unit tests for the PostgreSQL test database safety guard.

Pure in-memory unit tests: zero database connections, zero network requests.
Uses synthetic URLs only; never references real credentials or environments.
"""

from __future__ import annotations

import os
import traceback

import pytest

from tests.database_safety import (
    DESTRUCTIVE_OPT_IN_ENV_VAR,
    DESTRUCTIVE_OPT_IN_REQUIRED_VALUE,
    FORBIDDEN_PG_ENV_VARS,
    DatabaseSafetyError,
    bootstrap_test_database_environment,
    canonicalize_host,
    check_environment_database_safety,
    normalize_database_url,
    resolve_application_database_url,
    validate_test_database_safety,
)

# Synthetic URLs for testing (no real credentials, no live hosts)
SYNTHETIC_APP_URL = (
    "postgresql+psycopg://app_user:app_secret_pass@db.example.internal:5432"
    "/app_production"
)
SYNTHETIC_TEST_URL = (
    "postgresql+psycopg://test_user:test_secret_pass@test-db.example.internal:5432"
    "/test_database"
)


class TestDatabaseSafetyHostCanonicalization:
    """Tests for canonicalize_host IP normalization and loopback collision checks."""

    def test_canonicalizes_localhost_and_local(self):
        for name in ("localhost", "localhost.", "LOCALHOST", "local", "LOCAL."):
            assert canonicalize_host(name) == "localhost"

    def test_canonicalizes_ipv4_loopback_range(self):
        # The entire 127.0.0.0/8 range represents local loopback
        loopback_ips = [
            "127.0.0.1",
            "127.0.0.2",
            "127.1.2.3",
            "127.255.255.254",
        ]
        for ip in loopback_ips:
            assert canonicalize_host(ip) == "localhost"

    def test_canonicalizes_ipv6_compressed_and_expanded_loopback(self):
        # Compressed and expanded IPv6 loopback variants
        ipv6_loopbacks = [
            "::1",
            "[::1]",
            "0:0:0:0:0:0:0:1",
            "[0:0:0:0:0:0:0:1]",
            "0000:0000:0000:0000:0000:0000:0000:0001",
            "[0000:0000:0000:0000:0000:0000:0000:0001]",
        ]
        for ip in ipv6_loopbacks:
            assert canonicalize_host(ip) == "localhost"

    def test_canonicalizes_ipv4_mapped_ipv6_loopback_and_casing(self):
        # IPv4-mapped loopback addresses with mixed casing
        mapped_loopbacks = [
            "::ffff:127.0.0.1",
            "[::ffff:127.0.0.1]",
            "[::FFFF:127.0.0.1]",
            "[::ffff:127.0.0.2]",
            "[0:0:0:0:0:ffff:127.0.0.1]",
        ]
        for ip in mapped_loopbacks:
            assert canonicalize_host(ip) == "localhost"

    def test_canonicalizes_unspecified_local_bind_addresses(self):
        # INADDR_ANY / in6addr_any route to local machine interfaces
        for unspecified in ("0.0.0.0", "::", "[::]"):  # noqa: S104
            assert canonicalize_host(unspecified) == "localhost"

    def test_canonicalizes_external_ip_addresses(self):
        assert canonicalize_host("192.168.1.1") == "192.168.1.1"
        assert (
            canonicalize_host("2001:0db8:0000:0000:0000:0000:0000:0001")
            == "2001:db8::1"
        )
        assert canonicalize_host("[2001:db8::1]") == "2001:db8::1"

    def test_canonicalizes_standard_hostnames(self):
        assert canonicalize_host("db.example.internal") == "db.example.internal"
        assert canonicalize_host("DB.Example.Internal.") == "db.example.internal"

    def test_rejects_ambiguous_or_invalid_host_formats(self):
        invalid_hosts = [
            "127.000.000.001",  # Leading zero IPv4 (CVE-2021-29921 ambiguity)
            "999.999.999.999",  # Out-of-range numeric IPv4
            "host:bad",  # Unbracketed colon in hostname
            "host[bracket]",  # Embedded bracket
            "",  # Empty
        ]
        for bad_host in invalid_hosts:
            with pytest.raises(DatabaseSafetyError):
                canonicalize_host(bad_host)


class TestDatabaseSafetyNormalization:
    """Tests for database URL normalization and identity extraction."""

    def test_normalizes_standard_postgres_url(self):
        identity = normalize_database_url(SYNTHETIC_APP_URL)
        assert identity.backend == "postgresql"
        assert identity.host == "db.example.internal"
        assert identity.port == 5432
        assert identity.database == "app_production"

    def test_normalizes_localhost_aliases(self):
        for alias in (
            "localhost",
            "127.0.0.1",
            "127.0.0.2",
            "[::1]",
            "[0:0:0:0:0:0:0:1]",
            "0.0.0.0",  # noqa: S104
        ):
            url = f"postgresql+psycopg://user:pass@{alias}:5432/mydb"
            identity = normalize_database_url(url)
            assert identity.host == "localhost"

    def test_normalizes_hostname_casing_and_trailing_dots(self):
        url = "postgresql+psycopg://user:pass@DB.Example.Internal.:5432/mydb"
        identity = normalize_database_url(url)
        assert identity.host == "db.example.internal"

    def test_rejects_empty_or_whitespace_url(self):
        with pytest.raises(DatabaseSafetyError, match="must be a non-empty string"):
            normalize_database_url("")
        with pytest.raises(DatabaseSafetyError, match="whitespace-only string"):
            normalize_database_url("   ")

    def test_rejects_leading_or_trailing_whitespace(self):
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            normalize_database_url("  " + SYNTHETIC_APP_URL)
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            normalize_database_url(SYNTHETIC_APP_URL + "  ")

    def test_rejects_missing_host(self):
        missing_host_urls = [
            "postgresql:///mydb",
            "postgresql://user:pass@/mydb",
            "postgresql+psycopg:///mydb",
            "postgresql+psycopg://user:pass@:5432/mydb",
        ]
        for url in missing_host_urls:
            with pytest.raises(
                DatabaseSafetyError, match="must explicitly specify a hostname"
            ):
                normalize_database_url(url)

    def test_rejects_missing_port(self):
        missing_port_urls = [
            "postgresql+psycopg://user:pass@myhost/mydb",
            "postgresql://user:pass@localhost/mydb",
            "postgresql+psycopg://user:pass@127.0.0.1/mydb",
        ]
        for url in missing_port_urls:
            with pytest.raises(
                DatabaseSafetyError, match="must explicitly specify a port number"
            ):
                normalize_database_url(url)

    def test_rejects_invalid_port(self):
        invalid_port_urls = [
            "postgresql+psycopg://user:pass@myhost:0/mydb",
            "postgresql+psycopg://user:pass@myhost:65536/mydb",
            "postgresql+psycopg://user:pass@myhost:99999/mydb",
        ]
        for url in invalid_port_urls:
            with pytest.raises(
                DatabaseSafetyError, match="specifies an invalid port number"
            ):
                normalize_database_url(url)

    def test_rejects_missing_database_name(self):
        with pytest.raises(DatabaseSafetyError, match="must specify a database name"):
            normalize_database_url("postgresql+psycopg://user:pass@localhost:5432/")

    @pytest.mark.parametrize(
        "forbidden_param",
        [
            "host=other_host",
            "hostaddr=10.0.0.1",
            "port=5433",
            "dbname=other_db",
            "database=other_db",
            "service=my_service",
            "servicefile=/path/to/pg_service.conf",
            "target_session_attrs=primary",
            "load_balance_hosts=random",
            "options=-c%20search_path=schema",
            "dsn=my_dsn",
            "HOST=other_host",
            "HOSTADDR=10.0.0.1",
            "DBNAME=other_db",
            "SERVICE=my_service",
            "service",
        ],
    )
    def test_rejects_target_overriding_query_parameters(self, forbidden_param):
        url = f"postgresql+psycopg://user:pass@localhost:5432/mydb?{forbidden_param}"
        with pytest.raises(
            DatabaseSafetyError, match="Target-overriding query parameter"
        ):
            normalize_database_url(url)

    def test_rejects_multi_host_urls(self):
        multi_hosts = [
            "postgresql+psycopg://user:pass@host1,host2/mydb",
            "postgresql+psycopg://user:pass@host1:5432,host2:5433/mydb",
        ]
        for url in multi_hosts:
            with pytest.raises(DatabaseSafetyError, match="Multi-host database URLs"):
                normalize_database_url(url)

    def test_rejects_url_fragments(self):
        url = "postgresql+psycopg://user:pass@localhost:5432/mydb#target_override"
        with pytest.raises(DatabaseSafetyError, match="unexpected fragment"):
            normalize_database_url(url)

    def test_accepts_safe_operational_query_parameters(self):
        url = (
            "postgresql+psycopg://user:pass@myhost:5432/mydb"
            "?sslmode=require&channel_binding=require&connect_timeout=10"
            "&application_name=revenue_test_suite"
        )
        identity = normalize_database_url(url)
        assert identity.host == "myhost"
        assert identity.port == 5432
        assert identity.database == "mydb"


class TestDatabaseSafetyEnvironment:
    """Tests for PostgreSQL ambient environment safety validation."""

    def test_check_environment_passes_when_clean(self):
        # Empty env mapping passes cleanly
        check_environment_database_safety({})

    @pytest.mark.parametrize("var_name", sorted(FORBIDDEN_PG_ENV_VARS))
    def test_rejects_forbidden_pg_environment_variables(self, var_name, monkeypatch):
        synthetic_val = "synthetic_secret_override_val"
        monkeypatch.setenv(var_name, synthetic_val)

        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )
        err_msg = str(exc_info.value)
        assert "target-altering environment variable(s) detected" in err_msg
        assert var_name in err_msg
        # Value must NEVER appear in the error message
        assert synthetic_val not in err_msg

    def test_pghostaddr_conflicts_with_apparent_hostname(self, monkeypatch):
        """Setting PGHOSTADDR would route connection away from hostname."""
        monkeypatch.setenv("PGHOSTADDR", "10.0.0.99")
        with pytest.raises(DatabaseSafetyError, match="PGHOSTADDR"):
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )

    def test_service_related_environment_configuration(self, monkeypatch):
        """PGSERVICE and PGSERVICEFILE point to out-of-band service definitions."""
        monkeypatch.setenv("PGSERVICE", "my_service_entry")
        with pytest.raises(DatabaseSafetyError, match="PGSERVICE"):
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )

        monkeypatch.delenv("PGSERVICE")
        monkeypatch.setenv("PGSERVICEFILE", "/tmp/custom_pg_service.conf")  # noqa: S108
        with pytest.raises(DatabaseSafetyError, match="PGSERVICEFILE"):
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )

    def test_schema_session_affecting_pgoptions(self, monkeypatch):
        """PGOPTIONS alters backend search_path, transaction mode, etc."""
        monkeypatch.setenv("PGOPTIONS", "-c search_path=dev,public")
        with pytest.raises(DatabaseSafetyError, match="PGOPTIONS"):
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )


class TestDatabaseSafetyValidation:
    """Tests for the fail-closed validate_test_database_safety guard."""

    def test_unset_or_empty_test_url_is_noop(self):
        # When TEST_DATABASE_URL is absent/empty, integration tests are skipped;
        # the guard must return cleanly with empty string.
        assert (
            validate_test_database_safety(
                test_url="", app_url=SYNTHETIC_APP_URL, opt_in=None
            )
            == ""
        )
        assert (
            validate_test_database_safety(
                test_url=None, app_url=SYNTHETIC_APP_URL, opt_in=None
            )
            == ""
        )

    def test_rejects_whitespace_only_test_url(self):
        for ws in (" ", "   ", "\t", "\n", " \t \n "):
            with pytest.raises(DatabaseSafetyError, match="whitespace-only string"):
                validate_test_database_safety(
                    test_url=ws,
                    app_url=SYNTHETIC_APP_URL,
                    opt_in="true",
                )

    def test_rejects_leading_or_trailing_whitespace_test_url(self):
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            validate_test_database_safety(
                test_url="  " + SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL + "  ",
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )

    def test_rejects_when_destructive_opt_in_missing(self):
        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in=None,
            )
        err_msg = str(exc_info.value)
        assert DESTRUCTIVE_OPT_IN_ENV_VAR in err_msg
        assert DESTRUCTIVE_OPT_IN_REQUIRED_VALUE in err_msg

    def test_rejects_when_destructive_opt_in_invalid(self):
        for invalid_opt in ("false", "0", "no", "random_string"):
            with pytest.raises(DatabaseSafetyError) as exc_info:
                validate_test_database_safety(
                    test_url=SYNTHETIC_TEST_URL,
                    app_url=SYNTHETIC_APP_URL,
                    opt_in=invalid_opt,
                )
            assert "require explicit opt-in" in str(exc_info.value)

    def test_accepts_valid_destructive_opt_in_values(self):
        for valid_opt in ("true", "TRUE", "True", "1", "yes", "YES"):
            res = validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in=valid_opt,
            )
            assert res == SYNTHETIC_TEST_URL

    def test_rejects_non_postgresql_test_url(self):
        non_pg_urls = [
            "sqlite:///test.db",
            "sqlite:///:memory:",
            "mysql+pymysql://user:pass@host:3306/test_db",
            "oracle+cx_oracle://user:pass@host:1521/test_db",
        ]
        for url in non_pg_urls:
            with pytest.raises(DatabaseSafetyError) as exc_info:
                validate_test_database_safety(
                    test_url=url,
                    app_url=SYNTHETIC_APP_URL,
                    opt_in="true",
                )
            assert "must be a PostgreSQL connection string" in str(exc_info.value)

    def test_rejects_identical_app_and_test_database(self):
        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=SYNTHETIC_APP_URL,
                app_url=SYNTHETIC_APP_URL,
                opt_in="true",
            )
        assert "SAFETY VIOLATION" in str(exc_info.value)
        assert "targets the same database instance" in str(exc_info.value)

    def test_rejects_equivalent_targets_differing_only_by_creds_or_params(self):
        app_url = (
            "postgresql+psycopg://app_user:secret_pass1"
            "@ep-cluster.neon.tech:5432/main_db?sslmode=require"
        )
        test_url = (
            "postgresql+psycopg2://admin:other_secret_pass2"
            "@ep-cluster.neon.tech:5432/main_db"
            "?sslmode=verify-full&channel_binding=require"
        )

        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=test_url,
                app_url=app_url,
                opt_in="true",
            )
        assert "SAFETY VIOLATION" in str(exc_info.value)

    def test_rejects_equivalent_loopback_aliases_across_ipv4_and_ipv6(self):
        """All loopback variants (127.0.0.0/8, ::1, mapped) collide with localhost."""
        app_url = "postgresql+psycopg://user:pass@localhost:5432/shared_db"

        loopback_aliases = [
            "127.0.0.1",
            "127.0.0.2",
            "127.1.2.3",
            "[::1]",
            "[0:0:0:0:0:0:0:1]",
            "[0000:0000:0000:0000:0000:0000:0000:0001]",
            "[::ffff:127.0.0.1]",
            "[::FFFF:127.0.0.1]",
            "[::ffff:127.0.0.2]",
            "0.0.0.0",  # noqa: S104
            "[::]",
        ]

        for alias in loopback_aliases:
            test_url = f"postgresql+psycopg://user:pass@{alias}:5432/shared_db"
            with pytest.raises(DatabaseSafetyError) as exc_info:
                validate_test_database_safety(
                    test_url=test_url,
                    app_url=app_url,
                    opt_in="true",
                )
            assert "SAFETY VIOLATION" in str(exc_info.value)

    def test_allows_different_database_name_or_port_on_loopback(self):
        # Distinct database name on same loopback host -> Allowed
        validate_test_database_safety(
            test_url="postgresql+psycopg://user:pass@[::1]:5432/test_db",
            app_url="postgresql+psycopg://user:pass@localhost:5432/app_db",
            opt_in="true",
        )
        # Distinct port on loopback host -> Allowed
        validate_test_database_safety(
            test_url="postgresql+psycopg://user:pass@127.0.0.2:5433/shared_db",
            app_url="postgresql+psycopg://user:pass@localhost:5432/shared_db",
            opt_in="true",
        )

    def test_fails_closed_when_app_url_is_missing(self):
        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=SYNTHETIC_TEST_URL,
                app_url="",
                opt_in="true",
            )
        assert "could not be resolved" in str(exc_info.value)

    def test_allows_distinct_postgresql_target_with_opt_in(self):
        # Distinct host -> Allowed
        validate_test_database_safety(
            test_url="postgresql+psycopg://u:p@ep-test-branch.neon.tech:5432/neondb",
            app_url="postgresql+psycopg://u:p@ep-main-branch.neon.tech:5432/neondb",
            opt_in="true",
        )
        # Distinct database name on same host -> Allowed
        validate_test_database_safety(
            test_url="postgresql+psycopg://u:p@ep-branch.neon.tech:5432/test_db",
            app_url="postgresql+psycopg://u:p@ep-branch.neon.tech:5432/dev_db",
            opt_in="true",
        )
        # Distinct port -> Allowed
        validate_test_database_safety(
            test_url="postgresql+psycopg://u:p@localhost:5433/neondb",
            app_url="postgresql+psycopg://u:p@localhost:5432/neondb",
            opt_in="true",
        )

    def test_error_messages_never_leak_credentials(self):
        sensitive_pass = "SUPER_SECRET_COMPLEX_PASSWORD_999"  # noqa: S105
        sensitive_user = "CLASSIFIED_USERNAME_XYZ"
        sensitive_host = "secret-internal-db.corp.net"

        url_with_secrets = (
            f"postgresql+psycopg://{sensitive_user}:{sensitive_pass}"
            f"@{sensitive_host}:5432/colliding_db"
        )

        with pytest.raises(DatabaseSafetyError) as exc_info:
            validate_test_database_safety(
                test_url=url_with_secrets,
                app_url=url_with_secrets,
                opt_in="true",
            )

        err_text = str(exc_info.value)
        assert sensitive_pass not in err_text
        assert sensitive_user not in err_text
        assert sensitive_host not in err_text
        assert "colliding_db" not in err_text

    def test_malformed_url_handling_without_credential_leakage_in_traceback(self):
        """Malformed URL errors must not leak credentials into formatted traceback."""
        sensitive_pass = "CONFIDENTIAL_PASSWORD_LEAK_TEST_777"  # noqa: S105
        sensitive_user = "CONFIDENTIAL_USER_LEAK_TEST_888"

        malformed_urls = [
            f"postgresql+psycopg://{sensitive_user}:{sensitive_pass}@[invalid-ipv6/db",
            f"postgresql+psycopg://{sensitive_user}:{sensitive_pass}@host:notaport/db",
        ]

        for malformed in malformed_urls:
            with pytest.raises(DatabaseSafetyError) as exc_info:
                normalize_database_url(malformed)

            err_text = str(exc_info.value)
            formatted_tb = "".join(traceback.format_exception(exc_info.value))

            assert sensitive_pass not in err_text
            assert sensitive_user not in err_text
            assert sensitive_pass not in formatted_tb
            assert sensitive_user not in formatted_tb
            assert exc_info.value.__cause__ is None
            assert exc_info.value.__context__ is None

    def test_target_overriding_query_param_cannot_bypass_safety_guard(self):
        """Regression test: query params cannot bypass identity comparison."""
        app_url = "postgresql+psycopg://app_user:pass@ep-prod.neon.tech:5432/prod_db"
        # Test URL targets ep-test.neon.tech in netloc, but overrides host via query
        test_url_host_bypass = (
            "postgresql+psycopg://test_user:pass@ep-test.neon.tech:5432/test_db"
            "?host=ep-prod.neon.tech"
        )
        match_msg = "Target-overriding query parameter"
        with pytest.raises(DatabaseSafetyError, match=match_msg):
            validate_test_database_safety(
                test_url=test_url_host_bypass,
                app_url=app_url,
                opt_in="true",
            )

        # Test URL attempts redirect to prod_db via dbname query param
        test_url_db_bypass = (
            "postgresql+psycopg://test_user:pass@ep-test.neon.tech:5432/test_db"
            "?dbname=prod_db"
        )
        with pytest.raises(DatabaseSafetyError, match=match_msg):
            validate_test_database_safety(
                test_url=test_url_db_bypass,
                app_url=app_url,
                opt_in="true",
            )

    def test_application_url_with_target_overriding_query_param_fails_closed(self):
        """If application target URL has an ambiguous override, fail closed."""
        app_url_with_override = (
            "postgresql+psycopg://app_user:pass@ep-prod.neon.tech:5432/prod_db"
            "?service=production_service"
        )
        test_url = "postgresql+psycopg://test_user:pass@ep-test.neon.tech:5432/test_db"

        match_msg = "Target-overriding query parameter"
        with pytest.raises(DatabaseSafetyError, match=match_msg):
            validate_test_database_safety(
                test_url=test_url,
                app_url=app_url_with_override,
                opt_in="true",
            )


class TestDatabaseSafetyBootstrap:
    """Tests for bootstrap_test_database_environment behavior in conftest."""

    def test_bootstrap_unset_test_url_returns_empty_and_does_not_mutate_env(
        self, monkeypatch
    ):
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        res = bootstrap_test_database_environment(None)
        assert res == ""
        assert "TEST_DATABASE_URL" not in os.environ
        assert "DATABASE_URL" not in os.environ

    def test_bootstrap_empty_test_url_returns_empty(self, monkeypatch):
        monkeypatch.setenv("TEST_DATABASE_URL", "")
        res = bootstrap_test_database_environment("")
        assert res == ""

    def test_bootstrap_whitespace_only_test_url_fails_closed(self):
        for ws in (" ", "   ", "\t", "\n", "  \t\n  "):
            with pytest.raises(DatabaseSafetyError, match="whitespace-only string"):
                bootstrap_test_database_environment(ws)

    def test_bootstrap_leading_whitespace_test_url_fails_closed(self):
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            bootstrap_test_database_environment("  " + SYNTHETIC_TEST_URL)

    def test_bootstrap_trailing_whitespace_test_url_fails_closed(self):
        with pytest.raises(
            DatabaseSafetyError, match="leading or trailing whitespace"
        ):
            bootstrap_test_database_environment(SYNTHETIC_TEST_URL + "  ")

    def test_bootstrap_malformed_url_cannot_run_tests_without_passing_guard(
        self, monkeypatch
    ):
        malformed_urls = [
            "sqlite:///test.db",
            "not_a_valid_url",
            "postgresql+psycopg://user:pass@host/mydb",  # missing port
            "postgresql+psycopg:///mydb",  # missing host
        ]
        monkeypatch.setenv(
            DESTRUCTIVE_OPT_IN_ENV_VAR, DESTRUCTIVE_OPT_IN_REQUIRED_VALUE
        )
        monkeypatch.setenv("DATABASE_URL", SYNTHETIC_APP_URL)

        for bad_url in malformed_urls:
            with pytest.raises(DatabaseSafetyError):
                bootstrap_test_database_environment(bad_url)

    def test_bootstrap_valid_url_synchronizes_environ_and_returns_validated(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            DESTRUCTIVE_OPT_IN_ENV_VAR, DESTRUCTIVE_OPT_IN_REQUIRED_VALUE
        )
        monkeypatch.setenv("DATABASE_URL", SYNTHETIC_APP_URL)

        res = bootstrap_test_database_environment(SYNTHETIC_TEST_URL)
        assert res == SYNTHETIC_TEST_URL
        assert os.environ["DATABASE_URL"] == SYNTHETIC_TEST_URL
        assert os.environ["TEST_DATABASE_URL"] == SYNTHETIC_TEST_URL


class TestResolveApplicationDatabaseUrl:
    """Tests for resolving the application DATABASE_URL before overrides."""

    def test_resolves_from_ambient_environment(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", SYNTHETIC_APP_URL)
        resolved = resolve_application_database_url()
        assert resolved == SYNTHETIC_APP_URL

    def test_resolves_from_env_file_using_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(f"DATABASE_URL={SYNTHETIC_APP_URL}\nAPP_ENV=development\n")
        resolved = resolve_application_database_url(tmp_path)
        assert resolved == SYNTHETIC_APP_URL

    def test_resolves_from_env_file_with_quotes_and_trailing_comments(
        self, tmp_path, monkeypatch
    ):
        """pydantic-settings / python-dotenv properly handles quotes and comments."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            f'DATABASE_URL="{SYNTHETIC_APP_URL}" # trailing comment\n'
            "APP_ENV=development\n"
        )
        resolved = resolve_application_database_url(tmp_path)
        assert resolved == SYNTHETIC_APP_URL

    def test_resolves_from_env_file_case_insensitive(self, tmp_path, monkeypatch):
        """pydantic-settings matches environment keys case-insensitively."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(f"database_url={SYNTHETIC_APP_URL}\n")
        resolved = resolve_application_database_url(tmp_path)
        assert resolved == SYNTHETIC_APP_URL

    def test_returns_empty_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        resolved = resolve_application_database_url(tmp_path)
        assert resolved == ""
