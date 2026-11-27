"""NAND backups: find, validate, archive, verify, and build an SD rescue kit.

Sources for the formats:

* keys.bin layout (0x400 bytes; OTP at 0x100, NG/console id at 0x124, SEEPROM at
  0x200): wiibrew "Bootmii/NAND dump format", Dolphin ``IOS/IOSC.cpp``
  (``BootMiiKeyDump``).
* nand.bin sizes: Dolphin ``DiscIO/NANDImporter.cpp`` - 0x21000000 (pages with
  spare/ECC) or 0x21000400 (keys appended). 0x20000000 (no spare) is accepted
  here as a bare dump but BootMii can't restore it.
* nanddumper@ios (github.com/Naim2000/nanddumper_ios): writes
  ``/wii/backups/<YYMMDD>_<serial>_nand_<NN>.bin`` (keys appended), ``<serial>_keys.bin``,
  ``<nand>.bin.sha1`` (raw 20-byte digest) and ``sha1sums.txt`` (``<hex> *<name>``).
* Wii U NAND dumper (github.com/koolkdev/wiiu-nanddumper): ``otp.bin``,
  ``seeprom.bin``, ``slc.bin``, ``slccmpt.bin``, ``mlc.bin.partNN`` at the SD root.

Key material is never logged or returned in text form; only the console id
(the NG id, which BootMii itself prints) is exposed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Settings
from .layout import DriveLayout
from .fileops import copy_atomic, require_drive, sha1_file
from .progress import CancelToken, ProgressFn, Reporter

KEYS_SIZE = 0x400
NAND_SIZE_NO_ECC = 0x20000000          # 536,870,912 (pages without spare)
NAND_SIZE_ECC = 0x21000000             # 553,648,128 (pages + 64 B spare each)
NAND_SIZE_ECC_KEYS = 0x21000400        # 553,649,152 (BootMii >= 1.0, nanddumper@ios)
NAND_PAGE = 0x800
NAND_SPARE = 0x40
NAND_PAGES_PER_BLOCK = 64
CONSOLE_ID_OFFSET = 0x124              # OTP 0x24 inside keys.bin
OTP_OFFSET = 0x100
SUPERBLOCK_START = 0x1FC00000          # in the de-spared image
SUPERBLOCK_SIZE = 0x40000
SUPERBLOCK_COUNT = 16

VWII_FILES = ("otp.bin", "seeprom.bin", "slc.bin", "slccmpt.bin")
VWII_SIZES = {"otp.bin": 0x400, "seeprom.bin": 0x200, "slc.bin": NAND_SIZE_ECC,
              "slccmpt.bin": NAND_SIZE_ECC}

# BootMii SD files as hosted by wii.hacks.guide (pinned; verified 2026-11-25).
BOOTMII_SD_ZIP_URL = "https://wii.hacks.guide/assets/files/bootmii_sd_files.zip"
BOOTMII_SD_ZIP_SHA256 = "02973f1403f4de00c5f615eaa99ccc9e032476b378f1a18f62d64a056628e9e0"
BOOTMII_FILE_SHA1 = {
    "armboot.bin": "7302dff9ca4d5f025606c4b37aa6f94c2a5a3c7d",
    "ppcboot.elf": "24e832147bff46bb8df81f0feda388e243e5d70e",
}
BOOTMII_REQUIRED = ("armboot.bin", "ppcboot.elf")

MANIFEST = "manifest.json"
SHA1SUMS = "sha1sums.txt"


class NandError(Exception):
    """A NAND dump is invalid or an operation is unsafe."""


# --------------------------------------------------------------------------- helpers
def _sha1(path: Path, rep: Reporter | None = None) -> str:
    return sha1_file(path, rep)


def _copy(src: Path, dest: Path, rep: Reporter) -> str:
    """Atomic, fsynced copy returning the SHA-1 of the copied bytes."""
    return copy_atomic(src, dest, rep, want_hash=True, fsync=True)


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def console_id_label(console_id: int | None) -> str:
    """Folder/label form of a console id (8 hex digits) or ``unknown``."""
    return f"{console_id:08x}" if console_id is not None else "unknown"


# --------------------------------------------------------------------------- keys
@dataclass
class KeysInfo:
    """Non-secret facts about a 0x400-byte keys blob."""
    console_id: int
    tool: str                # "bootmii", "nanddumper@ios" or "unknown"
    model: str = ""          # from the nanddumper@ios header, if present
    serial: str = ""         # from the nanddumper@ios header, if present

    @property
    def label(self) -> str:
        return console_id_label(self.console_id)


def parse_keys(data: bytes) -> KeysInfo:
    """Parse a BootMii/nanddumper keys blob (exactly 0x400 bytes)."""
    if len(data) != KEYS_SIZE:
        raise NandError(f"keys.bin must be {KEYS_SIZE} bytes, got {len(data)}")
    cid = struct.unpack_from(">I", data, CONSOLE_ID_OFFSET)[0]
    if data[OTP_OFFSET:OTP_OFFSET + 0x80] == b"\x00" * 0x80 or \
            data[OTP_OFFSET:OTP_OFFSET + 0x80] == b"\xff" * 0x80:
        raise NandError("keys.bin has an empty OTP area")
    text = data[:0x100].split(b"\x00", 1)[0].decode("ascii", "replace")
    tool, model, serial = "unknown", "", ""
    if text.startswith("BackupMii"):
        tool = "bootmii"
    elif text.startswith("nanddumper@IOS"):
        tool = "nanddumper@ios"
        m = re.search(r"Console model:\s*([^\n]*)", text)
        model = m.group(1).strip() if m else ""
        m = re.search(r"Serial number:\s*([^\n]*)", text)
        serial = m.group(1).strip() if m else ""
    return KeysInfo(cid, tool, model, serial)


def read_keys(path: Path) -> KeysInfo:
    """Read and parse a keys.bin file."""
    return parse_keys(Path(path).read_bytes())


def read_embedded_keys(nand: Path) -> Optional[KeysInfo]:
    """Keys appended to a 0x21000400-byte nand.bin, if present."""
    nand = Path(nand)
    if nand.stat().st_size != NAND_SIZE_ECC_KEYS:
        return None
    with open(nand, "rb") as f:
        f.seek(NAND_SIZE_ECC)
        try:
            return parse_keys(f.read(KEYS_SIZE))
        except NandError:
            return None


# --------------------------------------------------------------------------- discovery
@dataclass
class NandDump:
    """A Wii NAND dump found on a drive (or chosen by the user)."""
    nand: Path
    keys: Optional[Path]
    tool: str                       # "bootmii" | "nanddumper@ios"
    size: int
    serial: str = ""                # from nanddumper@ios names
    date: str = ""                  # YYMMDD from nanddumper@ios names
    sha1_file: Optional[Path] = None      # <nand>.bin.sha1
    sha1sums: Optional[Path] = None       # sha1sums.txt
    bootmii_dir: Optional[Path] = None    # bootmii/ with armboot.bin + ppcboot.elf

    @property
    def size_kind(self) -> str:
        return {NAND_SIZE_ECC_KEYS: "ecc+keys", NAND_SIZE_ECC: "ecc",
                NAND_SIZE_NO_ECC: "no-ecc"}.get(self.size, "invalid")


@dataclass
class VwiiDump:
    """Files written by the Wii U NAND dumper."""
    root: Path
    files: dict[str, Path]          # name -> path (incl. mlc.bin.partNN)

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.files.values())


_NANDDUMPER = re.compile(r"^(\d{6})_(.+)_nand_(\d{2})\.bin$", re.I)


def _has_bootmii(d: Path) -> bool:
    return all((d / n).is_file() for n in BOOTMII_REQUIRED)


def find_nand_dumps(root: Path) -> list[NandDump]:
    """Find BootMii and nanddumper@ios NAND dumps on a drive root."""
    root = Path(root)
    lay = DriveLayout(root)
    bm = lay.bootmii if _has_bootmii(lay.bootmii) else None
    out: list[NandDump] = []
    for d in (root, lay.bootmii):
        n = d / "nand.bin"
        if n.is_file():
            k = d / "keys.bin"
            here = d / "bootmii" if _has_bootmii(d / "bootmii") else bm
            out.append(NandDump(n, k if k.is_file() else None, "bootmii", n.stat().st_size,
                                bootmii_dir=here))
    bdir = lay.nand_dumps
    if bdir.is_dir():
        files = {p.name.lower(): p for p in bdir.iterdir() if p.is_file()}
        sums = files.get(SHA1SUMS)
        for p in sorted(bdir.iterdir()):
            m = _NANDDUMPER.match(p.name)
            if not m or not p.is_file():
                continue
            date, serial = m.group(1), m.group(2)
            # only this console's keys: never pair a dump with some other *keys.bin
            keys = files.get(f"{serial}_keys.bin".lower())
            out.append(NandDump(p, keys, "nanddumper@ios", p.stat().st_size, serial=serial,
                                date=date, sha1_file=files.get(p.name.lower() + ".sha1"),
                                sha1sums=sums, bootmii_dir=bm))
        # renamed per the guide (nand.bin / keys.bin inside wii/backups)
        n = bdir / "nand.bin"
        if n.is_file() and not any(x.nand == n for x in out):
            k = bdir / "keys.bin"
            out.append(NandDump(n, k if k.is_file() else None, "nanddumper@ios",
                                n.stat().st_size, sha1sums=sums, bootmii_dir=bm))
    return out


def find_vwii_dumps(root: Path) -> list[VwiiDump]:
    """Find Wii U NAND dumper output (slc/slccmpt/otp/seeprom/mlc parts) at a root."""
    root = Path(root)
    out: list[VwiiDump] = []
    for d in (root, root / "wiiu" / "backups", root / "nanddumper"):
        if not d.is_dir():
            continue
        files = {n: d / n for n in VWII_FILES if (d / n).is_file()}
        files.update({p.name: p for p in sorted(d.glob("mlc.bin.part*")) if p.is_file()})
        if "slccmpt.bin" in files or "slc.bin" in files:
            out.append(VwiiDump(d, files))
    return out


# --------------------------------------------------------------------------- validation
@dataclass
class Validation:
    """Outcome of :func:`validate_dump`."""
    ok: bool
    console_id: Optional[int]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sha1: str = ""
    sha1_expected: str = ""
    sha1_matches: Optional[bool] = None   # None = no checksum file to compare
    superblocks: int = 0                  # SFFS superblocks found

    @property
    def console_label(self) -> str:
        return console_id_label(self.console_id)


def expected_sha1(dump: NandDump) -> str:
    """Checksum shipped with the dump (.sha1 raw/hex or sha1sums.txt), or ""."""
    if dump.sha1_file and dump.sha1_file.is_file():
        raw = dump.sha1_file.read_bytes()
        if len(raw) == 20:
            return raw.hex()
        m = re.search(rb"[0-9a-fA-F]{40}", raw)
        if m:
            return m.group(0).decode().lower()
    if dump.sha1sums and dump.sha1sums.is_file():
        for line in dump.sha1sums.read_text("utf-8", "replace").splitlines():
            ml = re.match(r"\s*([0-9a-fA-F]{40})\s+\*?(.+?)\s*$", line)
            if ml and ml.group(2).lower() == dump.nand.name.lower():
                return ml.group(1).lower()
    return ""


def count_superblocks(nand: Path) -> int:
    """Number of SFFS superblocks (magic ``SFFS``) in a dump; >0 means plausible."""
    size = Path(nand).stat().st_size
    found = 0
    with open(nand, "rb") as f:
        for i in range(SUPERBLOCK_COUNT):
            off = SUPERBLOCK_START + i * SUPERBLOCK_SIZE
            if size >= NAND_SIZE_ECC:
                off = off // NAND_PAGE * (NAND_PAGE + NAND_SPARE)
            if off + 4 > size:
                break
            f.seek(off)
            if f.read(4) == b"SFFS":
                found += 1
    return found


def validate_dump(dump: NandDump, *, check_hash: bool = True,
                  progress: ProgressFn | None = None,
                  cancel: CancelToken | None = None) -> Validation:
    """Check size, keys, console id consistency, SFFS superblocks and SHA-1."""
    v = Validation(ok=True, console_id=None)
    size = dump.nand.stat().st_size
    if size not in (NAND_SIZE_ECC_KEYS, NAND_SIZE_ECC, NAND_SIZE_NO_ECC):
        v.errors.append(f"nand.bin is {size:,} bytes; expected {NAND_SIZE_ECC_KEYS:,} "
                        f"or {NAND_SIZE_ECC:,}")
    elif size == NAND_SIZE_NO_ECC:
        v.warnings.append("nand.bin has no ECC/spare data; BootMii can't restore it")
    embedded = read_embedded_keys(dump.nand) if size == NAND_SIZE_ECC_KEYS else None
    if size == NAND_SIZE_ECC_KEYS and embedded is None:
        v.errors.append("the keys appended to nand.bin are invalid")
    sep: Optional[KeysInfo] = None
    if dump.keys is not None:
        try:
            sep = read_keys(dump.keys)
        except (OSError, NandError) as e:
            v.errors.append(f"keys.bin: {e}")
    elif embedded is None:
        v.errors.append("keys.bin is missing (needed to restore or decrypt this NAND)")
    if sep and embedded and sep.console_id != embedded.console_id:
        v.errors.append("keys.bin belongs to a different console than nand.bin")
    k = embedded or sep
    v.console_id = k.console_id if k else None
    if size in (NAND_SIZE_ECC_KEYS, NAND_SIZE_ECC, NAND_SIZE_NO_ECC):
        v.superblocks = count_superblocks(dump.nand)
        if v.superblocks == 0:
            v.errors.append("no NAND filesystem superblocks found; the dump looks corrupt")
    v.sha1_expected = expected_sha1(dump)
    if check_hash:
        rep = Reporter(progress, cancel, total=size, message=f"Hashing {dump.nand.name}")
        v.sha1 = _sha1(dump.nand, rep)
        if v.sha1_expected:
            v.sha1_matches = v.sha1 == v.sha1_expected
            if not v.sha1_matches:
                v.errors.append("SHA-1 does not match the checksum file; the dump is damaged")
    v.ok = not v.errors
    return v


# --------------------------------------------------------------------------- archive
@dataclass
class ArchivedNand:
    """A NAND backup stored in the library."""
    dir: Path
    console_id: str            # 8 hex digits or "unknown"
    kind: str                  # "wii" | "vwii"
    created: str
    files: dict[str, str]      # name -> sha1
    source: str = ""
    tool: str = ""
    serial: str = ""

    @property
    def nand(self) -> Path:
        return self.dir / "nand.bin"

    @property
    def keys(self) -> Path:
        return self.dir / "keys.bin"

    @property
    def bootmii_dir(self) -> Optional[Path]:
        d = self.dir / "bootmii"
        return d if _has_bootmii(d) else None


def _write_manifest(dest: Path, meta: dict, hashes: dict[str, str]) -> None:
    meta = dict(meta, files=hashes)
    (dest / MANIFEST).write_text(json.dumps(meta, indent=2), "utf-8")
    (dest / SHA1SUMS).write_text("".join(f"{h} *{n}\n" for n, h in sorted(hashes.items())),
                                 "utf-8")


def _read_archive(d: Path) -> Optional[ArchivedNand]:
    try:
        m = json.loads((d / MANIFEST).read_text("utf-8"))
        return ArchivedNand(d, m["console_id"], m.get("kind", "wii"), m.get("created", ""),
                            dict(m["files"]), m.get("source", ""), m.get("tool", ""),
                            m.get("serial", ""))
    except (OSError, ValueError, KeyError):
        return None


def list_archives(settings: Settings, console_id: str | None = None) -> list[ArchivedNand]:
    """All archived NAND backups (oldest first), optionally for one console."""
    root = settings.nand_backups_dir
    out: list[ArchivedNand] = []
    if not root.is_dir():
        return out
    for cdir in sorted(root.iterdir()):
        if not cdir.is_dir() or (console_id and cdir.name != console_id):
            continue
        for d in sorted(cdir.iterdir()):
            a = _read_archive(d) if d.is_dir() else None
            if a:
                out.append(a)
    return out


def _new_dir(parent: Path, now: datetime | None) -> Path:
    d = parent / _timestamp(now)
    i = 2
    while d.exists():
        d = parent / f"{_timestamp(now)}-{i}"
        i += 1
    return d


def archive_dump(dump: NandDump, settings: Settings, *, include_bootmii: bool = True,
                 now: datetime | None = None, progress: ProgressFn | None = None,
                 cancel: CancelToken | None = None) -> tuple[ArchivedNand, bool]:
    """Copy a validated dump into ``nand_backups_dir/<console-id>/<timestamp>/``.

    Stores ``nand.bin``, ``keys.bin`` (from the separate file, or extracted from
    the end of nand.bin), optionally the drive's ``bootmii/`` files, plus a
    ``manifest.json`` and ``sha1sums.txt``. Returns ``(archive, created)``;
    ``created`` is False when an identical nand.bin is already archived.
    Raises :class:`NandError` if the dump fails validation.
    """
    v = validate_dump(dump, check_hash=True, progress=progress, cancel=cancel)
    if not v.ok:
        raise NandError("; ".join(v.errors))
    label = v.console_label
    for a in list_archives(settings, label):
        if a.files.get("nand.bin") == v.sha1:
            return a, False
    cdir = settings.nand_backups_dir / label
    cdir.mkdir(parents=True, exist_ok=True)
    final = _new_dir(cdir, now)
    tmp = final.with_name(final.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    total = dump.size + (dump.keys.stat().st_size if dump.keys else 0)
    rep = Reporter(progress, cancel, total=total, message="Archiving NAND backup")
    try:
        hashes = {"nand.bin": _copy(dump.nand, tmp / "nand.bin", rep)}
        if hashes["nand.bin"] != v.sha1:
            raise NandError("nand.bin changed while it was being copied")
        if dump.keys is not None:
            hashes["keys.bin"] = _copy(dump.keys, tmp / "keys.bin", rep)
        else:
            with open(dump.nand, "rb") as f:
                f.seek(NAND_SIZE_ECC)
                kb = f.read(KEYS_SIZE)
            (tmp / "keys.bin").write_bytes(kb)
            hashes["keys.bin"] = hashlib.sha1(kb).hexdigest()
        for extra in (dump.sha1_file,):
            if extra is not None and extra.is_file():
                shutil.copy2(extra, tmp / extra.name)
        if include_bootmii and dump.bootmii_dir is not None:
            (tmp / "bootmii").mkdir()
            for p in sorted(dump.bootmii_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in (".bin", ".elf", ".ini"):
                    shutil.copy2(p, tmp / "bootmii" / p.name)
                    hashes[f"bootmii/{p.name}"] = _sha1(tmp / "bootmii" / p.name)
        meta = {"console_id": label, "kind": "wii",
                "created": (now or datetime.now()).isoformat(timespec="seconds"),
                "source": str(dump.nand), "tool": dump.tool, "serial": dump.serial,
                "nand_size": dump.size, "superblocks": v.superblocks}
        _write_manifest(tmp, meta, hashes)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    got = _read_archive(final)
    assert got is not None
    return got, True


def archive_vwii_dump(dump: VwiiDump, settings: Settings, *, now: datetime | None = None,
                      progress: ProgressFn | None = None,
                      cancel: CancelToken | None = None) -> ArchivedNand:
    """Collect + hash Wii U NAND dumper files into ``nand_backups_dir/wiiu-<id>/<ts>/``."""
    cid: Optional[int] = None
    otp = dump.files.get("otp.bin")
    if otp is not None and otp.stat().st_size >= 0x28:
        with open(otp, "rb") as f:
            f.seek(0x24)              # Wii-compatible bank: NG id at 0x24
            cid = struct.unpack(">I", f.read(4))[0]
    label = f"wiiu-{console_id_label(cid)}"
    cdir = settings.nand_backups_dir / label
    cdir.mkdir(parents=True, exist_ok=True)
    final = _new_dir(cdir, now)
    tmp = final.with_name(final.name + ".partial")
    tmp.mkdir()
    rep = Reporter(progress, cancel, total=dump.size, message="Archiving Wii U NAND dump")
    try:
        hashes = {n: _copy(p, tmp / n, rep) for n, p in sorted(dump.files.items())}
        _write_manifest(tmp, {"console_id": label, "kind": "vwii",
                              "created": (now or datetime.now()).isoformat(timespec="seconds"),
                              "source": str(dump.root), "tool": "wiiu-nanddumper"}, hashes)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    a = _read_archive(final)
    assert a is not None
    return a


def verify_vwii_dump(dump: VwiiDump) -> list[str]:
    """Size problems with a Wii U NAND dump (empty list = looks complete)."""
    probs = []
    for n, want in VWII_SIZES.items():
        p = dump.files.get(n)
        if p is None:
            probs.append(f"{n} is missing")
        elif p.stat().st_size != want:
            probs.append(f"{n} is {p.stat().st_size:,} bytes, expected {want:,}")
    return probs


def verify_archive(archive: ArchivedNand, progress: ProgressFn | None = None,
                   cancel: CancelToken | None = None) -> list[str]:
    """Re-hash every file in an archived backup; returns problems (empty = good)."""
    probs: list[str] = []
    total = sum((archive.dir / n).stat().st_size for n in archive.files
                if (archive.dir / n).is_file())
    rep = Reporter(progress, cancel, total=total, message="Verifying NAND backup")
    for name, want in sorted(archive.files.items()):
        p = archive.dir / name
        if not p.is_file():
            probs.append(f"{name} is missing")
        elif _sha1(p, rep) != want:
            probs.append(f"{name} is damaged (SHA-1 mismatch)")
    return probs


# --------------------------------------------------------------------------- rescue kit
RESCUE_README = """\
ReWii SD rescue kit - BootMii NAND restore
==========================================

