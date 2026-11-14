/* =============================================================================
   60_tasks / 01  Stream-triggered consolidation (task DAG)

       T_DETECT_NEW_TB   every 15 min, runs only WHEN the stream on RAW.TB_LINES has data;
            |            consumes the stream and logs the delivery
            v
       T_REFRESH_MARTS   CALL MARTS.SP_REFRESH()      (translation, IC, eliminations, marts)
            |
            v
       T_RUN_DQ          CALL AUDIT.SP_RUN_DQ()       (12 rules + close status)

   The WHEN condition is evaluated without a warehouse, so an idle pipeline costs nothing.
   Tasks are created SUSPENDED; run_pipeline.py tasks --resume / --suspend controls them.
   Run as: FIN_ENGINEER (needs EXECUTE TASK, granted in 00_setup/02)
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;
USE SCHEMA AUDIT;

-- Consume the stream (a DML statement advances the stream offset) and log the delivery
CREATE OR REPLACE PROCEDURE AUDIT.SP_REGISTER_NEW_TB()
    RETURNS VARCHAR
    LANGUAGE SQL
    EXECUTE AS CALLER
AS
$$
DECLARE
    n_rows INTEGER DEFAULT 0;
BEGIN
    INSERT INTO AUDIT.PIPELINE_RUNS (TRIGGERED_BY, NEW_TB_ROWS, NEW_TB_FILES, MESSAGE)
    SELECT 'TASK T_DETECT_NEW_TB', COUNT(*), COUNT(DISTINCT _FILE_NAME), 'New trial-balance data detected'
    FROM RAW.STR_TB_LINES;
    SELECT NEW_TB_ROWS INTO :n_rows FROM AUDIT.PIPELINE_RUNS ORDER BY RUN_TS DESC LIMIT 1;
    RETURN n_rows || ' new trial-balance rows registered';
END;
$$;

-- Suspend the root before replacing the DAG (no error if it does not exist yet)
ALTER TASK IF EXISTS AUDIT.T_DETECT_NEW_TB SUSPEND;

CREATE OR REPLACE TASK AUDIT.T_DETECT_NEW_TB
    WAREHOUSE = WH_FIN_XS
    SCHEDULE = '15 MINUTE'
    COMMENT = 'Root task: start consolidation when new trial balances arrived'
    WHEN SYSTEM$STREAM_HAS_DATA('RAW.STR_TB_LINES')
AS
    CALL AUDIT.SP_REGISTER_NEW_TB();

CREATE OR REPLACE TASK AUDIT.T_REFRESH_MARTS
    WAREHOUSE = WH_FIN_XS
    COMMENT = 'Rebuild integration tables and marts'
    AFTER AUDIT.T_DETECT_NEW_TB
AS
    CALL MARTS.SP_REFRESH();

CREATE OR REPLACE TASK AUDIT.T_RUN_DQ
    WAREHOUSE = WH_FIN_XS
    COMMENT = 'Evaluate data-quality rules and update the close status'
    AFTER AUDIT.T_REFRESH_MARTS
AS
    CALL AUDIT.SP_RUN_DQ();
