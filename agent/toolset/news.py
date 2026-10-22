"""News: which topics the attention loop follows for them.

The loop reads a feed per topic and tells them only what matters
(`agent/attention/sources/news.py`); these are how the user changes the list in
conversation -- "keep an eye on Anthropic", "stop telling me about
markets".
"""

from __future__ import annotations

from pydantic_ai import RunContext

from agent.turn import Deps


def register(tool) -> None:
    """Attach the news tools. `tool` is `register_tools`'s filter."""

    @tool
    async def follow_news(ctx: RunContext[Deps], topic: str) -> dict:
        """Start following news about a topic, an organisation or a person.

        For "keep an eye on X" or "let me know if anything happens with Y".
        Headlines about it are then judged with everything else: a line in a
        briefing when something matters, a message only if it changes the
        user's day -- say so, so they do not expect a stream.
        """
        from agent.attention import interests

        return interests.follow(ctx.deps.conn, topic)

    @tool
    async def unfollow_news(ctx: RunContext[Deps], topic: str) -> dict:
        """Stop following a topic. Use the name `followed_news` gives. For
        "stop telling me about X".
        """
        from agent.attention import interests

        return interests.unfollow(ctx.deps.conn, topic)

    @tool
    async def news_summary(ctx: RunContext[Deps], topic: str = "") -> dict:
        """What is happening in the news, summarised from the articles.

        For "what's the news?" (leave `topic` empty: everything the user
        follows, and any newsletters they get) or "what's the
        news on X?". Pass the summary on as it is and name the outlets; the
        `sources` are there if they ask where something came from.
        """
        from agent.attention import newsdesk

        return await newsdesk.on_request(ctx.deps.conn, topic)

    @tool
    async def followed_news(ctx: RunContext[Deps]) -> dict:
        """The news topics the user follows."""
        from agent.attention import interests

        return {"following": [i.topic for i in interests.active(ctx.deps.conn)]}
