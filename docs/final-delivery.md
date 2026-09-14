# InsightForge AI - Security, Testing, Performance & Final Delivery

> Phase 36 deliverable (spec Phases 68-69, the final phase). Ties together
> the security controls, test-scenario coverage, and performance budgets
> every prior phase already built, adds the few pieces that were
> genuinely missing, and documents the final end-to-end demo.

## 1. What the spec says and what it leaves open

**Phase 68** (verbatim): Security - ensure `.env` secrets, no API keys in
Git, SQL injection protection, safe LLM queries, sanitized logs. Testing -
test 20 named scenarios (listed in section 3 below). Performance - measure
ingestion/ETL/DB load/SQL/analytics/report generation/total pipeline time.

**Phase 69** (verbatim): "This is the final acceptance phase. Run the
entire system using a NEW dataset file. Place `sales_2026_09_09.csv` into
`data/incoming/`." `docs/PHASE_MAP.md`'s "Final killer demo" note adds:
"...then ask the AI Analyst 'Why did revenue decrease?'"

By Phase 36, almost everything the spec asks for already exists - Phases
0-35 are all `COMPLETE`. This phase's real job was to find what was
*missing*, not to rebuild what already works, and to tie it all into one
place a reviewer can check against.

## 2. Security checklist

| Item | Enforced by |
|---|---|
| `.env` secrets never committed | `tests/test_phase00_scaffold.py::test_env_file_is_not_committed` |
| `.gitignore` excludes secrets | `tests/test_phase00_scaffold.py::test_gitignore_excludes_secrets` |
| `.env.example` carries no real key | `tests/test_phase00_scaffold.py::test_env_example_is_committed_and_has_no_real_secrets` |
| No leaked secret anywhere git tracks (new) | `tests/test_phase36_final_delivery.py::test_no_leaked_secrets_in_tracked_files` |
| SQL injection protection | `tests/test_phase09_database.py::test_parameterised_read_is_injection_safe` (every statement is bound-parameter only, `src/database.py`) |
| Sanitized error messages / logs | `tests/test_phase09_database.py::test_sanitise_never_leaks_credentials_or_sql`; structured JSON logging (Phase 35, `src/observability.py`) never includes raw exception text, only `type(exc).__name__` |
| Safe LLM queries / table whitelist | `tests/test_phase29_nl_to_sql.py` (`validate_sql` blocks anything outside a read-only, whitelisted-table grammar) |
| Prompt-injection protection | `tests/test_phase28_ai_analyst.py` (grounding: every number in an answer must trace to the evidence package, never invented) |

The only genuinely new control this phase adds is the repo-wide secret
scan - every prior control already existed and was already tested; this
table just makes the mapping explicit (`docs/security.md` section 7 had
the same table without the "enforced by" test names filled in for the last
row).

## 3. Testing - the spec's 20 named scenarios

