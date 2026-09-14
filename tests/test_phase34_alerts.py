"""Phase 34 (spec Phase 66, FR-25) - severity-routed alert & email engine.

Routing, alert-building, and email content assembly are pure and get direct
unit tests. SMTP send is tested against a fake `smtplib.SMTP` (never a real
socket) for both the happy path and a simulated failure; the unconfigured
path (`SMTP_HOST` unset - the `.env.example` default) is tested directly.
`dispatch_alerts` is tested with a monkeypatched `generate_recommendations`
so routing/counting logic doesn't need a database. The real
`generate_recommendations` -> email round trip and the orchestrator wiring
(Stage 10, advisory - never affects `pipeline_runs.status`) need real data,
so those are `INSIGHTFORGE_PG_INTEGRATION=1`-gated, same convention as
Phases 20/21/26/33.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import src.alerts as alerts_module
from src.alerts import (
    ALERT_ROUTES,
    Alert,
    build_alert,
    build_email,
    dispatch_alerts,
    email_body,
    email_subject,
    route_for_severity,
    send_alert_email,
)
from src.config import AlertSettings
from src.recommendations import Recommendation

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase34_test__"


def test_phase34_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "alerts.py",
        PROJECT_ROOT / "docs" / "alerts.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.alerts"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# Routing - pure
# --------------------------------------------------------------------------- #
def test_alert_routes_match_spec():
    assert ALERT_ROUTES == {
        "LOW": "dashboard", "MEDIUM": "streamlit",
        "HIGH": "email", "CRITICAL": "immediate_email",
    }


@pytest.mark.parametrize("severity,expected", [
    ("LOW", "dashboard"), ("MEDIUM", "streamlit"),
    ("HIGH", "email"), ("CRITICAL", "immediate_email"),
])
def test_route_for_severity(severity, expected):
    assert route_for_severity(severity) == expected


def test_route_for_unknown_severity_defaults_to_dashboard():
    assert route_for_severity("NOT_A_SEVERITY") == "dashboard"


# --------------------------------------------------------------------------- #
# Alert building - pure
# --------------------------------------------------------------------------- #
def _recommendation(severity="HIGH", impact_value=12000.0):
    return Recommendation(
        rule_id="demand_decline", title="Investigate declining demand & inventory levels",
        rationale="Revenue moved down 15.0% and orders moved down 12.0%.",
        metric="revenue", period_key="2026-09-08", direction="down",
        severity=severity, confidence=0.8, impact_value=impact_value,
        priority_score=60.0, priority_band="HIGH", linked_anomaly_id=None, detail={},
    )


def test_build_alert_carries_all_four_required_fields():
    alert = build_alert(_recommendation())
    assert alert.severity == "HIGH"
    assert alert.route == "email"
    assert "revenue" in alert.issue and "down" in alert.issue
    assert alert.evidence == "Revenue moved down 15.0% and orders moved down 12.0%."
    assert "12,000.00" in alert.impact
    assert alert.recommendation == "Investigate declining demand & inventory levels"


def test_build_alert_handles_none_impact():
    alert = build_alert(_recommendation(impact_value=None))
    assert "not quantified" in alert.impact.lower()


def test_email_subject_flags_critical_as_urgent():
    critical_alert = build_alert(_recommendation(severity="CRITICAL"))
    high_alert = build_alert(_recommendation(severity="HIGH"))
    assert "URGENT" in email_subject(critical_alert)
    assert "ALERT" in email_subject(high_alert) and "URGENT" not in email_subject(high_alert)


def test_email_body_contains_all_four_sections():
    alert = build_alert(_recommendation())
    body = email_body(alert)
    for label in ("Issue:", "Evidence:", "Impact:", "Recommendation:"):
        assert label in body


def _settings(configured: bool) -> AlertSettings:
    return AlertSettings(
        smtp_host="smtp.example.com" if configured else None, smtp_port=587,
        smtp_user="user", smtp_password="pw", use_tls=True,
        email_from="insightforge@example.com", email_to="manager@example.com",
    )


def test_build_email_includes_attachments(tmp_path):
    xlsx = tmp_path / "report.xlsx"
    xlsx.write_bytes(b"fake-xlsx-bytes")
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")

    alert = build_alert(_recommendation())
    msg = build_email(alert, _settings(True), attachments=[xlsx, pdf])
    filenames = [part.get_filename() for part in msg.iter_attachments()]
    assert "report.xlsx" in filenames
    assert "report.pdf" in filenames


def test_build_email_skips_missing_attachment(tmp_path):
    missing = tmp_path / "does_not_exist.xlsx"
    alert = build_alert(_recommendation())
    msg = build_email(alert, _settings(True), attachments=[missing])
    assert list(msg.iter_attachments()) == []


# --------------------------------------------------------------------------- #
# SMTP send - fake smtplib.SMTP, never a real socket
# --------------------------------------------------------------------------- #
def test_send_alert_email_unconfigured_logs_and_returns_false():
    alert = build_alert(_recommendation())
    assert send_alert_email(alert, _settings(False)) is False


class _FakeSmtp:
    sent_messages: list = []

    def __init__(self, host, port, timeout=10):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        _FakeSmtp.sent_messages.append(msg)


class _FailingSmtp(_FakeSmtp):
    def send_message(self, msg):
        raise OSError("connection refused")


def test_send_alert_email_success(monkeypatch):
    _FakeSmtp.sent_messages = []
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", _FakeSmtp)
    alert = build_alert(_recommendation(severity="CRITICAL"))
    assert send_alert_email(alert, _settings(True)) is True
    assert len(_FakeSmtp.sent_messages) == 1


def test_send_alert_email_smtp_failure_returns_false_not_raise(monkeypatch):
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", _FailingSmtp)
    alert = build_alert(_recommendation())
    assert send_alert_email(alert, _settings(True)) is False


# --------------------------------------------------------------------------- #
# dispatch_alerts - monkeypatched generate_recommendations, no database
# --------------------------------------------------------------------------- #
def test_dispatch_alerts_routes_and_counts(monkeypatch):
    recs = [
        _recommendation(severity="LOW"),
        _recommendation(severity="MEDIUM"),
        _recommendation(severity="HIGH"),
        _recommendation(severity="CRITICAL"),
    ]
    monkeypatch.setattr(alerts_module, "generate_recommendations", lambda db: recs)
    monkeypatch.setattr(alerts_module.smtplib, "SMTP", _FakeSmtp)
    _FakeSmtp.sent_messages = []

    results = dispatch_alerts(db=None, settings=_settings(True))
    assert len(results) == 4
    routes = {r["route"] for r in results}
    assert routes == {"dashboard", "streamlit", "email", "immediate_email"}
    email_sent_count = sum(1 for r in results if r["email_sent"])
    assert email_sent_count == 2  # only HIGH + CRITICAL send
    assert len(_FakeSmtp.sent_messages) == 2


def test_dispatch_alerts_no_emails_when_unconfigured(monkeypatch):
    recs = [_recommendation(severity="HIGH"), _recommendation(severity="CRITICAL")]
    monkeypatch.setattr(alerts_module, "generate_recommendations", lambda db: recs)

    results = dispatch_alerts(db=None, settings=_settings(False))
    assert all(r["email_sent"] is False for r in results)


def test_dispatch_alerts_returns_empty_list_on_recommendation_failure(monkeypatch):
    def _raise(db):
        raise RuntimeError("boom")
    monkeypatch.setattr(alerts_module, "generate_recommendations", _raise)
    assert dispatch_alerts(db=None, settings=_settings(True)) == []


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL + orchestrator wiring
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pg_integration
def test_orchestrator_dispatches_alerts_without_affecting_status(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=2000, seed=34)
    dates = sorted(df["Order_Date"].unique())[:15]
    codes = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    assert all(c in (0, 9) for c in codes)
    run_ids = [r["run_id"] for r in db.fetch_all(
        "SELECT run_id FROM pipeline_runs WHERE file_name LIKE :pat ORDER BY run_id",
        {"pat": SENTINEL + "%"},
    )]
    runs = db.fetch_all(
        "SELECT run_id, status, stage_metrics FROM pipeline_runs WHERE run_id = ANY(:ids)",
        {"ids": run_ids},
    )
    for row in runs:
        assert row["status"] in ("SUCCESS", "WARNING", "FAILED")
        assert "alert" in row["stage_metrics"]
