-- Migration 13: a build keeps what the user last said beside its request.
--
-- `module_builds.request` is the `build_module` tool's argument, which the
-- model writes: words that steer the model, such as a mail's, write it too,
-- and the card showed it as what the user asked. So a build asked for in the
-- user's own conversation keeps their last message of that turn
-- (`user_said`, '' where no one was there), and the card shows it beside the
-- request (agent/toolset/control.py, core/lifecycle.record).

ALTER TABLE module_builds ADD COLUMN user_said TEXT NOT NULL DEFAULT '';
