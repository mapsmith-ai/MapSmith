"""Credentials must never reach a provenance manifest (issue #18).

The manifest is the artifact this project tells people to share — attach it to a
review, a bug report, a paper. `run_sql` takes arbitrary SQL written by an
agent, so a `CREATE SECRET` in the same session would otherwise be copied
verbatim into that artifact.

Every case below is a real syntax from the engines MapSmith touches, and each
asserts both halves: the secret is gone *and* the detail that says which
statement ran is still there. A redaction that blanks the whole query would
pass a leak test and destroy the audit trail.
"""

import json

import pytest

from mapsmith.provenance import REDACTED, ProvenanceRecord, redact_secrets


@pytest.mark.parametrize(
    ("sql", "secret", "keep"),
    [
        (
            ("CREATE SECRET s3 (TYPE S3, KEY_ID 'AKIAIOSFODNN7EXAMPLE', "
             "SECRET 'wJalrXUtnFEMI/K7MDENG')"),
            "AKIAIOSFODNN7EXAMPLE",
            "CREATE SECRET s3",  # the secret's NAME is not a credential
        ),
        (
            "CREATE PERSISTENT SECRET gcs_bucket (TYPE gcs, KEY_ID 'k', SECRET 'sup3r')",
            "sup3r",
            "gcs_bucket",
        ),
        # the name can be the suffix of a longer identifier: a plain word
        # boundary misses it, because an underscore is a word character
        ("SET s3_secret_access_key='abc123'", "abc123", "s3_secret_access_key"),
        ("SET s3_access_key_id=AKIA123", "AKIA123", "s3_access_key_id"),
        (
            "ATTACH 'dbname=geo' AS pg (TYPE postgres, PASSWORD 'hunter2')",
            "hunter2",
            "AS pg",
        ),
        (
            "SELECT * FROM read_parquet('postgresql://mario:hunter2@db.example.com/geo')",
            "hunter2",
            "mario",  # the user survives, the password does not
        ),
        ("SELECT * FROM t WHERE api_key = 'live_abc'", "live_abc", "api_key"),
    ],
)
def test_credentials_are_masked_and_the_statement_stays_readable(sql, secret, keep):
    out = redact_secrets(sql)
    assert secret not in out, out
    assert keep in out, out
    assert REDACTED in out


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM read_parquet('data/wells.parquet') WHERE depth > 100",
        # columns that merely START with a sensitive word are not credentials
        "SELECT secret_count, token_type FROM stats",
        "SELECT ST_Area(geom) FROM zones",
    ],
)
def test_ordinary_queries_are_left_alone(sql):
    """Over-redaction would quietly rewrite the audit trail of honest runs."""
    assert redact_secrets(sql) == sql


def test_every_record_is_redacted_without_the_engine_asking():
    """The guarantee is on ProvenanceRecord, not on its callers: a redaction a
    future engine can forget to call is not a redaction."""
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={"query": "CREATE SECRET s (TYPE S3, SECRET 'leaked')"},
        inputs=[],
    )
    assert "leaked" not in json.dumps(record.parameters)
    assert record.parameters_redacted is True


def test_the_manifest_says_that_it_was_redacted():
    """A manifest that silently differs from what executed would be a worse bug
    than the leak it prevents."""
    clean = ProvenanceRecord(
        operation="buffer_layer", parameters={"distance_meters": 500}, inputs=[]
    )
    assert clean.parameters_redacted is False
    assert clean.parameters == {"distance_meters": 500}


def test_redaction_reaches_nested_parameters():
    record = ProvenanceRecord(
        operation="run_plan",
        parameters={"steps": [{"sql": "SET s3_secret_access_key='abc123'"}]},
        inputs=[],
    )
    assert "abc123" not in json.dumps(record.parameters)
    assert record.parameters_redacted is True


def test_written_manifest_carries_no_secret(tmp_path):
    """End to end: what lands on disk is what gets shared."""
    target = tmp_path / "out.parquet"
    target.write_bytes(b"")
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={"query": "ATTACH 'x' AS pg (TYPE postgres, PASSWORD 'hunter2')"},
        inputs=[],
    )
    manifest = record.finish().write_for(target)
    text = manifest.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "AS pg" in text
    assert json.loads(text)["parameters_redacted"] is True
