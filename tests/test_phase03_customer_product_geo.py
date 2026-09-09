"""Phase 3 (spec Phases 7-9) - verify weighted customer / product / geography
realism was added to the generator without breaking any Phase 2 invariant.

The generator stays fully deterministic, so every threshold below is a fixed
inequality with margin, not a probabilistic bound.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from scripts import generate_dataset as gd

SMALL = 3_000
MEDIUM = 25_000
SEED = 3

INTERNAL_COLUMNS = {
    "appeal", "purchase_weight", "is_repeat", "city_weight",
    "base_price", "cost_ratio",
}


@pytest.fixture(scope="module")
def med_df() -> pd.DataFrame:
    return gd.generate_dataset(rows=MEDIUM, seed=SEED)


@pytest.fixture(scope="module")
def refs():
    rng = np.random.default_rng(SEED)
    return gd.build_reference_data(
        rng, gd._n_customers(MEDIUM), gd._n_products(MEDIUM)
    )


# --------------------------------------------------------------------------- #
# Determinism / contract
# --------------------------------------------------------------------------- #
def test_phase03_generation_is_deterministic():
    a = gd.generate_dataset(rows=SMALL, seed=SEED)
    b = gd.generate_dataset(rows=SMALL, seed=SEED)
    assert_frame_equal(a, b)


def test_reference_data_deterministic():
    a = gd.build_reference_data(
        np.random.default_rng(SEED), gd._n_customers(MEDIUM), gd._n_products(MEDIUM)
    )
    b = gd.build_reference_data(
        np.random.default_rng(SEED), gd._n_customers(MEDIUM), gd._n_products(MEDIUM)
    )
    for left, right in zip(a, b):
        assert_frame_equal(left, right)


def test_csv_still_22_columns_no_internal_weights(med_df):
    assert list(med_df.columns) == gd.COLUMNS
    assert len(gd.COLUMNS) == 22
    assert not (INTERNAL_COLUMNS & set(med_df.columns))


# --------------------------------------------------------------------------- #
# Product popularity (spec Phase 8)
# --------------------------------------------------------------------------- #
def test_product_appeal_present_and_dispersed(refs):
    _, products, _ = refs
    assert "appeal" in products.columns
    assert (products["appeal"] > 0).all()
    cov = products["appeal"].std() / products["appeal"].mean()
    assert cov > 0.5, f"appeal barely varies (CoV={cov:.2f})"


def test_product_popularity_skew_top_decile_revenue_share(med_df):
    rev = med_df.groupby("Product_ID")["Revenue"].sum().sort_values(ascending=False)
    k = math.ceil(0.10 * len(rev))
    top_share = rev.head(k).sum() / rev.sum()
    bottom_share = rev.tail(k).sum() / rev.sum()
    assert top_share >= 0.25, f"no popularity concentration (top decile={top_share:.2%})"
    assert top_share > 5 * bottom_share


# --------------------------------------------------------------------------- #
# Repeat customers / segment behaviour (spec Phase 7)
# --------------------------------------------------------------------------- #
def test_repeat_flag_minority_and_more_active(refs):
    customers, _, _ = refs
    frac = customers["is_repeat"].mean()
    assert 0.05 < frac < 0.30, f"repeat fraction out of range ({frac:.2%})"
    repeat_w = customers.loc[customers["is_repeat"], "purchase_weight"].mean()
    other_w = customers.loc[~customers["is_repeat"], "purchase_weight"].mean()
    assert repeat_w > 2 * other_w


def test_repeat_customers_order_count_skewed(med_df):
    oc = med_df.groupby("Customer_ID").size()
    assert oc.max() >= 15
    assert oc.max() >= 5 * oc.mean()
    k = max(1, math.ceil(0.05 * oc.size))
    top_share = oc.sort_values(ascending=False).head(k).sum() / len(med_df)
    assert top_share >= 0.20, f"top-5% customers only place {top_share:.2%} of orders"


def test_segment_influences_basket_and_discount(med_df):
    by_seg = med_df.groupby("Customer_Segment")[["Quantity", "Discount"]].mean()
    assert by_seg.loc["Corporate", "Quantity"] > by_seg.loc["Consumer", "Quantity"] + 0.1
    assert by_seg.loc["Corporate", "Discount"] > by_seg.loc["Consumer", "Discount"] + 0.005


# --------------------------------------------------------------------------- #
# Geography (spec Phase 9)
# --------------------------------------------------------------------------- #
def test_geography_expanded_min_counts():
    tuples = gd.valid_geo_tuples()
    assert len(tuples) >= 45
    states = {s for _, s, _ in tuples}
    assert len(states) >= 20
    for region, region_states in gd.GEOGRAPHY.items():
        assert len(region_states) >= 3, f"{region} has < 3 states"


def test_geo_hierarchy_still_valid(med_df):
    seen = set(map(tuple, med_df[["Region", "State", "City"]].to_numpy()))
    assert seen.issubset(gd.valid_geo_tuples())


def test_metro_cities_have_higher_order_share(med_df):
    share = med_df["City"].value_counts(normalize=True)
    is_metro = share.index.isin(gd.METRO_CITIES)
    assert share[is_metro].mean() > share[~is_metro].mean()
    assert share[is_metro].sum() >= 0.25


def test_metro_orders_ship_no_slower(med_df):
    shipped = med_df.dropna(subset=["Shipping_Days"])
    metro = shipped["City"].isin(gd.METRO_CITIES)
    assert shipped.loc[metro, "Shipping_Days"].mean() <= shipped.loc[~metro, "Shipping_Days"].mean()
    assert shipped["Shipping_Days"].between(0, 21).all()


# --------------------------------------------------------------------------- #
# Phase 2 invariants still hold
# --------------------------------------------------------------------------- #
def test_stable_attributes_still_hold(med_df):
    for key, attrs in (
        ("Customer_ID", ["Customer_Name", "Customer_Segment"]),
        ("Product_ID", ["Product_Name", "Category", "Sub_Category"]),
    ):
        counts = med_df.groupby(key)[attrs].nunique()
        assert (counts == 1).all().all(), f"{key} has drifting {attrs}"


def test_all_phase02_invariants_hold():
    rng = np.random.default_rng(SEED)
    customers, products, geo = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    orders = gd.generate_orders(
        rng, SMALL, customers, products, geo, gd.DEFAULT_START, gd.DEFAULT_END
    )
    df = gd.apply_business_model(orders)
    gd.validate_consistency(df, customers, products)  # must not raise

    assert np.allclose(
        df["Revenue"],
        np.round(df["Quantity"] * df["Unit_Price"] * (1.0 - df["Discount"]), 2),
        atol=0.01,
    )
    assert np.allclose(df["Profit"], np.round(df["Revenue"] - df["Cost"], 2), atol=0.01)
    assert df[gd.MANDATORY_COLUMNS].notna().all().all()
    assert df["Discount"].between(0.0, 0.8).all()
    assert df["Return_Status"].eq("Returned").equals(df["Order_Status"].eq("Returned"))


def test_referential_integrity_phase03():
    rng = np.random.default_rng(SEED)
    customers, products, geo = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    orders = gd.generate_orders(
        rng, SMALL, customers, products, geo, gd.DEFAULT_START, gd.DEFAULT_END
    )
    assert set(orders["Customer_ID"]).issubset(set(customers["Customer_ID"]))
    assert set(orders["Product_ID"]).issubset(set(products["Product_ID"]))
