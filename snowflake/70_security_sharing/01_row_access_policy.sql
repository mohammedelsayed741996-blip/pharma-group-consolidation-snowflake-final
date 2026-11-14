/* =============================================================================
   70_security_sharing / 01  Row-level security on the fact tables
   * FIN_ENGINEER and GROUP_CONTROLLER see every entity
   * Any other role sees the entities mapped to it in AUDIT.ROLE_ENTITY_MAP
     (example below: a CEE country controller for CZ01, CZ02, PL01, HU01, RO01)
   * Consumers of the secure share see everything the share exposes
   Power BI RLS roles mirror this mapping (see powerbi/ in Batch 3).
   Run as: FIN_ENGINEER - re-run after 40_marts/02 (which replaces the mart tables)
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;

CREATE TABLE IF NOT EXISTS AUDIT.ROLE_ENTITY_MAP (
    ROLE_NAME     VARCHAR,
    COMPANY_CODE  VARCHAR
)
COMMENT = 'Which entities a role may see in the MARTS fact tables';

MERGE INTO AUDIT.ROLE_ENTITY_MAP t
USING (
    SELECT 'COUNTRY_CONTROLLER' AS ROLE_NAME, COLUMN1 AS COMPANY_CODE
    FROM VALUES ('CZ01'), ('CZ02'), ('PL01'), ('HU01'), ('RO01')
) s
ON t.ROLE_NAME = s.ROLE_NAME AND t.COMPANY_CODE = s.COMPANY_CODE
WHEN NOT MATCHED THEN INSERT (ROLE_NAME, COMPANY_CODE) VALUES (s.ROLE_NAME, s.COMPANY_CODE);

-- A policy cannot be replaced while attached: detach first
ALTER TABLE IF EXISTS MARTS.FCT_CONSOLIDATED_GL DROP ALL ROW ACCESS POLICIES;
ALTER TABLE IF EXISTS MARTS.FCT_CASHFLOW        DROP ALL ROW ACCESS POLICIES;
ALTER TABLE IF EXISTS MARTS.FCT_IC_RECON        DROP ALL ROW ACCESS POLICIES;

CREATE OR REPLACE ROW ACCESS POLICY MARTS.RAP_ENTITY
    AS (ENTITY VARCHAR) RETURNS BOOLEAN ->
        IS_ROLE_IN_SESSION('FIN_ENGINEER')
        OR IS_ROLE_IN_SESSION('GROUP_CONTROLLER')
        OR INVOKER_SHARE() = 'HELIX_GROUP_AUDIT_SHARE'
        OR ENTITY = '*GROUP'
        OR EXISTS (
            SELECT 1
            FROM AUDIT.ROLE_ENTITY_MAP m
            WHERE m.ROLE_NAME = CURRENT_ROLE()
              AND m.COMPANY_CODE = ENTITY
        )
    COMMENT = 'Entity-level row security for country controllers';

ALTER TABLE MARTS.FCT_CONSOLIDATED_GL ADD ROW ACCESS POLICY MARTS.RAP_ENTITY ON (COMPANY_CODE);
ALTER TABLE MARTS.FCT_CASHFLOW        ADD ROW ACCESS POLICY MARTS.RAP_ENTITY ON (COMPANY_CODE);
ALTER TABLE MARTS.FCT_IC_RECON        ADD ROW ACCESS POLICY MARTS.RAP_ENTITY ON (ENTITY_B);
