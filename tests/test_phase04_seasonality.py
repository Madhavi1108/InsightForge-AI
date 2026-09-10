"""Phase 4 (spec Phase 10) - verify the seasonality engine reshapes per-date
demand (weekends, month-end, festive windows, annual curve) without breaking any
Phase 2 / 3 invariant.

The generator stays fully deterministic, so every threshold below is a fixed
inequality with margin, not a probabilistic bound.
"""
from __future__ import annotations

import calendar
import datetime as dt

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from scripts import generate_dataset as gd

SMALL = 3_000
MEDIUM = 25_000
SEED = 4

INTERNAL_COLUMNS = {
    "appeal", "purchase_weight", "is_repeat", "city_weight",
    "base_price", "cost_ratio",
}


@pytest.fixture(scope="module")
def med_df() -> pd.DataFrame:
    return gd.generate_dataset(rows=MEDIUM, seed=SEED)


@pytest.fixture(scope="module")
def per_day(med_df) -> pd.Series:
    """Order count per calendar date, reindexed over the full window (0-filled)."""
    dates = pd.to_datetime(med_df["Order_Date"]).dt.date
    full = pd.date_range(gd.DEFAULT_START, gd.DEFAULT_END, freq="D").date
    return dates.value_counts().reindex(full, fill_value=0)


# --------------------------------------------------------------------------- #
# Determinism / contract
# --------------------------------------------------------------------------- #
def test_phase04_generation_is_deterministic():
    a = gd.generate_dataset(rows=SMALL, seed=SEED)
    b = gd.generate_dataset(rows=SMALL, seed=SEED)
    assert_frame_equal(a, b)


def test_csv_still_22_columns_no_internal_weights(med_df):
    assert list(med_df.columns) == gd.COLUMNS
    assert len(gd.COLUMNS) == 22
    assert not (INTERNAL_COLUMNS & set(med_df.columns))


# --------------------------------------------------------------------------- #
# day_seasonality_weights - pure helper
# --------------------------------------------------------------------------- #
def test_weights_pure_positive_normalised():
    a = gd.day_seasonality_weights(gd.DEFAULT_START, gd.DEFAULT_END)
    b = gd.day_seasonality_weights(gd.DEFAULT_START, gd.DEFAULT_END)
    assert np.array_equal(a, b)  # deterministic, no RNG
    assert a.size == (gd.DEFAULT_END - gd.DEFAULT_START).days + 1
    assert np.isfinite(a).all()
    assert (a > 0).all()
    assert a.mean() == pytest.approx(1.0, abs=1e-9)


def test_weights_reject_backwards_window():
    with pytest.raises(ValueError):
        gd.day_seasonality_weights(gd.DEFAULT_END, gd.DEFAULT_START)


def test_weekend_weight_above_weekday_weight():
    start, end = gd.DEFAULT_START, gd.DEFAULT_END
    w = gd.day_seasonality_weights(start, end)
    days = [start + dt.timedelta(days=i) for i in range(w.size)]
    is_weekend = np.array([d.weekday() >= 5 for d in days])
    assert w[is_weekend].mean() > w[~is_weekend].mean() * 1.1


def test_seasonal_curve_climbs_from_late_spring_low_into_festive_quarter():
    """Isolate the smooth annual factor: it troughs in mid-May and rises into the
    Oct-Nov festive quarter, so a plain early-September day sits clearly above a
    plain mid-May day. Both probe days are ordinary weekdays - not weekends, not
    month-end, not inside any festive window - so only the annual curve differs."""
    w = gd.day_seasonality_weights(gd.DEFAULT_START, gd.DEFAULT_END)

    def plain_index(target: dt.date) -> int:
        d = target
        while (
            d.weekday() >= 5
            or d.day > calendar.monthrange(d.year, d.month)[1] - gd.MONTH_END_DAYS
            or any(
                dt.date.fromisoformat(s) <= d <= dt.date.fromisoformat(e)
                for _, s, e, _ in gd.FESTIVE_PERIODS
            )
        ):
            d += dt.timedelta(days=1)
        return (d - gd.DEFAULT_START).days

    spring_low = plain_index(dt.date(2026, 5, 13))
    late_summer = plain_index(dt.date(2026, 9, 1))
    assert w[late_summer] > w[spring_low] * 1.05


# --------------------------------------------------------------------------- #
# Effects visible in the generated dataset
# --------------------------------------------------------------------------- #
def test_weekend_orders_per_day_exceed_weekday(per_day):
    idx = pd.to_datetime(pd.Index(per_day.index))
    weekend = per_day[idx.weekday >= 5]
    weekday = per_day[idx.weekday < 5]
    assert weekend.mean() > weekday.mean() * 1.1


def test_month_end_orders_per_day_exceed_rest_of_month(per_day):
    idx = pd.to_datetime(pd.Index(per_day.index))
    last_day = idx.days_in_month
    is_month_end = idx.day > (last_day - gd.MONTH_END_DAYS)
    assert per_day[is_month_end].mean() > per_day[~is_month_end].mean() * 1.05


def test_festive_window_orders_per_day_spike(per_day):
    # Independence Day sale window - fully inside 2026-01-01 .. 2026-09-08.
    festive = per_day[
        (pd.Index(per_day.index) >= dt.date(2026, 8, 8))
        & (pd.Index(per_day.index) <= dt.date(2026, 8, 17))
    ]
    # Non-festive August baseline (18th onward, before Raksha Bandhan on the 26th).
    baseline = per_day[
        (pd.Index(per_day.index) >= dt.date(2026, 8, 18))
        & (pd.Index(per_day.index) <= dt.date(2026, 8, 25))
    ]
    assert festive.mean() > baseline.mean() * 1.15


def test_dates_within_window_and_well_covered(med_df, per_day):
    dates = pd.to_datetime(med_df["Order_Date"])
    assert dates.min().date() >= gd.DEFAULT_START
    assert dates.max().date() <= gd.DEFAULT_END
    # Redistribution, not truncation: nearly every day still gets orders.
    assert (per_day > 0).mean() >= 0.95


# --------------------------------------------------------------------------- #
# Phase 2 / 3 invariants still hold
# --------------------------------------------------------------------------- #
def test_all_prior_invariants_hold():
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
    assert set(orders["Customer_ID"]).issubset(set(customers["Customer_ID"]))
    assert set(orders["Product_ID"]).issubset(set(products["Product_ID"]))


def test_stable_attributes_still_hold(med_df):
    for key, attrs in (
        ("Customer_ID", ["Customer_Name", "Customer_Segment"]),
        ("Product_ID", ["Product_Name", "Category", "Sub_Category"]),
    ):
        counts = med_df.groupby(key)[attrs].nunique()
        assert (counts == 1).all().all(), f"{key} has drifting {attrs}"
