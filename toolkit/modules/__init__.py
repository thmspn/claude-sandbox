"""Module registry for the RE toolkit.

A module is any ``.py`` file in this package that defines:

* ``NAME: str``        - short CLI name (used as ``recon.py <NAME>``)
* ``DESCRIPTION: str`` - one-line summary shown in the menu
* ``main(argv: list[str]) -> int`` - run the module, return an exit code
* ``selftest() -> None`` - raise ``AssertionError`` on failure

Dropping such a file into this directory registers it automatically.
Files without ``NAME`` (shared helpers) are ignored.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Module:
    """A registered toolkit module."""

    name: str
    description: str
    main: Callable[[list[str]], int]
    selftest: Callable[[], None]


def discover() -> tuple[dict[str, Module], dict[str, str]]:
    """Import every module in this package.

    Returns ``(modules, errors)`` where ``modules`` maps NAME -> Module (sorted
    by name) and ``errors`` maps file name -> import/registration error text.
    """
    found: dict[str, Module] = {}
    errors: dict[str, str] = {}
    for info in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
            if not hasattr(mod, "NAME"):
                continue
            found[mod.NAME] = Module(mod.NAME, mod.DESCRIPTION, mod.main, mod.selftest)
        except Exception as exc:  # a broken module must not take down the toolkit
            errors[info.name] = f"{type(exc).__name__}: {exc}"
    return dict(sorted(found.items())), errors
