"""Phase 2 - verify the retail data generator and the business data model."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from scripts import generate_dataset as gd

SMALL = 3_000


@pytest.fixture(scope="module")
def small_df() -> pd.DataFrame:
    return gd.generate_dataset(rows=SMALL, seed=1)


def test_columns_exact_order(small_df):
    assert list(small_df.columns) == gd.COLUMNS
    assert len(gd.COLUMNS) == 22


def test_row_count_at_least_requested(small_df):
    assert len(small_df) >= SMALL


def test_generation_is_deterministic():
    a = gd.generate_dataset(rows=SMALL, seed=1)
    b = gd.generate_dataset(rows=SMALL, seed=1)
    assert_frame_equal(a, b)


def test_different_seed_changes_data():
    a = gd.generate_dataset(rows=SMALL, seed=1)
    b = gd.generate_dataset(rows=SMALL, seed=2)
    assert not a.equals(b)


def test_revenue_formula(small_df):
    expected = np.round(
        small_df["Quantity"] * small_df["Unit_Price"] * (1.0 - small_df["Discount"]), 2
    )
    assert np.allclose(small_df["Revenue"], expected, atol=0.01)


def test_profit_formula(small_df):
    expected = np.round(small_df["Revenue"] - small_df["Cost"], 2)
    assert np.allclose(small_df["Profit"], expected, atol=0.01)


def test_value_ranges(small_df):
    assert (small_df["Revenue"] >= 0).all()
    assert (small_df["Quantity"] >= 1).all()
    assert small_df["Discount"].between(0.0, 0.8).all()
    assert (small_df["Unit_Price"] > 0).all()


def test_mandatory_columns_non_null(small_df):
    assert small_df[gd.MANDATORY_COLUMNS].notna().all().all()


def test_shipping_days_null_only_for_unshipped(small_df):
    null_rows = small_df[small_df["Shipping_Days"].isna()]
    assert set(null_rows["Order_Status"].unique()).issubset({"Pending", "Cancelled"})


def test_geography_hierarchy_valid(small_df):
    seen = set(map(tuple, small_df[["Region", "State", "City"]].to_numpy()))
    assert seen.issubset(gd.valid_geo_tuples())


def test_category_hierarchy_valid(small_df):
    seen = set(map(tuple, small_df[["Category", "Sub_Category"]].to_numpy()))
    assert seen.issubset(gd.valid_category_pairs())


def test_referential_integrity():
    rng = np.random.default_rng(1)
    customers, products, geo = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    orders = gd.generate_orders(
        rng, SMALL, customers, products, geo, gd.DEFAULT_START, gd.DEFAULT_END
    )
    assert set(orders["Customer_ID"]).issubset(set(customers["Customer_ID"]))
    assert set(orders["Product_ID"]).issubset(set(products["Product_ID"]))


def test_return_status_consistent_with_order_status(small_df):
    returned = small_df["Return_Status"].eq("Returned")
    assert returned.equals(small_df["Order_Status"].eq("Returned"))


def test_order_date_within_window(small_df):
    dates = pd.to_datetime(small_df["Order_Date"])
    assert dates.min().date() >= gd.DEFAULT_START
    assert dates.max().date() <= gd.DEFAULT_END


def test_stable_customer_and_product_attributes(small_df):
    for key, attrs in (
        ("Customer_ID", ["Customer_Name", "Customer_Segment"]),
        ("Product_ID", ["Product_Name", "Category", "Sub_Category"]),
    ):
        counts = small_df.groupby(key)[attrs].nunique()
        assert (counts == 1).all().all(), f"{key} has drifting {attrs}"


def test_full_size_smoke():
    df = gd.generate_dataset(rows=150_000, seed=gd.DEFAULT_SEED)
    assert len(df) >= 150_000
    assert int(df[gd.MANDATORY_COLUMNS].isna().sum().sum()) == 0
    assert df["Revenue"].sum() > 0
