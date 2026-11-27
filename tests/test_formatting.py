"""Format planning (fake drives) and execution (image files only - never real devices)."""
from __future__ import annotations

import subprocess

import pytest

from rewii.core import formatting as fm
from rewii.core.drives import Drive, Volume
from rewii.core.fat32 import MAX_SIZE, PART_START_LBA, parse_boot_sector, parse_mbr
from rewii.core.formatting import (FormatRefused, execute_plan, helper_args, plan_format,
                                   relaunch_elevated_command)
from rewii.core.fileops import make_sparse, set_length

GiB = 1024 ** 3


def usb(**kw) -> Drive:
    base = dict(id="sdb:SER:64000000000", device="/dev/sdb", size=64_000_000_000, model="Ultra Fit",
                bus="USB", removable=True, is_usb=True, partition_style="GPT",
                volumes=[Volume("/dev/sdb1", 63_000_000_000, "exFAT", "STICK",
                                ["/media/me/STICK"], free=10)])
    base.update(kw)
    return Drive(**base)


def test_refuses_system_disk_even_with_force():
    with pytest.raises(FormatRefused):
        plan_format(usb(is_system=True), force=True, platform="linux")


def test_refuses_internal_unless_forced():
    d = usb(removable=False, is_usb=False, bus="SATA")
    with pytest.raises(FormatRefused):
        plan_format(d, platform="linux")
    assert plan_format(d, force=True, platform="linux").steps


def test_refuses_bad_label():
    with pytest.raises(FormatRefused):
        plan_format(usb(), label="WAY TOO LONG NAME", platform="linux")


def test_linux_plan(monkeypatch):
    monkeypatch.setattr(fm.shutil, "which", lambda n: f"/usr/bin/{n}")
    plan = plan_format(usb(), platform="linux")
    kinds = [s.kind for s in plan.steps]
    assert kinds == ["unmount", "write", "rescan", "mount"]
    assert plan.steps[0].command == ["udisksctl", "unmount", "--no-user-interaction", "-b", "/dev/sdb1"]
    assert plan.steps[2].command == ["blockdev", "--rereadpt", "/dev/sdb"]
    assert plan.steps[3].command[-1] == "/dev/sdb1"
    assert plan.raw_device == "/dev/sdb" and plan.requires_admin and not plan.is_image
    assert plan.cluster_size == 32768 and plan.label == "REWII"
    assert any("flash stick" in w for w in plan.warnings)
    assert "32 KB clusters" in plan.summary


def test_nvme_partition_naming(monkeypatch):
    monkeypatch.setattr(fm.shutil, "which", lambda n: f"/usr/bin/{n}")
    plan = plan_format(usb(device="/dev/nvme1n1", volumes=[]), platform="linux")
    assert plan.steps[-1].command[-1] == "/dev/nvme1n1p1"


def test_macos_plan():
    d = usb(device="/dev/disk4", volumes=[Volume("/dev/disk4s1", 1, "exFAT", mount_points=["/Volumes/X"])])
    plan = plan_format(d, platform="darwin")
    assert plan.raw_device == "/dev/rdisk4"
    assert plan.steps[0].command == ["diskutil", "unmountDisk", "force", "/dev/disk4"]
    assert plan.steps[-1].command == ["diskutil", "mountDisk", "/dev/disk4"]


def test_windows_plan():
    d = usb(device="\\\\.\\PhysicalDrive2", volumes=[Volume("\\\\.\\E:", 1, "exFAT", mount_points=["E:\\"])])
    plan = plan_format(d, platform="win32")
    assert [s.kind for s in plan.steps] == ["lock", "write", "rescan"]
    assert plan.steps[-1].command[-1] == "Update-Disk -Number 2"
    assert plan.raw_device == "\\\\.\\PhysicalDrive2"


def test_big_drive_uses_first_2tib():
    plan = plan_format(usb(size=4_000_787_030_016), platform="linux")
    assert plan.size_bytes == MAX_SIZE
    assert any("2.2 TB" in w for w in plan.warnings)


def test_small_drive_cluster_warning():
    plan = plan_format(usb(size=1 * GiB), platform="linux")
    assert plan.cluster_size < 32768
    assert any("clusters instead" in w for w in plan.warnings)


# --------------------------------------------------------------------------- execution
def image_drive(tmp_path, size=256 * 1024 ** 2) -> Drive:
    img = tmp_path / "usb.img"
    with open(img, "wb") as f:
        make_sparse(f)
        set_length(f, size)
    return Drive(id="image:usb", device=str(img), size=size, removable=True, is_usb=True)


