-- Migration 25: when an approved card's call began.
--
-- A card is `executing` from its claim, through the hold in which UNDO
-- still stops it, to the end of its call. UNDO and unlinking its chat took
-- an executing card back at any point, and its outcome then kept the undo
-- (step 11's second review). So a yes undone mid-call left an email sent
-- but recorded undone, which the mailbox, keyed on done, could send again,
-- and money spent but not counted against the caps (step 11's third
-- review). From when its call begins, nothing takes a card back; this is
-- when that was (agent/approvals.py, `_calling`).

ALTER TABLE actions ADD COLUMN called_at TEXT;
