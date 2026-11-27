"""What a vector read may cost, estimated before anything is materialised.

Reading costs memory by the row and by the decoded byte. Every format but one
pays for both on disk, so the size of the file bounds the read; Parquet stores
a repeated value once. Measured through `readers.read_vector`: five million
identical points are 80 kB on disk and 2 GB to open, and 100 000 identical
polygons of 100 vertices are 32 kB and 541 MB.

The first rule written for this counted rows against bytes on disk, and the
audit took it apart: it refused regular grids and MapSmith's own outputs, did
not see the size of a geometry, and was not consulted by the engines that
open a file themselves. These tests are the cases it found.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from mapsmith import limits, readers

CRS = "EPSG:32632"
POINT_WKB = 21  # one byte of order, four of type, two doubles


def _repeated(path: Path, rows: int) -> str:
    """`rows` identical points with identical attributes: kilobytes on disk."""
    frame = gpd.GeoDataFrame(
        {"value": [12.5] * rows, "name": ["one station"] * rows},
        geometry=[Point(500000.0, 4649776.0)] * rows,
        crs=CRS,
    )
    frame.to_parquet(path, compression="zstd")
    return str(path)


def _scattered(path: Path, rows: int) -> str:
    """`rows` different points: what an ordinary layer looks like on disk."""
    rng = np.random.default_rng(7)
    frame = gpd.GeoDataFrame(
        {"value": rng.random(rows)},
        geometry=gpd.points_from_xy(rng.uniform(4e5, 6e5, rows), rng.uniform(4.5e6, 4.7e6, rows)),
        crs=CRS,
    )
    frame.to_parquet(path)
    return str(path)


def test_the_estimate_is_a_cost_per_row_and_a_cost_per_decoded_byte():
    assert limits.estimated_read_memory(1000, 100) == 1000 * 100 + 100 * 8
    # A byte of a column of numbers costs less than one of a value that becomes an object.
    assert limits.estimated_read_memory(1000, 100, 40) == 1000 * 100 + 100 * 8 + 40 * 5


def test_the_allowance_is_the_budget_or_what_the_bytes_on_disk_entitle(monkeypatch):
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    assert limits.input_memory_allowance(10) == 1_000_000
    assert limits.input_memory_allowance(100_000) == 3_200_000
    limits.refuse_too_costly_a_read(10_000, 0, 10, "f.parquet")  # exactly the budget
    with pytest.raises(ValueError, match=r"holds 10,001 rows in 10 bytes on disk"):
        limits.refuse_too_costly_a_read(10_001, 0, 10, "f.parquet")
    limits.refuse_too_costly_a_read(10_001, 0, 100_000, "f.parquet")  # the file pays for it


def test_a_file_of_repeated_rows_is_refused_before_any_row_is_read(monkeypatch, tmp_path):
    """5000 rows of two values that become objects are 1.5 MB for being objects alone: over a
    budget of 1 MB, in a file of kilobytes."""
    path = _repeated(tmp_path / "same.parquet", 5000)
    size = Path(path).stat().st_size
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    # The premise, or the test is about nothing: the file does not pay for itself.
    assert limits.input_memory_allowance(size) == 1_000_000

    def never(*args, **kwargs):
        raise AssertionError("the file was opened for its rows")

    monkeypatch.setattr(readers.gpd, "read_parquet", never)
    with pytest.raises(ValueError, match=r"holds 5,000 rows") as refusal:
        readers.read_vector(path)
    assert limits.MAX_INPUT_MEMORY_ENV in str(refusal.value)


def test_few_rows_of_one_large_geometry_are_counted_by_what_they_decode_to(monkeypatch, tmp_path):
    """200 rows are 0.02 MB by the row. The polygon they repeat is 32 kB of WKB,
    6.4 MB decoded and 32 MB to hold: the file is the size of one of them."""
    ring = [(500000.0 + i, 4649776.0 + (i % 7)) for i in range(2000)]
    polygon = Polygon(ring)
    path = tmp_path / "big.parquet"
    gpd.GeoDataFrame(geometry=[polygon] * 200, crs=CRS).to_parquet(path, compression="zstd")
    decoded = 200 * len(polygon.wkb)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "5000000")
    assert limits.estimated_read_memory(200, 0) < 5_000_000 < limits.estimated_read_memory(200, decoded)
    assert limits.input_memory_allowance(path.stat().st_size) == 5_000_000
    with pytest.raises(ValueError, match=r"holds 200 rows and (at least )?[\d,]+ bytes of values"):
        readers.read_vector(str(path))


def test_an_ordinary_file_over_the_budget_is_read_because_its_size_pays_for_it(
    monkeypatch, tmp_path
):
    path = _scattered(tmp_path / "scattered.parquet", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    estimate = limits.estimated_read_memory(5000, 5000 * POINT_WKB)
    assert estimate > 1_000_000
    assert limits.input_memory_allowance(Path(path).stat().st_size) >= estimate
    assert len(readers.read_vector(path)) == 5000


def test_a_regular_grid_within_the_budget_is_read_however_well_it_compresses(
    monkeypatch, tmp_path
):
    """The case the first rule refused: honest, low-entropy, small on disk. 2500
    lattice points with one constant attribute are 1.1 MB by the estimate."""
    xs, ys = np.meshgrid(np.arange(50) * 10.0 + 500000.0, np.arange(50) * 10.0 + 4649000.0)
    path = tmp_path / "grid.parquet"
    gpd.GeoDataFrame(
        {"class": ["forest"] * 2500},
        geometry=gpd.points_from_xy(xs.ravel(), ys.ravel()),
        crs=CRS,
    ).to_parquet(path, compression="zstd")
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "2000000")
    estimate = limits.estimated_read_memory(2500, 2500 * (POINT_WKB + len("forest")))
    assert limits.input_memory_allowance(path.stat().st_size) == 2_000_000 >= estimate
    assert len(readers.read_vector(str(path))) == 2500


def test_a_directory_is_counted_from_the_files_that_will_be_read_not_from_their_names(
    monkeypatch, tmp_path
):
    """The dataset reader opens every file in the directory. A count over
    `*.parquet` passed one row and read 5001 (found by the audit)."""
    folder = tmp_path / "parts.parquet"
    folder.mkdir()
    _repeated(folder / "small.parquet", 1)
    _repeated(folder / "big.pq", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    with pytest.raises(ValueError, match=r"holds 5,001 rows"):
        readers.read_vector(str(folder))
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "100000000")
    assert len(readers.read_vector(str(folder))) == 5001


def test_verification_reads_back_an_output_whatever_it_holds(monkeypatch, tmp_path):
    """The limit is on inputs. Refusing to read an output back would lose its
    checks, so that read is not limited — and an output can be larger than its
    inputs, which `SECURITY.md` declares rather than this test pretending not."""
    path = _repeated(tmp_path / "output.parquet", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    assert len(readers.read_vector_or_table(path)) == 5000


def test_an_operation_refuses_through_the_reader_and_writes_nothing(monkeypatch, tmp_path):
    pytest.importorskip("rasterio")
    from mapsmith.engines import interpolation

    path = _repeated(tmp_path / "same.parquet", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    out = tmp_path / "surface.tif"
    with pytest.raises(ValueError, match=limits.MAX_INPUT_MEMORY_ENV):
        interpolation.tin_interpolation(path, str(out), "value", 10.0)
    assert not out.exists()
    assert not Path(f"{out}.provenance.json").exists()


def test_the_engines_that_open_a_parquet_themselves_ask_first(monkeypatch, tmp_path):
    """`calculate_field` and the DuckDB join stream their input and read their
    output back whole: two million identical rows in 30 kB went through
    `calculate_field` to `verified: true` at 926 MB (found by the audit)."""
    pytest.importorskip("duckdb")
    from mapsmith.engines import duckdb_engine, field_calculation

    path = _repeated(tmp_path / "same.parquet", 5000)
    other = _scattered(tmp_path / "other.parquet", 10)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000")
    out = tmp_path / "out.parquet"
    with pytest.raises(ValueError, match=limits.MAX_INPUT_MEMORY_ENV):
        field_calculation.calculate_field(path, str(out), "double", "value * 2")
    with pytest.raises(ValueError, match=limits.MAX_INPUT_MEMORY_ENV):
        duckdb_engine.spatial_join(path, other, str(out))
    with pytest.raises(ValueError, match=limits.MAX_INPUT_MEMORY_ENV):
        duckdb_engine.spatial_join(other, path, str(out))
    assert not out.exists()


def test_a_table_of_many_numbers_per_row_is_counted_from_the_footer(monkeypatch, tmp_path):
    """2000 rows of fifty float64 are 0.2 MB by the row and 0.8 MB of values,
    4.2 MB to hold: under a budget of 2 MB by the rows, over it by what is in
    them. The first estimate counted only binary and string columns, and a
    table of two hundred numbers a row cost ten times what it said (audit)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "wide.parquet"
    pq.write_table(
        pa.table({f"c{i}": np.full(2000, 1.5) for i in range(50)}), path, compression="zstd"
    )
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "2000000")
    assert limits.estimated_read_memory(2000, 0) < 2_000_000
    assert limits.estimated_read_memory(2000, 0, 2000 * 50 * 8) == 4_200_000
    with pytest.raises(ValueError, match=r"holds 2,000 rows and at least 800,000 bytes of values"):
        readers.refuse_costly_parquet(str(path))


