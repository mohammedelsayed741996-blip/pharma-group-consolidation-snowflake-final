/* =============================================================================
   30_integration / 03  Intercompany matching and eliminations
   Mirrors: orchestration/reference_engine.py  ic_lines(), ic_recon(), ic_elim()

   * IC lines   = translated rows on IC pair accounts whose trading partner is a valid
                  group company other than the entity itself (invalid partners are
                  reported by DQ07 and are NOT eliminated)
   * Matching   = side A vs side B per period / pair / entity pair. P&L pairs compare
                  movements, balance-sheet pairs compare cumulative balances
   * Elimination= reverse both sides; the net of each pair and period is booked to the
                  IC difference account (9100 P&L, 9110 balance sheet), so every
                  elimination journal nets to zero (DQ09)
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA INTEGRATION;

CREATE OR REPLACE VIEW INTEGRATION.V_IC_LINES AS
WITH src AS (
    SELECT SCENARIO, COMPANY_CODE, PERIOD_ID, GROUP_ACCOUNT, TRADING_PARTNER, COST_CENTER,
           PROFIT_CENTER, AMOUNT_GC, AMOUNT_GC_CC
    FROM INTEGRATION.GL_TRANSLATED
    UNION ALL
    SELECT SCENARIO, COMPANY_CODE, PERIOD_ID, GROUP_ACCOUNT, TRADING_PARTNER, COST_CENTER,
           PROFIT_CENTER, AMOUNT_GC, AMOUNT_GC_CC
    FROM INTEGRATION.V_BUDGET_TRANSLATED
),
pair_accounts AS (
    SELECT PAIR_TYPE, SIDE_A_ACCOUNT AS GROUP_ACCOUNT, 'A' AS SIDE, BASIS FROM INTEGRATION.IC_PAIRS
    UNION ALL
    SELECT PAIR_TYPE, SIDE_B_ACCOUNT AS GROUP_ACCOUNT, 'B' AS SIDE, BASIS FROM INTEGRATION.IC_PAIRS
)
SELECT
    s.*,
    pa.PAIR_TYPE,
    pa.SIDE,
    pa.BASIS,
    IFF(pa.SIDE = 'A', s.COMPANY_CODE, s.TRADING_PARTNER) AS ENTITY_A,
    IFF(pa.SIDE = 'A', s.TRADING_PARTNER, s.COMPANY_CODE) AS ENTITY_B
FROM src s
JOIN pair_accounts pa
  ON pa.GROUP_ACCOUNT = s.GROUP_ACCOUNT
JOIN STAGING.V_ENTITIES pe                     -- partner must be an existing group company
  ON pe.COMPANY_CODE = s.TRADING_PARTNER
WHERE s.TRADING_PARTNER <> s.COMPANY_CODE;

-- -----------------------------------------------------------------------------
-- Reconciliation (feeds the IC cockpit and rule DQ08)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_IC_RECON AS
WITH cfg AS (
    SELECT
        MAX(IFF(CONFIG_KEY = 'IC_TOLERANCE_ABS', VALUE_NUM, NULL)) AS TOL_ABS,
        MAX(IFF(CONFIG_KEY = 'IC_TOLERANCE_PCT', VALUE_NUM, NULL)) AS TOL_PCT
    FROM INTEGRATION.CONFIG
),
agg AS (
    SELECT SCENARIO, PAIR_TYPE, BASIS, ENTITY_A, ENTITY_B, PERIOD_ID,
           SUM(IFF(SIDE = 'A', AMOUNT_GC, 0)) AS A_GC,
           SUM(IFF(SIDE = 'B', AMOUNT_GC, 0)) AS B_GC
    FROM INTEGRATION.V_IC_LINES
    GROUP BY SCENARIO, PAIR_TYPE, BASIS, ENTITY_A, ENTITY_B, PERIOD_ID
),
bs_keys AS (
    SELECT DISTINCT SCENARIO, PAIR_TYPE, BASIS, ENTITY_A, ENTITY_B
    FROM agg
    WHERE BASIS = 'BS'
),
bs AS (
    SELECT
        k.SCENARIO, k.PAIR_TYPE, k.ENTITY_A, k.ENTITY_B, p.PERIOD_ID,
        SUM(COALESCE(a.A_GC, 0)) OVER (
            PARTITION BY k.SCENARIO, k.PAIR_TYPE, k.ENTITY_A, k.ENTITY_B ORDER BY p.PERIOD_SEQ
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS A_GC,
        SUM(COALESCE(a.B_GC, 0)) OVER (
            PARTITION BY k.SCENARIO, k.PAIR_TYPE, k.ENTITY_A, k.ENTITY_B ORDER BY p.PERIOD_SEQ
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS B_GC
    FROM bs_keys k
    CROSS JOIN INTEGRATION.V_PERIOD_SPINE p
    LEFT JOIN agg a
           ON a.SCENARIO = k.SCENARIO AND a.PAIR_TYPE = k.PAIR_TYPE
          AND a.ENTITY_A = k.ENTITY_A AND a.ENTITY_B = k.ENTITY_B
          AND a.PERIOD_ID = p.PERIOD_ID
),
all_rows AS (
    SELECT SCENARIO, PERIOD_ID, PAIR_TYPE, ENTITY_A, ENTITY_B, A_GC, B_GC FROM agg WHERE BASIS = 'PL'
    UNION ALL
    SELECT SCENARIO, PERIOD_ID, PAIR_TYPE, ENTITY_A, ENTITY_B, A_GC, B_GC FROM bs WHERE A_GC <> 0 OR B_GC <> 0
)
SELECT
    r.SCENARIO, r.PERIOD_ID, r.PAIR_TYPE, r.ENTITY_A, r.ENTITY_B,
    r.A_GC                                   AS SIDE_A_GC,
    r.B_GC                                   AS SIDE_B_GC,
    r.A_GC + r.B_GC                          AS DIFFERENCE_GC,
    GREATEST(c.TOL_ABS, c.TOL_PCT * GREATEST(ABS(r.A_GC), ABS(r.B_GC))) AS TOLERANCE_GC,
    CASE
        WHEN r.A_GC = 0 OR r.B_GC = 0                 THEN 'ONE_SIDED'
        WHEN ABS(r.A_GC + r.B_GC) <= TOLERANCE_GC     THEN 'MATCHED'
        ELSE 'MISMATCH'
    END                                      AS MATCH_STATUS
FROM all_rows r
CROSS JOIN cfg c;

CREATE OR REPLACE TABLE INTEGRATION.IC_RECON AS
SELECT * FROM INTEGRATION.V_IC_RECON LIMIT 0;

-- -----------------------------------------------------------------------------
-- Elimination journal (layer IC_ELIM, both scenarios)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_IC_ELIM AS
SELECT
    SCENARIO, 'IC_ELIM' AS LAYER, COMPANY_CODE, PERIOD_ID, GROUP_ACCOUNT, TRADING_PARTNER,
    COST_CENTER, PROFIT_CENTER,
    NULL::NUMBER(38, 2)                    AS AMOUNT_LC,
    (-AMOUNT_GC)::NUMBER(38, 2)            AS AMOUNT_GC,
    (-AMOUNT_GC_CC)::NUMBER(38, 2)         AS AMOUNT_GC_CC,
    'IC_' || PAIR_TYPE                     AS SOURCE_RULE
FROM INTEGRATION.V_IC_LINES
UNION ALL
SELECT
    SCENARIO, 'IC_ELIM', ENTITY_A, PERIOD_ID, IFF(BASIS = 'BS', '9110', '9100'), ENTITY_B, '', '',
    NULL, SUM(AMOUNT_GC), SUM(AMOUNT_GC_CC), 'IC_DIFFERENCE'
FROM INTEGRATION.V_IC_LINES
GROUP BY SCENARIO, ENTITY_A, ENTITY_B, PERIOD_ID, PAIR_TYPE, BASIS
HAVING SUM(AMOUNT_GC) <> 0;