`tests/test_phase36_final_delivery.py::SCENARIO_COVERAGE` is the
authoritative mapping (spec wording -> `module::test_function`), and
`test_scenario_coverage_reference_exists` machine-checks every entry still
exists (so this table can't silently go stale):

| Scenario | Covered by |
|---|---|
| Valid file | `test_phase13_star_schema_etl::test_clean_file_loads_fact_sales_and_closes_success` |
| Missing columns | `test_phase11_schema_validation::test_structural_fail_missing_column` |
| Extra columns | `test_phase11_schema_validation::test_structural_fail_extra_column` |
| Duplicate file | `test_phase10_file_ingestion::test_ingest_duplicate_skips_and_archives` |
| Duplicate rows | `test_phase14_data_quality_engine::test_uniqueness_flags_full_row_duplicates` |
| NULLs | `test_phase02_generator::test_mandatory_columns_non_null` |
| Invalid date | `test_phase11_schema_validation::test_field_violation` |
| Negative quantity | `test_phase36_final_delivery::test_negative_quantity_rejected` (new) |
| Invalid discount | `test_phase05_dataquality_anomaly::test_invalid_discounts_present` |
| Unknown category | `test_phase36_final_delivery::test_unknown_category_rejected` (new) |
| Empty file | `test_phase10_file_ingestion::test_ingest_empty_file_raises_without_touching_db_or_file` |
| Database unavailable | `test_phase10_file_ingestion::test_orchestrator_run_file_returns_4_when_db_unavailable` |
| Email failure | `test_phase34_alerts::test_send_alert_email_smtp_failure_returns_false_not_raise` |
| Anomaly | `test_phase20_anomaly_fusion::test_obvious_spike_is_fused_with_all_flags` |
| No anomaly | `test_phase20_anomaly_fusion::test_flat_series_produces_no_anomalies` |
| Drift | `test_phase21_drift_detection::test_detect_drift_returns_result_with_enough_rows` |
| RCA | `test_phase22_root_cause::test_analyze_root_cause_assembles_flat_and_tiered_result` |
| Forecast | `test_phase25_forecasting::test_forecast_metric_happy_path_produces_horizon_rows` |
| AI | `test_phase28_ai_analyst::test_answer_question_routes_all_six_spec_questions` |
| NL-to-SQL | `test_phase29_nl_to_sql::test_ask_blocks_unsafe_generated_sql` |

Two scenarios ("Negative quantity", "Unknown category") existed only under
generic contract-violation names (`out_of_range`, `not_in_allowed_values`)
before this phase - `test_phase36_final_delivery.py` adds the literally-
named tests so the spec's own wording is traceable, without duplicating
`test_phase11_schema_validation.py`'s broader fixture set.

## 4. Performance budget

`docs/requirements.md`'s budget table, and how each row is verified:

| Stage | Budget | Verified by |
|---|---|---|
| Ingestion + fingerprint | < 2 s | `pipeline_runs.stage_metrics.ingest.seconds` (Phase 10/35) |
| ETL (Python fallback) | < 20 s | `stage_metrics.etl.seconds` (Phase 13/35) |
| Report generation (Excel + PDF) | < 15 s | `stage_metrics.report.seconds` (Phase 33/35) |
| Total pipeline | < 90 s | `pipeline_runs.duration_s` |
| Database load | < 10 s | *(inside the ETL stage's own timing above - not split out separately)* |
| SQL KPIs + views | < 5 s | *(ad hoc SQL views, Phase 16 - not a per-file orchestrator stage; not asserted here)* |
| Analytics + intelligence | < 30 s | *(RCA/impact/RFM/forecasting/recommendations, Phases 22-26 - on-demand modules, not per-file orchestrator stages; not asserted here)* |

`tests/test_phase36_final_delivery.py::test_stage_durations_within
_documented_budget` (`INSIGHTFORGE_PG_INTEGRATION=1`-gated) asserts the
four directly-measurable rows against a live run. The last three rows
aren't split into their own orchestrator stage timers (database load is
folded into the ETL stage; SQL/analytics are on-demand modules invoked
separately, e.g. from the Streamlit pages or the reporting stage) - listed
here for completeness rather than silently omitted.

## 5. Final end-to-end delivery (the "killer demo")

`scripts/run_final_demo.py` automates Phase 69 exactly: copies
`data/sales_2026_09_09.csv` (already generated by Phase 6, held outside
`data/incoming/` for this exact moment - `docs/dataset-design.md`) into
`data/incoming/`, runs it through `src.orchestrator.run_file` (every stage
from ingestion through alerting happens automatically - FR-28's "no manual
stage execution"), then asks the AI Analyst the demo's own question and
prints the grounded answer (summary, root cause, impact, recommendation,
evidence).

**This sandbox has no Docker and no PostgreSQL installed**
(`docker --version` -> command not found), so the actual live run could
not be executed in this session - consistent with every
`INSIGHTFORGE_PG_INTEGRATION=1`-gated test since Phase 9, none of which
have run live here either. Per development rule 1 ("never fake
functionality"), this is stated plainly rather than fabricating a
transcript. To run the real demo:

```powershell
docker compose -f config/docker-compose.postgres.yml up -d
python scripts/apply_schema.py
python scripts/run_final_demo.py
```

`tests/test_phase36_final_delivery.py::test_final_demo_dataset_runs_end
_to_end_and_ai_analyst_answers` is the automated (also
`INSIGHTFORGE_PG_INTEGRATION=1`-gated) version of the same demo, run
against a temporary incoming directory rather than the real one.

## 6. Deliberately not built

No CI workflow (`.github/`) and no security-scanner dependency
(`bandit`/`safety`/`pip-audit`) were added. The spec names *behaviours* to
ensure ("no API keys in Git", "SQL injection protection", ...), not a
specific tool or a CI system, and this repo has never had either - adding
one now would be scope beyond what Phase 36 actually asks for. The
repo-wide secret scan (section 2) is written in plain Python instead,
consistent with this project's "pytest only, no extra tooling" testing
convention throughout every prior phase.

## 7. Verify

```powershell
pytest -q tests/test_phase36_final_delivery.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase36_final_delivery.py
python scripts/run_final_demo.py --help
```

## 8. Related documents

- [`security.md`](security.md) - the full security model this phase's checklist cross-references
- [`requirements.md`](requirements.md) - FR-27/FR-28, NFR-03/NFR-05, the performance budget table
- [`observability.md`](observability.md) - `stage_metrics` timing this phase's performance test reads
- [`dataset-design.md`](dataset-design.md) - `sales_2026_09_09.csv`'s origin
- [`PHASE_MAP.md`](PHASE_MAP.md) - the "Final killer demo" one-line summary