def test_values_inside_a_list_and_values_typed_as_a_dictionary_are_counted(monkeypatch, tmp_path):
    """Neither is a binary column at the top level, and both were taken for
    nothing: a list of binaries 496 times over the estimate, a geometry stored
    as a dictionary of one polygon without limit (audit)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    value = b"x" * 1000
    nested = tmp_path / "nested.parquet"
    pq.write_table(
        pa.table({"parts": pa.array([[value] * 10] * 300, pa.list_(pa.binary()))}),
        nested, compression="zstd",
    )
    typed = tmp_path / "typed.parquet"
    pq.write_table(
        pa.table({"g": pa.DictionaryArray.from_arrays(pa.array([0] * 3000, pa.int32()), [value])}),
        typed, compression="zstd",
    )
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "5000000")
    for path, rows in ((nested, 300), (typed, 3000)):
        # 3 MB of values in a file of kilobytes, and few enough rows to pass on those.
        assert path.stat().st_size < 50_000
        assert limits.estimated_read_memory(rows, 0) < 5_000_000
        with pytest.raises(ValueError, match=rf"holds {rows:,} rows and .*bytes of values"):
            readers.refuse_costly_parquet(str(path))


def test_a_footer_that_disagrees_with_itself_is_refused(monkeypatch):
    """The fixture is an honest file of 2000 rows, each a 50 kB value stored
    once, with two integers of its footer rewritten: the file and its row group
    declare one row, the column still two thousand values. A reader goes by the
    column and read all of them, 209 MB; a count that went by the row group saw
    one and admitted it at a limit of 100 MB (audit, after the first fix, which
    took the larger count for the rows and still read the values of one)."""
    path = Path(__file__).parent / "data" / "footer_declares_one_row_of_2000.parquet"
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "100000000")
    with pytest.raises(
        ValueError, match=r"declares a row count of 1 and its column '\w+' holds 2,000 values"
    ):
        readers.refuse_costly_parquet(str(path))
    with pytest.raises(ValueError, match="cannot be sized before it is read"):
        readers.read_vector(str(path))


def test_a_decimal_is_priced_as_the_object_it_becomes(monkeypatch, tmp_path):
    """Four bytes on disk and a Python `Decimal` in memory, about 150 bytes: a
    hundred decimal columns estimated at 73 MB read at 456 (audit). 20 columns
    of 2000 are 40 000 values, 6.1 MB at 19 counted bytes each and 0.8 at 4."""
    import decimal

    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "decimals.parquet"
    column = pa.array([decimal.Decimal("12.50")] * 2000, pa.decimal128(9, 2))
    pq.write_table(pa.table({f"d{i}": column for i in range(20)}), path, compression="zstd")
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "3000000")
    assert limits.estimated_read_memory(2000, 0, 40_000 * 4) < 3_000_000
    assert limits.estimated_read_memory(2000, 40_000 * 19) > 3_000_000
    with pytest.raises(ValueError, match=r"holds 2,000 rows and at least 760,000 bytes of values"):
        readers.refuse_costly_parquet(str(path))


def test_a_large_value_repeated_inside_a_struct_is_counted_from_its_dictionary(
    monkeypatch, tmp_path
):
    """Not a binary column at the top level, so the first version read it
    plainly, 65 536 rows at a time: 8000 rows of a 50 kB value in 130 kB of
    file took the CHECK to 820 MB before it refused (audit). 400 rows are
    20 MB of values; the count must come to exactly that, which it only does
    cheaply by adding up the dictionary's one length 400 times."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    blob = b"y" * 50_000
    path = tmp_path / "struct.parquet"
    pq.write_table(
        pa.table({"st": pa.array([{"blob": blob, "i": 1}] * 400)}), path, compression="zstd"
    )
    assert path.stat().st_size < 100_000
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "10000000")
    with pytest.raises(ValueError, match=r"holds 400 rows and (at least )?[\d,]+ bytes of values"):
        readers.refuse_costly_parquet(str(path))
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1000000000")
    readers.refuse_costly_parquet(str(path))  # 0.12 MB of rows and 160 MB of values: admitted


