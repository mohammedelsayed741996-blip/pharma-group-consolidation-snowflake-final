/* =============================================================================
   10_raw / 01  Landing tables
   Every column is VARCHAR: RAW stores values exactly as delivered. Typing happens
   in STAGING. Metadata columns record the stage path, row number and load.
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA RAW;

-- Legal entities (SAP company codes)
CREATE TABLE IF NOT EXISTS RAW.ENTITIES (
    COMPANY_CODE                 VARCHAR,
    COMPANY_NAME                 VARCHAR,
    COUNTRY_CODE                 VARCHAR,
    COUNTRY_NAME                 VARCHAR,
    CITY                         VARCHAR,
    REGION                       VARCHAR,
    LOCAL_CURRENCY               VARCHAR,
    ENTITY_TYPE                  VARCHAR,
    PARENT_COMPANY               VARCHAR,
    OWNERSHIP_PCT                VARCHAR,
    CONSOLIDATION_METHOD         VARCHAR,
    ACQUISITION_DATE             VARCHAR,
    GOODWILL_EUR                 VARCHAR,
    LOCAL_CHART_OF_ACCOUNTS      VARCHAR,
    SOURCE_SYSTEM                VARCHAR,
    STATUTORY_TAX_RATE           VARCHAR,
    IS_ACTIVE                    VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Legal entities (SAP company codes)';

-- Currencies
CREATE TABLE IF NOT EXISTS RAW.CURRENCIES (
    CURRENCY_CODE                VARCHAR,
    CURRENCY_NAME                VARCHAR,
    DECIMALS                     VARCHAR,
    IS_GROUP_CURRENCY            VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Currencies';

-- Business units / segments
CREATE TABLE IF NOT EXISTS RAW.BUSINESS_UNITS (
    BUSINESS_UNIT                VARCHAR,
    BUSINESS_UNIT_NAME           VARCHAR,
    SEGMENT                      VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Business units / segments';

-- Group chart of accounts (120)
CREATE TABLE IF NOT EXISTS RAW.GROUP_ACCOUNTS (
    GROUP_ACCOUNT                VARCHAR,
    ACCOUNT_NAME                 VARCHAR,
    STATEMENT                    VARCHAR,
    ACCOUNT_CLASS                VARCHAR,
    FS_GROUP                     VARCHAR,
    FS_LINE                      VARCHAR,
    PRESENTATION_SIGN            VARCHAR,
    TRANSLATION_METHOD           VARCHAR,
    EBITDA_FLAG                  VARCHAR,
    CASHFLOW_LINE                VARCHAR,
    IS_INTERCOMPANY              VARCHAR,
    IC_PAIR_TYPE                 VARCHAR,
    IC_SIDE                      VARCHAR,
    PARTNER_REQUIRED             VARCHAR,
    CONSOLIDATION_ONLY           VARCHAR,
    SORT_ORDER                   VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Group chart of accounts (120)';

-- Local charts of accounts mapped to group accounts
CREATE TABLE IF NOT EXISTS RAW.LOCAL_ACCOUNTS (
    CHART_OF_ACCOUNTS            VARCHAR,
    LOCAL_ACCOUNT                VARCHAR,
    LOCAL_ACCOUNT_NAME           VARCHAR,
    GROUP_ACCOUNT                VARCHAR,
    VALID_FROM                   VARCHAR,
    VALID_TO                     VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Local charts of accounts mapped to group accounts';

-- Cost centres (SCD Type 2)
CREATE TABLE IF NOT EXISTS RAW.COST_CENTRES (
    COST_CENTER                  VARCHAR,
    COMPANY_CODE                 VARCHAR,
    DEPARTMENT                   VARCHAR,
    COST_CENTER_NAME             VARCHAR,
    RESPONSIBLE_PERSON           VARCHAR,
    DEFAULT_PROFIT_CENTER        VARCHAR,
    FUNCTION                     VARCHAR,
    VALID_FROM                   VARCHAR,
    VALID_TO                     VARCHAR,
    IS_CURRENT                   VARCHAR,
    SCD_VERSION                  VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Cost centres (SCD Type 2)';

-- Profit centres
CREATE TABLE IF NOT EXISTS RAW.PROFIT_CENTRES (
    PROFIT_CENTER                VARCHAR,
    COMPANY_CODE                 VARCHAR,
    BUSINESS_UNIT                VARCHAR,
    SEGMENT                      VARCHAR,
    PROFIT_CENTER_NAME           VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Profit centres';

-- Intercompany pricing rules
CREATE TABLE IF NOT EXISTS RAW.IC_TRANSFER_PRICING (
    SELLER_COMPANY               VARCHAR,
    FLOW_TYPE                    VARCHAR,
    PRICING_METHOD               VARCHAR,
    MARKUP_PCT                   VARCHAR,
    SELLER_MARGIN_ON_SALES_PCT   VARCHAR,
    SETTLEMENT_CURRENCY          VARCHAR,
    PAYMENT_TERMS_DAYS           VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Intercompany pricing rules';

-- Data-quality rule catalogue
CREATE TABLE IF NOT EXISTS RAW.DQ_RULES (
    RULE_ID                      VARCHAR,
    RULE_NAME                    VARCHAR,
    SEVERITY                     VARCHAR,
    LAYER                        VARCHAR,
    DESCRIPTION                  VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Data-quality rule catalogue';

-- Monthly average and closing rates (LC per EUR)
CREATE TABLE IF NOT EXISTS RAW.FX_RATES (
    CURRENCY                     VARCHAR,
    PERIOD_ID                    VARCHAR,
    AVG_RATE                     VARCHAR,
    CLOSING_RATE                 VARCHAR,
    RATE_SOURCE                  VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Monthly average and closing rates (LC per EUR)';

-- Budget rates
CREATE TABLE IF NOT EXISTS RAW.FX_BUDGET (
    CURRENCY                     VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    BUDGET_RATE                  VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Budget rates';

-- Historical rates for equity
CREATE TABLE IF NOT EXISTS RAW.FX_HISTORICAL (
    COMPANY_CODE                 VARCHAR,
    GROUP_ACCOUNT                VARCHAR,
    CURRENCY                     VARCHAR,
    HISTORICAL_RATE              VARCHAR,
    RATE_BASIS                   VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Historical rates for equity';

-- Intercompany sub-ledger
CREATE TABLE IF NOT EXISTS RAW.IC_TRANSACTIONS (
    IC_DOCUMENT_ID               VARCHAR,
    FLOW_TYPE                    VARCHAR,
    SELLER_COMPANY               VARCHAR,
    BUYER_COMPANY                VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    FISCAL_PERIOD                VARCHAR,
    DOCUMENT_DATE                VARCHAR,
    SELLER_GROUP_ACCOUNT         VARCHAR,
    BUYER_GROUP_ACCOUNT          VARCHAR,
    BUSINESS_UNIT                VARCHAR,
    DOCUMENT_CURRENCY            VARCHAR,
    AMOUNT_DOC                   VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Intercompany sub-ledger';

-- P&L budget on group accounts
CREATE TABLE IF NOT EXISTS RAW.BUDGET (
    VERSION                      VARCHAR,
    COMPANY_CODE                 VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    FISCAL_PERIOD                VARCHAR,
    GROUP_ACCOUNT                VARCHAR,
    TRADING_PARTNER              VARCHAR,
    COST_CENTER                  VARCHAR,
    PROFIT_CENTER                VARCHAR,
    CURRENCY                     VARCHAR,
    AMOUNT_LC                    VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'P&L budget on group accounts';

-- Source manifest (control totals per file)
CREATE TABLE IF NOT EXISTS RAW.CONTROL_TOTALS (
    FILE_NAME                    VARCHAR,
    COMPANY_CODE                 VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    FISCAL_PERIOD                VARCHAR,
    ROW_COUNT                    VARCHAR,
    SUM_AMOUNT_LC                VARCHAR,
    SUM_ABS_AMOUNT_LC            VARCHAR,
    DELIVERY_STATUS              VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Source manifest (control totals per file)';

-- Management adjustments (non-recurring items, EUR)
CREATE TABLE IF NOT EXISTS RAW.MGMT_ADJUSTMENTS (
    ADJUSTMENT_ID                VARCHAR,
    COMPANY_CODE                 VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    FISCAL_PERIOD                VARCHAR,
    GROUP_ACCOUNT                VARCHAR,
    COST_CENTER                  VARCHAR,
    PROFIT_CENTER                VARCHAR,
    AMOUNT_EUR                   VARCHAR,
    ADJUSTMENT_TYPE              VARCHAR,
    DESCRIPTION                  VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Management adjustments (non-recurring items, EUR)';

-- Trial-balance movements, one file per entity and period
CREATE TABLE IF NOT EXISTS RAW.TB_LINES (
    COMPANY_CODE                 VARCHAR,
    FISCAL_YEAR                  VARCHAR,
    FISCAL_PERIOD                VARCHAR,
    GL_ACCOUNT                   VARCHAR,
    TRADING_PARTNER              VARCHAR,
    COST_CENTER                  VARCHAR,
    PROFIT_CENTER                VARCHAR,
    CURRENCY                     VARCHAR,
    AMOUNT_LC                    VARCHAR,
    SOURCE_SYSTEM                VARCHAR,
    EXTRACT_TIMESTAMP            VARCHAR,
    _FILE_NAME                   VARCHAR,
    _FILE_ROW_NUMBER             NUMBER(38,0),
    _LOAD_ID                     VARCHAR,
    _LOADED_AT                   TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Trial-balance movements, one file per entity and period';

-- Stream used by the task DAG (60_tasks) to detect newly delivered trial balances
CREATE STREAM IF NOT EXISTS RAW.STR_TB_LINES ON TABLE RAW.TB_LINES APPEND_ONLY = TRUE
    COMMENT = 'New trial-balance rows since the last consolidation run';
