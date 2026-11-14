/* =============================================================================
   30_integration / 01  Configuration seeds, account master, period spine, FX
   Mirrors: orchestration/reference_engine.py  constants, accounts(), period_spine(), fx_filled()
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA INTEGRATION;

-- -----------------------------------------------------------------------------
-- Platform parameters
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE INTEGRATION.CONFIG (
    CONFIG_KEY   VARCHAR,
    VALUE_NUM    NUMBER(38, 6),
    DESCRIPTION  VARCHAR
);
INSERT INTO INTEGRATION.CONFIG (CONFIG_KEY, VALUE_NUM, DESCRIPTION) VALUES
    ('FIRST_YEAR',        2023,     'First fiscal year; period 0 of this year holds the opening balances'),
    ('N_PERIODS',         36,       'Number of monthly periods after the opening period'),
    ('IC_TOLERANCE_ABS',  1000,     'IC pairs match if |difference| <= max(this EUR amount, pct x larger side)'),
    ('IC_TOLERANCE_PCT',  0.005,    'Relative IC tolerance'),
    ('CHECK_TOLERANCE',   1,        'EUR rounding tolerance for DQ09 / DQ10 / DQ11'),
    ('FX_JUMP_THRESHOLD', 0.10,     'DQ04: maximum month-on-month rate change');

-- -----------------------------------------------------------------------------
-- Accounts used only by the consolidation engine (in addition to the 120 group accounts)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE INTEGRATION.CONSOLIDATION_ACCOUNTS (
    GROUP_ACCOUNT VARCHAR, ACCOUNT_NAME VARCHAR, STATEMENT VARCHAR, ACCOUNT_CLASS VARCHAR,
    FS_GROUP VARCHAR, FS_LINE VARCHAR, TRANSLATION_METHOD VARCHAR, EBITDA_FLAG BOOLEAN,
    CASHFLOW_LINE VARCHAR, SORT_ORDER NUMBER
);
INSERT INTO INTEGRATION.CONSOLIDATION_ACCOUNTS VALUES
    ('9100', 'Intercompany differences - P&L', 'PL', 'EXPENSE', 'Operating expenses', 'Intercompany differences', 'AVG', TRUE, 'NET_INCOME', 9100),
    ('9110', 'Intercompany differences - balance sheet suspense', 'BS', 'ASSET', 'Current assets', 'Other current assets', 'CLOSING', FALSE, 'OP_OTHER', 9110),
    ('9200', 'Non-recurring items (management adjustments)', 'PL', 'EXPENSE', 'Non-recurring items', 'Non-recurring items', 'AVG', FALSE, 'NET_INCOME', 9200),
    ('9300', 'Profit attributable to non-controlling interests', 'PL', 'EXPENSE', 'Profit attribution', 'Non-controlling interests', 'AVG', FALSE, 'NET_INCOME', 9300),
    ('9900', 'Data-quality suspense (unmapped / unbalanced)', 'BS', 'ASSET', 'Current assets', 'Suspense', 'CLOSING', FALSE, 'OP_OTHER', 9900);

-- -----------------------------------------------------------------------------
-- Intercompany pair definitions
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE INTEGRATION.IC_PAIRS (
    PAIR_TYPE VARCHAR, SIDE_A_ACCOUNT VARCHAR, SIDE_B_ACCOUNT VARCHAR, BASIS VARCHAR, DESCRIPTION VARCHAR
);
INSERT INTO INTEGRATION.IC_PAIRS VALUES
    ('SALES_COGS', '4100', '5010', 'PL', 'IC sales of goods vs IC purchases'),
    ('MGMT_FEE',   '4300', '6800', 'PL', 'Management fee revenue vs expense'),
    ('INTEREST',   '7010', '7210', 'PL', 'IC interest income vs expense'),
    ('DIVIDEND',   '7100', '3300', 'PL', 'Dividend income vs dividends distributed (transaction-rate equity)'),
    ('AR_AP',      '1190', '2090', 'BS', 'IC receivables vs IC payables (cumulative balances)'),
    ('LOAN',       '1400', '2200', 'BS', 'IC loans receivable vs payable (cumulative balances)');

-- -----------------------------------------------------------------------------
-- Consolidation layers
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE INTEGRATION.LAYERS (
    LAYER VARCHAR, LAYER_ORDER NUMBER, LAYER_NAME VARCHAR, IN_STATUTORY BOOLEAN, IN_MANAGEMENT BOOLEAN
);
INSERT INTO INTEGRATION.LAYERS VALUES
    ('TRANSLATED', 1, 'Entity books translated to EUR (incl. CTA)', TRUE, TRUE),
    ('IC_ELIM',    2, 'Intercompany eliminations',                  TRUE, TRUE),
    ('CONSOL_ADJ', 3, 'UPI, investment/equity elimination, NCI',     TRUE, TRUE),
    ('MGMT_ADJ',   4, 'Management adjustments (non-recurring)',      FALSE, TRUE);

-- -----------------------------------------------------------------------------
-- Account master = 120 group accounts + consolidation accounts
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_ACCOUNTS AS
SELECT GROUP_ACCOUNT, ACCOUNT_NAME, STATEMENT, ACCOUNT_CLASS, FS_GROUP, FS_LINE, PRESENTATION_SIGN,
       TRANSLATION_METHOD, EBITDA_FLAG, CASHFLOW_LINE, IS_INTERCOMPANY, IC_PAIR_TYPE, IC_SIDE,
       CONSOLIDATION_ONLY, SORT_ORDER, 'GROUP_COA' AS ACCOUNT_SOURCE
FROM STAGING.V_GROUP_ACCOUNTS
UNION ALL
SELECT GROUP_ACCOUNT, ACCOUNT_NAME, STATEMENT, ACCOUNT_CLASS, FS_GROUP, FS_LINE,
       IFF(ACCOUNT_CLASS = 'ASSET', 1, -1), TRANSLATION_METHOD, EBITDA_FLAG, CASHFLOW_LINE,
       FALSE, '', '', TRUE, SORT_ORDER, 'CONSOLIDATION'
FROM INTEGRATION.CONSOLIDATION_ACCOUNTS;

-- -----------------------------------------------------------------------------
-- Period spine: period 0 (opening balances) + N monthly periods
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_PERIOD_SPINE AS
WITH cfg AS (
    SELECT
        MAX(IFF(CONFIG_KEY = 'FIRST_YEAR', VALUE_NUM, NULL))::INT AS FIRST_YEAR,
        MAX(IFF(CONFIG_KEY = 'N_PERIODS',  VALUE_NUM, NULL))::INT AS N_PERIODS
    FROM INTEGRATION.CONFIG
),
seq AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS PERIOD_SEQ
    FROM TABLE(GENERATOR(ROWCOUNT => 400))
),
base AS (
    SELECT
        s.PERIOD_SEQ,
        IFF(s.PERIOD_SEQ = 0, c.FIRST_YEAR, c.FIRST_YEAR + FLOOR((s.PERIOD_SEQ - 1) / 12))::INT AS FISCAL_YEAR,
        IFF(s.PERIOD_SEQ = 0, 0, MOD(s.PERIOD_SEQ - 1, 12) + 1)::INT                          AS FISCAL_PERIOD,
        c.FIRST_YEAR
    FROM seq s
    CROSS JOIN cfg c
    WHERE s.PERIOD_SEQ <= c.N_PERIODS
)
SELECT
    PERIOD_SEQ,
    TO_VARCHAR(FISCAL_YEAR * 100 + FISCAL_PERIOD)                                    AS PERIOD_ID,
    FISCAL_YEAR,
    FISCAL_PERIOD,
    IFF(PERIOD_SEQ = 0, 0, CEIL(FISCAL_PERIOD / 3))::INT                            AS FISCAL_QUARTER,
    IFF(PERIOD_SEQ = 0, TO_VARCHAR((FIRST_YEAR - 1) * 100 + 12),
        TO_VARCHAR(FISCAL_YEAR * 100 + FISCAL_PERIOD))                               AS FX_PERIOD,
    IFF(PERIOD_SEQ = 0, DATE_FROM_PARTS(FIRST_YEAR - 1, 12, 31),
        LAST_DAY(DATE_FROM_PARTS(FISCAL_YEAR, FISCAL_PERIOD, 1)))                    AS PERIOD_END_DATE,
    PERIOD_SEQ = 0                                                                   AS IS_OPENING
FROM base;

-- -----------------------------------------------------------------------------
-- FX rates on a dense currency x FX-period spine; gaps use the last known rate
-- (rate_status = 'FALLBACK' is reported by rule DQ03)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_FX_RATES_FILLED AS
WITH ccy AS (
    SELECT DISTINCT LOCAL_CURRENCY AS CURRENCY FROM STAGING.V_ENTITIES
),
spine AS (
    SELECT c.CURRENCY, p.FX_PERIOD
    FROM ccy c
    CROSS JOIN INTEGRATION.V_PERIOD_SPINE p
)
SELECT
    s.CURRENCY,
    s.FX_PERIOD,
    LAST_VALUE(f.AVG_RATE) IGNORE NULLS OVER (
        PARTITION BY s.CURRENCY ORDER BY s.FX_PERIOD
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)                 AS AVG_RATE,
    LAST_VALUE(f.CLOSING_RATE) IGNORE NULLS OVER (
        PARTITION BY s.CURRENCY ORDER BY s.FX_PERIOD
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)                 AS CLOSING_RATE,
    IFF(f.AVG_RATE IS NULL OR f.CLOSING_RATE IS NULL, 'FALLBACK', 'ACTUAL') AS RATE_STATUS
FROM spine s
LEFT JOIN STAGING.V_FX_RATES f
       ON f.CURRENCY = s.CURRENCY
      AND f.PERIOD_ID = s.FX_PERIOD;

-- Materialised (refreshed by MARTS.SP_REFRESH); created empty here so dependent views compile
CREATE OR REPLACE TABLE INTEGRATION.FX_RATES_FILLED AS
SELECT * FROM INTEGRATION.V_FX_RATES_FILLED LIMIT 0;
