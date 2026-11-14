/* =============================================================================
   00_setup / 01  Warehouse, database and schemas
   Run as: SYSADMIN (any role that can create warehouses and databases)
   ============================================================================= */
USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS WH_FIN_XS
    WITH WAREHOUSE_SIZE = 'XSMALL'
         AUTO_SUSPEND = 60
         AUTO_RESUME = TRUE
         INITIALLY_SUSPENDED = TRUE
    COMMENT = 'Helix Pharma finance consolidation - ELT and reporting';

CREATE DATABASE IF NOT EXISTS HELIX_FIN
    COMMENT = 'Helix Pharma Group - financial consolidation platform (synthetic data)';

CREATE SCHEMA IF NOT EXISTS HELIX_FIN.RAW         COMMENT = 'Landing: files exactly as delivered (all VARCHAR) + load metadata';
CREATE SCHEMA IF NOT EXISTS HELIX_FIN.STAGING     COMMENT = 'Typed, trimmed, hashed and de-duplicated views over RAW';
CREATE SCHEMA IF NOT EXISTS HELIX_FIN.INTEGRATION COMMENT = 'Business logic: mapping, IAS 21 translation, IC matching, eliminations, NCI';
CREATE SCHEMA IF NOT EXISTS HELIX_FIN.MARTS       COMMENT = 'Star schema for Power BI and secure views for data sharing';
CREATE SCHEMA IF NOT EXISTS HELIX_FIN.AUDIT       COMMENT = 'Load log, data-quality results, close status, pipeline runs';
