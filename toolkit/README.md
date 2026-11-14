# RE toolkit

Helper scripts for the CyberSci regional CTF (reversing category). **Fully offline**:
no network calls, no APIs, no telemetry, no runtime installs.

## Requirements

- Python 3.9+ (standard library only)
- Optional libraries (capstone, pyelftools, pwntools) are only ever imported inside
  `try/except`, with a stdlib fallback or a "skipped: install X" message. Current modules need none.

## Run

```
python3 recon.py                  # interactive numbered menu
python3 recon.py <module> [args]  # run one module directly
python3 recon.py list             # list modules
python3 recon.py --selftest       # run every module's self-test, print pass/fail summary
```

## Modules

| Module   | Description |
|----------|-------------|
| `triage` | Magic-based file type, size, md5/sha1/sha256, strings (ASCII + UTF-16LE), entropy overall and per section with a packed/encrypted note. ELF: class, endianness, arch, entry, sections, interpreter, NX/PIE/canary/RELRO/FORTIFY. PE: header info and sections. |

```
python3 recon.py triage ./crackme -n 6 --max-strings 50
python3 recon.py triage ./crackme --no-strings
```

## Adding a module

Drop a file in `modules/` defining `NAME`, `DESCRIPTION`, `main(argv) -> int` and
`selftest() -> None` (raise `AssertionError` on failure). It is discovered automatically,
appears in the menu, and is included in `--selftest`. Files without `NAME` are ignored.
