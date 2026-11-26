"""Online play settings: USB Loader GX's private-server patch and GXGlobal.cfg editing.

USB Loader GX (github.com/wiidev/usbloadergx) keeps its global settings in
``apps/usbloader_gx/GXGlobal.cfg``: ``# USB Loader GX R<rev> ...`` header,
then ``name = value`` lines (source/settings/CSettings.cpp, ``Save()`` and
``ParseLine()``). Important details from that source:

* the whole file is ignored unless a line contains ``USB Loader GX R<rev>``
  with ``rev >= 1031`` (``ValidVersion``), so a file ReWii creates must have it;
* lines starting with ``#`` are comments; name and value are trimmed;
* unknown keys are ignored, missing keys keep their defaults, and the loader
  rewrites the full file the next time it saves its settings.

``PrivateServer`` values (SettingsEnums.h): 0 OFF, 1 NoSSL only, 2 Wiimmfi,
3 AltWFC (patches to zwei.moe), 4 Custom (uses ``CustomAddress``).
wii.hacks.guide (docs/nintendowfc.md) tells users to set it to Wiimmfi.
WiiLink WFC is *not* offered by the loader; the guide uses the separate
"WiiLink WFC Patcher" homebrew app for it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Union
from collections.abc import Mapping

from .layout import DriveLayout

#: Header written when ReWii creates GXGlobal.cfg (rev must be >= 1031; current release is 1283).
GX_HEADER = "# USB Loader GX R1283 - Main settings file"
GX_MIN_REV = 1031

PRIVATE_SERVERS: dict[str, int] = {"off": 0, "nossl": 1, "wiimmfi": 2, "altwfc": 3, "custom": 4}
PRIVATE_SERVER_NAMES = {v: k for k, v in PRIVATE_SERVERS.items()}


class UnsupportedServer(ValueError):
    """The requested online service can't be set through the loader."""


@dataclass(frozen=True)
class GxOption:
    """A GXGlobal.cfg key ReWii knows (verified in CSettings.cpp)."""

    key: str
    label: str
    choices: Optional[dict[str, str]] = None   # value -> label; None = free text
    default: str = ""


#: Small, verified set of settings ReWii's settings screen may edit.
GX_OPTIONS: dict[str, GxOption] = {o.key: o for o in [
    GxOption("PrivateServer", "Online play server",
             {"0": "Off", "1": "NoSSL only", "2": "Wiimmfi", "3": "AltWFC", "4": "Custom"}, "0"),
    GxOption("CustomAddress", "Custom server address", None, "wiimmfi.de"),
    GxOption("BannerAnimStart", "Start banner animation",
             {"0": "When zooming in", "1": "After zooming in"}, "0"),
    GxOption("GameCubeMode", "GameCube loader",
             {"0": "MIOS (disc only)", "1": "Devolution", "2": "Nintendont"}, "2"),
    GxOption("NINLoaderPath", "Nintendont folder", None, "usb1:/apps/nintendont/"),
    GxOption("GameCubePath", "GameCube games folder (USB)", None, "usb1:/games/"),
    GxOption("language", "Game language",
             {"0": "Japanese", "1": "English", "2": "German", "3": "French", "4": "Spanish",
              "5": "Italian", "6": "Dutch", "7": "Simplified Chinese", "8": "Traditional Chinese",
              "9": "Korean", "10": "Console default"}, "10"),
]}

Value = Union[str, int, None]


def _parse(text: str) -> tuple[list[str], dict[str, str]]:
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip(" ")] = value.strip(" \r\n")
    return lines, values


def read_gx_config(layout: DriveLayout) -> dict[str, str]:
    """All ``name = value`` pairs in GXGlobal.cfg (empty if the file doesn't exist)."""
    try:
        text = layout.gx_config.read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    return _parse(text)[1]


def _has_valid_header(lines: list[str]) -> bool:
    for line in lines:
        low = line.lower()
        i = low.find("usb loader gx r")
        if i >= 0:
            digits = ""
            for c in line[i + len("usb loader gx r"):]:
                if not c.isdigit():
                    break
                digits += c
            return bool(digits) and int(digits) >= GX_MIN_REV
    return False


def write_gx_config(layout: DriveLayout, updates: Mapping[str, Value]) -> dict[str, str]:
    """Set (or with ``None``, remove) keys in GXGlobal.cfg, preserving every other line.

    Existing keys are changed in place; new keys are appended. A valid header
    is added if missing so the loader doesn't ignore the file. Returns the new
    key/value mapping. Values must not contain line breaks.
    """
    path = layout.gx_config
    try:
        text = path.read_text("utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    lines, _ = _parse(text)
    pending = {}
    for k, v in updates.items():
        if "=" in k or "\n" in k or not k.strip():
            raise ValueError(f"Invalid setting name {k!r}")
        if v is not None and ("\n" in str(v) or "\r" in str(v)):
            raise ValueError(f"Invalid value for {k}")
        pending[k] = None if v is None else str(v)
    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        if not line.startswith("#") and "=" in line:
            name = line.split("=", 1)[0].strip(" ")
            if name in pending:
                if name in done or pending[name] is None:
                    continue          # drop duplicates / removed keys
                out.append(f"{name} = {pending[name]}")
                done.add(name)
                continue
        out.append(line)
    for k, v in pending.items():
        if k not in done and v is not None:
            out.append(f"{k} = {v}")
    if not _has_valid_header(out):
        out.insert(0, GX_HEADER)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", "utf-8", newline="\n")
    os.replace(tmp, path)
    return _parse("\n".join(out))[1]


def set_loader_private_server(layout: DriveLayout, server: str, custom_address: Optional[str] = None) -> int:
    """Set USB Loader GX's global "Private Server" option.

    ``server``: "off", "nossl", "wiimmfi", "altwfc" or "custom" (needs
    ``custom_address``, more than 3 characters as the loader requires). ""/"none"
    means off. "wiilink" raises :class:`UnsupportedServer`: USB Loader GX has
    no WiiLink WFC option; use the WiiLink WFC Patcher app instead.
    Returns the stored numeric value.
    """
    s = (server or "off").strip().lower()
    if s in ("", "none"):
        s = "off"
    if s in ("wiilink", "wiilink wfc", "wiilinkwfc"):
        raise UnsupportedServer("USB Loader GX can't connect games to WiiLink WFC by itself. Install the "
                                "\"WiiLink WFC Patcher\" app from the Open Shop Channel, or choose Wiimmfi.")
    if s not in PRIVATE_SERVERS:
        raise ValueError(f"Unknown server {server!r}")
    updates: dict[str, Value] = {"PrivateServer": PRIVATE_SERVERS[s]}
    if s == "custom":
        if not custom_address or len(custom_address.strip()) <= 3 or any(c.isspace() for c in custom_address.strip()):
            raise ValueError("Enter the custom server's domain name (for example example.com)")
        updates["CustomAddress"] = custom_address.strip()
    write_gx_config(layout, updates)
    return PRIVATE_SERVERS[s]


def get_loader_private_server(layout: DriveLayout) -> str:
    """Current "Private Server" setting as a name ("off" if unset or unknown)."""
    v = read_gx_config(layout).get("PrivateServer", "0")
    try:
        return PRIVATE_SERVER_NAMES.get(int(v), "off")
    except ValueError:
        return "off"
