"""
Static checks of the Snowflake SQL - no Snowflake account needed.

* every script splits into statements and parentheses balance in each statement
* every referenced HELIX object is created BEFORE it is used, in pipeline execution order
  (procedures and tasks resolve objects at call time, so they only need to exist somewhere)
* RAW table columns match the CSV headers, and every COPY selects the right number of fields
* SQL seed tables (consolidation accounts, IC pairs, parameters) match the reference engine
* all 12 data-quality rules are implemented
"""
from __future__ import annotations

import csv
import re

import pytest

from orchestration import reference_engine as ref
from orchestration.run_pipeline import COPY_FILE, SQL_DIR, STEP_FILES, read_statements, split_sql

EXEC_ORDER = (STEP_FILES["setup"] + [COPY_FILE] + STEP_FILES["transform"] + STEP_FILES["security"]
              + STEP_FILES["share"] + STEP_FILES["tasks"])
PARAMS = {"TB_FORCE": "FALSE", "LOAD_ID": "TEST"}
SCHEMAS = ("RAW", "STAGING", "INTEGRATION", "MARTS", "AUDIT")
CREATE_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?"
    r"(TABLE|VIEW|PROCEDURE|TASK|STREAM|STAGE|FILE\s+FORMAT|ROW\s+ACCESS\s+POLICY|SHARE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:HELIX_FIN\.)?([A-Z0-9_]+\.[A-Z0-9_]+|[A-Z0-9_]+)", re.I)
REF_RE = re.compile(r"\b(?:HELIX_FIN\.)?(RAW|STAGING|INTEGRATION|MARTS|AUDIT)\.([A-Z][A-Z0-9_]*)\b")


def all_statements():
    for rel in EXEC_ORDER:
        for k, stmt in enumerate(read_statements(rel, PARAMS)):
            yield rel, k, stmt


def created_name(stmt: str):
    m = CREATE_RE.match(stmt)
    return (m.group(1).upper().split()[0], m.group(2).upper()) if m else (None, None)


def strip_quoted(stmt: str) -> str:
    """Remove string literals and $$ bodies (used for the parenthesis check)."""
    stmt = re.sub(r"\$\$.*?\$\$", "", stmt, flags=re.S)
    return re.sub(r"'(?:[^']|'')*'", "''", stmt)


def test_every_file_exists_and_splits():
    for rel in EXEC_ORDER:
        assert (SQL_DIR / rel).exists(), rel
        assert read_statements(rel, PARAMS), f"{rel} has no statements"


@pytest.mark.parametrize("rel", EXEC_ORDER)
def test_parentheses_balance(rel):
    for stmt in read_statements(rel, PARAMS):
        bodies = re.findall(r"\$\$(.*?)\$\$", stmt, flags=re.S)
        for part in [strip_quoted(stmt)] + [strip_quoted(s) for b in bodies for s in split_sql(b)]:
            depth = 0
            for ch in part:
                depth += {"(": 1, ")": -1}.get(ch, 0)
                assert depth >= 0, f"{rel}: unbalanced ')' in {part[:120]}"
            assert depth == 0, f"{rel}: unbalanced '(' in {part[:120]}"


def test_objects_are_created_before_use():
    created: set[str] = set()
    deferred: list[tuple[str, str]] = []           # (file, reference) checked at the end
    problems = []
    for rel, k, stmt in all_statements():
        kind, name = created_name(stmt)
        if name and "." in name:
            created.add(name)
        # free-text strings (comments, messages) are ignored; strings that ARE an object name
        # (FORMAT_NAME = 'RAW.FF_CSV', SYSTEM$STREAM_HAS_DATA('RAW.STR_TB_LINES')) are checked
        code = re.sub(r"'(?:[^']|'')*'",
                      lambda m: m.group(0) if re.fullmatch(r"'[A-Z_]+\.[A-Z0-9_]+'", m.group(0), re.I) else "''",
                      stmt)
        refs = {f"{s}.{o}".upper() for s, o in REF_RE.findall(code.upper())}
        refs.discard(name)
        call_time = kind in ("PROCEDURE", "TASK")
        guarded = re.match(r"^(ALTER|TRUNCATE)\s+\w+(\s+\w+)*?\s+IF\s+EXISTS", stmt, re.I)
        for r in refs:
            if call_time:
                deferred.append((rel, r))
            elif r not in created and not guarded:
                problems.append(f"{rel} stmt {k + 1}: {r} used before it is created")
    for rel, r in deferred:
        if r not in created:
            problems.append(f"{rel}: {r} referenced in a procedure/task but never created")
    assert not problems, "\n".join(problems)


