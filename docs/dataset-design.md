# InsightForge AI - Dataset Design

> Phase 1 deliverable (spec Phase 4). The retail dataset the generator produces
> (spec Phase 5+, >=150,000 rows, fixed `RANDOM_SEED`, split into daily
> `sales_YYYY_MM_DD.csv` files). One row = one order line.

## Field dictionary (22 fields)

| # | Field | Type | Description | Constraints / range | Nullable | Example |
|--:|-------|------|-------------|---------------------|----------|---------|
| 1 | `Order_ID` | string | Business key for the order | Pattern `ORD-<8 digits>`; unique per order (repeats across lines of a multi-item order) | no | `ORD-00184722` |
| 2 | `Order_Date` | date | Date the order was placed | ISO `YYYY-MM-DD`; within the generated window; not in the future | no | `2026-08-14` |
| 3 | `Customer_ID` | string | Business key for the customer | Pattern `CUST-<6 digits>`; must exist in the customer set | no | `CUST-004821` |
| 4 | `Customer_Name` | string | Display name | 2-60 chars; consistent per `Customer_ID` | no | `Ananya Rao` |
| 5 | `Customer_Segment` | category | Customer segment | one of `Consumer`, `Corporate`, `Home Office` | no | `Corporate` |
| 6 | `Product_ID` | string | Business key for the product | Pattern `PROD-<5 digits>`; must exist in the product set | no | `PROD-01930` |
| 7 | `Product_Name` | string | Display name | 2-80 chars; consistent per `Product_ID` | no | `Laptop X 14"` |
| 8 | `Category` | category | Top-level product category | one of `Electronics`, `Furniture`, `Office Supplies`, `Clothing`, `Home & Kitchen` | no | `Electronics` |
| 9 | `Sub_Category` | category | Sub-category | valid child of `Category` (e.g. `Electronics -> Laptop, Phone, Accessories`) | no | `Laptop` |
| 10 | `Region` | category | Indian sales region | one of `North`, `South`, `East`, `West`, `Central` | no | `West` |
| 11 | `State` | category | Indian state | valid child of `Region` | no | `Maharashtra` |
| 12 | `City` | category | Indian city | valid child of `State` | no | `Pune` |
| 13 | `Quantity` | integer | Units ordered on this line | `>= 1` (bad-data injection may create `<= 0`) | no | `2` |
| 14 | `Unit_Price` | decimal(10,2) | Price per unit before discount (INR) | `> 0`; category-realistic (bad-data injection may create extremes) | no | `54990.00` |
| 15 | `Discount` | decimal(4,3) | Fractional discount | `0.000 - 0.800` (bad-data injection may create `<0` or `>1`) | no | `0.100` |
| 16 | `Revenue` | decimal(12,2) | Line revenue (INR) | **derived**: `Quantity * Unit_Price * (1 - Discount)`; `>= 0` | no | `98982.00` |
| 17 | `Cost` | decimal(12,2) | Line cost of goods (INR) | `> 0`; `< Revenue` in the typical case | no | `81000.00` |
| 18 | `Profit` | decimal(12,2) | Line profit (INR) | **derived**: `Revenue - Cost`; may be negative | no | `17982.00` |
| 19 | `Payment_Method` | category | Payment channel | one of `UPI`, `Credit Card`, `Debit Card`, `Net Banking`, `COD`, `Wallet` | no | `UPI` |
| 20 | `Shipping_Days` | integer | Days from order to delivery | `0 - 21` (anomaly injection may inflate this) | yes (pending orders) | `4` |
| 21 | `Order_Status` | category | Fulfilment status | one of `Completed`, `Pending`, `Cancelled`, `Returned` | no | `Completed` |
| 22 | `Return_Status` | category | Whether the line was returned | one of `Not Returned`, `Returned`; consistent with `Order_Status` | no | `Not Returned` |

## Derived-field rules (spec Phase 6 - business data model)

```
Revenue = Quantity * Unit_Price * (1 - Discount)
Profit  = Revenue - Cost
Margin  = Profit / Revenue            (analytics only, not stored per row)
```

The generator computes `Revenue` and `Profit` from the base fields so the dataset
is internally consistent; the ETL re-derives and asserts them on load.

## Geography hierarchy (spec Phase 9)

