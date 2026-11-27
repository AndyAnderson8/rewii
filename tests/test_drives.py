"""Drive enumeration parsers, tested with captured/realistic tool output."""
from __future__ import annotations

import json
import plistlib

from rewii.core import drives as dr
from rewii.core.drives import (Drive, Volume, drive_for_path, enrich, list_drives, normalize_fs,
                               parse_diskutil, parse_diskutil_list, parse_lsblk, parse_windows)
from rewii.core.fat32 import build_boot_sector, plan_fat32

# --------------------------------------------------------------------------- Linux
LSBLK = r"""
{
   "blockdevices": [
      {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512110190592, "type": "disk",
       "fstype": null, "fsver": null, "label": null, "mountpoints": [null], "rm": false,
       "hotplug": false, "tran": "nvme", "model": "Samsung SSD 980 PRO 500GB", "serial": "S5GXNX0R123",
       "pttype": "gpt", "pkname": null,
       "children": [
          {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "size": 536870912, "type": "part",
           "fstype": "vfat", "fsver": "FAT32", "label": null, "mountpoints": ["/boot/efi"], "rm": false,
           "hotplug": false, "tran": "nvme", "model": null, "serial": null, "pttype": "gpt", "pkname": "nvme0n1"},
          {"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "size": 511571771392, "type": "part",
           "fstype": "crypto_LUKS", "fsver": "2", "label": null, "mountpoints": [null], "rm": false,
           "hotplug": false, "tran": "nvme", "model": null, "serial": null, "pttype": "gpt", "pkname": "nvme0n1",
           "children": [
              {"name": "luks-abc", "path": "/dev/mapper/luks-abc", "size": 511554994176, "type": "crypt",
               "fstype": "ext4", "fsver": "1.0", "label": null, "mountpoints": ["/"], "rm": false,
               "hotplug": false, "tran": null, "model": null, "serial": null, "pttype": null, "pkname": "nvme0n1p2"}
           ]}
       ]},
      {"name": "sda", "path": "/dev/sda", "size": 2000398934016, "type": "disk",
       "fstype": null, "fsver": null, "label": null, "mountpoints": [null], "rm": false,
       "hotplug": true, "tran": "usb", "model": "WD Elements 25A2", "serial": "575836314142",
       "pttype": "gpt", "pkname": null,
       "children": [
          {"name": "sda1", "path": "/dev/sda1", "size": 2000396836864, "type": "part",
           "fstype": "ntfs", "fsver": null, "label": "Elements", "mountpoints": ["/media/me/Elements"],
           "rm": false, "hotplug": true, "tran": "usb", "model": null, "serial": null, "pttype": "gpt", "pkname": "sda"}
       ]},
      {"name": "sdb", "path": "/dev/sdb", "size": 62109253632, "type": "disk",
       "fstype": null, "fsver": null, "label": null, "mountpoints": [null], "rm": true,
       "hotplug": true, "tran": "usb", "model": "Ultra Fit", "serial": "4C530001230811115372",
       "pttype": "dos", "pkname": null,
       "children": [
          {"name": "sdb1", "path": "/dev/sdb1", "size": 62108205056, "type": "part",
           "fstype": "vfat", "fsver": "FAT32", "label": "REWII", "mountpoints": ["/media/me/REWII"],
           "rm": true, "hotplug": true, "tran": "usb", "model": null, "serial": null, "pttype": "dos", "pkname": "sdb"}
       ]},
      {"name": "sdc", "path": "/dev/sdc", "size": 500107862016, "type": "disk",
       "fstype": null, "fsver": null, "label": null, "mountpoints": [null], "rm": false,
       "hotplug": true, "tran": "usb", "model": "External USB 3.0", "serial": "20170331000C3",
       "pttype": "dos", "pkname": null,
       "children": [
          {"name": "sdc1", "path": "/dev/sdc1", "size": 500106813440, "type": "part",
           "fstype": null, "fsver": null, "label": null, "mountpoints": [null],
           "rm": false, "hotplug": true, "tran": "usb", "model": null, "serial": null, "pttype": "dos", "pkname": "sdc"}
       ]},
      {"name": "sdd", "path": "/dev/sdd", "size": 1000204886016, "type": "disk",
       "fstype": null, "fsver": null, "label": null, "mountpoints": [null], "rm": false,
       "hotplug": false, "tran": "sata", "model": "ST1000DM010-2EP102", "serial": "Z9A1",
       "pttype": "gpt", "pkname": null,
       "children": [
          {"name": "sdd1", "path": "/dev/sdd1", "size": 1000202788864, "type": "part",
           "fstype": "ext4", "fsver": "1.0", "label": "data", "mountpoints": ["/mnt/data"],
           "rm": false, "hotplug": false, "tran": "sata", "model": null, "serial": null, "pttype": "gpt", "pkname": "sdd"}
       ]},
      {"name": "loop0", "path": "/dev/loop0", "size": 4096, "type": "loop", "fstype": "squashfs",
       "fsver": "4.0", "label": null, "mountpoints": ["/snap/bare/5"], "rm": false, "hotplug": false,
       "tran": null, "model": null, "serial": null, "pttype": null, "pkname": null}
   ]
}
"""

