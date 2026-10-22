-- Migration 8: self-upgrade v1's ledger goes.
--
-- `upgrades` held each attempt to change Steve's own tree: its branch, its
-- diff, the gate's output and whether it merged. Step 6 deleted that path
-- (06-modules.md, section 8): what Steve builds for itself is a module now,
-- recorded by the module builds when they come. Nothing reads the table any
-- more, and no install has rows in it worth keeping.
--
-- `issue_attempts.upgrade_id` stays as a column: it is 0 or a number that
-- named an attempt, and the night's tries at an issue still count by row.

DROP INDEX idx_upgrades_pending;
DROP TABLE upgrades;
