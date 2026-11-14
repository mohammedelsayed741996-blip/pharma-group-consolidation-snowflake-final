"""
Run the Helix Pharma consolidation platform in Snowflake, step by step.

    python orchestration/run_pipeline.py setup          # warehouse, database, schemas, roles, stage, RAW + AUDIT tables
    python orchestration/run_pipeline.py load           # PUT local files + COPY INTO (incremental for trial balances)
    python orchestration/run_pipeline.py load --late    # deliver data/late_arrivals (incremental-load demo)
    python orchestration/run_pipeline.py load --full-refresh
    python orchestration/run_pipeline.py transform      # deploy STAGING, INTEGRATION, MARTS, AUDIT objects
    python orchestration/run_pipeline.py refresh        # CALL MARTS.SP_REFRESH()
    python orchestration/run_pipeline.py dq             # CALL AUDIT.SP_RUN_DQ() + findings summary
    python orchestration/run_pipeline.py security [--share]   # row access policy, secure views (+ share)
    python orchestration/run_pipeline.py tasks [--resume | --suspend]
    python orchestration/run_pipeline.py status         # close-status grid (entity x period)
    python orchestration/run_pipeline.py verify         # reconcile Snowflake against the pandas reference engine
    python orchestration/run_pipeline.py all            # setup, load, transform, refresh, dq, security, tasks, verify

Connection settings come from environment variables or a .env file (see .env.example).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "snowflake"
sys.path.insert(0, str(REPO_ROOT))

# Files executed by each step (order matters)
STEP_FILES = {
    "setup": [
        "00_setup/01_warehouse_database_schemas.sql",
        "00_setup/02_roles_grants.sql",
        "00_setup/03_file_formats_stages.sql",
        "10_raw/01_raw_tables.sql",
        "50_audit/01_audit_tables.sql",
    ],
    "transform": [
        "20_staging/01_staging_views.sql",
        "30_integration/01_config_spines_fx.sql",
        "30_integration/02_mapping_translation.sql",
        "30_integration/03_intercompany.sql",
        "30_integration/04_consolidation_adjustments.sql",
        "40_marts/01_mart_views.sql",
        "40_marts/02_mart_tables.sql",
        "40_marts/03_refresh_procedure.sql",
        "50_audit/02_dq_rules.sql",
    ],
    "security": [
        "70_security_sharing/01_row_access_policy.sql",
        "70_security_sharing/02_secure_views.sql",
    ],
    "share": ["70_security_sharing/03_secure_share.sql"],
    "tasks": ["60_tasks/01_streams_tasks.sql"],
}
COPY_FILE = "10_raw/02_copy_into.sql"

# Local folder (relative to the data dir) -> stage folder, file glob
UPLOADS = [
    ("master", "master", "*.csv"),
    ("fx", "fx", "*.csv"),
    ("raw/tb", "tb", "tb_*.csv"),
    ("raw/ic", "ic", "*.csv"),
    ("raw/budget", "budget", "budget_*.csv"),
    ("raw/control", "control", "*.csv"),
    ("raw/adjustments", "adjustments", "*.csv"),
]


# --------------------------------------------------------------------------- #
# SQL handling (no Snowflake dependency - unit tested offline)
# --------------------------------------------------------------------------- #
def split_sql(text: str) -> list[str]:
    """Split a SQL script into statements.

    Handles -- line comments, /* block comments */, 'single-quoted strings' (with ''
    escapes), "quoted identifiers" and $$ dollar-quoted bodies (stored procedures).
    Comments are removed; statements are returned without the trailing semicolon.
    """
    statements, buf = [], []
    i, n = 0, len(text)
    while i < n:
        c, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":                                   # line comment
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and nxt == "*":                                   # block comment
            j = text.find("*/", i + 2)
            if j == -1:
                raise ValueError("Unterminated block comment")
            i = j + 2
            buf.append(" ")
            continue
        if c == "$" and nxt == "$":                                   # $$ ... $$ body
            j = text.find("$$", i + 2)
            if j == -1:
                raise ValueError("Unterminated $$ block")
            buf.append(text[i:j + 2])
            i = j + 2
            continue
        if c in ("'", '"'):                                           # quoted text
            j = i + 1
            while True:
                if j >= n:
                    raise ValueError(f"Unterminated quote starting at offset {i}")
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:                # escaped quote
                        j += 2
                        continue
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def render(sql: str, params: dict[str, str]) -> str:
    for k, v in params.items():
        sql = sql.replace("{{" + k + "}}", v)
    return sql


def read_statements(rel_path: str, params: dict[str, str] | None = None) -> list[str]:
    text = (SQL_DIR / rel_path).read_text(encoding="utf-8")
    return split_sql(render(text, params or {}))


# --------------------------------------------------------------------------- #
# Snowflake connection
# --------------------------------------------------------------------------- #
def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:          # python-dotenv is optional; plain environment variables also work
        pass


def connect(role: str | None = None):
    import snowflake.connector

    load_env()
    params = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": role or os.environ.get("SNOWFLAKE_ROLE", "FIN_ENGINEER"),
        "session_parameters": {"QUERY_TAG": "helix-consolidation"},
        "client_session_keep_alive": False,
    }
    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
    if key_path:
        from cryptography.hazmat.primitives import serialization

        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
        key = serialization.load_pem_private_key(
            Path(key_path).expanduser().read_bytes(),
            password=passphrase.encode() if passphrase else None)
        params["private_key"] = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
    else:
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    return snowflake.connector.connect(**params)


def use_context(cur) -> None:
    cur.execute(f"USE WAREHOUSE {os.environ.get('SNOWFLAKE_WAREHOUSE', 'WH_FIN_XS')}")
    cur.execute(f"USE DATABASE {os.environ.get('SNOWFLAKE_DATABASE', 'HELIX_FIN')}")


def run_statements(cur, statements: list[str], label: str, quiet: bool = False) -> list:
    """Execute statements one by one; stop with a clear message on the first error."""
    results = []
    for k, stmt in enumerate(statements, 1):
        first_line = " ".join(stmt.split())[:110]
        t0 = time.time()
        try:
            cur.execute(stmt)
        except Exception as exc:                    # noqa: BLE001 - re-raised with context
            print(f"\n  FAILED in {label}, statement {k}/{len(statements)}:\n    {first_line}\n  {exc}")
            raise SystemExit(1) from exc
        rows = cur.fetchall() if cur.description else []
        cols = [d[0] for d in cur.description] if cur.description else []
        results.append((stmt, cols, rows))
        if not quiet:
            print(f"  [{k:>2}/{len(statements)}] {time.time() - t0:5.1f}s  {first_line}")
    return results


def run_files(cur, step: str) -> None:
    for rel in STEP_FILES[step]:
        print(f"-- {rel}")
        run_statements(cur, read_statements(rel), rel)


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def step_setup(args) -> None:
    conn = connect(role=os.environ.get("SNOWFLAKE_ADMIN_ROLE", "ACCOUNTADMIN"))
    try:
        run_files(conn.cursor(), "setup")
    finally:
        conn.close()


def _put(cur, local_dir: Path, pattern: str, stage_folder: str) -> int:
    files = sorted(local_dir.glob(pattern))
    if not files:
        return 0
    uri = (local_dir.resolve() / pattern).as_posix()
    if not uri.startswith("/"):
        uri = "/" + uri                                          # Windows: file:///C:/...
    cur.execute(f"PUT 'file://{uri}' @RAW.LANDING/{stage_folder}/ "
                f"AUTO_COMPRESS = TRUE OVERWRITE = TRUE PARALLEL = 8")
    cur.fetchall()
    return len(files)


def _log_copy(cur, load_id: str, stmt: str, cols: list[str], rows: list) -> None:
    target = stmt.split()[2] if stmt.upper().startswith("COPY INTO") else "?"
    cols_l = [c.lower() for c in cols]
    records = []
    for r in rows:
        d = dict(zip(cols_l, r))
        records.append((load_id, target, d.get("file", ""), str(d.get("status", "")),
                        d.get("rows_parsed"), d.get("rows_loaded"), d.get("errors_seen"),
                        (d.get("first_error") or "")[:1000]))
    if records:
        cur.executemany(
            "INSERT INTO AUDIT.LOAD_LOG (LOAD_ID, TARGET_TABLE, FILE_NAME, STATUS, ROWS_PARSED, "
            "ROWS_LOADED, ERRORS_SEEN, FIRST_ERROR) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", records)
    loaded = sum(int(r[5] or 0) for r in records)
    files = sum(1 for r in records if r[2])
    print(f"      {target:<24} files: {files:>4}  rows loaded: {loaded:>8,}")


def step_load(args) -> None:
    data_dir = Path(args.data).resolve()
    load_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    conn = connect()
    cur = conn.cursor()
    try:
        use_context(cur)
        if args.late:
            n = _put(cur, data_dir / "late_arrivals", "tb_*.csv", "tb")
            print(f"PUT {n} late file(s) into @RAW.LANDING/tb/")
        else:
            if args.full_refresh:
                print("Full refresh: clearing stage folder tb/ and RAW.TB_LINES")
                cur.execute("REMOVE @RAW.LANDING/tb/")
                cur.execute("TRUNCATE TABLE RAW.TB_LINES")
            for local, stage, pattern in UPLOADS:
                n = _put(cur, data_dir / local, pattern, stage)
                print(f"PUT {n:>4} file(s)  {local:<16} -> @RAW.LANDING/{stage}/")
        params = {"TB_FORCE": "TRUE" if args.full_refresh else "FALSE", "LOAD_ID": load_id}
        statements = read_statements(COPY_FILE, params)
        if args.late:   # only the trial-balance COPY and the load-id stamp
            statements = [s for s in statements if "RAW.TB_LINES" in s and not s.upper().startswith("TRUNCATE")]
        print(f"COPY INTO (load id {load_id})")
        for stmt, cols, rows in run_statements(cur, statements, COPY_FILE, quiet=True):
            if stmt.upper().startswith("COPY INTO"):
                _log_copy(cur, load_id, stmt, cols, rows)
    finally:
        conn.close()


def step_transform(args) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        run_files(cur, "transform")
        print("\nNote: mart tables were re-created - run the 'security' step to re-attach row access policies.")
    finally:
        conn.close()


def _call(proc: str) -> str:
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        t0 = time.time()
        cur.execute(f"CALL {proc}")
        msg = cur.fetchone()[0]
        print(f"{proc}: {msg}  ({time.time() - t0:.1f}s)")
        return msg
    finally:
        conn.close()


def step_refresh(args) -> None:
    _call("MARTS.SP_REFRESH()")


def step_dq(args) -> None:
    _call("AUDIT.SP_RUN_DQ()")
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        cur.execute("SELECT RULE_ID, SEVERITY, COUNT(*) FROM AUDIT.V_DQ_LATEST GROUP BY 1, 2 ORDER BY 1")
        rows = cur.fetchall()
        print("\nFindings by rule (latest run):")
        for rule, sev, n in rows:
            print(f"  {rule}  {sev:<8} {n:>5}")
        if not rows:
            print("  none - all 12 rules passed")
    finally:
        conn.close()


def step_security(args) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        run_files(cur, "security")
    finally:
        conn.close()
    if args.share:
        conn = connect(role=os.environ.get("SNOWFLAKE_ADMIN_ROLE", "ACCOUNTADMIN"))
        try:
            run_files(conn.cursor(), "share")
        finally:
            conn.close()


def step_tasks(args) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        if not args.resume and not args.suspend:
            run_files(cur, "tasks")
            print("Task DAG created (suspended). Use --resume to start it.")
        if args.resume:     # children first, root last
            for t in ("AUDIT.T_RUN_DQ", "AUDIT.T_REFRESH_MARTS", "AUDIT.T_DETECT_NEW_TB"):
                cur.execute(f"ALTER TASK {t} RESUME")
            print("Task DAG resumed: new trial balances are consolidated within 15 minutes.")
        if args.suspend:
            cur.execute("ALTER TASK AUDIT.T_DETECT_NEW_TB SUSPEND")
            print("Root task suspended.")
    finally:
        conn.close()


def step_status(args) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        cur.execute("SELECT PERIOD_ID, COMPANY_CODE, CLOSE_STATUS, BLOCKING_RULES, WARNING_RULES "
                    "FROM AUDIT.V_CLOSE_STATUS ORDER BY PERIOD_ID, COMPANY_CODE")
        rows = cur.fetchall()
    finally:
        conn.close()
    companies = sorted({r[1] for r in rows if r[1] != "*GROUP"}) + ["*GROUP"]
    grid: dict[str, dict[str, str]] = {}
    details = []
    for pid, comp, status, block, warn in rows:
        grid.setdefault(pid, {})[comp] = {"BLOCKED": "B", "WARNING": "w", "CLEAN": "."}[status]
        if status == "BLOCKED" and comp != "*GROUP":
            details.append(f"  {pid} {comp}: {block}")
    print("Close status   (B = blocked, w = warning, . = clean)")
    print("period  " + " ".join(f"{c[:4]:>4}" for c in companies))
    for pid in sorted(grid):
        print(f"{pid}  " + " ".join(f"{grid[pid].get(c, '?'):>4}" for c in companies))
    if details:
        print("\nBlocking findings:\n" + "\n".join(details))


def step_verify(args) -> None:
    """Reconcile Snowflake results with the pandas reference engine on the same files."""
    import pandas as pd
    from orchestration.reference_engine import run_reference, summary

    conn = connect()
    try:
        cur = conn.cursor()
        use_context(cur)
        cur.execute("SELECT COUNT(*) FROM STAGING.V_TB_LINES WHERE FILE_NAME IN "
                    "(SELECT FILE_NAME FROM STAGING.V_CONTROL_TOTALS WHERE DELIVERY_STATUS = 'LATE')")
        include_late = cur.fetchone()[0] > 0
        cur.execute("SELECT RULE_ID, COMPANY_CODE, COALESCE(PARTNER_CODE, ''), PERIOD_ID, COALESCE(OBJECT_NAME, '') "
                    "FROM AUDIT.V_DQ_LATEST")
        sf_dq = {tuple(r) for r in cur.fetchall()}
        cur.execute("SELECT SCENARIO, LAYER, PERIOD_ID, GROUP_ACCOUNT, SUM(AMOUNT_GC) "
                    "FROM MARTS.FCT_CONSOLIDATED_GL GROUP BY 1, 2, 3, 4")
        sf_fact = pd.DataFrame(cur.fetchall(), columns=["scenario", "layer", "period_id", "group_account", "sf"])
        cur.execute("SELECT LAYER, PERIOD_ID, CASHFLOW_LINE, SUM(AMOUNT_GC) FROM MARTS.FCT_CASHFLOW GROUP BY 1, 2, 3")
        sf_cf = pd.DataFrame(cur.fetchall(), columns=["layer", "period_id", "cashflow_line", "sf"])
        cur.execute("SELECT MATCH_STATUS, COUNT(*) FROM MARTS.FCT_IC_RECON WHERE SCENARIO = 'ACT' GROUP BY 1")
        sf_recon = dict(cur.fetchall())
    finally:
        conn.close()

    print(f"Running reference engine on {args.data} (late files included: {include_late}) ...")
    ref = run_reference(args.data, include_late=include_late)
    checks = []
    # 1. data-quality findings must be identical
    rd = ref["dq_results"]
    ref_dq = set(zip(rd["rule_id"], rd["company_code"], rd["partner_code"].fillna(""), rd["period_id"],
                     rd["object_name"].fillna("")))
    checks.append(("DQ findings identical", sf_dq == ref_dq,
                   f"Snowflake {len(sf_dq)} / reference {len(ref_dq)}; only in SF: {sorted(sf_dq - ref_dq)[:3]} "
                   f"only in ref: {sorted(ref_dq - sf_dq)[:3]}"))
    # 2. consolidated fact by scenario / layer / period / account
    f = ref["fact"].groupby(["scenario", "layer", "period_id", "group_account"])["amount_gc"].sum().div(100).rename("ref")
    sf_fact["sf"] = sf_fact["sf"].astype(float)
    j = sf_fact.set_index(["scenario", "layer", "period_id", "group_account"])["sf"].to_frame().join(f, how="outer").fillna(0)
    worst = float((j["sf"] - j["ref"]).abs().max())
    checks.append(("Fact cells within EUR 0.05", worst <= 0.05, f"{len(j):,} cells, max |diff| = {worst:,.2f}"))
    # 3. cash flow
    c = ref["cashflow"].groupby(["layer", "period_id", "cashflow_line"])["amount_gc"].sum().div(100).rename("ref")
    sf_cf["sf"] = sf_cf["sf"].astype(float)
    jc = sf_cf.set_index(["layer", "period_id", "cashflow_line"])["sf"].to_frame().join(c, how="outer").fillna(0)
    worst_c = float((jc["sf"] - jc["ref"]).abs().max())
    checks.append(("Cash-flow cells within EUR 0.05", worst_c <= 0.05, f"{len(jc):,} cells, max |diff| = {worst_c:,.2f}"))
    # 4. IC reconciliation status counts
    ref_recon = ref["ic_recon"]["match_status"].value_counts().to_dict()
    checks.append(("IC recon status counts identical", sf_recon == ref_recon, f"SF {sf_recon} / ref {ref_recon}"))

    print("\nReconciliation Snowflake vs reference engine")
    for name, ok, info in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<34} {info}")
    print("\nHeadline figures (reference engine, identical in Snowflake when all checks pass):")
    for k, v in summary(ref).items():
        print(f"  {k:<28} {v}")
    if not all(ok for _, ok, _ in checks):
        raise SystemExit(1)


def step_all(args) -> None:
    for name, fn in (("setup", step_setup), ("load", step_load), ("transform", step_transform),
                     ("refresh", step_refresh), ("dq", step_dq), ("security", step_security),
                     ("tasks", step_tasks), ("verify", step_verify)):
        print(f"\n===== {name.upper()} =====")
        fn(args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["setup", "load", "transform", "refresh", "dq", "security",
                                         "tasks", "status", "verify", "all"])
    parser.add_argument("--data", default=os.environ.get("HELIX_DATA_DIR", str(REPO_ROOT / "data")),
                        help="data folder produced by generator/run_all.py")
    parser.add_argument("--late", action="store_true", help="load: deliver data/late_arrivals only")
    parser.add_argument("--full-refresh", action="store_true", help="load: reload all trial balances")
    parser.add_argument("--share", action="store_true", help="security: also create the secure share")
    parser.add_argument("--resume", action="store_true", help="tasks: resume the task DAG")
    parser.add_argument("--suspend", action="store_true", help="tasks: suspend the task DAG")
    args = parser.parse_args()
    load_env()
    t0 = time.time()
    {"setup": step_setup, "load": step_load, "transform": step_transform, "refresh": step_refresh,
     "dq": step_dq, "security": step_security, "tasks": step_tasks, "status": step_status,
     "verify": step_verify, "all": step_all}[args.step](args)
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
