-- Migration 19: the photos that came with the user's message, and where
-- their channel keeps them (channels/photos.py `link`).
--
-- A photo reached the model with its message, and the message was stored
-- as "[1 photo attached]" and its words: the day showed that line where the
-- photo was, so the user couldn't see which photo a reply was about. The
-- day shows each one on its message now (agent/worker/journal.py), fetched
-- by the message and its place (agent/worker/app_photos.py `get_photo`),
-- never by a path a request names.
--
-- The files go after a day; the rows stay, so the day says "a photo".

CREATE TABLE message_photos (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (message_id, n)
);
