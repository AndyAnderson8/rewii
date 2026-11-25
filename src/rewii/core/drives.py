"""Find the drives a user could set up for their Wii, on Windows, macOS and Linux.

Enumeration shells out to the OS's own tools (no admin needed):

* Linux: ``lsblk -J -b -o ...``
* macOS: ``diskutil list -plist [external]`` + ``diskutil info -plist <disk>``
* Windows: PowerShell ``Get-Disk`` / ``Get-Partition`` / ``Get-Volume`` as JSON

Each OS has a pure ``parse_*`` function (unit-tested with captured output) that
turns the tool output into :class:`Drive` objects; :func:`list_drives` runs the
tools and then enriches the result (free space, cluster size, raw WBFS
detection) from the live system.

Safety: every Drive has ``is_system``. :func:`list_drives` never returns a
system/boot disk, and ``formatting`` refuses them again before writing.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import psutil

from .fat32 import parse_boot_sector

# Filesystem names used throughout ReWii.
FAT32, EXFAT, NTFS, WBFS, EXT4, HFS, APFS, FAT16, UNKNOWN = (
    "FAT32", "exFAT", "NTFS", "WBFS", "ext4", "HFS", "APFS", "FAT16", "unknown")

NEEDS_ADMIN = "needs admin"   # Drive.notes entry when a raw read was refused

_SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "[SWAP]",
                  "/System/Volumes/Data", "/private/var/vm"}

_FLASH_HINTS = re.compile(
    r"flash|cruzer|ultra fit|ultra usb|datatraveler|jumpdrive|usb disk|stick|thumb|"
    r"pendrive|pen drive|dual drive|glide|blade|ultra\b|exceria|kingston dt|dtse9|transmemory",
    re.I)

Runner = Callable[[Sequence[str]], str]


# --------------------------------------------------------------------------- model
@dataclass
class Volume:
    """One partition/volume on a drive."""

    device: str                       # /dev/sdb1, /dev/disk4s1, \\.\E: or \\?\Volume{...}
    size: int = 0
    filesystem: str = UNKNOWN
    label: str = ""
    mount_points: list[str] = field(default_factory=list)   # mount dirs or "E:\\"
    free: Optional[int] = None
    cluster_size: Optional[int] = None
    number: int = 0                   # partition number (1-based), 0 if unknown


@dataclass
class Drive:
    """A physical disk as ReWii shows it in the drive picker.

    The top-level ``volume_device``/``filesystem``/``label``/``mount_points``/
    ``free``/``cluster_size`` describe the drive's *main* volume (the largest
    partition, or ``selected_volume`` if set), which is the one a Wii would
    use. All partitions are in ``volumes``.
    """

    id: str                           # stable within a session; used as the format confirm token
    device: str                       # \\.\PhysicalDriveN | /dev/diskN | /dev/sdX
    size: int
    model: str = ""
    bus: str = ""                     # "USB", "SATA", "NVMe", "SD", ...
    removable: bool = False           # removable media / hot-pluggable
    is_usb: bool = False
    is_system: bool = False           # holds the running OS: never offer, never format
    partition_style: str = "none"     # "MBR" | "GPT" | "none" (no table / raw)
    volumes: list[Volume] = field(default_factory=list)
    serial: str = ""
    media_removable: bool = False     # OS says "removable media" (typical of flash sticks)
    notes: list[str] = field(default_factory=list)
    selected_volume: Optional[str] = None  # device of the volume to treat as main (drive_for_path)

    # main-volume shortcuts ---------------------------------------------------
    @property
    def main(self) -> Optional[Volume]:
        """The selected volume, else the largest one (None for an empty/raw disk)."""
        if self.selected_volume:
            for v in self.volumes:
                if v.device == self.selected_volume:
                    return v
        return max(self.volumes, key=lambda v: v.size) if self.volumes else None

    @property
    def volume_device(self) -> Optional[str]:
        return self.main.device if self.main else None

    @property
    def filesystem(self) -> str:
        return self.main.filesystem if self.main else UNKNOWN

    @property
    def label(self) -> str:
        return self.main.label if self.main else ""

    @property
    def mount_points(self) -> list[str]:
        return self.main.mount_points if self.main else []

    @property
    def mount_point(self) -> Optional[str]:
        mps = self.mount_points
        return mps[0] if mps else None

    @property
    def free(self) -> Optional[int]:
        return self.main.free if self.main else None

    @property
    def cluster_size(self) -> Optional[int]:
        return self.main.cluster_size if self.main else None

    @property
    def needs_admin(self) -> bool:
        """True if some detail (e.g. raw WBFS detection) needs admin rights to read."""
        return NEEDS_ADMIN in self.notes

    @property
    def looks_like_flash(self) -> bool:
        """Heuristic: is this a USB flash stick rather than a hard drive/SSD?"""
        if self.bus.upper() in ("SD", "MMC"):
            return True
        if _FLASH_HINTS.search(self.model or ""):
            return True
        return self.media_removable and self.size <= 512 * 1000 ** 3

    @property
    def display_name(self) -> str:
        """Short human-readable name, e.g. ``SanDisk Ultra 64.0 GB (E:)``."""
        name = self.model or self.label or self.device
        where = f" ({self.mount_point})" if self.mount_point else ""
        return f"{name} {human_size(self.size)}{where}"


def human_size(n: Optional[int]) -> str:
    """Format a byte count the way drive makers do (decimal GB/TB)."""
    if n is None:
        return "?"
    for unit, div in (("TB", 1000 ** 4), ("GB", 1000 ** 3), ("MB", 1000 ** 2), ("KB", 1000)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def normalize_fs(name: Optional[str], version: Optional[str] = None) -> str:
    """Map an OS-specific filesystem name onto ReWii's names."""
    n = (name or "").strip().lower()
    v = (version or "").strip().upper()
    if not n:
        return UNKNOWN
    if n in ("vfat", "fat", "msdos", "ms-dos", "fat32", "dos_fat_32", "ms-dos (fat32)",
             "ms-dos fat32"):
        if v in ("FAT16", "FAT12"):
            return FAT16
        return FAT32
    if n in ("fat16", "fat12", "dos_fat_16", "dos_fat_12", "ms-dos fat16", "ms-dos (fat16)"):
        return FAT16
    if n in ("exfat", "ex-fat"):
        return EXFAT
    if n in ("ntfs", "windows_ntfs"):
        return NTFS
    if n == "wbfs":
        return WBFS
    if n in ("ext4", "ext3", "ext2"):
        return EXT4 if n == "ext4" else n
    if n in ("hfs", "hfsplus", "hfs+", "apple_hfs", "apple_hfsx", "mac os extended"):
        return HFS
    if n in ("apfs", "apple_apfs"):
        return APFS
    if n.startswith("ms-dos") or "fat32" in n:
        return FAT32
    if "exfat" in n:
        return EXFAT
    if "ntfs" in n:
        return NTFS
    return UNKNOWN


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- Linux
LSBLK_COLUMNS = "NAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,MOUNTPOINTS,RM,HOTPLUG,TRAN,MODEL,SERIAL,PTTYPE,PKNAME"
LSBLK_COLUMNS_OLD = "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,RM,HOTPLUG,TRAN,MODEL,PTTYPE,PKNAME"


