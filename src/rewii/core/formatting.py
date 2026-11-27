"""Prepare a drive for the Wii: plan and run a FAT32 (MBR, 32 KiB clusters) format.

The flow the UI follows::

    plan = plan_format(drive)                 # raises FormatRefused for system disks etc.
    show(plan.summary, plan.steps, plan.warnings)
    if not is_admin():                         # raw disk writes need admin/root
        run_elevated(helper_args(drive.id))    # or tell the user how to rerun
    execute_plan(plan, confirm_token=drive.id, progress=..., cancel=...)

``execute_plan`` only touches a device when ``confirm_token`` equals the
drive's id, which the UI only passes after the user typed the drive name.
Before writing, the drive list is re-read and the drive must still be present
with the same id and must not be a system disk.

Per OS, after the safety checks:

* Windows: lock + dismount every volume on the disk with
  ``FSCTL_LOCK_VOLUME``/``FSCTL_DISMOUNT_VOLUME`` (handles stay open while
  writing), write ``\\\\.\\PhysicalDriveN``, ``IOCTL_DISK_UPDATE_PROPERTIES``,
  release the locks, then ``Update-Disk`` so Windows mounts the new volume.
* macOS: ``diskutil unmountDisk force /dev/diskN``, write ``/dev/rdiskN``,
  ``diskutil mountDisk /dev/diskN``.
* Linux: ``udisksctl unmount`` (falls back to ``umount``) every mounted
  partition, write ``/dev/sdX``, ``blockdev --rereadpt`` (or ``partprobe``),
  then try ``udisksctl mount`` so the desktop sees the drive again.

A drive whose ``device`` is a regular file (a disk image) is formatted
directly with no OS steps; that's what the tests use.
"""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .drives import Drive, human_size, list_drives
from .fat32 import MAX_SIZE, Fat32Error, FormatResult, format_fat32, normalize_label, plan_fat32
from .progress import CancelToken, ProgressFn

DEFAULT_LABEL = "REWII"
DEFAULT_CLUSTER = 32768


class FormatRefused(Exception):
    """Formatting this drive is not allowed (system disk, not removable, bad token...)."""


class FormatFailed(Exception):
    """An OS step failed while formatting; the message is user-presentable."""


@dataclass
class FormatStep:
    """One step of a format, shown to the user before anything happens."""

    kind: str                 # "unmount" | "lock" | "write" | "rescan" | "mount"
    description: str          # plain language
    command: Optional[list[str]] = None   # external command, if the step runs one


