"""Restricted, deterministic field calculation over DuckDB vector relations."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .. import readers, verify, workspace
from ..provenance import InputRecord, ProvenanceRecord
from .duckdb_engine import _COPY_OPTIONS, _connect, _engine_info, _path_identity, _quote, _rel

_MAX_EXPRESSION_LENGTH = 4000
_MAX_AST_DEPTH = 40
_MAX_AST_NODES = 200
_NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "DOUBLE", "DECIMAL",
}
_ARITHMETIC = {"+", "-", "*", "/", "//", "%"}
_COMPARISONS = {
    "COMPARE_EQUAL", "COMPARE_NOTEQUAL", "COMPARE_LESSTHAN",
    "COMPARE_LESSTHANOREQUALTO", "COMPARE_GREATERTHAN",
    "COMPARE_GREATERTHANOREQUALTO", "COMPARE_DISTINCT_FROM",
    "COMPARE_NOT_DISTINCT_FROM",
}
_OPERATORS = {
    "OPERATOR_NOT", "OPERATOR_IS_NULL", "OPERATOR_IS_NOT_NULL",
    "OPERATOR_COALESCE",
}
ON_EXISTING = ("error", "replace")


class _GeoWkbType:
    def __new__(cls, crs: str):
        import pyarrow as pa

        class GeoWkb(pa.ExtensionType):
            def __init__(self, declaration: str):
                self.declaration = declaration
                super().__init__(pa.binary(), "geoarrow.wkb")

            def __arrow_ext_serialize__(self) -> bytes:
                return self.declaration.encode()

            @classmethod
            def __arrow_ext_deserialize__(cls, storage_type, serialized):
                return cls(serialized.decode())

        return GeoWkb(crs)


def _quote_identifier(name: str) -> str:
    if not name:
        raise ValueError("field_name must not be empty")
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def _statement(relation: str, field_name: str, expression: str, on_existing: str) -> str:
    if on_existing not in {"error", "replace"}:
        raise ValueError("on_existing must be 'error' or 'replace'")
    alias = _quote_identifier(field_name)
    if on_existing == "replace":
        return f"SELECT * REPLACE ({expression} AS {alias}) FROM {relation}"
    return f"SELECT *, {expression} AS {alias} FROM {relation}"


def _validate_statement(
    query: str,
    con: Any,
    columns: dict[str, tuple[str, str]],
    field_name: str,
    on_existing: str,
    relation: str | None = None,
) -> None:
    if on_existing not in {"error", "replace"}:
        raise ValueError("on_existing must be 'error' or 'replace'")
    if len(query) > _MAX_EXPRESSION_LENGTH:
        raise ValueError("the calculate_field statement is too long")
    raw = con.execute("SELECT json_serialize_sql(?::VARCHAR)", [query]).fetchone()[0]
    tree = json.loads(raw)
    if tree.get("error"):
        raise ValueError(f"invalid calculate_field expression: {tree.get('error_message', 'parse error')}")
    statements = tree.get("statements") or []
    if len(statements) != 1:
        raise ValueError("calculate_field accepts exactly one SELECT statement")
    node = statements[0].get("node") or {}
    if node.get("type") != "SELECT_NODE" or node.get("modifiers"):
        raise ValueError("calculate_field accepts only a plain SELECT")
    if (node.get("cte_map") or {}).get("map"):
        raise ValueError("calculate_field does not accept CTEs")
    for key in ("where_clause", "having", "qualify", "sample"):
        if node.get(key) is not None:
            raise ValueError("calculate_field does not accept filters or sampling")
    if node.get("group_expressions") or node.get("group_sets"):
        raise ValueError("calculate_field does not accept grouping")
    if relation is not None:
        expected_raw = con.execute(
            "SELECT json_serialize_sql(?::VARCHAR)", [f"SELECT * FROM {relation}"]
        ).fetchone()[0]
        expected_from = json.loads(expected_raw)["statements"][0]["node"]["from_table"]
        if _without_query_locations(node.get("from_table")) != _without_query_locations(expected_from):
            raise ValueError("the FROM relation does not match input_path")
    elif (node.get("from_table") or {}).get("type") != "TABLE_FUNCTION":
        raise ValueError("calculate_field accepts only a direct relation")
    select_list = node.get("select_list") or []
    if on_existing == "error":
        if len(select_list) != 2 or select_list[0].get("class") != "STAR":
            raise ValueError("calculate_field expects the source columns and one new field")
        expression = select_list[1]
        if expression.get("alias", "").lower() != field_name.lower():
            raise ValueError("the calculated field alias does not match field_name")
        star = select_list[0]
        if star.get("replace_list") or star.get("exclude_list") or star.get("rename_list"):
            raise ValueError("calculate_field does not accept star modifiers")
    else:
        if len(select_list) != 1 or select_list[0].get("class") != "STAR":
            raise ValueError("calculate_field expects one replacement field")
        replacements = select_list[0].get("replace_list") or []
        if len(replacements) != 1 or replacements[0].get("key", "").lower() != field_name.lower():
            raise ValueError("the replacement field does not match field_name")
        expression = replacements[0].get("value") or {}
    if _ast_node_count(expression) > _MAX_AST_NODES:
        raise ValueError("the calculate_field expression has too many nodes")
    _validate_expression(expression, columns)


def _ast_node_count(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + sum(_ast_node_count(value) for value in node.values())
    if isinstance(node, list):
        return sum(_ast_node_count(item) for item in node)
    return 0


def _without_query_locations(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _without_query_locations(value)
            for key, value in node.items() if key != "query_location"
        }
    if isinstance(node, list):
        return [_without_query_locations(item) for item in node]
    return node


def _validate_expression(node: Any, columns: dict[str, tuple[str, str]], depth: int = 0) -> None:
    if depth > _MAX_AST_DEPTH:
        raise ValueError("the calculate_field expression is nested too deeply")
    if not isinstance(node, dict):
        raise TypeError("invalid node in calculate_field expression")
    kind = node.get("class")
    if kind == "COLUMN_REF":
        names = node.get("column_names") or []
        if len(names) != 1:
            raise ValueError("calculate_field accepts only unqualified input columns")
        matches = [
            (source_name, duckdb_type)
            for key, (source_name, duckdb_type) in columns.items()
            if key.lower() == names[0].lower() or source_name.lower() == names[0].lower()
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous input column {names[0]!r}")
        if matches[0][1].upper().split("(", 1)[0] not in _NUMERIC_TYPES | {"BOOLEAN"}:
            raise ValueError(f"column {names[0]!r} is not numeric or boolean")
        return
    if kind == "CONSTANT":
        value = node.get("value") or {}
        if value.get("is_null"):
            return
        type_id = (value.get("type") or {}).get("id")
        if type_id not in _NUMERIC_TYPES:
            raise ValueError("calculate_field accepts only numeric, boolean, and null constants")
        return
    if kind == "CAST":
        child = node.get("child") or {}
        value = child.get("value") or {}
        if (
            (node.get("cast_type") or {}).get("id") == "BOOLEAN"
            and child.get("class") == "CONSTANT"
            and (value.get("type") or {}).get("id") == "VARCHAR"
            and value.get("value") in {"t", "f"}
        ):
            return
        raise ValueError("calculate_field accepts no explicit casts")
    if kind == "FUNCTION":
        name = str(node.get("function_name", "")).lower()
        children = node.get("children") or []
        if node.get("schema") or node.get("catalog") or node.get("distinct"):
            raise ValueError("calculate_field does not accept function modifiers")
        if node.get("filter") is not None or (node.get("order_bys") or {}).get("orders"):
            raise ValueError("calculate_field does not accept filtered or ordered functions")
        if name in _ARITHMETIC and node.get("is_operator"):
            valid_arity = len(children) == 2 or (name in {"+", "-"} and len(children) == 1)
        elif name == "nullif":
            valid_arity = len(children) == 2
        else:
            raise ValueError(f"function {name!r} is not allowed in calculate_field")
        if not valid_arity:
            raise ValueError(f"wrong arity for {name} in calculate_field")
        for child in children:
            _validate_expression(child, columns, depth + 1)
        return
    if kind == "COMPARISON":
        if node.get("type") not in _COMPARISONS:
            raise ValueError("comparison is not allowed in calculate_field")
        _validate_expression(node.get("left") or {}, columns, depth + 1)
        _validate_expression(node.get("right") or {}, columns, depth + 1)
        return
    if kind == "CONJUNCTION":
        if node.get("type") not in {"CONJUNCTION_AND", "CONJUNCTION_OR"}:
            raise ValueError("conjunction is not allowed in calculate_field")
        for child in node.get("children") or []:
            _validate_expression(child, columns, depth + 1)
        return
    if kind == "OPERATOR":
        if node.get("type") not in _OPERATORS:
            raise ValueError("operator is not allowed in calculate_field")
        children = node.get("children") or []
        if node.get("type") == "OPERATOR_COALESCE" and not children:
            raise ValueError("COALESCE needs at least one argument")
        for child in children:
            _validate_expression(child, columns, depth + 1)
        return
    if kind == "CASE":
        checks = node.get("case_checks") or []
        if not checks:
            raise ValueError("CASE needs at least one WHEN branch")
        for check in checks:
            _validate_expression(check.get("when_expr") or {}, columns, depth + 1)
            _validate_expression(check.get("then_expr") or {}, columns, depth + 1)
        if node.get("else_expr") is not None:
            _validate_expression(node["else_expr"], columns, depth + 1)
        return
    if kind == "BETWEEN":
        for key in ("input", "lower", "upper"):
            _validate_expression(node.get(key) or {}, columns, depth + 1)
        return
    raise ValueError(f"{kind or 'unknown'} is not allowed in calculate_field")


def _source_schema(con: Any, relation: str) -> dict[str, tuple[str, str]]:
    rows = con.sql(f"DESCRIBE SELECT * FROM {relation}").fetchall()
    columns: dict[str, tuple[str, str]] = {}
    for name, duckdb_type, *_ in rows:
        key = name.lower()
        if key in columns:
            raise ValueError(f"ambiguous input column {name!r}")
        columns[key] = (name, str(duckdb_type).upper())
    return columns


def _parquet_geo(path: str) -> dict[str, Any] | None:
    if not path.lower().endswith(".parquet"):
        return None
    import pyarrow.parquet as pq

    metadata = pq.read_schema(path).metadata or {}
    return json.loads(metadata[b"geo"]) if b"geo" in metadata else None


def _crs_declaration_matches(source: Any, output: Any) -> bool:
    if source is None or output is None:
        return source == output
    return verify.same_crs(source, output)


def _geo_metadata_matches(source: dict[str, Any], output: dict[str, Any]) -> bool:
    if source.get("primary_column") != output.get("primary_column"):
        return False
    source_columns = source.get("columns") or {}
    output_columns = output.get("columns") or {}
    if set(source_columns) != set(output_columns):
        return False
    for name in source_columns:
        for key in ("geometry_types", "encoding"):
            if source_columns[name].get(key) != output_columns[name].get(key):
                return False
        if not _crs_declaration_matches(source_columns[name].get("crs"), output_columns[name].get("crs")):
            return False
    return True


def _write_empty_output(
    input_path: str,
    output_path: str,
    source_geo: dict[str, Any] | None,
) -> None:
    import pyarrow.parquet as pq

    native_declarations = dict(readers.native_geometry_columns(input_path))
    if source_geo is not None or native_declarations:
        descriptor, temporary = tempfile.mkstemp(
            dir=Path(output_path).parent,
            prefix=f".{Path(output_path).name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        import pyarrow as pa

        reader = None
        source_reader = None
        try:
            if source_geo is None:
                import geopandas as gpd

                source_geo = gpd.io.arrow._create_metadata(readers.read_vector(input_path))
            reader = pq.ParquetFile(output_path)
            source_reader = pq.ParquetFile(input_path)
            metadata = dict(reader.schema_arrow.metadata or {})
            metadata[b"geo"] = json.dumps(source_geo).encode()
            fields = [
                pa.field(name, _GeoWkbType(native_declarations[name]))
                if name in native_declarations else reader.schema_arrow.field(name)
                for name in reader.schema_arrow.names
            ]
            with pq.ParquetWriter(temporary, pa.schema(fields, metadata=metadata)) as writer:
                for batch in reader.iter_batches():
                    writer.write_batch(batch)
            reader.close()
            source_reader.close()
            Path(temporary).replace(output_path)
        finally:
            if reader is not None:
                reader.close()
            if source_reader is not None:
                source_reader.close()
            Path(temporary).unlink(missing_ok=True)
        return

    source = readers.read_vector(input_path)
    table = pq.read_table(output_path)
    for name in table.column_names:
        if name not in source.columns:
            source[name] = table[name].to_pandas()
    source = source[table.column_names]
    source.to_parquet(output_path)


def calculate_field(
    input_path: str,
    output_path: str,
    field_name: str,
    expression: str,
    on_existing: str = "error",
) -> dict[str, Any]:
    """Create or replace one numeric or boolean field without changing features."""
    if on_existing not in {"error", "replace"}:
        raise ValueError("on_existing must be 'error' or 'replace'")
    if _path_identity(input_path) == _path_identity(output_path):
        raise ValueError("calculate_field cannot overwrite its input")
    workspace.guard(input_path, "input_path")
    workspace.guard(output_path, "output_path")
    readers.refuse_ambiguous_container(input_path)
    source_crs = verify.probe_crs(input_path)
    if source_crs == verify.UNKNOWN_CRS:
        raise ValueError(f"{input_path} has no CRS, so calculate_field cannot preserve it")
    relation = _rel(input_path)
    con = _connect()
    columns = _source_schema(con, relation)
    destination = field_name
    if on_existing == "error" and field_name.lower() in columns:
        raise ValueError(f"field {field_name!r} already exists; use on_existing='replace'")
    if on_existing == "replace":
        if field_name.lower() not in columns:
            raise ValueError(f"field {field_name!r} does not exist and cannot be replaced")
        destination, destination_type = columns[field_name.lower()]
        if destination_type.startswith("GEOMETRY"):
            raise ValueError("calculate_field cannot replace a geometry column")
    query = _statement(relation, destination, expression, on_existing)
    _validate_statement(query, con, columns, destination, on_existing, relation=relation)
    source_count = con.sql(f"SELECT count(*) FROM {relation}").fetchone()[0]
    source_geo = _parquet_geo(input_path)
    record = ProvenanceRecord(
        operation="calculate_field",
        parameters={
            "field_name": destination,
            "expression": expression,
            "on_existing": on_existing,
            "features_before": int(source_count),
        },
        inputs=[InputRecord.from_path(input_path, crs=source_crs, argument="input_path")],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "analysis_crs": source_crs,
        "reason": "field calculation changes an attribute, never coordinates",
    }
    with verify.audit_on_failure(record, output_path, []):
        con.sql(f"COPY ({query}) TO '{_quote(output_path)}' ({_COPY_OPTIONS})")
        if source_count == 0:
            con.close()
            _write_empty_output(input_path, output_path, source_geo)
            con = _connect()

    def checks() -> list[verify.Check]:
        output_relation = f"read_parquet('{_quote(output_path)}')"
        output_columns = _source_schema(con, output_relation)
        expected_names = [name for name, _ in columns.values()]
        if on_existing == "error":
            expected_names.append(destination)
        excluded = _quote_identifier(destination)
        source_relation = f"SELECT * EXCLUDE ({excluded}) FROM {output_relation}"
        target_relation = (
            f"SELECT * EXCLUDE ({excluded}) FROM {relation}"
            if on_existing == "replace" else f"SELECT * FROM {relation}"
        )
        missing = con.sql(
            f"SELECT count(*) FROM (({source_relation}) EXCEPT ALL ({target_relation}))"
        ).fetchone()[0]
        extra = con.sql(
            f"SELECT count(*) FROM (({target_relation}) EXCEPT ALL ({source_relation}))"
        ).fetchone()[0]
        nonfinite = 0
        output_type = output_columns[destination.lower()][1]
        if output_type.split("(", 1)[0] in _NUMERIC_TYPES and output_type != "BOOLEAN":
            nonfinite = con.sql(
                f"SELECT count(*) FROM {output_relation} WHERE {excluded} IS NOT NULL "
                f"AND (isnan(CAST({excluded} AS DOUBLE)) OR isinf(CAST({excluded} AS DOUBLE)))"
            ).fetchone()[0]
        output_geo = _parquet_geo(output_path)
        source_native = dict(readers.native_geometry_columns(input_path))
        output_native = dict(readers.native_geometry_columns(output_path))
        native_metadata_ok = not source_native or (
            set(source_native) == set(output_native)
            and all(
                _crs_declaration_matches(source_native[name], output_native[name])
                for name in source_native
            )
        )
        metadata_ok = native_metadata_ok and (
            source_geo is None or (
                output_geo is not None and _geo_metadata_matches(source_geo, output_geo)
            )
        )
        return [
            *verify.verify_vector_output(
                output_path,
                expect_crs=source_crs,
                expect_count=source_count,
            ),
            verify.Check(
                "x-mapsmith:unchanged_features",
                missing == 0 and extra == 0,
                f"{missing} missing and {extra} changed rows outside {destination}",
            ),
            verify.Check(
                "x-mapsmith:calculated_field_position",
                [name for name, _ in output_columns.values()] == expected_names,
                f"expected columns {expected_names}, got {list(output_columns.values())}",
            ),
            verify.Check(
                "x-mapsmith:finite_calculated_values",
                nonfinite == 0,
                f"{nonfinite} NaN or infinite values in {destination}",
            ),
            verify.Check(
                "x-mapsmith:geoparquet_metadata_preserved",
                metadata_ok,
                "primary and secondary geometry metadata are unchanged"
                if metadata_ok else "geometry metadata changed",
            ),
        ]

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="calculate_field",
        preconditions=[],
        checks_fn=checks,
        repair=False,
    )
    return {
        "output": str(output_path),
        "feature_count": int(source_count),
        "field_name": destination,
        "provenance": manifest,
        "verified": True,
        **extras,
    }