def _lsblk_mounts(node: dict) -> list[str]:
    mps = node.get("mountpoints")
    if mps is None:
        mp = node.get("mountpoint")
        mps = [mp] if mp else []
    return [m for m in mps if m]


def _lsblk_all_mounts(node: dict) -> Iterable[str]:
    yield from _lsblk_mounts(node)
    for ch in node.get("children") or []:
        yield from _lsblk_all_mounts(ch)


def parse_lsblk(data: str | dict) -> list[Drive]:
    """Parse ``lsblk -J -b -o LSBLK_COLUMNS`` output into Drives (type=disk only).

    A disk is ``is_system`` if any of its partitions (or volumes stacked on
    them: LVM, LUKS, RAID) is mounted at a system path such as ``/`` or ``/boot``.
    Loop, ROM and zram devices are skipped.
    """
    doc = json.loads(data) if isinstance(data, str) else data
    drives: list[Drive] = []
    for node in doc.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        name = node.get("name") or ""
        if name.startswith(("zram", "loop", "sr", "ram")):
            continue
        path = node.get("path") or f"/dev/{name}"
        size = _as_int(node.get("size"))
        pt = (node.get("pttype") or "").lower()
        style = {"dos": "MBR", "gpt": "GPT"}.get(pt, "none")
        vols: list[Volume] = []
        children = [c for c in (node.get("children") or []) if c.get("type") == "part"]
        for i, ch in enumerate(children, 1):
            num = _as_int(re.sub(r"^.*?(\d+)$", r"\1", ch.get("name") or "")) or i
            vols.append(Volume(
                device=ch.get("path") or f"/dev/{ch.get('name')}",
                size=_as_int(ch.get("size")),
                filesystem=normalize_fs(ch.get("fstype"), ch.get("fsver")),
                label=ch.get("label") or "",
                mount_points=_lsblk_mounts(ch),
                number=num,
            ))
        if not children and node.get("fstype"):
            # whole-disk filesystem ("superfloppy"), common on sticks formatted by cameras/macs
            vols.append(Volume(device=path, size=size,
                               filesystem=normalize_fs(node.get("fstype"), node.get("fsver")),
                               label=node.get("label") or "", mount_points=_lsblk_mounts(node)))
        mounts = set(_lsblk_all_mounts(node))
        tran = (node.get("tran") or "").lower()
        bus = {"usb": "USB", "sata": "SATA", "nvme": "NVMe", "mmc": "SD", "ata": "ATA",
               "virtio": "virtio", "scsi": "SCSI", "sas": "SAS"}.get(tran, tran.upper())
        serial = (node.get("serial") or "").strip()
        rm = _as_bool(node.get("rm"))
        drives.append(Drive(
            id=f"{name}:{serial or '-'}:{size}",
            device=path,
            size=size,
            model=(node.get("model") or "").strip(),
            bus=bus,
            removable=rm or _as_bool(node.get("hotplug")) or tran == "usb",
            is_usb=tran == "usb",
            is_system=bool(mounts & _SYSTEM_MOUNTS),
            partition_style=style,
            volumes=vols,
            serial=serial,
            media_removable=rm,
        ))
    return drives


