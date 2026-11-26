# ReWii

**The all-in-one toolkit for softmodded Wiis.** One app for everything you do on the computer side once your Wii has the Homebrew Channel:

- **Games** – add Wii and GameCube games (ISO, WBFS, CISO, RVZ, WIA, NKit, CleanRip dumps), put them on the drive in the right layout, verify them, back them up, rename them, download covers, and pick cheat codes (for USB Loader GX and GameCube channels).
- **Channels** – put any Wii or GameCube game on the Wii Menu. For Wii games, choose the launch screen: the game's **cover art** or a plain **black** screen. GameCube channels start the game straight in Nintendont.
- **WADs** – inspect any WAD (type, region, IOS, signature, damage) and stage it for YAWMME. Dangerous ones (wrong region or console system files) are blocked.
- **Homebrew** – browse, install and update all 300+ Open Shop Channel apps, or send one straight to the Wii over Wi-Fi.
- **Saves** – back up Wii and GameCube saves with full version history, and restore any version.
- **Backups** – snapshot the whole drive, clone it to a bigger drive, and keep verified NAND backups with an SD rescue kit for BootMii.
- **Drive** – detect problems and fix them, **format FAT32** at any size (Windows can't format drives over 32 GB as FAT32), and set up the essential apps in one click.
- **System & Guides** – step-by-step guides for the parts done on the Wii itself: NAND backup, Priiloader, d2x cIOS, installing channels, saves, online play. ReWii downloads the files each guide needs. It can also read SysCheck reports, update the Homebrew Channel and BootMii (HackMii installer), keep Priiloader's hack list current or add patches, and edit BootMii's and USB Loader GX's settings.

Exploiting the Wii (installing the Homebrew Channel) is out of scope. See [wii.hacks.guide](https://wii.hacks.guide) for that.

## The ReWii way: one USB drive

Everything lives on one FAT32 USB drive; no SD card needed. Plug it into the Wii's **back USB port closest to the edge**.

```
apps/                       homebrew (USB Loader GX, Nintendont, YAWMME, …)
wbfs/Title [ID]/ID.wbfs     Wii games (split into .wbf1… over 4 GB)
games/Title [ID]/game.iso   GameCube games (+ disc2.iso)
txtcodes/  codes/           cheats
savegames/  saves/          Wii saves / Nintendont memory cards
wad/                        WADs waiting to be installed with YAWMME
```

An SD card is only needed for BootMii (NAND restore) and the Wii Menu's own Data Management.

## Getting it

Download the file for your computer from Releases and run it. There's nothing to install.

| OS | File |
|---|---|
| Windows | `rewii-windows-x86_64.exe` (double-click) |
| macOS (Apple Silicon / Intel) | `rewii-macos-arm64` / `rewii-macos-x86_64`. The first time, right-click → Open, or run `xattr -d com.apple.quarantine rewii-macos-*` |
| Linux | `rewii-linux-x86_64` (`chmod +x`, then run it in a terminal) |

It runs in a terminal window and works with the mouse or keyboard.

### From source

```
uv sync
uv run rewii                # the app
uv run rewii --help         # scriptable commands (games, channel, verify, health, drives, …)
uv run pytest               # tests
uv run pyinstaller packaging/rewii.spec   # build the single-file app
```

## Where things are kept

- **Library** (games, channels, saves, backups): `~/ReWii` by default. You can change it in Settings.
- **Settings and log**: the per-OS config folder (`%APPDATA%\ReWii`, `~/Library/Application Support/ReWii`, `~/.config/ReWii`).
- **Downloads cache** (covers, app zips, title database, tools): the per-OS cache folder.

## Safety

- Nothing is written to a drive without asking first. Formatting requires typing `ERASE`, and system disks are never offered.
- WADs that can brick a Wii (another region's or console's System Menu or IOS, boot2) are blocked.
- Channels are never made for games known to brick the Wii Menu (Mario Party 9, A Boy and His Blob).
- NAND rescue kits refuse a backup from a different console.

## Notes on game channels

ReWii's channels carry their own copy of Configurable USB Loader in "direct start" mode (as CRAP/make_wad.py did), so they need nothing installed except the d2x cIOS. They boot the game straight from `wbfs/` on the USB drive.

- **Cover art** style: the game's cover replaces the loader's splash screen while the game loads.
- **Black** style: the loader is started with `intro=0`, so the screen stays black until the game starts.
- **Classic** style: the stock loader screen.

**GameCube channels** contain their own copy of Nintendont (v6.509 or newer, taken from the Open Shop Channel). It's started with an autoboot config pointing at the game's folder on the USB drive, and with AHB access in the channel's TMD. The channel banner is built from USB Loader GX's GameCube banner template: the disc's own banner picture becomes the channel picture and icon, and the title, publisher, year and description from GameTDB fill the template's text. Your `nincfg.bin` settings (video mode, memory card size…) are carried over.

- Make the channel for disc 1; Nintendont switches to `disc2.iso` itself.
- The channel points at the game's folder on the drive. If you rename or move the game, make the channel again.

## License

ReWii is free software under the **GNU GPL v3 or later** (see [LICENSE](LICENSE)). Bundled third-party files keep their own licenses (see [NOTICE.md](NOTICE.md)).

## Credits

ReWii builds on the work of the Wii homebrew community: wii.hacks.guide, GameTDB, the Open Shop Channel, libWiiPy, nod, USB Loader GX, Configurable USB Loader, Nintendont, and many more. See [NOTICE.md](NOTICE.md).