def test_refresh_procedure_covers_every_mart_view():
    views = {name for _, _, s in all_statements() for kind, name in [created_name(s)]
             if kind == "VIEW" and name and name.startswith("INTEGRATION.V_MART_")}
    proc = (SQL_DIR / "40_marts/03_refresh_procedure.sql").read_text()
    tables = (SQL_DIR / "40_marts/02_mart_tables.sql").read_text()
    for v in views:
        mart = "MARTS." + v.split("V_MART_")[1]
        assert f"INSERT OVERWRITE INTO {mart} SELECT * FROM {v};" in proc, v
        assert f"CREATE OR REPLACE TABLE {mart} AS SELECT * FROM {v} LIMIT 0;" in tables, v


RAW_FILES = {
    "ENTITIES": "master/entities.csv", "CURRENCIES": "master/currencies.csv",
    "BUSINESS_UNITS": "master/business_units.csv", "GROUP_ACCOUNTS": "master/group_accounts.csv",
    "LOCAL_ACCOUNTS": "master/local_accounts.csv", "COST_CENTRES": "master/cost_centres.csv",
    "PROFIT_CENTRES": "master/profit_centres.csv", "IC_TRANSFER_PRICING": "master/ic_transfer_pricing.csv",
    "DQ_RULES": "master/dq_rules.csv", "FX_RATES": "fx/fx_rates.csv", "FX_BUDGET": "fx/fx_budget.csv",
    "FX_HISTORICAL": "fx/fx_historical.csv", "IC_TRANSACTIONS": "raw/ic/ic_transactions.csv",
    "BUDGET": "raw/budget/budget_2024.csv", "CONTROL_TOTALS": "raw/control/control_totals.csv",
    "MGMT_ADJUSTMENTS": "raw/adjustments/management_adjustments.csv", "TB_LINES": "raw/tb/tb_ES01_202401.csv",
}


def test_raw_tables_match_csv_headers(error_data):
    ddl = {}
    for stmt in read_statements("10_raw/01_raw_tables.sql"):
        m = re.match(r"CREATE TABLE IF NOT EXISTS RAW\.(\w+) \((.*)\)\s*COMMENT", stmt, re.S)
        if m:
            cols = [ln.strip().split()[0] for ln in m.group(2).split(",\n")]
            ddl[m.group(1)] = [c for c in cols if not c.startswith("_")]
    assert set(ddl) == set(RAW_FILES)
    for table, rel in RAW_FILES.items():
        header = next(csv.reader(open(error_data["dir"] / rel, encoding="utf-8")))
        assert ddl[table] == [h.upper() for h in header], table
    for stmt in read_statements(COPY_FILE, PARAMS):
        m = re.match(r"COPY INTO RAW\.(\w+) \(([^)]*)\)", stmt)
        if m:
            n_cols = len(ddl[m.group(1)])
            assert stmt.count("$") == n_cols + 2, m.group(1)     # + METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
            assert m.group(2).count(",") + 1 == n_cols + 2


def test_seeds_match_reference_engine():
    sql = (SQL_DIR / "30_integration/01_config_spines_fx.sql").read_text()
    accounts = set(re.findall(r"\('(\d{4})', '[^']*', '(?:PL|BS)'", sql))
    assert accounts == {a[0] for a in ref.CONSOLIDATION_ACCOUNTS}
    pairs = {p: (a, b, basis) for p, a, b, basis in
             re.findall(r"\('([A-Z_]+)',\s+'(\d{4})', '(\d{4})', '(PL|BS)'", sql)}
    assert pairs == ref.IC_PAIRS
    config_block = next(st for st in split_sql(sql) if st.startswith("INSERT INTO INTEGRATION.CONFIG"))
    config = {k: float(v) for k, v in re.findall(r"\('([A-Z_]+)',\s+([0-9.]+),", config_block)}
    assert config == {"FIRST_YEAR": ref.FIRST_YEAR, "N_PERIODS": ref.N_PERIODS,
                      "IC_TOLERANCE_ABS": ref.IC_TOLERANCE_ABS, "IC_TOLERANCE_PCT": ref.IC_TOLERANCE_PCT,
                      "CHECK_TOLERANCE": ref.CHECK_TOLERANCE, "FX_JUMP_THRESHOLD": ref.FX_JUMP_THRESHOLD}


def test_all_twelve_rules_implemented():
    sql = (SQL_DIR / "50_audit/02_dq_rules.sql").read_text()
    for k in range(1, 13):
        assert f"'DQ{k:02d}'" in sql, f"DQ{k:02d} missing"


def test_no_placeholders_left():
    for path in SQL_DIR.rglob("*.sql"):
        text = path.read_text()
        assert "TODO" not in text and "FIXME" not in text, path
