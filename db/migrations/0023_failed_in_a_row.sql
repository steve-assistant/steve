-- Migration 23: how many calls in a row a watched module has failed.
--
-- A module names the kind of each failure, and the failures it says were
-- its account's or the network's are not counted against it, so a broken
-- module that always said so was never turned off (step 11's audit, B4).
-- Ten failures in a row turn it off whatever it said (core/lifecycle.py
-- `noted`); a call that works starts the run again.

ALTER TABLE module_watch ADD COLUMN failed_in_a_row INTEGER NOT NULL DEFAULT 0;
