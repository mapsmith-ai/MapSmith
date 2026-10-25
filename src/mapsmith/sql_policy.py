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

import json
import re
from typing import Any

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

#: A credential word is allowed a trailing component, from a closed list. Without
#: it `SET s3_secret_key = '...'` passed this layer: the prefix could not eat
#: `s3_secret_` because `key` alone is not a credential word, and `secret` was
#: not followed by a word boundary. `s3_secret_access_key` -- the setting that
#: actually exists -- was caught, so this is depth rather than a live hole; but
#: the redaction matcher in `provenance.py` has the same allowance for the same
#: reason, and two layers written from one idea should not disagree about which
#: names are credentials.
_TAIL = r"(?:[_.\-]?(?:key|keys|value|values|pem|der|jwt|base64|b64))*"

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
    (re.compile(rf"""(?i)\b(?:SET|PRAGMA)\s+(?:GLOBAL\s+|SESSION\s+|LOCAL\s+)?(?:VARIABLE\s+)?["`\[]?[A-Za-z0-9_.]*(?:{_WORDS}){_TAIL}\b"""),
     "a credential setting"),
    # ATTACH with credentials, either as an option or as URI userinfo
    (re.compile(rf"(?i)\bATTACH\b[^;]*?(?:\b(?:{_WORDS})\b\s*(?::=|=)|://[^\s:/@]+:[^\s@/]+@)"),
     "an ATTACH carrying credentials"),
)


#: The environment variable that allows named extensions to be installed and
#: loaded from a statement. Named extensions, not a boolean: "yes, everything"
#: is how a caller ends up with the `aws` extension and a credential reader in a
#: process that was supposed to do geoprocessing.
ALLOW_EXTENSIONS = "MAPSMITH_ALLOW_EXTENSIONS"

#: `INSTALL name` / `LOAD name`, in DuckDB's spellings. `FORCE INSTALL` is one,
#: and the name can be quoted or a path to a local `.duckdb_extension` file.
#: Anchored to STATEMENT position — start of input, or after a semicolon. The
#: first version matched the word anywhere, so `SELECT load FROM sediment` came
#: back refused as loading an extension called "FROM", and so did `ORDER BY load
#: DESC` and a string literal mentioning INSTALL. `load` is an everyday column
#: name: sediment load, nutrient load, traffic load. A guard that refuses
#: ordinary work teaches its reader to route around it, which costs more than
#: the guard is worth — and the test that stated exactly that principle had four
#: cases, none of which exercised it.
_EXTENSION_STATEMENT = re.compile(
    r"""(?im)(?:^|;)\s*(?:FORCE\s+)?(INSTALL|LOAD)\s+(?:["'`]?)([A-Za-z0-9_.:/\-]+)"""
)


def allowed_extensions() -> frozenset[str]:
    """The extensions this process may install or load, from the environment.

    Empty by default. Set `MAPSMITH_ALLOW_EXTENSIONS=postgres,azure` to allow
    exactly those two, in the environment of the process that starts the server
    — which is outside the agent's reach, and that is the point.
    """
    import os

    raw = os.environ.get(ALLOW_EXTENSIONS, "")
    return frozenset(
        name.strip().lower() for name in raw.split(",") if name.strip()
    )


def _extension_statements(query: str) -> list[tuple[str, str]]:
    """Every INSTALL/LOAD in this SQL, as (verb, extension name).

    Uses **DuckDB's own parser**, not a scan, because a scan lost twice on the
    same statement in one day. The first version was a regex over
    `strip_comments(query)`, and an audit walked past it two ways:

        LOAD $$aws$$                          the name class had no `$`
        SELECT '--' AS x; LOAD aws; SELECT 1  strip_comments saw the `--` INSIDE
                                              the string literal, deleted the
                                              rest, and handed the scanner
                                              "SELECT ' " while DuckDB executed
                                              all three statements

    The second one is the deeper hole and it is not fixable by a better regex:
    finding statement boundaries in SQL that has string literals and dollar
    quoting is parsing, and there is a parser right here. `extract_statements`
    classifies both cases as `StatementType.LOAD` and leaves
    `SELECT load FROM sediment` a SELECT, which is the false positive a scan
    also produced.

    Fails CLOSED. If the parser cannot read the SQL, the statement will fail in
    DuckDB anyway, but a regex backstop still runs — an unparseable query is not
    a reason to stop looking.
    """
    import duckdb

    found: list[tuple[str, str]] = []
    try:
        statements = duckdb.extract_statements(query)
    except Exception:  # noqa: BLE001 - unparseable SQL: fall back, do not pass
        statements = None

    if statements is not None:
        for statement in statements:
            # DuckDB reports INSTALL and LOAD under one statement type.
            if not str(statement.type).endswith("LOAD"):
                continue
            match = _EXTENSION_NAME.search(statement.query)
            if match:
                found.append((match.group(1).upper(), _unquote(match.group(2))))
        return found

    for match in _EXTENSION_STATEMENT.finditer(strip_comments(query)):
        found.append((match.group(1).upper(), _unquote(match.group(2))))
    return found


