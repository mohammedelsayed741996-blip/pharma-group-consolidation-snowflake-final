/* =============================================================================
   30_integration / 04  Consolidation adjustments and management adjustments
   Mirrors: orchestration/reference_engine.py  consol_adj(), mgmt_adj()

   CONSOL_ADJ (statutory and management view)
     UPI          unrealised profit in inventory: buyer's closing balance of 1340 (goods from
                  the group factory) x seller margin on sales. Dr 5090 / Cr 1340 for the change
                  of the period; at the opening period the counter-entry is retained earnings.
     INVESTMENT   parent's investment (1800) against the subsidiary's share capital and capital
                  reserve at acquisition; difference = goodwill (1700); minority share -> NCI (3950)
     NCI_*        non-controlling interests (PL01, 80 % owned): share of opening retained
                  earnings, of each period's profit (9300), of CTA and the minority dividend
   MGMT_ADJ (management view only)
     Group Controlling reclassifies non-recurring items to 9200 (excluded from adjusted EBITDA)
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA INTEGRATION;

CREATE OR REPLACE VIEW INTEGRATION.V_CONSOL_ADJ AS
WITH spine AS (
    SELECT PERIOD_SEQ, PERIOD_ID FROM INTEGRATION.V_PERIOD_SPINE
),
opening AS (
    SELECT PERIOD_ID FROM INTEGRATION.V_PERIOD_SPINE WHERE PERIOD_SEQ = 0
),
-- ---------------- Unrealised profit in inventory ----------------
upi_base AS (
    SELECT b.COMPANY_CODE AS BUYER, b.TRADING_PARTNER AS SELLER, b.PERIOD_SEQ, b.PERIOD_ID,
           SUM(b.BAL_GC) AS BAL_GC
    FROM INTEGRATION.GL_BS_BALANCES b
    WHERE b.GROUP_ACCOUNT = '1340'
    GROUP BY b.COMPANY_CODE, b.TRADING_PARTNER, b.PERIOD_SEQ, b.PERIOD_ID
),
upi_bal AS (
    SELECT u.BUYER, u.SELLER, u.PERIOD_SEQ, u.PERIOD_ID,
           -ROUND(u.BAL_GC * tp.SELLER_MARGIN_ON_SALES_PCT, 2) AS UPI_BAL
    FROM upi_base u
    JOIN STAGING.V_IC_TRANSFER_PRICING tp
      ON tp.SELLER_COMPANY = u.SELLER
     AND tp.FLOW_TYPE = 'GOODS'
),
upi_mov AS (
    SELECT BUYER, SELLER, PERIOD_SEQ, PERIOD_ID,
           UPI_BAL - LAG(UPI_BAL, 1, 0) OVER (PARTITION BY BUYER, SELLER ORDER BY PERIOD_SEQ) AS MOV
    FROM upi_bal
),
upi AS (
    SELECT BUYER AS COMPANY_CODE, PERIOD_ID, '1340' AS GROUP_ACCOUNT, SELLER AS TRADING_PARTNER,
           MOV AS AMOUNT_GC, 'UPI' AS SOURCE_RULE
    FROM upi_mov
    UNION ALL
    SELECT BUYER, PERIOD_ID, IFF(PERIOD_SEQ = 0, '3200', '5090'), SELLER, -MOV, 'UPI'
    FROM upi_mov
),
-- ---------------- Investment / equity elimination and NCI at acquisition ----------------
open_bal AS (
    SELECT COMPANY_CODE, GROUP_ACCOUNT, TRADING_PARTNER, SUM(BAL_GC) AS BAL_GC
    FROM INTEGRATION.GL_BS_BALANCES
    WHERE PERIOD_SEQ = 0
    GROUP BY COMPANY_CODE, GROUP_ACCOUNT, TRADING_PARTNER
),
subs AS (
    SELECT
        e.COMPANY_CODE AS SUB, e.PARENT_COMPANY AS PARENT, e.OWNERSHIP_PCT AS OWN,
        COALESCE(inv.BAL_GC, 0) AS INV,
        COALESCE(sc.BAL_GC, 0)  AS SC,
        COALESCE(cr.BAL_GC, 0)  AS CR,
        COALESCE(re.BAL_GC, 0)  AS RE,
        -(COALESCE(sc.BAL_GC, 0) + COALESCE(cr.BAL_GC, 0))           AS EQ,
        ROUND(e.OWNERSHIP_PCT * EQ, 2)                               AS OWN_EQ
    FROM STAGING.V_ENTITIES e
    LEFT JOIN open_bal inv ON inv.COMPANY_CODE = e.PARENT_COMPANY AND inv.GROUP_ACCOUNT = '1800' AND inv.TRADING_PARTNER = e.COMPANY_CODE
    LEFT JOIN open_bal sc  ON sc.COMPANY_CODE = e.COMPANY_CODE  AND sc.GROUP_ACCOUNT = '3000' AND sc.TRADING_PARTNER = ''
    LEFT JOIN open_bal cr  ON cr.COMPANY_CODE = e.COMPANY_CODE  AND cr.GROUP_ACCOUNT = '3100' AND cr.TRADING_PARTNER = ''
    LEFT JOIN open_bal re  ON re.COMPANY_CODE = e.COMPANY_CODE  AND re.GROUP_ACCOUNT = '3200' AND re.TRADING_PARTNER = ''
    WHERE e.CONSOLIDATION_METHOD = 'FULL'
),
investment AS (
    SELECT PARENT AS COMPANY_CODE, o.PERIOD_ID, '1800' AS GROUP_ACCOUNT, SUB AS TRADING_PARTNER, -INV AS AMOUNT_GC, 'INVESTMENT' AS SOURCE_RULE FROM subs CROSS JOIN opening o
    UNION ALL SELECT SUB, o.PERIOD_ID, '3000', PARENT, -SC, 'INVESTMENT' FROM subs CROSS JOIN opening o
    UNION ALL SELECT SUB, o.PERIOD_ID, '3100', PARENT, -CR, 'INVESTMENT' FROM subs CROSS JOIN opening o
    UNION ALL SELECT SUB, o.PERIOD_ID, '1700', PARENT, INV - OWN_EQ, 'INVESTMENT' FROM subs CROSS JOIN opening o
    UNION ALL SELECT SUB, o.PERIOD_ID, '3950', PARENT, -(EQ - OWN_EQ), 'INVESTMENT' FROM subs CROSS JOIN opening o
),
-- ---------------- Non-controlling interests after acquisition ----------------
nci_subs AS (
    SELECT SUB, 1 - OWN AS NCI_SHARE, RE FROM subs WHERE OWN < 1
),
nci_re AS (
    SELECT n.SUB, o.PERIOD_ID, ROUND(n.NCI_SHARE * n.RE, 2) AS X
    FROM nci_subs n CROSS JOIN opening o
),
translated_sub AS (
    SELECT g.COMPANY_CODE, g.PERIOD_ID, g.GROUP_ACCOUNT, g.TRADING_PARTNER, g.AMOUNT_GC,
           a.STATEMENT, n.NCI_SHARE
    FROM INTEGRATION.GL_TRANSLATED g
    JOIN nci_subs n ON n.SUB = g.COMPANY_CODE
    JOIN INTEGRATION.V_ACCOUNTS a ON a.GROUP_ACCOUNT = g.GROUP_ACCOUNT
),
nci_profit AS (
    SELECT COMPANY_CODE AS SUB, PERIOD_ID, ROUND(MAX(NCI_SHARE) * SUM(AMOUNT_GC), 2) AS Y
    FROM translated_sub WHERE STATEMENT = 'PL'
    GROUP BY COMPANY_CODE, PERIOD_ID
),
nci_cta AS (
    SELECT COMPANY_CODE AS SUB, PERIOD_ID, ROUND(MAX(NCI_SHARE) * SUM(AMOUNT_GC), 2) AS Z
    FROM translated_sub WHERE GROUP_ACCOUNT = '3900'
    GROUP BY COMPANY_CODE, PERIOD_ID
),
nci_div AS (
    SELECT COMPANY_CODE AS SUB, PERIOD_ID, SUM(AMOUNT_GC) AS W
    FROM translated_sub WHERE GROUP_ACCOUNT = '3300' AND TRADING_PARTNER = ''
    GROUP BY COMPANY_CODE, PERIOD_ID
),
nci AS (
    SELECT SUB AS COMPANY_CODE, PERIOD_ID, '3200' AS GROUP_ACCOUNT, '' AS TRADING_PARTNER, -X AS AMOUNT_GC, 'NCI_RE_OPENING' AS SOURCE_RULE FROM nci_re
    UNION ALL SELECT SUB, PERIOD_ID, '3950', '', X,  'NCI_RE_OPENING' FROM nci_re
    UNION ALL SELECT SUB, PERIOD_ID, '9300', '', -Y, 'NCI_PROFIT'     FROM nci_profit
    UNION ALL SELECT SUB, PERIOD_ID, '3950', '', Y,  'NCI_PROFIT'     FROM nci_profit
    UNION ALL SELECT SUB, PERIOD_ID, '3900', '', -Z, 'NCI_CTA'        FROM nci_cta
    UNION ALL SELECT SUB, PERIOD_ID, '3950', '', Z,  'NCI_CTA'        FROM nci_cta
    UNION ALL SELECT SUB, PERIOD_ID, '3300', '', -W, 'NCI_DIVIDEND'   FROM nci_div
    UNION ALL SELECT SUB, PERIOD_ID, '3950', '', W,  'NCI_DIVIDEND'   FROM nci_div
),
all_adj AS (
    SELECT * FROM upi
    UNION ALL SELECT * FROM investment
    UNION ALL SELECT * FROM nci
)
SELECT
    'ACT' AS SCENARIO, 'CONSOL_ADJ' AS LAYER, COMPANY_CODE, PERIOD_ID, GROUP_ACCOUNT, TRADING_PARTNER,
    '' AS COST_CENTER, '' AS PROFIT_CENTER,
    NULL::NUMBER(38, 2) AS AMOUNT_LC, AMOUNT_GC::NUMBER(38, 2) AS AMOUNT_GC,
    NULL::NUMBER(38, 2) AS AMOUNT_GC_CC, SOURCE_RULE
FROM all_adj
WHERE AMOUNT_GC <> 0;

-- -----------------------------------------------------------------------------
-- Management adjustments (management view only)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW INTEGRATION.V_MGMT_ADJ AS
SELECT 'ACT' AS SCENARIO, 'MGMT_ADJ' AS LAYER, COMPANY_CODE, PERIOD_ID, GROUP_ACCOUNT, '' AS TRADING_PARTNER,
       COST_CENTER, PROFIT_CENTER, NULL::NUMBER(38, 2) AS AMOUNT_LC,
       (-AMOUNT_EUR)::NUMBER(38, 2) AS AMOUNT_GC, (-AMOUNT_EUR)::NUMBER(38, 2) AS AMOUNT_GC_CC,
       ADJUSTMENT_ID AS SOURCE_RULE
FROM STAGING.V_MGMT_ADJUSTMENTS
UNION ALL
SELECT 'ACT', 'MGMT_ADJ', COMPANY_CODE, PERIOD_ID, '9200', '', COST_CENTER, PROFIT_CENTER, NULL,
       AMOUNT_EUR, AMOUNT_EUR, ADJUSTMENT_ID
FROM STAGING.V_MGMT_ADJUSTMENTS;
