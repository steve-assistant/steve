-- Migration 22: when a conversation first read text from outside.
--
-- From then on, a conversation the user is in opens only the pages it was
-- shown (agent/toolset/web.py `unshown`): a model fooled by an email could
-- otherwise open an address carrying what it read. The mark lasts the
-- conversation, and the worker restarts often, so it is kept on the session
-- (agent/loop.py `Conversation.reading`). NULL: nothing from outside read.

ALTER TABLE sessions ADD COLUMN read_outside_at TEXT;
