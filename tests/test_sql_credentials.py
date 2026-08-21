"""Credential-bearing SQL is refused before it runs, and the refusal is narrow.

`run_sql` records its query verbatim in a manifest people are invited to share,
so a credential in the statement is a credential in a published artifact
(issue #18). The first fix was to redact the text; an adversarial audit escaped
it four ways in minutes, one of which redacted the *wrong* argument and kept the
secret. So the statement is refused instead, and redaction stays as a second
layer for the things that reach a manifest without being SQL.

Two halves matter equally here. Refusing the credential statements is the
obvious one. Not refusing honest work is the one that decides whether this
policy survives contact with users: a false positive here does not mangle a
record, it blocks the query.
"""

import pytest

from mapsmith import sql_policy

# Each entry is a real syntax from the audit or from DuckDB's own documentation.
MUST_REFUSE = [
    # the one secret type MapSmith's sandbox can actually construct
    "CREATE SECRET t (TYPE http, EXTRA_HTTP_HEADERS MAP{'Authorization': 'Bearer X'})",
    "CREATE SECRET s (TYPE S3, KEY_ID 'AKIA1', SECRET 'shh')",
    "CREATE OR REPLACE PERSISTENT SECRET s (TYPE S3, SECRET 'shh')",
    "CREATE TEMPORARY SECRET s (TYPE S3, SECRET 'shh')",
    # a comment cannot split the keywords: the audit used exactly this trick
    # against the redaction matcher
    "create /* sneak */ secret s (TYPE S3)",
    "CREATE\n\tSECRET s (TYPE S3)",
    # credential settings, in DuckDB's several spellings
    "SET s3_secret_access_key = 'x'",
    "SET s3_access_key_id='AKIA1'",
    "SET GLOBAL s3_session_token = 'x'",
    "PRAGMA http_proxy_password='x'",
    # ATTACH carrying credentials as an option or as URI userinfo
    "ATTACH 'dbname=x password=shh' AS pg (TYPE postgres)",
    "ATTACH 'postgres://user:shh@host/db' AS pg",
]

# Ordinary work. If any of these ever starts raising, the policy is broken in
# the direction users notice.
MUST_ALLOW = [
    "SELECT * FROM read_parquet('roads.parquet') WHERE name = 'via Roma'",
    "SELECT count(*) FROM ST_Read('zones.gpkg')",
    "CREATE TABLE joined AS SELECT * FROM a JOIN b ON a.id = b.id",
    "SET memory_limit = '2GB'",
    "SET threads = 4",
    # columns and tables whose names merely read like credentials
    "SELECT secret_count, token_type FROM audit_log",
    "SELECT * FROM secrets_inventory WHERE password_age > 90",
    # the word in a string literal, not in a credential position
    "INSERT INTO notes VALUES ('remember the password rotation')",
    # DuckDB's own way of reading credentials from the operator's environment:
    # nothing secret appears in the statement, which is the whole point
    "CALL load_aws_credentials()",
]


@pytest.mark.parametrize("query", MUST_REFUSE)
def test_credential_statements_are_refused(query):
    with pytest.raises(ValueError) as excinfo:
        sql_policy.refuse_credentials_in_sql(query)
    message = str(excinfo.value)
    # the message has to say what to do instead, or the agent retries the same
    # statement and the user sees a loop
    assert "outside the agent's reach" in message
    assert "refuses" in message


@pytest.mark.parametrize("query", MUST_ALLOW)
def test_honest_queries_are_not_refused(query):
    sql_policy.refuse_credentials_in_sql(query)


def test_comments_cannot_hide_a_keyword():
    """Comment stripping is what makes the keyword match trustworthy."""
    assert sql_policy.strip_comments("CREATE /* x */ SECRET s") == "CREATE   SECRET s"
    assert sql_policy.strip_comments("SELECT 1 -- CREATE SECRET\nFROM t") == "SELECT 1  \nFROM t"


def test_the_refusal_names_the_offending_fragment():
    """The message quotes what was matched, so a false positive is diagnosable
    from the error alone rather than by reading this module."""
    with pytest.raises(ValueError, match="CREATE SECRET"):
        sql_policy.refuse_credentials_in_sql("CREATE SECRET s (TYPE S3)")


def test_run_sql_refuses_before_touching_the_engine(tmp_path, monkeypatch):
    """The check must sit in front of execution, not after it: a secret that is
    created and then reported as an error is still a created secret."""
    pytest.importorskip("duckdb")
    from mapsmith.engines import duckdb_engine

    monkeypatch.setattr(
        duckdb_engine, "_connect", lambda: pytest.fail("the engine was reached")
    )
    with pytest.raises(ValueError, match="refuses"):
        duckdb_engine.run_sql(
            "CREATE SECRET t (TYPE http, EXTRA_HTTP_HEADERS MAP{'Authorization': 'Bearer X'})"
        )


def test_persistent_secrets_are_disabled_in_the_connection():
    """Defence in depth for the same leak by a different route: a persistent
    secret is written to ~/.duckdb/stored_secrets — outside any workspace and
    beyond the session. Under a workspace the jail already refuses that write;
    unconfined mode would not, so the setting is applied in both."""
    duckdb = pytest.importorskip("duckdb")
    from mapsmith.engines import duckdb_engine

    con = duckdb_engine._connect()
    assert con.sql("SELECT current_setting('allow_persistent_secrets')").fetchone()[0] is False
    assert con.sql("SELECT current_setting('allow_unredacted_secrets')").fetchone()[0] is False
    # and the configuration lock makes it a policy rather than a default
    with pytest.raises(duckdb.Error):
        con.execute("SET allow_persistent_secrets = true")
