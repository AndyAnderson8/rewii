"""Step-by-step guides for the parts that happen on the Wii itself.

ReWii prepares the files on the drive ("prepare" action), then the guide tells
the user exactly what to press. Content condensed from wii.hacks.guide.
Exploiting/installing the Homebrew Channel is out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Guide:
    id: str
    title: str
    summary: str
    body: str                       # markdown
    prepare: str = ""               # action id the UI can run before the steps
    consoles: tuple[str, ...] = ("wii", "vwii", "mini", "family")
    essential: bool = False
    link: str = ""


GUIDES: list[Guide] = [
    Guide(
        "start", "Start here: the ReWii setup",
        "The order to do things in on a freshly softmodded Wii.",
        """\
Your Wii already has the **Homebrew Channel**. With ReWii everything lives on **one USB drive**.

1. **Drive → Prepare** – formats the drive FAT32 (erases it!). Skip if it's already FAT32.
2. **Drive → Set up** – installs the starter apps (USB Loader GX, Nintendont, YAWMME,
   SaveGame Manager GX, WiiXplorer, nanddumper, Priiloader, d2x cIOS installer, SysCheck).
3. On the Wii: **Back up your NAND** (guide below). Do this before anything else.
4. On the Wii: **Install Priiloader** (brick protection).
5. On the Wii: **Install the d2x cIOS** (needed to play games from USB).
6. **Games → Add games**, then **Put on drive**, then **Covers**.
7. Optional: **Make channels** so games (Wii and GameCube) show up on the Wii Menu.

Plug the drive into the **back USB port closest to the edge of the Wii** (port 0).
Hard drives and SSDs are more reliable than flash sticks. 3.5" drives need their own power.
""", essential=True),
    Guide(
        "nand-backup", "Back up your Wii's NAND",
        "A full copy of the Wii's internal memory - your undo button if anything goes wrong.",
        """\
**Do this first.** A NAND backup can save a bricked Wii.

**Prepare:** ReWii puts *nanddumper@ios* on the drive (Drive → Set up includes it).

On the Wii:
1. Open the **Homebrew Channel** and start **nanddumper@ios**.
2. Choose to dump to **USB** and wait (5–10 minutes). Don't turn the Wii off.
3. When it's done, return to the Homebrew Channel and turn the Wii off.

Back on the computer, plug the drive in and use **Backups → NAND → Collect from drive**.
ReWii checks the dump, names it by console and keeps it in your library.
Keep a second copy somewhere safe (cloud, another disk).

*BootMii* can also back up the NAND, but it needs an SD card.
""", prepare="nanddumper", essential=True, link="https://wii.hacks.guide/nand-backup"),
    Guide(
        "priiloader", "Install Priiloader (brick protection)",
        "Lets you recover from most bricks and blocks Nintendo updates.",
        """\
**Prepare:** ReWii puts *Priiloader* in apps/ on the drive.

On the Wii:
1. Open the **Homebrew Channel** → **Priiloader**.
2. Press **+** to install. Wait for "Done", then exit.
3. Turn the Wii off. Turn it on **while holding RESET** – Priiloader's menu appears.
4. **System Menu Hacks**: turn on *Block Disc Updates* and *Block Online Updates*
   (Wii mini: *Block Disc Updates*). Optional: *Remove Deflicker*. Save.
5. Choose **System Menu** to continue.

To enter Priiloader later: hold RESET while turning the Wii on.
""", prepare="priiloader", consoles=("wii", "vwii", "mini", "family"), essential=True,
        link="https://wii.hacks.guide/priiloader"),
    Guide(
        "cios", "Install the d2x cIOS (play games from USB)",
        "USB Loader GX and game channels need it.",
        """\
**Prepare:** ReWii downloads the four base IOS files from Nintendo's servers for your console
and puts them on the drive together with the d2x cIOS installer. Set your console type in
Settings first.

On the Wii (original Wii / Family Edition / vWii):
1. Homebrew Channel → **d2x cIOS Installer**. Press A to continue.
2. Set: **cIOS d2x v11-beta3**, **base 56**, **slot 249**, revision 65535. Press A, then A to install.
3. Repeat with **base 57 → slot 250**, then **base 38 → slot 248**, then **base 58 → slot 251**.
4. Press B to exit when all four are done.

Wii mini uses the special *d2xl* installer: only **base 57 → slot 249**.
""", prepare="cios", essential=True, link="https://wii.hacks.guide/cios"),
    Guide(
        "install-wads", "Install channels and WADs (YAWMME)",
        "Put game channels, Virtual Console or WiiWare titles on the Wii Menu.",
        """\
**Prepare:** Games → *Make channel*, or Channels & WADs → *Put on drive*. Files go to the
drive's **wad/** folder. GameCube channels start the game in Nintendont; make them after the
game is on the drive, and make them again if you rename or move the game.