`Region -> State -> City` relationships are fixed and valid: every `City` belongs
to exactly one `State`, every `State` to exactly one `Region`. The generator
draws from a curated Indian region/state/city table (**5 regions, >=20 states,
>=45 cities**; every region has at least 3 states); invalid combinations only
appear via controlled bad-data injection.

Each city carries an internal **demand weight** (tier `metro` / `large` / `other`
- *not* a CSV column), so metros (Mumbai, New Delhi, Bengaluru, Hyderabad,
Chennai, Kolkata, Pune, Ahmedabad) receive a disproportionate share of orders.
Metro deliveries also run ~1 day faster (`METRO_SHIPPING_BONUS`).

## Category hierarchy

`Category -> Sub_Category` is fixed. `Product_ID` maps to exactly one
`Product_Name`, `Category`, and `Sub_Category`.

## Customer behaviour model (spec Phase 7)

Segments are unchanged (`Consumer`, `Corporate`, `Home Office`). About **15% of
customers are high-frequency "repeat" buyers**: generation assigns each customer
an internal lognormal `purchase_weight` and an `is_repeat` flag (neither is a CSV
column) and selects the ordering customer in proportion to that weight, so a
minority of customers generate a large share of order lines. `Corporate` and
`Home Office` customers buy in larger quantities and take deeper discounts than
`Consumer` (`SEGMENT_QUANTITY_WEIGHTS` / `SEGMENT_DISCOUNT_WEIGHTS`; `Consumer`
reuses the global baselines). Category mix by segment is deferred (Phase 24
candidate).

## Product popularity model (spec Phase 8)

Each product gets an internal lognormal `appeal` weight (not a CSV column) and is
selected in proportion to it, producing Pareto-like revenue concentration
(top-decile products carry a large majority of revenue). The catalog shape is
unchanged (5 categories, same sub-categories); "products behave differently" is
realised through `appeal` dispersion, not new fields.

## Seasonality model (spec Phase 10)

The generator does **not** spread orders uniformly across the date window. A pure,
deterministic helper `day_seasonality_weights(start, end)` produces one demand
multiplier per calendar date, and `generate_orders` draws each order's
`Order_Date` in proportion to it (`rng.choice(n_days, p=...)`). Only the
**distribution of `Order_Date`** changes - no row-level field (Quantity, Discount,
Unit_Price, returns, shipping) and no total-row-count change. The multiplier is
the product of four independent effects, and the whole vector is mean-normalised
to `1.0` so expected volume is unchanged:

| Effect | Constant(s) | Rule |
|--------|-------------|------|
| **Weekends** | `WEEKEND_UPLIFT = 1.25` | Saturday / Sunday demand is 25% higher. |
| **Month-end** | `MONTH_END_DAYS = 3`, `MONTH_END_UPLIFT = 1.20` | The last 3 calendar days of every month get a 20% payday / month-end-target push. |
| **Festive periods** | `FESTIVE_PERIODS` | Curated Indian shopping-sale windows `(name, start, end, multiplier)`: Republic Day (×1.35), Holi (×1.20), financial-year-end (×1.30), Akshaya Tritiya (×1.15), Independence Day (×1.45), Raksha Bandhan (×1.20). Overlapping windows take the **max** multiplier - they do not compound. Dates are curated / approximate, not astronomically exact. |
| **Seasonal demand** | `SEASONAL_AMPLITUDE = 0.15`, `SEASONAL_PEAK_DOY = 315` | Smooth annual curve `1 + A·cos(2π·(doy − peak) / 365.25)`, peaking in mid-November and troughing in mid-May: demand sags through late spring, then climbs into the Oct–Nov festive quarter. |

`day_seasonality_weights` is side-effect-free and independently unit-tested
(`tests/test_phase04_seasonality.py`); `summarise()` reports weekend-vs-weekday
orders/day and the festive-window order share so the effect is visible on every
run.

## Internal generator-only columns

`appeal`, `purchase_weight`, `is_repeat`, `city_weight`, `base_price`,
`cost_ratio` live only on the generator's reference tables. `validate_consistency`
asserts none of them reach the dataset - the CSV is exactly the 22 fields above.

## Controlled imperfections (spec Phases 11-12)

`generate_dataset()` returns the **clean, validated** frame. Phase 5 layers two
deterministic injectors on top to build the *delivery* dataset the pipeline
actually ingests (`build_delivery_dataset()`):

1. `inject_business_anomaly()` - the planted anomaly (below).
2. `inject_data_quality_issues()` - the bad-data taxonomy (below).

