"""
One command to build the complete synthetic data set for the Helix Pharma Group platform.

Steps
  1. Master data            entities, currencies, CoA, local mappings, cost/profit centres, FX
  2. Trial balances         36 monthly closes + opening balances, IC sub-ledger, budget
  3. Validation             proves the *clean* data is internally consistent before errors are added:
                            every TB balances, every account maps, every IC pair matches in EUR,
                            and the IC sub-ledger reconciles to the general ledger
  4. Error injection        12 deliberate data-quality defects + answer key + source manifest
  5. Summary metrics        printed and saved to data/_expected/summary_metrics.{json,md};
                            these are the numbers for the CV / README placeholders

Usage
  python generator/run_all.py                    # default: ./data, with errors
  python generator/run_all.py --no-errors        # clean data only
  python generator/run_all.py --update-readme    # also writes the metrics table into README.md
  python generator/run_all.py --ecb-csv path/to/eurofxref-hist.csv   # real ECB rates
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:                        # python -m generator.run_all
    from . import config as cfg
    from .generate_master_data import generate_master_data
    from .generate_tb import generate_tb
    from .inject_errors import inject_errors, write_control_totals
else:                                  # python generator/run_all.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config as cfg  # type: ignore
    from generate_master_data import generate_master_data  # type: ignore
    from generate_tb import generate_tb  # type: ignore
    from inject_errors import inject_errors, write_control_totals  # type: ignore

PL_IC_FLOWS = ["GOODS", "CONTRACT_MANUFACTURING", "MANAGEMENT_FEE", "LOAN_INTEREST", "DIVIDEND"]
TOLERANCE_EUR = 1.0     # rounding tolerance for the clean-data IC checks


# --------------------------------------------------------------------------- #
# Translation helpers (a small pandas reference implementation of the ETL)
# --------------------------------------------------------------------------- #
def enrich(tb: pd.DataFrame, master: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Map local -> group accounts and attach average / closing FX rates."""
    ent = master["entities"][["company_code", "local_chart_of_accounts"]]
    la = master["local_accounts"][["chart_of_accounts", "local_account", "group_account"]]
    df = tb.merge(ent, on="company_code", how="left")
    df = df.merge(la, left_on=["local_chart_of_accounts", "gl_account"],
                  right_on=["chart_of_accounts", "local_account"], how="left")
    df["cents"] = df["amount_lc"].astype(float).mul(100).round().astype("int64")
    df["trading_partner"] = df["trading_partner"].fillna("")
    df["period_id"] = df["fiscal_year"].astype(str) + df["fiscal_period"].astype(int).map("{:02d}".format)
    df["fx_period"] = np.where(df["fiscal_period"].astype(int) == 0, cfg.OPENING_FX_PERIOD, df["period_id"])
    fx = master["fx_rates"][["currency", "period_id", "avg_rate", "closing_rate"]].rename(
        columns={"period_id": "fx_period"})
    df = df.merge(fx, on=["currency", "fx_period"], how="left")
    ga = master["group_accounts"][["group_account", "statement", "fs_group", "fs_line", "ebitda_flag"]]
    return df.merge(ga, on="group_account", how="left")


def cumulative_balances(df: pd.DataFrame, accounts: set[str]) -> pd.DataFrame:
    """Period-end balances (opening + all movements to date) for the given BS accounts."""
    sub = df[df["group_account"].isin(accounts)]
    mv = sub.groupby(["company_code", "currency", "group_account", "trading_partner", "period_id"])["cents"].sum()
    wide = mv.unstack("period_id").reindex(columns=sorted(df["period_id"].unique())).fillna(0)
    cum = wide.cumsum(axis=1).reset_index().melt(          # melt instead of stack: same in pandas 2.x and 3.x
        id_vars=["company_code", "currency", "group_account", "trading_partner"],
        var_name="period_id", value_name="balance_cents")
    cum["fx_period"] = np.where(cum["period_id"].str.endswith("00"), cfg.OPENING_FX_PERIOD, cum["period_id"])
    return cum


