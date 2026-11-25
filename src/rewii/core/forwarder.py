"""Game channels ("forwarder" WADs): put a game on the Wii Menu.

The channel is self-contained: it carries its own copy of Configurable USB Loader
(CFG, GPLv2, by oggzee) built in "direct start" mode, with the game ID patched in.
When the channel is opened it boots straight into the game from the USB drive.
It needs no loader installed on the drive, only a cIOS (d2x, slot 249).

Launch screen styles (Settings.forwarder_style):
  * ``cover``   - the game's cover art is shown while the game loads
                  (CFG's built-in splash image is replaced with the cover)
  * ``black``   - a plain black screen until the game starts
                  (``intro=0`` is passed to CFG through the DOL's argv area)
  * ``classic`` - CFG's stock splash and loading text (what CRAP made)

Ported from Andy's make_wad.py (which replaced CRAP).
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional

from .config import cache_dir
from .disc import DiscError, aes_cbc_decrypt, aes_cbc_encrypt, common_key, open_disc, parse_header, read_wii_file

STYLES = ("cover", "black", "classic")
MAX_CHANNELS_WARN = 40       # the Wii Menu holds 48 channels incl. system ones

# Games whose banners brick the Wii Menu when installed as a channel
# (wii.hacks.guide/wiigsc). Matched on the first 3 characters of the game ID.
BANNER_BRICK_IDS = {
    "SSQ": "Mario Party 9",
    "SBL": "A Boy and His Blob",
}

# CFG's built-in intro4 splash (640x480 PNG) inside ConfUsbLoader*.dol, which the
# "cover" style replaces. Located by content hash so a different DOL fails safely.
_SPLASH_LEN = 32080
_SPLASH_SHA1 = None  # filled lazily from the bundled DOL


class ForwarderError(Exception):
    pass


class BannerBrickRisk(ForwarderError):
    pass


def _asset(name: str) -> bytes:
    return resources.files("rewii.assets.forwarder").joinpath(name).read_bytes()


def cert_chain() -> bytes:
    """Nintendo's public certificate chain (from NUS, cached)."""
    p = cache_dir() / "cert_chain.bin"
    if p.exists() and p.stat().st_size == 2560:
        return p.read_bytes()
    try:
        from libWiiPy.title.nus import download_cert_chain
        data = download_cert_chain()
    except Exception as e:  # pragma: no cover - network
        raise ForwarderError("Need internet once to download the Wii certificate chain") from e
    p.write_bytes(data)
    return data


def channel_id_for(game_id: str) -> str:
    return "U" + game_id[1:4]


def brick_risk(game_id: str) -> Optional[str]:
    return BANNER_BRICK_IDS.get(game_id[:3].upper())


# --------------------------------------------------------------------------- DOL patching
def _dol_sections(dol: bytes) -> list[tuple[int, int, int]]:
    offs = struct.unpack(">18I", dol[0:0x48])
    addrs = struct.unpack(">18I", dol[0x48:0x90])
    sizes = struct.unpack(">18I", dol[0x90:0xD8])
    return [(offs[i], addrs[i], sizes[i]) for i in range(18)]


def _dol_file_offset(dol: bytes, addr: int) -> int:
    for off, a, s in _dol_sections(dol):
        if s and a <= addr < a + s:
            return off + addr - a
    raise ForwarderError("address not in DOL")


def patch_game_id(dol: bytearray, game_id: str) -> None:
    hits = [m.start() for m in re.finditer(b"#GAMEID", dol)]
    if len(hits) != 1:
        raise ForwarderError(f"expected one #GAMEID placeholder in the loader, found {len(hits)}")
    # keep the '#', overwrite the 6 ID bytes (CFG compares "#GAMEID" then uses buf+1)
    dol[hits[0] + 1:hits[0] + 7] = game_id.encode("ascii")