def test_the_fields_of_a_struct_and_times_of_day_are_priced_as_objects(monkeypatch, tmp_path):
    """A struct becomes a dictionary a row and a time a `datetime.time`: two
    hundred booleans in a struct read at 5.6 times their estimate and a hundred
    `time32` columns at 2.8 (audit). 2000 rows of fifty booleans in a struct
    are 100 000 values, 0.1 MB by their width and 0.8 at eight bytes each;
    2000 rows of twenty times are 40 000 values, 0.16 MB and 0.76 at nineteen."""
    import datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    struct = tmp_path / "struct_of_booleans.parquet"
    pq.write_table(
        pa.table({"s": pa.array([{f"b{i}": True for i in range(50)}] * 2000)}),
        struct, compression="zstd",
    )
    times = tmp_path / "times.parquet"
    column = pa.array([datetime.time(12, 30)] * 2000, pa.time32("s"))
    pq.write_table(pa.table({f"t{i}": column for i in range(20)}), times, compression="zstd")
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "3000000")
    assert limits.estimated_read_memory(2000, 0, 100_000) < 3_000_000
    with pytest.raises(ValueError, match=r"holds 2,000 rows and at least 800,000 bytes of values"):
        readers.refuse_costly_parquet(str(struct))
    with pytest.raises(ValueError, match=r"holds 2,000 rows and at least 760,000 bytes of values"):
        readers.refuse_costly_parquet(str(times))


