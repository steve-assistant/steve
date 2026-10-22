-- Migration 10: the self-written tools' grant ledger goes.
--
-- `tool_audits` held a second model's verdict on each tool Steve wrote into
-- `tools/` that asked for a capability, keyed to the hash of its source,
-- and the user's revocations. Step 6 deleted that path (06-modules.md,
-- item 6): a tool is a module's now, run in the sandbox under the grants the
-- user approved on its card, and a module is turned off, not revoked. Nothing
-- reads the table any more.

DROP TABLE tool_audits;
