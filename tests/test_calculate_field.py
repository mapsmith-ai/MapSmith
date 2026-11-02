"""Restricted field calculation: real DuckDB ASTs and closed-form vector outputs."""

import json
from pathlib import Path

import geopandas as gpd
import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point

from mapsmith import verify
from mapsmith.engines import duckdb_engine, field_calculation


@pytest.fixture
def con():
    connection = duckdb_engine._connect()
    yield connection
    connection.close()


@pytest.fixture
def columns():
    return {
        "a": ("a", "DOUBLE"),
        "b": ("b", "DOUBLE"),
        "flag": ("flag", "BOOLEAN"),
        "label": ("label", "VARCHAR"),
    }


def check(con, columns, expression, on_existing="error", field_name="result"):
    query = field_calculation._statement(
        "read_parquet('source.parquet')", field_name, expression, on_existing
    )
    field_calculation._validate_statement(query, con, columns, field_name, on_existing)


@pytest.mark.parametrize(
    "expression",
    [
        "a + b * 2",
        "-a + 4",
        "a / NULLIF(b, 0)",
        "a // b",
        "a % b",
        "CASE WHEN a > 3 AND flag THEN COALESCE(b, 42) ELSE 0 END",
        "IF(a < 10, 1, 0)",
        "IFNULL(b, 0)",
        "a BETWEEN 1 AND 5",
        "a IS NOT DISTINCT FROM b",
        "NOT flag OR a != b",
        "flag = TRUE",
    ],
)
def test_supported_expressions_are_accepted(con, columns, expression):
    check(con, columns, expression)


def test_replacement_statement_is_accepted(con, columns):
    check(con, columns, "COALESCE(a, 0)", on_existing="replace", field_name="a")


@pytest.mark.parametrize(
    "expression",
    [
        "random()",
        "read_blob('secret.txt')",
        "sum(a)",
        "sum(a) OVER ()",
        "(SELECT max(a))",
        "CAST(a AS VARCHAR)",
        "source.a",
        "a + 1; SELECT 2",
        "'text'",
        "DATE '2026-01-01'",
        "label + 1",
        "a + '1'",
        "[1, 2]",
        "a ^ 2",
        "a ** 2",
    ],
)
def test_unsafe_or_unsupported_expressions_are_refused(con, columns, expression):
    with pytest.raises(ValueError):
        check(con, columns, expression)


def test_ascii_case_ambiguity_is_refused(con, columns):
    columns["A"] = ("A", "DOUBLE")
    with pytest.raises(ValueError, match="ambiguous|Ambiguous"):
        check(con, columns, "a + 1")


def test_identifier_quoting_is_deterministic(con, columns):
    query = field_calculation._statement(
        "read_parquet('source.parquet')", 'odd"name', "a + 1", "error"
    )
    assert 'AS "odd""name"' in query


def test_expression_node_budget_is_enforced(con, columns):
    expression = f"COALESCE({', '.join(['a'] * 201)})"
    with pytest.raises(ValueError, match="nodes|too large|too complex"):
        check(con, columns, expression)


def _people(path: Path, empty: bool = False) -> Path:
    gdf = gpd.GeoDataFrame(
        {
            "a": [] if empty else [7.0],
            "b": [] if empty else [2.0],
            "nullable": [] if empty else [None],
            "flag": [] if empty else [True],
            "label": [] if empty else ["x"],
            "geom": gpd.GeoSeries([] if empty else [Point(0, 0)], dtype="geometry"),
            "other": gpd.GeoSeries([] if empty else [Point(1, 1)], dtype="geometry"),
        },
        geometry="geom",
        crs="EPSG:4326",
    )
    gdf.to_parquet(path)
    return path