# --------------------------------------------------------------------------- #
# Validation of the clean data
# --------------------------------------------------------------------------- #
def validate_clean(df: pd.DataFrame, ic_docs: pd.DataFrame, master: dict[str, pd.DataFrame]) -> dict:
    results = {}
    # V1 - every trial balance nets to zero
    tb_sum = df.groupby(["company_code", "period_id"])["cents"].sum()
    results["V1_tb_balanced"] = int((tb_sum != 0).sum()) == 0
    # V2 - every local account maps to a group account
    results["V2_all_accounts_mapped"] = bool(df["group_account"].notna().all())
    # V3 - P&L intercompany pairs match in EUR (translated at the monthly average rate)
    worst_pl = 0.0
    for pair, (a, b, basis) in cfg.IC_PAIRS.items():
        if basis != "PL":
            continue
        sa = df[(df["group_account"] == a) & (df["trading_partner"] != "")].assign(
            ent_a=lambda x: x["company_code"], ent_b=lambda x: x["trading_partner"])
        sb = df[(df["group_account"] == b) & (df["trading_partner"] != "")].assign(
            ent_a=lambda x: x["trading_partner"], ent_b=lambda x: x["company_code"])
        both = pd.concat([sa, sb])
        both["eur"] = both["cents"] / 100 / both["avg_rate"]
        diff = both.groupby(["period_id", "ent_a", "ent_b"])["eur"].sum().abs()
        worst_pl = max(worst_pl, float(diff.max()) if len(diff) else 0.0)
    results["V3_pl_ic_max_diff_eur"] = round(worst_pl, 2)
    # V4 - IC balances (receivables/payables, loans) match at the closing rate in every period
    fx = master["fx_rates"][["currency", "period_id", "closing_rate"]].rename(columns={"period_id": "fx_period"})
    worst_bs = 0.0
    for pair, (a, b, basis) in cfg.IC_PAIRS.items():
        if basis != "BS":
            continue
        cum = cumulative_balances(df, {a, b}).merge(fx, on=["currency", "fx_period"])
        cum = cum[cum["trading_partner"] != ""]
        cum["eur"] = cum["balance_cents"] / 100 / cum["closing_rate"]
        cum["ent_a"] = np.where(cum["group_account"] == a, cum["company_code"], cum["trading_partner"])
        cum["ent_b"] = np.where(cum["group_account"] == a, cum["trading_partner"], cum["company_code"])
        diff = cum.groupby(["period_id", "ent_a", "ent_b"])["eur"].sum().abs()
        worst_bs = max(worst_bs, float(diff.max()))
    results["V4_bs_ic_max_diff_eur"] = round(worst_bs, 2)
    # V5 - IC sub-ledger reconciles to the general ledger (side A, EUR)
    flow_acct = {"GOODS": "4100", "CONTRACT_MANUFACTURING": "4100", "MANAGEMENT_FEE": "4300",
                 "LOAN_INTEREST": "7010", "DIVIDEND": "7100"}
    docs = ic_docs[ic_docs["flow_type"].isin(flow_acct)].copy()
    docs["group_account"] = docs["flow_type"].map(flow_acct)
    # side A entity: seller, except dividends where the receiving holding books the income
    docs["ent_a"] = np.where(docs["flow_type"] == "DIVIDEND", docs["buyer_company"], docs["seller_company"])
    docs["ent_b"] = np.where(docs["flow_type"] == "DIVIDEND", docs["seller_company"], docs["buyer_company"])
    docs["period_id"] = docs["fiscal_year"].astype(str) + docs["fiscal_period"].map("{:02d}".format)
    sub = docs.groupby(["period_id", "ent_a", "ent_b", "group_account"])["amount_doc"].sum()
    gl = df[df["group_account"].isin(set(flow_acct.values())) & (df["trading_partner"] != "")].copy()
    gl["eur"] = -gl["cents"] / 100 / gl["avg_rate"]
    gl = gl.rename(columns={"company_code": "ent_a", "trading_partner": "ent_b"})
    gl = gl.groupby(["period_id", "ent_a", "ent_b", "group_account"])["eur"].sum()
    joined = pd.concat([sub.rename("subledger"), gl.rename("ledger")], axis=1).fillna(0)
    results["V5_subledger_vs_ledger_max_diff_eur"] = round(float((joined["subledger"] - joined["ledger"]).abs().max()), 2)
    results["passed"] = (results["V1_tb_balanced"] and results["V2_all_accounts_mapped"]
                         and results["V3_pl_ic_max_diff_eur"] <= TOLERANCE_EUR
                         and results["V4_bs_ic_max_diff_eur"] <= TOLERANCE_EUR
                         and results["V5_subledger_vs_ledger_max_diff_eur"] <= TOLERANCE_EUR)
    return results