On the Wii:
1. Homebrew Channel → **YAWM ModMii Edition**. Choose **USB** as the source if asked.
2. It opens the **wad/** folder. Highlight a WAD, press **+** to mark it (mark several if you like).
3. Press **A** and choose **Install**. Wait until each one says *Success*.
4. Press HOME to exit. The new channels are on the Wii Menu.

Never install WADs meant for another region or for the Wii U (vWii) – ReWii warns you.
To remove a channel, use the Wii's own Data Management, or YAWMME's *Uninstall*.
""", link="https://wii.hacks.guide/yawmme"),
    Guide(
        "usbloadergx", "Play games with USB Loader GX",
        "The game launcher for Wii and GameCube games on your drive.",
        """\
1. Homebrew Channel → **USB Loader GX** (or its channel, if you installed one).
2. Your games appear with covers. Pick one and press **A** to start.
3. GameCube games run through **Nintendont** automatically; use a GameCube controller,
   or a Classic Controller / supported USB pad.
4. Cheats: in a game's settings turn on **Ocarina** and pick codes (download them in Games → Cheats).

If games don't show up: make sure the drive is in the right USB port and the cIOS is installed.
""", link="https://wii.hacks.guide/wii-loaders"),
    Guide(
        "saves", "Back up and restore game saves",
        "Copy saves between the Wii and your computer.",
        """\
**Wii saves** – use *SaveGame Manager GX* (installed by Drive → Set up):
1. Homebrew Channel → **SaveGame Manager GX**.
2. **Backup**: pick saves → *Extract*. They go to the drive's **savegames/** folder.
3. On the computer: Saves → **Back up from drive**.
4. **Restore**: Saves → *Restore to drive*, then in SaveGame Manager GX use *Install*.

**GameCube saves** (Nintendont): kept on the drive in **saves/** as memory card files.
ReWii backs these up directly – nothing to do on the Wii.
""", link="https://wiki.hacks.guide/wiki/Wii:Transferring_Game_Saves"),
    Guide(
        "online", "Play online again (WiiLink WFC / Wiimmfi)",
        "Nintendo's online service closed in 2014; these replacements bring it back.",
        """\
**Wiimmfi:** ReWii sets USB Loader GX's **private server** option for you
(Settings → Online play server). Start the game from USB Loader GX and use its online mode.

**WiiLink WFC:** install the **WiiLink WFC Patcher** app (Homebrew section) and run it from the
Homebrew Channel; it patches your installed games and channels.

For discs and game channels, Priiloader's *Wiimmfi patch* hack also works. For WiiConnect24 channels (Forecast, News, Check Mii Out…) see
https://wiilink.ca.
""", link="https://wii.hacks.guide/nintendowfc"),
    Guide(
        "syscheck", "Check your Wii's setup (SysCheck)",
        "Generates a report ReWii can read to tell you what's missing.",
        """\
1. Homebrew Channel → **SysCheck ME** → press A to run the check, then *Save*.
2. It saves **syscheck.csv** to the drive.
3. On the computer: System → **Read SysCheck report**.
""", prepare="syscheck", link="https://wii.hacks.guide/syscheck"),
    Guide(
        "dump", "Rip your own discs (CleanRip)",
        "Make backups of Wii and GameCube discs you own.",
        """\
1. Homebrew Channel → **CleanRip**. Choose **USB**, FAT32, and *chunk size: max*.
2. Insert the disc, choose *new device* answers as asked, and let it dump (can take ~1 hour).
3. On the computer: Games → **Add games**, pick the *.part0.iso* – ReWii joins the parts and
   checks the dump.

GameCube discs can't be ripped on a Wii mini, Family Edition or Wii U.
""", prepare="cleanrip", link="https://wii.hacks.guide/dump-games"),
    Guide(
        "update-hbc", "Update the Homebrew Channel / BootMii",
        "Reinstall or update the Homebrew Channel, or install BootMii.",
        """\
The Homebrew Channel and BootMii are installed by the **HackMii installer**.

**Prepare:** ReWii downloads the official HackMii installer (from bootmii.org) to
apps/hackmii_installer on the drive.

On the Wii:
1. Homebrew Channel → **HackMii Installer**. Wait for the scam warning, then press 1.
2. Choose **Install the Homebrew Channel** to update it (it keeps your apps), then Yes.
3. Optional: **BootMii** → *Install BootMii as IOS* (as boot2 if offered). BootMii itself runs
   from an **SD card** - copy the `bootmii` folder from your NAND backup's rescue kit to one.
4. Choose **Exit**.
""", prepare="hackmii", link="https://wii.hacks.guide/hackmii"),
    Guide(
        "priiloader-hacks", "Priiloader hacks (patches)",
        "Keep Priiloader's hack list current, or add a patch such as the Wii U clock fix.",
        """\
Priiloader's *System Menu Hacks* come from **hacks_hash.ini** in apps/priiloader on the drive.

- **Update hack list:** System → *Priiloader hacks* → *Update* downloads the newest official list
  (patches you added are kept).
- **Add a patch:** paste the text from a patch generator - for example the Wii U (vWii) clock fix
  from https://garyodernichts.github.io/priiloader-patch-gen - and ReWii checks and adds it.

Then on the Wii: hold RESET while turning it on → Priiloader → **System Menu Hacks** → turn the
hack on → **Save settings**.
""", prepare="priiloader-hacks", link="https://wii.hacks.guide/priiloader"),
    Guide(
        "recovery", "If something goes wrong",
        "What to do if the Wii won't start normally.",
        """\
- **Hold RESET while turning on** → Priiloader. From there you can start the Homebrew
  Channel and fix things (e.g. reinstall a WAD or remove a bad theme).
- **BootMii restore** (needs an SD card): Backups → NAND → *Make SD rescue kit* puts your NAND
  backup and BootMii on an SD card. Never restore another Wii's backup.
- Black screen after installing a channel: hold RESET → Priiloader → Homebrew Channel →
  YAWMME → uninstall that WAD.
- More help: https://wii.hacks.guide/bricks and the r/WiiHacks community.
""", link="https://wii.hacks.guide/bricks"),
]


def get(gid: str) -> Guide:
    for g in GUIDES:
        if g.id == gid:
            return g
    raise KeyError(gid)
