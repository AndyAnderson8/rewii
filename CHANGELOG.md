# Changelog

All notable changes to ReWii. Versions follow [Semantic Versioning](https://semver.org/);
while the version is 0.x, releases are marked as pre-releases.

## [Unreleased]

## [0.1.0] - 2026-11-27

First release. **Pre-release:** everything is tested automatically on Windows, macOS
and Linux, but nothing has been tried on a real Wii yet, and drive formatting has
only been tested on disk images. Keep Priiloader installed and a NAND backup at hand.

### Games
- Add Wii and GameCube games from ISO, WBFS, CISO, RVZ, WIA, NKit and CleanRip dumps;
  correct `wbfs/` / `games/` layout, automatic 4 GB splitting, optional scrubbing.
- Verify games (Wii cluster hashes; refuses truncated or incomplete images), copy
  back to the PC, rename, fix misplaced games, import from old WBFS-formatted drives.
- Cover art from GameTDB; cheat codes with a picker that builds `.gct` files.

### Channels and WADs
- Wii Menu channels for Wii games with a cover-art, black or classic launch screen.
- Wii Menu channels for GameCube games (start straight in Nintendont).
- WAD inspection and staging for YAWMME; WADs that can brick a Wii (wrong region or
  console, stub or modified system files, System Menu downgrades) are blocked.

### Homebrew, saves, backups
- Open Shop Channel browser with install/update/remove and a one-click starter kit.
- Wii and GameCube save backups with version history; restore any version.
- Whole-drive snapshots, compare, restore and clone; verified NAND backups and an SD
  rescue kit for BootMii.

### Drive and system
- Drive health check with safe automatic fixes; FAT32 formatting at any size.
- Step-by-step guides for the parts done on the Wii, SysCheck reports, cIOS and 4.3
  update files from Nintendo's servers, HackMii installer, Priiloader hacks,
  bootmii.ini and USB Loader GX settings, Riivolution mods, themes.

### Downloads
Single-file apps for Windows (x86-64), macOS (Apple silicon and Intel) and Linux
(x86-64). `SHA256SUMS.txt` lists their checksums. On macOS the first launch needs
right-click → Open (the app isn't signed).

[Unreleased]: https://github.com/AndyAnderson8/rewii/compare/v0.1.0...dev
[0.1.0]: https://github.com/AndyAnderson8/rewii/releases/tag/v0.1.0
