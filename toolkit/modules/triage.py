"""triage - first-look report on a file (offline, stdlib only).

Reports magic-based file type, size, hashes, strings (ASCII + UTF-16LE),
entropy (overall and per section) and, for ELF, headers and checksec-style
protections. PE files get basic header info.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import math
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

NAME = "triage"
DESCRIPTION = "File type, hashes, strings, entropy, ELF/PE info, checksec"

MAGICS: list[tuple[bytes, str]] = [
    (b"\x7fELF", "ELF executable/object"),
    (b"MZ", "DOS/PE executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit (LE)"),
    (b"\xce\xfa\xed\xfe", "Mach-O 32-bit (LE)"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal / Java class"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF8", "GIF image"),
    (b"%PDF", "PDF document"),
    (b"PK\x03\x04", "ZIP archive (also jar/apk/docx)"),
    (b"\x1f\x8b", "gzip data"),
    (b"BZh", "bzip2 data"),
    (b"\xfd7zXZ\x00", "xz data"),
    (b"7z\xbc\xaf\x27\x1c", "7-zip archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"\x00asm", "WebAssembly module"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"UPX!", "UPX-packed data"),
    (b"\xd4\xc3\xb2\xa1", "pcap capture (LE)"),
    (b"\xa1\xb2\xc3\xd4", "pcap capture (BE)"),
    (b"\x0a\x0d\x0d\x0a", "pcapng capture"),
    (b"#!", "script (shebang)"),
]

ELF_MACHINES = {
    0x02: "SPARC", 0x03: "x86", 0x08: "MIPS", 0x14: "PowerPC", 0x15: "PowerPC64",
    0x28: "ARM", 0x3E: "x86-64", 0xB7: "AArch64", 0xF3: "RISC-V", 0x16: "S390",
}
ELF_TYPES = {1: "REL (relocatable)", 2: "EXEC", 3: "DYN (shared/PIE)", 4: "CORE"}
PE_MACHINES = {0x14C: "x86", 0x8664: "x86-64", 0x1C0: "ARM", 0xAA64: "AArch64", 0x200: "IA-64"}
PE_SUBSYSTEMS = {1: "native", 2: "Windows GUI", 3: "Windows console", 9: "WinCE GUI", 10: "EFI app"}

PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_GNU_STACK, PT_GNU_RELRO = 1, 2, 3, 0x6474E551, 0x6474E552
SHT_NOBITS = 8
DT_NULL, DT_BIND_NOW, DT_FLAGS, DT_FLAGS_1 = 0, 24, 30, 0x6FFFFFFB
DF_BIND_NOW, DF_1_NOW, DF_1_PIE = 0x8, 0x1, 0x08000000

ENTROPY_PACKED = 7.2
ENTROPY_DENSE = 6.5


@dataclass
class Section:
    """A section (ELF) or section-like region (PE)."""

    name: str
    kind: str
    addr: int
    offset: int
    size: int
    flags: str
    data: bytes = field(repr=False, default=b"")


# ---------------------------------------------------------------- generic

def identify(data: bytes) -> str:
    """Identify file type from magic bytes (extension is never consulted)."""
    for magic, desc in MAGICS:
        if data.startswith(magic):
            return desc
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar archive"
    if data and all(b in b"\t\n\r" or 0x20 <= b < 0x7F for b in data[:4096]):
        return "ASCII text"
    return "unknown binary data"


def hashes(data: bytes) -> dict[str, str]:
    """Return md5/sha1/sha256 hex digests."""
    return {n: hashlib.new(n, data).hexdigest() for n in ("md5", "sha1", "sha256")}


def shannon_entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte (0.0 - 8.0)."""
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def entropy_note(entropy: float, size: int) -> str:
    """Heuristic interpretation of an entropy value."""
    if size < 64:
        return "too small to judge"
    if entropy >= ENTROPY_PACKED:
        return "HIGH: likely packed/encrypted/compressed"
    if entropy >= ENTROPY_DENSE:
        return "elevated: dense code or partly compressed data"
    return "normal"


def extract_strings(data: bytes, min_len: int = 4) -> list[tuple[int, str, str]]:
    """Classic ``strings``: return (offset, encoding, text), sorted by offset.

    ASCII runs are printable bytes (0x20-0x7e plus tab); UTF-16LE runs are
    printable ASCII characters each followed by a NUL byte.
    """
    n = max(1, min_len)
    out: list[tuple[int, str, str]] = []
    for m in re.finditer(rb"[\x20-\x7e\t]{%d,}" % n, data):
        out.append((m.start(), "ascii", m.group().decode("ascii")))
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % n, data):
        out.append((m.start(), "utf16le", m.group().decode("utf-16-le")))
    return sorted(out)


