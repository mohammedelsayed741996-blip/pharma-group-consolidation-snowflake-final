"""
Generate the master (reference) data for the Helix Pharma Group platform.

Outputs (CSV, written below <data_dir>):
    master/entities.csv             12 legal entities (SAP company codes)
    master/currencies.csv           8 currencies
    master/business_units.csv       management segments (Rx, OTC, MedDev, Herbal, Corporate)
    master/group_accounts.csv       120-account group chart of accounts with reporting attributes
    master/local_accounts.csv       10 local charts of accounts mapped to the group chart
    master/cost_centres.csv         SAP CO-style cost centres, SCD Type 2 (one reorganisation)
    master/profit_centres.csv       profit centres per entity x business unit
    master/ic_transfer_pricing.csv  intercompany pricing rules (markups, fee rate, loan rate)
    master/dq_rules.csv             catalogue of the 12 automated data-quality rules
    fx/fx_rates.csv                 monthly average & closing rates (LC per EUR)
    fx/fx_budget.csv                budget rate per currency and fiscal year
    fx/fx_historical.csv            historical rates for equity accounts per entity

Run standalone:  python generator/generate_master_data.py --out data
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:                        # imported as part of the generator package
    from . import config as cfg
else:                                  # executed as a script: python generator/<file>.py
    import config as cfg               # type: ignore

FIRST_NAMES = ["Anna", "Lukas", "Maria", "Jan", "Sofia", "Pavel", "Elena", "Marco", "Katarzyna",
               "Tomas", "Laura", "David", "Eva", "Javier", "Ingrid", "Peter", "Ioana", "Zsofia",
               "Oliver", "Chiara", "Martin", "Agnieszka", "Lena", "Andrei"]
LAST_NAMES = ["Schmidt", "Novak", "Garcia", "Rossi", "Kowalski", "Horvath", "Popescu", "Smith",
              "Meier", "Andersson", "Fischer", "Dvorak", "Lopez", "Bianchi", "Nowak", "Nagy",
              "Ionescu", "Brown", "Keller", "Lindqvist"]


# --------------------------------------------------------------------------- #
# Entities, currencies, business units
# --------------------------------------------------------------------------- #
def build_entities() -> pd.DataFrame:
    rows = []
    for e in cfg.ENTITIES:
        rows.append({
            "company_code": e["code"],
            "company_name": e["name"],
            "country_code": e["country"],
            "country_name": e["country_name"],
            "city": e["city"],
            "region": e["region"],
            "local_currency": e["ccy"],
            "entity_type": e["etype"],
            "parent_company": e["parent"],
            "ownership_pct": e["own"],
            "consolidation_method": "PARENT" if not e["parent"] else "FULL",
            "acquisition_date": e["acq"],
            "goodwill_eur": e["goodwill_eur"],
            "local_chart_of_accounts": e["coa"],
            "source_system": e["erp"],
            "statutory_tax_rate": e["tax"],
            "is_active": True,
        })
    return pd.DataFrame(rows)


def build_currencies() -> pd.DataFrame:
    return pd.DataFrame(
        [{"currency_code": c, "currency_name": n, "decimals": d, "is_group_currency": c == cfg.GROUP_CURRENCY}
         for c, n, _s, _dr, _v, d in cfg.CURRENCIES])


def build_business_units() -> pd.DataFrame:
    return pd.DataFrame([{"business_unit": c, "business_unit_name": n, "segment": s}
                         for c, n, s in cfg.BUSINESS_UNITS])


# --------------------------------------------------------------------------- #
# Group chart of accounts
# --------------------------------------------------------------------------- #
def build_group_accounts() -> pd.DataFrame:
    rows = []
    for (acct, name, fs_group, fs_line, tm, cf_line, ic_pair, ic_side, cons_only) in cfg.GROUP_ACCOUNTS:
        bs = cfg.is_balance_sheet(acct)
        cls = cfg.account_class(acct)
        rows.append({
            "group_account": acct,
            "account_name": name,
            "statement": "BS" if bs else "PL",
            "account_class": cls,
            "fs_group": fs_group,
            "fs_line": fs_line,
            # assets shown positive; liabilities, equity and the whole P&L are flipped so that
            # revenue and profit display as positive numbers and costs as negative numbers
            "presentation_sign": 1 if cls == "ASSET" else -1,
            "translation_method": tm,
            "ebitda_flag": (not bs) and fs_group not in ("Depreciation & amortisation",
                                                         "Financial result", "Income tax"),
            "cashflow_line": cf_line if bs else "NET_INCOME",
            "is_intercompany": ic_pair != "",
            "ic_pair_type": ic_pair,
            "ic_side": ic_side,
            "partner_required": ic_pair in ("SALES_COGS", "MGMT_FEE", "INTEREST", "DIVIDEND",
                                            "AR_AP", "LOAN", "INVESTMENT", "UPI_BASE"),
            "consolidation_only": cons_only,
            "sort_order": int(acct),
        })
    df = pd.DataFrame(rows)
    assert len(df) == 120, f"Group chart must contain exactly 120 accounts, found {len(df)}"
    assert df["group_account"].is_unique
    return df


def build_local_accounts(rng: np.random.Generator, group_accounts: pd.DataFrame) -> pd.DataFrame:
    """Create one local chart of accounts per country and map it to the group chart.

    * Consolidation-only and system accounts are never posted locally, so they get no mapping.
    * ~15 % of eligible accounts (non-IC P&L accounts plus bank and trade payables) are split
      into two local accounts -> a many-to-one mapping the ETL must aggregate correctly.
    """
    postable = group_accounts[(~group_accounts["consolidation_only"]) &
                              (group_accounts["translation_method"] != "CARRYFORWARD")]
    eligible = set(postable.loc[(postable["statement"] == "PL") & (~postable["is_intercompany"]),
                                "group_account"]) | {"1000", "2000"}
    rows = []
    for chart, spec in cfg.LOCAL_CHARTS.items():
        split_accounts = {a for a in sorted(eligible) if rng.random() < cfg.LOCAL_SPLIT_SHARE}
        split_accounts |= {"1000", "6000"}                 # guarantee at least two splits per chart
        counters: dict[str, int] = {}
        for _, ga in postable.iterrows():
            acct = ga["group_account"]
            prefix = spec["prefix"][acct[0]]
            n_local = 2 if acct in split_accounts else 1
            suffixes = (["(core)", "(other)"] if acct not in cfg.PERSONNEL_ACCOUNTS
                        else ["(employees)", "(management)"]) if n_local == 2 else [""]
            for suffix in suffixes:
                counters[prefix] = counters.get(prefix, 0) + 10
                local = prefix + str(counters[prefix]).zfill(spec["width"] - 1)
                rows.append({
                    "chart_of_accounts": chart,
                    "local_account": local,
                    "local_account_name": f"{ga['account_name']} {suffix}".strip(),
                    "group_account": acct,
                    "valid_from": "2000-01-01",
                    "valid_to": "9999-12-31",
                })
    df = pd.DataFrame(rows)
    assert not df.duplicated(["chart_of_accounts", "local_account"]).any()
    return df


# --------------------------------------------------------------------------- #
# Cost centres (SCD2) and profit centres
# --------------------------------------------------------------------------- #
def build_cost_centres(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    reorg_end = (pd.Timestamp(cfg.REORG_DATE) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    for e in cfg.ENTITIES:
        for dept, (dname, func, types) in cfg.DEPARTMENTS.items():
            if e["etype"] not in types:
                continue
            base = {
                "cost_center": f"CC-{e['code']}-{dept}",
                "company_code": e["code"],
                "department": dept,
                "cost_center_name": f"{dname} {e['country']}",
                "responsible_person": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "default_profit_center": f"PC-{e['code']}-CORP",
            }
            if dept == "MEDAFF":     # SCD Type 2: function changes on the reorganisation date
                rows.append({**base, "function": "R&D", "valid_from": "2000-01-01",
                             "valid_to": reorg_end, "is_current": False, "scd_version": 1})
                rows.append({**base, "function": "S&M", "valid_from": cfg.REORG_DATE,
                             "valid_to": "9999-12-31", "is_current": True, "scd_version": 2})
            else:
                rows.append({**base, "function": func, "valid_from": "2000-01-01",
                             "valid_to": "9999-12-31", "is_current": True, "scd_version": 1})
    return pd.DataFrame(rows)


def build_profit_centres() -> pd.DataFrame:
    bu_lookup = {c: (n, s) for c, n, s in cfg.BUSINESS_UNITS}
    rows = []
    for e in cfg.ENTITIES:
        for bu in list(e["bu_mix"].keys()) + ["CORP"]:
            name, segment = bu_lookup[bu]
            rows.append({"profit_center": f"PC-{e['code']}-{bu}", "company_code": e["code"],
                         "business_unit": bu, "segment": segment,
                         "profit_center_name": f"{name} - {e['country_name']}"})
    return pd.DataFrame(rows)


def build_ic_transfer_pricing() -> pd.DataFrame:
    return pd.DataFrame([
        {"seller_company": "DE02", "flow_type": "GOODS", "pricing_method": "COST_PLUS",
         "markup_pct": cfg.DE02_MARKUP,
         "seller_margin_on_sales_pct": round(cfg.DE02_MARKUP / (1 + cfg.DE02_MARKUP), 6),
         "settlement_currency": "EUR", "payment_terms_days": 60},
        {"seller_company": "CZ02", "flow_type": "CONTRACT_MANUFACTURING", "pricing_method": "COST_PLUS",
         "markup_pct": cfg.CZ02_MARKUP,
         "seller_margin_on_sales_pct": round(cfg.CZ02_MARKUP / (1 + cfg.CZ02_MARKUP), 6),
         "settlement_currency": "EUR", "payment_terms_days": 30},
        {"seller_company": "DE01", "flow_type": "MANAGEMENT_FEE", "pricing_method": "PCT_OF_NET_SALES",
         "markup_pct": cfg.MGMT_FEE_RATE, "seller_margin_on_sales_pct": 1.0,
         "settlement_currency": "EUR", "payment_terms_days": 30},
        {"seller_company": "DE01", "flow_type": "LOAN", "pricing_method": "FIXED_INTEREST",
         "markup_pct": cfg.IC_LOAN_RATE, "seller_margin_on_sales_pct": 1.0,
         "settlement_currency": "EUR", "payment_terms_days": 0},
    ])


# --------------------------------------------------------------------------- #
# FX rates
# --------------------------------------------------------------------------- #
def _simulate_daily_rates(rng: np.random.Generator) -> pd.DataFrame:
    """Geometric random walk per currency on business days, starting 1 Dec 2022."""
    days = pd.bdate_range(f"{cfg.FIRST_YEAR - 1}-12-01", f"{cfg.LAST_YEAR}-12-31")
    out = {"date": days}
    for code, _n, start, drift, vol, _d in cfg.CURRENCIES:
        if code == cfg.GROUP_CURRENCY:
            out[code] = np.ones(len(days))
            continue
        shocks = rng.normal(drift / 252, vol / np.sqrt(252), size=len(days))
        shocks[0] = 0.0
        out[code] = start * np.exp(np.cumsum(shocks))
    return pd.DataFrame(out)


def _read_ecb_daily(ecb_csv: Path) -> pd.DataFrame:
    """Read the ECB 'eurofxref-hist.csv' file (Date + one column per currency)."""
    raw = pd.read_csv(ecb_csv)
    raw = raw.rename(columns={"Date": "date"})
    raw["date"] = pd.to_datetime(raw["date"])
    keep = ["date"] + [c for c, *_ in cfg.CURRENCIES if c != cfg.GROUP_CURRENCY]
    missing = [c for c in keep if c not in raw.columns]
    if missing:
        raise ValueError(f"ECB file is missing columns: {missing}")
    df = raw[keep].copy()
    for c in keep[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df[cfg.GROUP_CURRENCY] = 1.0
    lo, hi = pd.Timestamp(f"{cfg.FIRST_YEAR - 1}-12-01"), pd.Timestamp(f"{cfg.LAST_YEAR}-12-31")
    return df[(df["date"] >= lo) & (df["date"] <= hi)].sort_values("date").dropna()


def build_fx_rates(rng: np.random.Generator, ecb_csv: Path | None = None) -> pd.DataFrame:
    daily = _read_ecb_daily(ecb_csv) if ecb_csv else _simulate_daily_rates(rng)
    source = "ECB_REFERENCE" if ecb_csv else "SIMULATED"
    daily["period_id"] = daily["date"].dt.strftime("%Y%m")
    rows = []
    for code, *_ in cfg.CURRENCIES:
        g = daily[["date", "period_id", code]].sort_values("date").groupby("period_id")[code]
        avg, close = g.mean(), g.last()
        for pid in avg.index:
            rows.append({"currency": code, "period_id": pid,
                         "avg_rate": round(float(avg[pid]), 6),
                         "closing_rate": round(float(close[pid]), 6),
                         "rate_source": "GROUP_CURRENCY" if code == cfg.GROUP_CURRENCY else source})
    return pd.DataFrame(rows).sort_values(["currency", "period_id"]).reset_index(drop=True)


def build_fx_budget(fx: pd.DataFrame) -> pd.DataFrame:
    """Budget rate for year Y = closing rate of December Y-1, rounded as a planner would."""
    rows = []
    for code, *_ in cfg.CURRENCIES:
        for y in cfg.YEARS:
            dec = fx[(fx["currency"] == code) & (fx["period_id"] == f"{y - 1}12")]
            rate = float(dec["closing_rate"].iloc[0]) if len(dec) else 1.0
            digits = 0 if rate > 100 else (2 if rate > 10 else 3)
            rows.append({"currency": code, "fiscal_year": y, "budget_rate": round(rate, digits)})
    return pd.DataFrame(rows)


def build_fx_historical(fx: pd.DataFrame) -> pd.DataFrame:
    """Historical rates per entity for equity accounts (IAS 21.39 / 21.48 logic).

    3000/3100 use the rate at founding/acquisition. 3200 (retained earnings brought forward at
    data migration) uses a blended rate halfway between the historical and the opening rate,
    representing the profits accumulated between acquisition and 31 Dec 2022.
    """
    rows = []
    opening = fx[fx["period_id"] == cfg.OPENING_FX_PERIOD].set_index("currency")["closing_rate"]
    for e in cfg.ENTITIES:
        hist = e["hist_rate"]
        blended = 1.0 if e["ccy"] == cfg.GROUP_CURRENCY else round((hist + float(opening[e["ccy"]])) / 2, 6)
        for acct, rate, basis in (("3000", hist, f"rate at acquisition/founding {e['acq']}"),
                                  ("3100", hist, f"rate at acquisition/founding {e['acq']}"),
                                  ("3200", blended, "blended rate of post-acquisition profits to 2022-12-31")):
            rows.append({"company_code": e["code"], "group_account": acct, "currency": e["ccy"],
                         "historical_rate": rate, "rate_basis": basis})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def generate_master_data(data_dir: Path, seed: int = cfg.SEED,
                         ecb_csv: Path | None = None) -> dict[str, pd.DataFrame]:
    """Build every reference table, write it to CSV and return the DataFrames."""
    rng = np.random.default_rng(seed)
    group_accounts = build_group_accounts()
    fx = build_fx_rates(rng, ecb_csv)
    tables = {
        "entities": build_entities(),
        "currencies": build_currencies(),
        "business_units": build_business_units(),
        "group_accounts": group_accounts,
        "local_accounts": build_local_accounts(rng, group_accounts),
        "cost_centres": build_cost_centres(rng),
        "profit_centres": build_profit_centres(),
        "ic_transfer_pricing": build_ic_transfer_pricing(),
        "fx_rates": fx,
        "fx_budget": build_fx_budget(fx),
        "fx_historical": build_fx_historical(fx),
        "dq_rules": pd.DataFrame(cfg.DQ_RULES, columns=["rule_id", "rule_name", "severity", "layer", "description"]),
    }
    (data_dir / "master").mkdir(parents=True, exist_ok=True)
    (data_dir / "fx").mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        folder = "fx" if name.startswith("fx_") else "master"
        df.to_csv(data_dir / folder / f"{name}.csv", index=False)
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=cfg.DEFAULT_DATA_DIR, help="output data directory")
    parser.add_argument("--seed", type=int, default=cfg.SEED)
    parser.add_argument("--ecb-csv", type=Path, default=None,
                        help="optional path to ECB eurofxref-hist.csv to use real rates")
    args = parser.parse_args()
    tables = generate_master_data(args.out, args.seed, args.ecb_csv)
    for name, df in tables.items():
        print(f"{name:<22} {len(df):>6} rows")


if __name__ == "__main__":
    main()
