# ReWii: Requirements (DRAFT v0.2, awaiting review)

**ReWii** is an all-in-one terminal app for managing a softmodded Wii from your computer. "ReWii AIO" is the tagline and long name; the command is `rewii`.

Status: research complete, no code written yet. v0.2 folds in the decisions from the first review.

## 1. Goal

One app that does everything on the **computer side** for a Wii that already has the Homebrew Channel. It covers games (Wii and GameCube), channels, WADs, homebrew apps, saves, cheats, covers, drive setup, and backup/restore. It should be simple enough for non-technical people: download one file, run it, and follow the menus.

**Out of scope:**
- The jailbreak/exploit itself (Letterbomb, Bluebomb, str2hax, the HackMii install).
- Talking to the Wii directly. The one exception is the optional wiiload network send.

## 2. Decisions so far

| # | Decision |
|---|---|
| D1 | Name: **ReWii**. It's free on PyPI and GitHub, and no web search result for it is a Wii tool. |
| D2 | Runs on **Windows, macOS and Linux**. |
| D3 | Users never install Python. We ship one **standalone executable per OS** from GitHub Releases. The core is pure Python with no required external tools. |
| D4 | **The ReWii way is one USB drive and no SD card.** The drive is FAT32 (MBR) and holds homebrew apps, Wii games, GameCube games, covers, cheats, saves and WADs. An SD card is supported but optional, only for the few things that need one (see 3.2). |
| D5 | Region-agnostic and mostly console-agnostic. Console type and region matter only for system-level downloads (see 3.3). |
| D6 | Development happens in this local git repo. GitHub comes later. Nothing is written to the NAS. |
| D7 | Game library location is a user-chosen folder, defaulting to `~/ReWii/`. |

## 3. Research findings

### 3.1 The author's current setup (read from the NAS, reference only)
This is the legacy stack:
- a raw WBFS-formatted USB partition
- CFG USB Loader, with channels built by `make_wad.py` (Conf USB Loader forwarders)
- WAD Manager, Wii Backup Manager and SaveGame Manager GX
- Nintendont GameCube games on the SD card
- a BootMii NAND backup

The current wii.hacks.guide setup instead uses a FAT32 drive with USB Loader GX or WiiFlow Lite, YAWMME and TinyWiiBackupManager. ReWii follows the current guide. `make_wad.py`'s WBFS reader and WAD builder get ported into ReWii, and it already has a FAT variant.

### 3.2 USB-only (D4): what works without an SD card
- **Works from USB:**
  - The Homebrew Channel loads `apps/` from USB.
  - USB Loader GX, WiiFlow Lite and Nintendont work from USB, and so do GameCube games in `games/`.
  - YAWMME, Priiloader, the d2x cIOS installer, SaveGame Manager GX, WiiXplorer, SysCheck and nanddumper@ios (a NAND backup tool) all run from USB.
  - The Wii mini has no SD slot, so the whole guide already supports USB-only for it.
- **Needs an SD card:**
  - BootMii itself, which is needed for a **NAND restore** (unbricking).
  - The Wii Menu's own Data Management save copy, which writes to SD.
  - CleanRip disc dumping can use USB, so it doesn't need one.

  ReWii offers a small "SD rescue kit" (BootMii files + `nand.bin`/`keys.bin`) for recovery. Everything else goes on the USB drive.
- **Practical notes for the guide:**
  - Use the **rear USB port closest to the edge** (port 0).
  - A hard drive or SSD is better than a flash stick, and 3.5" drives need external power.
  - Wii games need roughly 128 GB or more.

### 3.3 Does console or region matter?
- **Games, GameCube games, covers, cheats, homebrew and saves:** no. Loaders handle region, and the game file brings its own ID. ReWii just copies the right files into the right places.
- **Where region and console type matter** (only "system" features):
  - Downloading IOS and cIOS base WADs and System Menu updates: the wrong ones **can brick** the console.
  - Themes, which must match the menu region and version.
  - NAND restore, which must use that console's own backup.
  - Channel forwarders, which have separate Wii and vWii builds.

  ReWii asks for console type and region only when one of these is used, or reads them from a SysCheck report.
- **vWii (the Wii mode on a Wii U):** works the same for games, GameCube via Nintendont, homebrew, cheats and saves. The differences:
  - different IOS versions for cIOS
  - no BootMii NAND restore
  - no GameCube disc dumping
  - vWii-specific forwarders and Priiloader extras

  ReWii supports vWii with a few guarded differences.
- **Wii mini:** USB-only (matches D4). It has no BootMii restore and uses a different cIOS (d2xl).

