"""A module's process in the sandbox: what core starts, and what answers it.

Core starts the runtime's Python on this file with the module's id, its
code folder, its state folder, the user's zone and its account (JSON, or
`-` for none). It mounts the code folder as the package the id names,
imports the code file the manifest names, and answers core's calls on the
pipe until core closes it:

- `tool`: the function a tool of the manifest names, as
  `name(ctx, **args)`;
- `perform`: the module's `perform(ctx, action, args)`, with exactly what
  the user approved on a card;
- `collect`: the module's `collect(ctx)`, for a module with an attention
  source: what it has noticed, as a list of items (`docs/modules.md`);
- `provider`: one method of the provider a class of the manifest names in
  the code, made for the call from the account's context, for a connector
  from outside. What it takes and gives crosses as `steve_sdk.wire` says.

A name the manifest does not list as a tool is refused here as well as in
core, so a module's other functions are not reachable by name. What a call
may reach was decided in core, from the copy the user approved: the
broker holds the grants, and this end holds nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from steve_sdk import wire
from steve_sdk.context import Account
from steve_sdk.link import SandboxContext, connect
from steve_sdk.manifest import load
from steve_sdk.mount import code as code_of
from steve_sdk.mount import mount


def _account(text: str, module_id: str) -> Account:
    if text == "-":
        return Account(key="", name="", module=module_id)
    held = json.loads(text)
    return Account(key=held["key"], name=held["name"], module=module_id,
                   address=held.get("address", ""), fields=held.get("fields") or {},
                   preset=held.get("preset"))


def main(argv: list[str]) -> None:
    module_id, folder, state, zone, account = argv
    link = connect()
    code = Path(folder)
    mount(module_id, code)
    manifest = load(code / "manifest.toml")
    source = code_of(module_id, code, manifest.code)
    ctx = SandboxContext(link, account=_account(account, module_id), state_dir=Path(state),
                         zone=zone)
    tools = {tool.name for tool in manifest.tools}

    def tool(params: dict) -> Any:
        name, args = params.get("name"), params.get("args")
        if name not in tools or not isinstance(args, dict):
            raise ValueError(f"{module_id} has no tool {name!r}")
        return getattr(source, name)(ctx, **args)

    def perform(params: dict) -> Any:
        action, args = params.get("action"), params.get("args")
        if not isinstance(action, str) or not isinstance(args, dict):
            raise ValueError("an action is a name and its arguments")
        return source.perform(ctx, action, args)

    def collect(params: dict) -> Any:
        if manifest.attention is None:
            raise ValueError(f"{module_id} has no attention source")
        return source.collect(ctx)

    def provider(params: dict) -> Any:
        klass, method = params.get("klass"), params.get("method")
        spec = manifest.specs.get(klass) if isinstance(klass, str) else None
        if spec is None or spec.engine != "code":
            raise ValueError(f"{module_id} provides no {klass!r} from its code")
        made = getattr(source, spec.settings["provider"])(ctx)
        if method == "describe":
            return {"capabilities": sorted(made.capabilities)}
        if method not in wire.methods(klass):
            raise ValueError(f"a {klass} provider has no method {method!r}")
        args, kwargs = wire.load(params.get("args", [])), wire.load(params.get("kwargs", {}))
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError("a method's arguments are a list and a table")
        return wire.dump(getattr(made, method)(*args, **kwargs))

    link.serve({"tool": tool, "perform": perform, "collect": collect, "provider": provider})


if __name__ == "__main__":
    main(sys.argv[1:])
