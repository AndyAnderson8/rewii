"""Tests for rewii.core.nand with tiny synthetic dumps (geometry shrunk via monkeypatch)."""
from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from rewii.core import nand as N
from rewii.core import net
from rewii.core.config import Settings

PAGES = 256
NO_ECC = PAGES * 0x800
ECC = PAGES * 0x840


@pytest.fixture(autouse=True)
def small_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(N, "NAND_SIZE_NO_ECC", NO_ECC)
    monkeypatch.setattr(N, "NAND_SIZE_ECC", ECC)
    monkeypatch.setattr(N, "NAND_SIZE_ECC_KEYS", ECC + 0x400)
    monkeypatch.setattr(N, "SUPERBLOCK_START", NO_ECC - 16 * 0x1000)
    monkeypatch.setattr(N, "SUPERBLOCK_SIZE", 0x1000)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(library_dir=str(tmp_path / "lib"))


def make_keys(console_id: int, header: bytes = b"BackupMii v1, ConsoleID: x\n") -> bytes:
    k = bytearray(0x400)
    k[:len(header)] = header
    k[0x100:0x180] = bytes((i * 7 + 1) & 0xFF for i in range(0x80))   # fake OTP
    struct.pack_into(">I", k, 0x124, console_id)
    return bytes(k)


def make_nand(console_id: int | None = None, superblocks: bool = True) -> bytes:
    data = bytearray(ECC)
    if superblocks:
        for i in range(16):
            off = (N.SUPERBLOCK_START + i * N.SUPERBLOCK_SIZE) // 0x800 * 0x840
            data[off:off + 4] = b"SFFS"
    data[0:4] = b"\x01\x02\x03\x04"
    if console_id is not None:
        data += make_keys(console_id)
    return bytes(data)


def make_bootmii(d: Path) -> Path:
    b = d / "bootmii"
    b.mkdir(parents=True)
    (b / "armboot.bin").write_bytes(b"arm")
    (b / "ppcboot.elf").write_bytes(b"ppc")
    (b / "bootmii.ini").write_text("AUTOBOOT=SYSMENU\n")
    return b


def test_parse_keys() -> None:
    k = N.parse_keys(make_keys(0x0408CAFA))
    assert k.console_id == 0x0408CAFA and k.label == "0408cafa" and k.tool == "bootmii"
    nd = N.parse_keys(make_keys(5, b"nanddumper@IOS by x\nConsole model: RVL-001 [x]\n"
                                   b"Console ID: 00000005\nSerial number: LU123\n"))
    assert nd.tool == "nanddumper@ios" and nd.serial == "LU123"
    with pytest.raises(N.NandError):
        N.parse_keys(b"\0" * 0x400)
    with pytest.raises(N.NandError):
        N.parse_keys(b"\0" * 10)


def test_find_and_validate_bootmii(tmp_path: Path) -> None:
    (tmp_path / "nand.bin").write_bytes(make_nand())
    (tmp_path / "keys.bin").write_bytes(make_keys(0x11223344))
    make_bootmii(tmp_path)
    [d] = N.find_nand_dumps(tmp_path)
    assert d.tool == "bootmii" and d.size_kind == "ecc" and d.bootmii_dir is not None
    v = N.validate_dump(d)
    assert v.ok, v.errors
    assert v.console_label == "11223344" and v.superblocks == 16 and v.sha1_matches is None


def test_validate_problems(tmp_path: Path) -> None:
    (tmp_path / "nand.bin").write_bytes(make_nand(0x1, superblocks=False))
    (tmp_path / "keys.bin").write_bytes(make_keys(0x2))
    [d] = N.find_nand_dumps(tmp_path)
    v = N.validate_dump(d)
    assert not v.ok
    assert any("different console" in e for e in v.errors)
    assert any("superblocks" in e for e in v.errors)
    (tmp_path / "nand.bin").write_bytes(b"\0" * 1000)
    (tmp_path / "keys.bin").unlink()
    v = N.validate_dump(N.find_nand_dumps(tmp_path)[0])
    assert any("bytes" in e for e in v.errors) and any("missing" in e for e in v.errors)


def test_nanddumper_naming_and_sha1(tmp_path: Path) -> None:
    b = tmp_path / "wii" / "backups"
    b.mkdir(parents=True)
    data = make_nand(0xABCDEF01)
    name = "251125_LU123456789_nand_00.bin"
    (b / name).write_bytes(data)
    (b / "LU123456789_keys.bin").write_bytes(make_keys(0xABCDEF01))
    (b / (name + ".sha1")).write_bytes(hashlib.sha1(data).digest())
    (b / "sha1sums.txt").write_text(f"{hashlib.sha1(data).hexdigest()} *{name}\n")
    [d] = N.find_nand_dumps(tmp_path)
    assert d.tool == "nanddumper@ios" and d.serial == "LU123456789" and d.date == "251125"
    assert d.size_kind == "ecc+keys"
    v = N.validate_dump(d)
    assert v.ok and v.sha1_matches is True
    (b / (name + ".sha1")).write_bytes(b"\0" * 20)
    v = N.validate_dump(d)
    assert not v.ok and v.sha1_matches is False
    (b / (name + ".sha1")).unlink()
    d = N.find_nand_dumps(tmp_path)[0]
    assert N.expected_sha1(d) == hashlib.sha1(data).hexdigest()   # from sha1sums.txt


