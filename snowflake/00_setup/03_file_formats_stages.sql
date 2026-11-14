/* =============================================================================
   00_setup / 03  File format and landing stage
   Run as: FIN_ENGINEER
   ============================================================================= */
USE ROLE FIN_ENGINEER;
USE WAREHOUSE WH_FIN_XS;
USE DATABASE HELIX_FIN;

-- RAW keeps values exactly as delivered: empty fields stay '' (no NULL substitution)
CREATE OR REPLACE FILE FORMAT RAW.FF_CSV
    TYPE = CSV
    FIELD_DELIMITER = ','
    SKIP_HEADER = 1
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    NULL_IF = ()
    EMPTY_FIELD_AS_NULL = FALSE
    TRIM_SPACE = FALSE
    ERROR_ON_COLUMN_COUNT_MISMATCH = TRUE
    ENCODING = 'UTF8'
    COMMENT = 'Source CSV extracts with header row';

-- Internal stage; the Python loader PUTs files into sub-folders:
--   master/  fx/  tb/  ic/  budget/  control/  adjustments/
CREATE STAGE IF NOT EXISTS RAW.LANDING
    FILE_FORMAT = RAW.FF_CSV
    COMMENT = 'Landing zone for ERP extracts, FX feed, budget and manifests';