def test_a_counting_batch_holds_a_thousand_rows_whatever_the_footer_says_they_weigh():
    """The batch is sized from the footer's bytes per row, and a footer
    rewritten to declare none made the check decode 6000 rows of 50 kB at once,
    775 MB (audit). The cap on rows is the part no footer can move."""
    assert readers._COUNTING_BATCH_ROWS <= 1024


def test_three_million_named_points_would_be_entitled_by_their_own_size(monkeypatch, tmp_path):
    """An ordinary layer must pass at any size, and the arithmetic that decides
    it is checked here on a small one: 5000 scattered points with a distinct
    name and a number. Their estimate over their size on disk has to stay under
    the entitlement, with room — at a factor of 24 it was 26, and three million
    of them were refused (found while re-measuring after the fifth audit)."""
    rng = np.random.default_rng(11)
    path = tmp_path / "named.parquet"
    gpd.GeoDataFrame(
        {"value": rng.random(5000), "name": [f"station {i}" for i in range(5000)]},
        geometry=gpd.points_from_xy(rng.uniform(4e5, 6e5, 5000), rng.uniform(4.5e6, 4.7e6, 5000)),
        crs=CRS,
    ).to_parquet(path)
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "1")  # nothing passes on the budget
    readers.refuse_costly_parquet(str(path))  # passes on what the file occupies


