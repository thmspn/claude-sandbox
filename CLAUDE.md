# CLAUDE.md

Standing project constraints for this repository.

## Project

`toolkit/` is a reverse-engineering helper toolkit for CTF competitions
(CyberSci regionals: 4-hour jeopardy CTF; reversing, crypto, forensics and web
categories).

## Hard rule: fully offline

Toolkit code must run 100% offline. No network calls, no API calls, no
telemetry, and no runtime `pip install`. This is a competition rule.

## Technical constraints

- Python 3, targeting Kali Linux.
- Standard library first. Third-party libraries (capstone, pyelftools,
  pwntools) may only be used as optional imports, wrapped in `try/except` with
  a working fallback.
- Every module plugs into the `toolkit/modules` registry and provides a
  self-test runnable via `python3 toolkit/recon.py --selftest`.
- Use type hints and docstrings. No placeholder code.

## Workflow

- Keep PRs focused: one module or one coherent change per request.
