"""System helpers that aren't on the Open Shop Channel.

* HackMii installer (updates/reinstalls the Homebrew Channel and BootMii) from bootmii.org
* d2xl cIOS installer for the Wii mini (hosted by wii.hacks.guide)
* Priiloader's hack list (hacks_hash.ini) from Priiloader's GitHub releases, plus
  appending custom patches (e.g. the vWii clock fix from the Priiloader patch generator)
* bootmii.ini (BootMii's settings, on the SD card)
* ReWii's own update check

Downloads are pinned by SHA-256 where the file never changes.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import net
from .config import cache_dir
from .layout import DriveLayout
from .progress import CancelToken, ProgressFn

HACKMII_URL = "https://bootmii.org/hackmii_installer_v1.2.zip"
HACKMII_SHA256 = "32b7406956860ffb50d46f83e5fb6b8d67cd2612781cd97619d0c2371c84c945"
D2XL_MINI_URL = "https://wii.hacks.guide/assets/files/d2xl-cios-installer-mini.zip"
D2XL_MINI_SHA256 = "c63659112453c6b2d8383d3bdeca5ad60c400781e571d9c32016528b594ab934"
PRIILOADER_RELEASE_API = "https://api.github.com/repos/DacoTaco/priiloader/releases/latest"

# Set this once ReWii has a public GitHub repo ("owner/name"); empty disables the check.
UPDATE_REPO = ""


class SysFileError(Exception):
    pass


def _fetch_pinned(url: str, sha256: str, name: str, progress: ProgressFn | None,
                  cancel: CancelToken | None) -> bytes:
    p = cache_dir() / "sysfiles" / name
    if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == sha256:
        return p.read_bytes()
    net.download(url, p, progress=progress, cancel=cancel)
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        p.unlink(missing_ok=True)
        raise SysFileError(f"{name}: the download doesn't match the known file - not using it")
    return data


# --------------------------------------------------------------------------- HackMii
HACKMII_META = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<app version="1">
  <name>HackMii Installer</name>
  <coder>Team Twiizers</coder>
  <version>1.2</version>
  <short_description>Install/update the Homebrew Channel and BootMii</short_description>
  <long_description>Staged by ReWii. Use it to update or reinstall the Homebrew Channel, or to install/remove BootMii.</long_description>
  <ahb_access/>
</app>
"""


def stage_hackmii(layout: DriveLayout, progress: ProgressFn | None = None,
                  cancel: CancelToken | None = None) -> Path:
    """Put the HackMii installer in apps/hackmii_installer/ (runs from the Homebrew Channel)."""
    data = _fetch_pinned(HACKMII_URL, HACKMII_SHA256, "hackmii_installer_v1.2.zip", progress, cancel)
    dest = layout.app_dir("hackmii_installer")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        (dest / "boot.elf").write_bytes(zf.read("hackmii_installer_v1.2/boot.elf"))
        (dest / "README.txt").write_bytes(zf.read("hackmii_installer_v1.2/README.txt"))
    (dest / "meta.xml").write_text(HACKMII_META, "utf-8")
    return dest


# --------------------------------------------------------------------------- Wii mini cIOS
def stage_d2xl_mini(layout: DriveLayout, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None) -> Path:
    """Put the Wii mini d2xl cIOS installer in apps/ (from wii.hacks.guide)."""
    data = _fetch_pinned(D2XL_MINI_URL, D2XL_MINI_SHA256, "d2xl-cios-installer-mini.zip", progress, cancel)
    root = layout.root.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (layout.root / info.filename).resolve()
            if root not in target.parents or not info.filename.startswith("apps/"):
                raise SysFileError("unexpected file in the d2xl zip")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return layout.app_dir("d2x-cios-installer-mini")


# --------------------------------------------------------------------------- Priiloader hacks
def hacks_path(layout: DriveLayout) -> Path:
    """Priiloader loads apps/priiloader/hacks_hash.ini from SD or USB."""
    return layout.app_dir("priiloader") / "hacks_hash.ini"


def hack_names(text: str) -> list[str]:
    seen: list[str] = []
    for m in re.finditer(r"^\[(.+?)\]\s*$", text, re.M):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def validate_hack_block(text: str) -> list[str]:
    """Problems with a hacks_hash.ini snippet ([] = looks fine)."""
    errs = []
    blocks = [b for b in re.split(r"(?m)^(?=\[)", text.strip()) if b.strip()]
    if not text.strip() or not blocks or not blocks[0].startswith("["):
        return ["it must start with a [Hack name] line"]
    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        name = lines[0]
        keys = [ln.split("=", 1)[0].strip().lower() for ln in lines[1:] if "=" in ln]
        bad = [ln for ln in lines[1:] if "=" not in ln]
        if bad:
            errs.append(f"{name}: lines without '=': {bad[0]}")
        for k in ("maxversion", "minversion"):
            if k not in keys:
                errs.append(f"{name}: missing {k}")
        if not ({"hash", "offset"} & set(keys)) or "patch" not in keys:
            errs.append(f"{name}: needs hash/offset and patch lines")
        for ln in lines[1:]:
            if "=" in ln:
                k, v = ln.split("=", 1)
                if k.strip().lower() in ("hash", "patch", "offset") and not re.fullmatch(
                        r"\s*0x[0-9a-fA-F]+(\s*,\s*0x[0-9a-fA-F]+)*\s*", v):
                    errs.append(f"{name}: bad value '{ln}'")
    return errs