def test_execute_image(tmp_path):
    d = image_drive(tmp_path)
    plan = plan_format(d, label="wii")
    assert plan.is_image and not plan.requires_admin
    res = execute_plan(plan, confirm_token="image:usb")
    with open(d.device, "rb") as f:
        mbr = f.read(512)
        assert parse_mbr(mbr)[0][:2] == (0x0C, PART_START_LBA)
        f.seek(PART_START_LBA * 512)
        info = parse_boot_sector(f.read(512))
    assert info.filesystem == "FAT32" and info.label == "WII"
    assert res.label == "WII"


def test_execute_requires_matching_token(tmp_path):
    d = image_drive(tmp_path)
    plan = plan_format(d)
    for bad in ("", "image:other", "USB"):
        with pytest.raises(FormatRefused):
            execute_plan(plan, confirm_token=bad)
    with open(d.device, "rb") as f:
        assert f.read(512) == bytes(512)        # untouched


def test_execute_device_rechecks_presence_and_never_writes(monkeypatch):
    """Real-device path: drive gone/changed -> refuse before any command or write."""
    plan = plan_format(usb(), platform="linux")
    ran = []
    monkeypatch.setattr(fm, "_write", lambda *a: pytest.fail("must not write"))

    def runner(cmd):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.raises(FormatRefused):
        execute_plan(plan, plan.drive.id, runner=runner, lister=lambda: [])
    changed = usb(id="sdb:OTHER:64000000000")
    with pytest.raises(FormatRefused):
        execute_plan(plan, plan.drive.id, runner=runner, lister=lambda: [changed])
    now_system = usb(is_system=True)
    with pytest.raises(FormatRefused):
        execute_plan(plan, plan.drive.id, runner=runner, lister=lambda: [now_system])
    assert ran == []


def test_execute_device_flow_with_fakes(monkeypatch):
    """Step order for a real device, with the write and commands faked out."""
    monkeypatch.setattr(fm.shutil, "which", lambda n: f"/usr/bin/{n}")
    plan = plan_format(usb(), platform="linux")
    log = []
    monkeypatch.setattr(fm, "is_admin", lambda: True)
    monkeypatch.setattr(fm, "_write", lambda p, prog, c: log.append(("write", p.raw_device)) or "ok")

    def runner(cmd):
        log.append(tuple(cmd[:2]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert execute_plan(plan, plan.drive.id, runner=runner, lister=lambda: [usb()]) == "ok"
    assert log == [("udisksctl", "unmount"), ("write", "/dev/sdb"), ("blockdev", "--rereadpt"),
                   ("udisksctl", "mount")]


def test_execute_unmount_failure(monkeypatch):
    monkeypatch.setattr(fm.shutil, "which", lambda n: f"/usr/bin/{n}")
    plan = plan_format(usb(), platform="linux")
    monkeypatch.setattr(fm, "is_admin", lambda: True)
    monkeypatch.setattr(fm, "_write", lambda *a: pytest.fail("must not write"))
    with pytest.raises(fm.FormatFailed):
        execute_plan(plan, plan.drive.id, lister=lambda: [usb()],
                     runner=lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "target is busy"))


def test_elevation_commands(monkeypatch):
    method, argv = relaunch_elevated_command(helper_args("sdb:X:1"), platform="darwin")
    assert method == "osascript" and argv[0] == "osascript"
    assert "with administrator privileges" in argv[-1] and "format-drive" in argv[-1]
    method, argv = relaunch_elevated_command(["x"], platform="win32")
    assert method == "shellexecute-runas" and argv[-1] == "x"
    monkeypatch.setattr(fm.shutil, "which", lambda n: None)
    method, argv = relaunch_elevated_command(["x"], platform="linux")
    assert method == "sudo" and argv[0] == "sudo"
    monkeypatch.setattr(fm.shutil, "which", lambda n: "/usr/bin/pkexec")
    method, argv = relaunch_elevated_command(["x"], platform="linux")
    assert method == "pkexec" and argv[-1] == "x"
    assert isinstance(fm.is_admin(), bool)


def test_execute_refuses_internal_disk_unless_plan_was_forced(monkeypatch):
    monkeypatch.setattr(fm, "_write", lambda *a: pytest.fail("must not write"))
    plan = plan_format(usb(), platform="linux")
    internal = usb(removable=False, is_usb=False)       # same id, but it is really an internal disk
    with pytest.raises(FormatRefused):
        execute_plan(plan, plan.drive.id, lister=lambda: [internal])


def test_post_write_step_failure_does_not_fail_the_format(monkeypatch):
    monkeypatch.setattr(fm.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(fm, "is_admin", lambda: True)
    monkeypatch.setattr(fm, "_write", lambda p, prog, c: "ok")
    plan = plan_format(usb(), platform="linux")

    def runner(cmd):
        if cmd[0] == "blockdev" or cmd[:2] == ["udisksctl", "mount"]:
            raise subprocess.TimeoutExpired(cmd, 120)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert execute_plan(plan, plan.drive.id, runner=runner, lister=lambda: [usb()]) == "ok"
