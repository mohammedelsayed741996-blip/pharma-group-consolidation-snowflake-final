"""Tests of the synthetic data generator (Batch 1) - consistency of the clean data and the answer key."""
from __future__ import annotations

import hashlib

import pandas as pd

from generator import config as cfg
from generator.run_all import enrich, validate_clean
from tests.conftest import build_dataset


def test_group_chart_has_120_accounts(clean_data):
    ga = clean_data["master"]["group_accounts"]
    assert len(ga) == 120
    assert ga["group_account"].is_unique


def test_entities_countries_currencies(clean_data):
    ent = clean_data["master"]["entities"]
    assert len(ent) == 12
    assert ent["country_code"].nunique() == 10
    assert ent["local_currency"].nunique() == 8
    assert (ent.loc[ent["company_code"] == "PL01", "ownership_pct"] == 0.8).all()


def test_one_file_per_entity_and_period(clean_data):
    files = list((clean_data["dir"] / "raw" / "tb").glob("tb_*.csv"))
    assert len(files) == 12 * (len(cfg.PERIODS) + 1)        # + opening balance period 0


def test_clean_data_is_internally_consistent(clean_data):
    res = clean_data["generated"]
    df = enrich(res["tb"], clean_data["master"])
    v = validate_clean(df, res["ic_transactions"], clean_data["master"])
    assert v["V1_tb_balanced"]
    assert v["V2_all_accounts_mapped"]
    assert v["V3_pl_ic_max_diff_eur"] <= 1.0
    assert v["V4_bs_ic_max_diff_eur"] <= 1.0
    assert v["V5_subledger_vs_ledger_max_diff_eur"] <= 1.0


def test_generator_is_deterministic(clean_data, tmp_path):
    other = build_dataset(tmp_path / "again", with_errors=False)

    def digest(folder):
        h = hashlib.sha256()
        for p in sorted((folder / "raw" / "tb").glob("*.csv")):
            h.update(p.read_bytes())
        return h.hexdigest()

    assert digest(clean_data["dir"]) == digest(other["dir"])


def test_management_adjustments_match_one_off_items(clean_data):
    adj = pd.read_csv(clean_data["dir"] / "raw" / "adjustments" / "management_adjustments.csv", dtype=str)
    assert len(adj) == len(cfg.ONE_OFF_ITEMS)
    assert set(adj["adjustment_id"]) == {i["adjustment_id"] for i in cfg.ONE_OFF_ITEMS}


def test_answer_key(answer_key):
    assert len(answer_key) == 12
    assert (answer_key["severity"] == "BLOCKING").sum() == 7
    assert (answer_key["severity"] == "WARNING").sum() == 5
    assert set(answer_key["dq_rule_id"]) <= {r[0] for r in cfg.DQ_RULES}


def test_manifest_matches_delivered_files_except_truncated(error_data):
    d = error_data["dir"]
    ct = pd.read_csv(d / "raw" / "control" / "control_totals.csv")
    mismatched = []
    for r in ct.itertuples():
        folder = "raw/tb" if r.delivery_status == "DELIVERED" else "late_arrivals"
        n = len(pd.read_csv(d / folder / r.file_name))
        if n != r.row_count:
            mismatched.append(r.file_name)
    assert mismatched == ["tb_ES01_202508.csv"]
    assert (d / "late_arrivals" / "tb_GB01_202512.csv").exists()
