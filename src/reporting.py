"""InsightForge AI - automated Excel & PDF reports (Phase 33 / spec Phases
64-65, FR-24).

The spec (verbatim) is two short lists: Phase 64 - create ``src/reporting.py``,
generate ``InsightForge_Report_DATE.xlsx`` with 12 named sheets (Executive
Summary, KPIs, Regions, Categories, Products, Customers, Anomalies, Root
Causes, Impact, Recommendations, Forecast, Data Quality); Phase 65 - generate
``InsightForge_Executive_Report_DATE.pdf`` (executive summary, KPI changes,
major anomalies, root causes, impact, forecast, recommendations - "keep it
management-friendly"). No sheet layout, trigger mechanism, or date format is
specified beyond that; the rest below is this project's own documented
operational choice, following the pattern every prior spec-silent phase used.

**Trigger: per pipeline run.** ``docs/data-flow.md`` places Report as
pipeline stage 17 and FR-24 says "generated per run" - unlike Phases 22-26
(RCA/impact/RFM/forecasting/recommendations), which stayed on-demand only,
this module *is* wired into ``src/orchestrator.py`` as a new advisory stage
(same convention as Stage 7 anomaly fusion / Stage 8 drift detection:
wrapped in ``try/except``, logged into ``stage_metrics``, never changes the
run's ``status`` or exit code).

**Data assembly is computed fresh, not read back from run_id-scoped
tables** - recommendations/RCA/impact/forecast are still on-demand-only
engines (Phases 22-26) with no ``run_id`` column tying their tables to
*this* run, so :func:`generate_report_data` calls each engine directly for
the latest available period, exactly like ``src.recommendations``'s own
best-effort RCA/impact/forecast enrichment. Every section is independently
wrapped: a failed section becomes an empty table (Excel) or a one-line note
(PDF) plus an entry in :attr:`ReportData.errors`, never a raised exception -
the report itself must never fail the pipeline run (``docs/security.md``
section 5: no raw error detail is ever surfaced to output).

**Output convention** (stable - Phase 34's alert engine will attach these
same files to HIGH/CRITICAL emails): ``InsightForge_Report_<date>.xlsx`` and
``InsightForge_Executive_Report_<date>.pdf`` into ``reports/``
(``get_paths().reports``, already resolved from ``REPORTS_DIR``). ``<date>``
is the latest date the KPI data itself covers, not "today" - so a report
generated for an older backfilled file is dated by its own data, not by
wall-clock time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.change_detection import ChangeRecord, compare_period
from src.config import get_paths
from src.data_quality import DqReport
from src.database import Database
from src.forecasting import FORECAST_METRICS, ForecastPoint, forecast_metric
from src.impact_analysis import BusinessImpactResult, assess_business_impact
from src.recommendations import Recommendation, generate_recommendations
from src.root_cause import RCA_SUPPORTED_METRICS, RootCauseResult, analyze_root_cause

logger = logging.getLogger(__name__)

#: The spec's own 12 sheet names, in the spec's own order.
SHEET_NAMES = (
    "Executive Summary", "KPIs", "Regions", "Categories", "Products",
    "Customers", "Anomalies", "Root Causes", "Impact", "Recommendations",
    "Forecast", "Data Quality",
)

#: One row per view/query name - reused for both fetching and, on failure,
#: the empty-sheet fallback note.
_VIEW_SQL = {
    "KPIs": "SELECT * FROM daily_kpis ORDER BY order_date",
    "Regions": "SELECT * FROM regional_performance ORDER BY revenue DESC",
    "Categories": "SELECT * FROM category_performance ORDER BY revenue DESC",
    "Products": "SELECT * FROM product_performance ORDER BY revenue DESC",
    "Customers": "SELECT * FROM customer_performance ORDER BY revenue DESC",
}

_HEADER_COLOR = "#1F4E78"


@dataclass(frozen=True)
class ReportData:
    """Everything both the Excel workbook and the PDF summary render from.

    ``errors`` maps a sheet/section label to a short failure note - present
    only for sections that failed; a healthy report has an empty dict.
    """

    run_id: int
    report_date: str
    kpis: pd.DataFrame
    regions: pd.DataFrame
    categories: pd.DataFrame
    products: pd.DataFrame
    customers: pd.DataFrame
    anomalies: pd.DataFrame
    data_quality: pd.DataFrame
    dq_report: DqReport | None
    changes: list[ChangeRecord] = field(default_factory=list)
    root_causes: list[RootCauseResult] = field(default_factory=list)
    impact: BusinessImpactResult | None = None
    recommendations: list[Recommendation] = field(default_factory=list)
    forecasts: list[ForecastPoint] = field(default_factory=list)
    errors: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Best-effort section fetchers - never raise, log + record a note instead
# --------------------------------------------------------------------------- #
def _safe_view_df(db: Database, label: str, sql: str, errors: dict) -> pd.DataFrame:
    try:
        return pd.DataFrame(db.fetch_all(sql))
    except Exception as exc:  # noqa: BLE001 - one sheet's failure must not sink the report
        errors[label] = f"{label} unavailable: {type(exc).__name__}"
        logger.warning("reporting: %s query failed: %s", label, exc)
        return pd.DataFrame()


def _fetch_anomalies(db: Database, run_id: int, errors: dict) -> pd.DataFrame:
    try:
        rows = db.fetch_all(
            "SELECT metric, anomaly_date, grain, observed_value, expected_value, "
            "deviation_pct, direction, detector_votes, confidence, severity, "
            "persistence_days FROM anomalies WHERE run_id = :run_id "
            "ORDER BY confidence DESC NULLS LAST",
            {"run_id": run_id},
        )
        return pd.DataFrame(rows)
    except Exception as exc:  # noqa: BLE001
        errors["Anomalies"] = f"Anomalies unavailable: {type(exc).__name__}"
        logger.warning("reporting: anomalies query failed: %s", exc)
        return pd.DataFrame()


def _fetch_dq_rows(db: Database, run_id: int, errors: dict) -> list[dict]:
    """Fallback for standalone use (no in-memory ``DqReport`` available):
    the persisted ``data_quality_results`` rows for this run."""
    try:
        return db.fetch_all(
            "SELECT dimension, score, passed, records_checked, records_failed "
            "FROM data_quality_results WHERE run_id = :run_id ORDER BY dimension",
            {"run_id": run_id},
        )
    except Exception as exc:  # noqa: BLE001
        errors["Data Quality"] = f"Data Quality unavailable: {type(exc).__name__}"
        logger.warning("reporting: data quality query failed: %s", exc)
        return []


def _fetch_changes(db: Database, errors: dict) -> list[ChangeRecord]:
    try:
        return [c for c in compare_period(db, "day") if c.significant]
    except Exception as exc:  # noqa: BLE001
        errors["changes"] = f"period comparison unavailable: {type(exc).__name__}"
        logger.warning("reporting: period comparison failed: %s", exc)
        return []


def _fetch_recommendations(db: Database, errors: dict) -> list[Recommendation]:
    try:
        return generate_recommendations(db)
    except Exception as exc:  # noqa: BLE001
        errors["Recommendations"] = f"Recommendations unavailable: {type(exc).__name__}"
        logger.warning("reporting: recommendations failed: %s", exc)
        return []


def _fetch_root_causes(
    db: Database, changes: list[ChangeRecord], errors: dict,
) -> list[RootCauseResult]:
    results: list[RootCauseResult] = []
    changed_by_metric = {c.metric: c for c in changes}
    for metric in RCA_SUPPORTED_METRICS:
        change = changed_by_metric.get(metric)
        if change is None:
            continue
        try:
            results.append(analyze_root_cause(
                db, metric,
                change.period_key, change.period_key,
                change.previous_period_key, change.previous_period_key,
            ))
        except Exception as exc:  # noqa: BLE001
            errors[f"Root Causes:{metric}"] = f"{metric} RCA unavailable: {type(exc).__name__}"
            logger.warning("reporting: root cause for %s failed: %s", metric, exc)
    return results


def _fetch_impact(db: Database, errors: dict) -> BusinessImpactResult | None:
    try:
        return assess_business_impact(db)
    except Exception as exc:  # noqa: BLE001
        errors["Impact"] = f"Impact unavailable: {type(exc).__name__}"
        logger.warning("reporting: business impact failed: %s", exc)
        return None


def _fetch_forecasts(db: Database, errors: dict) -> list[ForecastPoint]:
    points: list[ForecastPoint] = []
    for metric in FORECAST_METRICS:
        try:
            metric_points = forecast_metric(db, metric, horizon=7)
            if metric_points:
                points.extend(metric_points)
        except Exception as exc:  # noqa: BLE001
            errors[f"Forecast:{metric}"] = f"{metric} forecast unavailable: {type(exc).__name__}"
            logger.warning("reporting: forecast for %s failed: %s", metric, exc)
    return points


def generate_report_data(
    db: Database, run_id: int, dq_report: DqReport | None = None,
) -> ReportData:
    """Assemble every sheet/section fresh for ``run_id``.

    ``dq_report`` lets the orchestrator pass the just-computed in-memory
    :class:`~src.data_quality.DqReport` straight through (avoiding a
    redundant query and a race against its own not-yet-committed insert);
    standalone callers (e.g. :mod:`scripts.generate_report`) omit it and
    the Data Quality sheet is read back from the persisted table instead.
    """
    errors: dict[str, str] = {}

    kpis = _safe_view_df(db, "KPIs", _VIEW_SQL["KPIs"], errors)
    regions = _safe_view_df(db, "Regions", _VIEW_SQL["Regions"], errors)
    categories = _safe_view_df(db, "Categories", _VIEW_SQL["Categories"], errors)
    products = _safe_view_df(db, "Products", _VIEW_SQL["Products"], errors)
    customers = _safe_view_df(db, "Customers", _VIEW_SQL["Customers"], errors)
    anomalies = _fetch_anomalies(db, run_id, errors)

    report_date = str(kpis["order_date"].max()) if not kpis.empty else "unknown"

    if dq_report is not None:
        dq_rows = [
            {"dimension": d.dimension, "score": d.score, "passed": d.passed,
             "records_checked": d.records_checked, "records_failed": d.records_failed}
            for d in dq_report.dimensions
        ]
    else:
        dq_rows = _fetch_dq_rows(db, run_id, errors)

    changes = _fetch_changes(db, errors)

    return ReportData(
        run_id=run_id, report_date=report_date,
        kpis=kpis, regions=regions, categories=categories, products=products,
        customers=customers, anomalies=anomalies, data_quality=pd.DataFrame(dq_rows),
        dq_report=dq_report, changes=changes,
        root_causes=_fetch_root_causes(db, changes, errors),
        impact=_fetch_impact(db, errors),
        recommendations=_fetch_recommendations(db, errors),
        forecasts=_fetch_forecasts(db, errors),
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# Dataclass-list -> DataFrame helpers (shared by Excel + PDF)
# --------------------------------------------------------------------------- #
def _recommendations_df(recs: list[Recommendation]) -> pd.DataFrame:
    return pd.DataFrame([
        {"rule_id": r.rule_id, "title": r.title, "rationale": r.rationale,
         "metric": r.metric, "period_key": r.period_key, "direction": r.direction,
         "severity": r.severity, "confidence": r.confidence,
         "impact_value": r.impact_value, "priority_score": r.priority_score,
         "priority_band": r.priority_band}
        for r in recs
    ])


def _root_causes_df(results: list[RootCauseResult]) -> pd.DataFrame:
    return pd.DataFrame([
        {"metric": r.metric, "change": r.change, "primary_driver": r.primary_driver,
         "contribution": r.contribution, "confidence": r.confidence}
        for r in results
    ])


def _forecasts_df(points: list[ForecastPoint]) -> pd.DataFrame:
    return pd.DataFrame([
        {"metric": p.metric, "horizon_days": p.horizon_days, "forecast_date": p.forecast_date,
         "forecast_value": p.forecast_value, "lower_bound": p.lower_bound,
         "upper_bound": p.upper_bound, "model": p.model, "mae": p.mae, "rmse": p.rmse,
         "mape": p.mape}
        for p in points
    ])


def _impact_df(impact: BusinessImpactResult | None) -> pd.DataFrame:
    if impact is None:
        return pd.DataFrame()
    return pd.DataFrame([{
        "date": impact.date,
        "expected_revenue": impact.expected_revenue, "actual_revenue": impact.actual_revenue,
        "revenue_gap": impact.revenue_gap, "expected_profit": impact.expected_profit,
        "actual_profit": impact.actual_profit, "profit_gap": impact.profit_gap,
        "revenue_at_risk": impact.revenue_at_risk, "profit_at_risk": impact.profit_at_risk,
        "customers_affected": impact.customers_affected, "orders_affected": impact.orders_affected,
    }])


def _executive_summary_df(data: ReportData) -> pd.DataFrame:
    latest_kpi = data.kpis.iloc[-1].to_dict() if not data.kpis.empty else {}
    top_recs = sorted(data.recommendations, key=lambda r: r.priority_score, reverse=True)
    severity_counts = (
        data.anomalies["severity"].value_counts().to_dict()
        if not data.anomalies.empty and "severity" in data.anomalies.columns else {}
    )
    rows = [
        ("Report date", data.report_date),
        ("Revenue (latest day)", latest_kpi.get("revenue")),
        ("Profit (latest day)", latest_kpi.get("profit")),
        ("Margin % (latest day)", latest_kpi.get("margin_pct")),
        ("Orders (latest day)", latest_kpi.get("orders")),
        ("Significant period-over-period moves", len(data.changes)),
        ("Anomalies this run", len(data.anomalies)),
        ("Anomalies by severity",
         ", ".join(f"{k}: {v}" for k, v in severity_counts.items()) or "none"),
        ("Data quality gate", data.dq_report.gate if data.dq_report else "n/a"),
        ("Data quality score", data.dq_report.overall_score if data.dq_report else "n/a"),
        ("Top recommendation", top_recs[0].title if top_recs else "none"),
    ]
    return pd.DataFrame(rows, columns=["Metric", "Value"])


# --------------------------------------------------------------------------- #
# Excel (Phase 64) - openpyxl/XlsxWriter, one sheet per spec name
# --------------------------------------------------------------------------- #
def write_excel_report(data: ReportData, path: Path) -> None:
    """The 12-sheet workbook, in the spec's own sheet order. A section that
    failed (see :attr:`ReportData.errors`) or has no rows for this run
    renders as a one-row "note" sheet rather than being omitted - every
    spec-named sheet is always present."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sheets = {
        "Executive Summary": _executive_summary_df(data),
        "KPIs": data.kpis,
        "Regions": data.regions,
        "Categories": data.categories,
        "Products": data.products,
        "Customers": data.customers,
        "Anomalies": data.anomalies,
        "Root Causes": _root_causes_df(data.root_causes),
        "Impact": _impact_df(data.impact),
        "Recommendations": _recommendations_df(data.recommendations),
        "Forecast": _forecasts_df(data.forecasts),
        "Data Quality": data.data_quality,
    }
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        workbook = writer.book
        header_format = workbook.add_format(
            {"bold": True, "bg_color": _HEADER_COLOR, "font_color": "white"}
        )
        for name in SHEET_NAMES:
            df = sheets[name]
            if df.empty:
                df = pd.DataFrame({"note": [data.errors.get(name, "no data for this run")]})
            df.to_excel(writer, sheet_name=name, index=False)
            worksheet = writer.sheets[name]
            for col_idx, col_name in enumerate(df.columns):
                worksheet.write(0, col_idx, col_name, header_format)
                longest = df[col_name].astype(str).str.len().max()
                width = max(12, min(40, int(longest) + 2 if pd.notna(longest) else 12))
                worksheet.set_column(col_idx, col_idx, width)