def _list_linux(run: Runner) -> list[Drive]:
    try:
        out = run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS])
    except (OSError, subprocess.CalledProcessError):
        out = run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS_OLD])
    return parse_lsblk(out)


# --------------------------------------------------------------------------- macOS
def _plist(data: bytes | str | dict) -> dict:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        data = data.encode("utf-8")
    return plistlib.loads(data)


def parse_diskutil_list(data: bytes | str | dict) -> list[str]:
    """Return the whole-disk identifiers (``disk4``...) from ``diskutil list -plist``."""
    doc = _plist(data)
    if "WholeDisks" in doc:
        return list(doc["WholeDisks"])
    return [d["DeviceIdentifier"] for d in doc.get("AllDisksAndPartitions", [])]


def parse_diskutil(list_data: bytes | str | dict,
                   infos: Mapping[str, bytes | str | dict],
                   root_disks: Iterable[str] = ()) -> list[Drive]:
    """Build Drives from ``diskutil list -plist`` and per-disk ``diskutil info -plist``.

    ``infos`` maps device identifiers (whole disks and partitions) to their
    ``diskutil info -plist`` output. ``root_disks`` are whole disks known to
    hold the running system (from ``diskutil info -plist /``). Synthesized APFS
    containers and disk images are skipped.
    """
    doc = _plist(list_data)
    root = set(root_disks)
    drives: list[Drive] = []
    for entry in doc.get("AllDisksAndPartitions", []):
        ident = entry["DeviceIdentifier"]
        info = _plist(infos[ident]) if ident in infos else {}
        if info.get("VirtualOrPhysical") == "Virtual" or info.get("BusProtocol") == "Disk Image":
            continue
        if entry.get("APFSPhysicalStores"):      # synthesized APFS container disk
            continue
        content = entry.get("Content") or info.get("Content") or ""
        style = {"FDisk_partition_scheme": "MBR", "GUID_partition_scheme": "GPT"}.get(content, "none")
        size = _as_int(entry.get("Size") or info.get("TotalSize") or info.get("Size"))
        vols: list[Volume] = []
        parts = entry.get("Partitions") or []
        for i, p in enumerate(parts, 1):
            pid = p["DeviceIdentifier"]
            pinfo = _plist(infos[pid]) if pid in infos else {}
            pcontent = p.get("Content") or pinfo.get("Content") or ""
            if pcontent in ("EFI", "Apple_Boot", "Microsoft Reserved"):
                continue
            fs = normalize_fs(pinfo.get("FilesystemType") or pinfo.get("FilesystemName")
                              or pcontent)
            if pinfo.get("FilesystemName", "").upper().endswith("FAT16)") or "FAT16" in pinfo.get(
                    "FilesystemUserVisibleName", ""):
                fs = FAT16
            if fs == UNKNOWN and pcontent == "Windows_NTFS":
                fs = NTFS
            if fs == UNKNOWN and pcontent == "Windows_FAT_32":
                fs = FAT32
            mp = p.get("MountPoint") or pinfo.get("MountPoint") or ""
            free = pinfo.get("FreeSpace") or pinfo.get("APFSContainerFree")
            vols.append(Volume(
                device="/dev/" + pid,
                size=_as_int(p.get("Size") or pinfo.get("TotalSize")),
                filesystem=fs,
                label=p.get("VolumeName") or pinfo.get("VolumeName") or "",
                mount_points=[mp] if mp else [],
                free=_as_int(free) if free is not None and mp else None,
                cluster_size=_as_int(pinfo.get("VolumeAllocationBlockSize")) or None,
                number=i,
            ))
            if pid in root or mp in _SYSTEM_MOUNTS:
                root.add(ident)
        if not parts and entry.get("Content") not in (None, "", "FDisk_partition_scheme",
                                                       "GUID_partition_scheme"):
            mp = entry.get("MountPoint") or info.get("MountPoint") or ""
            vols.append(Volume(device="/dev/" + ident, size=size,
                               filesystem=normalize_fs(info.get("FilesystemType") or content),
                               label=entry.get("VolumeName") or info.get("VolumeName") or "",
                               mount_points=[mp] if mp else [],
                               cluster_size=_as_int(info.get("VolumeAllocationBlockSize")) or None))
        bus = info.get("BusProtocol") or ""
        internal = _as_bool(info.get("Internal", False))
        removable_media = _as_bool(info.get("RemovableMedia") or info.get("Removable"))
        ejectable = _as_bool(info.get("Ejectable", False)) or _as_bool(
            info.get("RemovableMediaOrExternalDevice", False))
        serial = str(info.get("DiskUUID") or info.get("MediaUUID") or "")
        drives.append(Drive(
            id=f"{ident}:{serial or '-'}:{size}",
            device="/dev/" + ident,
            size=size,
            model=(info.get("MediaName") or info.get("IORegistryEntryName") or "").strip(),
            bus={"USB": "USB", "SATA": "SATA", "PCI-Express": "NVMe", "Apple Fabric": "NVMe",
                 "Secure Digital": "SD", "Thunderbolt": "Thunderbolt"}.get(bus, bus),
            removable=(not internal) or removable_media or ejectable,
            is_usb=bus == "USB",
            # Internal disks are always treated as system disks on macOS (conservative).
            is_system=ident in root or internal,
            partition_style=style,
            volumes=vols,
            serial=serial,
            media_removable=removable_media,
        ))
    return drives


