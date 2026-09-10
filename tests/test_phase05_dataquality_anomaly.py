"""Phase 5 (spec Phases 11-12) - verify controlled data-quality injection and the
planted business anomaly, without disturbing the clean generation path.

The generator stays fully deterministic, so every threshold below is a fixed
inequality with margin, not a probabilistic bound.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from scripts import generate_dataset as gd

SMALL = 3_000
MEDIUM = 25_000
ANOMALY_ROWS = 80_000
SEED = 5

INTERNAL_COLUMNS = {
    "appeal", "purchase_weight", "is_repeat", "city_weight",
    "base_price", "cost_ratio",
}
ISSUE_TYPES = {kind for kind, _, _ in gd.DQ_ISSUE_PLAN}


@pytest.fixture(scope="module")
def deliv_med() -> gd.DeliveryResult:
    return gd.build_delivery_dataset(rows=MEDIUM, seed=SEED)


@pytest.fixture(scope="module")
def deliv_anom() -> gd.DeliveryResult:
    return gd.build_delivery_dataset(rows=ANOMALY_ROWS, seed=SEED)


@pytest.fixture(scope="module")
def med_refs():
    rng = np.random.default_rng(SEED)
    return gd.build_reference_data(
        rng, gd._n_customers(MEDIUM), gd._n_products(MEDIUM)
    )


# --------------------------------------------------------------------------- #
# Determinism / contract
# --------------------------------------------------------------------------- #
def test_delivery_build_is_deterministic():
    a = gd.build_delivery_dataset(rows=SMALL, seed=SEED)
    b = gd.build_delivery_dataset(rows=SMALL, seed=SEED)
    assert_frame_equal(a.delivery_df, b.delivery_df)
    assert_frame_equal(a.issue_log, b.issue_log)


def test_delivery_structure_preserved(deliv_med):
    assert list(deliv_med.delivery_df.columns) == gd.COLUMNS
    assert len(gd.COLUMNS) == 22
    assert not (INTERNAL_COLUMNS & set(deliv_med.delivery_df.columns))
    assert deliv_med.delivery_df["Order_ID"].notna().all()
    gd.validate_delivery_structure(deliv_med.delivery_df)  # must not raise


def test_clean_path_untouched():
    # generate_dataset() is still clean + validated.
    rng = np.random.default_rng(SEED)
    customers, products, geo = gd.build_reference_data(
        rng, gd._n_customers(SMALL), gd._n_products(SMALL)
    )
    orders = gd.generate_orders(
        rng, SMALL, customers, products, geo, gd.DEFAULT_START, gd.DEFAULT_END
    )
    df = gd.apply_business_model(orders)
    gd.validate_consistency(df, customers, products)  # must not raise

    # No-injection delivery build == the clean frame, empty issue log.
    res = gd.build_delivery_dataset(
        rows=SMALL, seed=SEED, inject_issues=False, inject_anomaly=False
    )
    assert_frame_equal(res.delivery_df, res.clean_df)
    assert len(res.issue_log) == 0
    assert res.counts["delivered_rows"] == res.counts["clean_rows"]


def test_all_prior_invariants_hold():
    a = gd.generate_dataset(rows=SMALL, seed=SEED)
    b = gd.generate_dataset(rows=SMALL, seed=SEED)
    assert_frame_equal(a, b)
    assert np.allclose(
        a["Revenue"],
        np.round(a["Quantity"] * a["Unit_Price"] * (1.0 - a["Discount"]), 2),
        atol=0.01,
    )
    assert np.allclose(a["Profit"], np.round(a["Revenue"] - a["Cost"], 2), atol=0.01)
    assert a[gd.MANDATORY_COLUMNS].notna().all().all()
    assert a["Discount"].between(0.0, 0.8).all()


# --------------------------------------------------------------------------- #
# Data-quality injection (spec Phase 11)
# --------------------------------------------------------------------------- #
def test_scaled_plan_every_category_at_least_one():
    plan = gd._scaled_plan(500)
    assert [k for k, _, _ in plan] == [k for k, _, _ in gd.DQ_ISSUE_PLAN]
    assert all(c >= 1 for _, _, c in plan)


def test_issue_log_integrity(deliv_med):
    log = deliv_med.issue_log
    plan = dict((k, c) for k, _, c in gd._scaled_plan(MEDIUM))
    assert list(log.columns) == list(gd.DQ_ISSUE_LOG_COLUMNS)
    assert len(log) == sum(plan.values())
    assert set(log["issue_type"]) == ISSUE_TYPES
    assert set(log["dq_dimension"]).issubset(set(gd.DQ_DIMENSIONS))
    by_type = log.groupby("issue_type").size()
    for kind, count in plan.items():
        assert int(by_type[kind]) == count, kind
    # every logged Order_ID is a real business key
    assert log["Order_ID"].astype(str).str.match(r"^ORD-\d{8}$").all()


def test_corruption_is_controlled(deliv_med):
    touched = deliv_med.issue_log["row_pos"].astype(int).nunique()
    assert touched < 0.03 * deliv_med.counts["clean_rows"]
    assert deliv_med.counts["corruption_pct"] < 3.0


def test_nulls_in_mandatory_columns(deliv_med):
    assert deliv_med.delivery_df[gd.MANDATORY_COLUMNS].isna().any().any()


def test_duplicate_rows_present(deliv_med):
    dup_count = dict((k, c) for k, _, c in gd._scaled_plan(MEDIUM))["duplicate_row"]
    dups = deliv_med.delivery_df["Order_ID"].duplicated(keep=False)
    assert dups.sum() >= 2 * dup_count - 1  # each duplicated id appears >= twice


def test_invalid_dates_present(deliv_med):
    parsed = pd.to_datetime(deliv_med.delivery_df["Order_Date"], errors="coerce")
    assert parsed.isna().any()                       # unparseable / empty
    assert (parsed.dt.year > 2026).any()             # out-of-window future date


def test_negative_quantities_present(deliv_med):
    q = pd.to_numeric(deliv_med.delivery_df["Quantity"], errors="coerce")
    assert (q <= 0).sum() >= 1


def test_invalid_discounts_present(deliv_med):
    d = pd.to_numeric(deliv_med.delivery_df["Discount"], errors="coerce")
    assert (d < 0).any()
    assert (d > 1).any()


def test_unknown_customer_ids_present(deliv_med, med_refs):
    customers, _, _ = med_refs
    known = set(customers["Customer_ID"])
    ids = deliv_med.delivery_df["Customer_ID"].dropna()
    assert (~ids.isin(known)).any()


def test_invalid_categories_present(deliv_med):
    cats = deliv_med.delivery_df["Category"].dropna()
    assert (~cats.isin(gd.CATEGORIES)).any()


def test_extreme_prices_present(deliv_med):
    p = pd.to_numeric(deliv_med.delivery_df["Unit_Price"], errors="coerce")
    assert (p > 1e7).any()
    assert (p <= 0).any()


def test_inconsistent_text_present(deliv_med):
    seg = deliv_med.delivery_df["Customer_Segment"].dropna()
    reg = deliv_med.delivery_df["Region"].dropna()
    pay = deliv_med.delivery_df["Payment_Method"].dropna()
    off_contract = (
        (~seg.isin(gd.CUSTOMER_SEGMENTS)).any()
        or (~reg.isin(gd.GEOGRAPHY)).any()
        or (~pay.isin(gd.PAYMENT_METHODS)).any()
    )
    assert off_contract
    # the mangling is whitespace / case only - normalising recovers a real value
    assert reg.str.strip().str.title().isin(list(gd.GEOGRAPHY)).any()


# --------------------------------------------------------------------------- #
# Business anomaly (spec Phase 12)
# --------------------------------------------------------------------------- #
def test_anomaly_primary_tier_signal(deliv_anom):
    st = deliv_anom.anomaly_stats
    b, a = st["primary"]["baseline"], st["primary"]["anomaly"]
    assert b["orders"] > 0 and a["orders"] > 0
    assert a["revenue"] < b["revenue"] * 0.85                 # Revenue down
    assert a["orders"] <= b["orders"] * 1.05                  # Orders not up
    assert a["avg_discount"] - b["avg_discount"] >= 0.12      # Discount up
    assert a["return_rate"] - b["return_rate"] >= 0.15        # Returns up
    assert a["avg_shipping_days"] - b["avg_shipping_days"] >= 4.0  # Shipping up
    assert st["rows_dropped"]["primary"] >= 5


def test_anomaly_secondary_tier_lighter_drag(deliv_anom):
    st = deliv_anom.anomaly_stats
    b, a = st["secondary"]["baseline"], st["secondary"]["anomaly"]
    assert 0.02 <= a["avg_discount"] - b["avg_discount"] < 0.12
    assert a["return_rate"] - b["return_rate"] >= 0.02
    assert a["avg_shipping_days"] - b["avg_shipping_days"] >= 1.5


def test_anomaly_is_localised(deliv_anom):
    st = deliv_anom.anomaly_stats
    b, a = st["macro"]["baseline"], st["macro"]["anomaly"]
    assert abs(a["avg_discount"] - b["avg_discount"]) < 0.02
    assert abs(a["return_rate"] - b["return_rate"]) < 0.02


def test_anomaly_rows_stay_model_consistent():
    # clean baseline + anomaly, no data-quality corruption
    res = gd.build_delivery_dataset(
        rows=ANOMALY_ROWS, seed=SEED, inject_issues=False, inject_anomaly=True
    )
    rng = np.random.default_rng(SEED)
    customers, products, _ = gd.build_reference_data(
        rng, gd._n_customers(ANOMALY_ROWS), gd._n_products(ANOMALY_ROWS)
    )
    gd.validate_consistency(res.delivery_df, customers, products)  # must not raise


def test_anomaly_confined_to_window_and_segment(deliv_anom):
    df = deliv_anom.delivery_df
    parsed = pd.to_datetime(df["Order_Date"], errors="coerce")
    in_window = parsed.between(
        pd.Timestamp(gd.ANOMALY_WINDOW_START), pd.Timestamp(gd.ANOMALY_WINDOW_END)
    ).fillna(False)
    slice_mask = (
        in_window
        & (df["Region"] == "West")
        & (df["Category"] == "Electronics")
        & (df["Sub_Category"] == "Laptop")
    )
    sl = df.loc[slice_mask]
    assert sl["Discount"].astype(float).mean() > 0.15
    assert pd.to_numeric(sl["Shipping_Days"]).mean() > 9.0


def test_ground_truth_markdown_documents_the_anomaly(deliv_anom):
    md = gd.anomaly_ground_truth_markdown(
        deliv_anom.anomaly_stats, deliv_anom.counts, seed=SEED
    )
    for token in (
        "West", "Electronics", "Laptop",
        gd.ANOMALY_WINDOW_START.isoformat(), gd.ANOMALY_WINDOW_END.isoformat(),
        "Realised deltas", "| Orders |", "AUTO-GENERATED",
    ):
        assert token in md
