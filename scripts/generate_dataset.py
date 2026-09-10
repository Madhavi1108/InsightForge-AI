#!/usr/bin/env python3
"""InsightForge AI - retail data generator (spec Phases 5-10).

Produces a **clean, internally consistent** retail order dataset that matches
``docs/dataset-design.md``: 22 fields, valid Region->State->City and
Category->Sub_Category hierarchies, stable per-customer / per-product attributes,
and the business data model

    Revenue = Quantity * Unit_Price * (1 - Discount)
    Profit  = Revenue - Cost

Generation is fully deterministic for a given ``--seed`` (default 20260909) and
vectorised with NumPy.

Phase 3 (spec Phases 7-9) adds weighted realism to the clean baseline: a
lognormal product *appeal* weight so a few products dominate revenue, a ~15%
subset of high-frequency *repeat* customers, segment-driven basket size /
discount, and an expanded metro-weighted Indian geography. The internal weight
columns (``appeal``, ``purchase_weight``, ``is_repeat``, ``city_weight``) live
only on the reference tables and never reach the 22-field CSV.

Phase 4 (spec Phase 10) adds a **seasonality engine**: a deterministic per-date
demand weighting (:func:`day_seasonality_weights`) combining weekend uplift,
month-end effects, curated festive-sale windows, and a smooth annual demand
curve. It only reshapes how many orders land on each ``Order_Date`` -- no
row-level field (discount, quantity, price, returns, shipping) changes, and the
weights are mean-normalised so total expected volume is unchanged. The festive
dates in ``FESTIVE_PERIODS`` are curated / approximate shopping-sale windows, not
astronomically exact festival dates; the requirement is documented, reproducible
logic.

Deferred to later phases (see ``docs/PHASE_MAP.md``):

* NULLs, duplicates, invalid values, the injected business anomaly -- Phase 5
* split into ``data/incoming/sales_YYYY_MM_DD.csv`` -- Phase 6

Usage
-----
    python scripts/generate_dataset.py
    python scripts/generate_dataset.py --rows 150000 --seed 20260909 \
        --start 2026-01-01 --end 2026-09-08 --output data/full_dataset.csv
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Canonical schema - order matches docs/dataset-design.md exactly.
# --------------------------------------------------------------------------- #
COLUMNS = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]

# Only Shipping_Days may be null (Pending / Cancelled orders have not shipped).
MANDATORY_COLUMNS = [c for c in COLUMNS if c != "Shipping_Days"]

DEFAULT_SEED = 20260909
DEFAULT_START = dt.date(2026, 1, 1)
DEFAULT_END = dt.date(2026, 9, 8)
DEFAULT_ROWS = 150_000
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "full_dataset.csv"

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
# Indian geography (spec Phase 9): Region -> State -> City, every city in exactly
# one state, every state in exactly one region. >= 20 states / >= 45 cities.
GEOGRAPHY: dict[str, dict[str, list[str]]] = {
    "North": {
        "Delhi": ["New Delhi"],
        "Punjab": ["Ludhiana", "Amritsar", "Jalandhar"],
        "Haryana": ["Gurugram", "Faridabad", "Panipat"],
        "Uttar Pradesh": ["Lucknow", "Kanpur", "Noida", "Agra", "Varanasi"],
        "Himachal Pradesh": ["Shimla", "Solan"],
        "Jammu and Kashmir": ["Srinagar", "Jammu"],
    },
    "South": {
        "Karnataka": ["Bengaluru", "Mysuru", "Hubballi", "Mangaluru"],
        "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli"],
        "Telangana": ["Hyderabad", "Warangal"],
        "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode"],
        "Andhra Pradesh": ["Visakhapatnam", "Vijayawada", "Guntur"],
    },
    "East": {
        "West Bengal": ["Kolkata", "Howrah", "Siliguri", "Durgapur"],
        "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela"],
        "Bihar": ["Patna", "Gaya", "Bhagalpur"],
        "Jharkhand": ["Ranchi", "Jamshedpur", "Dhanbad"],
        "Assam": ["Guwahati", "Dibrugarh"],
    },
    "West": {
        "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik", "Thane"],
        "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot"],
        "Rajasthan": ["Jaipur", "Jodhpur", "Kota", "Udaipur"],
        "Goa": ["Panaji", "Margao"],
    },
    "Central": {
        "Madhya Pradesh": ["Indore", "Bhopal", "Gwalior", "Jabalpur", "Ujjain"],
        "Chhattisgarh": ["Raipur", "Bhilai", "Bilaspur"],
        "Uttarakhand": ["Dehradun", "Haridwar", "Rishikesh", "Haldwani"],
    },
}
REGION_BASE_SHIPPING = {"North": 5, "South": 4, "East": 6, "West": 3, "Central": 5}

# City demand tiers (spec Phase 9): metros pull a disproportionate order share.
METRO_CITIES = {
    "Mumbai", "New Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata",
    "Pune", "Ahmedabad",
}
LARGE_CITIES = {
    "Jaipur", "Surat", "Lucknow", "Kanpur", "Nagpur", "Indore", "Bhopal",
    "Visakhapatnam", "Coimbatore", "Kochi", "Gurugram", "Noida", "Patna",
    "Bhubaneswar", "Vadodara", "Ludhiana", "Agra", "Nashik", "Faridabad",
    "Ranchi", "Jamshedpur", "Madurai", "Vijayawada", "Guwahati", "Thane",
    "Thiruvananthapuram", "Dehradun", "Amritsar", "Jodhpur", "Raipur", "Howrah",
    "Varanasi",
}
CITY_TIER_WEIGHT = {"metro": 6.0, "large": 2.5, "other": 1.0}
METRO_SHIPPING_BONUS = -1  # metro deliveries run ~1 day faster

# category -> {sub_category: (price_lo, price_hi, cost_ratio_lo, cost_ratio_hi)} (INR)
CATALOG: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "Electronics": {
        "Laptop": (35_000, 130_000, 0.78, 0.90),
        "Phone": (9_000, 95_000, 0.80, 0.92),
        "Camera": (12_000, 160_000, 0.80, 0.90),
        "Audio": (800, 28_000, 0.60, 0.80),
        "Accessories": (300, 6_000, 0.55, 0.75),
    },
    "Furniture": {
        "Chairs": (2_500, 30_000, 0.60, 0.80),
        "Tables": (4_000, 55_000, 0.60, 0.80),
        "Bookcases": (3_000, 40_000, 0.62, 0.82),
        "Storage": (1_500, 25_000, 0.58, 0.78),
    },
    "Office Supplies": {
        "Paper": (50, 1_200, 0.50, 0.68),
        "Binders": (80, 1_500, 0.50, 0.68),
        "Pens": (30, 900, 0.45, 0.65),
        "Art": (120, 3_500, 0.52, 0.70),
        "Storage": (200, 4_000, 0.55, 0.72),
    },
    "Clothing": {
        "Menswear": (400, 7_000, 0.40, 0.62),
        "Womenswear": (400, 8_000, 0.40, 0.62),
        "Footwear": (600, 9_000, 0.45, 0.65),
        "Accessories": (200, 4_000, 0.38, 0.60),
    },
    "Home & Kitchen": {
        "Cookware": (500, 18_000, 0.55, 0.75),
        "Appliances": (1_500, 45_000, 0.62, 0.82),
        "Decor": (300, 12_000, 0.50, 0.72),
        "Bedding": (700, 15_000, 0.48, 0.70),
    },
}
CATEGORIES = list(CATALOG)
CATEGORY_WEIGHTS = np.array([0.16, 0.14, 0.30, 0.24, 0.16])

# Product popularity (spec Phase 8): each product gets a lognormal *appeal*
# weight; a few products end up carrying most of the revenue.
PRODUCT_APPEAL_SIGMA = 1.1

CUSTOMER_SEGMENTS = ["Consumer", "Corporate", "Home Office"]
SEGMENT_WEIGHTS = np.array([0.52, 0.30, 0.18])

# Repeat customers (spec Phase 7): baseline purchase propensity is lognormal; a
# flagged minority is boosted so a small set of customers drives many orders.
CUSTOMER_FREQUENCY_SIGMA = 0.7
REPEAT_CUSTOMER_FRACTION = 0.15
REPEAT_CUSTOMER_BOOST = 6.0

PAYMENT_METHODS = ["UPI", "Credit Card", "Debit Card", "Net Banking", "COD", "Wallet"]
PAYMENT_WEIGHTS = np.array([0.34, 0.22, 0.14, 0.12, 0.12, 0.06])

ORDER_STATUSES = ["Completed", "Pending", "Cancelled", "Returned"]
ORDER_STATUS_WEIGHTS = np.array([0.86, 0.06, 0.04, 0.04])

QUANTITY_CHOICES = np.array([1, 2, 3, 4, 5])
QUANTITY_WEIGHTS = np.array([0.50, 0.24, 0.14, 0.08, 0.04])

DISCOUNT_CHOICES = np.array([0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30])
DISCOUNT_WEIGHTS = np.array([0.40, 0.20, 0.15, 0.10, 0.08, 0.04, 0.03])

# Segment-driven behaviour (spec Phase 7): "Consumer" reuses the baselines above;
# Corporate / Home Office buy in larger quantities and negotiate deeper
# discounts. Each row is a distribution over QUANTITY_CHOICES / DISCOUNT_CHOICES.
SEGMENT_QUANTITY_WEIGHTS = {
    "Consumer": QUANTITY_WEIGHTS,
    "Corporate": np.array([0.34, 0.26, 0.20, 0.13, 0.07]),
    "Home Office": np.array([0.42, 0.27, 0.17, 0.09, 0.05]),
}
SEGMENT_DISCOUNT_WEIGHTS = {
    "Consumer": DISCOUNT_WEIGHTS,
    "Corporate": np.array([0.22, 0.20, 0.20, 0.16, 0.12, 0.06, 0.04]),
    "Home Office": np.array([0.32, 0.22, 0.18, 0.13, 0.09, 0.04, 0.02]),
}

# --------------------------------------------------------------------------- #
# Seasonality engine (spec Phase 10)
# --------------------------------------------------------------------------- #
# A deterministic per-date demand multiplier reshapes how many orders fall on
# each Order_Date. Four independent effects are multiplied together and the
# resulting vector is mean-normalised, so only the *distribution* of dates
# changes - never a row-level field or the total row count.
#
# 1. Weekends: consumer e-commerce demand is higher on Saturday / Sunday.
WEEKEND_UPLIFT = 1.25
# 2. Month-end: the last MONTH_END_DAYS calendar days of every month see a
#    payday / month-end-target push.
MONTH_END_DAYS = 3
MONTH_END_UPLIFT = 1.20
# 3. Festive periods: curated Indian shopping-sale windows (name, start, end,
#    multiplier). Dates are approximate / illustrative, not astronomically exact;
#    overlapping windows take the max multiplier (they do not compound).
FESTIVE_PERIODS: list[tuple[str, str, str, float]] = [
    ("Republic Day sale", "2026-01-20", "2026-01-27", 1.35),
    ("Holi", "2026-03-01", "2026-03-04", 1.20),
    ("Financial year-end sale", "2026-03-25", "2026-03-31", 1.30),
    ("Akshaya Tritiya", "2026-04-20", "2026-04-22", 1.15),
    ("Independence Day sale", "2026-08-08", "2026-08-17", 1.45),
    ("Raksha Bandhan", "2026-08-26", "2026-08-29", 1.20),
]
# 4. Seasonal demand: a smooth annual curve 1 + A * cos(2*pi*(doy - peak)/365.25).
#    The peak sits in mid-November (SEASONAL_PEAK_DOY, outside the generation
#    window) and the trough in mid-May, so across the window demand sags through
#    late spring and then climbs into the Oct-Nov Indian festive quarter.
SEASONAL_AMPLITUDE = 0.15
SEASONAL_PEAK_DOY = 315

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna",
    "Ishaan", "Rohan", "Ananya", "Diya", "Aadhya", "Isha", "Kavya", "Anika",
    "Navya", "Riya", "Meera", "Sara", "Rahul", "Priya", "Neha", "Karan",
    "Pooja", "Amit", "Sneha", "Vikram", "Divya", "Nikhil",
]
LAST_NAMES = [
    "Sharma", "Verma", "Rao", "Reddy", "Nair", "Iyer", "Patel", "Shah", "Gupta",
    "Mehta", "Bose", "Das", "Chopra", "Malhotra", "Kapoor", "Singh", "Kumar",
    "Joshi", "Desai", "Menon",
]
PRODUCT_SERIES = [
    "Pro", "Max", "Lite", "Plus", "Neo", "Prime", "Edge", "Core", "One", "Air",
    "Go", "Ultra",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _city_tier(city: str) -> str:
    if city in METRO_CITIES:
        return "metro"
    if city in LARGE_CITIES:
        return "large"
    return "other"


def _geo_rows() -> pd.DataFrame:
    """Flatten GEOGRAPHY into a (Region, State, City, base_shipping, city_weight) table."""
    rows = []
    for region, states in GEOGRAPHY.items():
        for state, cities in states.items():
            for city in cities:
                rows.append(
                    (
                        region,
                        state,
                        city,
                        REGION_BASE_SHIPPING[region],
                        CITY_TIER_WEIGHT[_city_tier(city)],
                    )
                )
    return pd.DataFrame(
        rows, columns=["Region", "State", "City", "base_shipping", "city_weight"]
    )


def _p(weights) -> np.ndarray:
    """Normalise a non-negative weight vector to sum to 1 (for ``rng.choice`` ``p=``)."""
    weights = np.asarray(weights, dtype=float)
    return weights / weights.sum()


def valid_geo_tuples() -> set[tuple[str, str, str]]:
    return {
        (r, s, c)
        for r, states in GEOGRAPHY.items()
        for s, cities in states.items()
        for c in cities
    }


def valid_category_pairs() -> set[tuple[str, str]]:
    return {(cat, sub) for cat, subs in CATALOG.items() for sub in subs}


def _n_customers(rows: int) -> int:
    return max(500, rows // 12)


def _n_products(rows: int) -> int:
    return max(80, rows // 500)


def day_seasonality_weights(start: dt.date, end: dt.date) -> np.ndarray:
    """Per-date demand multipliers for every day in ``[start, end]`` (spec Phase 10).

    Combines four independent effects - weekend uplift, month-end uplift, curated
    festive-sale windows, and a smooth annual demand curve - by multiplication,
    then mean-normalises the result to ``1.0`` so the total expected order volume
    is unchanged and the seasonality is purely redistributive. Pure and
    deterministic: no RNG, depends only on the calendar.
    """
    n_days = (end - start).days + 1
    if n_days < 1:
        raise ValueError("end date must not precede start date")

    festive = [
        (dt.date.fromisoformat(s), dt.date.fromisoformat(e), float(m))
        for _, s, e, m in FESTIVE_PERIODS
    ]

    weights = np.empty(n_days, dtype=float)
    for i in range(n_days):
        day = start + dt.timedelta(days=i)

        factor = 1.0
        if day.weekday() >= 5:  # Saturday=5, Sunday=6
            factor *= WEEKEND_UPLIFT

        days_in_month = calendar.monthrange(day.year, day.month)[1]
        if day.day > days_in_month - MONTH_END_DAYS:
            factor *= MONTH_END_UPLIFT

        festive_mult = max(
            (m for lo, hi, m in festive if lo <= day <= hi), default=1.0
        )
        factor *= festive_mult

        doy = day.timetuple().tm_yday
        factor *= 1.0 + SEASONAL_AMPLITUDE * np.cos(
            2.0 * np.pi * (doy - SEASONAL_PEAK_DOY) / 365.25
        )

        weights[i] = factor

    return weights / weights.mean()


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
def build_reference_data(
    rng: np.random.Generator, n_customers: int, n_products: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build the customer, product and geography reference tables."""
    # Customers -----------------------------------------------------------
    cust_first = rng.choice(FIRST_NAMES, n_customers)
    cust_last = rng.choice(LAST_NAMES, n_customers)
    customers = pd.DataFrame(
        {
            "Customer_ID": [f"CUST-{i:06d}" for i in range(1, n_customers + 1)],
            "Customer_Name": [f"{f} {l}" for f, l in zip(cust_first, cust_last)],
            "Customer_Segment": rng.choice(
                CUSTOMER_SEGMENTS, n_customers, p=SEGMENT_WEIGHTS
            ),
        }
    )
    # Repeat-customer behaviour: lognormal baseline propensity, boosted for a
    # flagged minority. Internal columns - never written to the CSV.
    freq_base = rng.lognormal(0.0, CUSTOMER_FREQUENCY_SIGMA, n_customers)
    is_repeat = rng.random(n_customers) < REPEAT_CUSTOMER_FRACTION
    customers["is_repeat"] = is_repeat
    customers["purchase_weight"] = np.where(
        is_repeat, freq_base * REPEAT_CUSTOMER_BOOST, freq_base
    )

    # Products ----------------------------------------------------------------
    prod_category = rng.choice(CATEGORIES, n_products, p=CATEGORY_WEIGHTS)
    sub_category = np.empty(n_products, dtype=object)
    base_price = np.empty(n_products, dtype=float)
    cost_ratio = np.empty(n_products, dtype=float)

    for category in CATEGORIES:  # fixed iteration order -> deterministic
        mask = prod_category == category
        k = int(mask.sum())
        if k == 0:
            continue
        subs = list(CATALOG[category])
        idx = rng.integers(0, len(subs), k)
        sub_category[mask] = np.array(subs, dtype=object)[idx]
        lo = np.array([CATALOG[category][s][0] for s in subs])[idx]
        hi = np.array([CATALOG[category][s][1] for s in subs])[idx]
        clo = np.array([CATALOG[category][s][2] for s in subs])[idx]
        chi = np.array([CATALOG[category][s][3] for s in subs])[idx]
        base_price[mask] = rng.uniform(lo, hi)
        cost_ratio[mask] = rng.uniform(clo, chi)

    series = rng.choice(PRODUCT_SERIES, n_products)
    model_no = rng.integers(100, 1000, n_products)
    # Product popularity: lognormal appeal weight (internal column). A few
    # products sell far more than the rest -> Pareto-like revenue concentration.
    appeal = rng.lognormal(0.0, PRODUCT_APPEAL_SIGMA, n_products)
    products = pd.DataFrame(
        {
            "Product_ID": [f"PROD-{i:05d}" for i in range(1, n_products + 1)],
            "Product_Name": [
                f"{sub} {ser} {num}"
                for sub, ser, num in zip(sub_category, series, model_no)
            ],
            "Category": prod_category,
            "Sub_Category": sub_category,
            "base_price": np.round(base_price, 2),
            "cost_ratio": cost_ratio,
            "appeal": appeal,
        }
    )

    return customers, products, _geo_rows()


