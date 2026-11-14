"""
Integration tests against a live Snowflake account (opt-in).

Prerequisites
    1. .env filled in (see .env.example)
    2. python generator/run_all.py
    3. python orchestration/run_pipeline.py all
Run
    HELIX_RUN_SNOWFLAKE_TESTS=1 pytest -m snowflake          (PowerShell: $env:HELIX_RUN_SNOWFLAKE_TESTS=1)

The tests use the files in HELIX_DATA_DIR (default ./data) - the same files that were loaded.
"""
from __future__ import annotations

import os
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest

from orchestration import run_pipeline as rp
from tests.conftest import finding_matches, period_of, unexplained_findings

pytestmark = [
    pytest.mark.snowflake,
    pytest.mark.skipif(os.environ.get("HELIX_RUN_SNOWFLAKE_TESTS") != "1",
                       reason="set HELIX_RUN_SNOWFLAKE_TESTS=1 to run against Snowflake"),
]
DATA_DIR = Path(os.environ.get("HELIX_DATA_DIR", rp.REPO_ROOT / "data"))
COUNTRY_CONTROLLER_ENTITIES = {"CZ01", "CZ02", "PL01", "HU01", "RO01"}


@pytest.fixture(scope="module")
def cur():
    conn = rp.connect()
    c = conn.cursor()
    rp.use_context(c)
    yield c
    conn.close()


def query_df(cur, sql: str) -> pd.DataFrame:
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d[0].lower() for d in cur.description])


@pytest.fixture(scope="module")
def dq(cur) -> pd.DataFrame:
    return query_df(cur, "SELECT RULE_ID, SEVERITY, COMPANY_CODE, COALESCE(PARTNER_CODE, '') AS PARTNER_CODE, "
                         "PERIOD_ID FROM AUDIT.V_DQ_LATEST")


@pytest.fixture(scope="module")
def key() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "_expected" / "injected_errors.csv", dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def late_loaded(cur) -> bool:
    cur.execute("SELECT COUNT(*) FROM STAGING.V_TB_LINES WHERE FILE_NAME = 'tb_GB01_202512.csv'")
    return cur.fetchone()[0] > 0


def test_a_successful_dq_run_exists(cur):
    cur.execute("SELECT COUNT(*) FROM AUDIT.DQ_RUNS WHERE STATUS = 'SUCCEEDED'")
    assert cur.fetchone()[0] >= 1


def test_load_log_recorded_every_trial_balance_file(cur):
    cur.execute("SELECT COUNT(DISTINCT FILE_NAME) FROM AUDIT.LOAD_LOG WHERE TARGET_TABLE = 'RAW.TB_LINES'")
    assert cur.fetchone()[0] >= 12 * 37 - 1


@pytest.mark.parametrize("error_id", [f"E{i:02d}" for i in range(1, 13)])
def test_planted_error_detected_in_snowflake(dq, key, late_loaded, error_id):
    e = key[key["error_id"] == error_id].iloc[0]
    if error_id == "E05" and late_loaded:
        pytest.skip("GB01 2025-12 was delivered late and loaded - E05 is resolved")
    hits = finding_matches(dq, e["dq_rule_id"], e["company_code"], period_of(e))
    assert not hits.empty, f"{error_id} not detected in Snowflake"
    assert (hits["severity"] == e["severity"]).all()


def test_no_unexplained_findings_in_snowflake(dq, key):
    extra = unexplained_findings(dq, key)
    assert extra.empty, extra.to_string()


def test_snowflake_reconciles_with_reference_engine():
    rp.step_verify(Namespace(data=str(DATA_DIR)))       # raises SystemExit on any mismatch


def test_consolidated_balance_sheet_balances(cur):
    cur.execute("SELECT ABS(SUM(AMOUNT_GC)) FROM MARTS.FCT_CONSOLIDATED_GL WHERE SCENARIO = 'ACT'")
    assert float(cur.fetchone()[0]) <= 1.0


def test_row_access_policy_limits_country_controller():
    conn = rp.connect(role="COUNTRY_CONTROLLER")
    try:
        c = conn.cursor()
        rp.use_context(c)
        c.execute("USE SECONDARY ROLES NONE")         # only the primary role counts
        c.execute("SELECT DISTINCT COMPANY_CODE FROM MARTS.FCT_CONSOLIDATED_GL")
        seen = {r[0] for r in c.fetchall()}
    finally:
        conn.close()
    assert seen and seen <= COUNTRY_CONTROLLER_ENTITIES


def test_task_dag_exists(cur):
    cur.execute("SHOW TASKS IN SCHEMA AUDIT")
    names = {r[1] for r in cur.fetchall()}
    assert {"T_DETECT_NEW_TB", "T_REFRESH_MARTS", "T_RUN_DQ"} <= names
