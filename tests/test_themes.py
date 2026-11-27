"""Tests for rewii.core.themes with synthetic U8 archives."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from rewii.core import themes as T


def build_u8(files: dict[str, bytes]) -> bytes:
    """Minimal U8 writer: files keyed by absolute path."""
    tree: dict = {}
    for path, data in files.items():
        node = tree
        parts = path.strip("/").split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = data
    nodes: list[list] = []          # [type, name, a, b]
    names = bytearray(b"\0")
    blobs: list[tuple[int, bytes]] = []

    def add_dir(name: str, content: dict, parent: int) -> None:
        idx = len(nodes)
        noff = len(names) if name else 0
        if name:
            names.extend(name.encode() + b"\0")
        nodes.append([1, noff, parent, 0])
        for k in sorted(content):
            v = content[k]
            if isinstance(v, dict):
                add_dir(k, v, idx)
            else:
                fo = len(names)
                names.extend(k.encode() + b"\0")
                nodes.append([0, fo, 0, len(v)])
                blobs.append((len(nodes) - 1, v))
        nodes[idx][3] = len(nodes)

    add_dir("", tree, 0)
    hdr_size = len(nodes) * 12 + len(names)
    data_off = (0x20 + hdr_size + 0x1F) & ~0x1F
    body = bytearray()
    for idx, v in blobs:
        nodes[idx][2] = data_off + len(body)
        body += v + b"\0" * (-len(v) % 0x20)
    out = bytearray(struct.pack(">IIII", T.U8_MAGIC, 0x20, hdr_size, data_off) + b"\0" * 16)
    for t, n, a, b in nodes:
        out += struct.pack(">I", (t << 24) | n) + struct.pack(">II", a, b)
    out += names
    out += b"\0" * (data_off - len(out))
    return bytes(out + body)


def theme(region: str = "US2", build: bytes = b"D:\\4_3\\ipl\\bin\\RVL\\Final_U\\main.elf",
          signed: bool = True) -> bytes:
    sel = bytearray(0x200)
    sel[0x40:0x40 + len(build)] = build
    if signed:
        sel[0x100:0x10A] = b"Wii_Themer"
    return build_u8({"/www.arc": b"w", "/layout/common/health.ash": b"h",
                     "/sound/IplSound.brsar": b"s", f"/html/{region}/iplsetting.ash": b"i",
                     f"/FINAL/{region}/main.sel": bytes(sel)})


def test_parse_u8_paths() -> None:
    files = T.parse_u8(build_u8({"/a.txt": b"A", "/d/e/f.bin": b"FFF", "/d/g": b"G"}))
    assert set(files) == {"/a.txt", "/d/e/f.bin", "/d/g"}
    data = build_u8({"/d/e/f.bin": b"FFF"})
    e = T.parse_u8(data)["/d/e/f.bin"]
    assert data[e.offset:e.offset + e.size] == b"FFF"
    with pytest.raises(T.ThemeError):
        T.parse_u8(b"nope" * 10)


def test_identify_theme(tmp_path: Path) -> None:
    p = tmp_path / "dark.csm"
    p.write_bytes(theme())
    i = T.identify_theme(p)
    assert i.is_theme and i.regions == ["U"] and i.platform == "wii"
    assert (i.major, i.minor) == (4, 3) and i.signed == "Wii" and i.version_label == "4.3U"
    p.write_bytes(theme("EU2", b"Compat_irdrepo\\ipl\\Compat\\x", signed=False))
    i = T.identify_theme(p)
    assert i.region_name == "PAL" and i.platform == "vwii" and not i.signed
    p.write_bytes(build_u8({"/x": b"1"}))
    i = T.identify_theme(p)
    assert i.is_u8 and not i.is_theme


def test_stage_theme(tmp_path: Path) -> None:
    p = tmp_path / "dark.csm"
    p.write_bytes(theme())
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    st = T.stage_theme(p, drive, console_type="wii", region="NTSC-U")
    assert st.dest == drive / "themes" / "dark.csm" and st.dest.read_bytes() == p.read_bytes()
    with pytest.raises(T.ThemeError, match="brick"):
        T.stage_theme(p, drive, console_type="wii", region="PAL")
    with pytest.raises(T.ThemeError, match="vwii"):
        T.stage_theme(p, drive, console_type="vwii", region="NTSC-U")
    with pytest.raises(T.ThemeError, match="console type"):
        T.stage_theme(p, drive, console_type="", region="")
    p.write_bytes(theme(build=b"D:\\4_2\\ipl\\bin\\RVL\\Final_U\\x".replace(b"4_2", b"3_2")))
    with pytest.raises(T.ThemeError, match="3.x"):
        T.stage_theme(p, tmp_path / "d2", console_type="wii", region="NTSC-U")
    assert [t.path.name for t in T.list_staged_themes(drive)] == ["dark.csm"]


def test_only_43_themes_and_no_region_from_file_name(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)   # the drive is plugged in
    for minor in (1, 2):
        p = tmp_path / f"old4{minor}.csm"
        p.write_bytes(theme(build=f"D:\\4_{minor}\\ipl\\bin\\RVL\\Final_U\\main.elf".encode()))
        i = T.identify_theme(p)
        assert (i.major, i.minor) == (4, minor)
        with pytest.raises(T.ThemeError, match=f"4.{minor}"):
            T.stage_theme(p, drive, console_type="wii", region="NTSC-U")
    assert not (drive / "themes").exists() or not any((drive / "themes").iterdir())
    # a file merely *named* like the PAL 4.3 base proves nothing about its region
    fake = tmp_path / "0000009a.app"
    fake.write_bytes(build_u8({"/www.arc": b"w", "/layout/common/health.ash": b"h",
                               "/sound/IplSound.brsar": b"s"}))
    i = T.identify_theme(fake)
    assert i.is_theme and i.regions == [] and i.platform == "wii"
    with pytest.raises(T.ThemeError, match="region"):
        T.stage_theme(fake, drive, console_type="wii", region="PAL")
    # staging a theme that already sits in themes/ is a no-op, not an error
    ok = tmp_path / "ok.csm"
    ok.write_bytes(theme())
    st = T.stage_theme(ok, drive, console_type="wii", region="NTSC-U")
    again = T.stage_theme(st.dest, drive, console_type="wii", region="NTSC-U")
    assert again.dest == st.dest and st.dest.read_bytes() == ok.read_bytes()
    assert not list((drive / "themes").glob("*.part"))
