"""InsightForge AI - Alteryx sales / customer / product ETL (Phase 13 /
spec Phases 25-26).

Pipeline **stage 5** (``docs/data-flow.md``): the orchestrator runs this on
the file in ``data/raw/`` right after the Phase 12 ingestion/data-quality
workflows, loading only the rows that passed schema validation (Phase 11)
into the star schema. This is the phase that finally flips a run from
``PARTIAL`` to ``SUCCESS``.

Two Alteryx artifacts under ``alteryx/`` (``03_sales_etl.yxmd``,
``04_customer_product_etl.yxmd``) plus a Python fallback, same
engine-selection contract as Phase 12
(:class:`src.config.AlteryxSettings` / :func:`src.alteryx.invoke_alteryx_engine`):
configured -> attempt the real engine (retried), otherwise -> the Python
loader directly. Unlike Phase 12's workflows (which only report), the
**load** itself is retried up to :data:`MAX_LOAD_ATTEMPTS` times; exhausting
retries raises :class:`EtlLoadError` and the caller marks the run ``FAILED``.

Per ``docs/database-schema.md`` (FR-05), ``Revenue``/``Profit`` are
**re-derived** on load, never trusted from the CSV.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.alteryx import AlteryxExecutionError, invoke_alteryx_engine
from src.config import AlteryxSettings, get_alteryx_settings
from src.database import Database
from src.validation import DataContract, ValidationResult, get_contract

#: How many times the load itself (Python fallback or post-engine verify) is
#: retried before giving up and raising :class:`EtlLoadError`.
MAX_LOAD_ATTEMPTS = 3
LOAD_RETRY_BACKOFF_SECONDS = 1.0

_ONE_CENT = Decimal("0.01")


class EtlLoadError(RuntimeError):
    """Raised when the star-schema load fails after every retry.

    ``str()`` never contains SQL text, bind values, or a connection string
    (``docs/security.md``).
    """


@dataclass(frozen=True)
class EtlResult:
    engine: str  # "alteryx" | "python_fallback"
    verified: bool
    seconds: float
    summary: dict = field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def date_dim_row(d: date) -> dict:
    """Build a ``dim_date`` row for calendar day ``d``."""
    last_day_of_month = calendar.monthrange(d.year, d.month)[1]
    return {
        "date_key": d,
        "year": d.year,
        "quarter": (d.month - 1) // 3 + 1,
        "month": d.month,
        "month_name": d.strftime("%B"),
        "day": d.day,
        "day_of_week": d.isoweekday(),  # 1 = Monday .. 7 = Sunday (ISO)
        "day_name": d.strftime("%A"),
        "week_of_year": d.isocalendar()[1],
        "is_weekend": d.isoweekday() in (6, 7),
        "is_month_end": d.day == last_day_of_month,
    }


def recompute_revenue_profit(quantity: str, unit_price: str, discount: str,
                             cost: str) -> tuple[Decimal, Decimal]:
    """Re-derive ``Revenue``/``Profit`` from the other measures (FR-05).

    Never trusts the CSV's own ``Revenue``/``Profit`` columns.
    """
    q = Decimal(str(quantity))
    up = Decimal(str(unit_price))
    disc = Decimal(str(discount))
    cost_d = Decimal(str(cost))
    revenue = (q * up * (Decimal("1") - disc)).quantize(_ONE_CENT, rounding=ROUND_HALF_UP)
    profit = (revenue - cost_d).quantize(_ONE_CENT, rounding=ROUND_HALF_UP)
    return revenue, profit


def valid_rows_frame(csv_path: Path, vres: ValidationResult) -> pd.DataFrame:
    """Re-read ``csv_path`` (same way ``validate_csv`` does) minus rejected rows."""
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, na_values=[])
    if vres.rejected_rows:
        drop_idx = [rn - 1 for rn in vres.rejected_rows]
        df = df.drop(index=drop_idx)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Python fallback loader (the only implementation actually exercised here -
# Alteryx isn't installed; see docs/alteryx-workflows.md / star-schema-etl.md)
# --------------------------------------------------------------------------- #
_DIM_CUSTOMER_SQL = (
    "INSERT INTO dim_customer (customer_id, customer_name, customer_segment, first_seen_run_id) "
    "VALUES (:customer_id, :customer_name, :customer_segment, :run_id) "
    "ON CONFLICT (customer_id) DO NOTHING"
)
_DIM_PRODUCT_SQL = (
    "INSERT INTO dim_product (product_id, product_name, category, sub_category, first_seen_run_id) "
    "VALUES (:product_id, :product_name, :category, :sub_category, :run_id) "
    "ON CONFLICT (product_id) DO NOTHING"
)
_DIM_REGION_SQL = (
    "INSERT INTO dim_region (region, state, city, first_seen_run_id) "
    "VALUES (:region, :state, :city, :run_id) "
    "ON CONFLICT (region, state, city) DO NOTHING"
)
_DIM_DATE_SQL = (
    "INSERT INTO dim_date (date_key, year, quarter, month, month_name, day, "
    "day_of_week, day_name, week_of_year, is_weekend, is_month_end) "
    "VALUES (:date_key, :year, :quarter, :month, :month_name, :day, "
    ":day_of_week, :day_name, :week_of_year, :is_weekend, :is_month_end) "
    "ON CONFLICT (date_key) DO NOTHING"
)
_FACT_SALES_SQL = (
    "INSERT INTO fact_sales ("
    "run_id, date_key, customer_key, product_key, region_key, "
    "order_id, order_date, customer_id, product_id, region, category, "
    "sub_category, customer_segment, quantity, unit_price, discount, "
    "revenue, cost, profit, payment_method, shipping_days, order_status, "
    "return_status) VALUES ("
    ":run_id, :date_key, :customer_key, :product_key, :region_key, "
    ":order_id, :order_date, :customer_id, :product_id, :region, :category, "
    ":sub_category, :customer_segment, :quantity, :unit_price, :discount, "
    ":revenue, :cost, :profit, :payment_method, :shipping_days, :order_status, "
    ":return_status)"
)


def _upsert(conn, sql: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    return conn.execute(text(sql), rows).rowcount


def load_valid_rows(csv_path: Path, run_id: int, db: Database,
                    vres: ValidationResult, contract: DataContract | None = None) -> dict:
    """Load every contract-valid row of ``csv_path`` into the star schema.

    Runs inside one transaction: dimensions are upserted first (so the fact
    rows' foreign keys are always satisfiable), then ``fact_sales`` is
    bulk-inserted. ``Revenue``/``Profit`` are re-derived, never trusted from
    the CSV (FR-05, ``docs/database-schema.md``).
    """
    contract = contract or get_contract()
    df = valid_rows_frame(csv_path, vres)
    if df.empty:
        return {"rows_loaded": 0, "customers_upserted": 0, "products_upserted": 0,
                "regions_upserted": 0, "dates_upserted": 0}

    customers = df[["Customer_ID", "Customer_Name", "Customer_Segment"]].drop_duplicates(
        "Customer_ID")
    products = df[["Product_ID", "Product_Name", "Category", "Sub_Category"]].drop_duplicates(
        "Product_ID")
    regions = df[["Region", "State", "City"]].drop_duplicates()
    dates = sorted({date.fromisoformat(d) for d in df["Order_Date"]})

    with db.transaction() as conn:
        customers_upserted = _upsert(conn, _DIM_CUSTOMER_SQL, [
            {"customer_id": r.Customer_ID, "customer_name": r.Customer_Name,
             "customer_segment": r.Customer_Segment, "run_id": run_id}
            for r in customers.itertuples()
        ])
        products_upserted = _upsert(conn, _DIM_PRODUCT_SQL, [
            {"product_id": r.Product_ID, "product_name": r.Product_Name,
             "category": r.Category, "sub_category": r.Sub_Category, "run_id": run_id}
            for r in products.itertuples()
        ])
        regions_upserted = _upsert(conn, _DIM_REGION_SQL, [
            {"region": r.Region, "state": r.State, "city": r.City, "run_id": run_id}
            for r in regions.itertuples()
        ])
        dates_upserted = _upsert(conn, _DIM_DATE_SQL, [date_dim_row(d) for d in dates])

        customer_keys = {
            cid: conn.execute(text("SELECT customer_key FROM dim_customer WHERE customer_id = :c"),
                              {"c": cid}).scalar()
            for cid in customers["Customer_ID"]
        }
        product_keys = {
            pid: conn.execute(text("SELECT product_key FROM dim_product WHERE product_id = :p"),
                              {"p": pid}).scalar()
            for pid in products["Product_ID"]
        }
        region_keys = {
            (r.Region, r.State, r.City): conn.execute(
                text("SELECT region_key FROM dim_region WHERE region = :r AND state = :s AND city = :c"),
                {"r": r.Region, "s": r.State, "c": r.City},
            ).scalar()
            for r in regions.itertuples()
        }

        fact_rows = []
        for r in df.itertuples():
            revenue, profit = recompute_revenue_profit(
                r.Quantity, r.Unit_Price, r.Discount, r.Cost)
            fact_rows.append({
                "run_id": run_id,
                "date_key": date.fromisoformat(r.Order_Date),
                "customer_key": customer_keys[r.Customer_ID],
                "product_key": product_keys[r.Product_ID],
                "region_key": region_keys[(r.Region, r.State, r.City)],
                "order_id": r.Order_ID, "order_date": date.fromisoformat(r.Order_Date),
                "customer_id": r.Customer_ID, "product_id": r.Product_ID,
                "region": r.Region, "category": r.Category, "sub_category": r.Sub_Category,
                "customer_segment": r.Customer_Segment,
                "quantity": int(float(r.Quantity)),
                "unit_price": Decimal(r.Unit_Price), "discount": Decimal(r.Discount),
                "revenue": revenue, "cost": Decimal(r.Cost), "profit": profit,
                "payment_method": r.Payment_Method or None,
                "shipping_days": int(float(r.Shipping_Days)) if r.Shipping_Days.strip() else None,
                "order_status": r.Order_Status, "return_status": r.Return_Status,
            })
        conn.execute(text(_FACT_SALES_SQL), fact_rows)

    return {
        "rows_loaded": len(fact_rows),
        "customers_upserted": customers_upserted,
        "products_upserted": products_upserted,
        "regions_upserted": regions_upserted,
        "dates_upserted": dates_upserted,
    }


# --------------------------------------------------------------------------- #
# Engine selection + retry-then-fail
# --------------------------------------------------------------------------- #
def _verify_engine_load(db: Database, run_id: int, expected_rows: int) -> dict:
    loaded = db.scalar("SELECT count(*) FROM fact_sales WHERE run_id = :r", {"r": run_id})
    if loaded != expected_rows:
        raise EtlLoadError(
            f"alteryx load produced {loaded} fact_sales rows, expected {expected_rows}"
        )
    return {"rows_loaded": int(loaded)}


def run_sales_etl_workflow(csv_path: Path, run_id: int, db: Database,
                           vres: ValidationResult, settings: AlteryxSettings | None = None,
                           contract: DataContract | None = None) -> EtlResult:
    """Workflows 3+4 - ``03_sales_etl.yxmd`` / ``04_customer_product_etl.yxmd``.

    Retries the **load** (not just an engine invocation) up to
    :data:`MAX_LOAD_ATTEMPTS` times; exhausting retries raises
    :class:`EtlLoadError`, which the orchestrator turns into a ``FAILED`` run.
    """
    settings = settings or get_alteryx_settings()
    contract = contract or get_contract()
    engine = "alteryx" if settings.is_configured() else "python_fallback"
    started = time.monotonic()

    last_error: Exception | None = None
    degraded_from_alteryx: str | None = None
    for attempt in range(1, MAX_LOAD_ATTEMPTS + 1):
        try:
            if engine == "alteryx":
                expected = len(valid_rows_frame(csv_path, vres))
                invoke_alteryx_engine(settings.workflow_path("03_sales_etl.yxmd"), csv_path, settings)
                invoke_alteryx_engine(
                    settings.workflow_path("04_customer_product_etl.yxmd"), csv_path, settings)
                summary = _verify_engine_load(db, run_id, expected)
                verified = True
            else:
                summary = load_valid_rows(csv_path, run_id, db, vres, contract)
                verified = False
            return EtlResult(engine=engine, verified=verified,
                             seconds=round(time.monotonic() - started, 3), summary=summary,
                             error=degraded_from_alteryx)
        except AlteryxExecutionError as exc:
            # Engine unavailable/failed to invoke - degrade to the Python
            # loader for the remaining attempts (never faked as having run).
            engine = "python_fallback"
            degraded_from_alteryx = str(exc)
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - a genuine load error, retried then failed
            last_error = exc
        if attempt < MAX_LOAD_ATTEMPTS:
            time.sleep(LOAD_RETRY_BACKOFF_SECONDS)

    raise EtlLoadError(
        f"star-schema load failed after {MAX_LOAD_ATTEMPTS} attempts: {type(last_error).__name__}"
    ) from last_error