Console ID of this backup: {console}
Backup made: {created}

WARNING: Only restore this backup to the SAME Wii it came from (console ID
{console}). Restoring another console's NAND WILL BRICK your Wii.
BootMii restore does not work on a Wii U (vWii) or a Wii mini.

How to restore:
1. Put this SD card in the Wii (it must contain nand.bin, keys.bin and bootmii/).
2. Start BootMii (boot2: power on; or from the Homebrew Channel: Home > Launch BootMii).
3. Use the Power/Reset buttons (or a GameCube controller) to choose the
   "Settings" (gears) icon, then the "Restore" icon (blue arrow towards the Wii).
4. Wait until it finishes. Do not turn off the Wii during the restore.

SHA-1 of nand.bin: {sha1}
"""


@dataclass
class RescueKit:
    """Files placed by :func:`build_rescue_kit`."""
    target: Path
    files: list[Path]
    warnings: list[str]


def build_rescue_kit(archive: ArchivedNand, target: Path, *,
                     bootmii_dir: Path | None = None,
                     target_keys: Path | None = None,
                     overwrite: bool = False,
                     progress: ProgressFn | None = None,
                     cancel: CancelToken | None = None) -> RescueKit:
    """Prepare an SD card root with nand.bin, keys.bin, bootmii/ and a README.

    * Refuses vWii archives (BootMii restore isn't possible on a Wii U).
    * If ``target_keys`` (the console's own keys.bin) is given, refuses when its
      console id differs from the backup's.
    * BootMii files come from ``bootmii_dir`` or the archive's saved copy; if
      neither exists, raises (see :func:`fetch_bootmii_sd_files`).
    * Refuses to replace an existing different nand.bin/keys.bin unless
      ``overwrite`` is set.
    """
    if archive.kind != "wii":
        raise NandError("BootMii can't restore a Wii U (vWii) NAND")
    if not archive.nand.is_file() or not archive.keys.is_file():
        raise NandError("this backup is missing nand.bin or keys.bin")
    if archive.nand.stat().st_size == NAND_SIZE_NO_ECC:
        raise NandError("this NAND dump has no ECC data, so BootMii can't restore it")
    # trust the key file itself, not the label written in the manifest
    real_id = read_keys(archive.keys).label
    if real_id != archive.console_id:
        raise NandError(f"this backup is filed under console {archive.console_id} but its keys.bin "
                        f"belongs to console {real_id}; not building a kit from it")
    warnings = ["Restoring another console's NAND bricks the Wii. Only use this kit "
                f"on the console with ID {archive.console_id}."]
    if target_keys is not None:
        tk = read_keys(target_keys)
        if tk.label != archive.console_id:
            raise NandError(f"this backup is from console {archive.console_id}, but the target "
                            f"console is {tk.label}; restoring it would brick the Wii")
    else:
        warnings.append("No keys.bin from the target console was given, so ReWii could not "
                        "confirm this backup belongs to it.")
    bm = Path(bootmii_dir) if bootmii_dir else archive.bootmii_dir
    if bm is None or not _has_bootmii(bm):
        raise NandError("BootMii files (armboot.bin, ppcboot.elf) are needed: point ReWii at a "
                        "bootmii folder or download them first")
    probs = verify_archive(archive, progress, cancel)
    if probs:
        raise NandError("the archived backup failed verification: " + "; ".join(probs))
    target = require_drive(target)          # the card must be there; never create it
    plan = [(archive.nand, target / "nand.bin"), (archive.keys, target / "keys.bin")]
    plan += [(p, target / "bootmii" / p.name) for p in sorted(bm.iterdir())
             if p.is_file() and p.suffix.lower() in (".bin", ".elf", ".ini")]
    todo = []
    for src, dst in plan:
        if dst.is_file():
            if dst.stat().st_size == src.stat().st_size and _sha1(dst) == _sha1(src):
                continue                    # already on the card, identical: nothing to write
            if not overwrite:
                raise NandError(f"{dst} already exists and is different; refusing to replace it")
        todo.append((src, dst))
    # each file is written next to the one it replaces, so count every file in full
    need = sum(s.stat().st_size for s, _ in todo) + (1 << 20 if todo else 0)
    free = shutil.disk_usage(target).free
    if need > free:
        raise NandError(f"Not enough free space on the SD card: the rescue kit needs "
                        f"{need / 1e6:.0f} MB but only {free / 1e6:.0f} MB is free.")
    rep = Reporter(progress, cancel, total=sum(s.stat().st_size for s, _ in todo),
                   message="Writing rescue kit")
    for src, dst in todo:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy(src, dst, rep)
    files = [dst for _, dst in plan]
    readme = target / "REWII-RESCUE-README.txt"
    readme.write_text(RESCUE_README.format(console=archive.console_id, created=archive.created,
                                           sha1=archive.files.get("nand.bin", "?")), "utf-8")
    files.append(readme)
    if not (bm / "bootmii.ini").is_file():
        warnings.append("No bootmii.ini was included; BootMii will use its defaults.")
    return RescueKit(target, files, warnings)


def fetch_bootmii_sd_files(dest: Path, *, progress: ProgressFn | None = None,
                           cancel: CancelToken | None = None) -> Path:
    """Download the BootMii SD files (wii.hacks.guide copy) into ``dest/bootmii``.

    The zip and each extracted file are checked against pinned hashes. Returns
    the ``bootmii`` folder. Raises :class:`NandError` on any mismatch.
    """
    from . import net
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / "bootmii_sd_files.zip"
    net.download(BOOTMII_SD_ZIP_URL, zpath, progress=progress, cancel=cancel)
    try:
        if hashlib.sha256(zpath.read_bytes()).hexdigest() != BOOTMII_SD_ZIP_SHA256:
            raise NandError("downloaded BootMii files don't match the expected checksum")
        out = dest / "bootmii"
        out.mkdir(exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            for name, want in BOOTMII_FILE_SHA1.items():
                data = z.read(f"bootmii/{name}")
                if hashlib.sha1(data).hexdigest() != want:
                    raise NandError(f"{name} in the BootMii download has the wrong checksum")
                (out / name).write_bytes(data)
        return out
    finally:
        zpath.unlink(missing_ok=True)
