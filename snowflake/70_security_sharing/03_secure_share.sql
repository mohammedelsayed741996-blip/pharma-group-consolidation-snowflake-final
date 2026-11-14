/* =============================================================================
   70_security_sharing / 03  Secure Data Share for the group auditor
   Requires ACCOUNTADMIN (or a role with CREATE SHARE). Trial accounts may not be
   allowed to add consumer or reader accounts: the share is still created and can be
   inspected with SHOW SHARES / DESC SHARE.
   To publish to a real consumer account, uncomment the ALTER SHARE line.
   Run as: ACCOUNTADMIN  (run_pipeline.py security --share)
   ============================================================================= */
USE ROLE ACCOUNTADMIN;

CREATE SHARE IF NOT EXISTS HELIX_GROUP_AUDIT_SHARE
    COMMENT = 'Helix Pharma consolidated statutory figures for the group auditor';

GRANT USAGE ON DATABASE HELIX_FIN TO SHARE HELIX_GROUP_AUDIT_SHARE;
GRANT USAGE ON SCHEMA HELIX_FIN.MARTS TO SHARE HELIX_GROUP_AUDIT_SHARE;
GRANT SELECT ON VIEW HELIX_FIN.MARTS.SV_GROUP_PL TO SHARE HELIX_GROUP_AUDIT_SHARE;
GRANT SELECT ON VIEW HELIX_FIN.MARTS.SV_GROUP_BS TO SHARE HELIX_GROUP_AUDIT_SHARE;
GRANT SELECT ON VIEW HELIX_FIN.MARTS.SV_IC_RECON TO SHARE HELIX_GROUP_AUDIT_SHARE;

-- ALTER SHARE HELIX_GROUP_AUDIT_SHARE ADD ACCOUNTS = <auditor_org>.<auditor_account>;