# -------------------------------------------------------------------- ELF

def _cstr(blob: bytes, off: int) -> str:
    """Read a NUL-terminated string from blob at off."""
    if off < 0 or off >= len(blob):
        return ""
    end = blob.find(b"\0", off)
    return blob[off:end if end != -1 else len(blob)].decode("latin-1")


def parse_elf(data: bytes) -> dict:
    """Parse an ELF image. Raises ValueError if truncated or malformed."""
    if len(data) < 20 or data[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    if data[4] not in (1, 2) or data[5] not in (1, 2):
        raise ValueError("invalid ELF class/endianness")
    is64 = data[4] == 2
    e = "<" if data[5] == 1 else ">"
    try:
        if is64:
            (etype, machine, _v, entry, phoff, shoff, _fl, _eh, phentsz, phnum,
             shentsz, shnum, shstrndx) = struct.unpack_from(e + "HHIQQQIHHHHHH", data, 16)
        else:
            (etype, machine, _v, entry, phoff, shoff, _fl, _eh, phentsz, phnum,
             shentsz, shnum, shstrndx) = struct.unpack_from(e + "HHIIIIIHHHHHH", data, 16)

        phdrs: list[tuple[int, int, int, int]] = []  # type, flags, offset, filesz
        for i in range(phnum if phoff else 0):
            o = phoff + i * phentsz
            if is64:
                t, fl, off, _va, _pa, fsz, _msz, _al = struct.unpack_from(e + "IIQQQQQQ", data, o)
            else:
                t, off, _va, _pa, fsz, _msz, fl, _al = struct.unpack_from(e + "IIIIIIII", data, o)
            phdrs.append((t, fl, off, fsz))

        raw: list[tuple[int, int, int, int, int, int]] = []  # name,type,flags,addr,off,size
        for i in range(shnum if shoff else 0):
            o = shoff + i * shentsz
            if is64:
                nm, ty, fl, ad, off, sz, *_ = struct.unpack_from(e + "IIQQQQ", data, o)
            else:
                nm, ty, fl, ad, off, sz, *_ = struct.unpack_from(e + "IIIIII", data, o)
            raw.append((nm, ty, fl, ad, off, sz))
    except struct.error as exc:
        raise ValueError(f"truncated ELF headers: {exc}") from exc

    strtab = b""
    if 0 <= shstrndx < len(raw):
        _, _, _, _, off, sz = raw[shstrndx]
        strtab = data[off:off + sz]
    sections: list[Section] = []
    for nm, ty, fl, ad, off, sz in raw:
        flags = "".join(c for bit, c in ((2, "A"), (1, "W"), (4, "X")) if fl & bit)
        body = b"" if ty == SHT_NOBITS else data[off:off + sz]
        sections.append(Section(_cstr(strtab, nm), f"{ty:#x}", ad, off, sz, flags, body))

    interp = ""
    dyn_tags: dict[int, int] = {}
    for t, _fl, off, fsz in phdrs:
        if t == PT_INTERP:
            interp = _cstr(data[off:off + fsz], 0)
        elif t == PT_DYNAMIC:
            step = 16 if is64 else 8
            fmt = e + ("qQ" if is64 else "iI")
            for o in range(off, min(off + fsz, len(data)) - step + 1, step):
                tag, val = struct.unpack_from(fmt, data, o)
                if tag == DT_NULL:
                    break
                dyn_tags[tag] = dyn_tags.get(tag, 0) | val if tag in (DT_FLAGS, DT_FLAGS_1) else val

    return {
        "bits": 64 if is64 else 32,
        "endian": "little" if e == "<" else "big",
        "type": ELF_TYPES.get(etype, f"unknown ({etype})"),
        "etype": etype,
        "machine": ELF_MACHINES.get(machine, f"unknown ({machine:#x})"),
        "entry": entry,
        "interp": interp,
        "phdrs": phdrs,
        "dyn_tags": dyn_tags,
        "sections": sections,
    }


def elf_protections(data: bytes, info: dict) -> dict[str, str]:
    """checksec-style protections for a parsed ELF (see parse_elf)."""
    ptypes = {t: fl for t, fl, _o, _s in info["phdrs"]}
    tags: dict[int, int] = info["dyn_tags"]

    if PT_GNU_STACK not in ptypes:
        nx = "unknown (no PT_GNU_STACK; usually executable stack)"
    else:
        nx = "disabled (stack executable)" if ptypes[PT_GNU_STACK] & 1 else "enabled"

    if info["etype"] == 3:
        pie = "enabled" if info["interp"] or tags.get(DT_FLAGS_1, 0) & DF_1_PIE else "shared object / static-PIE"
    else:
        pie = "disabled (fixed base)" if info["etype"] == 2 else "n/a"

    if PT_GNU_RELRO not in ptypes:
        relro = "none"
    else:
        now = (DT_BIND_NOW in tags or tags.get(DT_FLAGS, 0) & DF_BIND_NOW
               or tags.get(DT_FLAGS_1, 0) & DF_1_NOW)
        relro = "full" if now else "partial"

    # Symbol names live in .dynstr/.strtab; fall back to scanning the file
    # (stripped static binaries) only when no string-table sections exist.
    tables = [s.data for s in info["sections"] if s.name in (".dynstr", ".strtab")]
    names: set[str] = set()
    for blob in tables or [data]:
        names.update(re.findall(r"[A-Za-z0-9_.]{4,}", blob.decode("latin-1")))
    canary = "found" if "__stack_chk_fail" in names or "__intel_security_cookie" in names else "not found"
    chk = sorted(n for n in names if n.startswith("__") and n.endswith("_chk") and n != "__stack_chk")
    fortify = f"yes ({', '.join(chk[:6])})" if chk else "no _chk functions imported"
    return {"NX": nx, "PIE": pie, "Stack canary": canary, "RELRO": relro, "FORTIFY": fortify}


# --------------------------------------------------------------------- PE

def parse_pe(data: bytes) -> dict:
    """Parse basic PE headers. Raises ValueError if not a valid PE."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("no MZ header")
    (lfanew,) = struct.unpack_from("<I", data, 0x3C)
    if data[lfanew:lfanew + 4] != b"PE\0\0":
        raise ValueError("MZ file without PE signature (plain DOS executable)")
    try:
        machine, nsec, stamp, _sp, _ns, optsz, chars = struct.unpack_from("<HHIIIHH", data, lfanew + 4)
        opt = lfanew + 24
        magic, = struct.unpack_from("<H", data, opt)
        entry, = struct.unpack_from("<I", data, opt + 16)
        base = struct.unpack_from("<Q", data, opt + 24)[0] if magic == 0x20B else struct.unpack_from("<I", data, opt + 28)[0]
        subsys, = struct.unpack_from("<H", data, opt + 68)
        sections: list[Section] = []
        for i in range(nsec):
            o = opt + optsz + i * 40
            nm, vsz, va, rsz, roff = struct.unpack_from("<8sIIII", data, o)
            sections.append(Section(nm.rstrip(b"\0").decode("latin-1"), "", va, roff, rsz, "",
                                    data[roff:roff + rsz]))
    except struct.error as exc:
        raise ValueError(f"truncated PE headers: {exc}") from exc
    return {
        "machine": PE_MACHINES.get(machine, f"unknown ({machine:#x})"),
        "kind": "PE32+" if magic == 0x20B else "PE32",
        "dll": bool(chars & 0x2000),
        "timestamp": stamp, "entry_rva": entry, "image_base": base,
        "subsystem": PE_SUBSYSTEMS.get(subsys, str(subsys)),
        "sections": sections,
    }


# ----------------------------------------------------------------- report

def _section_table(sections: list[Section], with_flags: bool) -> list[str]:
    lines = [f"  {'name':<16}{'addr':>12}{'offset':>10}{'size':>10}  {'entropy':>7}  note"]
    for s in sections:
        if not s.name and not s.size:
            continue
        ent = shannon_entropy(s.data)
        shown = f"{ent:7.3f}" if s.data else "      -"
        note = entropy_note(ent, len(s.data)) if s.data else "no file data"
        flag = f" [{s.flags}]" if with_flags and s.flags else ""
        lines.append(f"  {s.name:<16}{s.addr:>#12x}{s.offset:>#10x}{s.size:>#10x}  {shown}  {note}{flag}")
    return lines


def report(data: bytes, label: str, min_len: int = 4, show_strings: bool = True,
           max_strings: int = 0) -> str:
    """Build the full triage report for a byte buffer."""
    out = [f"== triage: {label} ==", f"type:   {identify(data)}", f"size:   {len(data)} bytes"]
    out += [f"{k:<7} {v}" for k, v in hashes(data).items()]
    ent = shannon_entropy(data)
    out.append(f"entropy: {ent:.3f} / 8.0 - {entropy_note(ent, len(data))}")

    if data.startswith(b"\x7fELF"):
        try:
            info = parse_elf(data)
            out += ["", "-- ELF --",
                    f"class:   ELF{info['bits']}", f"endian:  {info['endian']}",
                    f"type:    {info['type']}", f"machine: {info['machine']}",
                    f"entry:   {info['entry']:#x}", f"interp:  {info['interp'] or '(none; static or no PT_INTERP)'}",
                    "", f"-- sections ({len(info['sections'])}) --"]
            out += _section_table(info["sections"], True)
            out += ["", "-- protections --"]
            out += [f"{k:<13}{v}" for k, v in elf_protections(data, info).items()]
            if not any(s.name for s in info["sections"]):
                out.append("note: no section headers (stripped/packed/hand-crafted?)")
        except ValueError as exc:
            out.append(f"ELF parse error: {exc} (possibly corrupted or deliberately malformed)")
    elif data.startswith(b"MZ"):
        try:
            pe = parse_pe(data)
            out += ["", "-- PE --", f"format:    {pe['kind']} {'DLL' if pe['dll'] else 'executable'}",
                    f"machine:   {pe['machine']}", f"subsystem: {pe['subsystem']}",
                    f"entry RVA: {pe['entry_rva']:#x}", f"image base: {pe['image_base']:#x}",
                    f"timestamp: {pe['timestamp']:#x}", "", f"-- sections ({len(pe['sections'])}) --"]
            out += _section_table(pe["sections"], False)
        except ValueError as exc:
            out.append(f"PE: {exc}")

    if show_strings:
        found = extract_strings(data, min_len)
        out += ["", f"-- strings (min length {min_len}): {len(found)} found --"]
        shown = found[:max_strings] if max_strings else found
        out += [f"{off:08x} {enc[0]} {txt}" for off, enc, txt in shown]
        if len(shown) < len(found):
            out.append(f"... {len(found) - len(shown)} more (raise --max-strings or use 0)")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(prog="recon.py triage", description=__doc__.splitlines()[0])
    p.add_argument("path", help="file to analyze")
    p.add_argument("-n", "--min-len", type=int, default=4, help="minimum string length (default 4)")
    p.add_argument("--no-strings", action="store_true", help="skip string extraction")
    p.add_argument("--max-strings", type=int, default=0, help="limit strings shown (0 = all)")
    args = p.parse_args(argv)
    try:
        data = Path(args.path).read_bytes()
    except OSError as exc:
        print(f"error: {exc}")
        return 1
    print(report(data, args.path, args.min_len, not args.no_strings, args.max_strings))
    return 0


# --------------------------------------------------------------- self-test

def build_elf64(*, pie: bool = False, nx: bool = True, relro: str = "full",
                canary: bool = True, fortify: bool = True, interp: bool = True) -> bytes:
    """Construct a minimal synthetic little-endian ELF64 x86-64 image."""
    dynstr = b"\0puts\0" + (b"__stack_chk_fail\0" if canary else b"") + (b"__printf_chk\0" if fortify else b"")
    shstr = b"\0.dynstr\0.shstrtab\0"
    interp_s = b"/lib64/ld-linux-x86-64.so.2\0" if interp else b""
    phdrs: list[tuple[int, int]] = []
    if interp:
        phdrs.append((PT_INTERP, 4))
    phdrs += [(PT_LOAD, 5), (PT_DYNAMIC, 6), (PT_GNU_STACK, 6 if nx else 7)]
    if relro != "none":
        phdrs.append((PT_GNU_RELRO, 4))
    ph_off = 64
    dyn_off = ph_off + 56 * len(phdrs)
    dyn = struct.pack("<qQ", DT_BIND_NOW, 0) if relro == "full" else b""
    dyn += struct.pack("<qQ", DT_NULL, 0)
    interp_off = dyn_off + len(dyn)
    dynstr_off = interp_off + len(interp_s)
    shstr_off = dynstr_off + len(dynstr)
    sh_off = shstr_off + len(shstr)
    ph = b""
    for t, fl in phdrs:
        off, sz = {PT_INTERP: (interp_off, len(interp_s)), PT_DYNAMIC: (dyn_off, len(dyn))}.get(t, (0, 0))
        ph += struct.pack("<IIQQQQQQ", t, fl, off, off, off, sz, sz, 8)
    sh = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    sh += struct.pack("<IIQQQQIIQQ", 1, 3, 2, 0, dynstr_off, len(dynstr), 0, 0, 1, 0)
    sh += struct.pack("<IIQQQQIIQQ", 9, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)
    hdr = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    hdr += struct.pack("<HHIQQQIHHHHHH", 3 if pie else 2, 0x3E, 1, 0x401000, ph_off, sh_off, 0,
                       64, 56, len(phdrs), 64, 3, 2)
    return hdr + ph + dyn + interp_s + dynstr + shstr + sh


def build_pe() -> bytes:
    """Construct a minimal synthetic PE32+ image with one section."""
    opt = struct.pack("<H", 0x20B) + bytes(14) + struct.pack("<I", 0x1000) + bytes(4)
    opt += struct.pack("<Q", 0x140000000) + bytes(36) + struct.pack("<H", 3)
    opt += bytes(240 - len(opt))
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, 1, 0x5F000000, 0, 0, 240, 0x22)
    sec = struct.pack("<8sIIIIIIHHI", b".text", 16, 0x1000, 16, 0x200, 0, 0, 0, 0, 0x60000020)
    dos = b"MZ" + bytes(0x3A) + struct.pack("<I", 0x40)
    return dos + coff + opt + sec + bytes(0x200 - len(dos + coff + opt + sec)) + b"\x90" * 16


def selftest() -> None:
    """Raise AssertionError if any triage check fails."""
    # magic / hashes / entropy
    assert identify(b"\x7fELF" + bytes(60)) == "ELF executable/object"
    assert identify(b"\x1f\x8b\x08" + bytes(10)) == "gzip data"
    assert identify(b"hello world\n") == "ASCII text"
    assert hashes(b"abc")["md5"] == "900150983cd24fb0d6963f7d28e17f72"
    assert hashes(b"abc")["sha256"].startswith("ba7816bf")
    assert shannon_entropy(bytes(256)) == 0.0
    assert abs(shannon_entropy(bytes(range(256)) * 4) - 8.0) < 1e-9
    assert entropy_note(7.9, 1000).startswith("HIGH") and entropy_note(3.0, 1000) == "normal"

    # strings: ascii + utf16le, min length
    blob = b"\x00\x01abcd\x00xy\xff" + "wide!".encode("utf-16-le") + b"\xff"
    got = extract_strings(blob, 4)
    assert (2, "ascii", "abcd") in got and (10, "utf16le", "wide!") in got
    assert all(t != "xy" for _, _, t in got)
    assert any(t == "xy" for _, _, t in extract_strings(blob, 2))

    # ELF protections on synthetic buffers
    full = build_elf64(pie=True)
    info = parse_elf(full)
    assert (info["bits"], info["endian"], info["machine"]) == (64, "little", "x86-64")
    assert info["interp"] == "/lib64/ld-linux-x86-64.so.2" and info["entry"] == 0x401000
    assert [s.name for s in info["sections"]] == ["", ".dynstr", ".shstrtab"]
    prot = elf_protections(full, info)
    assert prot["NX"] == "enabled" and prot["PIE"] == "enabled" and prot["RELRO"] == "full"
    assert prot["Stack canary"] == "found" and prot["FORTIFY"].startswith("yes")
    weak = build_elf64(nx=False, relro="none", canary=False, fortify=False)
    prot = elf_protections(weak, parse_elf(weak))
    assert prot["NX"].startswith("disabled") and prot["PIE"].startswith("disabled")
    assert prot["RELRO"] == "none" and prot["Stack canary"] == "not found"
    assert prot["FORTIFY"].startswith("no")
    partial = build_elf64(relro="partial")
    assert elf_protections(partial, parse_elf(partial))["RELRO"] == "partial"
    text = report(full, "synthetic-elf")
    assert "ELF64" in text and "-- protections --" in text and "__printf_chk" in text

    # truncated / malformed ELF must not crash the report
    assert "ELF parse error" in report(full[:30], "trunc")
    try:
        parse_elf(b"MZ")
        raise AssertionError("parse_elf accepted non-ELF")
    except ValueError:
        pass

    # PE
    pe = parse_pe(build_pe())
    assert pe["kind"] == "PE32+" and pe["machine"] == "x86-64" and pe["subsystem"] == "Windows console"
    assert [s.name for s in pe["sections"]] == [".text"]
    assert "-- PE --" in report(build_pe(), "synthetic-pe")

    # real files in the repo: recon.py itself, and this file
    here = Path(__file__).resolve()
    for target in (here.parent.parent / "recon.py", here):
        text = report(target.read_bytes(), str(target), max_strings=5)
        assert "type:   ASCII text" in text or "script" in text
        assert "sha256" in text and "-- strings" in text
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["/nonexistent/definitely-missing"]) == 1