### 3.4 Scope of wii.hacks.guide (post-exploit)
Source: github.com/hacks-guide/Guide_Wii, plus wiki.hacks.guide.
- **Essentials:** NAND backup, Priiloader, d2x cIOS (IOS38/56/57/58 from Nintendo's update servers, NUS), the 4.3 update, SysCheck, Open Shop Channel, WiiLink (online services), WiiLink WFC/Wiimmfi (online play), BootMii recovery, brick triage.
- **Homebrew:** recommended apps, themes, dumping discs (CleanRip) and WADs.
- **Games:** Wii backups (ISO/WBFS/CISO on console; RVZ, WIA and NKit must be converted), GameCube via Nintendont, GameTDB covers, cheats (`txtcodes/`), Riivolution mods, forwarder channels.
- **Saves:** `data.bin` (Data Management), SaveGame Manager GX `savegames/`, GameCube `.gci`/`.raw`.
- **Gaps on other platforms:** WiiGSC (forwarders), ThemeMii and ModMii are **Windows-only**. TinyWiiBackupManager is cross-platform but only covers games and homebrew.

### 3.5 Building blocks
- **libWiiPy** (pure Python): NUS downloads, WAD pack/unpack, fakesigning and decryption keys built in.
- **GameTDB:** `wiitdb.zip` for titles, `art.gametdb.com` for covers.
- **Open Shop Channel API:** `hbb1.oscwii.org/api/v4/contents`.
- **wiidev/cheats** for txtcodes.
- **`nodtool`** (prebuilt binaries for all OSes) for RVZ/WIA/NKit conversion. It's the only external binary; ReWii downloads it on demand so users don't have to.

## 4. Drive layout ("the ReWii way")

```
USB (FAT32, MBR, 32 KB clusters)
├── apps/                         homebrew (usbloader_gx, nintendont, yawmme, …)
├── wbfs/Title [GAMEID]/GAMEID.wbfs   (+ .wbf1 … if over 4 GB)
├── games/Title [GAMEID]/game.iso     (+ disc2.iso)  GameCube for Nintendont
├── apps/usbloader_gx/images/…    covers
├── txtcodes/ , codes/            cheats
├── savegames/                    SaveGame Manager GX saves
├── saves/                        Nintendont memory cards
├── wad/                          WADs staged for YAWMME
└── rewii/                        ReWii marker, drive manifest and logs
```
Default loader: **USB Loader GX**. It's maintained and handles covers, cheats, Nintendont and per-game settings, and it's the guide standard. WiiFlow Lite is optional. CFG USB Loader is supported for legacy layouts only.

## 5. Functional requirements
Priority: **MVP** = first release, **Next** = second, **Later** = nice-to-have.

### 5.1 Drive setup
- **R1 (MVP)** Pick a drive from the list of removable drives, showing size, filesystem, partition style, cluster size and free space. Flag problems such as exFAT/NTFS, GPT, the wrong cluster size, or a flash stick.
- **R2 (MVP)** **Prepare drive:** format it FAT32 with MBR and 32 KB clusters, after a confirmation where the user types the drive name. This needs admin rights on every OS. FAT32 over 32 GB isn't possible with Windows' built-in tools, so ReWii includes its own formatter.
- **R3 (MVP)** **Set up the drive:** one-click install of the starter apps (USB Loader GX, Nintendont, YAWMME, SaveGame Manager GX, WiiXplorer) plus the folder structure.
- **R4 (Next)** Health check plus "fix it": nested `apps/apps`, wrong game folder names, oversized files, missing apps.

### 5.2 Games
- **R5 (MVP)** Library view showing games on the computer and on the drive side by side: title, ID, region, size, cover and cheat status, with titles from GameTDB.
- **R6 (MVP)** Add Wii games from ISO or WBFS. ReWii names them correctly, splits them at 4 GB and optionally scrubs them. Add GameCube games (ISO/GCM), handling two-disc games.
- **R7 (MVP)** Remove games from the drive, verify them, and copy them back to the computer.
- **R8 (Next)** Convert RVZ, WIA, NKit and CISO to something the console can load (via nodtool).
- **R9 (Next)** Join CleanRip `.partN.iso` dumps and check their MD5.
- **R10 (Next)** Import from a legacy raw WBFS partition or a disk image of one (migration path for old setups).

### 5.3 Covers, cheats, metadata
- **R11 (MVP)** Download covers (2D, 3D, full, disc) for every game into USB Loader GX's paths (WiiFlow if chosen), with region fallback.
- **R12 (Next)** Cheats: fetch txtcodes per game, and optionally build `.gct` files from selected codes.
- **R13 (Later)** Custom banners.

### 5.4 Channels and WADs
- **R14 (MVP)** Make a **game channel** (forwarder WAD) for any game, singly or in batch, so the game appears on the Wii Menu. This ports `make_wad.py`. The forwarder targets the USB layout (the FAT variant); see open question Q2. The WAD is verified after it's built.
- **R15 (MVP)** Brick blocklist: refuse forwarders for Mario Party 9, A Boy and His Blob, and others that are known to brick. Warn when the console nears the 48-channel limit.
- **R16 (MVP)** WAD manager: inspect a WAD (title, ID, type, region, IOS, valid or not) and stage chosen WADs to `wad/` for YAWMME. Optionally keep a WAD library on the computer.
- **R17 (Next)** NUS downloads for cIOS base WADs, the 4.3 System Menu and IOS. Guarded by console type and region, with confirmation.
- **R18 (Later)** Wiimmfi/WiiLink WFC patching of games and WiiWare.

### 5.5 Homebrew
- **R19 (MVP)** Open Shop Channel browser: search, install, update and remove apps, filtered to the console type. Shows installed versus latest versions.
- **R20 (Later)** Send an app over the network with wiiload.

### 5.6 Saves
- **R21 (MVP)** Find all saves on the drive (SaveGame Manager GX, Nintendont memory cards, GCMM `.gci`, and an SD `private/` folder if one is attached) and catalog them by game.
- **R22 (MVP)** Back up saves to the computer as timestamped versions, and restore any version to the drive.
- **R23 (Later)** Decrypt `data.bin`, and import/export with Dolphin.

### 5.7 Backup and restore
- **R24 (MVP)** **Full drive backup:** snapshot the whole USB, everything or everything except games, to the computer. Restore to the same or a new drive, with a size check.
- **R25 (MVP)** **NAND backups:**
  - Collect nanddumper or BootMii output (`nand.bin`, `keys.bin`) from the drive, verify it, and label it with console serial and date. The guide stresses keeping a copy in two places.
  - Build the "SD rescue kit" for a BootMii restore, which only works with the matching console's keys.
- **R26 (Next)** Drive-to-drive clone (moving to a bigger drive).

### 5.8 Guides and system
- **R27 (Next)** Guided checklists for the steps done on the Wii itself: Priiloader, cIOS, NAND backup, WiiLink, installing channels. ReWii stages the files, then shows the exact button presses (like the author's Instructions.txt, built in).
- **R28 (Next)** SysCheck import: a health report (missing cIOS, old HBC and so on) with fix actions.
- **R29 (Later)** Riivolution mods, themes, loader settings editor, cleanup and uninstall helpers.

## 6. Non-functional requirements
- **Non-technical friendly:**
  - Single-file download per OS, nothing else to install.
  - Plain-language menus with mouse and keyboard support.
  - Every screen explains what will happen.
  - First run starts a setup wizard.
- **Safety:**
  - Confirmation before any write to a drive, and formatting requires typing the drive name.
  - Nothing is deleted without offering a backup.
  - Brick-risk actions are guarded by console checks and blocklists.
  - `--dry-run` mode and a log file.
- **Cross-platform:** tested in CI on Windows, macOS and Linux. Admin rights are requested only for formatting and raw disk reads.
- **Offline-capable:** cached GameTDB, covers and OSC index. Online features fail gracefully.
- **Performance:** handles 100+ games, with progress bars and cancel on copies and conversions.
- **Scriptable (Next):** `rewii <command>` CLI equivalents for power users.
- **Legal and repo hygiene:**
  - No game data, no Nintendo binaries and no keys committed to the repo.
  - Keys come from libWiiPy at runtime.
  - Forwarder loader DOLs ship only if their license allows; otherwise they're downloaded on first use.

## 7. Tech plan
- Python 3.12 with **Textual** for the terminal UI.
- `libWiiPy` for WAD and NUS work, plus `httpx` for downloads.
- **PyInstaller** builds the single-file binaries for Windows, macOS (Intel and Apple Silicon) and Linux in GitHub Actions.
- Tests: pytest with small synthetic fixtures.

## 8. Open questions
1. **Scope of the first release:** is the "MVP" list in section 5 the right set for version 1, and should anything move earlier or later?
2. **Which loader the game channels launch:** the existing `make_wad.py` channels launch CFG USB Loader. For the USB Loader GX standard, I'll check whether a direct-to-game forwarder exists that works the same way. If not, the channels launch through CFG USB Loader's FAT build, which works fine alongside USB Loader GX. Is that acceptable?
3. **Dev testing:** can I copy one small game from the NAS for local testing (read-only on the NAS side), or should I only use synthetic test files?
