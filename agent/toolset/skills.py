"""Skills in their conversations (agent/skills.py): load one, list them, take
one back.

Their conversations only. `register_tools` offers them to every run, but no
unattended toolset names them (`WORKER_TOOLS`, a routine's, a goal's, a
mail's), and each refuses in an unattended run besides: a skill's text is
offered where the user is, and nowhere else (core/skills.py).
"""

from __future__ import annotations

from pydantic_ai import RunContext

from agent import modtext, skills
from agent.turn import Deps

NOT_HERE = {"ok": False, "error": "skills are only for conversations the user is in"}


def _theirs(ctx: RunContext[Deps]) -> bool:
    from core.call_policy import is_autonomous

    return not ctx.deps.worker and not is_autonomous(ctx.deps.channel)


def register(tool) -> None:
    @tool
    async def use_skill(ctx: RunContext[Deps], skill: int | str) -> dict:
        """The steps of a skill from the list in your instructions, to follow
        for this request: its number, or a module's `<module>.<number>`. A
        skill grants nothing: every action is still checked and asked about
        as usual."""
        if not _theirs(ctx):
            return NOT_HERE
        key = str(skill).strip()
        found = (modtext.skill(ctx.deps.conn, key) if "." in key
                 else skills.use(ctx.deps.conn, int(key), ctx.deps.session_id)
                 if key.isdigit() else None)
        if found is None:
            return {"ok": False, "error": f"no skill {skill} is kept"}
        return {"ok": True, "skill": found.id, "title": found.title, "steps": found.text()}

    @tool
    async def list_skills(ctx: RunContext[Deps]) -> dict:
        """The skills kept: what each is for, its steps, and how often it was
        used. For when the user asks what you have learned to do.
        """
        if not _theirs(ctx):
            return NOT_HERE
        conn = ctx.deps.conn
        return {"skills": [s.as_dict() for s in skills.active(conn)]
                if modtext.skills_on(conn) else [],
                "from_modules": [s.as_dict() for s in modtext.skills(conn)],
                "revoke": "The user can say REVOKE SKILL and its number. A module's skills "
                          "go with the module, on the Extensions page; so do all of these, "
                          "with the Skills module."}

    @tool
    async def revoke_skill(ctx: RunContext[Deps], skill: int | str) -> dict:
        """Stop using a skill, when the user asks. It is kept in the record,
        revoked.
        """
        if not _theirs(ctx):
            return NOT_HERE
        if not str(skill).strip().isdigit():
            return {"ok": False, "error": f"skill {skill} is a module's: it goes with the "
                                          "module, which the user can turn off on the "
                                          "Extensions page"}
        gone = skills.revoke(ctx.deps.conn, int(str(skill).strip()))
        if gone is None:
            return {"ok": False, "error": f"no skill {skill} is kept"}
        return {"ok": True, "revoked": gone.id, "title": gone.title}
