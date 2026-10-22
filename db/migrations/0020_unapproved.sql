-- Migration 20: a routine or goal whose card ends without a yes ends too.
--
-- A routine or goal that may act without asking waits for the user's yes
-- on its card (`approve_routine`, `approve_goal`). A no left it waiting all
-- the same, with no card left to say yes to: listed as waiting for a yes,
-- named so in the morning's briefing, and a routine held one of its twenty
-- places for good. So did a card that expired unanswered, one withdrawn,
-- and a yes undone during the hold. Step 10's Telegram scenario found it
-- (19 October 2026).
--
-- Ended by trigger, as migration 18 stamps changes, not by each way a card
-- ends: there are four, and the next one added would forget.

CREATE TRIGGER routine_unapproved AFTER UPDATE OF status ON actions
WHEN NEW.tool = 'approve_routine'
 AND NEW.status IN ('rejected', 'expired', 'withdrawn', 'undone')
BEGIN
    UPDATE routines SET status = 'cancelled'
     WHERE id = json_extract(NEW.args_json, '$.routine_id')
       AND status = 'awaiting_approval';
END;

CREATE TRIGGER goal_unapproved AFTER UPDATE OF status ON actions
WHEN NEW.tool = 'approve_goal'
 AND NEW.status IN ('rejected', 'expired', 'withdrawn', 'undone')
BEGIN
    UPDATE goals SET status = 'dropped', outcome = 'its card was not approved',
                     next_check_at = NULL
     WHERE id = json_extract(NEW.args_json, '$.goal_id')
       AND status = 'awaiting_approval';
END;
