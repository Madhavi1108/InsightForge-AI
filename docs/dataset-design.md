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
draws from a curated Indian region/state/city table; invalid combinations only
appear via controlled bad-data injection.

## Category hierarchy

`Category -> Sub_Category` is fixed. `Product_ID` maps to exactly one
`Product_Name`, `Category`, and `Sub_Category`.

## Controlled imperfections (spec Phases 11-12, generated later)

- **Bad data (reproducible)**: NULLs, duplicate rows, invalid dates, negative
  quantities, discounts `<0` or `>1`, unknown `Customer_ID` / `Category`, extreme
  prices, inconsistent text casing.
- **Business anomaly (documented ground truth)**: for a selected period,
  `West -> Electronics -> Laptop`: Orders down, Revenue down, Returns up,
  Shipping_Days up, Discount up. Recorded in `docs/anomaly-ground-truth.md` when
  the generator is built (new Phase 5) and used to validate anomaly detection and
  RCA.

## Daily files (spec Phase 13)

The full dataset is split by `Order_Date` into `data/incoming/sales_YYYY_MM_DD.csv`
(e.g. `sales_2026_08_01.csv` ... `sales_2026_09_08.csv`), plus a held-out
`sales_2026_09_09.csv` for the final end-to-end demo.