# --------------------------------------------------------------------------- #
# Summary metrics
# --------------------------------------------------------------------------- #
def compute_metrics(df: pd.DataFrame, res: dict, master: dict, errors: pd.DataFrame | None,
                    validation: dict) -> dict:
    pl = df[df["statement"] == "PL"].copy()
    pl["eur"] = -pl["cents"] / 100 / pl["avg_rate"]                   # profit-positive presentation
    net_sales = pl[pl["fs_line"].isin(["Gross sales", "Gross-to-net deductions"])].groupby("fiscal_year")["eur"].sum()
    ebitda = pl[pl["ebitda_flag"].astype(bool)].groupby("fiscal_year")["eur"].sum()
    ic = res["ic_transactions"]
    ic_pl = ic[ic["flow_type"].isin(PL_IC_FLOWS)]
    ic_value_year = ic_pl.groupby("fiscal_year")["amount_doc"].sum()
    by_flow = ic.groupby(["flow_type", "fiscal_year"])["amount_doc"].sum().unstack().fillna(0) / 1e6
    n_err = 0 if errors is None else len(errors)
    n_block = 0 if errors is None else int((errors["severity"] == "BLOCKING").sum())
    m = {
        "entities": len(cfg.ENTITIES),
        "countries": len({e["country"] for e in cfg.ENTITIES}),
        "currencies": len(cfg.CURRENCIES),
        "monthly_closes": len(cfg.PERIODS),
        "group_accounts": len(master["group_accounts"]),
        "local_charts_of_accounts": int(master["local_accounts"]["chart_of_accounts"].nunique()),
        "local_accounts": len(master["local_accounts"]),
        "cost_centres": int(master["cost_centres"]["cost_center"].nunique()),
        "profit_centres": len(master["profit_centres"]),
        "tb_files": int(res["tb_files"]),
        "tb_rows": int(len(res["tb"])),
        "budget_rows": int(len(res["budget"])),
        "ic_documents_total": int(len(ic)),
        "ic_documents_pl_flows": int(len(ic_pl)),
        "ic_value_eur_m_per_year": {int(k): round(v / 1e6, 1) for k, v in ic_value_year.items()},
        "ic_value_eur_m_avg_per_year": round(float(ic_value_year.mean()) / 1e6, 1),
        "ic_value_eur_m_by_flow": {f: {int(y): round(v, 1) for y, v in row.items()} for f, row in by_flow.iterrows()},
        "group_net_sales_eur_m": {int(k): round(v / 1e6, 1) for k, v in net_sales.items()},
        "group_ebitda_pre_upi_eur_m": {int(k): round(v / 1e6, 1) for k, v in ebitda.items()},
        "group_ebitda_margin_pct": {int(k): round(100 * ebitda[k] / net_sales[k], 1) for k in net_sales.index},
        "injected_errors_total": n_err,
        "injected_errors_blocking": n_block,
        "injected_errors_warning": n_err - n_block,
        "validation": validation,
    }
    last = max(m["group_net_sales_eur_m"])
    m["cv_lines"] = {
        "consolidation": f"Consolidated {m['entities']} legal entities in {m['currencies']} currencies across "
                         f"{m['monthly_closes']} monthly closes",
        "eliminations": f"Eliminated {m['ic_documents_total']:,} intercompany transactions worth "
                        f"EUR {m['ic_value_eur_m_avg_per_year']:.0f}M per year",
        "data_quality": f"12 automated data-quality rules that blocked {n_block} and flagged {n_err - n_block} "
                        f"faulty submissions",
        "scale": f"Group net sales EUR {m['group_net_sales_eur_m'][last]:.0f}M ({last}), "
                 f"EBITDA margin {m['group_ebitda_margin_pct'][last]:.1f} %",
    }
    return m