# --------------------------------------------------------------------------- #
# Order generation
# --------------------------------------------------------------------------- #
def generate_orders(
    rng: np.random.Generator,
    n_rows: int,
    customers: pd.DataFrame,
    products: pd.DataFrame,
    geo: pd.DataFrame,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Generate ``n_rows`` clean order lines (one line per order)."""
    n_days = (end - start).days + 1
    if n_days < 1:
        raise ValueError("end date must not precede start date")

    # Weighted selection: repeat customers, popular products and metro cities are
    # drawn disproportionately often. Indices stay in-range -> referential
    # integrity is preserved.
    cust_idx = rng.choice(
        len(customers), size=n_rows, p=_p(customers["purchase_weight"].to_numpy())
    )
    prod_idx = rng.choice(
        len(products), size=n_rows, p=_p(products["appeal"].to_numpy())
    )
    geo_idx = rng.choice(
        len(geo), size=n_rows, p=_p(geo["city_weight"].to_numpy())
    )
    # Seasonality engine (spec Phase 10): dates are drawn in proportion to a
    # deterministic per-day demand multiplier rather than uniformly.
    day_offset = rng.choice(
        n_days, size=n_rows, p=_p(day_seasonality_weights(start, end))
    )

    order_date = np.datetime64(start) + day_offset.astype("timedelta64[D]")

    # Basket size and discount depend on the customer's segment.
    row_segment = customers["Customer_Segment"].to_numpy()[cust_idx]
    quantity = np.empty(n_rows, dtype=np.int64)
    discount = np.empty(n_rows, dtype=float)
    for seg in CUSTOMER_SEGMENTS:  # fixed iteration order -> deterministic
        mask = row_segment == seg
        k = int(mask.sum())
        if k == 0:
            continue
        quantity[mask] = rng.choice(
            QUANTITY_CHOICES, k, p=_p(SEGMENT_QUANTITY_WEIGHTS[seg])
        )
        discount[mask] = rng.choice(
            DISCOUNT_CHOICES, k, p=_p(SEGMENT_DISCOUNT_WEIGHTS[seg])
        )

    base_price = products["base_price"].to_numpy()[prod_idx]
    cost_ratio = products["cost_ratio"].to_numpy()[prod_idx]
    unit_price = np.maximum(np.round(base_price * rng.normal(1.0, 0.03, n_rows), 2), 1.0)
    unit_cost = np.round(base_price * cost_ratio, 2)

    payment_method = rng.choice(PAYMENT_METHODS, n_rows, p=PAYMENT_WEIGHTS)
    order_status = rng.choice(ORDER_STATUSES, n_rows, p=ORDER_STATUS_WEIGHTS)

    base_ship = geo["base_shipping"].to_numpy()[geo_idx].astype(int)
    metro_row = np.isin(geo["City"].to_numpy()[geo_idx], list(METRO_CITIES))
    base_ship = base_ship + np.where(metro_row, METRO_SHIPPING_BONUS, 0)
    shipping_days = np.clip(base_ship + rng.integers(-1, 4, n_rows), 0, 21).astype(float)
    not_shipped = np.isin(order_status, ["Pending", "Cancelled"])
    shipping_days[not_shipped] = np.nan

    return_status = np.where(order_status == "Returned", "Returned", "Not Returned")

    df = pd.DataFrame(
        {
            "Order_ID": [f"ORD-{i:08d}" for i in range(1, n_rows + 1)],
            "Order_Date": order_date,
            "Customer_ID": customers["Customer_ID"].to_numpy()[cust_idx],
            "Customer_Name": customers["Customer_Name"].to_numpy()[cust_idx],
            "Customer_Segment": customers["Customer_Segment"].to_numpy()[cust_idx],
            "Product_ID": products["Product_ID"].to_numpy()[prod_idx],
            "Product_Name": products["Product_Name"].to_numpy()[prod_idx],
            "Category": products["Category"].to_numpy()[prod_idx],
            "Sub_Category": products["Sub_Category"].to_numpy()[prod_idx],
            "Region": geo["Region"].to_numpy()[geo_idx],
            "State": geo["State"].to_numpy()[geo_idx],
            "City": geo["City"].to_numpy()[geo_idx],
            "Quantity": quantity.astype(int),
            "Unit_Price": unit_price,
            "Discount": np.round(discount, 3),
            "Cost": np.round(quantity * unit_cost, 2),
            "Payment_Method": payment_method,
            "Shipping_Days": shipping_days,
            "Order_Status": order_status,
            "Return_Status": return_status,
        }
    )
    return df


def apply_business_model(df: pd.DataFrame) -> pd.DataFrame:
    """Derive Revenue and Profit. The **only** place these formulas live."""
    df = df.copy()
    df["Revenue"] = np.round(
        df["Quantity"] * df["Unit_Price"] * (1.0 - df["Discount"]), 2
    )
    df["Profit"] = np.round(df["Revenue"] - df["Cost"], 2)
    return df[COLUMNS]


def validate_consistency(
    df: pd.DataFrame, customers: pd.DataFrame, products: pd.DataFrame
) -> None:
    """Assert the dataset obeys the business data model and the design contract."""
    assert list(df.columns) == COLUMNS, "column set / order does not match COLUMNS"

    internal_only = {
        "appeal", "purchase_weight", "is_repeat", "city_weight",
        "base_price", "cost_ratio",
    }
    assert not (internal_only & set(df.columns)), "internal generator column leaked into the dataset"

    revenue_check = np.round(
        df["Quantity"] * df["Unit_Price"] * (1.0 - df["Discount"]), 2
    )
    assert np.allclose(df["Revenue"], revenue_check, atol=0.01), "Revenue formula broken"
    profit_check = np.round(df["Revenue"] - df["Cost"], 2)
    assert np.allclose(df["Profit"], profit_check, atol=0.01), "Profit formula broken"

    assert (df["Revenue"] >= 0).all(), "negative Revenue"
    assert (df["Quantity"] >= 1).all(), "Quantity < 1"
    assert df["Discount"].between(0.0, 0.8).all(), "Discount out of range"

    assert df[MANDATORY_COLUMNS].notna().all().all(), "null in a mandatory column"

    assert set(df["Customer_ID"]).issubset(set(customers["Customer_ID"])), "unknown Customer_ID"
    assert set(df["Product_ID"]).issubset(set(products["Product_ID"])), "unknown Product_ID"

    geo_seen = set(map(tuple, df[["Region", "State", "City"]].to_numpy()))
    assert geo_seen.issubset(valid_geo_tuples()), "invalid Region/State/City combo"
    cat_seen = set(map(tuple, df[["Category", "Sub_Category"]].to_numpy()))
    assert cat_seen.issubset(valid_category_pairs()), "invalid Category/Sub_Category combo"

    returned = df["Return_Status"].eq("Returned")
    status_returned = df["Order_Status"].eq("Returned")
    assert returned.equals(status_returned), "Return_Status inconsistent with Order_Status"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def generate_dataset(
    rows: int = DEFAULT_ROWS,
    seed: int = DEFAULT_SEED,
    start: dt.date = DEFAULT_START,
    end: dt.date = DEFAULT_END,
) -> pd.DataFrame:
    """Generate a clean, validated retail dataset. Deterministic for a given seed."""
    if rows < 1:
        raise ValueError("rows must be >= 1")
    rng = np.random.default_rng(seed)
    customers, products, geo = build_reference_data(
        rng, _n_customers(rows), _n_products(rows)
    )
    orders = generate_orders(rng, rows, customers, products, geo, start, end)
    df = apply_business_model(orders)
    validate_consistency(df, customers, products)
    return df.reset_index(drop=True)


def summarise(df: pd.DataFrame) -> str:
    dates = pd.to_datetime(df["Order_Date"])
    prod_rev = df.groupby("Product_ID")["Revenue"].sum().sort_values(ascending=False)
    top_decile = max(1, len(prod_rev) // 10)
    prod_share = prod_rev.head(top_decile).sum() / prod_rev.sum()
    order_counts = df.groupby("Customer_ID").size().sort_values(ascending=False)
    top_5pct = max(1, len(order_counts) // 20)
    cust_share = order_counts.head(top_5pct).sum() / len(df)
    metro_share = df["City"].isin(METRO_CITIES).mean()

    per_day = dates.dt.date.value_counts()
    weekend_days = {d for d in per_day.index if d.weekday() >= 5}
    weekend_rate = per_day[[d in weekend_days for d in per_day.index]].mean()
    weekday_rate = per_day[[d not in weekend_days for d in per_day.index]].mean()
    festive_days = {
        lo + dt.timedelta(days=k)
        for _, s, e, _ in FESTIVE_PERIODS
        for lo, hi in [(dt.date.fromisoformat(s), dt.date.fromisoformat(e))]
        for k in range((hi - lo).days + 1)
    }
    festive_share = dates.dt.date.isin(festive_days).mean()

    lines = [
        f"rows                        : {len(df):,}",
        f"date span                   : {dates.min().date()} .. {dates.max().date()}",
        f"distinct customers          : {df['Customer_ID'].nunique():,}",
        f"distinct products           : {df['Product_ID'].nunique():,}",
        f"distinct states / cities    : {df['State'].nunique()} / {df['City'].nunique()}",
        f"total revenue (INR)         : {df['Revenue'].sum():,.2f}",
        f"total profit  (INR)         : {df['Profit'].sum():,.2f}",
        f"top-decile product rev share: {prod_share:.2%}",
        f"busiest customer orders     : {int(order_counts.iloc[0])}",
        f"orders from top-5% customers: {cust_share:.2%}",
        f"metro-city order share      : {metro_share:.2%}",
        f"orders/day weekend vs weekday: {weekend_rate:,.0f} vs {weekday_rate:,.0f}",
        f"festive-window order share  : {festive_share:.2%}",
        f"mandatory nulls             : {int(df[MANDATORY_COLUMNS].isna().sum().sum())}",
        f"shipping_days nulls         : {int(df['Shipping_Days'].isna().sum())} "
        f"(Pending/Cancelled)",
        f"return rate                 : {df['Return_Status'].eq('Returned').mean():.2%}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_dataset.py",
        description="InsightForge AI - retail data generator (Phases 2-4)",
    )
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("InsightForge AI - retail data generator")
    print("=" * 40)
    df = generate_dataset(
        rows=args.rows, seed=args.seed, start=args.start, end=args.end
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, date_format="%Y-%m-%d")
    print(summarise(df))
    print(f"\n[ok] wrote {len(df):,} rows -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