# --------------------------------------------------------------------------- #
# PDF (Phase 65) - reportlab platypus, management-friendly summary
# --------------------------------------------------------------------------- #
def _table(df: pd.DataFrame) -> Table:
    rows = [list(df.columns)] + df.astype(object).where(pd.notna(df), "").values.tolist()
    table = Table(rows, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_HEADER_COLOR)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def write_pdf_report(data: ReportData, path: Path) -> None:
    """The management-friendly summary: executive summary, KPI changes,
    major anomalies, root causes, impact, forecast, recommendations -
    the spec's Phase 65 list, in that order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path), pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    story = [
        Paragraph("InsightForge AI - Executive Report", styles["Title"]),
        Paragraph(f"Report date: {data.report_date} | Run #{data.run_id}", styles["Normal"]),
        Spacer(1, 0.2 * inch),
    ]

    story.append(Paragraph("Executive Summary", styles["Heading2"]))
    story.append(_table(_executive_summary_df(data)))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("KPI Changes", styles["Heading2"]))
    if data.changes:
        change_df = pd.DataFrame([
            {"Metric": c.metric, "Direction": c.direction, "% Change": c.pct_change,
             "Current": c.current, "Previous": c.previous}
            for c in data.changes
        ])
        story.append(_table(change_df))
    else:
        story.append(Paragraph("No significant period-over-period moves.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Major Anomalies", styles["Heading2"]))
    if not data.anomalies.empty:
        cols = [c for c in ("metric", "anomaly_date", "direction", "severity", "confidence")
                if c in data.anomalies.columns]
        top_anomalies = data.anomalies.sort_values(
            "confidence", ascending=False, na_position="last",
        ).head(10)
        story.append(_table(top_anomalies[cols]))
    else:
        story.append(Paragraph("No anomalies detected in this run.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Root Causes", styles["Heading2"]))
    if data.root_causes:
        story.append(_table(_root_causes_df(data.root_causes)))
    else:
        story.append(Paragraph("No root-cause analysis triggered this run.", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Business Impact", styles["Heading2"]))
    if data.impact is not None:
        story.append(_table(_impact_df(data.impact)))
    else:
        story.append(Paragraph(
            "Business impact unavailable (insufficient rolling-baseline history).",
            styles["Normal"],
        ))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Forecast (7-day)", styles["Heading2"]))
    if data.forecasts:
        forecast_df = _forecasts_df(data.forecasts)[
            ["metric", "forecast_date", "forecast_value", "lower_bound", "upper_bound"]
        ]
        story.append(_table(forecast_df))
    else:
        story.append(Paragraph("Forecast unavailable (insufficient daily history).", styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Recommendations", styles["Heading2"]))
    if data.recommendations:
        top_recs = sorted(data.recommendations, key=lambda r: r.priority_score, reverse=True)[:10]
        story.append(_table(pd.DataFrame([
            {"Title": r.title, "Severity": r.severity, "Priority": r.priority_score,
             "Band": r.priority_band}
            for r in top_recs
        ])))
    else:
        story.append(Paragraph("No recommendations triggered this run.", styles["Normal"]))

    doc.build(story)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def generate_reports(
    db: Database, run_id: int, dq_report: DqReport | None = None,
    reports_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Assemble :class:`ReportData` then write both dated files into
    ``reports_dir`` (default: ``get_paths().reports``). Returns
    ``(excel_path, pdf_path)``. Never raises: every section it depends on is
    already best-effort, and the two ``write_*`` calls are pure formatting
    over already-safe data.

    ``reports_dir`` is accepted explicitly (rather than always resolving
    ``get_paths()`` internally) so a caller that already resolved
    :class:`~src.config.PipelinePaths` for a run - ``src.orchestrator``, or a
    test with a monkeypatched ``get_paths`` - writes to the same directory
    without a second, independently-monkeypatched lookup.
    """
    data = generate_report_data(db, run_id, dq_report)
    reports_dir = reports_dir if reports_dir is not None else get_paths().reports
    reports_dir.mkdir(parents=True, exist_ok=True)
    excel_path = reports_dir / f"InsightForge_Report_{data.report_date}.xlsx"
    pdf_path = reports_dir / f"InsightForge_Executive_Report_{data.report_date}.pdf"
    write_excel_report(data, excel_path)
    write_pdf_report(data, pdf_path)
    return excel_path, pdf_path
