"""
Inject realistic data-quality defects into the raw source files.

A consolidation platform is only credible if it can prove that it *catches* bad data.
This module edits the clean files produced by generate_tb.py and writes an answer key
(data/_expected/injected_errors.csv) that the pytest suite in Batch 2 uses to assert
that every defect is detected by the matching Snowflake data-quality rule.

All edits except E01 and E12 keep each trial balance balanced, so the defect is caught only
by the rule it targets (secondary effects are listed in the answer key).

| id  | rule | severity | defect                                                           |
|-----|------|----------|------------------------------------------------------------------|
| E01 | DQ01 | BLOCKING | HU01 2024-03: amount typo (+12,345.67) -> TB does not balance     |
| E02 | DQ02 | BLOCKING | RO01 2025-02: 40 % of consulting booked on a new, unmapped account|
| E03 | DQ03 | BLOCKING | HUF rates for 2025-11 missing from the FX feed                    |
| E04 | DQ04 | WARNING  | SEK closing rate 2025-06 keyed 18 % too high                      |
| E05 | DQ05 | BLOCKING | GB01 2025-12 file not delivered (moved to data/late_arrivals/)    |
| E06 | DQ06 | WARNING  | CZ01 2024-09: 25 rows delivered twice                             |
| E07 | DQ07 | BLOCKING | IT01 2024-11: payable to DE02 booked with partner code DE03       |
| E08 | DQ07 | BLOCKING | ES01 2025-04: IC purchases booked with own company as partner     |
| E09 | DQ08 | WARNING  | PL01: Dec-2024 IC purchases from DE02 booked in Jan-2025 (cut-off)|
| E10 | DQ08 | WARNING  | HU01 2025-03: management fee converted at a wrong rate (+3 %)     |
| E11 | DQ08 | WARNING  | SE01 2025-06: management fee not booked by the buyer (one-sided)  |
| E12 | DQ12 | BLOCKING | ES01 2025-08: file truncated in transfer (3 rows lost)            |

Run standalone (after generate_tb.py):  python generator/inject_errors.py --out data
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:                        # imported as part of the generator package
    from . import config as cfg
else:                                  # executed as a script: python generator/<file>.py
    import config as cfg               # type: ignore

TB_DTYPES = {c: str for c in ("company_code", "gl_account", "trading_partner", "cost_center",
                              "profit_center", "currency", "amount_lc", "source_system",
                              "extract_timestamp")}


# --------------------------------------------------------------------------- #
# File helpers (amounts are edited as integer cents to avoid float drift)
# --------------------------------------------------------------------------- #
def tb_path(data_dir: Path, company: str, year: int, period: int, folder: str = "raw/tb") -> Path:
    return data_dir / folder / f"tb_{company}_{year}{period:02d}.csv"


def read_tb(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=TB_DTYPES, keep_default_na=False)
    df["cents"] = df["amount_lc"].map(lambda v: int(round(float(v) * 100)))
    return df


def write_tb(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    out["amount_lc"] = (out["cents"] / 100).map(lambda v: f"{v:.2f}")
    out.drop(columns=["cents"]).to_csv(path, index=False)


def group_lookup(data_dir: Path) -> dict[str, dict[str, str]]:
    """company_code -> {local_account: group_account}."""
    la = pd.read_csv(data_dir / "master" / "local_accounts.csv", dtype=str)
    out = {}
    for e in cfg.ENTITIES:
        sub = la[la["chart_of_accounts"] == e["coa"]]
        out[e["code"]] = dict(zip(sub["local_account"], sub["group_account"]))
    return out


def local_for(data_dir: Path, company: str, group_account: str) -> list[str]:
    la = pd.read_csv(data_dir / "master" / "local_accounts.csv", dtype=str)
    coa = cfg.ENTITY_BY_CODE[company]["coa"]
    return sorted(la.loc[(la["chart_of_accounts"] == coa) & (la["group_account"] == group_account),
                         "local_account"].tolist())


def _rows_for(df: pd.DataFrame, gl_map: dict[str, str], group_account: str, partner: str | None = None):
    mask = df["gl_account"].map(gl_map).eq(group_account)
    if partner is not None:
        mask &= df["trading_partner"].eq(partner)
    return df.index[mask]


def _entry(error_id, rule_id, company, year, period, obj, description, secondary=""):
    _rid, rname, severity, _layer, _desc = cfg.DQ_RULE_INDEX[rule_id]
    return {"error_id": error_id, "dq_rule_id": rule_id, "dq_rule_name": rname, "severity": severity,
            "company_code": company, "fiscal_year": year, "fiscal_period": period, "object": obj,
            "description": description, "expected_secondary_rules": secondary}


# --------------------------------------------------------------------------- #
# Individual defects
# --------------------------------------------------------------------------- #
def e01_unbalanced(data_dir, gl):
    c, y, p = "HU01", 2024, 3
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    opex = df.index[df["gl_account"].map(gl[c]).str.startswith("6") & (df["cents"] > 0)]
    idx = df.loc[opex, "cents"].idxmax()
    before = df.at[idx, "cents"]
    df.at[idx, "cents"] = before + 1_234_567
    write_tb(df, path)
    return _entry("E01", "DQ01", c, y, p, path.name,
                  f"Amount on local account {df.at[idx, 'gl_account']} keyed as {(before + 1_234_567) / 100:.2f} "
                  f"instead of {before / 100:.2f}; trial balance out by 12,345.67 HUF (difference parked on suspense 9900)")


def e02_unmapped(data_dir, gl):
    c, y, p = "RO01", 2025, 2
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    idx = df.loc[_rows_for(df, gl[c], "6380"), "cents"].idxmax()
    moved = int(round(df.at[idx, "cents"] * 0.40))
    new_account = "659990"                        # created locally, never added to the mapping table
    assert new_account not in gl[c]
    new_row = df.loc[idx].copy()
    new_row["gl_account"] = new_account
    new_row["cents"] = moved
    df.at[idx, "cents"] -= moved
    df = pd.concat([df, new_row.to_frame().T], ignore_index=True)
    df["cents"] = df["cents"].astype("int64")
    write_tb(df, path)
    return _entry("E02", "DQ02", c, y, p, path.name,
                  f"New local account {new_account} ('IT consulting services') used for {moved / 100:.2f} RON "
                  f"but not mapped to the group chart of accounts")


def e03_missing_fx(data_dir):
    path = data_dir / "fx" / "fx_rates.csv"
    fx = pd.read_csv(path, dtype={"period_id": str})
    fx = fx[~((fx["currency"] == "HUF") & (fx["period_id"] == "202511"))]
    fx.to_csv(path, index=False)
    return _entry("E03", "DQ03", "HU01", 2025, 11, "fx_rates.csv",
                  "Average and closing rate for HUF 2025-11 missing from the treasury FX feed; the platform falls back "
                  "to the prior month's rate, so HU01's IC pairs mismatch in 2025-11", "DQ08")


def e04_fx_outlier(data_dir):
    path = data_dir / "fx" / "fx_rates.csv"
    fx = pd.read_csv(path, dtype={"period_id": str})
    mask = (fx["currency"] == "SEK") & (fx["period_id"] == "202506")
    before = float(fx.loc[mask, "closing_rate"].iloc[0])
    fx.loc[mask, "closing_rate"] = round(before * 1.18, 6)
    fx.to_csv(path, index=False)
    return _entry("E04", "DQ04", "SE01", 2025, 6, "fx_rates.csv",
                  f"SEK closing rate 2025-06 keyed as {before * 1.18:.4f} instead of {before:.4f} (+18 %); "
                  "2025-07 is flagged too because the rate snaps back; SE01's IC balances mismatch in 2025-06", "DQ04,DQ08")


def e05_late_file(data_dir):
    c, y, p = "GB01", 2025, 12
    src = tb_path(data_dir, c, y, p)
    dst_dir = data_dir / "late_arrivals"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst_dir / src.name))
    return _entry("E05", "DQ05", c, y, p, src.name,
                  "December 2025 trial balance of GB01 not delivered by the close deadline; it arrives later in "
                  "data/late_arrivals/ (use it to demo incremental loading with streams)", "DQ08,DQ12")


def e06_duplicates(data_dir, rng):
    c, y, p = "CZ01", 2024, 9
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    dup = df.iloc[sorted(rng.choice(len(df), size=25, replace=False))]
    write_tb(pd.concat([df, dup], ignore_index=True), path)
    return _entry("E06", "DQ06", c, y, p, path.name,
                  "25 rows delivered twice by the interface (exact duplicates); removed in staging, so the TB still balances")


def e07_unknown_partner(data_dir, gl):
    c, y, p = "IT01", 2024, 11
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    idx = _rows_for(df, gl[c], "2090", "DE02")
    df.loc[idx, "trading_partner"] = "DE03"
    write_tb(df, path)
    return _entry("E07", "DQ07", c, y, p, path.name,
                  "Payable to DE02 booked with non-existent trading partner DE03; the IT01/DE02 payable stays unmatched "
                  "until corrected", "DQ08")


def e08_self_partner(data_dir, gl):
    c, y, p = "ES01", 2025, 4
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    idx = _rows_for(df, gl[c], "5010", "DE02")
    df.loc[idx, "trading_partner"] = c
    write_tb(df, path)
    return _entry("E08", "DQ07", c, y, p, path.name,
                  "Intercompany purchases from DE02 booked with the entity's own code ES01 as trading partner", "DQ08")


def _add_to_row(df, mask_idx, delta):
    """Add delta cents to the first matching row."""
    df.at[mask_idx[0], "cents"] += delta
    return df


def e09_cutoff(data_dir, gl):
    c = "PL01"
    dec_path, jan_path = tb_path(data_dir, c, 2024, 12), tb_path(data_dir, c, 2025, 1)
    dec, jan = read_tb(dec_path), read_tb(jan_path)
    purch_idx = _rows_for(dec, gl[c], "5010", "DE02")
    moved = dec.loc[purch_idx].copy()
    total = int(moved["cents"].sum())
    dec = dec.drop(index=purch_idx)
    dec = _add_to_row(dec, _rows_for(dec, gl[c], "2090", "DE02"), total)          # payable no longer booked
    keys = ["gl_account", "trading_partner", "cost_center", "profit_center"]
    for _, r in moved.iterrows():                                                   # book in January instead
        hit = jan.index[(jan[keys] == r[keys]).all(axis=1)]
        if len(hit):
            jan.at[hit[0], "cents"] += r["cents"]
        else:
            r = r.copy()
            r["fiscal_year"], r["fiscal_period"] = 2025, 1
            r["extract_timestamp"] = jan["extract_timestamp"].iloc[0]
            jan = pd.concat([jan, r.to_frame().T], ignore_index=True)
    jan["cents"] = jan["cents"].astype("int64")
    jan = _add_to_row(jan, _rows_for(jan, gl[c], "2090", "DE02"), -total)
    write_tb(dec, dec_path)
    write_tb(jan, jan_path)
    return _entry("E09", "DQ08", c, 2024, 12, f"{dec_path.name}, {jan_path.name}",
                  f"Cut-off error: December IC purchases from DE02 ({total / 100:,.2f} PLN) booked in January 2025; "
                  "IC difference appears in both 2024-12 and 2025-01")


def e10_wrong_rate(data_dir, gl):
    c, y, p = "HU01", 2025, 3
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    fee_idx = _rows_for(df, gl[c], "6800", "DE01")
    delta = int(round(df.loc[fee_idx, "cents"].sum() * 0.03))
    df = _add_to_row(df, fee_idx, delta)
    df = _add_to_row(df, _rows_for(df, gl[c], "2090", "DE01"), -delta)
    write_tb(df, path)
    return _entry("E10", "DQ08", c, y, p, path.name,
                  f"Management fee invoice converted at a wrong HUF rate (+3 %, {delta / 100:,.2f} HUF); the payable "
                  "difference persists in later periods until corrected", "DQ08")


def e11_one_sided(data_dir, gl):
    c, y, p = "SE01", 2025, 6
    path = tb_path(data_dir, c, y, p)
    df = read_tb(path)
    fee_idx = _rows_for(df, gl[c], "6800", "DE01")
    fee = int(df.loc[fee_idx, "cents"].sum())
    df = df.drop(index=fee_idx)
    df = _add_to_row(df, _rows_for(df, gl[c], "2090", "DE01"), fee)
    write_tb(df, path)
    return _entry("E11", "DQ08", c, y, p, path.name,
                  f"Management fee invoice from DE01 ({fee / 100:,.2f} SEK) not booked by SE01; only the seller side exists; "
                  "the payable difference persists until corrected", "DQ08")


def e12_truncated(data_dir):
    c, y, p = "ES01", 2025, 8
    path = tb_path(data_dir, c, y, p)
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-3]))
    return _entry("E12", "DQ12", c, y, p, path.name,
                  "File truncated during SFTP transfer: last 3 rows lost after the source system computed control totals",
                  "DQ01")


# --------------------------------------------------------------------------- #
# Control totals (source manifest)
# --------------------------------------------------------------------------- #
def write_control_totals(data_dir: Path) -> pd.DataFrame:
    """Source-system manifest: row count and amount totals for every extracted file.

    Late files are included because the source system extracted them; they were only
    delivered late. Snowflake compares loaded data against this manifest (rule DQ12).
    """
    rows = []
    for folder, status in (("raw/tb", "DELIVERED"), ("late_arrivals", "LATE")):
        for path in sorted((data_dir / folder).glob("tb_*.csv")):
            df = read_tb(path)
            _, company, yyyymm = path.stem.split("_")
            rows.append({"file_name": path.name, "company_code": company, "fiscal_year": int(yyyymm[:4]),
                         "fiscal_period": int(yyyymm[4:]), "row_count": len(df),
                         "sum_amount_lc": f"{df['cents'].sum() / 100:.2f}",
                         "sum_abs_amount_lc": f"{df['cents'].abs().sum() / 100:.2f}",
                         "delivery_status": status})
    manifest = pd.DataFrame(rows)
    (data_dir / "raw" / "control").mkdir(parents=True, exist_ok=True)
    manifest.to_csv(data_dir / "raw" / "control" / "control_totals.csv", index=False)
    return manifest


def inject_errors(data_dir: Path, seed: int = cfg.SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 99)
    gl = group_lookup(data_dir)
    entries = [
        e01_unbalanced(data_dir, gl),
        e02_unmapped(data_dir, gl),
        e03_missing_fx(data_dir),
        e04_fx_outlier(data_dir),
        e05_late_file(data_dir),
        e06_duplicates(data_dir, rng),
        e07_unknown_partner(data_dir, gl),
        e08_self_partner(data_dir, gl),
        e09_cutoff(data_dir, gl),
        e10_wrong_rate(data_dir, gl),
        e11_one_sided(data_dir, gl),
    ]
    write_control_totals(data_dir)             # manifest reflects what the source system sent ...
    entries.append(e12_truncated(data_dir))    # ... before the transfer truncated one file
    key = pd.DataFrame(entries)
    (data_dir / "_expected").mkdir(parents=True, exist_ok=True)
    key.to_csv(data_dir / "_expected" / "injected_errors.csv", index=False)
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=cfg.DEFAULT_DATA_DIR)
    parser.add_argument("--seed", type=int, default=cfg.SEED)
    args = parser.parse_args()
    key = inject_errors(args.out, args.seed)
    print(key[["error_id", "dq_rule_id", "severity", "company_code", "fiscal_year", "fiscal_period"]].to_string(index=False))


if __name__ == "__main__":
    main()
