"""``rewii selftest``: does this build of ReWii work at all?

Meant for the packaged (PyInstaller) app, where the test suite isn't available:
it proves the bundled files are really inside the executable and that the
third-party libraries load and work, by building real channel WADs. It needs no
network, no drive and no user files - everything happens in a temporary folder
with synthetic discs, a stand-in Nintendont and a stand-in certificate chain.
"""
from __future__ import annotations

import io
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from .errors import ReWiiError

#: Every file ReWii ships in ``rewii/assets/forwarder`` (see NOTICE.md).
BUNDLED_ASSETS = ("ConfUsbLoader.dol", "ConfUsbLoaderFat.dol", "gc_banner_template.bnr",
                  "nand_loader.app", "ticket_template.tik", "tmd_template.tmd")
STUB_CERT_CHAIN = bytes(0xA00)      # the builders only embed the chain; its content isn't used here


class SelfTestFailed(ReWiiError):
    """A self-test step didn't give the expected result."""


def _need(ok: object, what: str) -> None:
    if not ok:
        raise SelfTestFailed(what)


def check_assets() -> str:
    from . import forwarder
    folder = resources.files("rewii.assets.forwarder")
    present = {f.name for f in folder.iterdir() if f.is_file() and not f.name.startswith("__")}
    missing = sorted(set(BUNDLED_ASSETS) - present)
    _need(not missing, f"bundled files missing: {', '.join(missing)}")
    sizes = {name: len(forwarder.asset(name)) for name in BUNDLED_ASSETS}
    _need(all(sizes.values()), "a bundled file is empty")
    return f"{len(sizes)} bundled files, {sum(sizes.values())} bytes"


def check_libraries() -> str:
    """Pillow, pycryptodome and libWiiPy do real work (not just import)."""
    from libWiiPy.archive import lz77
    from PIL import Image

    from .disc import aes_cbc_decrypt, aes_cbc_encrypt, common_key
    buf = io.BytesIO()
    Image.new("RGB", (64, 90), (200, 30, 30)).resize((32, 45), Image.Resampling.LANCZOS).save(buf, "PNG")
    _need(buf.getvalue()[:4] == b"\x89PNG", "Pillow couldn't write a PNG")
    key = common_key(0)
    _need(len(key) == 16, "libWiiPy returned no common key")
    data = bytes(range(256)) * 4
    _need(aes_cbc_decrypt(key, bytes(16), aes_cbc_encrypt(key, bytes(16), data)) == data, "AES round trip failed")
    _need(lz77.decompress_lz77(lz77.compress_lz77(data)) == data, "libWiiPy LZ77 round trip failed")
    return "Pillow, pycryptodome and libWiiPy work"


def _cover_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (160, 224), (20, 90, 200)).save(buf, "PNG")
    return buf.getvalue()


def check_wii_channel(tmp: Path) -> str:
    from . import forwarder, synthdisc
    iso = synthdisc.make_wii_iso(tmp / "wii.iso", "RSLF01", "SELFTEST WII", logical_size=0x400000)
    built = []
    for style in forwarder.STYLES:
        ci = forwarder.make_channel(iso, tmp / f"wii-{style}", style=style, cover_png=_cover_png(),
                                    cert_chain=STUB_CERT_CHAIN)
        ok, why = forwarder.verify_wad(ci.path)
        _need(ok, f"the {style} Wii channel failed verification: {why}")
        _need(forwarder.channel_game_id(ci.path) == "RSLF01", f"the {style} Wii channel boots the wrong game")
        _need(ci.channel_id == "USLF", "wrong channel ID")
        built.append(f"{style} {ci.size} B")
    return "Wii channel WADs built and verified (" + ", ".join(built) + ")"


def check_gc_channel(tmp: Path) -> str:
    from . import forwarder, gcforwarder, synthdisc
    from .layout import DriveLayout
    lay = DriveLayout(tmp / "drive")
    lay.root.mkdir()
    game = lay.gc_game_file("SELFTEST GC", "GSLF01")
    game.parent.mkdir(parents=True)
    synthdisc.make_gc_iso(game, "GSLF01", "SELFTEST GC")
    # any libogc DOL with an argv area stands in for Nintendont
    nintendont = forwarder.asset("ConfUsbLoaderFat.dol") + b"$$Version:6.510\0"
    _need(gcforwarder.nintendont_version(nintendont) == (6, 510), "Nintendont version not recognised")
    ci = gcforwarder.make_gc_channel(game, lay, tmp / "gc-out", nintendont=nintendont,
                                     cert_chain=STUB_CERT_CHAIN, publisher="ReWii", year="2026")
    ok, why = forwarder.verify_wad(ci.path)
    _need(ok, f"the GameCube channel failed verification: {why}")
    _need(forwarder.channel_game_id(ci.path) == "GSLF01", "the GameCube channel boots the wrong game")
    _need(ci.channel_id == "GSLF", "wrong channel ID")
    return f"GameCube channel WAD built and verified ({ci.size} B, banner from the template)"


def run(report: Callable[[str], None] = print) -> bool:
    """Run every step, reporting one line each. True if all passed."""
    ok = True
    with tempfile.TemporaryDirectory(prefix="rewii-selftest-") as d:
        tmp = Path(d)
        steps: list[tuple[str, Callable[[], str]]] = [
            ("bundled files", check_assets),
            ("libraries", check_libraries),
            ("Wii channel", lambda: check_wii_channel(tmp)),
            ("GameCube channel", lambda: check_gc_channel(tmp)),
        ]
        for name, step in steps:
            try:
                report(f"ok    {name}: {step()}")
            except Exception as e:  # noqa: BLE001 - a self-test reports everything, never crashes
                ok = False
                report(f"FAIL  {name}: {type(e).__name__}: {e}")
    return ok