def _unquote(name: str) -> str:
    """The extension name without whatever was wrapped around it."""
    stripped = name.strip()
    for opening, closing in (("$$", "$$"), ("'", "'"), ('"', '"'), ("`", "`")):
        if stripped.startswith(opening) and stripped.endswith(closing) and len(
            stripped
        ) > len(opening) + len(closing) - 1:
            return stripped[len(opening) : -len(closing)]
    return stripped


#: The name inside a single INSTALL/LOAD statement the parser has already
#: isolated. Wider than the scanning pattern on purpose: the statement boundary
#: is no longer this expression's problem, so it can accept dollar quoting.
_EXTENSION_NAME = re.compile(
    r"""(?i)\b(?:FORCE\s+)?(INSTALL|LOAD)\s+(\$\$[^$]*\$\$|["'`][^"'`]*["'`]|[A-Za-z0-9_.:/\-]+)"""
)


def refuse_extension_loading_in_sql(query: str) -> None:
    """Raise if the statement would install or load a DuckDB extension.

    An `INSTALL` is an HTTPS fetch of a **native binary** from
    `extensions.duckdb.org`, executed in this process. It was allowed by default
    in unconfined mode, and `SECURITY.md` said in the same artifact that what
    remained unconfined was file access "and nothing else". An audit used it to
    make `run_sql` return this machine's real cloud credentials:

        INSTALL aws; LOAD aws; LOAD httpfs;
        SELECT * FROM load_aws_credentials()

    None of the existing layers saw it. `autoinstall_known_extensions=false`
    stops *implicit* loading only; `lock_configuration=true` does not stop an
    explicit `INSTALL`; and the remote-path scan looks for `://` and `/vsi`,
    which `INSTALL postgres` does not contain.

    So the answer is not a better scan but a decision that belongs to a person:
    either the extension is already loaded, or somebody says which ones may be.
    `spatial` is unaffected — MapSmith loads it through the Python API before
    the configuration is locked, so no statement has to ask for it.
    """
    allowed = allowed_extensions()
    for verb, name in _extension_statements(query):
        if name.lower() in allowed:
            continue
        raise ValueError(
            f"this SQL would {verb} the DuckDB extension {name!r}, which MapSmith "
            f"refuses. An INSTALL downloads a native binary and runs it in this "
            f"process, and the statement was written by a model rather than by "
            f"you. "
            f"If you want it, say so where the agent cannot: start the server with "
            f"{ALLOW_EXTENSIONS}={name} in its environment (comma-separated for "
            f"more than one). Extensions already loaded keep working — this "
            f"refuses acquiring new ones, not using what is there."
        )


#: Functions whose text arguments DuckDB's spatial extension hands to its OWN
#: PROJ, which opens files natively and so walks past `allowed_directories`.
#: Measured 2026-10-25 under a workspace: `ST_Transform(g, 'EPSG:4326',
#: '+init=<a file outside>:1')` read the file (x came back as the `+x_0` written
#: in it), and so did `ST_SetCRS(g, '+init=…')` followed by the two-argument
#: `ST_Transform`, while `read_text` on the same path was refused.
_CRS_FUNCTIONS = frozenset({"st_transform", "st_setcrs"})

#: Readers whose dataset carries a coordinate system that reaches PROJ: GDAL
#: opens a grid the SRS names while reading it, and a GeoParquet's CRS becomes
#: the source of a two-argument `ST_Transform`. Each path goes through the same
#: gate as a tool argument, `workspace.guard`, which reads the declared SRS as
#: text before anything parses it. Readers of CSV, JSON or text carry no CRS
#: and stay behind DuckDB's own filesystem sandbox, which does hold for them.
_DATASET_READERS = frozenset(
    {"st_read", "st_read_meta", "st_readshp", "st_readosm", "shapefile_meta", "read_parquet", "parquet_scan"}
)

