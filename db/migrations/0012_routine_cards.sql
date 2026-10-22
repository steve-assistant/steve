-- Migration 12: a card a module's routine asked for names the module.
--
-- A module's routine runs core's own tools, and every action it asks for is
-- a card (agent/modroutines.py). Such a card names the account its tool acts
-- on, or none, never the module whose words asked for it: so turning the
-- module off, or removing it, left the card waiting, and a later yes acted
-- for a module the user had turned away from. Off means original
-- (05-context.md, section 7). So a card carries the key of the module whose
-- routine asked for it (`routine_module`, NULL for anything else), and
-- `core/modules.withdraw_cards` withdraws it with the module's own cards.

ALTER TABLE actions ADD COLUMN routine_module TEXT;

CREATE INDEX idx_actions_routine_module
    ON actions(routine_module) WHERE status = 'pending';