@dataclass
class FormatPlan:
    """Everything that will happen to a drive, computed without touching it."""

    drive: Drive
    platform: str
    label: str
    cluster_size: int          # cluster size that will actually be used
    size_bytes: int            # bytes of the disk that will be used (max 2 TiB)
    raw_device: str            # what gets opened for writing
    steps: list[FormatStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_admin: bool = True
    is_image: bool = False

    @property
    def summary(self) -> str:
        """One-line plain-language summary for the confirmation screen."""
        return (f"Erase {self.drive.display_name} and make it a FAT32 drive named "
                f"{self.label or '(no name)'} with {self.cluster_size // 1024} KB clusters.")


# --------------------------------------------------------------------------- privileges
def is_admin() -> bool:
    """True if this process can write raw disks (Administrator on Windows, root elsewhere)."""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
    return os.geteuid() == 0


def self_command(args: Sequence[str] = ()) -> list[str]:
    """The command line that starts ReWii again (frozen exe or ``python -m rewii``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "rewii", *args]


def helper_args(drive_id: str, label: str = DEFAULT_LABEL,
                cluster_size: int = DEFAULT_CLUSTER) -> list[str]:
    """Arguments for the elevated, non-interactive format helper (see report: needs CLI wiring)."""
    return ["format-drive", "--drive-id", drive_id, "--confirm", drive_id,
            "--label", label, "--cluster-size", str(cluster_size)]


def relaunch_elevated_command(args: Sequence[str] = (), platform: Optional[str] = None
                              ) -> tuple[str, list[str]]:
    """Describe how to rerun ReWii (with ``args``) with admin rights on this OS.

    Returns ``(method, argv)``:

    * Windows: ``("shellexecute-runas", [exe, *params])`` - run with
      ``ShellExecuteW(None, "runas", exe, params, ...)`` (UAC prompt; see
      :func:`run_elevated`).
    * macOS: ``("osascript", ["osascript", "-e", 'do shell script "..." with
      administrator privileges'])`` - the system password dialog. There's no
      terminal, so only use it for the non-interactive helper.
    * Linux: ``("pkexec", ["pkexec", ...])`` when pkexec exists (graphical
      polkit prompt), else ``("sudo", ["sudo", ...])`` which needs a terminal.
    """
    plat = platform or sys.platform
    cmd = self_command(args)
    if plat == "win32":
        return "shellexecute-runas", cmd
    if plat == "darwin":
        script = " ".join(shlex.quote(c) for c in cmd)
        script = script.replace("\\", "\\\\").replace('"', '\\"')
        return "osascript", ["osascript", "-e",
                             f'do shell script "{script}" with administrator privileges']
    if shutil.which("pkexec"):
        # pkexec drops the environment; keep the display so a GUI agent can prompt
        env = [f"{k}={os.environ[k]}" for k in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY")
               if k in os.environ]
        return "pkexec", ["pkexec", "env", *env, *cmd]
    return "sudo", ["sudo", *cmd]


def run_elevated(args: Sequence[str] = (), wait: bool = True) -> int:
    """Run ReWii with ``args`` elevated (prompting the user) and return its exit code.

    Only called by the UI. Returns -1 if the user cancelled the prompt or the
    exit code is unknown (``wait=False``).
    """
    method, argv = relaunch_elevated_command(args)
    if method == "shellexecute-runas":   # pragma: no cover - Windows only
        return _shellexecute_runas(argv, wait)
    proc = subprocess.Popen(argv)
    if not wait:
        return -1
    return proc.wait()


def _shellexecute_runas(argv: list[str], wait: bool) -> int:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = subprocess.list2cmdline(argv[1:])
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):  # type: ignore[attr-defined]
        return -1
    if not wait or not info.hProcess:
        return -1
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return int(code.value)


# --------------------------------------------------------------------------- planning
def _is_image(path: str) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def plan_format(drive: Drive, label: str = DEFAULT_LABEL, cluster_size: int = DEFAULT_CLUSTER,
                force: bool = False, platform: Optional[str] = None) -> FormatPlan:
    """Work out how ``drive`` would be formatted, without touching it.

    Raises :class:`FormatRefused` if the drive is a system/boot disk (always),
    or isn't removable/USB and ``force`` is False. Drives over 2 TB get only
    their first 2 TB used, with a warning.
    """
    if drive.is_system:
        raise FormatRefused(
            f"{drive.display_name} holds your computer's operating system. ReWii will never erase it.")
    if not (drive.removable or drive.is_usb) and not force:
        raise FormatRefused(
            f"{drive.display_name} is an internal drive, not a USB drive. ReWii only erases "
            "USB drives unless you explicitly force it.")
    try:
        lab = normalize_label(label)
    except Fat32Error as e:
        raise FormatRefused(str(e)) from e

    plat = platform or sys.platform
    image = _is_image(drive.device)
    warnings: list[str] = []
    size = drive.size
    if size > MAX_SIZE:
        size = MAX_SIZE
        warnings.append(
            f"This drive is {human_size(drive.size)}. A Wii-compatible (MBR) FAT32 drive can "
            "only use the first 2.2 TB; the rest will stay empty.")
    try:
        geo = plan_fat32(size, cluster_size, partition=True)
    except Fat32Error as e:
        raise FormatRefused(str(e)) from e
    if geo.cluster_size != cluster_size:
        warnings.append(f"The drive is small, so it will use {geo.cluster_size // 1024 or 0.5} KB "
                        f"clusters instead of {cluster_size // 1024} KB.")
    if drive.looks_like_flash:
        warnings.append("This looks like a USB flash stick. It works, but a hard drive or SSD "
                        "is faster and more reliable for games.")
    mounted = [v for v in drive.volumes if v.mount_points]
    if drive.volumes:
        warnings.append("Everything on this drive will be erased. Copy off anything you want to keep.")

    steps: list[FormatStep] = []
    raw = drive.device
    if image:
        steps.append(FormatStep("write", f"Write a new FAT32 file system to the image {drive.device}"))
        return FormatPlan(drive, plat, lab, geo.cluster_size, size, raw, steps, warnings,
                          requires_admin=False, is_image=True)

    if plat == "win32":
        for v in drive.volumes:
            steps.append(FormatStep("lock", f"Close and lock {v.mount_points[0] if v.mount_points else v.device}"))
        steps.append(FormatStep("write", f"Write the new partition table and FAT32 to {drive.device}"))
        steps.append(FormatStep("rescan", "Ask Windows to re-read the drive",
                                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 f"Update-Disk -Number {_win_disk_number(drive.device)}"]))
    elif plat == "darwin":
        steps.append(FormatStep("unmount", "Unmount the drive's volumes",
                                ["diskutil", "unmountDisk", "force", drive.device]))
        raw = drive.device.replace("/dev/disk", "/dev/rdisk", 1)
        steps.append(FormatStep("write", f"Write the new partition table and FAT32 to {raw}"))
        steps.append(FormatStep("mount", "Mount the new volume", ["diskutil", "mountDisk", drive.device]))
    else:
        for v in mounted:
            for mp in v.mount_points:
                if shutil.which("udisksctl"):
                    cmd = ["udisksctl", "unmount", "--no-user-interaction", "-b", v.device]
                else:
                    cmd = ["umount", mp]
                steps.append(FormatStep("unmount", f"Unmount {mp}", cmd))
                break
        steps.append(FormatStep("write", f"Write the new partition table and FAT32 to {drive.device}"))
        if shutil.which("blockdev"):
            rescan = ["blockdev", "--rereadpt", drive.device]
        else:
            rescan = ["partprobe", drive.device]
        steps.append(FormatStep("rescan", "Ask Linux to re-read the partition table", rescan))
        if shutil.which("udisksctl"):
            steps.append(FormatStep("mount", "Mount the new volume",
                                    ["udisksctl", "mount", "--no-user-interaction", "-b",
                                     _linux_part1(drive.device)]))
    return FormatPlan(drive, plat, lab, geo.cluster_size, size, raw, steps, warnings,
                      requires_admin=True)


def _win_disk_number(device: str) -> int:
    digits = "".join(c for c in device.rsplit("PhysicalDrive", 1)[-1] if c.isdigit())
    return int(digits or -1)


def _linux_part1(device: str) -> str:
    """First-partition device name: /dev/sdb -> /dev/sdb1, /dev/nvme0n1 -> /dev/nvme0n1p1."""
    return device + ("p1" if device[-1:].isdigit() else "1")


# --------------------------------------------------------------------------- execution
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[Any]]
Lister = Callable[[], list[Drive]]


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess[Any]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=flags)


def execute_plan(plan: FormatPlan, confirm_token: str,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 *, runner: CommandRunner = _default_run,
                 lister: Optional[Lister] = None) -> FormatResult:
    """Carry out ``plan``. ERASES THE DRIVE.

    ``confirm_token`` must equal ``plan.drive.id``. For real devices the drive
    list is re-read (``lister``, default ``list_drives(include_fixed=True)``)
    and the drive must still be there, unchanged and not a system disk.
    Raises :class:`FormatRefused` before touching anything, or
    :class:`FormatFailed` if an OS step fails.
    """
    drive = plan.drive
    if not confirm_token or confirm_token != drive.id:
        raise FormatRefused("Confirmation does not match this drive; nothing was changed.")
    if drive.is_system:
        raise FormatRefused("Refusing to erase a system disk.")
    if plan.is_image:
        if not _is_image(drive.device):
            raise FormatRefused(f"{drive.device} is no longer a regular file.")
        return format_fat32(drive.device, plan.size_bytes, plan.label, plan.cluster_size,
                            partition=True, progress=progress, cancel=cancel)

    current = (lister or (lambda: list_drives(include_fixed=True, do_enrich=False)))()
    match = [d for d in current if d.id == drive.id]
    if not match:
        raise FormatRefused("The drive is no longer connected (or has changed). Nothing was changed.")
    if match[0].is_system:
        raise FormatRefused("Refusing to erase a system disk.")
    if not is_admin() and (plan.platform == "win32" or not os.access(plan.raw_device, os.W_OK)):
        raise FormatRefused("Erasing a drive needs administrator rights.")
    if cancel is not None:
        cancel.check()

    if plan.platform == "win32":  # pragma: no cover - Windows only
        return _execute_windows(plan, progress, cancel, runner)

    for step in plan.steps:
        if step.kind == "unmount" and step.command:
            r = runner(step.command)
            if r.returncode != 0 and step.command[0] == "udisksctl":
                r = runner(["umount", step.command[-1]])   # retry with plain umount <device>
            if r.returncode != 0:
                raise FormatFailed(f"Couldn't unmount the drive ({_err(r)}). Close any programs "
                                   "using it and try again.")
    result = _write(plan, progress, cancel)
    for step in plan.steps:
        if step.kind in ("rescan", "mount") and step.command:
            r = runner(step.command)
            if r.returncode != 0 and step.kind == "rescan" and shutil.which("partprobe"):
                runner(["partprobe", drive.device])
    return result


def _err(r: subprocess.CompletedProcess[Any]) -> str:
    return (str(r.stderr or r.stdout or "").strip().splitlines() or [f"exit {r.returncode}"])[-1]


def _write(plan: FormatPlan, progress: Optional[ProgressFn],
           cancel: Optional[CancelToken]) -> FormatResult:
    try:
        fd = os.open(plan.raw_device, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError as e:
        raise FormatFailed(f"Couldn't open {plan.raw_device} for writing: {e.strerror}") from e
    with os.fdopen(fd, "r+b", buffering=0) as f:
        return format_fat32(f, plan.size_bytes, plan.label, plan.cluster_size, partition=True,
                            progress=progress, cancel=cancel)


# --------------------------------------------------------------------------- Windows internals
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_UNLOCK_VOLUME = 0x0009001C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140


def _execute_windows(plan: FormatPlan, progress: Optional[ProgressFn],
                     cancel: Optional[CancelToken],
                     runner: CommandRunner) -> FormatResult:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    INVALID = wintypes.HANDLE(-1).value
    GENERIC_RW = 0x80000000 | 0x40000000
    SHARE_RW = 0x1 | 0x2
    OPEN_EXISTING = 3

    def ioctl(h: int, code: int) -> bool:
        ret = wintypes.DWORD()
        return bool(k32.DeviceIoControl(h, code, None, 0, None, 0, ctypes.byref(ret), None))

    locked: list[int] = []
    try:
        for v in plan.drive.volumes:
            path = v.device if v.device.startswith("\\\\") else f"\\\\.\\{v.device.rstrip(':')}:"
            h = k32.CreateFileW(path, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
            if h == INVALID or h is None:
                continue  # volume without a mountable path (e.g. raw WBFS): nothing to lock
            locked.append(h)
            ok = False
            for _ in range(20):
                if ioctl(h, FSCTL_LOCK_VOLUME):
                    ok = True
                    break
                import time
                time.sleep(0.25)
            if not ok:
                raise FormatFailed(f"{v.mount_points[0] if v.mount_points else v.device} is in use. "
                                   "Close Explorer windows and programs using the drive, then try again.")
            ioctl(h, FSCTL_DISMOUNT_VOLUME)
        result = _write(plan, progress, cancel)
        hd = k32.CreateFileW(plan.raw_device, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if hd not in (INVALID, None):
            ioctl(hd, IOCTL_DISK_UPDATE_PROPERTIES)
            k32.CloseHandle(hd)
    finally:
        for h in locked:
            ioctl(h, FSCTL_UNLOCK_VOLUME)
            k32.CloseHandle(h)
    for step in plan.steps:
        if step.kind == "rescan" and step.command:
            runner(step.command)
    return result


def format_image(path: str | os.PathLike[str], size_bytes: int, label: str = DEFAULT_LABEL,
                 cluster_size: int = DEFAULT_CLUSTER, progress: Optional[ProgressFn] = None,
                 cancel: Optional[CancelToken] = None) -> FormatResult:
    """Create/format a disk image file (no admin, no OS steps). Convenience for tests/dev."""
    p = Path(path)
    if p.exists() and not p.is_file():
        raise FormatRefused(f"{p} is not a regular file.")
    return format_fat32(p, size_bytes, label, cluster_size, partition=True,
                        progress=progress, cancel=cancel)