def _list_macos(run_bytes: Callable[[Sequence[str]], bytes], include_fixed: bool) -> list[Drive]:
    args = ["diskutil", "list", "-plist"] + ([] if include_fixed else ["external"])
    lst = _plist(run_bytes(args))
    infos: dict[str, bytes] = {}
    for entry in lst.get("AllDisksAndPartitions", []):
        idents = [entry["DeviceIdentifier"]] + [p["DeviceIdentifier"]
                                                 for p in entry.get("Partitions") or []]
        for ident in idents:
            try:
                infos[ident] = run_bytes(["diskutil", "info", "-plist", ident])
            except (OSError, subprocess.CalledProcessError):
                pass
    root: set[str] = set()
    try:
        ri = _plist(run_bytes(["diskutil", "info", "-plist", "/"]))
        if ri.get("ParentWholeDisk"):
            root.add(ri["ParentWholeDisk"])
        for store in ri.get("APFSPhysicalStores") or []:
            sid = store.get("APFSPhysicalStore", "") if isinstance(store, dict) else str(store)
            m = re.match(r"(disk\d+)", sid)
            if m:
                root.add(m.group(1))
    except (OSError, subprocess.CalledProcessError, ValueError):
        pass
    return parse_diskutil(lst, infos, root)


# --------------------------------------------------------------------------- Windows
WINDOWS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$d = @(Get-Disk | Select-Object Number,FriendlyName,Model,SerialNumber,UniqueId,Size,
    @{n='PartitionStyle';e={[string]$_.PartitionStyle}},@{n='BusType';e={[string]$_.BusType}},
    IsBoot,IsSystem,IsOffline,IsReadOnly,BootFromDisk)
