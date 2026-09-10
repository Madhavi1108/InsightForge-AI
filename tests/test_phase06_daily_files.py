"""Phase 6 (spec Phase 13) - daily file split & data/ lifecycle directories.

Uses small row counts + ``tmp_path`` so tests run fast and never touch the real
``data/`` directory.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from scripts import generate_dataset as gd

SMALL = 3_000
SEED = 5


@pytest.fixture(scope="module")
def small_delivery() -> gd.DeliveryResult:
    return gd.build_delivery_dataset(rows=SMALL, seed=SEED)


# --------------------------------------------------------------------------- #
# split_daily_files
# --------------------------------------------------------------------------- #
def test_split_writes_one_file_per_day_and_conserves_rows(tmp_path, small_delivery):
    df = small_delivery.delivery_df
    out_dir = tmp_path / "incoming"
    written = gd.split_daily_files(df, out_dir)

    assert written, "expected at least one output file"
    total_rows = sum(len(pd.read_csv(p)) for p in written)
    assert total_rows == len(df)

    parsed = pd.to_datetime(df["Order_Date"], errors="coerce")
    n_valid_days = parsed.dt.date.nunique()
    non_invalid_files = [p for p in written if p.name != gd.INVALID_DATE_FILENAME]
    assert len(non_invalid_files) == n_valid_days

    if parsed.isna().any():
        assert (out_dir / gd.INVALID_DATE_FILENAME).exists()
    else:
        assert not (out_dir / gd.INVALID_DATE_FILENAME).exists()


def test_split_filenames_and_columns(tmp_path, small_delivery):
    df = small_delivery.delivery_df
    out_dir = tmp_path / "incoming"
    written = gd.split_daily_files(df, out_dir)

    for path in written:
        assert path.name.startswith("sales_")
        assert path.suffix == ".csv"
        assert list(pd.read_csv(path).columns) == gd.COLUMNS
        if path.name != gd.INVALID_DATE_FILENAME:
            # sales_YYYY_MM_DD.csv
            date_part = path.stem.removeprefix("sales_")
            dt.datetime.strptime(date_part, "%Y_%m_%d")


def test_split_round_trips_a_single_day(tmp_path):
    # A hand-built frame with two distinct clean dates, no bad dates.
    small = gd.generate_dataset(rows=500, seed=SEED)
    out_dir = tmp_path / "incoming"
    written = gd.split_daily_files(small, out_dir)
    assert not (out_dir / gd.INVALID_DATE_FILENAME).exists()

    first_day = pd.to_datetime(small["Order_Date"]).dt.date.min()
    path = out_dir / f"sales_{first_day:%Y_%m_%d}.csv"
    assert path in written

    expected = small.loc[pd.to_datetime(small["Order_Date"]).dt.date == first_day].reset_index(drop=True)
    actual = pd.read_csv(path)
    actual["Order_Date"] = pd.to_datetime(actual["Order_Date"])
    expected = expected.copy()
    expected["Order_Date"] = pd.to_datetime(expected["Order_Date"])
    assert_frame_equal(actual, expected, check_dtype=False)


def test_split_is_deterministic(tmp_path, small_delivery):
    df = small_delivery.delivery_df
    a = gd.split_daily_files(df, tmp_path / "a")
    b = gd.split_daily_files(df, tmp_path / "b")
    assert {p.name for p in a} == {p.name for p in b}
    for pa in a:
        pb = tmp_path / "b" / pa.name
        assert_frame_equal(pd.read_csv(pa), pd.read_csv(pb))


# --------------------------------------------------------------------------- #
# ensure_lifecycle_dirs
# --------------------------------------------------------------------------- #
def test_ensure_lifecycle_dirs_creates_all_five(tmp_path):
    created = gd.ensure_lifecycle_dirs(tmp_path)
    assert {d.name for d in created} == set(gd.LIFECYCLE_DIRS)
    for name in gd.LIFECYCLE_DIRS:
        d = tmp_path / name
        assert d.is_dir()
        assert (d / ".gitkeep").exists()


def test_ensure_lifecycle_dirs_is_idempotent(tmp_path):
    gd.ensure_lifecycle_dirs(tmp_path)
    gd.ensure_lifecycle_dirs(tmp_path)  # must not raise
    for name in gd.LIFECYCLE_DIRS:
        assert list((tmp_path / name).iterdir()) == [tmp_path / name / ".gitkeep"]


# --------------------------------------------------------------------------- #
# generate_demo_day
# --------------------------------------------------------------------------- #
def test_demo_day_is_deterministic():
    a = gd.generate_demo_day(seed=SEED, rows=SMALL)
    b = gd.generate_demo_day(seed=SEED, rows=SMALL)
    assert_frame_equal(a, b)


def test_demo_day_dates_and_referential_integrity():
    df = gd.generate_demo_day(seed=SEED, rows=SMALL)
    assert (pd.to_datetime(df["Order_Date"]).dt.date == gd.DEMO_DAY).all()

    rng = np.random.default_rng(SEED)
    customers, products, _ = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    assert set(df["Customer_ID"]).issubset(set(customers["Customer_ID"]))
    assert set(df["Product_ID"]).issubset(set(products["Product_ID"]))


def test_demo_day_is_clean_and_valid():
    df = gd.generate_demo_day(seed=SEED, rows=SMALL)
    rng = np.random.default_rng(SEED)
    customers, products, _ = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    gd.validate_consistency(df, customers, products)  # must not raise
    assert len(df) > 0


def test_demo_day_row_count_defaults_to_average_daily_volume():
    n_days_main = (gd.DEFAULT_END - gd.DEFAULT_START).days + 1
    expected = max(1, round(gd.DEFAULT_ROWS / n_days_main))
    df = gd.generate_demo_day(seed=SEED, rows=gd.DEFAULT_ROWS)
    assert len(df) == expected
