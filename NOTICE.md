# Third-party components

## Files shipped inside ReWii

ReWii ships these files unmodified in `src/rewii/assets/forwarder/` (modified copies are generated
at run time when a channel is built). The SHA-256 of each file as shipped is given so the exact
binary can be identified.

| File | What | License | Source | SHA-256 |
|---|---|---|---|---|
| `ConfUsbLoader.dol` | Configurable USB Loader (by oggzee and contributors), "direct start" build for WBFS-partition drives, as distributed with CRAP | GPLv2 | https://github.com/jayfen7/cfg-loader (export of the Google Code project) | `6f97f62a693ad3ce8fbf959e84f27232fe8bd0c9e6a559599c086fa3591a20c9` |
| `ConfUsbLoaderFat.dol` | The same loader, FAT/NTFS build | GPLv2 | as above | `21d6d3ab198205effd2ea4567c60dbfe35a8c3dceeaf67352ed24b8d8d6699b7` |
| `nand_loader.app` | NAND loader stub that boots the channel's DOL, as emitted by CRAP | believed GPLv2 - **unverified** | bundled with CRAP | `b2034ac3b46bc10d8de702830d7c3f79ffd0d6cfd78f4548a73ca20e65ae1fd8` |
| `gc_banner_template.bnr` | GameCube channel banner template (`data/binary/custom_banner.bnr`) from USB Loader GX by Dimok and contributors | GPLv3 | https://github.com/wiidev/usbloadergx | `40ff3bb76cca1d7c0e9c9c9cc13250f9dca1c62263b18be57bbd800a71b8b31c` |
| `ticket_template.tik`, `tmd_template.tmd` | Fakesigned ticket/TMD templates from CRAP (data structures, no program code) | no license stated - **unverified** | bundled with CRAP | `e4e9285cd760b411472491f9d2ae67f9bb661dca29da2532d58b2910d6d27f90`, `237121d170b07a323bf744ef53879263865da87685a418788ff06e8cd4c4aa36` |

### What is not verified - stated plainly

- **Exact upstream revisions are not known.** The two loader builds and the NAND loader were taken
  from a CRAP distribution, which does not record which cfg-loader revision they were built from,
  and nobody has rebuilt them from source to compare. The banner template was copied from the
  USB Loader GX repository without recording the commit. The table therefore pins the *binaries*
  (by hash), not source revisions.
- **Corresponding source.** The links above are the upstream projects' source repositories. Because
  the exact revisions are unknown, ReWii cannot currently point at the exact corresponding source
  for these GPL binaries. Anyone redistributing ReWii should treat this as an open item: either
  identify the revisions, or replace the binaries with ones built from a known revision.
- **`nand_loader.app`'s author and license** are assumed (it is commonly attributed to marcan's
  open-source NAND loader, GPLv2) and have not been confirmed against a source.
- **The ticket/TMD templates** carry no license statement of their own.

## Downloaded at run time (not shipped)

- Nintendo's certificate chain and system titles: Nintendo's update servers (NUS)
- Homebrew apps: Open Shop Channel (https://oscwii.org)
- Cover art and the title database: GameTDB (https://www.gametdb.com)
- Cheat codes: https://codes.rc24.xyz
- Nintendont (embedded in GameCube channels when they're built; GPLv2): https://github.com/FIX94/Nintendont via the Open Shop Channel
- `nodtool` (disc conversion): https://github.com/encounter/nod (MIT/Apache-2.0), verified against a pinned SHA-256
- BootMii SD files and the HackMii / d2xl installers: bootmii.org and wii.hacks.guide (verified against pinned hashes)

## Python libraries

ReWii's own dependencies (`pyproject.toml`) and everything they pull in. All of these are inside
the single-file app built with PyInstaller (exact versions: `uv.lock`):

| Library | License |
|---|---|
| libWiiPy | MIT |
| requests (used by libWiiPy) | Apache-2.0 |
| urllib3, charset-normalizer (used by requests) | MIT |
| certifi (CA bundle; used by requests and httpx) | MPL-2.0 |
| idna (used by requests and httpx) | BSD-3-Clause |
| httpx, httpcore | BSD-3-Clause |
| h11, anyio | MIT |
| Textual, Rich | MIT |
| markdown-it-py, mdurl, mdit-py-plugins, linkify-it-py (used by Textual/Rich) | MIT |
| Pygments (used by Rich) | BSD-2-Clause |
| typing_extensions | PSF-2.0 |
| Pillow | MIT-CMU (HPND) |
| pycryptodome | BSD-2-Clause, Unlicense (public domain) and MIT parts |
| psutil | BSD-3-Clause |
| platformdirs | MIT |

The packaged app also contains the Python interpreter and standard library (PSF-2.0) and the
PyInstaller bootloader (GPLv2-or-later with an exception that permits distributing the bundled
application under any license). The single-file app does not currently carry these libraries'
license texts inside it; they are available from each project, and from the `*.dist-info` folders
of a normal `uv sync` install.
