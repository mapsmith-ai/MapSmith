"""Refuse agent-written SQL that carries a credential, instead of redacting it.

Issue #18 started from a manifest leak: ``run_sql`` records the query verbatim,
manifests are meant to be shared, so ``CREATE SECRET (... SECRET 'AKIA…')``
ended up in an artifact this project encourages people to attach to a bug
report. The first fix was redaction — mask the value, keep the statement
readable — and an adversarial audit took it apart in minutes. Measured against
the shipped 0.2.1 matcher, all four of these left the secret in the manifest:

    CREATE SECRET t (TYPE http, EXTRA_HTTP_HEADERS MAP{'Authorization': 'Bearer X'})
    CREATE SECRET s (TYPE S3, SECRET E'shh\\'x')      -- escaped-string literal
    CREATE SECRET s (TYPE S3, SECRET $$shh$$)         -- dollar quoting
    CREATE SECRET s (TYPE S3, SECRET /* c */ 'shh')   -- comment between the two

The second one is worse than a miss: the ``E'`` prefix desynchronises the quote
pairing, so the matcher redacted a *different* argument and kept the secret —
a manifest that is both wrong and leaky. That is the shape of the problem:
redaction is a text scan without a SQL parser, and every dialect has one more
way to write a string. Chasing them one at a time is a losing race, so the
statement itself is refused before it runs and there is nothing left to redact.

What that costs: nothing documented. No README, doc page or notebook shows
credentials being configured through ``run_sql``, and in MapSmith's sandbox
exactly one secret type is even constructible (``http`` — extension autoloading
is off, so ``TYPE s3``/``azure``/``postgres`` fail with "does not exist"). What
it protects is the case that matters: a credential the *user* pasted into the
chat, which the model then writes into SQL, which MapSmith writes into a file
meant to be published.

Redaction stays in ``provenance.redact_secrets`` as a second layer, because a
credential can reach a manifest through a parameter that is not SQL at all.
"""

from __future__ import annotations

import re

# `--` to end of line, and `/* ... */` including newlines. Removed before
# matching so `CREATE /*x*/ SECRET` cannot walk past the check — the audit used
# exactly that trick against the redaction matcher.
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

# Fragments that make a setting name credential-bearing. Kept as fragments
# rather than a fixed list of settings: DuckDB moves these between releases
# (in 1.5.5 the s3_* settings are gone from duckdb_settings() and live in the
# secrets manager instead), and a stale allow-list fails open.
_CREDENTIAL_WORDS = (
    "secret", "password", "passwd", "pwd", "passphrase", "token", "key_id",
    "access_key", "credential", "authorization", "sas", "signature",
    "client_secret", "private_key", "connection_string", "conninfo",
)
_WORDS = "|".join(_CREDENTIAL_WORDS)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # CREATE [OR REPLACE] [PERSISTENT|TEMPORARY] SECRET [IF NOT EXISTS] name (...)
    (re.compile(r"(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PERSISTENT\s+|TEMPORARY\s+|TEMP\s+)?SECRET\b"),
     "a DuckDB secret"),
    # SET / PRAGMA of a credential-bearing setting, in any of DuckDB's spellings
    # The optional quoting character before the name is load-bearing: DuckDB
    # accepts a quoted identifier, and without it the opening quote broke the
    # contiguity with the credential word, so `SET "s3_secret_access_key" = '…'`
    # passed BOTH layers — neither refused nor redacted. Fifth escape found by
    # an audit, and it fell under neither documented limit: the name was
    # perfectly recognisable, the statement did configure a credential.
    (re.compile(rf"""(?i)\b(?:SET|PRAGMA)\s+(?:GLOBAL\s+|SESSION\s+|LOCAL\s+)?(?:VARIABLE\s+)?["`\[]?[A-Za-z0-9_.]*(?:{_WORDS})\b"""),
     "a credential setting"),
    # ATTACH with credentials, either as an option or as URI userinfo
    (re.compile(rf"(?i)\bATTACH\b[^;]*?(?:\b(?:{_WORDS})\b\s*(?::=|=)|://[^\s:/@]+:[^\s@/]+@)"),
     "an ATTACH carrying credentials"),
)


def strip_comments(query: str) -> str:
    """SQL with comments replaced by a space, so tokens cannot be split by one."""
    return _COMMENTS.sub(" ", query)


def refuse_credentials_in_sql(query: str) -> None:
    """Raise if the statement would set a credential. Silent otherwise.

    Keyed on the *shape* of the statement, never on what a value looks like: an
    honest ``SELECT secret_count FROM audit`` is not refused, and a credential
    that happens to look innocuous is still refused. Value-shaped detection is
    what made the redaction matcher lose.
    """
    # imported here, not at module level: provenance imports the package root,
    # and this module is pulled in early by the engines
    from .provenance import redact_secrets

    text = strip_comments(query)
    for pattern, what in _RULES:
        match = pattern.search(text)
        if match:
            # The ATTACH rule matches through the URI userinfo, so the quoted
            # fragment carried the password into the error message — and from
            # there onto disk, since a plan manifest records step errors. The
            # message that refuses a credential must not be how it escapes.
            quoted = redact_secrets(match.group(0).strip())
            raise ValueError(
                f"this SQL configures {what} ({quoted!r}), which MapSmith "
                "refuses: the statement is written by the model and recorded verbatim in "
                "the provenance manifest, which is meant to be shared. Configure "
                "credentials outside the agent's reach — in the environment of the process "
                "that starts the server — rather than in a statement a tool call can carry."
            )
