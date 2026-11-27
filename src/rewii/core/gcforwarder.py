"""GameCube game channels: a Wii Menu channel that boots a GameCube game in Nintendont.

How it works (all self-contained in the WAD):
  * content 1 is Nintendont's own boot.dol with a command line injected into its
    libogc argv area: argv[0] = "usb:/apps/Nintendont/boot.dol",
    argv[1] = a NIN_CFG struct with AUTO_BOOT + USB + the game's path. This is the
    same mechanism USB Loader GX and WiiFlow use to start GameCube games.
  * the TMD grants AHB access (Nintendont needs it) and asks for IOS58.
  * content 0 (the Wii Menu banner) is built from USB Loader GX's GameCube banner
    template: the game's own disc banner picture becomes the channel picture and
    icon, and the title / publisher / year / description fill the template's
    fixed-size text slots in place, so the layout structure is never changed.

GameCube channels go straight to the game; the cover/black styles of Wii
channels don't apply (Nintendont shows nothing while autobooting).
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .disc import open_disc, parse_fst, parse_header
from .forwarder import (BANNER_BRICK_IDS, ChannelInfo, ForwarderError, _asset, _pad, add_argv,
                        aes_cbc_decrypt, aes_cbc_encrypt, cert_chain, common_key, fakesign, verify_wad)

NIN_MAGIC = 0x01070CF6
NIN_CFG_VERSION = 0x0B                  # Nintendont v6.509+
NIN_CFG_SIZE = 548
NIN_MIN_VERSION = (6, 509)
CFG_CHEATS = 1 << 0
CFG_MEMCARDEMU = 1 << 3
CFG_CHEAT_PATH = 1 << 4
CFG_AUTO_BOOT = 1 << 7
CFG_USB = 1 << 10
NIN_LAN_AUTO = 0xFFFFFFFF
NIN_APP_DIR = "usb:/apps/Nintendont/boot.dol"
TMD_ACCESS_AHB_DVD = 0x3                # AHB access + DVD video
IOS58 = 0x000000010000003A


class NintendontMissing(ForwarderError):
    pass


def gc_channel_id(game_id: str) -> str:
    """Channel title ID for a GameCube game: 'G' + the game ID's 2nd-4th characters."""
    return "G" + game_id[1:4]


# --------------------------------------------------------------------------- Nintendont
def nintendont_version(dol: bytes) -> Optional[tuple[int, int]]:
    m = re.search(rb"\$\$Version:(\d+)\.(\d+)", dol)
    return (int(m.group(1)), int(m.group(2))) if m else None


def get_nintendont_dol(drive_root: Path | None = None, allow_download: bool = True) -> bytes:
    """Nintendont's boot.dol: from the Open Shop Channel (cached), else the drive's copy."""
    candidates: list[bytes] = []
    if allow_download:
        try:
            from . import osc
            app = osc.find_app(osc.fetch_index(), "Nintendont")
            if app:
                z = osc.download_archive(app)
                with zipfile.ZipFile(z) as zf:
                    name = next(n for n in zf.namelist() if n.lower().endswith("nintendont/boot.dol"))
                    candidates.append(zf.read(name))
        except Exception:
            pass
    if drive_root is not None:
        for folder in ("Nintendont", "nintendont"):
            p = Path(drive_root) / "apps" / folder / "boot.dol"
            if p.is_file():
                candidates.append(p.read_bytes())
                break
    for dol in candidates:
        v = nintendont_version(dol)
        if v and v >= NIN_MIN_VERSION:
            return dol
    raise NintendontMissing("Need Nintendont v6.509 or newer: connect to the internet once, or install "
                            "Nintendont on the drive from the Homebrew section")


