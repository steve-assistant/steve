-- Migration 6: the page that fixes an account, kept with its health.
--
-- An account's health said what was wrong, and only that. For Google, the
-- commonest first failure is an API not turned on in the user's own Google
-- Cloud project, and the fix is one page there, which only the error knew:
-- the user was told "needs you" and left to find it. A provider now names
-- that page with its error (`steve_sdk.errors.ConnectorError.fix`), core
-- keeps it here, an https address only (core/health.py), and the app opens
-- it from the account (05-connectors.md, section 5).

ALTER TABLE account_health ADD COLUMN fix TEXT;  -- an https page that fixes it, if any