Both use child RNGs (`seed + 1`, `seed + 2`) so the clean generator's stream is
untouched and every existing invariant test still passes. The CLI writes the
delivery dataset by default; `--clean` (also `--no-anomaly` / `--no-issues`)
restores the pristine file.

### Data-quality injection (spec Phase 11)

A small disjoint set of rows (~1.2% at 150k) is corrupted, each defect recorded
in an **issue log** (`data/full_dataset_issues.csv`, columns
`Order_ID, row_pos, issue_type, dq_dimension, detail`) that is the ground truth
for the Phase 14 data-quality engine. `Revenue` / `Profit` are **not** re-derived
after corruption, so a bad `Quantity` / `Unit_Price` / `Discount` also carries a
deliberate Consistency defect. Counts scale linearly with the row count.

| `issue_type` | DQ dimension | count @150k | how |
|--------------|--------------|------------:|-----|
| `null_mandatory` | Completeness | 400 | a mandatory cell (`Customer_ID`, `City`, `Quantity`, …) set to NULL |
| `duplicate_row` | Uniqueness | 250 | verbatim row copies appended (same `Order_ID`) |
| `invalid_date` | Timeliness | 150 | `"2026-13-40"`, `"31/02/2026"`, `"2027-06-01"` (future), `""` |
| `negative_quantity` | Validity | 150 | `Quantity` set to `0`, `-1`, `-3` |
| `invalid_discount` | Validity | 150 | `Discount` set to `-0.10`, `1.50`, `2.0` |
| `unknown_customer_id` | Referential Integrity | 120 | `Customer_ID` set to `CUST-999999`, `UNKNOWN`, `cust-abc` |
| `invalid_category` | Referential Integrity | 120 | `Category` set to `Gadgets`, `misc`, `ELECTRONICS!!` |
| `extreme_price` | Accuracy | 120 | `Unit_Price` set to `0.0`, `99_999_999.0`, `-500.0` |
| `inconsistent_text` | Consistency | 300 | case / whitespace noise on `Customer_Segment` / `Region` / `Payment_Method` |

### Business anomaly (documented ground truth) (spec Phase 12)

Planted over `2026-08-01 .. 2026-09-08`, two tiers:

- **Primary** (`West -> Electronics -> Laptop`, the RCA leaf): drop 55% of orders,
  `Discount +0.20`, `+30 pp` returns, `Shipping_Days +10`.
- **Secondary** (`West -> Electronics`, non-Laptop): drop 18%, `Discount +0.05`,
  `+6 pp` returns, `Shipping_Days +3` - so the Region and Category roll-ups also
  move.

Anomaly rows stay internally consistent (Revenue / Profit re-derived) - they are
*abnormal but valid*, not bad data. The realised before/after deltas (anomaly
window vs the preceding 39-day baseline window, per tier + whole-business) are
written to [`anomaly-ground-truth.md`](anomaly-ground-truth.md) on every run and
consumed by the change-detection / anomaly / RCA / impact phases (spec 33-46).

## Daily files (spec Phase 13)

The full dataset is split by `Order_Date` into `data/incoming/sales_YYYY_MM_DD.csv`
(e.g. `sales_2026_08_01.csv` ... `sales_2026_09_08.csv`), plus a held-out
`sales_2026_09_09.csv` for the final end-to-end demo.

## Generator

`scripts/generate_dataset.py` (new Phases 2-5) produces the retail dataset.
`generate_dataset()` builds the **clean baseline**: 150,000 internally consistent
rows over 2026-01-01..2026-09-08, deterministic for a given `--seed` (default
`20260909`), enforcing the derived-field rules above via `validate_consistency()`.
Phase 3 adds weighted realism (product `appeal`, repeat-customer
`purchase_weight`, segment-driven basket/discount, metro-weighted city demand);
Phase 4 adds the seasonality engine (weekend / month-end / festive / annual-curve
demand weighting); Phase 5 adds `build_delivery_dataset()`, which layers the
controlled imperfections + planted anomaly above and writes the dirty
`data/full_dataset.csv`, the issue log `data/full_dataset_issues.csv` (both
git-ignored), and `docs/anomaly-ground-truth.md`. The daily-file split is Phase 6.

```
python scripts/generate_dataset.py --rows 150000 --seed 20260909   # delivery (dirty) dataset
python scripts/generate_dataset.py --clean                          # pristine baseline
```