def build_nin_cfg(game_path: str, game_id: str, base: bytes | None = None,
                  cheat_path: str | None = None) -> bytes:
    """NIN_CFG for autobooting ``game_path`` (path on the USB drive without 'usb:',
    e.g. '/games/Title [GKYE01]/game.iso'). Settings are inherited from ``base``
    (the drive's nincfg.bin) when given."""
    if len(game_path.encode()) >= 255:
        raise ForwarderError("game path is too long for Nintendont")
    cfg = bytearray(NIN_CFG_SIZE)
    config, video, lang, maxpads, mcblocks, vscale, voffset = CFG_MEMCARDEMU, 0, NIN_LAN_AUTO, 4, 2, 0, 0
    if base and len(base) >= 544 and struct.unpack(">I", base[:4])[0] == NIN_MAGIC:
        config, video, lang = struct.unpack(">III", base[8:20])
        maxpads = struct.unpack(">I", base[532:536])[0] or 4
        mcblocks, vscale, voffset = base[540], base[541], base[542]
        config &= ~(CFG_CHEATS | CFG_CHEAT_PATH)      # cheat path is per game; not carried over
    config |= CFG_AUTO_BOOT | CFG_USB
    if cheat_path:
        if len(cheat_path.encode()) >= 255:
            raise ForwarderError("cheat path is too long for Nintendont")
        config |= CFG_CHEATS | CFG_CHEAT_PATH
        cfg[275:275 + len(cheat_path.encode())] = cheat_path.encode()
    struct.pack_into(">IIIII", cfg, 0, NIN_MAGIC, NIN_CFG_VERSION, config, video, lang)
    p = game_path.encode()
    cfg[20:20 + len(p)] = p
    struct.pack_into(">I", cfg, 532, min(maxpads, 4))
    cfg[536:540] = game_id[:4].encode("ascii")
    cfg[540], cfg[541], cfg[542] = mcblocks, vscale, voffset
    return bytes(cfg)


def build_gc_dol(nintendont: bytes, game_path: str, game_id: str, base_cfg: bytes | None = None,
                 cheat_path: str | None = None) -> bytes:
    cfg = build_nin_cfg(game_path, game_id, base_cfg, cheat_path)
    return add_argv(nintendont, [NIN_APP_DIR.encode(), cfg])


# --------------------------------------------------------------------------- GC disc banner
@dataclass
class GcBanner:
    rgba: bytes                 # 96x32 RGBA
    name: str = ""
    company: str = ""
    full_name: str = ""
    full_company: str = ""
    description: str = ""


def rgb5a3_decode(data: bytes, w: int, h: int) -> bytes:
    out = bytearray(w * h * 4)
    i = 0
    for ty in range(0, h, 4):
        for tx in range(0, w, 4):
            for y in range(4):
                for x in range(4):
                    v = struct.unpack_from(">H", data, i)[0]
                    i += 2
                    if v & 0x8000:
                        r, g, b, a = (v >> 10) & 31, (v >> 5) & 31, v & 31, 255
                        r, g, b = r * 255 // 31, g * 255 // 31, b * 255 // 31
                    else:
                        a = ((v >> 12) & 7) * 255 // 7
                        r, g, b = ((v >> 8) & 15) * 17, ((v >> 4) & 15) * 17, (v & 15) * 17
                    px, py = tx + x, ty + y
                    if px < w and py < h:
                        o = (py * w + px) * 4
                        out[o:o + 4] = bytes((r, g, b, a))
    return bytes(out)


def rgb5a3_encode(rgba: bytes, w: int, h: int) -> bytes:
    out = bytearray()
    for ty in range(0, h, 4):
        for tx in range(0, w, 4):
            for y in range(4):
                for x in range(4):
                    px, py = tx + x, ty + y
                    if px < w and py < h:
                        r, g, b, a = rgba[(py * w + px) * 4:(py * w + px) * 4 + 4]
                    else:
                        r = g = b = a = 0
                    if a >= 0xE0:
                        v = 0x8000 | ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
                    else:
                        v = ((a >> 5) << 12) | ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                    out += struct.pack(">H", v)
    return bytes(out)


