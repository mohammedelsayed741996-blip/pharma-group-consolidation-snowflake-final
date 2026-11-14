/* =============================================================================
   50_audit / 02  The 12 data-quality rules, the DQ procedure and the close status
   Mirrors: orchestration/reference_engine.py  dq_rules(), close_status()

   AUDIT.V_DQ_CHECKS returns one row per finding (failures only). AUDIT.SP_RUN_DQ()
   stores them with a run id. A BLOCKING finding marks the entity-period - and the group
   for that period - as BLOCKED: the period cannot be released. Data is still processed:
   unmapped / unbalanced amounts sit on suspense account 9900 and invalid IC lines are not
   eliminated, so the impact is visible instead of silently dropped.
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA AUDIT;

CREATE OR REPLACE VIEW AUDIT.V_DQ_CHECKS
    COMMENT = 'Findings of the 12 data-quality rules (failures only)'
AS
WITH cfg AS (
    SELECT
        MAX(IFF(CONFIG_KEY = 'CHECK_TOLERANCE',   VALUE_NUM, NULL)) AS CHECK_TOL,
        MAX(IFF(CONFIG_KEY = 'FX_JUMP_THRESHOLD', VALUE_NUM, NULL)) AS FX_JUMP
    FROM INTEGRATION.CONFIG
),
spine AS (
    SELECT PERIOD_ID, PERIOD_SEQ, FX_PERIOD FROM INTEGRATION.V_PERIOD_SPINE
),
fx_change AS (
    SELECT
        CURRENCY, PERIOD_ID,
        ABS(AVG_RATE     / LAG(AVG_RATE)     OVER (PARTITION BY CURRENCY ORDER BY PERIOD_ID) - 1) AS CHG_AVG,
        ABS(CLOSING_RATE / LAG(CLOSING_RATE) OVER (PARTITION BY CURRENCY ORDER BY PERIOD_ID) - 1) AS CHG_CLOSE
    FROM STAGING.V_FX_RATES
),
delivered AS (
    SELECT DISTINCT COMPANY_CODE, PERIOD_ID FROM STAGING.V_TB_LINES
),
bs_by_period AS (
    SELECT s.PERIOD_ID, s.PERIOD_SEQ, COALESCE(SUM(f.AMOUNT_GC), 0) AS MV
    FROM spine s
    LEFT JOIN MARTS.FCT_CONSOLIDATED_GL f
           ON f.PERIOD_ID = s.PERIOD_ID
          AND f.SCENARIO = 'ACT'
    GROUP BY s.PERIOD_ID, s.PERIOD_SEQ
),
bs_cumulative AS (
    SELECT PERIOD_ID,
           SUM(MV) OVER (ORDER BY PERIOD_SEQ ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS CUM_GC
    FROM bs_by_period
),
cf_translated AS (
    SELECT COMPANY_CODE, PERIOD_ID, SUM(AMOUNT_GC) AS CF_GC
    FROM MARTS.FCT_CASHFLOW
    WHERE LAYER = 'TRANSLATED'
    GROUP BY COMPANY_CODE, PERIOD_ID
),
cash_movement AS (
    SELECT g.COMPANY_CODE, g.PERIOD_ID, SUM(g.AMOUNT_GC) AS CASH_GC
    FROM INTEGRATION.GL_TRANSLATED g
    JOIN INTEGRATION.V_ACCOUNTS a ON a.GROUP_ACCOUNT = g.GROUP_ACCOUNT
    JOIN spine s ON s.PERIOD_ID = g.PERIOD_ID
    WHERE a.CASHFLOW_LINE = 'CASH'
      AND s.PERIOD_SEQ > 0
    GROUP BY g.COMPANY_CODE, g.PERIOD_ID
),
cf_difference AS (
    SELECT COALESCE(c.COMPANY_CODE, m.COMPANY_CODE) AS COMPANY_CODE,
           COALESCE(c.PERIOD_ID, m.PERIOD_ID)       AS PERIOD_ID,
           COALESCE(c.CF_GC, 0) - COALESCE(m.CASH_GC, 0) AS DIFF_GC
    FROM cf_translated c
    FULL OUTER JOIN cash_movement m
      ON m.COMPANY_CODE = c.COMPANY_CODE
     AND m.PERIOD_ID = c.PERIOD_ID
),
loaded_files AS (
    SELECT FILE_NAME, COUNT(*) AS ROWS_LOADED, SUM(AMOUNT_LC) AS AMOUNT_LOADED
    FROM STAGING.V_TB_TYPED
    GROUP BY FILE_NAME
)
-- DQ01 TB_BALANCED ------------------------------------------------------------
SELECT 'DQ01' AS RULE_ID, COMPANY_CODE, '' AS PARTNER_CODE, PERIOD_ID, '' AS OBJECT_NAME,
       1 AS FAILING_ROWS, SUM(AMOUNT_LC)::FLOAT AS METRIC_VALUE,
       'TB out of balance by ' || TO_VARCHAR(SUM(AMOUNT_LC)) AS DETAILS
FROM STAGING.V_TB_LINES
GROUP BY COMPANY_CODE, PERIOD_ID
HAVING SUM(AMOUNT_LC) <> 0

-- DQ02 ACCOUNT_MAPPED ---------------------------------------------------------
UNION ALL
SELECT 'DQ02', t.COMPANY_CODE, '', t.PERIOD_ID, t.GL_ACCOUNT, COUNT(*), SUM(t.AMOUNT_LC)::FLOAT,
       'Unmapped local account ' || t.GL_ACCOUNT
FROM STAGING.V_TB_LINES t
LEFT JOIN STAGING.V_ENTITIES e
       ON e.COMPANY_CODE = t.COMPANY_CODE
LEFT JOIN STAGING.V_LOCAL_ACCOUNTS la
       ON la.CHART_OF_ACCOUNTS = e.LOCAL_CHART_OF_ACCOUNTS
      AND la.LOCAL_ACCOUNT = t.GL_ACCOUNT
WHERE la.LOCAL_ACCOUNT IS NULL
GROUP BY t.COMPANY_CODE, t.PERIOD_ID, t.GL_ACCOUNT

-- DQ03 FX_COMPLETE (reported for every entity using the currency) -------------
UNION ALL
SELECT 'DQ03', e.COMPANY_CODE, '', s.PERIOD_ID, f.CURRENCY, 1, NULL::FLOAT,
       f.CURRENCY || ' rates missing for ' || f.FX_PERIOD
FROM INTEGRATION.V_FX_RATES_FILLED f
JOIN STAGING.V_ENTITIES e ON e.LOCAL_CURRENCY = f.CURRENCY
JOIN spine s ON s.FX_PERIOD = f.FX_PERIOD
WHERE f.RATE_STATUS = 'FALLBACK'

-- DQ04 FX_PLAUSIBLE ------------------------------------------------------------
UNION ALL
SELECT 'DQ04', e.COMPANY_CODE, '', COALESCE(s.PERIOD_ID, x.PERIOD_ID), x.CURRENCY, 1,
       GREATEST(COALESCE(x.CHG_AVG, 0), COALESCE(x.CHG_CLOSE, 0))::FLOAT,
       x.CURRENCY || ' rate moved ' || TO_VARCHAR(ROUND(100 * GREATEST(COALESCE(x.CHG_AVG, 0), COALESCE(x.CHG_CLOSE, 0)), 1))
           || ' % vs prior month'
FROM fx_change x
JOIN STAGING.V_ENTITIES e ON e.LOCAL_CURRENCY = x.CURRENCY
LEFT JOIN spine s ON s.FX_PERIOD = x.PERIOD_ID
WHERE GREATEST(COALESCE(x.CHG_AVG, 0), COALESCE(x.CHG_CLOSE, 0)) > (SELECT FX_JUMP FROM cfg)

-- DQ05 SUBMISSION_COMPLETE -----------------------------------------------------
UNION ALL
SELECT 'DQ05', e.COMPANY_CODE, '', s.PERIOD_ID, '', 0, NULL::FLOAT, 'No trial balance delivered'
FROM STAGING.V_ENTITIES e
CROSS JOIN spine s
LEFT JOIN delivered d
       ON d.COMPANY_CODE = e.COMPANY_CODE
      AND d.PERIOD_ID = s.PERIOD_ID
WHERE d.COMPANY_CODE IS NULL

-- DQ06 NO_DUPLICATES -------------------------------------------------------------
UNION ALL
SELECT 'DQ06', COMPANY_CODE, '', PERIOD_ID, FILE_NAME, COUNT(*) - COUNT(DISTINCT ROW_HASH), NULL::FLOAT,
       TO_VARCHAR(COUNT(*) - COUNT(DISTINCT ROW_HASH)) || ' duplicate rows'
FROM STAGING.V_TB_TYPED
GROUP BY COMPANY_CODE, PERIOD_ID, FILE_NAME
HAVING COUNT(*) > COUNT(DISTINCT ROW_HASH)

-- DQ07 PARTNER_VALID -------------------------------------------------------------
UNION ALL
SELECT 'DQ07', t.COMPANY_CODE, t.TRADING_PARTNER, t.PERIOD_ID, '', COUNT(*), NULL::FLOAT,
       'Invalid trading partner ' || t.TRADING_PARTNER
FROM STAGING.V_TB_LINES t
LEFT JOIN STAGING.V_ENTITIES pe
       ON pe.COMPANY_CODE = t.TRADING_PARTNER
WHERE t.TRADING_PARTNER <> ''
  AND (pe.COMPANY_CODE IS NULL OR t.TRADING_PARTNER = t.COMPANY_CODE)
GROUP BY t.COMPANY_CODE, t.TRADING_PARTNER, t.PERIOD_ID

-- DQ08 IC_MATCHED (reported on the side-B entity, partner = side A) ---------------
UNION ALL
SELECT 'DQ08', ENTITY_B, ENTITY_A, PERIOD_ID, PAIR_TYPE, 1, DIFFERENCE_GC::FLOAT,
       PAIR_TYPE || ' ' || MATCH_STATUS || ': A ' || TO_VARCHAR(SIDE_A_GC) || ' / B ' || TO_VARCHAR(SIDE_B_GC)
FROM INTEGRATION.IC_RECON
WHERE SCENARIO = 'ACT'
  AND MATCH_STATUS <> 'MATCHED'

-- DQ09 ELIMINATIONS_BALANCED -----------------------------------------------------
UNION ALL
SELECT 'DQ09', '*GROUP', '', PERIOD_ID, LAYER, 1, SUM(AMOUNT_GC)::FLOAT,
       LAYER || ' out of balance by ' || TO_VARCHAR(SUM(AMOUNT_GC))
FROM MARTS.FCT_CONSOLIDATED_GL
WHERE SCENARIO = 'ACT'
  AND LAYER <> 'TRANSLATED'
GROUP BY LAYER, PERIOD_ID
HAVING ABS(SUM(AMOUNT_GC)) > (SELECT CHECK_TOL FROM cfg)

-- DQ10 BS_BALANCED ---------------------------------------------------------------
UNION ALL
SELECT 'DQ10', '*GROUP', '', PERIOD_ID, '', 1, CUM_GC::FLOAT,
       'Assets - liabilities - equity = ' || TO_VARCHAR(CUM_GC)
FROM bs_cumulative
WHERE ABS(CUM_GC) > (SELECT CHECK_TOL FROM cfg)

-- DQ11 CASHFLOW_RECONCILED -------------------------------------------------------
UNION ALL
SELECT 'DQ11', COMPANY_CODE, '', PERIOD_ID, 'TRANSLATED', 1, DIFF_GC::FLOAT,
       'Cash flow vs cash movement differs by ' || TO_VARCHAR(DIFF_GC)
FROM cf_difference
WHERE ABS(DIFF_GC) > (SELECT CHECK_TOL FROM cfg)
UNION ALL
SELECT 'DQ11', '*GROUP', '', PERIOD_ID, 'CONSOLIDATION', 1, SUM(AMOUNT_GC)::FLOAT,
       'Consolidation layers'' cash flow not zero: ' || TO_VARCHAR(SUM(AMOUNT_GC))
FROM MARTS.FCT_CASHFLOW
WHERE LAYER <> 'TRANSLATED'
GROUP BY PERIOD_ID
HAVING ABS(SUM(AMOUNT_GC)) > (SELECT CHECK_TOL FROM cfg)

-- DQ12 SOURCE_TO_REPORT ------------------------------------------------------------
UNION ALL
SELECT 'DQ12', ct.COMPANY_CODE, '', ct.PERIOD_ID, ct.FILE_NAME,
       ct.ROW_COUNT - COALESCE(l.ROWS_LOADED, 0), NULL::FLOAT,
       IFF(l.FILE_NAME IS NULL, 'File in manifest but not loaded',
           'Loaded ' || TO_VARCHAR(l.ROWS_LOADED) || ' rows vs ' || TO_VARCHAR(ct.ROW_COUNT) || ' in manifest')
FROM STAGING.V_CONTROL_TOTALS ct
LEFT JOIN loaded_files l
       ON l.FILE_NAME = ct.FILE_NAME
WHERE l.FILE_NAME IS NULL
   OR l.ROWS_LOADED <> ct.ROW_COUNT
   OR l.AMOUNT_LOADED <> ct.SUM_AMOUNT_LC
UNION ALL
SELECT 'DQ12', SPLIT_PART(l.FILE_NAME, '_', 2), '', LEFT(SPLIT_PART(l.FILE_NAME, '_', 3), 6), l.FILE_NAME,
       l.ROWS_LOADED, NULL::FLOAT, 'Loaded file not in manifest'
FROM loaded_files l
LEFT JOIN STAGING.V_CONTROL_TOTALS ct
       ON ct.FILE_NAME = l.FILE_NAME
WHERE ct.FILE_NAME IS NULL;

-- -----------------------------------------------------------------------------
-- Close status of the latest successful DQ run
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW AUDIT.V_DQ_LATEST AS
SELECT r.*
FROM AUDIT.DQ_RESULTS r
JOIN (
    SELECT RUN_ID
    FROM AUDIT.DQ_RUNS
    WHERE STATUS = 'SUCCEEDED'
    QUALIFY ROW_NUMBER() OVER (ORDER BY FINISHED_AT DESC) = 1
) l
  ON l.RUN_ID = r.RUN_ID;

CREATE OR REPLACE VIEW AUDIT.V_CLOSE_STATUS AS
WITH grid AS (
    SELECT p.PERIOD_ID, p.PERIOD_SEQ, c.COMPANY_CODE
    FROM INTEGRATION.V_PERIOD_SPINE p
    CROSS JOIN (
        SELECT COMPANY_CODE FROM STAGING.V_ENTITIES
        UNION ALL
        SELECT '*GROUP'
    ) c
),
agg AS (
    SELECT
        PERIOD_ID, COMPANY_CODE,
        LISTAGG(DISTINCT IFF(SEVERITY = 'BLOCKING', RULE_ID, NULL), ',')
            WITHIN GROUP (ORDER BY IFF(SEVERITY = 'BLOCKING', RULE_ID, NULL)) AS BLOCKING_RULES,
        LISTAGG(DISTINCT IFF(SEVERITY = 'WARNING', RULE_ID, NULL), ',')
            WITHIN GROUP (ORDER BY IFF(SEVERITY = 'WARNING', RULE_ID, NULL))  AS WARNING_RULES,
        COUNT(*) AS N_FINDINGS
    FROM AUDIT.V_DQ_LATEST
    GROUP BY PERIOD_ID, COMPANY_CODE
),
joined AS (
    SELECT g.PERIOD_ID, g.PERIOD_SEQ, g.COMPANY_CODE,
           COALESCE(a.BLOCKING_RULES, '') AS BLOCKING_RULES,
           COALESCE(a.WARNING_RULES, '')  AS WARNING_RULES,
           COALESCE(a.N_FINDINGS, 0)      AS N_FINDINGS
    FROM grid g
    LEFT JOIN agg a
           ON a.PERIOD_ID = g.PERIOD_ID
          AND a.COMPANY_CODE = g.COMPANY_CODE
)
SELECT
    PERIOD_ID, PERIOD_SEQ, COMPANY_CODE, BLOCKING_RULES, WARNING_RULES, N_FINDINGS,
    CASE
        WHEN BLOCKING_RULES <> ''
          OR (COMPANY_CODE = '*GROUP'
              AND MAX(IFF(COMPANY_CODE <> '*GROUP' AND BLOCKING_RULES <> '', 1, 0)) OVER (PARTITION BY PERIOD_ID) = 1)
            THEN 'BLOCKED'
        WHEN WARNING_RULES <> ''
          OR (COMPANY_CODE = '*GROUP'
              AND MAX(IFF(COMPANY_CODE <> '*GROUP' AND WARNING_RULES <> '', 1, 0)) OVER (PARTITION BY PERIOD_ID) = 1)
            THEN 'WARNING'
        ELSE 'CLEAN'
    END AS CLOSE_STATUS
FROM joined;

-- -----------------------------------------------------------------------------
-- DQ procedure
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE AUDIT.SP_RUN_DQ()
    RETURNS VARCHAR
    LANGUAGE SQL
    EXECUTE AS CALLER
    COMMENT = 'Evaluate the 12 data-quality rules and store the findings'
AS
$$
DECLARE
    run_id            VARCHAR DEFAULT UUID_STRING();
    n_fail            INTEGER DEFAULT 0;
    n_block           INTEGER DEFAULT 0;
    n_periods_blocked INTEGER DEFAULT 0;
BEGIN
    INSERT INTO AUDIT.DQ_RUNS (RUN_ID, STARTED_AT, STATUS)
    VALUES (:run_id, CURRENT_TIMESTAMP(), 'RUNNING');

    INSERT INTO AUDIT.DQ_RESULTS (RUN_ID, RULE_ID, RULE_NAME, SEVERITY, COMPANY_CODE, PARTNER_CODE,
                                  PERIOD_ID, OBJECT_NAME, FAILING_ROWS, METRIC_VALUE, DETAILS, CHECKED_AT)
    SELECT :run_id, c.RULE_ID, r.RULE_NAME, r.SEVERITY, c.COMPANY_CODE, c.PARTNER_CODE,
           c.PERIOD_ID, c.OBJECT_NAME, c.FAILING_ROWS, c.METRIC_VALUE, c.DETAILS, CURRENT_TIMESTAMP()
    FROM AUDIT.V_DQ_CHECKS c
    JOIN STAGING.V_DQ_RULES r
      ON r.RULE_ID = c.RULE_ID;

    SELECT COUNT(*), COUNT_IF(SEVERITY = 'BLOCKING')
      INTO :n_fail, :n_block
      FROM AUDIT.DQ_RESULTS
     WHERE RUN_ID = :run_id;

    UPDATE AUDIT.DQ_RUNS
       SET FINISHED_AT = CURRENT_TIMESTAMP(), STATUS = 'SUCCEEDED',
           N_FAILURES = :n_fail, N_BLOCKING = :n_block
     WHERE RUN_ID = :run_id;

    SELECT COUNT(*) INTO :n_periods_blocked
      FROM AUDIT.V_CLOSE_STATUS
     WHERE COMPANY_CODE = '*GROUP' AND CLOSE_STATUS = 'BLOCKED';

    UPDATE AUDIT.DQ_RUNS SET PERIODS_BLOCKED = :n_periods_blocked WHERE RUN_ID = :run_id;

    RETURN 'DQ run ' || run_id || ': ' || n_fail || ' findings (' || n_block || ' blocking), '
           || n_periods_blocked || ' periods blocked';
EXCEPTION
    WHEN OTHER THEN
        UPDATE AUDIT.DQ_RUNS SET FINISHED_AT = CURRENT_TIMESTAMP(), STATUS = 'FAILED' WHERE RUN_ID = :run_id;
        RAISE;
END;
$$;

-- -----------------------------------------------------------------------------
-- Reporting views in MARTS (readers do not need access to AUDIT)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW MARTS.V_CLOSE_STATUS AS
SELECT * FROM AUDIT.V_CLOSE_STATUS;

CREATE OR REPLACE VIEW MARTS.V_DQ_RESULTS AS
SELECT RULE_ID, RULE_NAME, SEVERITY, COMPANY_CODE, PARTNER_CODE, PERIOD_ID, OBJECT_NAME,
       FAILING_ROWS, METRIC_VALUE, DETAILS, CHECKED_AT
FROM AUDIT.V_DQ_LATEST;
