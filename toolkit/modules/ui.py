"""Terminal styling helpers for recon.py's interactive menu (raw ANSI, stdlib only).

This is a helper, not a toolkit module: it has no NAME/DESCRIPTION/run/selftest, so the
registry in ``modules/__init__.py`` skips it. Colors are emitted only when stdout is a TTY
and ``NO_COLOR`` is unset; otherwise every helper returns plain text.
"""
from __future__ import annotations

import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
WHITE = "\033[97m"

RULE_WIDTH = 80

BANNER = (
    "██████╗ ███████╗ ██████╗ ██████╗ ███╗   ██╗",
    "██╔══██╗██╔════╝██╔════╝██╔═══██╗████╗  ██║",
    "██████╔╝█████╗  ██║     ██║   ██║██╔██╗ ██║",
    "██╔══██╗██╔══╝  ██║     ██║   ██║██║╚██╗██║",
    "██║  ██║███████╗╚██████╗╚██████╔╝██║ ╚████║",
    "╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝",
)
SUBTITLE = "CTF Reverse-Engineering Toolkit - offline"


def use_color() -> bool:
    """True when stdout is a TTY and NO_COLOR is not set."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _unicode_ok() -> bool:
    """True when stdout's encoding can render the box/symbol characters used here."""
    try:
        "─▶▸✓✗⚠ℹ█╗".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def paint(text: str, *codes: str) -> str:
    """Wrap ``text`` in ANSI ``codes`` when color is enabled, else return it unchanged."""
    if not codes or not use_color():
        return text
    return "".join(codes) + text + RESET


def rule() -> str:
    """A horizontal rule, cyan when color is enabled."""
    return paint(("─" if _unicode_ok() else "-") * RULE_WIDTH, CYAN)


def header(title: str) -> str:
    """Section header: rule, ``▶ TITLE``, rule."""
    arrow = "▶" if _unicode_ok() else ">"
    return f"{rule()}\n{paint(f'{arrow} {title}', BOLD, YELLOW)}\n{rule()}"


def banner() -> str:
    """Blue block-letter banner with a cyan subtitle (plain text when color is off)."""
    if not _unicode_ok():
        return f"RECON\n{SUBTITLE}"
    lines = [paint(line, BOLD, BLUE) for line in BANNER]
    lines.append(paint(SUBTITLE, CYAN))
    return "\n".join(lines)


def category(title: str) -> str:
    """Yellow ``▸ CATEGORY`` heading."""
    return paint(f"{'▸' if _unicode_ok() else '>'} {title}", BOLD, YELLOW)


def menu_row(key: str, name: str, desc: str, name_width: int = 12, num_width: int = 3) -> str:
    """One menu row: cyan right-aligned key, padded name, white description cut to fit the rule."""
    room = RULE_WIDTH - (2 + num_width + 2 + name_width + 1)
    if len(desc) > room:
        desc = desc[: room - 1].rstrip() + ("…" if _unicode_ok() else ".")
    return f"  {paint(key.rjust(num_width), CYAN)}  {name:<{name_width}} {paint(desc, WHITE)}".rstrip()


def _status(symbol: str, ascii_symbol: str, color: str, msg: str) -> str:
    return f"{paint(symbol if _unicode_ok() else ascii_symbol, color)} {msg}"


def success(msg: str) -> str:
    """Green ``✓`` status line."""
    return _status("✓", "[ok]", GREEN, msg)


def error(msg: str) -> str:
    """Red ``✗`` status line."""
    return _status("✗", "[x]", RED, msg)


def warning(msg: str) -> str:
    """Yellow ``⚠`` status line."""
    return _status("⚠", "[!]", YELLOW, msg)


def info(msg: str) -> str:
    """Blue ``ℹ`` status line."""
    return _status("ℹ", "[i]", BLUE, msg)
