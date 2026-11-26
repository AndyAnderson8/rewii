# Third-party components

ReWii ships these files unmodified (modified copies are generated at run time when building a channel):

| File | What | License | Source |
|---|---|---|---|
| `src/rewii/assets/forwarder/ConfUsbLoader.dol`, `ConfUsbLoaderFat.dol` | Configurable USB Loader (by oggzee), direct-start builds as distributed with CRAP | GPLv2 | https://github.com/jayfen7/cfg-loader (Google Code export) |
| `src/rewii/assets/forwarder/nand_loader.app` | marcan's NAND loader, as emitted by CRAP | GPLv2 (unverified) | bundled with CRAP |
| `src/rewii/assets/forwarder/gc_banner_template.bnr` | GameCube channel banner template (`data/binary/custom_banner.bnr`) from USB Loader GX by Dimok and contributors | GPLv3 | https://github.com/wiidev/usbloadergx |
| `src/rewii/assets/forwarder/*_template.tik/.tmd` | Fakesigned ticket/TMD templates from CRAP | — | — |

These are downloaded at run time and not bundled:

- Nintendo certificate chain and system titles: Nintendo's update servers (NUS)
- Homebrew apps: Open Shop Channel (https://oscwii.org)
- Cover art and the title database: GameTDB (https://www.gametdb.com)
- Cheat codes: https://codes.rc24.xyz
- Nintendont (embedded in GameCube channels when they're built; GPLv2): https://github.com/FIX94/Nintendont via the Open Shop Channel
- `nodtool` (disc conversion): https://github.com/encounter/nod (MIT/Apache-2.0)
- BootMii SD files: wii.hacks.guide (checked against pinned hashes)

Python libraries: libWiiPy (MIT), Textual and Rich (MIT), httpx (BSD), pycryptodome (BSD/Public Domain), Pillow (MIT-CMU), psutil (BSD), platformdirs (MIT).
