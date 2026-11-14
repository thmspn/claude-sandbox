#!/usr/bin/env python3
"""Master script for the offline RE toolkit.

    python3 recon.py                 interactive menu
    python3 recon.py <module> [args] run one module directly
    python3 recon.py --selftest      run every module's self-test

Performs no network access of any kind.
"""
from __future__ import annotations

import shlex
import sys
import traceback

from modules import Module, discover


def print_modules(modules: dict[str, Module]) -> None:
    """Print the numbered module list."""
    for i, mod in enumerate(modules.values(), 1):
        print(f"  {i}. {mod.name:<12} {mod.description}")


def run_selftests(modules: dict[str, Module], errors: dict[str, str]) -> int:
    """Run all self-tests, print a summary, return a process exit code."""
    failed = 0
    for name, err in errors.items():
        print(f"[FAIL] {name}: failed to load: {err}")
        failed += 1
    for name, mod in modules.items():
        try:
            mod.selftest()
            print(f"[PASS] {name}")
        except Exception:
            failed += 1
            print(f"[FAIL] {name}")
            print("       " + traceback.format_exc().strip().replace("\n", "\n       "))
    total = len(modules) + len(errors)
    print(f"\n{total - failed}/{total} modules passed")
    return 1 if failed else 0


def menu(modules: dict[str, Module]) -> int:
    """Interactive numbered menu; loops until the user quits."""
    names = list(modules)
    while True:
        print("\n=== RE toolkit (offline) ===")
        print_modules(modules)
        print("  s. selftest\n  q. quit")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "s":
            run_selftests(modules, {})
            continue
        if choice in modules:
            name = choice
        elif choice.isdigit() and 1 <= int(choice) <= len(names):
            name = names[int(choice) - 1]
        else:
            print("invalid choice")
            continue
        try:
            args = shlex.split(input(f"{name} args (e.g. a file path; -h for help): "))
        except (EOFError, ValueError) as exc:
            print(f"bad input: {exc}")
            continue
        try:
            modules[name].main(args)
        except SystemExit:
            pass  # argparse exits on -h or bad args; stay in the menu


def main(argv: list[str]) -> int:
    """Entry point."""
    modules, errors = discover()
    if argv and argv[0] == "--selftest":
        return run_selftests(modules, errors)
    for name, err in errors.items():
        print(f"warning: module {name} failed to load: {err}", file=sys.stderr)
    if not argv:
        return menu(modules)
    if argv[0] in ("-h", "--help", "list"):
        print(__doc__)
        print_modules(modules)
        return 0
    if argv[0] not in modules:
        print(f"unknown module: {argv[0]}\n", file=sys.stderr)
        print_modules(modules)
        return 2
    return modules[argv[0]].main(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