# util-linux < 2.37: MOUNTPOINT (singular), strings for numbers/flags
LSBLK_OLD = r"""
{"blockdevices": [
  {"name": "sdb", "path": "/dev/sdb", "size": "31914983424", "type": "disk", "fstype": "vfat",
   "label": "STICK", "mountpoint": "/media/STICK", "rm": "1", "hotplug": "1", "tran": "usb",
   "model": "Cruzer Blade    ", "pttype": null, "pkname": null}
]}
"""


def test_parse_lsblk():
    ds = {d.device: d for d in parse_lsblk(LSBLK)}
    assert set(ds) == {"/dev/nvme0n1", "/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"}
    nv = ds["/dev/nvme0n1"]
    assert nv.is_system and not nv.removable and nv.bus == "NVMe"   # "/" via LUKS child
    hdd = ds["/dev/sda"]
    assert (hdd.filesystem, hdd.partition_style, hdd.label) == ("NTFS", "GPT", "Elements")
    assert hdd.removable and hdd.is_usb and not hdd.is_system and not hdd.looks_like_flash
    assert hdd.mount_point == "/media/me/Elements"
    stick = ds["/dev/sdb"]
    assert (stick.filesystem, stick.partition_style, stick.volume_device) == ("FAT32", "MBR", "/dev/sdb1")
    assert stick.media_removable and stick.looks_like_flash
    assert stick.id == "sdb:4C530001230811115372:62109253632"
    raw = ds["/dev/sdc"]
    assert raw.filesystem == "unknown" and raw.mount_points == []
    assert not ds["/dev/sdd"].removable and not ds["/dev/sdd"].is_system


def test_parse_lsblk_old_format_superfloppy():
    (d,) = parse_lsblk(LSBLK_OLD)
    assert d.size == 31914983424 and d.removable and d.media_removable
    assert d.partition_style == "none"
    assert d.volumes[0].device == "/dev/sdb" and d.mount_point == "/media/STICK"
    assert d.model == "Cruzer Blade" and d.looks_like_flash


def test_list_drives_linux_filters(monkeypatch):
    def runner(args):
        assert args[:3] == ["lsblk", "-J", "-b"]
        return LSBLK
    usb = list_drives(platform="linux", runner=runner, do_enrich=False)
    assert [d.device for d in usb] == ["/dev/sda", "/dev/sdb", "/dev/sdc"]
    allfixed = list_drives(include_fixed=True, platform="linux", runner=runner, do_enrich=False)
    assert "/dev/sdd" in [d.device for d in allfixed]
    assert "/dev/nvme0n1" not in [d.device for d in allfixed]    # never the system disk


def test_list_drives_tool_failure_returns_empty():
    def boom(args):
        raise OSError("lsblk missing")
    assert list_drives(platform="linux", runner=boom) == []


def test_enrich_detects_wbfs_and_needs_admin():
    raw = next(d for d in parse_lsblk(LSBLK) if d.device == "/dev/sdc")
    enrich(raw, read_sector=lambda dev: b"WBFS" + bytes(508))
    assert raw.filesystem == "WBFS" and not raw.needs_admin
    raw2 = next(d for d in parse_lsblk(LSBLK) if d.device == "/dev/sdc")
    enrich(raw2, read_sector=lambda dev: None)
    assert raw2.needs_admin and raw2.filesystem == "unknown"


def test_enrich_cluster_from_boot_sector():
    d = Drive("x", "/dev/sdz", 64 * 1024 ** 3,
              volumes=[Volume("/dev/sdz1", 64 * 1024 ** 3, "FAT32")])
    boot = build_boot_sector(plan_fat32(64 * 1024 ** 3, 32768), 1, "REWII")
    enrich(d, read_sector=lambda dev: boot)
    assert d.cluster_size == 32768