def test_calculate_field_creates_an_arithmetic_column(tmp_path):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "quotient.parquet"
    result = field_calculation.calculate_field(
        str(source), str(out), "quotient", "a / b"
    )
    assert result["feature_count"] == 1
    assert gpd.read_parquet(out)["quotient"].tolist() == [3.5]
    manifest = json.loads(Path(f"{out}.provenance.json").read_text(encoding="utf-8"))
    assert manifest["operation"] == "calculate_field"
    assert manifest["parameters"]["expression"] == "a / b"
    assert manifest["inputs"][0]["argument"] == "input_path"


def test_calculate_field_integer_division(tmp_path):
    source = tmp_path / "integers.parquet"
    gpd.GeoDataFrame(
        {"a": [7], "b": [2]}, geometry=[Point(0, 0)], crs="EPSG:4326"
    ).to_parquet(source)
    out = tmp_path / "integer.parquet"
    field_calculation.calculate_field(str(source), str(out), "quotient", "a // b")
    assert gpd.read_parquet(out)["quotient"].tolist() == [3]


def test_calculate_field_case_and_coalesce(tmp_path):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "case.parquet"
    field_calculation.calculate_field(
        str(source), str(out), "answer",
        "CASE WHEN a > 3 AND flag THEN COALESCE(nullable, 42) ELSE 0 END",
    )
    assert gpd.read_parquet(out)["answer"].tolist() == [42]


def test_calculate_field_replacement_preserves_position_and_source_spelling(tmp_path):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "replacement.parquet"
    field_calculation.calculate_field(
        str(source), str(out), "A", "a + 1", on_existing="replace"
    )
    got = gpd.read_parquet(out)
    assert got.columns.tolist() == ["a", "b", "nullable", "flag", "label", "geom", "other"]
    assert got["a"].tolist() == [8.0]


def test_calculate_field_refuses_collision_and_geometry_overwrite(tmp_path):
    source = _people(tmp_path / "people.parquet")
    with pytest.raises(ValueError, match="exists|collision"):
        field_calculation.calculate_field(
            str(source), str(tmp_path / "collision.parquet"), "a", "a + 1"
        )
    with pytest.raises(ValueError, match="geometry"):
        field_calculation.calculate_field(
            str(source), str(tmp_path / "geometry.parquet"), "geom", "a + 1",
            on_existing="replace",
        )


def test_calculate_field_refuses_non_finite_values_and_keeps_manifest(tmp_path):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "infinite.parquet"
    with pytest.raises(verify.VerificationError):
        field_calculation.calculate_field(
            str(source), str(out), "invalid", "1.0 / 0.0"
        )
    manifest = json.loads(Path(f"{out}.provenance.json").read_text(encoding="utf-8"))
    assert any(
        check["name"] == "x-mapsmith:finite_calculated_values" and not check["passed"]
        for check in manifest["verification"]
    )


def test_calculate_field_empty_input_preserves_schema_and_geo_metadata(tmp_path):
    source = _people(tmp_path / "empty.parquet", empty=True)
    out = tmp_path / "empty_out.parquet"
    field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    got = gpd.read_parquet(out)
    assert got.empty
    assert got.crs.to_epsg() == 4326
    geo = json.loads((pq.read_schema(out).metadata or {})[b"geo"])
    assert geo["primary_column"] == "geom"
    assert set(geo["columns"]) == {"geom", "other"}
    assert got.columns.tolist() == ["a", "b", "nullable", "flag", "label", "geom", "other", "answer"]


def test_calculate_field_does_not_overwrite_existing_metadata_tmp(tmp_path):
    source = _people(tmp_path / "empty.parquet", empty=True)
    out = tmp_path / "out.parquet"
    victim = tmp_path / "out.parquet.metadata.tmp"
    victim.write_bytes(b"do not delete")
    field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    assert victim.read_bytes() == b"do not delete"


def test_calculate_field_cleans_metadata_tmp_when_rewrite_fails(tmp_path, monkeypatch):
    source = _people(tmp_path / "empty.parquet", empty=True)
    out = tmp_path / "out.parquet"

    def fail_open(_path):
        raise RuntimeError("cannot read parquet")

    monkeypatch.setattr(pq, "ParquetFile", fail_open)
    with pytest.raises(RuntimeError, match="cannot read parquet"):
        field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    assert list(tmp_path.glob(".out.parquet.*.tmp")) == []


