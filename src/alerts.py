"""InsightForge AI - severity-routed alert & email engine (Phase 34 / spec
Phase 66, FR-25).

The spec (verbatim): create ``src/alerts.py``; route by severity -
``LOW -> Dashboard``, ``MEDIUM -> Streamlit``, ``HIGH -> Email``,
``CRITICAL -> Immediate Email``; every email must contain issue, evidence,
impact, and recommendation, and attach the PDF/Excel report "where
appropriate". No trigger source, routing implementation, or email format is
specified beyond that.

**Trigger: fresh recommendations, same as reporting (Phase 33).**
`docs/data-flow.md`'s stage list runs ``... -> RECOMMEND -> EXPLAIN ->
REPORT -> ALERT -> AUDIT`` - Alert consumes the same recommendations Report
already surfaces. `src.recommendations.Recommendation` already carries
exactly the four required fields in different words: ``title`` (the
recommended action), ``rationale`` (the issue + evidence - including any
RCA/forecast corroboration notes already appended, `docs/recommendations.md`
section 6), ``severity``, and ``impact_value`` (the dollar impact). Reusing
it directly means one rule/severity/impact model everywhere, not a second
one invented for alerting.

**"Dashboard" and "Streamlit" routes need no code.** Both already display
severity-tagged data straight from Postgres: ``anomalies``/``recommendations``
rows are queried live by the Streamlit pages (Phase 30) and by Power BI's
Risk & Anomalies page (Phase 32). Routing a LOW/MEDIUM alert therefore means
classifying it and stopping there - no extra write, no extra table. Only
``HIGH``/``CRITICAL`` (the two email routes) do real work: send via SMTP.

**Wired into the orchestrator, like reporting.** Same reasoning as Phase 33
(FR-24/FR-25 both name "per run" behaviour): `src/orchestrator.py` calls
:func:`dispatch_alerts` as a new advisory stage (Stage 10, right after the
Phase 33 report stage), wrapped in ``try/except`` - an SMTP failure is
logged into ``stage_metrics`` and never fails the run
(``docs/architecture.md``: "Email send failure -> Log, keep the report
artifacts, do not fail the whole run").

**Unconfigured SMTP never fakes a send.** Same "configured means a real
attempt, else a deterministic, honest fallback" convention as
:class:`~src.config.AlteryxSettings`/:class:`~src.config.LlmSettings`: a
blank ``SMTP_HOST`` (``.env.example`` ships it blank) means every
HIGH/CRITICAL alert is logged instead of sent, and ``email_sent=False`` is
reported rather than a fabricated delivery.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from src.config import AlertSettings, get_alert_settings
from src.database import Database
from src.recommendations import Recommendation, generate_recommendations

logger = logging.getLogger(__name__)

#: The spec's own four routes, keyed by severity (FR-25 verbatim).
ALERT_ROUTES = {
    "LOW": "dashboard",
    "MEDIUM": "streamlit",
    "HIGH": "email",
    "CRITICAL": "immediate_email",
}
#: Routes that actually send an email - the other two need no code (see module docstring).
EMAIL_ROUTES = ("email", "immediate_email")

_XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
_PDF_MIME = ("application", "pdf")


def route_for_severity(severity: str) -> str:
    """One of :data:`ALERT_ROUTES`'s values; an unrecognised severity is
    treated as ``LOW`` (never silently escalated to an email route)."""
    return ALERT_ROUTES.get(severity, "dashboard")


@dataclass(frozen=True)
class Alert:
    """One routed alert - the spec's four required email fields, plus
    enough context to log/route it even when no email is sent."""

    severity: str
    route: str
    metric: str
    period_key: str
    issue: str
    evidence: str
    impact: str
    recommendation: str


# --------------------------------------------------------------------------- #
# Pure core - no database, no network
# --------------------------------------------------------------------------- #
def _format_impact(impact_value: float | None) -> str:
    if impact_value is None:
        return "Impact not quantified (insufficient rolling-baseline history)."
    return f"${abs(impact_value):,.2f} at risk."


def build_alert(rec: Recommendation) -> Alert:
    """One :class:`Alert` from a :class:`~src.recommendations.Recommendation`."""
    return Alert(
        severity=rec.severity, route=route_for_severity(rec.severity),
        metric=rec.metric, period_key=rec.period_key,
        issue=(f"{rec.metric} moved {rec.direction} significantly on "
               f"{rec.period_key} (rule: {rec.rule_id})."),
        evidence=rec.rationale,
        impact=_format_impact(rec.impact_value),
        recommendation=rec.title,
    )


def email_subject(alert: Alert) -> str:
    prefix = "URGENT" if alert.route == "immediate_email" else "ALERT"
    return f"[InsightForge {prefix}] {alert.issue}"


def email_body(alert: Alert) -> str:
    return (
        f"Severity: {alert.severity}\n"
        f"Metric: {alert.metric}  |  Period: {alert.period_key}\n\n"
        f"Issue:\n{alert.issue}\n\n"
        f"Evidence:\n{alert.evidence}\n\n"
        f"Impact:\n{alert.impact}\n\n"
        f"Recommendation:\n{alert.recommendation}\n"
    )


def build_email(
    alert: Alert, settings: AlertSettings, attachments: list[Path] | None = None,
) -> EmailMessage:
    """The full :class:`~email.message.EmailMessage`, attachments included -
    split out from :func:`send_alert_email` so tests can inspect content
    without touching a socket."""
    msg = EmailMessage()
    msg["Subject"] = email_subject(alert)
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    msg.set_content(email_body(alert))

    for path in attachments or []:
        if path is None or not path.is_file():
            continue
        maintype, subtype = _PDF_MIME if path.suffix.lower() == ".pdf" else _XLSX_MIME
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name,
        )
    return msg


# --------------------------------------------------------------------------- #
# SMTP send - the only network I/O in this module
# --------------------------------------------------------------------------- #
def send_alert_email(
    alert: Alert, settings: AlertSettings | None = None,
    attachments: list[Path] | None = None,
) -> bool:
    """``True`` iff the message was actually handed to an SMTP server.

    Never raises: an unconfigured ``SMTP_HOST`` logs the alert instead of
    sending (no fabricated delivery); any SMTP exception is caught and
    logged (``docs/architecture.md``: a send failure must not fail the run).
    No secret (``smtp_password``) is ever included in a log line
    (``docs/security.md`` section 5).
    """
    settings = settings if settings is not None else get_alert_settings()
    if not settings.is_configured():
        logger.info(
            "alerts: SMTP not configured (SMTP_HOST unset) - logging instead of "
            "sending: severity=%s metric=%s issue=%s",
            alert.severity, alert.metric, alert.issue,
        )
        return False

    msg = build_email(alert, settings, attachments)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.use_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password or "")
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - a send failure must not fail the run
        logger.warning("alerts: SMTP send failed (%s): severity=%s metric=%s",
                        type(exc).__name__, alert.severity, alert.metric)
        return False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def dispatch_alerts(
    db: Database, excel_path: Path | None = None, pdf_path: Path | None = None,
    settings: AlertSettings | None = None,
) -> list[dict]:
    """Route every current significant recommendation per FR-25's severity
    table. Returns one summary dict (``severity``, ``route``, ``metric``,
    ``email_sent``) per alert, for ``stage_metrics``/callers to inspect.

    Computes recommendations fresh via
    :func:`src.recommendations.generate_recommendations` (same on-demand
    shape reporting uses, `docs/reporting.md` section 3) rather than
    depending on another stage's in-memory result - keeps this stage
    independently retriable and testable, matching every other advisory
    orchestrator stage. Never raises: a failure to even compute
    recommendations degrades to "no alerts this run", not an exception.
    """
    try:
        recs = generate_recommendations(db)
    except Exception as exc:  # noqa: BLE001 - advisory analytics, never fails the run
        logger.warning("alerts: recommendations unavailable: %s", type(exc).__name__)
        return []

    settings = settings if settings is not None else get_alert_settings()
    attachments = [p for p in (excel_path, pdf_path) if p is not None]

    results = []
    for rec in recs:
        alert = build_alert(rec)
        email_sent = (
            send_alert_email(alert, settings, attachments)
            if alert.route in EMAIL_ROUTES else False
        )
        results.append({
            "severity": alert.severity, "route": alert.route,
            "metric": alert.metric, "email_sent": email_sent,
        })
    return results
