"""A module's folder, imported as the package its id names.

A module's files are kept by version (`<data>/modules/<id>/code/<version>/`,
06-modules.md, section 5), and a folder named `1.2` is no package name: a
test that imported the module through its folder would not import at all,
and one that put the folder itself on the path would import each file as a
top-level module, where a relative import fails. So core mounts the folder
under the module's id before anything of it runs, and its files import each
other relatively, or as `<id>.<file>`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def mount(name: str, folder: Path) -> types.ModuleType:
    """The files in `folder` as the package `name`, running its
    `__init__.py` if it has one."""
    init = folder / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            name, init, submodule_search_locations=[str(folder)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {init}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        return package
    package = types.ModuleType(name)
    package.__path__ = [str(folder)]
    package.__package__ = name
    sys.modules[name] = package
    return package


def code(name: str, folder: Path, file: str) -> types.ModuleType:
    """`file` in the module mounted as `name`, imported as `<name>.<stem>`
    from its path in `folder`, so nothing else on the path can stand in
    for it."""
    qualified = f"{name}.{file.removesuffix('.py')}"
    spec = importlib.util.spec_from_file_location(qualified, folder / file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {folder / file}")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = loaded
    spec.loader.exec_module(loaded)
    return loaded