def test_calculate_field_preserves_primary_and_secondary_geometry_metadata(tmp_path):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "metadata.parquet"
    field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    source_geo = json.loads((pq.read_schema(source).metadata or {})[b"geo"])
    output_geo = json.loads((pq.read_schema(out).metadata or {})[b"geo"])
    assert output_geo["primary_column"] == source_geo["primary_column"] == "geom"
    assert set(output_geo["columns"]) == set(source_geo["columns"]) == {"geom", "other"}
    got = gpd.read_parquet(out)
    assert got.crs.to_epsg() == 4326
    assert got["geom"].tolist() == gpd.read_parquet(source)["geom"].tolist()
    assert got["other"].tolist() == gpd.read_parquet(source)["other"].tolist()


def test_calculate_field_refuses_changed_secondary_geometry_crs(tmp_path, monkeypatch):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "changed_crs.parquet"
    original_geo = field_calculation._parquet_geo

    def changed_secondary_crs(path):
        geo = original_geo(path)
        if str(path) == str(out):
            geo["columns"]["other"]["crs"] = "EPSG:32632"
        return geo

    monkeypatch.setattr(field_calculation, "_parquet_geo", changed_secondary_crs)
    with pytest.raises(verify.VerificationError):
        field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    manifest = json.loads(Path(f"{out}.provenance.json").read_text(encoding="utf-8"))
    assert any(
        check["name"] == "x-mapsmith:geoparquet_metadata_preserved" and not check["passed"]
        for check in manifest["verification"]
    )


def test_calculate_field_refuses_changed_native_geometry_declaration(tmp_path, monkeypatch):
    source = _people(tmp_path / "people.parquet")
    out = tmp_path / "changed_native.parquet"
    from mapsmith import readers

    monkeypatch.setattr(field_calculation, "_parquet_geo", lambda _path: None)

    def changed_native_declaration(path):
        declaration = "EPSG:32632" if str(path) == str(out) else "EPSG:4326"
        return [("geom", f'{{"crs":"{declaration}"}}')]

    monkeypatch.setattr(readers, "native_geometry_columns", changed_native_declaration)
    with pytest.raises(verify.VerificationError):
        field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    manifest = json.loads(Path(f"{out}.provenance.json").read_text(encoding="utf-8"))
    assert any(
        check["name"] == "x-mapsmith:geoparquet_metadata_preserved" and not check["passed"]
        for check in manifest["verification"]
    )


def test_calculate_field_refuses_input_without_crs(tmp_path):
    source = tmp_path / "plain.parquet"
    gpd.GeoDataFrame({"a": [1.0]}, geometry=[Point(0, 0)]).to_parquet(source)
    with pytest.raises(ValueError, match="CRS"):
        field_calculation.calculate_field(
            str(source), str(tmp_path / "out.parquet"), "answer", "a + 1"
        )


def test_calculate_field_catalog_contract_and_discovery():
    from mapsmith import catalog

    entry = next(op for op in catalog.OPERATIONS if op["name"] == "calculate_field")
    assert entry["status"] == "available"
    assert entry["tool"] is None
    assert entry["output_cardinality"] == "same_as_input"
    assert catalog.PARAMETER_CONTRACTS[("calculate_field", "on_existing")]["choices"] == [
        "error", "replace",
    ]
    assert catalog.PARAMETER_CONTRACTS[("calculate_field", "output_path")]["extensions"] == [
        ".parquet"
    ]
    survivors = catalog.applicable(
        input_kind="vector", produces="dataset:vector", dataset_inputs=1
    )
    assert "calculate_field" in {op["name"] for op in survivors}
    hits = catalog.entries(catalog.search("add a column computed from existing fields", limit=100))
    assert "calculate_field" in {op["name"] for op in hits}