def read_gc_banner(game_path: Path) -> Optional[GcBanner]:
    """The GameCube disc's opening.bnr (BNR1/BNR2): 96x32 picture + names."""
    with open_disc(game_path) as d:
        gid, _t, plat, *_ = parse_header(d.read(0, 0x60))
        if plat != "gc":
            raise ForwarderError("not a GameCube game")
        try:
            files, _ = parse_fst(d.read, 0)
        except Exception:
            return None
        f = next((x for x in files if x.path.lower() == "opening.bnr"), None)
        if f is None or f.size < 0x1820:
            return None
        data = d.read(f.offset, f.size)
    if data[:3] != b"BNR":
        return None
    enc = "shift_jis" if gid[3:4] == "J" else "cp1252"

    def s(a: int, n: int) -> str:
        return data[a:a + n].split(b"\0")[0].decode(enc, "replace").strip()

    base = 0x1820
    return GcBanner(rgb5a3_decode(data[0x20:0x1820], 96, 32), s(base, 0x20), s(base + 0x20, 0x20),
                    s(base + 0x40, 0x40), s(base + 0x80, 0x40), s(base + 0xC0, 0x80).replace("\n", " "))


# --------------------------------------------------------------------------- Wii banner from template
def _imd5_unwrap(data: bytes) -> bytes:
    if data[:4] != b"IMD5":
        raise ForwarderError("banner part has no IMD5 header")
    body = data[0x20:]
    from libWiiPy.archive import lz77
    return lz77.decompress_lz77(body) if body[:4] == b"LZ77" else body


def _imd5_wrap(raw: bytes) -> bytes:
    from libWiiPy.archive import lz77
    comp = lz77.compress_lz77(raw, 1)
    return b"IMD5" + struct.pack(">I", len(comp)) + bytes(8) + hashlib.md5(comp).digest() + comp


def _u8_files(u8: bytes) -> dict[str, tuple[int, int]]:
    """name -> (offset, size) for every file in a U8 archive (names are unique here)."""
    root_off, hdr_size, data_off = struct.unpack(">III", u8[4:16])
    n = struct.unpack(">I", u8[root_off + 8:root_off + 12])[0]
    strtab = root_off + n * 12
    out = {}
    for i in range(1, n):
        typ_name, off, size = struct.unpack(">III", u8[root_off + i * 12:root_off + i * 12 + 12])
        if typ_name >> 24 == 0:
            no = strtab + (typ_name & 0xFFFFFF)
            name = u8[no:u8.index(b"\0", no)].decode()
            out[name] = (off, size)
    return out


def _u8_replace(u8: bytes, new: dict[str, bytes]) -> bytes:
    """Rebuild a flat U8 archive with some files replaced, keeping its node table,
    string table, file order and 32-byte data alignment exactly as they were."""
    root_off, hdr_size, data_off = struct.unpack(">III", u8[4:16])
    n = struct.unpack(">I", u8[root_off + 8:root_off + 12])[0]
    strtab = root_off + n * 12
    head = bytearray(u8[:data_off])
    body = bytearray()
    for i in range(1, n):
        e = root_off + i * 12
        typ_name, off, size = struct.unpack(">III", u8[e:e + 12])
        if typ_name >> 24:
            continue
        no = strtab + (typ_name & 0xFFFFFF)
        name = u8[no:u8.index(b"\0", no)].decode()
        data = new.get(name, u8[off:off + size])
        if body:
            body += bytes((-(data_off + len(body))) % 32)
        struct.pack_into(">II", head, e + 4, data_off + len(body), len(data))
        body += data
    return bytes(head + body)


def _tpl_image(u8: bytearray, files: dict, name: str) -> tuple[int, int, int, int]:
    """(data offset in archive, width, height, format) of a TPL's first image."""
    off, _size = files[name]
    tbl = struct.unpack(">I", u8[off + 8:off + 12])[0]
    ih = struct.unpack(">I", u8[off + tbl:off + tbl + 4])[0]
    h, w, fmt, doff = struct.unpack(">HHII", u8[off + ih:off + ih + 12])
    return off + doff, w, h, fmt


def _set_tpl(u8: bytearray, files: dict, name: str, img) -> None:
    doff, w, h, fmt = _tpl_image(u8, files, name)
    if fmt != 5:
        raise ForwarderError(f"{name}: unexpected texture format")
    rgba = img.convert("RGBA").resize((w, h)).tobytes()
    enc = rgb5a3_encode(rgba, w, h)
    u8[doff:doff + len(enc)] = enc