# --------------------------------------------------------------------------- macOS
DISKUTIL_LIST_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>AllDisks</key>
	<array><string>disk4</string><string>disk4s1</string></array>
	<key>AllDisksAndPartitions</key>
	<array>
		<dict>
			<key>Content</key><string>FDisk_partition_scheme</string>
			<key>DeviceIdentifier</key><string>disk4</string>
			<key>OSInternal</key><false/>
			<key>Partitions</key>
			<array>
				<dict>
					<key>Content</key><string>DOS_FAT_32</string>
					<key>DeviceIdentifier</key><string>disk4s1</string>
					<key>DiskUUID</key><string>5E0F0E2C-0000-0000-0000-000000000000</string>
					<key>MountPoint</key><string>/Volumes/REWII</string>
					<key>Size</key><integer>62108205056</integer>
					<key>VolumeName</key><string>REWII</string>
					<key>VolumeUUID</key><string>3B7D1E5A-1C2B-3F2E-8C4B-2F1D9A6E7B11</string>
				</dict>
			</array>
			<key>Size</key><integer>62109253632</integer>
		</dict>
	</array>
	<key>VolumesFromDisks</key><array><string>REWII</string></array>
	<key>WholeDisks</key><array><string>disk4</string></array>
</dict>
</plist>
"""

DISK4_INFO = {
    "Bootable": False, "BusProtocol": "USB", "CanBeMadeBootable": False, "Content": "FDisk_partition_scheme",
    "DeviceBlockSize": 512, "DeviceIdentifier": "disk4", "DeviceNode": "/dev/disk4",
    "DeviceTreePath": "IODeviceTree:/arm-io@10F00000/usb-drd1@2280000", "Ejectable": True,
    "EjectableMediaAutomaticUnderSoftwareControl": False, "EjectableOnly": True,
    "IORegistryEntryName": "SanDisk Ultra Fit Media", "Internal": False,
    "MediaName": "Ultra Fit", "MediaType": "Generic", "Removable": True, "RemovableMedia": True,
    "RemovableMediaOrExternalDevice": True, "SMARTStatus": "Not Supported", "Size": 62109253632,
    "SolidState": False, "SupportsGlobalPermissionsDisable": False, "SystemImage": False,
    "TotalSize": 62109253632, "VirtualOrPhysical": "Physical", "WholeDisk": True, "Writable": True,
    "WritableMedia": True,
}
DISK4S1_INFO = {
    "BusProtocol": "USB", "Content": "DOS_FAT_32", "DeviceIdentifier": "disk4s1",
    "DeviceNode": "/dev/disk4s1", "FilesystemName": "MS-DOS FAT32", "FilesystemType": "msdos",
    "FilesystemUserVisibleName": "MS-DOS (FAT32)", "FreeSpace": 40000000000, "Internal": False,
    "MountPoint": "/Volumes/REWII", "ParentWholeDisk": "disk4", "Size": 62108205056,
    "TotalSize": 62108205056, "VolumeAllocationBlockSize": 32768, "VolumeName": "REWII",
    "WholeDisk": False, "Writable": True,
}


def _mac_full_list():
    """`diskutil list -plist` (no 'external'): internal disk0 + APFS container + disk image + USB HDD."""
    return {
        "AllDisksAndPartitions": [
            {"Content": "GUID_partition_scheme", "DeviceIdentifier": "disk0", "OSInternal": True,
             "Size": 500277790720, "Partitions": [
                 {"Content": "Apple_APFS_ISC", "DeviceIdentifier": "disk0s1", "Size": 524288000},
                 {"Content": "Apple_APFS", "DeviceIdentifier": "disk0s2", "Size": 494384795648}]},
            {"APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}], "Content": "",
             "DeviceIdentifier": "disk3", "OSInternal": False, "Size": 494384795648,
             "APFSVolumes": [{"DeviceIdentifier": "disk3s1", "MountPoint": "/System/Volumes/Data"}]},
            {"Content": "GUID_partition_scheme", "DeviceIdentifier": "disk5", "OSInternal": False,
             "Size": 4000787030016, "Partitions": [
                 {"Content": "EFI", "DeviceIdentifier": "disk5s1", "Size": 209715200, "VolumeName": "EFI"},
                 {"Content": "Microsoft Basic Data", "DeviceIdentifier": "disk5s2",
                  "Size": 4000575668224, "VolumeName": "My Passport", "MountPoint": "/Volumes/My Passport"}]},
            {"Content": "GUID_partition_scheme", "DeviceIdentifier": "disk6", "OSInternal": False,
             "Size": 104857600, "Partitions": []},
        ],
        "WholeDisks": ["disk0", "disk3", "disk5", "disk6"],
    }


def test_parse_diskutil_external_stick():
    assert parse_diskutil_list(DISKUTIL_LIST_XML) == ["disk4"]
    infos = {"disk4": plistlib.dumps(DISK4_INFO), "disk4s1": plistlib.dumps(DISK4S1_INFO)}
    (d,) = parse_diskutil(DISKUTIL_LIST_XML, infos)
    assert d.device == "/dev/disk4" and d.volume_device == "/dev/disk4s1"
    assert (d.filesystem, d.partition_style, d.cluster_size) == ("FAT32", "MBR", 32768)
    assert d.free == 40000000000 and d.mount_point == "/Volumes/REWII"
    assert d.is_usb and d.removable and not d.is_system and d.looks_like_flash
    assert d.model == "Ultra Fit"


def test_parse_diskutil_full_list():
    infos = {
        "disk0": {"BusProtocol": "Apple Fabric", "Internal": True, "MediaName": "APPLE SSD AP0512Q",
                  "VirtualOrPhysical": "Physical"},
        "disk3": {"BusProtocol": "Apple Fabric", "Internal": True, "VirtualOrPhysical": "Virtual"},
        "disk5": {"BusProtocol": "USB", "Internal": False, "MediaName": "WD My Passport 25E2",
                  "RemovableMedia": False, "Ejectable": True, "VirtualOrPhysical": "Physical"},
        "disk5s2": {"FilesystemType": "exfat", "FilesystemName": "ExFAT", "FreeSpace": 3000000000000,
                    "MountPoint": "/Volumes/My Passport", "VolumeAllocationBlockSize": 131072},
        "disk6": {"BusProtocol": "Disk Image", "Internal": False, "VirtualOrPhysical": "Virtual"},
    }
    ds = {d.device: d for d in parse_diskutil(_mac_full_list(), infos, root_disks=["disk0"])}
    assert set(ds) == {"/dev/disk0", "/dev/disk5"}      # synthesized APFS + disk image skipped
    assert ds["/dev/disk0"].is_system
    hdd = ds["/dev/disk5"]
    assert (hdd.filesystem, hdd.partition_style, hdd.label) == ("exFAT", "GPT", "My Passport")
    assert len(hdd.volumes) == 1                         # EFI partition hidden
    assert hdd.removable and not hdd.is_system and not hdd.looks_like_flash
    assert hdd.size > 2 * 1024 ** 4


def test_list_drives_macos_runner():
    calls = []

    def run_bytes(args):
        calls.append(args)
        if args[:3] == ["diskutil", "list", "-plist"]:
            assert args[-1] == "external"
            return DISKUTIL_LIST_XML
        ident = args[-1]
        if ident == "/":
            return plistlib.dumps({"ParentWholeDisk": "disk3",
                                   "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}]})
        return plistlib.dumps({"disk4": DISK4_INFO, "disk4s1": DISK4S1_INFO}[ident])

    (d,) = list_drives(platform="darwin", bytes_runner=run_bytes, do_enrich=False)
    assert d.device == "/dev/disk4"
    assert ["diskutil", "info", "-plist", "disk4s1"] in calls


# --------------------------------------------------------------------------- Windows
WINDOWS_JSON = r"""
{
    "disks":  [
                  {"Number": 0, "FriendlyName": "Samsung SSD 970 EVO Plus 1TB", "Model": "Samsung SSD 970 EVO Plus 1TB",
                   "SerialNumber": "0025_3852_91B0_1234.", "UniqueId": "eui.0025385291B01234",
                   "Size": 1000204886016, "PartitionStyle": "GPT", "BusType": "NVMe",
                   "IsBoot": true, "IsSystem": true, "IsOffline": false, "IsReadOnly": false, "BootFromDisk": true},
                  {"Number": 1, "FriendlyName": "SanDisk Ultra", "Model": "Ultra", "SerialNumber": "4C530001230811115372",
                   "UniqueId": "USBSTOR\\DISK&VEN_SANDISK&PROD_ULTRA&REV_1.00\\4C530001230811115372&0:DESKTOP-1",
                   "Size": 62109253632, "PartitionStyle": "MBR", "BusType": "USB",
                   "IsBoot": false, "IsSystem": false, "IsOffline": false, "IsReadOnly": false, "BootFromDisk": false},
                  {"Number": 2, "FriendlyName": "WD Elements 25A2", "Model": "Elements 25A2", "SerialNumber": "575836314142",
                   "UniqueId": "USBSTOR\\DISK&VEN_WD&PROD_ELEMENTS_25A2\\575836314142&0:DESKTOP-1",
                   "Size": 2000398934016, "PartitionStyle": 2, "BusType": 7,
                   "IsBoot": false, "IsSystem": false, "IsOffline": false, "IsReadOnly": false, "BootFromDisk": false},
                  {"Number": 3, "FriendlyName": "Generic STORAGE DEVICE", "SerialNumber": "000000009451",
                   "UniqueId": "USBSTOR\\X", "Size": 500107862016, "PartitionStyle": "MBR", "BusType": "USB",
                   "IsBoot": false, "IsSystem": false, "IsOffline": false, "IsReadOnly": false, "BootFromDisk": false}
              ],
    "partitions":  [
                       {"DiskNumber": 0, "PartitionNumber": 1, "DriveLetter": "\u0000", "Offset": 1048576,
                        "Size": 104857600, "MbrType": null, "GptType": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}",
                        "IsBoot": false, "IsSystem": true, "IsActive": false,
                        "AccessPaths": ["\\\\?\\Volume{1b2a3c4d-0000-0000-0000-100000000000}\\"], "Type": "System"},
                       {"DiskNumber": 0, "PartitionNumber": 3, "DriveLetter": "C", "Offset": 122683392,
                        "Size": 999000000000, "MbrType": null, "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
                        "IsBoot": true, "IsSystem": false, "IsActive": false,
                        "AccessPaths": ["C:\\", "\\\\?\\Volume{aaaa0000-0000-0000-0000-000000000003}\\"], "Type": "Basic"},
                       {"DiskNumber": 1, "PartitionNumber": 1, "DriveLetter": "E", "Offset": 1048576,
                        "Size": 62108205056, "MbrType": 12, "GptType": null,
                        "IsBoot": false, "IsSystem": false, "IsActive": false,
                        "AccessPaths": ["E:\\", "\\\\?\\Volume{bbbb0000-0000-0000-0000-000000000001}\\"], "Type": "FAT32 XINT13"},
                       {"DiskNumber": 2, "PartitionNumber": 1, "DriveLetter": "\u0000", "Offset": 17408,
                        "Size": 16759808, "MbrType": null, "GptType": "{e3c9e316-0b5c-4db8-817d-f92df00215ae}",
                        "IsBoot": false, "IsSystem": false, "IsActive": false, "AccessPaths": null, "Type": "Reserved"},
                       {"DiskNumber": 2, "PartitionNumber": 2, "DriveLetter": 70, "Offset": 16777216,
                        "Size": 2000381018112, "MbrType": null, "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
                        "IsBoot": false, "IsSystem": false, "IsActive": false,
                        "AccessPaths": ["F:\\", "\\\\?\\Volume{cccc0000-0000-0000-0000-000000000002}\\"], "Type": "Basic"},
                       {"DiskNumber": 3, "PartitionNumber": 1, "DriveLetter": "\u0000", "Offset": 1048576,
                        "Size": 500106813440, "MbrType": 37, "GptType": null,
                        "IsBoot": false, "IsSystem": false, "IsActive": false,
                        "AccessPaths": ["\\\\?\\Volume{dddd0000-0000-0000-0000-000000000004}\\"], "Type": "Unknown"}
                   ],
    "volumes":  [
                    {"DriveLetter": "C", "FileSystem": "NTFS", "FileSystemLabel": "Windows", "Size": 999000000000,
                     "SizeRemaining": 500000000000, "UniqueId": "\\\\?\\Volume{aaaa0000-0000-0000-0000-000000000003}\\",
                     "AllocationUnitSize": 4096, "DriveType": "Fixed"},
                    {"DriveLetter": "E", "FileSystem": "FAT32", "FileSystemLabel": "REWII", "Size": 62091395072,
                     "SizeRemaining": 50000000000, "UniqueId": "\\\\?\\Volume{bbbb0000-0000-0000-0000-000000000001}\\",
                     "AllocationUnitSize": 32768, "DriveType": "Removable"},
                    {"DriveLetter": "F", "FileSystem": "NTFS", "FileSystemLabel": "Elements", "Size": 2000381014016,
                     "SizeRemaining": 1500000000000, "UniqueId": "\\\\?\\Volume{cccc0000-0000-0000-0000-000000000002}\\",
                     "AllocationUnitSize": 4096, "DriveType": "Fixed"},
                    {"DriveLetter": "", "FileSystem": "", "FileSystemLabel": "", "Size": 0,
                     "SizeRemaining": 0, "UniqueId": "\\\\?\\Volume{dddd0000-0000-0000-0000-000000000004}\\",
                     "AllocationUnitSize": 0, "DriveType": "Fixed"}
                ],
    "systemdrive":  "C:"
}
"""


def test_parse_windows():
    ds = {d.device: d for d in parse_windows(WINDOWS_JSON)}
    assert set(ds) == {f"\\\\.\\PhysicalDrive{i}" for i in range(4)}
    assert ds["\\\\.\\PhysicalDrive0"].is_system
    stick = ds["\\\\.\\PhysicalDrive1"]
    assert (stick.filesystem, stick.partition_style, stick.cluster_size) == ("FAT32", "MBR", 32768)
    assert stick.mount_point == "E:\\" and stick.volume_device == "\\\\.\\E:"
    assert stick.free == 50000000000 and stick.media_removable and stick.looks_like_flash
    assert stick.is_usb and stick.removable and stick.label == "REWII"
    hdd = ds["\\\\.\\PhysicalDrive2"]                 # integer enums + char-code letter
    assert (hdd.bus, hdd.partition_style, hdd.filesystem) == ("USB", "GPT", "NTFS")
    assert hdd.mount_point == "F:\\" and len(hdd.volumes) == 1   # MSR partition hidden
    assert not hdd.looks_like_flash and not hdd.is_system
    raw = ds["\\\\.\\PhysicalDrive3"]                 # raw WBFS-style partition, no letter
    assert raw.filesystem == "unknown" and raw.mount_points == []
    assert raw.volume_device.startswith("\\\\?\\Volume{dddd")


def test_parse_windows_single_objects():
    data = json.loads(WINDOWS_JSON)
    data["disks"] = data["disks"][1]
    data["partitions"] = data["partitions"][2]
    data["volumes"] = data["volumes"][1]
    (d,) = parse_windows(data)
    assert d.filesystem == "FAT32" and d.mount_point == "E:\\"


def test_list_drives_windows_runner():
    def runner(args):
        assert args[0] == "powershell" and "-NonInteractive" in args
        return WINDOWS_JSON
    out = list_drives(platform="win32", runner=runner, do_enrich=False)
    assert [d.device for d in out] == ["\\\\.\\PhysicalDrive1", "\\\\.\\PhysicalDrive2",
                                       "\\\\.\\PhysicalDrive3"]


# --------------------------------------------------------------------------- misc
def test_normalize_fs():
    assert normalize_fs("vfat", "FAT32") == "FAT32"
    assert normalize_fs("vfat", "FAT16") == "FAT16"
    assert normalize_fs("msdos") == "FAT32"
    assert normalize_fs("exfat") == "exFAT"
    assert normalize_fs("ntfs") == "NTFS"
    assert normalize_fs("hfsplus") == "HFS"
    assert normalize_fs("apfs") == "APFS"
    assert normalize_fs(None) == "unknown"
    assert normalize_fs("LVM2_member") == "unknown"


def test_drive_for_path(tmp_path):
    a = tmp_path / "a"
    (a / "wbfs").mkdir(parents=True)
    d = Drive("id1", "/dev/sdx", 10, volumes=[
        Volume("/dev/sdx2", 5, "NTFS", mount_points=[str(tmp_path / "other")]),
        Volume("/dev/sdx1", 4, "FAT32", mount_points=[str(a)]),
    ])
    got = drive_for_path(a / "wbfs", [d])
    assert got is not None and got.volume_device == "/dev/sdx1" and got.filesystem == "FAT32"
    # not on any listed drive -> falls back to psutil (the volume holding tmp_path)
    fallback = drive_for_path(tmp_path, [])
    assert fallback is None or fallback.mount_points


def test_display_name_and_human_size():
    d = Drive("i", "/dev/sdb", 64_000_000_000, model="Ultra Fit",
              volumes=[Volume("/dev/sdb1", 1, mount_points=["/media/x"])])
    assert d.display_name == "Ultra Fit 64.0 GB (/media/x)"
    assert dr.human_size(None) == "?"