@pytest.mark.parametrize("on_existing", ["error", "replace"])
def test_calculate_field_refuses_from_injection_before_copy(tmp_path, on_existing):
    declared = _people(tmp_path / "declared.parquet")
    other = tmp_path / "other.parquet"
    gpd.GeoDataFrame({"secret": [42]}, geometry=[Point(0, 0)], crs="EPSG:4326").to_parquet(other)
    expression = (
        "1 AS result FROM read_parquet('other.parquet') --"
        if on_existing == "error"
        else "1 AS result) FROM read_parquet('other.parquet') --"
    )
    field_name = "result" if on_existing == "error" else "a"
    out = tmp_path / "out.parquet"
    with pytest.raises(ValueError, match="FROM|relation"):
        field_calculation.calculate_field(
            str(declared), str(out), field_name, expression, on_existing=on_existing
        )
    assert not out.exists()


def test_calculate_field_preserves_native_geometry_types(tmp_path):
    import duckdb

    source = tmp_path / "both.parquet"
    con = duckdb.connect()
    con.load_extension("spatial")
    con.execute(
        f"COPY (SELECT 7.0 AS a, ST_Point(0, 0) AS geom, ST_Point(1, 1) AS other) "
        f"TO '{source.as_posix()}' (FORMAT parquet, geoparquet_version 'BOTH')"
    )
    con.close()
    out = tmp_path / "out.parquet"
    field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    from mapsmith import readers

    assert [name for name, _ in readers.native_geometry_columns(out)] == ["geom", "other"]


def test_calculate_field_does_not_materialize_the_whole_output(tmp_path, monkeypatch):
    import inspect

    source = _people(tmp_path / "people.parquet")
    original_read_table = pq.read_table

    def guarded_read_table(*args, **kwargs):
        if inspect.stack()[1].function == "calculate_field":
            raise AssertionError("the whole output was materialized by the engine")
        return original_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", guarded_read_table)
    field_calculation.calculate_field(
        str(source), str(tmp_path / "out.parquet"), "answer", "a + 1"
    )


def test_calculate_field_accepts_parameterized_decimal_columns(tmp_path):
    import duckdb

    source = tmp_path / "decimal.parquet"
    con = duckdb.connect()
    con.load_extension("spatial")
    con.execute(
        f"COPY (SELECT CAST(7.0 AS DECIMAL(10, 1)) AS a, "
        f"CAST(2.0 AS DECIMAL(10, 1)) AS b, ST_Point(0, 0) AS geom) "
        f"TO '{source.as_posix()}' (FORMAT parquet, geoparquet_version 'BOTH')"
    )
    con.close()
    out = tmp_path / "out.parquet"
    field_calculation.calculate_field(str(source), str(out), "answer", "a / b")
    assert gpd.read_parquet(out)["answer"].tolist() == [3.5]


def test_calculate_field_empty_replacement_uses_the_calculated_type(tmp_path):
    source = _people(tmp_path / "empty.parquet", empty=True)
    out = tmp_path / "empty_out.parquet"
    field_calculation.calculate_field(
        str(source), str(out), "a", "a > 0", on_existing="replace"
    )
    assert pq.read_schema(out).field("a").type == "bool"


def test_calculate_field_empty_input_preserves_native_geometry_types(tmp_path):
    import duckdb

    source = tmp_path / "empty_both.parquet"
    con = duckdb.connect()
    con.load_extension("spatial")
    con.execute(
        f"COPY (SELECT CAST(7.0 AS DOUBLE) AS a, ST_Point(0, 0) AS geom, "
        f"ST_Point(1, 1) AS other LIMIT 0) "
        f"TO '{source.as_posix()}' (FORMAT parquet, geoparquet_version 'BOTH')"
    )
    con.close()
    out = tmp_path / "empty_out.parquet"
    field_calculation.calculate_field(str(source), str(out), "answer", "a + 1")
    from mapsmith import readers

    assert [name for name, _ in readers.native_geometry_columns(out)] == ["geom", "other"]