def add_argv(dol: bytes, args: list[str]) -> bytes:
    """Give a libogc DOL a command line, the way the Homebrew Channel does.

    libogc's crt0 has a ``__argv`` struct right after the entry branch, flagged by
    the magic "_arg". We store the argument strings in a new data section placed
    after the program's BSS and point the struct at it; libogc's __CheckARGV then
    moves them into the heap at startup.
    """
    d = bytearray(dol)
    entry = struct.unpack(">I", d[0xE0:0xE4])[0]
    e_off = _dol_file_offset(d, entry)
    if d[e_off + 4:e_off + 8] != b"_arg":
        raise ForwarderError("loader has no argv support")
    secs = _dol_sections(d)
    free = next((i for i in range(7, 18) if secs[i][2] == 0), None)
    if free is None:
        raise ForwarderError("no free DOL section for arguments")
    bss_addr, bss_size = struct.unpack(">II", d[0xD8:0xE0])
    end = max([a + s for _o, a, s in secs if s] + [bss_addr + bss_size])
    addr = (end + 0x1F) & ~0x1F
    cmd = b"".join(a.encode("utf-8") + b"\0" for a in args)
    blob = cmd + bytes(0x100)                      # room for argv pointers
    blob += bytes((-len(blob)) % 32)
    file_off = (len(d) + 0x1F) & ~0x1F
    d += bytes(file_off - len(d)) + blob
    struct.pack_into(">I", d, free * 4, file_off)
    struct.pack_into(">I", d, 0x48 + free * 4, addr)
    struct.pack_into(">I", d, 0x90 + free * 4, len(blob))
    # struct __argv { argvMagic, commandLine, length, argc, argv, endARGV }
    struct.pack_into(">III", d, e_off + 8, 0x5F617267, addr, len(cmd))
    return bytes(d)


def read_argv(dol: bytes) -> list[str]:
    """Inverse of add_argv (for verification)."""
    entry = struct.unpack(">I", dol[0xE0:0xE4])[0]
    e_off = _dol_file_offset(dol, entry)
    magic, addr, length = struct.unpack(">III", dol[e_off + 8:e_off + 20])
    if magic != 0x5F617267:
        return []
    off = _dol_file_offset(dol, addr)
    return [a.decode() for a in dol[off:off + length].split(b"\0")[:-1]]


def _find_splash(dol: bytes) -> int:
    """File offset of CFG's intro splash PNG (largest 640x480 PNG of the known size)."""
    for m in re.finditer(rb"\x89PNG\r\n\x1a\n", dol):
        o = m.start()
        w, h = struct.unpack(">II", dol[o + 16:o + 24])
        if (w, h) != (640, 480):
            continue
        p = o + 8
        while p < len(dol):
            ln, typ = struct.unpack(">I4s", dol[p:p + 8])
            p += 12 + ln
            if typ == b"IEND":
                break
        if p - o == _SPLASH_LEN:
            return o
    raise ForwarderError("could not find the loader's splash image")


def _black_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("P", (640, 480), 0).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def blank_backgrounds(dol: bytearray, splash: int) -> int:
    """Replace CFG's other built-in 640x480 backgrounds with black. Returns count."""
    n = 0
    black = _black_png()
    for m in list(re.finditer(rb"\x89PNG\r\n\x1a\n", bytes(dol))):
        o = m.start()
        if o == splash or struct.unpack(">II", dol[o + 16:o + 24]) != (640, 480):
            continue
        p = o + 8
        while p < len(dol):
            ln, typ = struct.unpack(">I4s", dol[p:p + 8])
            p += 12 + ln
            if typ == b"IEND":
                break
        if p - o >= len(black):
            dol[o:p] = black + bytes(p - o - len(black))
            n += 1
    return n