def test_lists_and_structs_are_counted_as_the_objects_they_become(monkeypatch, tmp_path):
    """The footer counts the numbers at the bottom of a nested column and not
    the lists and structs that hold them, which is what they cost. Three
    million structs of one small number in a list were 1.1 MB on disk,
    estimated at 129 MB by their widths, admitted at a limit of 200 and read
    at 700 (audit). 3000 rows, each a list of ten structs: 30 000 structs at
    230 bytes and 3000 lists at 170 are 7.4 MB before a number is counted."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "list_of_structs.parquet"
    kind = pa.list_(pa.struct([("a", pa.int8())]))
    pq.write_table(
        pa.table({"parts": pa.array([[{"a": 1}] * 10] * 3000, kind)}), path, compression="zstd"
    )
    assert path.stat().st_size < 20_000
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "5000000")
    # By rows and widths alone the file is far under the budget.
    assert limits.estimated_read_memory(3000, 0, 30_000 * 4) < 1_000_000
    with pytest.raises(ValueError, match=r"holds 3,000 rows and (at least )?[\d,]+ bytes of values"):
        readers.refuse_costly_parquet(str(path))
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "20000000")
    readers.refuse_costly_parquet(str(path))


def test_dictionaries_are_looked_at_a_few_columns_at_a_time_with_the_same_answer(
    monkeypatch, tmp_path
):
    """One look at every dictionary of a row group cost 990 MB on eighty
    columns of 65 536 short values (audit). The columns are looked at in groups
    that fit a budget, and how they are grouped must not change what is found:
    forty columns of a repeated 5 kB value, one column at a time and all at
    once."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "many_dictionaries.parquet"
    column = pa.array([b"z" * 5000] * 200)
    pq.write_table(pa.table({f"c{i:02d}": column for i in range(40)}), path, compression="zstd")
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, "100000000")
    messages = []
    for budget in (1, 10**12):
        monkeypatch.setattr(readers, "_DICTIONARY_PROBE_BYTES", budget)
        with pytest.raises(ValueError) as refusal:
            readers.refuse_costly_parquet(str(path))
        messages.append(str(refusal.value))
    assert messages[0] == messages[1]
    assert "holds 200 rows and" in messages[0]


def test_counting_opens_a_file_a_fixed_number_of_times_however_many_row_groups(
    monkeypatch, tmp_path
):
    """Opening the file again for every row group made a thousand groups take
    9 s to count (audit). Two hundred groups, and at most three readers."""
    import pyarrow.parquet as pq

    path = tmp_path / "groups.parquet"
    frame = gpd.read_parquet(_scattered(tmp_path / "source.parquet", 2000))
    frame.to_parquet(path, row_group_size=10)
    assert pq.ParquetFile(path).metadata.num_row_groups == 200
    opened = []
    real = pq.ParquetFile

    def counting(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", counting)
    readers.refuse_costly_parquet(str(path))
    assert len(opened) <= 3


def test_the_size_on_disk_is_what_the_file_occupies(tmp_path):
    """Never more than its length: a sparse file is long and holds nothing."""
    path = Path(_scattered(tmp_path / "scattered.parquet", 5000))
    assert 0 < readers._bytes_on_disk(path) <= path.stat().st_size


def test_the_default_is_two_gigabytes():
    assert limits.max_input_memory() == 2_000_000_000


@pytest.mark.parametrize("raw", ["lots", "0", "-3"])
def test_a_budget_that_is_not_a_positive_whole_number_is_refused(monkeypatch, raw):
    monkeypatch.setenv(limits.MAX_INPUT_MEMORY_ENV, raw)
    with pytest.raises(ValueError, match=limits.MAX_INPUT_MEMORY_ENV):
        limits.max_input_memory()
