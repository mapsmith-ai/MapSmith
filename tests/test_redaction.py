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
        # --- the four syntaxes an adversarial audit used to walk past the first
        # version of this matcher. Each one leaked in 0.2.1.
        (
            "CREATE SECRET t (TYPE http, EXTRA_HTTP_HEADERS MAP{'Authorization': 'Bearer TOK'})",
            "TOK",
            "EXTRA_HTTP_HEADERS",  # the credential is a quoted KEY, not an identifier
        ),
        # E'...' desynchronised the quote pairing, so the old matcher redacted a
        # DIFFERENT argument and kept the secret: wrong *and* leaky
        (r"CREATE SECRET s (TYPE S3, KEY_ID 'AKIA1', SECRET E'shh\'x')", "shh", "KEY_ID"),
        ("CREATE SECRET s (TYPE S3, SECRET $$dollar$$)", "dollar", "TYPE S3"),
        ("CREATE SECRET s (TYPE S3, SECRET $tag$tagged$tag$)", "tagged", "TYPE S3"),
        ("CREATE SECRET s (TYPE S3, SECRET /* c */ 'hidden')", "hidden", "TYPE S3"),
        ("CREATE SECRET s (TYPE S3, SECRET -- c\n 'commented')", "commented", "TYPE S3"),
        # a signed URL carries its credential in the query string, and reaches a
        # manifest as an input path rather than as SQL
        (
            "https://b.s3.amazonaws.com/x.parquet?X-Amz-Signature=deadbeef&partition=7",
            "deadbeef",
            "partition=7",  # the rest of the URL must survive the redaction
        ),
    ],
)
def test_credentials_are_masked_and_the_statement_stays_readable(sql, secret, keep):
    out = redact_secrets(sql)
    assert secret not in out, out
    assert keep in out, out
    assert REDACTED in out


def test_a_redacted_statement_still_parses():
    """A manifest people are told to attach to a bug report should survive being
    pasted back into a client. Replacing a string literal with a bare
    `<redacted>` left SQL that no longer parses."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t (api_key VARCHAR)")
    redacted = redact_secrets("SELECT * FROM t WHERE api_key = 'live_abc'")
    con.execute("EXPLAIN " + redacted)  # raises on a syntax error, which is the point


def test_no_space_assignment_still_matches():
    """`token='x'` with no whitespace. Appending `?` to a `+`-quantified group
    makes it lazy rather than optional, which silently turned this form off."""
    assert redact_secrets("SET s3_secret_access_key='abc123'") != "SET s3_secret_access_key='abc123'"
    assert "abc123" not in redact_secrets("token='abc123'")
    assert "abc123" not in redact_secrets("token=abc123")


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


def test_redaction_covers_the_other_manifest_fields(tmp_path):
    """Parameters were never the only field that can carry a credential: an
    input path can be a signed URL, and a note or a CRS decision quotes the
    argument it was made about. Redacting one field and publishing the rest
    would be a redaction in name only."""
    from mapsmith.provenance import InputRecord

    target = tmp_path / "out.parquet"
    target.write_bytes(b"")
    source = tmp_path / "in.parquet"
    source.write_bytes(b"")
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={},
        inputs=[InputRecord.from_path(source)],
        crs_decisions={"input": "read from https://u:hunter2@host/x.parquet"},
        notes=["retried with token='live_abc'"],
    )
    record.inputs[0].path = "https://b.s3.amazonaws.com/x.parquet?X-Amz-Signature=deadbeef"
    text = record.finish().write_for(target).read_text(encoding="utf-8")
    for secret in ("hunter2", "live_abc", "deadbeef"):
        assert secret not in text, secret
    assert json.loads(text)["parameters_redacted"] is True


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


def test_fields_assigned_after_construction_are_redacted_too(tmp_path):
    """The shape every engine actually uses, which nothing covered.

    Redaction used to run only in `__post_init__`, and no engine passes
    `crs_decisions` or `notes` to the constructor — all of them assign after.
    So the two fields SECURITY.md lists as covered were never redacted on any
    shipped path, and `parameters_redacted` stayed false. The test that claimed
    to cover them passed both as constructor arguments: green, and proving
    nothing about the code that runs.
    """
    target = tmp_path / "out.parquet"
    target.write_bytes(b"x")
    record = ProvenanceRecord(operation="buffer_layer", parameters={}, inputs=[])
    record.crs_decisions = {"reason": "reprojected using password='hunter2'"}
    record.notes.append("retried with token='live_abc'")
    record.repairs.append({"error": "engine said secret='deadbeef'"})

    text = record.finish().write_for(target).read_text(encoding="utf-8")

    for secret in ("hunter2", "live_abc", "deadbeef"):
        assert secret not in text, secret
    assert json.loads(text)["parameters_redacted"] is True


def test_a_credential_name_is_the_same_name_with_hyphens():
    """`x_api_key` was masked and `x-api-key` was not, and they are one name.

    The vocabulary is written with underscores because that is how SQL spells
    it; HTTP spells the same names with hyphens, and a header is exactly the
    shape that reaches a manifest through a remote path or a map literal. Each
    underscore in the list now accepts either separator.
    """
    from mapsmith.provenance import REDACTED, redact_secrets

    assert redact_secrets({"x-api-key": "SEKRIT"}) == {"x-api-key": REDACTED}
    assert redact_secrets({"x_api_key": "SEKRIT"}) == {"x_api_key": REDACTED}
    # And through a nesting level, which is where a header actually lives.
    assert redact_secrets({"headers": {"x-api-key": "SEKRIT"}}) == {
        "headers": {"x-api-key": REDACTED}
    }


def test_an_azure_signed_url_is_redacted_like_an_amazon_one():
    """The same signed URL was masked from one cloud and printed from the other.

    Azure spells its shared-access signature `sig`, and Microsoft's Planetary
    Computer — a source a geospatial agent reaches for — hands out exactly that
    form. `X-Amz-Signature` was in the vocabulary and `sig` was not, so the
    credential travelled into `inputs[].path` in clear.

    The rest of the URL has to survive: a redaction that eats everything after
    the parameter destroys the record it was protecting.
    """
    from mapsmith.provenance import REDACTED, redact_secrets

    azure = (
        "https://x.blob.core.windows.net/c/f.tif?sv=2024-01&sig=Abc123DEF%2F&se=2026"
    )
    masked = redact_secrets(azure)
    assert "Abc123DEF" not in masked
    assert REDACTED in masked
    assert masked.endswith("&se=2026"), "the redaction swallowed the rest of the URL"
    assert "sv=2024-01" in masked

    amazon = "https://s3.amazonaws.com/b/k?X-Amz-Signature=Abc123&x=1"
    assert "Abc123" not in redact_secrets(amazon)


def test_a_short_credential_name_does_not_fire_on_ordinary_words():
    """`sig` is three letters, and the reason it is safe is the anchoring.

    The concern when it was proposed was `design=` and `redesign=`. Both end in
    `ign`, and every rule requires the name to be followed by `=` or a word
    boundary, so neither matches — but a redaction that fires on ordinary fields
    teaches its reader to distrust the mask, so it is asserted rather than
    reasoned about.
    """
    from mapsmith.provenance import REDACTED, redact_secrets

    assert REDACTED not in redact_secrets(
        "https://example.com/p?design=5&redesign=blue"
    )
    assert redact_secrets({"design": "blue", "redesign": "green"}) == {
        "design": "blue",
        "redesign": "green",
    }