def _set_text(u8: bytearray, files: dict, layout: str, pane: str, text: str) -> None:
    """Write ``text`` into a txt1 pane's existing buffer (never grows the layout)."""
    off, size = files[layout]
    lyt = u8[off:off + size]
    _m, _b, _v, _sz, hsz, nsec = struct.unpack(">4sHHIHH", lyt[:16])
    p = hsz
    for _ in range(nsec):
        kind, ssz = struct.unpack(">4sI", lyt[p:p + 8])
        if kind == b"txt1" and lyt[p + 0x0C:p + 0x1C].split(b"\0")[0].decode() == pane:
            _len, maxlen = struct.unpack(">HH", lyt[p + 0x4C:p + 0x50])
            toff = struct.unpack(">I", lyt[p + 0x58:p + 0x5C])[0]
            chars = maxlen // 2 - 1
            enc = text[:chars].encode("utf-16-be") + b"\0\0"
            buf = enc + bytes(maxlen - len(enc))
            a = off + p + toff
            u8[a:a + maxlen] = buf
            struct.pack_into(">H", u8, off + p + 0x4C, len(enc))
            return
        p += ssz
    raise ForwarderError(f"text pane {pane} not found")


def _image_on_black(rgba_img, w: int, h: int):
    from PIL import Image
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    scale = min(w / rgba_img.width, h / rgba_img.height)
    img = rgba_img.resize((max(1, round(rgba_img.width * scale)), max(1, round(rgba_img.height * scale))),
                          Image.LANCZOS)
    canvas.alpha_composite(img.convert("RGBA"), ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def build_gc_banner(title: str, banner: GcBanner | None, *, publisher: str = "", year: str = "",
                    game_id: str = "", synopsis: str = "", cover_png: bytes | None = None) -> bytes:
    """Channel banner (content 0) for a GameCube game."""
    from PIL import Image

    tpl = bytearray(_asset("gc_banner_template.bnr"))
    i = tpl.find(b"IMET")
    if i != 0x80:
        raise ForwarderError("banner template is not a channel banner")
    head, imet, body = tpl[:0x40], bytearray(tpl[0x40:0x640]), tpl[0x640:]
    files_outer = _u8_files(bytes(body))

    def part(name: str) -> bytes:
        o, n = files_outer[name]
        return bytes(body[o:o + n])

    pic = None
    if banner is not None:
        pic = Image.frombytes("RGBA", (96, 32), banner.rgba)
    elif cover_png:
        pic = Image.open(io.BytesIO(cover_png)).convert("RGBA")

    # --- banner.bin: picture + text slots
    raw = bytearray(_imd5_unwrap(part("banner.bin")))
    files = _u8_files(bytes(raw))
    if pic is not None:
        _set_tpl(raw, files, "HBPic.tpl", _image_on_black(pic, 128, 48))
    texts = {
        "T_name": title,
        "T_short_descript": "GameCube game · starts in Nintendont",
        "T_PF": "GameCube",
        "T_Coded_by": "Publisher",
        "T_coder": publisher,
        "T_Released": "Released:",
        "T_release_date": year,
        "T_versiontext": "Game ID:",
        "T_version": game_id,
        "T_long_descripti": synopsis,
    }
    for pane, text in texts.items():
        _set_text(raw, files, "banner.brlyt", pane, text)
    new_banner = _imd5_wrap(bytes(raw))

    # --- icon.bin: the Wii Menu tile
    raw = bytearray(_imd5_unwrap(part("icon.bin")))
    files = _u8_files(bytes(raw))
    if pic is not None:
        _set_tpl(raw, files, "Iconpng.tpl", _image_on_black(pic, 128, 96))
    new_icon = _imd5_wrap(bytes(raw))

    # --- IMET names (sizes are of the uncompressed parts and don't change)
    name = title[:40].encode("utf-16-be")
    for lang in range(10):
        a = 0x5C + lang * 84
        imet[a:a + 84] = name + bytes(84 - len(name))
    imet[0x5F0:0x600] = bytes(16)
    imet[0x5F0:0x600] = hashlib.md5(bytes(imet)).digest()
    return bytes(head) + bytes(imet) + _u8_replace(bytes(body), {"banner.bin": new_banner, "icon.bin": new_icon})


# --------------------------------------------------------------------------- WAD
def build_gc_wad(banner: bytes, dol: bytes, game_id: str) -> bytes:
    ck = common_key(0)
    nand = _asset("nand_loader.app")
    contents = [banner, dol, nand]
    chan_id = gc_channel_id(game_id)
    tid = struct.pack(">I", 0x00010001) + chan_id.encode("ascii")

    tik = bytearray(_asset("ticket_template.tik"))
    tik[0x1DC:0x1E4] = tid
    tkey = aes_cbc_decrypt(ck, tid + bytes(8), bytes(tik[0x1BF:0x1CF]))
    tik = fakesign(bytes(tik), 0x1F1)

    tmd = bytearray(_asset("tmd_template.tmd"))
    struct.pack_into(">Q", tmd, 0x184, IOS58)
    tmd[0x18C:0x194] = tid
    struct.pack_into(">I", tmd, 0x1D8, TMD_ACCESS_AHB_DVD)
    for i, data in enumerate(contents):
        rec = 0x1E4 + 36 * i
        tmd[rec + 8:rec + 16] = struct.pack(">Q", len(data))
        tmd[rec + 16:rec + 36] = hashlib.sha1(data).digest()
    tmd = fakesign(bytes(tmd), 0x1D4)

    cert = cert_chain()
    import time
    trailer = _pad(b"CMiiUT" + str(int(time.time())).encode(), 0x40)
    enc = []
    for i, data in enumerate(contents):
        iv = struct.pack(">H", i) + bytes(14)
        enc.append(_pad(aes_cbc_encrypt(tkey, iv, _pad(data, 16)), 64))
    blob = b"".join(enc)
    hdr = struct.pack(">I2sHIIIIII", 0x20, b"Is", 0, len(cert), 0, len(tik), len(tmd), len(blob), len(trailer))
    return _pad(hdr, 64) + _pad(cert, 64) + _pad(tik, 64) + _pad(tmd, 64) + blob + _pad(trailer, 64)


def make_gc_channel(game_path: Path, drive_root: Path, out_dir: Path, *, title: str | None = None,
                    publisher: str = "", year: str = "", synopsis: str = "",
                    cover_png: bytes | None = None, nintendont: bytes | None = None) -> ChannelInfo:
    """Build a channel for a GameCube game that is already on the drive at ``game_path``."""
    game_path, drive_root = Path(game_path), Path(drive_root)
    with open_disc(game_path) as d:
        gid, disc_title, plat, disc_no, _v = parse_header(d.read(0, 0x60))
    if plat != "gc":
        raise ForwarderError("not a GameCube game")
    if gid[:3] in BANNER_BRICK_IDS:
        raise ForwarderError(f"{BANNER_BRICK_IDS[gid[:3]]} is blocked")
    try:
        rel = "/" + game_path.resolve().relative_to(drive_root.resolve()).as_posix()
    except ValueError as e:
        raise ForwarderError("the game must be on the Wii drive (put it on the drive first)") from e
    if disc_no:
        raise ForwarderError("make the channel for disc 1 - Nintendont switches to disc 2 by itself")
    title = title or disc_title
    gb = read_gc_banner(game_path)
    if not publisher and gb:
        publisher = gb.full_company or gb.company
    if not synopsis and gb:
        synopsis = gb.description
    base_cfg = None
    p = drive_root / "nincfg.bin"
    if p.is_file():
        base_cfg = p.read_bytes()
    nin = nintendont or get_nintendont_dol(drive_root)
    gct = drive_root / "codes" / f"{gid}.gct"
    dol = build_gc_dol(nin, rel, gid, base_cfg, f"/codes/{gid}.gct" if gct.is_file() else None)
    banner = build_gc_banner(title, gb, publisher=publisher, year=year, game_id=gid, synopsis=synopsis,
                             cover_png=cover_png)
    wad = build_gc_wad(banner, dol, gid)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from .layout import safe_name
    out = out_dir / f"{safe_name(title)} - {gc_channel_id(gid)} - {gid}.wad"
    out.write_bytes(wad)
    ok, msg = verify_wad(out)
    if not ok:
        out.unlink(missing_ok=True)
        raise ForwarderError(f"built channel failed verification: {msg}")
    return ChannelInfo(gid, title, gc_channel_id(gid), "gamecube", out, len(wad))
