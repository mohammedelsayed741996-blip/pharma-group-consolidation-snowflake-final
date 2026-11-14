"""
Pandas reference implementation of the Helix Pharma consolidation engine.

Why this exists
---------------
The production logic lives in Snowflake SQL (snowflake/20_staging ... 50_audit). This module
implements *exactly the same rules* in pandas so that:

1. the consolidation design can be tested offline, in CI, without a Snowflake account
   (tests/test_reference_engine.py), and
2. the Snowflake results can be reconciled against an independent implementation
   (run_pipeline.py verify  /  tests/test_snowflake_integration.py).

Each function is named after the Snowflake object it mirrors.

Amounts are handled as integer cents. Rounding follows Snowflake's ROUND (half away from zero).

Usage
-----
    from orchestration.reference_engine import run_reference
    res = run_reference("data")                  # dict of DataFrames
    res["dq_results"], res["fact"], res["cashflow"], res["close_status"]
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Consolidation constants (mirrored by seeds in snowflake/30_integration/01_seeds.sql)
# --------------------------------------------------------------------------- #
FIRST_YEAR, N_PERIODS = 2023, 36
OPENING_PERIOD = f"{FIRST_YEAR}00"
OPENING_FX_PERIOD = f"{FIRST_YEAR - 1}12"
IC_TOLERANCE_ABS = 1000.00          # EUR
IC_TOLERANCE_PCT = 0.005
CHECK_TOLERANCE = 1.00              # EUR, for DQ10 / DQ11 rounding
FX_JUMP_THRESHOLD = 0.10

CONSOLIDATION_ACCOUNTS = [
    # account, name, statement, account_class, fs_group, fs_line, translation, ebitda, cashflow_line
    ("9100", "Intercompany differences - P&L", "PL", "EXPENSE", "Operating expenses", "Intercompany differences", "AVG", True, "NET_INCOME"),
    ("9110", "Intercompany differences - balance sheet suspense", "BS", "ASSET", "Current assets", "Other current assets", "CLOSING", False, "OP_OTHER"),
    ("9200", "Non-recurring items (management adjustments)", "PL", "EXPENSE", "Non-recurring items", "Non-recurring items", "AVG", False, "NET_INCOME"),
    ("9300", "Profit attributable to non-controlling interests", "PL", "EXPENSE", "Profit attribution", "Non-controlling interests", "AVG", False, "NET_INCOME"),
    ("9900", "Data-quality suspense (unmapped / unbalanced)", "BS", "ASSET", "Current assets", "Suspense", "CLOSING", False, "OP_OTHER"),
]
IC_PAIRS = {  # pair_type: (side A account, side B account, basis)
    "SALES_COGS": ("4100", "5010", "PL"), "MGMT_FEE": ("4300", "6800", "PL"),
    "INTEREST": ("7010", "7210", "PL"), "DIVIDEND": ("7100", "3300", "PL"),
    "AR_AP": ("1190", "2090", "BS"), "LOAN": ("1400", "2200", "BS"),
}
ACCOUNT_TO_PAIR = {acct: (pair, side) for pair, (a, b, _) in IC_PAIRS.items() for acct, side in ((a, "A"), (b, "B"))}
LAYERS = ["TRANSLATED", "IC_ELIM", "CONSOL_ADJ", "MGMT_ADJ"]
STATUTORY_LAYERS = ["TRANSLATED", "IC_ELIM", "CONSOL_ADJ"]
STAGING_PERIOD_RULES = {"DQ01", "DQ02", "DQ05", "DQ06", "DQ07", "DQ12"}


def rnd(x) -> np.ndarray:
    """Round to integer (cents) half away from zero - same as Snowflake ROUND."""
    x = np.asarray(x, dtype=float)
    return (np.sign(x) * np.floor(np.abs(x) + 0.5)).astype("int64")


def to_long(wide: pd.DataFrame, value_name: str) -> pd.DataFrame:
    """Wide (index = keys, columns = period_seq) -> long; works identically in pandas 2.x and 3.x."""
    keys = list(wide.index.names)
    return wide.reset_index().melt(id_vars=keys, var_name="period_seq", value_name=value_name)


def period_spine() -> pd.DataFrame:
    rows = [{"period_id": OPENING_PERIOD, "fiscal_year": FIRST_YEAR, "fiscal_period": 0,
             "fx_period": OPENING_FX_PERIOD, "period_seq": 0}]
    for k in range(1, N_PERIODS + 1):
        y, m = FIRST_YEAR + (k - 1) // 12, (k - 1) % 12 + 1
        rows.append({"period_id": f"{y}{m:02d}", "fiscal_year": y, "fiscal_period": m,
                     "fx_period": f"{y}{m:02d}", "period_seq": k})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# RAW + STAGING
# --------------------------------------------------------------------------- #
def load_raw_tb(data_dir: Path, include_late: bool = False) -> pd.DataFrame:
    """RAW.TB_LINES: every delivered file, all columns as text, plus file metadata."""
    folders = [data_dir / "raw" / "tb"] + ([data_dir / "late_arrivals"] if include_late else [])
    frames = []
    for folder in folders:
        for path in sorted(folder.glob("tb_*.csv")):
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
            df["_file_name"] = path.name
            df["_file_row_number"] = np.arange(1, len(df) + 1)
            frames.append(df)
    return pd.concat(frames, ignore_index=True)


def staging_tb_typed(raw: pd.DataFrame) -> pd.DataFrame:
    """STAGING.V_TB_TYPED: typed columns, period id and SHA2 row hash (before de-duplication)."""
    df = raw.copy()
    for c in ("company_code", "gl_account", "trading_partner", "cost_center", "profit_center", "currency"):
        df[c] = df[c].fillna("").str.strip()
    df["fiscal_year"] = df["fiscal_year"].astype(int)
    df["fiscal_period"] = df["fiscal_period"].astype(int)
    df["period_id"] = df["fiscal_year"].astype(str) + df["fiscal_period"].map("{:02d}".format)
    df["amount_cents"] = rnd(df["amount_lc"].astype(float) * 100)
    key = df[["company_code", "fiscal_year", "fiscal_period", "gl_account", "trading_partner",
              "cost_center", "profit_center", "currency", "amount_lc"]].astype(str).agg("|".join, axis=1)
    df["row_hash"] = key.map(lambda s: hashlib.sha256(s.encode()).hexdigest())
    return df


def staging_tb(typed: pd.DataFrame) -> pd.DataFrame:
    """STAGING.V_TB_LINES: exact duplicates within a file removed (QUALIFY ROW_NUMBER() = 1)."""
    return typed.sort_values(["_file_name", "_file_row_number"]).drop_duplicates(["_file_name", "row_hash"])


def load_master(data_dir: Path) -> dict[str, pd.DataFrame]:
    m = {}
    for name in ("entities", "group_accounts", "local_accounts", "cost_centres", "profit_centres",
                 "ic_transfer_pricing", "dq_rules"):
        m[name] = pd.read_csv(data_dir / "master" / f"{name}.csv", dtype=str, keep_default_na=False)
    for name in ("fx_rates", "fx_budget", "fx_historical"):
        m[name] = pd.read_csv(data_dir / "fx" / f"{name}.csv", dtype=str, keep_default_na=False)
    m["budget"] = pd.concat([pd.read_csv(p, dtype=str, keep_default_na=False)
                             for p in sorted((data_dir / "raw" / "budget").glob("budget_*.csv"))])
    m["control_totals"] = pd.read_csv(data_dir / "raw" / "control" / "control_totals.csv", dtype=str)
    m["mgmt_adjustments"] = pd.read_csv(data_dir / "raw" / "adjustments" / "management_adjustments.csv",
                                        dtype=str, keep_default_na=False)
    m["entities"]["ownership_pct"] = m["entities"]["ownership_pct"].astype(float)
    return m


def accounts(master) -> pd.DataFrame:
    """STAGING.V_ACCOUNTS: 120 group accounts + consolidation accounts."""
    ga = master["group_accounts"][["group_account", "statement", "account_class", "fs_group", "fs_line",
                                   "translation_method", "ebitda_flag", "cashflow_line"]].copy()
    ga["ebitda_flag"] = ga["ebitda_flag"].str.lower().eq("true")
    extra = pd.DataFrame([{"group_account": a, "statement": s, "account_class": c, "fs_group": g, "fs_line": l,
                           "translation_method": t, "ebitda_flag": e, "cashflow_line": cf}
                          for a, _n, s, c, g, l, t, e, cf in CONSOLIDATION_ACCOUNTS])
    acc = pd.concat([ga, extra], ignore_index=True)
    acc["cashflow_line"] = acc["cashflow_line"].replace("", "OP_OTHER")
    return acc


# --------------------------------------------------------------------------- #
# INTEGRATION
# --------------------------------------------------------------------------- #
def fx_filled(master) -> pd.DataFrame:
    """INTEGRATION.V_FX_RATES: currency x FX period spine, gaps filled with the last known rate."""
    fx = master["fx_rates"].copy()
    fx["avg_rate"] = fx["avg_rate"].astype(float)
    fx["closing_rate"] = fx["closing_rate"].astype(float)
    periods = [OPENING_FX_PERIOD] + period_spine()["fx_period"].iloc[1:].tolist()
    ccys = sorted(master["entities"]["local_currency"].unique())
    spine = pd.MultiIndex.from_product([ccys, periods], names=["currency", "fx_period"]).to_frame(index=False)
    out = spine.merge(fx.rename(columns={"period_id": "fx_period"})[["currency", "fx_period", "avg_rate", "closing_rate"]],
                      on=["currency", "fx_period"], how="left")
    out["rate_status"] = np.where(out["avg_rate"].isna() | out["closing_rate"].isna(), "FALLBACK", "ACTUAL")
    out = out.sort_values(["currency", "fx_period"])
    out[["avg_rate", "closing_rate"]] = out.groupby("currency")[["avg_rate", "closing_rate"]].ffill()
    return out.reset_index(drop=True)


def tb_mapped(tb: pd.DataFrame, master, acc: pd.DataFrame) -> pd.DataFrame:
    """INTEGRATION.V_TB_MAPPED: local -> group account; unmapped and unbalanced amounts to suspense 9900."""
    ent = master["entities"][["company_code", "local_chart_of_accounts"]]
    la = master["local_accounts"][["chart_of_accounts", "local_account", "group_account"]]
    df = tb.merge(ent, on="company_code", how="left").merge(
        la, left_on=["local_chart_of_accounts", "gl_account"], right_on=["chart_of_accounts", "local_account"], how="left")
    df["mapping_status"] = np.where(df["group_account"].isna(), "UNMAPPED", "MAPPED")
    df["group_account"] = df["group_account"].fillna("9900")
    df["record_source"] = "TB"
    cols = ["company_code", "period_id", "fiscal_year", "fiscal_period", "group_account", "trading_partner",
            "cost_center", "profit_center", "currency", "amount_cents", "record_source"]
    df = df[cols]
    imb = df.groupby(["company_code", "period_id", "fiscal_year", "fiscal_period", "currency"], as_index=False)["amount_cents"].sum()
    imb = imb[imb["amount_cents"] != 0].copy()
    imb["amount_cents"] = -imb["amount_cents"]
    imb["group_account"], imb["trading_partner"], imb["cost_center"], imb["profit_center"] = "9900", "", "", ""
    imb["record_source"] = "DQ_BALANCING"
    out = pd.concat([df, imb[cols]], ignore_index=True)
    return out.merge(acc[["group_account", "statement", "translation_method"]], on="group_account", how="left")


def gl_translated(mapped: pd.DataFrame, master, fx: pd.DataFrame, acc: pd.DataFrame):
    """INTEGRATION.V_GL_TRANSLATED: IAS 21 translation to EUR + CTA, as monthly movements."""
    spine = period_spine()
    m = mapped.merge(spine[["period_id", "fx_period", "period_seq"]], on="period_id", how="left")
    m = m.merge(fx[["currency", "fx_period", "avg_rate", "closing_rate"]], on=["currency", "fx_period"], how="left")
    bud = master["fx_budget"].assign(fiscal_year=lambda d: d["fiscal_year"].astype(int),
                                     budget_rate=lambda d: d["budget_rate"].astype(float))
    m = m.merge(bud[["currency", "fiscal_year", "budget_rate"]], on=["currency", "fiscal_year"], how="left")
    # (a) P&L and transaction-dated equity (3300): movement x average rate
    is_mv = (m["statement"] == "PL") | (m["translation_method"] == "HISTORICAL_TXN")
    pl = m[is_mv].copy()
    pl["amount_gc"] = rnd(pl["amount_cents"] / pl["avg_rate"])
    pl["amount_gc_cc"] = np.where(pl["statement"] == "PL", rnd(pl["amount_cents"] / pl["budget_rate"]), np.nan)
    # (b) balance-sheet balances at closing / historical rate, movement = delta of translated balance
    bs = m[~is_mv]
    keys = ["company_code", "currency", "group_account", "trading_partner", "cost_center", "profit_center", "translation_method"]
    mv = bs.groupby(keys + ["period_seq"])["amount_cents"].sum().unstack("period_seq").reindex(
        columns=spine["period_seq"]).fillna(0).astype("int64")
    bal = mv.cumsum(axis=1)
    bal_long = to_long(bal, "bal_lc")
    mv_long = to_long(mv, "mov_lc")
    b = bal_long.merge(mv_long, on=keys + ["period_seq"]).merge(spine, on="period_seq")
    b = b.merge(fx[["currency", "fx_period", "closing_rate"]], on=["currency", "fx_period"], how="left")
    hist = master["fx_historical"].assign(historical_rate=lambda d: d["historical_rate"].astype(float))
    b = b.merge(hist[["company_code", "group_account", "historical_rate"]], on=["company_code", "group_account"], how="left")
    rate = np.where((b["translation_method"] == "HISTORICAL") & b["historical_rate"].notna(),
                    b["historical_rate"], b["closing_rate"])
    b["bal_gc"] = rnd(b["bal_lc"] / rate)
    b = b.sort_values(keys + ["period_seq"])
    b["amount_gc"] = b["bal_gc"] - b.groupby(keys)["bal_gc"].shift(1, fill_value=0)
    balances = b.copy()                                    # used by UPI / investment elimination
    b = b[(b["mov_lc"] != 0) | (b["amount_gc"] != 0)].rename(columns={"mov_lc": "amount_cents"})
    # (c) CTA = plug that keeps the translated trial balance at zero
    bs_tot = balances.groupby(["company_code", "period_seq"])["bal_gc"].sum()
    pl_mv = pl.groupby(["company_code", "period_seq"])["amount_gc"].sum().unstack().reindex(
        columns=spine["period_seq"]).fillna(0)
    pl_cum = to_long(pl_mv.cumsum(axis=1), "pl").set_index(["company_code", "period_seq"])["pl"]
    tot = bs_tot.add(pl_cum, fill_value=0).rename("tot").reset_index()
    tot = tot.sort_values(["company_code", "period_seq"])
    tot["cta_bal"] = -tot["tot"].astype("int64")
    tot["amount_gc"] = tot["cta_bal"] - tot.groupby("company_code")["cta_bal"].shift(1, fill_value=0)
    cta = tot[tot["amount_gc"] != 0].merge(spine, on="period_seq")
    cta = cta.merge(master["entities"][["company_code", "local_currency"]].rename(columns={"local_currency": "currency"}),
                    on="company_code")
    cta = cta.assign(group_account="3900", trading_partner="", cost_center="", profit_center="", amount_cents=0)
    cols = ["company_code", "period_id", "currency", "group_account", "trading_partner", "cost_center",
            "profit_center", "amount_cents", "amount_gc", "amount_gc_cc"]
    pl["source_rule"], b["source_rule"], cta["source_rule"] = "TRANSLATION", "TRANSLATION", "CTA"
    b["amount_gc_cc"], cta["amount_gc_cc"] = np.nan, np.nan
    out = pd.concat([pl[cols + ["source_rule"]], b[cols + ["source_rule"]], cta[cols + ["source_rule"]]], ignore_index=True)
    out = out.rename(columns={"amount_cents": "amount_lc"})
    out["scenario"], out["layer"] = "ACT", "TRANSLATED"
    return out, balances


def ic_lines(translated: pd.DataFrame, master) -> pd.DataFrame:
    """INTEGRATION.V_IC_LINES: translated rows on IC pair accounts with a valid partner."""
    codes = set(master["entities"]["company_code"])
    df = translated[translated["group_account"].isin(ACCOUNT_TO_PAIR)
                    & translated["trading_partner"].isin(codes)
                    & (translated["trading_partner"] != translated["company_code"])].copy()
    df["pair_type"] = df["group_account"].map(lambda a: ACCOUNT_TO_PAIR[a][0])
    df["side"] = df["group_account"].map(lambda a: ACCOUNT_TO_PAIR[a][1])
    df["entity_a"] = np.where(df["side"] == "A", df["company_code"], df["trading_partner"])
    df["entity_b"] = np.where(df["side"] == "A", df["trading_partner"], df["company_code"])
    return df


def ic_recon(lines: pd.DataFrame) -> pd.DataFrame:
    """INTEGRATION.V_IC_RECON: compare side A and side B (P&L: movements, BS: cumulative balances)."""
    spine = period_spine()
    g = lines.groupby(["pair_type", "entity_a", "entity_b", "period_id", "side"])["amount_gc"].sum().unstack("side").fillna(0)
    for s in ("A", "B"):
        if s not in g:
            g[s] = 0
    g = g.reset_index().merge(spine[["period_id", "period_seq"]], on="period_id")
    basis = g["pair_type"].map(lambda p: IC_PAIRS[p][2])
    bs = g[basis == "BS"]
    if len(bs):
        keys = ["pair_type", "entity_a", "entity_b"]
        dense = bs.set_index(keys + ["period_seq"])[["A", "B"]].unstack("period_seq").reindex(
            columns=pd.MultiIndex.from_product([["A", "B"], spine["period_seq"]])).fillna(0)
        cum_a = to_long(dense["A"].cumsum(axis=1), "A")
        cum_b = to_long(dense["B"].cumsum(axis=1), "B")
        bs = cum_a.merge(cum_b, on=keys + ["period_seq"]).merge(spine[["period_id", "period_seq"]], on="period_seq")
        bs = bs[(bs["A"] != 0) | (bs["B"] != 0)]
    out = pd.concat([g[basis == "PL"], bs], ignore_index=True)
    out = out.rename(columns={"A": "side_a_gc", "B": "side_b_gc"})
    out["difference_gc"] = out["side_a_gc"] + out["side_b_gc"]
    tol = np.maximum(IC_TOLERANCE_ABS * 100, IC_TOLERANCE_PCT * np.maximum(out["side_a_gc"].abs(), out["side_b_gc"].abs()))
    out["match_status"] = np.select(
        [(out["side_a_gc"] == 0) | (out["side_b_gc"] == 0), out["difference_gc"].abs() <= tol],
        ["ONE_SIDED", "MATCHED"], "MISMATCH")
    return out[["period_id", "pair_type", "entity_a", "entity_b", "side_a_gc", "side_b_gc", "difference_gc", "match_status"]]


def ic_elim(lines: pd.DataFrame, scenario: str) -> pd.DataFrame:
    """INTEGRATION.V_IC_ELIM: reverse both sides; the net per pair goes to 9100 / 9110."""
    rev = lines.copy()
    rev["amount_gc"] = -rev["amount_gc"]
    rev["amount_gc_cc"] = -rev["amount_gc_cc"]
    rev["amount_lc"] = np.nan
    rev["source_rule"] = "IC_" + rev["pair_type"]
    diff = lines.groupby(["period_id", "pair_type", "entity_a", "entity_b"], as_index=False)[["amount_gc", "amount_gc_cc"]].sum(min_count=1)
    diff = diff[diff["amount_gc"] != 0].copy()
    diff["group_account"] = diff["pair_type"].map(lambda p: "9110" if IC_PAIRS[p][2] == "BS" else "9100")
    diff = diff.assign(company_code=diff["entity_a"], trading_partner=diff["entity_b"], cost_center="",
                       profit_center="", amount_lc=np.nan, source_rule="IC_DIFFERENCE")
    cols = ["company_code", "period_id", "group_account", "trading_partner", "cost_center", "profit_center",
            "amount_lc", "amount_gc", "amount_gc_cc", "source_rule"]
    out = pd.concat([rev[cols], diff[cols]], ignore_index=True)
    out["scenario"], out["layer"] = scenario, "IC_ELIM"
    return out


def consol_adj(translated: pd.DataFrame, balances: pd.DataFrame, master) -> pd.DataFrame:
    """INTEGRATION.V_CONSOL_ADJ: UPI, investment/equity elimination with goodwill, NCI."""
    spine = period_spine()
    ent = master["entities"].set_index("company_code")
    rows = []

    def add(company, period, account, amount, partner="", rule=""):
        if amount != 0:
            rows.append({"company_code": company, "period_id": period, "group_account": account,
                         "trading_partner": partner, "cost_center": "", "profit_center": "",
                         "amount_gc": int(amount), "source_rule": rule})

    # Unrealised profit in inventory (downstream, DE02 -> sales companies)
    tp = master["ic_transfer_pricing"]
    margins = {r.seller_company: float(r.seller_margin_on_sales_pct) for r in tp.itertuples() if r.flow_type == "GOODS"}
    inv = balances[(balances["group_account"] == "1340") & balances["trading_partner"].isin(margins)]
    inv = inv.groupby(["company_code", "trading_partner", "period_seq"])["bal_gc"].sum().reset_index()
    for (buyer, seller), g in inv.groupby(["company_code", "trading_partner"]):
        g = g.sort_values("period_seq")
        upi_bal = -rnd(g["bal_gc"].to_numpy() * margins[seller])
        mov = np.diff(np.concatenate([[0], upi_bal]))
        for seq, mval in zip(g["period_seq"], mov):
            pid = spine.loc[spine["period_seq"] == seq, "period_id"].iloc[0]
            add(buyer, pid, "1340", mval, seller, "UPI")
            add(buyer, pid, "3200" if seq == 0 else "5090", -mval, seller, "UPI")
    # Investment / equity elimination at the opening period (acquisition-date equity)
    open_bal = balances[balances["period_seq"] == 0].groupby(["company_code", "group_account", "trading_partner"])["bal_gc"].sum()
    for sub, e in ent.iterrows():
        if e["consolidation_method"] != "FULL":
            continue
        parent, own = e["parent_company"], float(e["ownership_pct"])
        inv_v = int(open_bal.get((parent, "1800", sub), 0))
        sc = int(open_bal.get((sub, "3000", ""), 0))
        cr = int(open_bal.get((sub, "3100", ""), 0))
        eq = -(sc + cr)
        own_eq = int(rnd(own * eq))
        add(parent, OPENING_PERIOD, "1800", -inv_v, sub, "INVESTMENT")
        add(sub, OPENING_PERIOD, "3000", -sc, parent, "INVESTMENT")
        add(sub, OPENING_PERIOD, "3100", -cr, parent, "INVESTMENT")
        add(sub, OPENING_PERIOD, "1700", inv_v - own_eq, parent, "INVESTMENT")
        add(sub, OPENING_PERIOD, "3950", -(eq - own_eq), parent, "INVESTMENT")
        if own >= 1:
            continue
        nci = 1 - own
        # NCI share of post-acquisition retained earnings at migration
        re = int(open_bal.get((sub, "3200", ""), 0))
        x = int(rnd(nci * re))
        add(sub, OPENING_PERIOD, "3200", -x, "", "NCI_RE_OPENING")
        add(sub, OPENING_PERIOD, "3950", x, "", "NCI_RE_OPENING")
        t = translated[translated["company_code"] == sub]
        acc_pl = t["group_account"].str[0].isin(list("45678"))
        ni = t[acc_pl & (t["group_account"] != "3300")].groupby("period_id")["amount_gc"].sum()
        for pid, v in ni.items():
            y = int(rnd(nci * v))
            add(sub, pid, "9300", -y, "", "NCI_PROFIT")
            add(sub, pid, "3950", y, "", "NCI_PROFIT")
        cta = t[t["group_account"] == "3900"].groupby("period_id")["amount_gc"].sum()
        for pid, v in cta.items():
            z = int(rnd(nci * v))
            add(sub, pid, "3900", -z, "", "NCI_CTA")
            add(sub, pid, "3950", z, "", "NCI_CTA")
        divs = t[(t["group_account"] == "3300") & (t["trading_partner"] == "")].groupby("period_id")["amount_gc"].sum()
        for pid, v in divs.items():
            add(sub, pid, "3300", -int(v), "", "NCI_DIVIDEND")
            add(sub, pid, "3950", int(v), "", "NCI_DIVIDEND")
    out = pd.DataFrame(rows)
    out["amount_lc"], out["amount_gc_cc"] = np.nan, np.nan
    out["scenario"], out["layer"] = "ACT", "CONSOL_ADJ"
    return out


def mgmt_adj(master) -> pd.DataFrame:
    """INTEGRATION.V_MGMT_ADJ: Group Controlling reclassifies non-recurring items (EUR) to 9200.

    Only the management view includes this layer, so statutory EBITDA keeps the costs and
    adjusted EBITDA excludes them.
    """
    a = master["mgmt_adjustments"].copy()
    a["period_id"] = a["fiscal_year"] + a["fiscal_period"].astype(int).map("{:02d}".format)
    a["amount"] = rnd(a["amount_eur"].astype(float) * 100)
    base = a[["company_code", "period_id", "cost_center", "profit_center", "adjustment_id"]]
    neg = base.assign(group_account=a["group_account"], amount_gc=-a["amount"])
    pos = base.assign(group_account="9200", amount_gc=a["amount"])
    out = pd.concat([neg, pos], ignore_index=True).rename(columns={"adjustment_id": "source_rule"})
    out["trading_partner"], out["amount_lc"] = "", np.nan
    out["amount_gc_cc"] = out["amount_gc"]
    out["scenario"], out["layer"] = "ACT", "MGMT_ADJ"
    return out


def budget_translated(master) -> pd.DataFrame:
    """INTEGRATION.V_BUDGET_TRANSLATED: budget at budget rates (constant currency = group currency)."""
    b = master["budget"].copy()
    b["fiscal_year"] = b["fiscal_year"].astype(int)
    b["period_id"] = b["fiscal_year"].astype(str) + b["fiscal_period"].astype(int).map("{:02d}".format)
    b["amount_lc"] = rnd(b["amount_lc"].astype(float) * 100)
    for c in ("trading_partner", "cost_center", "profit_center"):
        b[c] = b[c].fillna("")
    br = master["fx_budget"].assign(fiscal_year=lambda d: d["fiscal_year"].astype(int),
                                    budget_rate=lambda d: d["budget_rate"].astype(float))
    b = b.merge(br, on=["currency", "fiscal_year"], how="left")
    b["amount_gc"] = rnd(b["amount_lc"] / b["budget_rate"])
    b["amount_gc_cc"] = b["amount_gc"]
    b["scenario"], b["layer"], b["source_rule"] = "BUD", "TRANSLATED", "BUDGET"
    return b[["company_code", "period_id", "currency", "group_account", "trading_partner", "cost_center",
              "profit_center", "amount_lc", "amount_gc", "amount_gc_cc", "scenario", "layer", "source_rule"]]


# --------------------------------------------------------------------------- #
# MARTS
# --------------------------------------------------------------------------- #
def cashflow(fact: pd.DataFrame, translated: pd.DataFrame, fx: pd.DataFrame, acc: pd.DataFrame) -> pd.DataFrame:
    """MARTS.FCT_CASHFLOW (indirect method, inflow = positive)."""
    spine = period_spine()
    cf_line = acc.set_index("group_account")["cashflow_line"]
    stmt = acc.set_index("group_account")["statement"]
    out = []
    # TRANSLATED layer: LC movements at the average rate, FX effect on cash separately
    t = translated[translated["period_id"] != OPENING_PERIOD].merge(spine[["period_id", "fx_period"]], on="period_id")
    t = t.merge(fx[["currency", "fx_period", "avg_rate"]], on=["currency", "fx_period"], how="left")
    t["line"] = t["group_account"].map(cf_line)
    t["stmt"] = t["group_account"].map(stmt)
    ni = t[t["stmt"] == "PL"].groupby(["company_code", "period_id"])["amount_gc"].sum().mul(-1).rename("amount_gc").reset_index()
    ni["cashflow_line"] = "NET_INCOME"
    nc = t[(t["stmt"] == "BS") & (t["line"] != "CASH") & (t["group_account"] != "3900")]
    nc = nc.groupby(["company_code", "period_id", "line", "avg_rate"])["amount_lc"].sum().reset_index()
    nc["amount_gc"] = -rnd(nc["amount_lc"] / nc["avg_rate"])
    nc = nc.groupby(["company_code", "period_id", "line"])["amount_gc"].sum().reset_index().rename(columns={"line": "cashflow_line"})
    cash = t[t["line"] == "CASH"].groupby(["company_code", "period_id", "avg_rate"])[["amount_lc", "amount_gc"]].sum().reset_index()
    cash["fx_effect"] = cash["amount_gc"] - rnd(cash["amount_lc"] / cash["avg_rate"])
    fxe = cash[["company_code", "period_id", "fx_effect"]].rename(columns={"fx_effect": "amount_gc"})
    fxe["cashflow_line"] = "FX_EFFECT_ON_CASH"
    for df in (ni, nc, fxe):
        out.append(df.assign(layer="TRANSLATED"))
    # Consolidation layers: no cash, P&L -> net income, BS -> its line (equity reclasses excluded)
    o = fact[(fact["scenario"] == "ACT") & (fact["layer"] != "TRANSLATED") & (fact["period_id"] != OPENING_PERIOD)
             & (~fact["source_rule"].str.startswith("NCI_")) & (fact["group_account"] != "3900")].copy()
    o["cashflow_line"] = np.where(o["group_account"].map(stmt) == "PL", "NET_INCOME", o["group_account"].map(cf_line))
    o = o.groupby(["layer", "company_code", "period_id", "cashflow_line"])["amount_gc"].sum().mul(-1).reset_index()
    out.append(o)
    res = pd.concat(out, ignore_index=True)
    res["scenario"] = "ACT"
    return res[res["amount_gc"] != 0]


# --------------------------------------------------------------------------- #
# AUDIT - the 12 data-quality rules
# --------------------------------------------------------------------------- #
def dq_rules(raw, typed, tb, mapped_unmapped, master, fx, translated, recon, fact, cf) -> pd.DataFrame:
    rule_meta = master["dq_rules"].set_index("rule_id")
    res = []

    def fail(rule, company, period, rows=1, metric=None, partner="", obj="", details=""):
        res.append({"rule_id": rule, "rule_name": rule_meta.loc[rule, "rule_name"],
                    "severity": rule_meta.loc[rule, "severity"], "company_code": company,
                    "partner_code": partner, "period_id": period, "object_name": obj,
                    "failing_rows": int(rows), "metric_value": metric, "details": details})

    ent = master["entities"]
    codes = set(ent["company_code"])
    spine = period_spine()
    # DQ01 trial balance balanced (after de-duplication)
    s = tb.groupby(["company_code", "period_id"])["amount_cents"].sum()
    for (c, p), v in s[s != 0].items():
        fail("DQ01", c, p, metric=v / 100, details=f"TB out of balance by {v / 100:,.2f}")
    # DQ02 every local account mapped
    um = mapped_unmapped
    for (c, p, a), g in um.groupby(["company_code", "period_id", "gl_account"]):
        fail("DQ02", c, p, rows=len(g), metric=g["amount_cents"].sum() / 100, obj=a, details=f"Unmapped local account {a}")
    # DQ03 FX complete / DQ04 FX plausible (per currency -> every entity using it)
    raw_fx = master["fx_rates"].assign(avg=lambda d: d["avg_rate"].astype(float), close=lambda d: d["closing_rate"].astype(float))
    ccy_ent = ent.groupby("local_currency")["company_code"].apply(list).to_dict()
    to_period = {OPENING_FX_PERIOD: OPENING_PERIOD}
    for r in fx[fx["rate_status"] == "FALLBACK"].itertuples():
        for c in ccy_ent.get(r.currency, []):
            fail("DQ03", c, to_period.get(r.fx_period, r.fx_period), obj=r.currency, details=f"{r.currency} rates missing for {r.fx_period}")
    raw_fx = raw_fx.sort_values(["currency", "period_id"])
    for col in ("avg", "close"):
        raw_fx[f"chg_{col}"] = raw_fx.groupby("currency")[col].pct_change().abs()
    jumps = raw_fx[(raw_fx["chg_avg"] > FX_JUMP_THRESHOLD) | (raw_fx["chg_close"] > FX_JUMP_THRESHOLD)]
    for r in jumps.itertuples():
        for c in ccy_ent.get(r.currency, []):
            fail("DQ04", c, to_period.get(r.period_id, r.period_id), metric=max(r.chg_avg, r.chg_close), obj=r.currency,
                 details=f"{r.currency} rate moved {max(r.chg_avg, r.chg_close):.1%} vs prior month")
    # DQ05 submission complete
    delivered = set(zip(tb["company_code"], tb["period_id"]))
    for c in sorted(codes):
        for p in spine["period_id"]:
            if (c, p) not in delivered:
                fail("DQ05", c, p, rows=0, details="No trial balance delivered")
    # DQ06 duplicates within a delivery
    d = typed.groupby(["company_code", "period_id", "_file_name"]).agg(n=("row_hash", "size"), u=("row_hash", "nunique"))
    for (c, p, f), r in d[d["n"] > d["u"]].iterrows():
        fail("DQ06", c, p, rows=r["n"] - r["u"], obj=f, details=f"{r['n'] - r['u']} duplicate rows")
    # DQ07 partner valid
    bad = tb[(tb["trading_partner"] != "") & (~tb["trading_partner"].isin(codes) | (tb["trading_partner"] == tb["company_code"]))]
    for (c, p, pt), g in bad.groupby(["company_code", "period_id", "trading_partner"]):
        fail("DQ07", c, p, rows=len(g), partner=pt, details=f"Invalid trading partner {pt}")
    # DQ08 IC matched
    for r in recon[recon["match_status"] != "MATCHED"].itertuples():
        fail("DQ08", r.entity_b, r.period_id, metric=r.difference_gc / 100, partner=r.entity_a, obj=r.pair_type,
             details=f"{r.pair_type} {r.match_status}: A {r.side_a_gc / 100:,.2f} / B {r.side_b_gc / 100:,.2f}")
    # DQ09 eliminations & adjustments balanced per period
    act = fact[fact["scenario"] == "ACT"]
    e = act[act["layer"] != "TRANSLATED"].groupby(["layer", "period_id"])["amount_gc"].sum()
    for (layer, p), v in e[e.abs() > CHECK_TOLERANCE * 100].items():
        fail("DQ09", "*GROUP", p, metric=v / 100, obj=layer, details=f"{layer} out of balance by {v / 100:,.2f}")
    # DQ10 consolidated balance sheet balanced (cumulative, all layers)
    tot = act.groupby("period_id")["amount_gc"].sum().reindex(spine["period_id"]).fillna(0).cumsum()
    for p, v in tot[tot.abs() > CHECK_TOLERANCE * 100].items():
        fail("DQ10", "*GROUP", p, metric=v / 100, details=f"Assets - liabilities - equity = {v / 100:,.2f}")
    # DQ11 cash flow reconciles to the balance-sheet cash movement
    cash_accts = {"1000", "1010"}
    cash_mv = translated[translated["group_account"].isin(cash_accts) & (translated["period_id"] != OPENING_PERIOD)] \
        .groupby(["company_code", "period_id"])["amount_gc"].sum()
    cf_t = cf[cf["layer"] == "TRANSLATED"].groupby(["company_code", "period_id"])["amount_gc"].sum()
    diff = cf_t.sub(cash_mv, fill_value=0)
    for (c, p), v in diff[diff.abs() > CHECK_TOLERANCE * 100].items():
        fail("DQ11", c, p, metric=v / 100, obj="TRANSLATED", details=f"Cash flow vs cash movement differs by {v / 100:,.2f}")
    other = cf[cf["layer"] != "TRANSLATED"].groupby("period_id")["amount_gc"].sum()
    for p, v in other[other.abs() > CHECK_TOLERANCE * 100].items():
        fail("DQ11", "*GROUP", p, metric=v / 100, obj="CONSOLIDATION", details=f"Consolidation layers' cash flow not zero: {v / 100:,.2f}")
    # DQ12 source-to-report: loaded rows/amounts vs manifest
    ct = master["control_totals"].copy()
    loaded = raw.groupby("_file_name").agg(rows=("amount_lc", "size"),
                                           amt=("amount_lc", lambda s: int(rnd(s.astype(float) * 100).sum())))
    for r in ct.itertuples():
        p = f"{int(r.fiscal_year)}{int(r.fiscal_period):02d}"
        exp_amt = int(rnd(float(r.sum_amount_lc) * 100))
        if r.file_name not in loaded.index:
            fail("DQ12", r.company_code, p, rows=int(r.row_count), obj=r.file_name, details="File in manifest but not loaded")
        elif loaded.loc[r.file_name, "rows"] != int(r.row_count) or loaded.loc[r.file_name, "amt"] != exp_amt:
            fail("DQ12", r.company_code, p, rows=int(r.row_count) - loaded.loc[r.file_name, "rows"], obj=r.file_name,
                 details=f"Loaded {loaded.loc[r.file_name, 'rows']} rows vs {r.row_count} in manifest")
    for f in sorted(set(loaded.index) - set(ct["file_name"])):
        fail("DQ12", f.split("_")[1], f.split("_")[2][:6], obj=f, details="Loaded file not in manifest")
    return pd.DataFrame(res, columns=["rule_id", "rule_name", "severity", "company_code", "partner_code", "period_id",
                                      "object_name", "failing_rows", "metric_value", "details"])


def close_status(dq: pd.DataFrame, master) -> pd.DataFrame:
    """AUDIT.CLOSE_STATUS: BLOCKED / WARNING / CLEAN per entity and period, plus a *GROUP row."""
    spine = period_spine()
    codes = sorted(master["entities"]["company_code"]) + ["*GROUP"]
    grid = pd.MultiIndex.from_product([spine["period_id"], codes], names=["period_id", "company_code"]).to_frame(index=False)
    agg = dq.groupby(["period_id", "company_code"]).agg(
        blocking=("rule_id", lambda s: ",".join(sorted(set(s[dq.loc[s.index, "severity"] == "BLOCKING"])))),
        warning=("rule_id", lambda s: ",".join(sorted(set(s[dq.loc[s.index, "severity"] == "WARNING"]))))).reset_index()
    out = grid.merge(agg, on=["period_id", "company_code"], how="left").fillna("")
    grp = out[out["company_code"] != "*GROUP"].groupby("period_id").agg(
        any_block=("blocking", lambda s: (s != "").any()), any_warn=("warning", lambda s: (s != "").any()))
    out = out.merge(grp, on="period_id")
    is_grp = out["company_code"] == "*GROUP"
    blocked = (out["blocking"] != "") | (is_grp & out["any_block"])
    warned = (out["warning"] != "") | (is_grp & out["any_warn"])
    out["close_status"] = np.select([blocked, warned], ["BLOCKED", "WARNING"], "CLEAN")
    return out[["period_id", "company_code", "close_status", "blocking", "warning"]].rename(
        columns={"blocking": "blocking_rules", "warning": "warning_rules"})


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run_reference(data_dir: str | Path, include_late: bool = False) -> dict[str, pd.DataFrame]:
    data_dir = Path(data_dir)
    master = load_master(data_dir)
    acc = accounts(master)
    raw = load_raw_tb(data_dir, include_late)
    typed = staging_tb_typed(raw)
    tb = staging_tb(typed)
    ent = master["entities"][["company_code", "local_chart_of_accounts"]]
    la = master["local_accounts"][["chart_of_accounts", "local_account"]]
    chk = tb.merge(ent, on="company_code").merge(la, left_on=["local_chart_of_accounts", "gl_account"],
                                                 right_on=["chart_of_accounts", "local_account"], how="left")
    unmapped = chk[chk["local_account"].isna()]
    fx = fx_filled(master)
    mapped = tb_mapped(tb, master, acc)
    translated, balances = gl_translated(mapped, master, fx, acc)
    lines = ic_lines(translated, master)
    recon = ic_recon(lines)
    elim = ic_elim(lines, "ACT")
    adj = consol_adj(translated, balances, master)
    mgmt = mgmt_adj(master)
    bud = budget_translated(master)
    bud_lines = ic_lines(bud, master)
    bud_elim = ic_elim(bud_lines, "BUD")
    fact = pd.concat([translated, elim, adj, mgmt, bud, bud_elim], ignore_index=True)
    fact["trading_partner"] = fact["trading_partner"].fillna("")
    fact["source_rule"] = fact["source_rule"].fillna("")
    cf = cashflow(fact, translated, fx, acc)
    dq = dq_rules(raw, typed, tb, unmapped, master, fx, translated, recon, fact, cf)
    cs = close_status(dq, master)
    return {"master": master, "accounts": acc, "tb": tb, "mapped": mapped, "fx": fx, "translated": translated,
            "balances": balances, "ic_recon": recon, "fact": fact, "cashflow": cf, "dq_results": dq,
            "close_status": cs}


def summary(res: dict) -> dict:
    """Headline figures (EUR) used for README metrics and Snowflake reconciliation."""
    f = res["fact"]
    acc = res["accounts"].set_index("group_account")
    act = f[f["scenario"] == "ACT"].copy()
    act["year"] = act["period_id"].str[:4].astype(int)
    act["stmt"] = act["group_account"].map(acc["statement"])
    act["ebitda"] = act["group_account"].map(acc["ebitda_flag"]).fillna(False).astype(bool)
    act["fs_line"] = act["group_account"].map(acc["fs_line"])
    stat = act[act["layer"].isin(STATUTORY_LAYERS)]
    mgmt = act
    net_sales = -stat[stat["fs_line"].isin(["Gross sales", "Gross-to-net deductions"])].groupby("year")["amount_gc"].sum() / 100
    ebitda_stat = -stat[stat["ebitda"]].groupby("year")["amount_gc"].sum() / 100
    ebitda_adj = -mgmt[mgmt["ebitda"]].groupby("year")["amount_gc"].sum() / 100
    end = act[act["period_id"] <= "202512"]
    cta = -end[end["group_account"] == "3900"]["amount_gc"].sum() / 100
    nci = -end[end["group_account"] == "3950"]["amount_gc"].sum() / 100
    goodwill = end[end["group_account"] == "1700"]["amount_gc"].sum() / 100
    upi = -end[(end["source_rule"] == "UPI") & (end["group_account"] == "1340")]["amount_gc"].sum() / 100
    elim = act[(act["layer"] == "IC_ELIM") & (act["stmt"] == "PL")]
    ic_rev = elim[elim["group_account"].isin(["4100", "4300", "7010", "7100"])].groupby("year")["amount_gc"].sum() / 100
    recon = res["ic_recon"]
    return {
        "net_sales_eur": net_sales.round(2).to_dict(),
        "ebitda_statutory_eur": ebitda_stat.round(2).to_dict(),
        "ebitda_adjusted_eur": ebitda_adj.round(2).to_dict(),
        "ic_revenue_eliminated_eur": ic_rev.round(2).to_dict(),
        "cta_reserve_end_eur": round(cta, 2),
        "nci_end_eur": round(nci, 2),
        "goodwill_eur": round(goodwill, 2),
        "upi_end_eur": round(upi, 2),
        "ic_pairs_checked": int(len(recon)),
        "ic_match_rate_pct": round(100 * float((recon["match_status"] == "MATCHED").mean()), 2),
        "dq_failures": int(len(res["dq_results"])),
        "dq_rules_failed": sorted(res["dq_results"]["rule_id"].unique().tolist()),
        "periods_blocked": int(((res["close_status"]["company_code"] == "*GROUP")
                                & (res["close_status"]["close_status"] == "BLOCKED")).sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--include-late", action="store_true", help="also load data/late_arrivals")
    args = parser.parse_args()
    res = run_reference(args.data, args.include_late)
    import json
    print(json.dumps(summary(res), indent=2, default=str))
    dq = res["dq_results"]
    print(dq.groupby(["rule_id", "severity"]).size().rename("failures").to_string())


if __name__ == "__main__":
    main()