$p = @(Get-Partition | Select-Object DiskNumber,PartitionNumber,
    @{n='DriveLetter';e={[string]$_.DriveLetter}},Offset,Size,MbrType,GptType,
    IsBoot,IsSystem,IsActive,AccessPaths,@{n='Type';e={[string]$_.Type}})
$v = @(Get-Volume | Select-Object @{n='DriveLetter';e={[string]$_.DriveLetter}},
    FileSystem,FileSystemLabel,Size,SizeRemaining,UniqueId,AllocationUnitSize,
    @{n='DriveType';e={[string]$_.DriveType}})
@{disks=$d; partitions=$p; volumes=$v; systemdrive=$env:SystemDrive} | ConvertTo-Json -Depth 4
"""

_WIN_BUS = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre", 7: "USB",
            8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 15: "Virtual",
            16: "Storage Spaces", 17: "NVMe"}
_WIN_STYLE = {0: "none", 1: "MBR", 2: "GPT"}


def _win_letter(v: Any) -> str:
    if isinstance(v, int):
        return chr(v) if v > 0 else ""
    s = str(v or "").strip("\x00 ")
    return s[:1].upper() if s and s[0].isalpha() else ""


def _listify(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def parse_windows(data: str | dict) -> list[Drive]:
    """Parse the JSON produced by :data:`WINDOWS_PS` (Get-Disk/Get-Partition/Get-Volume).

    Handles enum values serialised either as strings ("USB") or integers (7),
    drive letters as strings or char codes, and single objects instead of arrays.
    """
    doc = json.loads(data) if isinstance(data, str) else data
    disks = _listify(doc.get("disks"))
    parts = _listify(doc.get("partitions"))
    vols = _listify(doc.get("volumes"))
    sysdrive = _win_letter((doc.get("systemdrive") or "C:")[:1])

    vol_by_id = {str(v.get("UniqueId") or "").lower(): v for v in vols if v.get("UniqueId")}
    vol_by_letter = {_win_letter(v.get("DriveLetter")): v for v in vols
                     if _win_letter(v.get("DriveLetter"))}
    drives: list[Drive] = []
    for d in disks:
        num = _as_int(d.get("Number"))
        bus_raw = d.get("BusType")
        bus = _WIN_BUS.get(bus_raw, str(bus_raw)) if isinstance(bus_raw, int) else str(bus_raw or "")
        style_raw = d.get("PartitionStyle")
        style = (_WIN_STYLE.get(style_raw, "none") if isinstance(style_raw, int)
                 else {"MBR": "MBR", "GPT": "GPT"}.get(str(style_raw or "").upper(), "none"))
        size = _as_int(d.get("Size"))
        is_system = _as_bool(d.get("IsBoot")) or _as_bool(d.get("IsSystem"))
        volumes: list[Volume] = []
        removable_media = False
        for p in parts:
            if _as_int(p.get("DiskNumber")) != num:
                continue
            letter = _win_letter(p.get("DriveLetter"))
            if _as_bool(p.get("IsBoot")) or _as_bool(p.get("IsSystem")) or (
                    letter and letter == sysdrive):
                is_system = True
            vol = None
            for ap in _listify(p.get("AccessPaths")):
                vol = vol_by_id.get(str(ap).lower())
                if vol:
                    break
            if vol is None and letter:
                vol = vol_by_letter.get(letter)
            ptype = str(p.get("Type") or "")
            if vol is None and ptype in ("Reserved", "System", "Recovery"):
                continue
            mps = [f"{letter}:\\"] if letter else [str(ap) for ap in _listify(p.get("AccessPaths"))
                                                   if not str(ap).startswith("\\\\?\\")]
            fs = UNKNOWN
            label = ""
            free = cluster = None
            if vol:
                fs = normalize_fs(vol.get("FileSystem"))
                label = vol.get("FileSystemLabel") or ""
                if mps:
                    free = _as_int(vol.get("SizeRemaining"))
                cluster = _as_int(vol.get("AllocationUnitSize")) or None
                dt = vol.get("DriveType")
                if dt in ("Removable", 2):
                    removable_media = True
            if letter:
                device = f"\\\\.\\{letter}:"
            elif vol and vol.get("UniqueId"):
                device = str(vol["UniqueId"]).rstrip("\\")
            else:
                device = f"\\\\.\\Harddisk{num}Partition{_as_int(p.get('PartitionNumber'))}"
            volumes.append(Volume(device=device, size=_as_int(p.get("Size")), filesystem=fs,
                                  label=label, mount_points=mps, free=free, cluster_size=cluster,
                                  number=_as_int(p.get("PartitionNumber"))))
        serial = str(d.get("SerialNumber") or "").strip()
        model = str(d.get("FriendlyName") or d.get("Model") or "").strip()
        drives.append(Drive(
            id=f"PhysicalDrive{num}:{serial or d.get('UniqueId') or '-'}:{size}",
            device=f"\\\\.\\PhysicalDrive{num}",
            size=size,
            model=model,
            bus=bus,
            removable=bus in ("USB", "SD", "MMC") or removable_media,
            is_usb=bus == "USB",
            is_system=is_system,
            partition_style=style,
            volumes=volumes,
            serial=serial,
            media_removable=removable_media,
        ))
    return drives


def _list_windows(run: Runner) -> list[Drive]:
    out = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-Command", WINDOWS_PS])
    return parse_windows(out)


# --------------------------------------------------------------------------- live helpers
def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_text(args: Sequence[str], timeout: float = 30) -> str:
    """Run a tool and return stdout as text (no console window on Windows)."""
    return subprocess.run(list(args), capture_output=True, text=True, check=True,
                          timeout=timeout, creationflags=_creationflags(),
                          encoding="utf-8", errors="replace").stdout


def run_bytes(args: Sequence[str], timeout: float = 30) -> bytes:
    """Run a tool and return stdout as bytes."""
    return subprocess.run(list(args), capture_output=True, check=True, timeout=timeout,
                          creationflags=_creationflags()).stdout


def read_first_sector(device: str) -> bytes | None:
    """Read the first 512 bytes of a volume device; None if access is denied/unsupported."""
    try:
        with open(device, "rb", buffering=0) as f:
            return f.read(512)
    except (PermissionError, OSError):
        return None


def cluster_size_from_os(mount: str) -> Optional[int]:
    """Cluster (allocation unit) size of a mounted volume, using no admin rights.

    Windows: ``GetDiskFreeSpaceW``. Linux/macOS: ``statvfs`` (for FAT volumes
    the kernel reports the cluster size as the block size).
    """
    try:
        if sys.platform == "win32":
            import ctypes
            spc, bps, _f, _t = (ctypes.c_ulong() for _ in range(4))
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(  # type: ignore[attr-defined]
                ctypes.c_wchar_p(mount), ctypes.byref(spc), ctypes.byref(bps),
                ctypes.byref(_f), ctypes.byref(_t))
            return spc.value * bps.value if ok else None
        st = os.statvfs(mount)
        return int(st.f_bsize) or None
    except (OSError, AttributeError, ValueError):
        return None


def enrich(drive: Drive, read_sector: Callable[[str], bytes | None] = read_first_sector) -> Drive:
    """Fill in free space, cluster size and raw-WBFS detection from the live system."""
    for v in drive.volumes:
        if v.mount_points and v.free is None:
            try:
                v.free = psutil.disk_usage(v.mount_points[0]).free
            except OSError:
                pass
        needs_raw = (v.cluster_size is None and v.filesystem in (FAT32, FAT16)) or (
            v.filesystem == UNKNOWN and not v.mount_points)
        if needs_raw:
            sec = read_sector(v.device)
            if sec is None:
                if v.filesystem == UNKNOWN and NEEDS_ADMIN not in drive.notes:
                    drive.notes.append(NEEDS_ADMIN)
            else:
                info = parse_boot_sector(sec)
                if info.filesystem == WBFS:
                    v.filesystem = WBFS
                elif v.filesystem == UNKNOWN and info.filesystem != "unknown":
                    v.filesystem = info.filesystem
                if v.cluster_size is None and info.cluster_size:
                    v.cluster_size = info.cluster_size
        if v.cluster_size is None and v.mount_points:
            v.cluster_size = cluster_size_from_os(v.mount_points[0])
    return drive


def list_drives(include_fixed: bool = False, *, platform: Optional[str] = None,
                runner: Optional[Runner] = None,
                bytes_runner: Optional[Callable[[Sequence[str]], bytes]] = None,
                do_enrich: bool = True) -> list[Drive]:
    """List drives a user could pick. System/boot disks are never returned.

    By default only removable/USB drives are listed; ``include_fixed=True``
    adds internal non-system disks too. Returns ``[]`` if the OS tool fails.
    """
    plat = platform or sys.platform
    run = runner or run_text
    try:
        if plat.startswith("linux"):
            drives = _list_linux(run)
        elif plat == "darwin":
            drives = _list_macos(bytes_runner or run_bytes, include_fixed)
        elif plat == "win32":
            drives = _list_windows(run)
        else:
            return []
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    out = [d for d in drives if not d.is_system and (include_fixed or d.removable)]
    if do_enrich:
        out = [enrich(d) for d in out]
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/") or os.sep


def drive_for_path(path: str | os.PathLike[str], drives: Optional[list[Drive]] = None) -> Optional[Drive]:
    """Return the Drive whose mounted volume contains ``path``.

    Searches ``drives`` (default: ``list_drives(include_fixed=True)``). If the
    path is on a volume not in that list (a system disk, a network share or an
    unsupported OS), a minimal Drive is synthesised from ``psutil`` so the
    health check still has filesystem, size, free space and cluster size; that
    Drive is marked ``is_system`` if it's the volume holding the OS.
    """
    target = _norm(os.fspath(Path(path).expanduser().resolve()))
    candidates = drives if drives is not None else list_drives(include_fixed=True)
    best: tuple[int, Optional[Drive], Optional[Volume]] = (-1, None, None)
    for d in candidates:
        for v in d.volumes:
            for mp in v.mount_points:
                m = _norm(mp)
                if (target == m or target.startswith(m.rstrip(os.sep) + os.sep)) and len(m) > best[0]:
                    best = (len(m), d, v)
    if best[1] is not None:
        d, mv = best[1], best[2]
        assert mv is not None
        # make the matched volume the "main" one for callers
        return replace(d, selected_volume=mv.device) if d.main is not mv else d
    return _drive_from_psutil(target)


def _drive_from_psutil(target: str) -> Optional[Drive]:
    try:
        parts = psutil.disk_partitions(all=False)
    except OSError:
        return None
    best = None
    for p in parts:
        m = _norm(p.mountpoint)
        if (target == m or target.startswith(m.rstrip(os.sep) + os.sep)) and (
                best is None or len(m) > len(_norm(best.mountpoint))):
            best = p
    if best is None:
        return None
    try:
        usage = psutil.disk_usage(best.mountpoint)
        size, free = usage.total, usage.free
    except OSError:
        size, free = 0, None
    vol = Volume(device=best.device, size=size, filesystem=normalize_fs(best.fstype),
                 mount_points=[best.mountpoint], free=free,
                 cluster_size=cluster_size_from_os(best.mountpoint))
    sysroot = _norm(os.environ.get("SystemDrive", "C:") + "\\") if sys.platform == "win32" else "/"
    return Drive(id=f"path:{best.device}:{size}", device=best.device, size=size,
                 is_system=_norm(best.mountpoint) == sysroot, volumes=[vol],
                 partition_style="unknown")


def has_tool(name: str) -> bool:
    """True if an external command is on PATH."""
    return shutil.which(name) is not None