#: The readers that hand the file to GDAL, which reads the SRS -- and opens a
#: grid it names -- in whatever format it recognises. The path gate can only
#: vouch for the formats `crs_policy` reads itself, so these take those and
#: nothing else (audit 2026-10-25: a FlatGeobuf naming a grid outside the
#: workspace passed the gate with nothing checked, and the grid was read).
_GDAL_READERS = frozenset({"st_read", "st_read_meta"})

#: File names DuckDB reads natively when they appear in FROM; any other name is
#: handed to GDAL by the spatial extension's replacement scan.
_NATIVE_SCAN = re.compile(r"(?i)\.(?:parquet|csv|tsv|json|jsonl|ndjson)(?:\.(?:gz|zst))?$")

#: `ST_Read` and a `layer` argument in statement text the parser cannot
#: serialise. Raw text, comments included, on purpose: a false positive costs
#: a rewrite as a SELECT, a miss costs the server.
_LAYER_IN_TEXT = re.compile(r"(?is)\bst_read\b.*\blayer\b")

#: Table functions that execute SQL held in a string, where no walk of the
#: statement can see it.
_SQL_IN_A_STRING = frozenset({"query", "query_table", "json_execute_serialized_sql"})

_GLOB_CHARACTERS = re.compile(r"[*?\[]")