def test_archive_verify_and_rescue_kit(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "nand.bin").write_bytes(make_nand(0x0408CAFA))     # keys appended only
    make_bootmii(drive)
    [d] = N.find_nand_dumps(drive)
    a, created = N.archive_dump(d, settings, now=datetime(2026, 2, 3, 4, 5, 6))
    assert created
    assert a.dir == settings.nand_backups_dir / "0408cafa" / "20260203-040506"
    assert a.keys.read_bytes() == make_keys(0x0408CAFA)          # extracted from nand.bin
    assert (a.dir / "sha1sums.txt").read_text().count("\n") == len(a.files)
    assert a.bootmii_dir is not None
    assert N.verify_archive(a) == []
    again, created2 = N.archive_dump(d, settings)
    assert not created2 and again.dir == a.dir
    assert [x.dir for x in N.list_archives(settings)] == [a.dir]

    sd = tmp_path / "sd"
    sd.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    (tmp_path / "mykeys.bin").write_bytes(make_keys(0x0408CAFA))
    kit = N.build_rescue_kit(a, sd, target_keys=tmp_path / "mykeys.bin")
    assert (sd / "nand.bin").read_bytes() == make_nand(0x0408CAFA)
    assert (sd / "keys.bin").is_file() and (sd / "bootmii" / "armboot.bin").is_file()
    assert (sd / "REWII-RESCUE-README.txt").read_text().count("0408cafa") >= 2
    assert any("brick" in w for w in kit.warnings)

    # wrong console -> refuse
    (tmp_path / "other.bin").write_bytes(make_keys(0x99999999))
    with pytest.raises(N.NandError, match="brick"):
        N.build_rescue_kit(a, tmp_path / "sd2", target_keys=tmp_path / "other.bin")
    # damaged archive -> refuse
    with open(a.nand, "r+b") as f:
        f.write(b"XXXX")
    assert N.verify_archive(a)
    with pytest.raises(N.NandError, match="verification"):
        N.build_rescue_kit(a, tmp_path / "sd3")


def test_rescue_kit_requires_bootmii(tmp_path: Path, settings: Settings) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "nand.bin").write_bytes(make_nand(0x5))
    a, _ = N.archive_dump(N.find_nand_dumps(drive)[0], settings)
    with pytest.raises(N.NandError, match="BootMii files"):
        N.build_rescue_kit(a, tmp_path / "sd")
    bm = make_bootmii(tmp_path / "user")
    (tmp_path / "sd").mkdir()                 # the card is in the reader
    kit = N.build_rescue_kit(a, tmp_path / "sd", bootmii_dir=bm)
    assert (tmp_path / "sd" / "bootmii" / "ppcboot.elf").is_file()
    assert any("No keys.bin" in w for w in kit.warnings)
    # existing different nand.bin on the target is not replaced silently
    (tmp_path / "sd" / "nand.bin").write_bytes(b"other")
    with pytest.raises(N.NandError, match="already exists"):
        N.build_rescue_kit(a, tmp_path / "sd", bootmii_dir=bm)


def test_vwii_dump(tmp_path: Path, settings: Settings) -> None:
    otp = bytearray(0x400)
    struct.pack_into(">I", otp, 0x24, 0xCAFE0001)
    (tmp_path / "otp.bin").write_bytes(bytes(otp))
    (tmp_path / "seeprom.bin").write_bytes(b"\0" * 0x200)
    (tmp_path / "slccmpt.bin").write_bytes(b"\1" * 100)
    (tmp_path / "mlc.bin.part01").write_bytes(b"\2" * 10)
    [v] = N.find_vwii_dumps(tmp_path)
    assert set(v.files) == {"otp.bin", "seeprom.bin", "slccmpt.bin", "mlc.bin.part01"}
    probs = N.verify_vwii_dump(v)
    assert any("slc.bin is missing" in p for p in probs)
    a = N.archive_vwii_dump(v, settings)
    assert a.console_id == "wiiu-cafe0001" and a.kind == "vwii"
    assert N.verify_archive(a) == []
    with pytest.raises(N.NandError):
        N.build_rescue_kit(a, tmp_path / "sd")


def test_fetch_bootmii_rejects_bad_download(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bootmii/armboot.bin", b"fake")
    payload = buf.getvalue()
    net.set_client(httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=payload))))
    try:
        with pytest.raises(N.NandError, match="checksum"):
            N.fetch_bootmii_sd_files(tmp_path)
        assert not (tmp_path / "bootmii_sd_files.zip").exists()
    finally:
        net.set_client(None)


def test_rescue_kit_skips_identical_files_and_checks_free_space(tmp_path: Path, settings: Settings,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import collections
    import shutil

    from rewii.core.fileops import DriveNotConnected
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "nand.bin").write_bytes(make_nand(0x5))
    a, _ = N.archive_dump(N.find_nand_dumps(drive)[0], settings)
    bm = make_bootmii(tmp_path / "user")
    sd = tmp_path / "sd"
    with pytest.raises(DriveNotConnected):                   # a card that isn't there is never created
        N.build_rescue_kit(a, sd, bootmii_dir=bm)
    assert not sd.exists()
    sd.mkdir()
    N.build_rescue_kit(a, sd, bootmii_dir=bm)
    copied: list[str] = []
    real = N._copy
    monkeypatch.setattr(N, "_copy", lambda s, d, r: (copied.append(d.name), real(s, d, r))[1])
    kit = N.build_rescue_kit(a, sd, bootmii_dir=bm)
    assert copied == [] and (sd / "nand.bin") in kit.files   # all identical: nothing rewritten
    (sd / "keys.bin").write_bytes(b"\0" * 1024)
    N.build_rescue_kit(a, sd, bootmii_dir=bm, overwrite=True)
    assert copied == ["keys.bin"]
    (sd / "nand.bin").unlink()
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 ** 9, 10 ** 9 - 1000, 1000))
    with pytest.raises(N.NandError, match="Not enough free space on the SD card"):
        N.build_rescue_kit(a, sd, bootmii_dir=bm)
    assert not (sd / "nand.bin").exists() and not list(sd.glob("*.rewii-part"))
