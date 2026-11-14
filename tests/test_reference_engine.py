"""
Tests of the consolidation logic (pandas reference engine = the same rules as the Snowflake SQL).

Clean data:   every rule passes, the group balance sheet balances, cash flow reconciles,
              eliminations remove all intercompany revenue, goodwill / NCI / UPI are correct.
Error data:   every one of the 12 planted defects is detected by its rule at the right entity
              and period, and every finding is explained by a planted defect.
"""
from __future__ import annotations

import pandas as pd
import pytest

from generator import config as cfg
from orchestration.reference_engine import STATUTORY_LAYERS, run_reference, summary
from tests.conftest import finding_matches, period_of, unexplained_findings

IC_REVENUE_ACCOUNTS = ["4100", "4300", "7010", "7100"]


def _act(ref) -> pd.DataFrame:
    f = ref["fact"]
    return f[f["scenario"] == "ACT"]


# --------------------------------------------------------------------------- #
# Clean data
# --------------------------------------------------------------------------- #
def test_clean_data_passes_all_rules(clean_ref):
    assert clean_ref["dq_results"].empty, clean_ref["dq_results"].head().to_string()
    assert (clean_ref["close_status"]["close_status"] == "CLEAN").all()


def test_every_entity_translated_tb_nets_to_zero(clean_ref):
    t = clean_ref["translated"]
    sums = t.groupby(["company_code", "period_id"])["amount_gc"].sum()
    assert (sums == 0).all()


def test_every_layer_balances_each_period(clean_ref):
    sums = _act(clean_ref).groupby(["layer", "period_id"])["amount_gc"].sum()
    assert sums.abs().max() == 0


def test_no_cta_for_euro_entities(clean_ref):
    t = clean_ref["translated"]
    eur = [e["code"] for e in cfg.ENTITIES if e["ccy"] == "EUR"]
    assert t[(t["group_account"] == "3900") & t["company_code"].isin(eur)].empty
    assert not t[(t["group_account"] == "3900") & ~t["company_code"].isin(eur)].empty


def test_all_ic_pairs_match_on_clean_data(clean_ref):
    assert (clean_ref["ic_recon"]["match_status"] == "MATCHED").all()


def test_eliminations_remove_all_intercompany_revenue(clean_ref):
    a = _act(clean_ref)
    stat = a[a["layer"].isin(STATUTORY_LAYERS) & a["group_account"].isin(IC_REVENUE_ACCOUNTS)]
    residual = stat.groupby("group_account")["amount_gc"].sum().abs()
    assert residual.max() <= 100        # <= EUR 1 of rounding across 36 periods


def test_goodwill_and_nci(clean_ref):
    a = _act(clean_ref)
    goodwill = a[a["group_account"] == "1700"]
    assert goodwill["amount_gc"].sum() / 100 == pytest.approx(8_000_000, abs=0.01)
    assert set(goodwill["company_code"]) == {"PL01"}
    nci = a[a["group_account"] == "3950"]
    assert set(nci["company_code"]) == {"PL01"}
    assert -nci["amount_gc"].sum() > 0                  # NCI is a credit balance (equity)


def test_upi_reduces_inventory(clean_ref):
    a = _act(clean_ref)
    upi = a[(a["source_rule"] == "UPI") & (a["group_account"] == "1340")]
    assert upi["amount_gc"].sum() < 0                    # inventory reduced by unrealised profit
    assert set(upi["trading_partner"]) == {"DE02"}


def test_adjusted_ebitda_bridge_equals_one_off_items(clean_ref):
    s = summary(clean_ref)
    for year in cfg.YEARS:
        one_offs = sum(i["amount_eur"] for i in cfg.ONE_OFF_ITEMS if i["year"] == year)
        diff = s["ebitda_adjusted_eur"][year] - s["ebitda_statutory_eur"][year]
        assert diff == pytest.approx(one_offs, abs=0.01)


def test_cash_flow_reconciles_to_cash_movement(clean_ref):
    cf = clean_ref["cashflow"].groupby("period_id")["amount_gc"].sum()
    t = clean_ref["translated"]
    cash = t[t["group_account"].isin(["1000", "1010"]) & (t["period_id"] != "202300")] \
        .groupby("period_id")["amount_gc"].sum()
    diff = cf.sub(cash, fill_value=0).abs()
    assert diff.max() <= 100 * 12                        # <= EUR 1 per entity of rounding


def test_budget_is_complete(clean_ref):
    f = clean_ref["fact"]
    bud = f[(f["scenario"] == "BUD") & (f["layer"] == "TRANSLATED")]
    assert bud["period_id"].nunique() == len(cfg.PERIODS)
    assert bud["company_code"].nunique() == 12


# --------------------------------------------------------------------------- #
# Data with the 12 planted defects
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("error_id", [f"E{i:02d}" for i in range(1, 13)])
def test_each_planted_error_is_detected(error_ref, answer_key, error_id):
    e = answer_key[answer_key["error_id"] == error_id].iloc[0]
    hits = finding_matches(error_ref["dq_results"], e["dq_rule_id"], e["company_code"], period_of(e))
    assert not hits.empty, f"{error_id} ({e['dq_rule_id']} {e['company_code']} {period_of(e)}) not detected"
    assert (hits["severity"] == e["severity"]).all()


def test_no_unexplained_findings(error_ref, answer_key):
    extra = unexplained_findings(error_ref["dq_results"], answer_key)
    assert extra.empty, extra.to_string()


def test_blocked_periods_match_blocking_errors(error_ref, answer_key):
    cs = error_ref["close_status"]
    blocked = set(cs[(cs["company_code"] == "*GROUP") & (cs["close_status"] == "BLOCKED")]["period_id"])
    expected = {period_of(r) for _, r in answer_key[answer_key["severity"] == "BLOCKING"].iterrows()}
    assert blocked == expected


def test_consolidation_still_balances_with_bad_data(error_ref):
    rules = set(error_ref["dq_results"]["rule_id"])
    assert not rules & {"DQ09", "DQ10", "DQ11"}          # suspense design keeps the books whole
    t = error_ref["translated"]
    suspense = t[t["group_account"] == "9900"]
    assert set(suspense["company_code"]) == {"HU01", "RO01", "ES01"}   # E01, E02, E12


def test_late_file_clears_gb01_december(error_data):
    ref = run_reference(error_data["dir"], include_late=True)
    dq = ref["dq_results"]
    gb = dq[(dq["period_id"] == "202512") & ((dq["company_code"] == "GB01") | (dq["partner_code"] == "GB01"))]
    assert gb.empty, gb.to_string()