def refuse_unchecked_crs_and_paths(query: str, con: Any) -> None:
    """Under a workspace, refuse SQL whose coordinate systems or dataset paths MapSmith cannot check.

    The connection sandbox (`allowed_directories`, external access off) is
    DuckDB's filesystem, and DuckDB's spatial extension carries its own PROJ,
    which does not go through it. So the statement is read with DuckDB's own
    parser (`json_serialize_sql`) and every place a CRS or a dataset path can
    enter is held to the policy the tools already apply:

    - a CRS argument of `ST_Transform` / `ST_SetCRS`, or a `GEOMETRY('…')`
      type, must be a literal that `crs_policy.parse` accepts;
    - a path given to a reader of datasets with a coordinate system
      (`ST_Read`, `read_parquet`, a file named in FROM) must be a literal that
      `workspace.guard` accepts; a glob is expanded by the sandboxed connection
      and every match is checked; and what GDAL would open (`ST_Read`, a file
      in FROM that DuckDB does not read natively) must be a format whose SRS
      `crs_policy` reads first, recognised by its bytes;
    - a statement the parser cannot serialise (anything but SELECT), a table
      function that runs SQL from a string, and GDAL open options are refused.

    Fails closed: what cannot be read as a constant is refused with how to
    write it, because a check that passes what it cannot see is the hole this
    replaces. Without a workspace, file access is unconfined by design and
    this returns at once.

    `con` is the sandboxed connection: it serialises the statement and expands
    globs, so a pattern is matched by the same engine, under the same sandbox,
    that will read it.
    """
    from . import workspace

    if workspace.root() is None:
        return
    raw = con.execute("SELECT json_serialize_sql(?::VARCHAR)", [query]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        raise ValueError(
            "under MAPSMITH_WORKSPACE, run_sql runs only SELECT statements that MapSmith can read "
            f"before they run ({tree.get('error_message', 'unreadable')}). Write the query as one or "
            "more SELECTs; to keep a result, pass output_path instead of COPY or CREATE TABLE."
        )
    ctes = {name.lower() for name in _cte_names(tree)}
    _walk(tree, ctes, con)


def refuse_missing_gdal_layer(query: str, con: Any) -> None:
    """Refuse `ST_Read(..., layer := 'x')` when the dataset has no layer called x.

    DuckDB's spatial extension (1.5.5) kills the process with a segmentation
    fault on that call, and with it the MCP server: one line of agent-written
    SQL was a denial of service (audit, 2026-10-25; reproduced on a bare
    connection, so the defect is upstream). The layer is checked against the
    names `ST_Read_Meta` lists before the statement runs. Applies in every
    mode, because a crash is not a question of file access.

    A statement the parser cannot serialise (anything but SELECT) cannot be
    walked, and the first version let it through: `CREATE TABLE t AS SELECT *
    FROM ST_Read(..., layer := 'nope')` still ended the process without a
    workspace (found by the showcase review, 2026-10-25). So when the text
    names both, it is refused -- closed, by a scan, because there is no tree
    to read -- and the caller is told to write the SELECT, which is checked.
    """
    raw = con.execute("SELECT json_serialize_sql(?::VARCHAR)", [query]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        if _LAYER_IN_TEXT.search(query):
            raise ValueError(
                "ST_Read(..., layer := …) is checked before it runs only inside a SELECT: DuckDB's spatial "
                "extension crashes the process on a layer the dataset does not have. Write the query as a "
                "SELECT, and pass output_path to keep the result."
            )
        return
    for node in _functions_named(tree, "st_read"):
        children = node.get("children") or []
        layer = _named_argument(children, "layer")
        if layer is None:
            continue
        path = _constant(children[0]) if children else None
        name = _constant(layer)
        if path is None or name is None or path[0] != "VARCHAR" or name[0] != "VARCHAR" or name[1] is None:
            raise ValueError(
                "ST_Read(..., layer := …) takes the path and the layer as literals, so the layer can be "
                "checked before GDAL opens it: DuckDB's spatial extension crashes the process on a layer "
                "the dataset does not have."
            )
        layers = con.execute(
            "SELECT [l.name FOR l IN layers] FROM ST_Read_Meta(?::VARCHAR)", [str(path[1])]
        ).fetchone()
        names = list(layers[0]) if layers and layers[0] is not None else []
        if str(name[1]) not in names:
            raise ValueError(
                f"ST_Read: {path[1]} has no layer {name[1]!r} (its layers: {names}). Refused before running, "
                "because DuckDB's spatial extension crashes the process on a missing layer."
            )


def _argument(child: Any) -> tuple[str, Any]:
    """(name, value node) of a table-function argument; the name is '' when positional.

    `layer := 'x'` serialises as the value with an alias; `layer = 'x'`
    serialises as a COMPARISON of a column called `layer` with the value, and
    DuckDB still EXECUTES it as the named parameter (second audit pass,
    2026-10-25). A check that reads one spelling is walked past by the other.
    """
    if not isinstance(child, dict):
        return "", child
    alias = str(child.get("alias", "")).lower()
    if alias:
        return alias, child
    left = child.get("left") or {}
    names = left.get("column_names") or []
    if child.get("type") == "COMPARE_EQUAL" and left.get("class") == "COLUMN_REF" and len(names) == 1:
        return str(names[0]).lower(), child.get("right")
    return "", child


def _named_argument(children: list[Any], name: str) -> Any:
    """The value node of the argument called `name`, in either spelling, or None."""
    for child in children:
        alias, value = _argument(child)
        if alias == name:
            return value
    return None


def _functions_named(node: Any, wanted: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("class") == "FUNCTION" and str(node.get("function_name", "")).lower() == wanted:
            found.append(node)
        for value in node.values():
            found.extend(_functions_named(value, wanted))
    elif isinstance(node, list):
        for item in node:
            found.extend(_functions_named(item, wanted))
    return found


def _cte_names(node: Any) -> list[str]:
    names: list[str] = []
    if isinstance(node, dict):
        for entry in (node.get("cte_map") or {}).get("map") or []:
            names.append(str(entry.get("key", "")))
        for value in node.values():
            names.extend(_cte_names(value))
    elif isinstance(node, list):
        for item in node:
            names.extend(_cte_names(item))
    return names


def _walk(node: Any, ctes: set[str], con: Any) -> None:
    """Visit every node: the checks look at a node, the recursion at what is inside it."""
    if isinstance(node, list):
        for item in node:
            _walk(item, ctes, con)
        return
    if not isinstance(node, dict):
        return
    if node.get("class") == "FUNCTION":
        name = str(node.get("function_name", "")).lower()
        children = node.get("children") or []
        if name in _SQL_IN_A_STRING:
            raise ValueError(
                f"{name}() runs SQL held in a string, which MapSmith cannot read before it runs; "
                "under MAPSMITH_WORKSPACE write the statement itself."
            )
        if name in _CRS_FUNCTIONS:
            for child in children[1:]:
                _check_crs_argument(name, child)
        if name in _DATASET_READERS:
            for child in children:
                alias, value = _argument(child)
                if alias in ("", "sibling_files"):
                    for path in _literal_paths(name, value):
                        _check_path(path, con, gdal=name in _GDAL_READERS and alias == "")
                elif alias == "open_options":
                    raise ValueError(
                        f"{name}(open_options := …) is refused under MAPSMITH_WORKSPACE: a GDAL open "
                        "option can name a file, and MapSmith does not check them. Read the dataset "
                        "without it."
                    )
    if node.get("type") == "BASE_TABLE":
        # `FROM 'parcels.gpkg'` is a reader too: DuckDB swaps a file name for
        # `read_parquet` or, with spatial loaded, for GDAL. A CTE of the same
        # name shadows the file, so it is not a path.
        table = str(node.get("table_name", ""))
        if table.lower() not in ctes and any(c in table for c in "./\\"):
            _check_path(table, con, gdal=not _NATIVE_SCAN.search(table))
    modifiers = node.get("user_type_modifiers")
    if modifiers:
        # GEOMETRY('…') carries a CRS in the type, and DuckDB resolves it.
        for modifier in modifiers:
            if (modifier.get("type") or {}).get("id") == "VARCHAR" and not modifier.get("is_null"):
                _check_crs_text("a GEOMETRY type", str(modifier.get("value")))
    for value in node.values():
        _walk(value, ctes, con)


def _constant(node: Any) -> tuple[str | None, Any] | None:
    """(type id, value) of a literal, or None when the node is not one."""
    if isinstance(node, dict) and node.get("class") == "CONSTANT":
        value = node.get("value") or {}
        return (value.get("type") or {}).get("id"), (None if value.get("is_null") else value.get("value"))
    return None


def _check_crs_argument(function: str, child: Any) -> None:
    constant = _constant(child)
    if constant is not None:
        kind, value = constant
        if kind == "BOOLEAN" or value is None:
            return
        if kind == "VARCHAR":
            _check_crs_text(f"{function}()", str(value))
            return
    # `always_xy := true` arrives as a cast of a literal to BOOLEAN, and a
    # BOOLEAN cannot carry a coordinate system, wherever it sits.
    if (
        isinstance(child, dict)
        and child.get("class") == "CAST"
        and (child.get("cast_type") or {}).get("id") == "BOOLEAN"
        and _constant(child.get("child")) is not None
    ):
        return
    raise ValueError(
        f"{function}() takes its coordinate systems as literals under MAPSMITH_WORKSPACE, such as "
        "'EPSG:32633': a CRS computed from a column or an expression cannot be checked before PROJ "
        "opens the files it names."
    )


def _check_crs_text(where: str, text: str) -> None:
    from . import crs_policy

    try:
        crs_policy.parse(text)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - whatever pyproj raises: what MapSmith cannot read is not passed on
        raise ValueError(
            f"{where}: PROJ could not read the coordinate system {text!r} ({exc}), and one MapSmith "
            "cannot check is not run."
        ) from None


def _literal_paths(function: str, child: Any) -> list[str]:
    constant = _constant(child)
    if constant is not None and constant[0] == "VARCHAR" and constant[1] is not None:
        return [str(constant[1])]
    if isinstance(child, dict) and child.get("function_name") == "list_value":
        items = [_constant(item) for item in child.get("children") or []]
        if all(item is not None and item[0] == "VARCHAR" and item[1] is not None for item in items):
            return [str(item[1]) for item in items]  # type: ignore[index]
    raise ValueError(
        f"{function}() takes its paths as literals under MAPSMITH_WORKSPACE ('data/parcels.gpkg', or "
        "a list of them): a path computed in SQL cannot be checked before the reader opens it."
    )


def _check_path(path: str, con: Any, gdal: bool = False) -> None:
    if not _GLOB_CHARACTERS.search(path):
        _check_one_path(path, gdal)
        return
    try:
        matches = [row[0] for row in con.execute("SELECT file FROM glob(?)", [path]).fetchall()]
    except Exception as exc:  # noqa: BLE001 - the sandbox refusing the pattern is a refusal here too
        raise ValueError(f"'run_sql': the pattern {path!r} could not be expanded inside the workspace ({exc}).") from None
    for match in matches:
        _check_one_path(str(match), gdal)


def _check_one_path(path: str, gdal: bool) -> None:
    from . import crs_policy, workspace

    workspace.guard(path, "run_sql")
    if gdal and not crs_policy.srs_is_read_before_gdal(path):
        raise ValueError(
            f"'run_sql': {path} would be opened by GDAL, which reads its coordinate system -- and a grid "
            "file it names, wherever that is -- before MapSmith can check it. Under MAPSMITH_WORKSPACE "
            "run_sql reads through GDAL only the formats MapSmith checks first: GeoPackage, a shapefile's "
            ".shp, GeoParquet. Convert it to one of those first."
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

    # Both the comment-stripped text AND the raw text, because stripping
    # comments cannot be done by a scan without a parser: a `--` inside a
    # STRING LITERAL made this scanner see `"SELECT ' "` for a query that went
    # on to create a secret, so `SELECT '--' AS x; CREATE SECRET ...` was not
    # refused. Fail closed — if either reading matches, refuse. The raw text can
    # produce a false positive on a credential word inside a comment, which
    # costs a caller one rewritten comment; the other direction costs a
    # credential.
    for text in (strip_comments(query), query):
        for pattern, what in _RULES:
            match = pattern.search(text)
            if not match:
                continue
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