def metrics_markdown(m: dict) -> str:
    years = sorted(m["group_net_sales_eur_m"])
    rows = [
        ("Legal entities / countries / currencies", f"{m['entities']} / {m['countries']} / {m['currencies']}"),
        ("Monthly closes simulated", f"{m['monthly_closes']} (+ opening balance period)"),
        ("Group chart of accounts / local accounts", f"{m['group_accounts']} / {m['local_accounts']} in {m['local_charts_of_accounts']} local charts"),
        ("Cost centres / profit centres", f"{m['cost_centres']} / {m['profit_centres']}"),
        ("Trial-balance files / rows", f"{m['tb_files']} / {m['tb_rows']:,}"),
        ("Intercompany documents (36 months)", f"{m['ic_documents_total']:,}"),
        ("Intercompany P&L value per year (avg)", f"EUR {m['ic_value_eur_m_avg_per_year']:.1f}M"),
    ]
    rows += [(f"Group net sales {y}", f"EUR {m['group_net_sales_eur_m'][y]:.1f}M") for y in years]
    rows += [(f"Group EBITDA {y} (before UPI elimination)",
              f"EUR {m['group_ebitda_pre_upi_eur_m'][y]:.1f}M ({m['group_ebitda_margin_pct'][y]:.1f} %)") for y in years]
    rows += [("Injected data-quality defects", f"{m['injected_errors_total']} "
              f"({m['injected_errors_blocking']} blocking, {m['injected_errors_warning']} warning)")]
    out = ["| Metric | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out)


def update_readme(md: str) -> bool:
    readme = cfg.REPO_ROOT / "README.md"
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(r"(<!-- GENERATOR_METRICS:START -->)(.*?)(<!-- GENERATOR_METRICS:END -->)", re.S)
    if not pattern.search(text):
        return False
    readme.write_text(pattern.sub(lambda mt: f"{mt.group(1)}\n{md}\n{mt.group(3)}", text), encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=cfg.DEFAULT_DATA_DIR, help="output data directory")
    parser.add_argument("--seed", type=int, default=cfg.SEED)
    parser.add_argument("--ecb-csv", type=Path, default=None, help="ECB eurofxref-hist.csv for real FX rates")
    parser.add_argument("--no-errors", action="store_true", help="skip data-quality error injection")
    parser.add_argument("--update-readme", action="store_true", help="write the metrics table into README.md")
    args = parser.parse_args()
    out: Path = args.out.resolve()
    t0 = time.time()

    # Start from a clean slate (only the folders this script owns)
    for sub in ("raw", "late_arrivals", "_expected", "master", "fx"):
        if (out / sub).exists():
            shutil.rmtree(out / sub)

    print("[1/5] Master data ...")
    master = generate_master_data(out, args.seed, args.ecb_csv)
    print("[2/5] Simulating 36 monthly closes, IC sub-ledger and budget ...")
    res = generate_tb(master, out, args.seed)
    print("[3/5] Validating clean data ...")
    df = enrich(res["tb"], master)
    validation = validate_clean(df, res["ic_transactions"], master)
    for k, v in validation.items():
        print(f"      {k:<40} {v}")
    if not validation["passed"]:
        raise SystemExit("Clean-data validation failed - aborting before error injection.")
    errors = None
    if args.no_errors:
        print("[4/5] Error injection skipped (--no-errors); writing source manifest ...")
        write_control_totals(out)
    else:
        print("[4/5] Injecting 12 data-quality defects ...")
        errors = inject_errors(out, args.seed)
    print("[5/5] Summary metrics ...")
    metrics = compute_metrics(df, res, master, errors, validation)
    (out / "_expected").mkdir(parents=True, exist_ok=True)
    (out / "_expected" / "summary_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    md = metrics_markdown(metrics)
    (out / "_expected" / "summary_metrics.md").write_text(md + "\n", encoding="utf-8")

    print("\n" + md + "\n")
    print("Intercompany value by flow (EUR m):")
    for flow, years in metrics["ic_value_eur_m_by_flow"].items():
        print(f"      {flow:<24} " + "  ".join(f"{y}: {v:>6.1f}" for y, v in years.items()))
    if errors is not None:
        print("\nInjected defects (answer key: data/_expected/injected_errors.csv):")
        print(errors[["error_id", "dq_rule_id", "severity", "company_code", "fiscal_year", "fiscal_period"]]
              .to_string(index=False))
    print("\nCV / README fill-ins:")
    for k, v in metrics["cv_lines"].items():
        print(f"  - {v}")
    if args.update_readme:
        print("\nREADME metrics table updated." if update_readme(md) else "\nREADME markers not found - not updated.")
    print(f"\nDone in {time.time() - t0:.1f}s -> {out}")


if __name__ == "__main__":
    main()
