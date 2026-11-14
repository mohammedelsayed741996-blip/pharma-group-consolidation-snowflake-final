/* =============================================================================
   30_integration / 02  Account mapping and IAS 21 currency translation
   Mirrors: orchestration/reference_engine.py  tb_mapped(), gl_translated(), budget_translated()

   Translation rules (functional currency -> EUR, rates = LC per EUR, EUR = LC / rate):
     AVG             P&L monthly movements at the month's average rate (YTD = sum of months)
     HISTORICAL_TXN  dividends distributed (3300): movement at the average rate of the month
     CLOSING         balance-sheet balances at the closing rate; movement = delta of balances
     HISTORICAL      share capital, capital reserve, migrated retained earnings at historical rate
     CTA (3900)      plug that keeps every entity's translated trial balance at zero
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA INTEGRATION;

-- -----------------------------------------------------------------------------
-- Local -> group account. Unmapped amounts and any TB imbalance are parked on the
-- visible suspense account 9900 so that nothing silently disappears (DQ01 / DQ02).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_TB_MAPPED AS
WITH tb AS (
    SELECT
        t.COMPANY_CODE, t.PERIOD_ID, t.FISCAL_YEAR, t.FISCAL_PERIOD, t.GL_ACCOUNT,
        t.TRADING_PARTNER, t.COST_CENTER, t.PROFIT_CENTER, t.CURRENCY, t.AMOUNT_LC,
        la.GROUP_ACCOUNT AS MAPPED_ACCOUNT
    FROM STAGING.V_TB_LINES t
    LEFT JOIN STAGING.V_ENTITIES e
           ON e.COMPANY_CODE = t.COMPANY_CODE
    LEFT JOIN INTEGRATION.V_PERIOD_SPINE p
           ON p.PERIOD_ID = t.PERIOD_ID
    LEFT JOIN STAGING.V_LOCAL_ACCOUNTS la
           ON la.CHART_OF_ACCOUNTS = e.LOCAL_CHART_OF_ACCOUNTS
          AND la.LOCAL_ACCOUNT = t.GL_ACCOUNT
          AND p.PERIOD_END_DATE BETWEEN la.VALID_FROM AND la.VALID_TO
),
mapped AS (
    SELECT
        COMPANY_CODE, PERIOD_ID, FISCAL_YEAR, FISCAL_PERIOD,
        COALESCE(MAPPED_ACCOUNT, '9900')                 AS GROUP_ACCOUNT,
        TRADING_PARTNER, COST_CENTER, PROFIT_CENTER, CURRENCY, AMOUNT_LC,
        IFF(MAPPED_ACCOUNT IS NULL, 'UNMAPPED', 'TB')    AS RECORD_SOURCE
    FROM tb
),
imbalance AS (
    SELECT
        COMPANY_CODE, PERIOD_ID, FISCAL_YEAR, FISCAL_PERIOD,
        '9900' AS GROUP_ACCOUNT, '' AS TRADING_PARTNER, '' AS COST_CENTER, '' AS PROFIT_CENTER,
        CURRENCY, -SUM(AMOUNT_LC) AS AMOUNT_LC, 'DQ_BALANCING' AS RECORD_SOURCE
    FROM mapped
    GROUP BY COMPANY_CODE, PERIOD_ID, FISCAL_YEAR, FISCAL_PERIOD, CURRENCY
    HAVING SUM(AMOUNT_LC) <> 0
),
unioned AS (
    SELECT * FROM mapped
    UNION ALL
    SELECT * FROM imbalance
)
SELECT u.*, a.STATEMENT, a.TRANSLATION_METHOD
FROM unioned u
JOIN INTEGRATION.V_ACCOUNTS a
  ON a.GROUP_ACCOUNT = u.GROUP_ACCOUNT;

-- -----------------------------------------------------------------------------
-- Balance-sheet balances on a dense key x period grid, translated at closing /
-- historical rate. Used for BS translation, CTA, UPI and the investment elimination.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_GL_BS_BALANCES AS
WITH mv AS (
    SELECT
        m.COMPANY_CODE, m.CURRENCY, m.GROUP_ACCOUNT, m.TRADING_PARTNER, m.COST_CENTER,
        m.PROFIT_CENTER, m.TRANSLATION_METHOD, p.PERIOD_SEQ,
        SUM(m.AMOUNT_LC) AS MOV_LC
    FROM INTEGRATION.V_TB_MAPPED m
    JOIN INTEGRATION.V_PERIOD_SPINE p
      ON p.PERIOD_ID = m.PERIOD_ID
    WHERE m.STATEMENT = 'BS'
      AND m.TRANSLATION_METHOD <> 'HISTORICAL_TXN'
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
),
keys AS (
    SELECT DISTINCT COMPANY_CODE, CURRENCY, GROUP_ACCOUNT, TRADING_PARTNER, COST_CENTER,
                    PROFIT_CENTER, TRANSLATION_METHOD
    FROM mv
),
dense AS (
    SELECT k.*, p.PERIOD_SEQ, p.PERIOD_ID, p.FX_PERIOD, COALESCE(mv.MOV_LC, 0) AS MOV_LC
    FROM keys k
    CROSS JOIN INTEGRATION.V_PERIOD_SPINE p
    LEFT JOIN mv
           ON mv.COMPANY_CODE = k.COMPANY_CODE
          AND mv.GROUP_ACCOUNT = k.GROUP_ACCOUNT
          AND mv.TRADING_PARTNER = k.TRADING_PARTNER
          AND mv.COST_CENTER = k.COST_CENTER
          AND mv.PROFIT_CENTER = k.PROFIT_CENTER
          AND mv.PERIOD_SEQ = p.PERIOD_SEQ
),
bal AS (
    SELECT
        d.*,
        SUM(d.MOV_LC) OVER (
            PARTITION BY d.COMPANY_CODE, d.GROUP_ACCOUNT, d.TRADING_PARTNER, d.COST_CENTER, d.PROFIT_CENTER
            ORDER BY d.PERIOD_SEQ
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS BAL_LC
    FROM dense d
)
SELECT
    b.COMPANY_CODE, b.CURRENCY, b.GROUP_ACCOUNT, b.TRADING_PARTNER, b.COST_CENTER, b.PROFIT_CENTER,
    b.TRANSLATION_METHOD, b.PERIOD_SEQ, b.PERIOD_ID, b.FX_PERIOD, b.MOV_LC, b.BAL_LC,
    IFF(b.TRANSLATION_METHOD = 'HISTORICAL' AND h.HISTORICAL_RATE IS NOT NULL,
        h.HISTORICAL_RATE, fx.CLOSING_RATE)                                  AS TRANSLATION_RATE,
    ROUND(b.BAL_LC / TRANSLATION_RATE, 2)                                    AS BAL_GC
FROM bal b
LEFT JOIN INTEGRATION.FX_RATES_FILLED fx
       ON fx.CURRENCY = b.CURRENCY
      AND fx.FX_PERIOD = b.FX_PERIOD
LEFT JOIN STAGING.V_FX_HISTORICAL h
       ON h.COMPANY_CODE = b.COMPANY_CODE
      AND h.GROUP_ACCOUNT = b.GROUP_ACCOUNT;

CREATE OR REPLACE TABLE INTEGRATION.GL_BS_BALANCES AS
SELECT * FROM INTEGRATION.V_GL_BS_BALANCES LIMIT 0;

-- -----------------------------------------------------------------------------
-- Translated general ledger (layer TRANSLATED, scenario ACT) as monthly movements
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_GL_TRANSLATED AS
WITH pl AS (           -- (a) P&L and transaction-dated equity at the average rate
    SELECT
        m.COMPANY_CODE, m.PERIOD_ID, p.PERIOD_SEQ, m.CURRENCY, m.GROUP_ACCOUNT, m.TRADING_PARTNER,
        m.COST_CENTER, m.PROFIT_CENTER, m.AMOUNT_LC,
        ROUND(m.AMOUNT_LC / fx.AVG_RATE, 2)                                         AS AMOUNT_GC,
        IFF(m.STATEMENT = 'PL', ROUND(m.AMOUNT_LC / b.BUDGET_RATE, 2), NULL)         AS AMOUNT_GC_CC
    FROM INTEGRATION.V_TB_MAPPED m
    JOIN INTEGRATION.V_PERIOD_SPINE p
      ON p.PERIOD_ID = m.PERIOD_ID
    LEFT JOIN INTEGRATION.FX_RATES_FILLED fx
           ON fx.CURRENCY = m.CURRENCY
          AND fx.FX_PERIOD = p.FX_PERIOD
    LEFT JOIN STAGING.V_FX_BUDGET b
           ON b.CURRENCY = m.CURRENCY
          AND b.FISCAL_YEAR = m.FISCAL_YEAR
    WHERE m.STATEMENT = 'PL'
       OR m.TRANSLATION_METHOD = 'HISTORICAL_TXN'
),
bs AS (                -- (b) balance sheet: movement = change of the translated balance
    SELECT
        COMPANY_CODE, PERIOD_ID, PERIOD_SEQ, CURRENCY, GROUP_ACCOUNT, TRADING_PARTNER,
        COST_CENTER, PROFIT_CENTER, MOV_LC AS AMOUNT_LC,
        BAL_GC - LAG(BAL_GC, 1, 0) OVER (
            PARTITION BY COMPANY_CODE, GROUP_ACCOUNT, TRADING_PARTNER, COST_CENTER, PROFIT_CENTER
            ORDER BY PERIOD_SEQ)                                                    AS AMOUNT_GC
    FROM INTEGRATION.GL_BS_BALANCES
),
ent_periods AS (
    SELECT e.COMPANY_CODE, e.LOCAL_CURRENCY AS CURRENCY, p.PERIOD_SEQ, p.PERIOD_ID
    FROM STAGING.V_ENTITIES e
    CROSS JOIN INTEGRATION.V_PERIOD_SPINE p
),
bs_tot AS (
    SELECT COMPANY_CODE, PERIOD_SEQ, SUM(BAL_GC) AS BS_GC
    FROM INTEGRATION.GL_BS_BALANCES
    GROUP BY COMPANY_CODE, PERIOD_SEQ
),
pl_mv AS (
    SELECT COMPANY_CODE, PERIOD_SEQ, SUM(AMOUNT_GC) AS PL_GC
    FROM pl
    GROUP BY COMPANY_CODE, PERIOD_SEQ
),
tot AS (               -- translated TB total = BS balances + cumulative P&L (all years)
    SELECT
        ep.COMPANY_CODE, ep.CURRENCY, ep.PERIOD_SEQ, ep.PERIOD_ID,
        COALESCE(bt.BS_GC, 0)
          + SUM(COALESCE(pm.PL_GC, 0)) OVER (
                PARTITION BY ep.COMPANY_CODE ORDER BY ep.PERIOD_SEQ
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)                   AS TOT_GC
    FROM ent_periods ep
    LEFT JOIN bs_tot bt ON bt.COMPANY_CODE = ep.COMPANY_CODE AND bt.PERIOD_SEQ = ep.PERIOD_SEQ
    LEFT JOIN pl_mv  pm ON pm.COMPANY_CODE = ep.COMPANY_CODE AND pm.PERIOD_SEQ = ep.PERIOD_SEQ
),
cta AS (               -- (c) CTA balance = -TOT; posted as movement
    SELECT
        COMPANY_CODE, PERIOD_ID, PERIOD_SEQ, CURRENCY,
        -TOT_GC - LAG(-TOT_GC, 1, 0) OVER (PARTITION BY COMPANY_CODE ORDER BY PERIOD_SEQ) AS AMOUNT_GC
    FROM tot
)
SELECT 'ACT' AS SCENARIO, 'TRANSLATED' AS LAYER, COMPANY_CODE, PERIOD_ID, CURRENCY, GROUP_ACCOUNT,
       TRADING_PARTNER, COST_CENTER, PROFIT_CENTER,
       AMOUNT_LC::NUMBER(38, 2) AS AMOUNT_LC, AMOUNT_GC::NUMBER(38, 2) AS AMOUNT_GC,
       AMOUNT_GC_CC::NUMBER(38, 2) AS AMOUNT_GC_CC, 'TRANSLATION' AS SOURCE_RULE
FROM pl
UNION ALL
SELECT 'ACT', 'TRANSLATED', COMPANY_CODE, PERIOD_ID, CURRENCY, GROUP_ACCOUNT, TRADING_PARTNER,
       COST_CENTER, PROFIT_CENTER, AMOUNT_LC, AMOUNT_GC, NULL, 'TRANSLATION'
FROM bs
WHERE AMOUNT_LC <> 0 OR AMOUNT_GC <> 0
UNION ALL
SELECT 'ACT', 'TRANSLATED', COMPANY_CODE, PERIOD_ID, CURRENCY, '3900', '', '', '', 0, AMOUNT_GC, NULL, 'CTA'
FROM cta
WHERE AMOUNT_GC <> 0;

CREATE OR REPLACE TABLE INTEGRATION.GL_TRANSLATED AS
SELECT * FROM INTEGRATION.V_GL_TRANSLATED LIMIT 0;

-- -----------------------------------------------------------------------------
-- Budget at budget rates (scenario BUD). Constant currency = group currency here.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_BUDGET_TRANSLATED AS
SELECT
    'BUD'                                              AS SCENARIO,
    'TRANSLATED'                                       AS LAYER,
    b.COMPANY_CODE, b.PERIOD_ID, b.CURRENCY, b.GROUP_ACCOUNT, b.TRADING_PARTNER,
    b.COST_CENTER, b.PROFIT_CENTER,
    b.AMOUNT_LC::NUMBER(38, 2)                         AS AMOUNT_LC,
    ROUND(b.AMOUNT_LC / r.BUDGET_RATE, 2)::NUMBER(38, 2) AS AMOUNT_GC,
    ROUND(b.AMOUNT_LC / r.BUDGET_RATE, 2)::NUMBER(38, 2) AS AMOUNT_GC_CC,
    'BUDGET'                                           AS SOURCE_RULE
FROM STAGING.V_BUDGET b
JOIN STAGING.V_FX_BUDGET r
  ON r.CURRENCY = b.CURRENCY
 AND r.FISCAL_YEAR = b.FISCAL_YEAR;
