#!/usr/bin/env python3
"""InsightForge AI - Phase 2 retail data generator (spec Phases 5-6).

Produces a **clean, internally consistent** retail order dataset that matches
``docs/dataset-design.md``: 22 fields, valid Region->State->City and
Category->Sub_Category hierarchies, stable per-customer / per-product attributes,
and the business data model

    Revenue = Quantity * Unit_Price * (1 - Discount)
    Profit  = Revenue - Cost

Generation is fully deterministic for a given ``--seed`` (default 20260909) and
vectorised with NumPy.

Deferred to later phases (see ``docs/PHASE_MAP.md``):

* repeat-customer behaviour, richer catalog, full Indian geography -- Phase 3
* weekend / month-end / festive / seasonal demand -- Phase 4
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
# Reference data (curated; deepened in Phase 3).
# --------------------------------------------------------------------------- #
GEOGRAPHY: dict[str, dict[str, list[str]]] = {
    "North": {
        "Delhi": ["New Delhi"],
        "Punjab": ["Ludhiana", "Amritsar"],
        "Uttar Pradesh": ["Lucknow", "Kanpur", "Noida"],
    },
    "South": {
        "Karnataka": ["Bengaluru", "Mysuru"],
        "Tamil Nadu": ["Chennai", "Coimbatore"],
        "Telangana": ["Hyderabad"],
        "Kerala": ["Kochi"],
    },
    "East": {
        "West Bengal": ["Kolkata", "Howrah"],
        "Odisha": ["Bhubaneswar"],
        "Bihar": ["Patna"],
        "Jharkhand": ["Ranchi"],
    },
    "West": {
        "Maharashtra": ["Mumbai", "Pune", "Nagpur"],
        "Gujarat": ["Ahmedabad", "Surat"],
        "Rajasthan": ["Jaipur", "Jodhpur"],
    },
    "Central": {
        "Madhya Pradesh": ["Indore", "Bhopal"],
        "Chhattisgarh": ["Raipur"],
    },
}
REGION_BASE_SHIPPING = {"North": 5, "South": 4, "East": 6, "West": 3, "Central": 5}

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

CUSTOMER_SEGMENTS = ["Consumer", "Corporate", "Home Office"]
SEGMENT_WEIGHTS = np.array([0.52, 0.30, 0.18])

PAYMENT_METHODS = ["UPI", "Credit Card", "Debit Card", "Net Banking", "COD", "Wallet"]
PAYMENT_WEIGHTS = np.array([0.34, 0.22, 0.14, 0.12, 0.12, 0.06])

ORDER_STATUSES = ["Completed", "Pending", "Cancelled", "Returned"]
ORDER_STATUS_WEIGHTS = np.array([0.86, 0.06, 0.04, 0.04])

QUANTITY_CHOICES = np.array([1, 2, 3, 4, 5])
QUANTITY_WEIGHTS = np.array([0.50, 0.24, 0.14, 0.08, 0.04])

DISCOUNT_CHOICES = np.array([0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30])
DISCOUNT_WEIGHTS = np.array([0.40, 0.20, 0.15, 0.10, 0.08, 0.04, 0.03])

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
def _geo_rows() -> pd.DataFrame:
    """Flatten GEOGRAPHY into a (Region, State, City, base_shipping) table."""
    rows = []
    for region, states in GEOGRAPHY.items():
        for state, cities in states.items():
            for city in cities:
                rows.append((region, state, city, REGION_BASE_SHIPPING[region]))
    return pd.DataFrame(rows, columns=["Region", "State", "City", "base_shipping"])


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

    cust_idx = rng.integers(0, len(customers), n_rows)
    prod_idx = rng.integers(0, len(products), n_rows)
    geo_idx = rng.integers(0, len(geo), n_rows)
    day_offset = rng.integers(0, n_days, n_rows)

    order_date = np.datetime64(start) + day_offset.astype("timedelta64[D]")

    quantity = rng.choice(QUANTITY_CHOICES, n_rows, p=QUANTITY_WEIGHTS)
    discount = rng.choice(DISCOUNT_CHOICES, n_rows, p=DISCOUNT_WEIGHTS)

    base_price = products["base_price"].to_numpy()[prod_idx]
    cost_ratio = products["cost_ratio"].to_numpy()[prod_idx]
    unit_price = np.maximum(np.round(base_price * rng.normal(1.0, 0.03, n_rows), 2), 1.0)
    unit_cost = np.round(base_price * cost_ratio, 2)

    payment_method = rng.choice(PAYMENT_METHODS, n_rows, p=PAYMENT_WEIGHTS)
    order_status = rng.choice(ORDER_STATUSES, n_rows, p=ORDER_STATUS_WEIGHTS)

    base_ship = geo["base_shipping"].to_numpy()[geo_idx]
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
    lines = [
        f"rows                 : {len(df):,}",
        f"date span            : {dates.min().date()} .. {dates.max().date()}",
        f"distinct customers   : {df['Customer_ID'].nunique():,}",
        f"distinct products    : {df['Product_ID'].nunique():,}",
        f"total revenue (INR)  : {df['Revenue'].sum():,.2f}",
        f"total profit  (INR)  : {df['Profit'].sum():,.2f}",
        f"mandatory nulls      : {int(df[MANDATORY_COLUMNS].isna().sum().sum())}",
        f"shipping_days nulls  : {int(df['Shipping_Days'].isna().sum())} "
        f"(Pending/Cancelled)",
        f"return rate          : {df['Return_Status'].eq('Returned').mean():.2%}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="generate_dataset.py",
        description="InsightForge AI - Phase 2 retail data generator",
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