def _write_lf(p: Path, text: str) -> None:
    """Write text with plain LF line endings on every OS (the Wii reads these files)."""
    p.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def _backup(p: Path) -> Optional[Path]:
    if p.is_file():
        b = p.with_name(p.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(p, b)
        return b
    return None


def update_priiloader_hacks(layout: DriveLayout, progress: ProgressFn | None = None,
                            cancel: CancelToken | None = None, keep_custom: bool = True) -> tuple[Path, str]:
    """Download the latest official hacks_hash.ini. Custom hacks you added with
    ReWii (marked "# ReWii custom") are kept. Returns (path, release tag)."""
    rel = net.get_json(PRIILOADER_RELEASE_API)
    asset = next((a for a in rel.get("assets", []) if a.get("name") == "hacks_hash.ini"), None)
    if asset is None:
        raise SysFileError("Priiloader's latest release has no hacks_hash.ini")
    data = net.get_bytes(asset["browser_download_url"])
    assert data is not None
    text = data.decode("utf-8", "replace")
    if not hack_names(text):
        raise SysFileError("downloaded hack list looks empty")
    p = hacks_path(layout)
    custom = ""
    if keep_custom and p.is_file():
        old = p.read_text("utf-8", "replace").replace("\r\n", "\n")
        i = old.find(CUSTOM_MARK)
        if i >= 0:
            custom = old[i:]
    _backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_lf(p, text.rstrip("\r\n") + "\n" + (("\n" + custom) if custom else ""))
    return p, str(rel.get("tag_name", ""))


CUSTOM_MARK = "# ReWii custom hacks"


def add_custom_hack(layout: DriveLayout, snippet: str) -> Path:
    """Append a patch (e.g. from the Priiloader patch generator) to hacks_hash.ini."""
    errs = validate_hack_block(snippet)
    if errs:
        raise SysFileError("That doesn't look like a Priiloader hack:\n" + "\n".join(errs))
    p = hacks_path(layout)
    if not p.is_file():
        raise SysFileError("No hacks_hash.ini on the drive yet - install Priiloader or update the hack list first")
    text = p.read_text("utf-8", "replace").replace("\r\n", "\n")
    _backup(p)
    if CUSTOM_MARK not in text:
        text = text.rstrip("\r\n") + "\n\n" + CUSTOM_MARK + "\n"
    _write_lf(p, text.rstrip("\r\n") + "\n" + snippet.strip() + "\n")
    return p


# --------------------------------------------------------------------------- bootmii.ini
BOOTMII_KEYS = {
    "VIDEO": ("NTSC", "PAL50", "PAL60", "PROGRESSIVE"),
    "AUTOBOOT": ("SYSMENU", "HBC", "OFF"),
    "BOOTDELAY": tuple(str(i) for i in range(0, 11)),
}


def read_bootmii_ini(sd_root: Path) -> dict[str, str]:
    p = Path(sd_root) / "bootmii" / "bootmii.ini"
    out: dict[str, str] = {}
    if p.is_file():
        for ln in p.read_text("latin-1").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip().upper()] = v.strip()
    return out


def write_bootmii_ini(sd_root: Path, values: dict[str, str]) -> Path:
    for k, v in values.items():
        if k not in BOOTMII_KEYS or v not in BOOTMII_KEYS[k]:
            raise SysFileError(f"invalid bootmii.ini setting {k}={v}")
    folder = Path(sd_root) / "bootmii"
    if not folder.is_dir():
        raise SysFileError("No bootmii folder on that card")
    p = folder / "bootmii.ini"
    lines = p.read_text("latin-1").splitlines() if p.is_file() else []
    done = set()
    for i, ln in enumerate(lines):
        if "=" in ln and not ln.lstrip().startswith("#"):
            k = ln.split("=", 1)[0].strip().upper()
            if k in values:
                lines[i] = f"{k}={values[k]}"
                done.add(k)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    _backup(p)
    p.write_bytes(("\r\n".join(lines) + "\r\n").encode("latin-1", "replace"))
    return p


# --------------------------------------------------------------------------- ReWii update check
@dataclass
class UpdateInfo:
    current: str
    latest: str
    url: str

    @property
    def newer(self) -> bool:
        return _ver(self.latest) > _ver(self.current)


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def check_for_update(current: str, repo: str = "") -> Optional[UpdateInfo]:
    """Latest ReWii release on GitHub, or None (no repo configured / offline)."""
    repo = repo or UPDATE_REPO
    if not repo:
        return None
    try:
        rel = net.get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except Exception:
        return None
    tag = str(rel.get("tag_name") or "")
    if not tag:
        return None
    return UpdateInfo(current, tag.lstrip("v"), str(rel.get("html_url") or ""))
