"""
Shared fixtures.

The offline tests never touch the data/ folder of the repository: every session generates
its own data sets into a temporary directory (about 5 seconds each), so the suite is
hermetic and runs in CI without a Snowflake account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from generator import config as cfg                               # noqa: E402
from generator.generate_master_data import generate_master_data   # noqa: E402
from generator.generate_tb import generate_tb                     # noqa: E402
from generator.inject_errors import inject_errors, write_control_totals  # noqa: E402
from orchestration.reference_engine import run_reference          # noqa: E402


def build_dataset(target: Path, with_errors: bool) -> dict:
    master = generate_master_data(target, cfg.SEED)
    res = generate_tb(master, target, cfg.SEED)
    if with_errors:
        inject_errors(target, cfg.SEED)
    else:
        write_control_totals(target)
    return {"dir": target, "master": master, "generated": res}


@pytest.fixture(scope="session")
def clean_data(tmp_path_factory) -> dict:
    return build_dataset(tmp_path_factory.mktemp("clean"), with_errors=False)


@pytest.fixture(scope="session")
def error_data(tmp_path_factory) -> dict:
    return build_dataset(tmp_path_factory.mktemp("errors"), with_errors=True)


@pytest.fixture(scope="session")
def clean_ref(clean_data) -> dict:
    return run_reference(clean_data["dir"])


@pytest.fixture(scope="session")
def error_ref(error_data) -> dict:
    return run_reference(error_data["dir"])


@pytest.fixture(scope="session")
def answer_key(error_data) -> pd.DataFrame:
    return pd.read_csv(error_data["dir"] / "_expected" / "injected_errors.csv", dtype=str, keep_default_na=False)


def period_of(row) -> str:
    return f"{int(row['fiscal_year'])}{int(row['fiscal_period']):02d}"


def finding_matches(dq: pd.DataFrame, rule: str, company: str, period: str) -> pd.DataFrame:
    """Findings of `rule` in `period` where the entity is the reporting company or the partner."""
    return dq[(dq["rule_id"] == rule) & (dq["period_id"] == period)
              & ((dq["company_code"] == company) | (dq["partner_code"] == company))]


def unexplained_findings(dq: pd.DataFrame, key: pd.DataFrame) -> pd.DataFrame:
    """Findings that no injected error accounts for.

    A finding is explained by an injected error when it involves the same entity (as company
    or partner), occurs in the error's period or later, and its rule is the error's rule,
    one of its listed secondary rules, or DQ08 (an unbooked / mis-booked IC item stays
    unmatched until it is corrected).
    """
    keep = []
    for _, f in dq.iterrows():
        ok = False
        for _, e in key.iterrows():
            rules = {e["dq_rule_id"], "DQ08"} | {r.split(" ")[0] for r in e["expected_secondary_rules"].split(",") if r}
            involved = e["company_code"] in (f["company_code"], f["partner_code"])
            if involved and f["period_id"] >= period_of(e) and f["rule_id"] in rules:
                ok = True
                break
        if not ok:
            keep.append(f)
    return pd.DataFrame(keep)
