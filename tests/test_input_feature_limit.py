"""The rows a file may hold for the bytes it occupies, counted before any is read.

Reading costs memory by the row. Every format but one pays for a row on disk
too, so the size of the file bounds the read; Parquet stores a repeated value
once, and measured through `readers.read_vector` a GeoParquet of 80 kB held
five million rows and took 2 GB to open.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from mapsmith import limits, readers

CRS = "EPSG:32632"


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


def test_the_cap_is_the_limit_or_one_row_for_every_sixteen_bytes(monkeypatch):
    """Closed form: a limit of 1000, and 16 bytes on disk for each row beyond it."""
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    limits.refuse_too_many_features(1000, 0, "f.parquet")  # at the limit, any size
    limits.refuse_too_many_features(1001, 16 * 1001, "f.parquet")  # paid for, to the byte
    with pytest.raises(ValueError, match=r"declares 1,001 rows in 16,015 bytes"):
        limits.refuse_too_many_features(1001, 16 * 1001 - 1, "f.parquet")


def test_a_file_of_repeated_rows_is_refused_before_any_row_is_read(monkeypatch, tmp_path):
    path = _repeated(tmp_path / "same.parquet", 5000)
    # The premise, or the test is about nothing: far fewer bytes than the rows need.
    assert Path(path).stat().st_size < limits.INPUT_BYTES_PER_FEATURE * 5000 // 4
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")

    def never(*args, **kwargs):
        raise AssertionError("the file was opened for its rows")

    monkeypatch.setattr(readers.gpd, "read_parquet", never)
    with pytest.raises(ValueError, match=r"declares 5,000 rows") as refusal:
        readers.read_vector(path)
    assert limits.MAX_INPUT_FEATURES_ENV in str(refusal.value)
    assert "its limit is 1,000 for this file" in str(refusal.value)


def test_an_ordinary_file_over_the_limit_is_read_because_its_size_pays_for_it(
    monkeypatch, tmp_path
):
    path = _scattered(tmp_path / "scattered.parquet", 5000)
    assert Path(path).stat().st_size >= limits.INPUT_BYTES_PER_FEATURE * 5000
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    assert len(readers.read_vector(path)) == 5000


def test_under_the_limit_a_file_of_repeated_rows_is_read(monkeypatch, tmp_path):
    path = _repeated(tmp_path / "few.parquet", 500)
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    assert len(readers.read_vector(path)) == 500


def test_a_directory_of_parquet_files_is_one_dataset_and_counted_as_one(monkeypatch, tmp_path):
    """Two files of 3000 rows are 6000: over a limit of 5000 together, under it apart.

    The directory was readable before the limit existed, and the first version
    of the check broke it by asking a directory for a Parquet footer.
    """
    folder = tmp_path / "parts.parquet"
    folder.mkdir()
    _repeated(folder / "a.parquet", 3000)
    _repeated(folder / "b.parquet", 3000)
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "5000")
    with pytest.raises(ValueError, match=r"declares 6,000 rows"):
        readers.read_vector(str(folder))
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "6000")
    assert len(readers.read_vector(str(folder))) == 6000


def test_a_directory_is_counted_from_the_files_that_will_be_read_not_from_their_names(
    monkeypatch, tmp_path
):
    """The dataset reader opens every file in the directory. A count over
    `*.parquet` passed one row and read 5001 (found by the audit)."""
    folder = tmp_path / "parts.parquet"
    folder.mkdir()
    _repeated(folder / "small.parquet", 1)
    _repeated(folder / "big.pq", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    with pytest.raises(ValueError, match=r"declares 5,001 rows"):
        readers.read_vector(str(folder))


def test_verification_reads_back_an_output_whatever_its_rows(monkeypatch, tmp_path):
    """The limit is on inputs. An output is as large as the operation that was
    allowed to write it, and refusing to read it back would lose the checks."""
    path = _repeated(tmp_path / "output.parquet", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    assert len(readers.read_vector_or_table(path)) == 5000


def test_an_operation_refuses_through_the_reader_and_writes_nothing(monkeypatch, tmp_path):
    pytest.importorskip("rasterio")
    from mapsmith.engines import interpolation

    path = _repeated(tmp_path / "same.parquet", 5000)
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, "1000")
    out = tmp_path / "surface.tif"
    with pytest.raises(ValueError, match=limits.MAX_INPUT_FEATURES_ENV):
        interpolation.tin_interpolation(path, str(out), "value", 10.0)
    assert not out.exists()
    assert not Path(f"{out}.provenance.json").exists()


def test_the_default_is_a_million_rows():
    assert limits.max_input_features() == 1_000_000


@pytest.mark.parametrize("raw", ["lots", "0", "-3"])
def test_a_limit_that_is_not_a_positive_whole_number_is_refused(monkeypatch, raw):
    monkeypatch.setenv(limits.MAX_INPUT_FEATURES_ENV, raw)
    with pytest.raises(ValueError, match=limits.MAX_INPUT_FEATURES_ENV):
        limits.max_input_features()