def splash_png(cover_png: bytes | None, budget: int = _SPLASH_LEN) -> bytes:
    """Compose a 640x480 PNG (cover centred on black) that fits in ``budget`` bytes."""
    from PIL import Image

    def render(height: int, colors: int) -> bytes:
        canvas = Image.new("RGB", (640, 480), (0, 0, 0))
        if cover_png:
            img = Image.open(io.BytesIO(cover_png)).convert("RGBA")
            scale = height / img.height
            img = img.resize((max(1, int(img.width * scale)), height), Image.LANCZOS)
            bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
            img = Image.alpha_composite(bg, img).convert("RGB")
            canvas.paste(img, ((640 - img.width) // 2, (480 - img.height) // 2))
        q = canvas.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) \
            if colors < 256 else canvas.quantize(colors=256)
        buf = io.BytesIO()
        q.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    for height in (440, 400, 360, 320, 280, 240, 200):
        for colors in (128, 64, 32, 16):
            data = render(height, colors)
            if len(data) <= budget:
                return data
    raise ForwarderError("cover image too detailed to embed")


def build_loader_dol(game_id: str, fat: bool = True, style: str = "cover",
                     cover_png: bytes | None = None) -> bytes:
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    dol = bytearray(_asset("ConfUsbLoaderFat.dol" if fat else "ConfUsbLoader.dol"))
    patch_game_id(dol, game_id)
    if style == "cover":
        o = _find_splash(dol)
        png = splash_png(cover_png)
        dol[o:o + _SPLASH_LEN] = png + bytes(_SPLASH_LEN - len(png))
        blank_backgrounds(dol, o)
    elif style == "black":
        blank_backgrounds(dol, _find_splash(bytes(dol)))
        dol = bytearray(add_argv(bytes(dol), ["rewii", "intro=0"]))
    return bytes(dol)


# --------------------------------------------------------------------------- WAD building
def _align(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def _pad(b: bytes, a: int) -> bytes:
    return b + bytes(_align(len(b), a) - len(b))


def fakesign(blob: bytes, brute_off: int) -> bytes:
    """Zero the RSA signature and brute-force 2 bytes until SHA1(body) starts with 00."""
    b = bytearray(blob)
    b[4:0x104] = bytes(0x100)
    for v in range(0x10000):
        b[brute_off:brute_off + 2] = struct.pack(">H", v)
        if hashlib.sha1(b[0x140:]).digest()[0] == 0:
            return bytes(b)
    raise ForwarderError("fakesign failed")


@dataclass
class ChannelInfo:
    game_id: str
    title: str
    channel_id: str
    style: str
    path: Path
    size: int


def build_wad(banner: bytes, dol: bytes, game_id: str) -> bytes:
    ck = common_key(0)
    if banner[0x40:0x44] != b"IMET":
        raise ForwarderError("game banner has an unexpected format")
    nand = _asset("nand_loader.app")
    contents = [banner, dol, nand]
    chan_id = channel_id_for(game_id)
    tid = struct.pack(">I", 0x00010001) + chan_id.encode("ascii")

    tik = bytearray(_asset("ticket_template.tik"))
    tik[0x1DC:0x1E4] = tid
    tkey = aes_cbc_decrypt(ck, tid + bytes(8), bytes(tik[0x1BF:0x1CF]))
    tik = fakesign(bytes(tik), 0x1F1)

    tmd = bytearray(_asset("tmd_template.tmd"))
    tmd[0x18C:0x194] = tid
    if struct.unpack(">H", tmd[0x1DE:0x1E0])[0] != 3:
        raise ForwarderError("TMD template has unexpected content count")
    for i, data in enumerate(contents):
        rec = 0x1E4 + 36 * i
        tmd[rec + 8:rec + 16] = struct.pack(">Q", len(data))
        tmd[rec + 16:rec + 36] = hashlib.sha1(data).digest()
    tmd = fakesign(bytes(tmd), 0x1D4)

    cert = cert_chain()
    trailer = _pad(b"CMiiUT" + str(int(time.time())).encode(), 0x40)
    enc = []
    for i, data in enumerate(contents):
        iv = struct.pack(">H", i) + bytes(14)
        enc.append(_pad(aes_cbc_encrypt(tkey, iv, _pad(data, 16)), 64))
    blob = b"".join(enc)
    hdr = struct.pack(">I2sHIIIIII", 0x20, b"Is", 0, len(cert), 0, len(tik), len(tmd), len(blob), len(trailer))
    return _pad(hdr, 64) + _pad(cert, 64) + _pad(tik, 64) + _pad(tmd, 64) + blob + _pad(trailer, 64)


def make_channel(game_path: Path, out_dir: Path, *, style: str = "cover", fat: bool = True,
                 cover_png: bytes | None = None, allow_risky: bool = False,
                 title: str | None = None) -> ChannelInfo:
    """Build a channel WAD for a Wii game image (WBFS/ISO/CISO).

    ``title`` (e.g. from GameTDB) is only used for the output file name.
    """
    with open_disc(game_path) as d:
        gid, disc_title, platform, *_ = parse_header(d.read(0, 0x60))
        title = title or disc_title
        if platform != "wii":
            raise ForwarderError("Channels can only be made for Wii games")
        risk = brick_risk(gid)
        if risk and not allow_risky:
            raise BannerBrickRisk(f"{risk} is known to brick the Wii Menu as a channel - refusing")
        try:
            banner = read_wii_file(d, "opening.bnr")
        except DiscError as e:
            raise ForwarderError(f"could not read the game's banner: {e}") from e
    dol = build_loader_dol(gid, fat=fat, style=style, cover_png=cover_png)
    wad = build_wad(banner, dol, gid)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from .layout import safe_name
    out = out_dir / f"{safe_name(title)} - {channel_id_for(gid)} - {gid}.wad"
    out.write_bytes(wad)
    ok, msg = verify_wad(out)
    if not ok:
        out.unlink(missing_ok=True)
        raise ForwarderError(f"built channel failed verification: {msg}")
    return ChannelInfo(gid, title, channel_id_for(gid), style, out, len(wad))


# --------------------------------------------------------------------------- verify
def unpack_wad(data: bytes) -> dict:
    hsz, _t, _v, csz, _r, tsz, msz, _dsz, _fsz = struct.unpack(">I2sHIIIIII", data[:0x20])
    pos = _align(hsz, 64)
    cert = data[pos:pos + csz]; pos = _align(pos + csz, 64)
    tik = data[pos:pos + tsz]; pos = _align(pos + tsz, 64)
    tmd = data[pos:pos + msz]; pos = _align(pos + msz, 64)
    ck = common_key(tik[0x1F1] if tik[0x1F1] in (0, 1, 2) else 0)
    tid = tik[0x1DC:0x1E4]
    tkey = aes_cbc_decrypt(ck, tid + bytes(8), tik[0x1BF:0x1CF])
    n = struct.unpack(">H", tmd[0x1DE:0x1E0])[0]
    contents, ok = [], True
    for i in range(n):
        rec = 0x1E4 + 36 * i
        idx = struct.unpack(">H", tmd[rec + 4:rec + 6])[0]
        size = struct.unpack(">Q", tmd[rec + 8:rec + 16])[0]
        encsz = _align(size, 16)
        dec = aes_cbc_decrypt(tkey, struct.pack(">H", idx) + bytes(14), data[pos:pos + encsz])[:size]
        pos += _align(encsz, 64)
        ok &= hashlib.sha1(dec).digest() == tmd[rec + 16:rec + 36]
        contents.append(dec)
    fake = hashlib.sha1(tik[0x140:]).digest()[0] == 0 and hashlib.sha1(tmd[0x140:]).digest()[0] == 0
    return dict(title_id=tid[4:].decode("ascii", "replace"), contents=contents,
                hashes_ok=ok, fakesigned=fake)


def verify_wad(path: Path) -> tuple[bool, str]:
    try:
        r = unpack_wad(Path(path).read_bytes())
    except Exception as e:
        return False, f"unreadable: {e}"
    if not r["hashes_ok"]:
        return False, "content hashes do not match"
    if not r["fakesigned"]:
        return False, "signature check failed"
    return True, "ok"


def channel_game_id(path: Path) -> Optional[str]:
    """Which game a ReWii/CRAP channel boots (reads the patched loader)."""
    try:
        r = unpack_wad(Path(path).read_bytes())
        dol = r["contents"][1]
        i = dol.find(b"CFGUSB")
        if i > 12 and dol[i - 12:i - 11] == b"#":
            return dol[i - 11:i - 5].decode("ascii")
    except Exception:
        return None
    return None


def cover_for_channel(game_id: str, lang: str = "EN") -> bytes | None:
    """Front cover art for the "cover" launch style: cache first, then GameTDB."""
    from . import covers
    for kind in ("cover", "coverfullHQ", "cover3D"):
        p = covers.cached_cover_path(game_id, kind)
        if p is not None:
            return p.read_bytes()
    try:
        data, _region = covers.download_cover(game_id, "cover", lang)
    except Exception:
        return None
    if data:
        p = covers.cache_path(game_id, "cover")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return data
