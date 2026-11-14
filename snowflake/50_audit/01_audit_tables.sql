/* =============================================================================
   50_audit / 01  Audit tables (history is kept; never truncated by the pipeline)
   Run as: FIN_ENGINEER (part of the "setup" step, before the first load)
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA AUDIT;

CREATE TABLE IF NOT EXISTS AUDIT.LOAD_LOG (
    LOAD_ID       VARCHAR,
    TARGET_TABLE  VARCHAR,
    FILE_NAME     VARCHAR,
    STATUS        VARCHAR,
    ROWS_PARSED   NUMBER,
    ROWS_LOADED   NUMBER,
    ERRORS_SEEN   NUMBER,
    FIRST_ERROR   VARCHAR,
    LOADED_AT     TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'One row per file per COPY INTO, written by run_pipeline.py';

CREATE TABLE IF NOT EXISTS AUDIT.DQ_RUNS (
    RUN_ID           VARCHAR,
    STARTED_AT       TIMESTAMP_LTZ,
    FINISHED_AT      TIMESTAMP_LTZ,
    STATUS           VARCHAR,
    N_FAILURES       NUMBER,
    N_BLOCKING       NUMBER,
    PERIODS_BLOCKED  NUMBER
)
COMMENT = 'One row per execution of AUDIT.SP_RUN_DQ()';

CREATE TABLE IF NOT EXISTS AUDIT.DQ_RESULTS (
    RUN_ID        VARCHAR,
    RULE_ID       VARCHAR,
    RULE_NAME     VARCHAR,
    SEVERITY      VARCHAR,
    COMPANY_CODE  VARCHAR,
    PARTNER_CODE  VARCHAR,
    PERIOD_ID     VARCHAR,
    OBJECT_NAME   VARCHAR,
    FAILING_ROWS  NUMBER,
    METRIC_VALUE  FLOAT,
    DETAILS       VARCHAR,
    CHECKED_AT    TIMESTAMP_LTZ
)
COMMENT = 'Every rule failure of every DQ run (only failures are stored)';

CREATE TABLE IF NOT EXISTS AUDIT.PIPELINE_RUNS (
    RUN_TS        TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    TRIGGERED_BY  VARCHAR,
    NEW_TB_ROWS   NUMBER,
    NEW_TB_FILES  NUMBER,
    MESSAGE       VARCHAR
)
COMMENT = 'Stream-triggered consolidation runs (task DAG)';
