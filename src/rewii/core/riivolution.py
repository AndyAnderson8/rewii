"""Install, list and remove Riivolution mods on a drive.

Riivolution reads ``<device>/riivolution/*.xml`` on SD or USB (format:
https://riivolution.github.io/wiki/Patch_Format/ ; path rules from
github.com/AerialX/rawksd ``launcher/source/riivolution_config.cpp``):

* the XML's own folder (``/riivolution``) is the starting root;
* ``<wiidisc root=...>`` then ``<patch root=...>`` are combined onto it -
  a value starting with ``/`` is absolute from the drive root, otherwise relative;
* every ``external`` / ``valuefile`` is combined onto its patch root the same way.

ReWii copies the mod's XMLs to ``riivolution/`` and each referenced data
folder to where those rules point, records what it placed in
``rewii/riivolution.json`` and keeps a copy of the mod in ``settings.mods_dir``.
"""
from __future__ import annotations

import json
import posixpath
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from .config import Settings
from .layout import DriveLayout, safe_name

RECORD = "riivolution.json"
JUNK = {"__macosx", ".ds_store", "thumbs.db", "desktop.ini"}


class ModError(Exception):
    """A mod is invalid or can't be installed/removed."""


# --------------------------------------------------------------------------- XML
@dataclass
class GameTarget:
    """One ``<id>`` element: game prefix (3-6 chars) and allowed region letters."""
    game: str
    regions: list[str] = field(default_factory=list)
    developer: str = ""

    def matches(self, game_id: str) -> bool:
        """Riivolution's rule: prefix match on ``game``, region = 4th char of the id."""
        gid = game_id.upper()
        if self.game and not gid.startswith(self.game.upper()):
            return False
        if self.regions and (len(gid) < 4 or gid[3] not in self.regions):
            return False
        if self.developer and len(gid) >= 6 and gid[4:6] != self.developer.upper():
            return False
        return True


@dataclass
class RiivoXml:
    """Parsed Riivolution XML."""
    path: Optional[Path]
    targets: list[GameTarget]
    patches: dict[str, str]                       # patch id -> resolved root (drive path)
    externals: list[str]                          # resolved drive paths (POSIX, absolute)
    savegame_paths: list[str] = field(default_factory=list)

    @property
    def game_ids(self) -> list[str]:
        """Human-readable target list, e.g. ``['RMCE', 'RMCP']`` or ``['RMC']``."""
        out = []
        for t in self.targets:
            if t.regions:
                out += [t.game + r for r in t.regions]
            else:
                out.append(t.game or "*")
        return out

    def targets_game(self, game_id: str) -> bool:
        return not self.targets or any(t.matches(game_id) for t in self.targets)

    def top_dirs(self) -> set[str]:
        """First path components of all external paths (folders the mod needs)."""
        return {PurePosixPath(p).parts[1] for p in self.externals
                if len(PurePosixPath(p).parts) > 1}


def combine(root: str, attr: str) -> str:
    """Riivolution ``AbsolutePathCombine``: absolute if ``attr`` starts with '/'."""
    attr = attr.replace("\\", "/")
    joined = attr if attr.startswith("/") else posixpath.join(root, attr)
    norm = posixpath.normpath("/" + joined.lstrip("/"))
    return "/" if norm in (".", "//") else norm


def parse_xml(source: Path | str | bytes, xml_dir: str = "/riivolution") -> RiivoXml:
    """Parse a Riivolution XML file (path) or its text; raises :class:`ModError`."""
    path: Optional[Path] = None
    try:
        if isinstance(source, Path):
            path = source
            root = ET.parse(source).getroot()
        else:
            root = ET.fromstring(source)
    except (ET.ParseError, OSError) as e:
        raise ModError(f"XML doesn't parse: {e}") from e
    if root.tag != "wiidisc":
        raise ModError("not a Riivolution XML (root element must be <wiidisc>)")
    if root.get("version", "1") != "1":
        raise ModError("unsupported Riivolution XML version")
    xmlroot = combine(xml_dir, root.get("root", "")) if root.get("root") else xml_dir
    targets = []
    for idel in root.findall("id"):
        regions = [r.get("type", "").upper() for r in idel.findall("region") if r.get("type")]
        targets.append(GameTarget(idel.get("game", ""), regions, idel.get("developer", "")))
    patches: dict[str, str] = {}
    externals: list[str] = []
    saves: list[str] = []
    for p in root.findall("patch"):
        proot = combine(xmlroot, p.get("root", "")) if p.get("root") else xmlroot
        patches[p.get("id", "")] = proot
        for el in p:
            for attr in ("external", "valuefile"):
                v = el.get(attr)
                if v and "{$" not in v:
                    full = combine(proot, v)
                    (saves if el.tag == "savegame" else externals).append(full)
    return RiivoXml(path, targets, patches, externals, saves)


# --------------------------------------------------------------------------- sources
def _safe_extract(zpath: Path, dest: Path) -> None:
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise ModError(f"unsafe path in zip: {info.filename!r}")
            target = dest.joinpath(*parts) if parts else dest
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fi, open(target, "wb") as fo:
                    shutil.copyfileobj(fi, fo)


def _find_base(folder: Path) -> tuple[Path, Path]:
    """Return ``(base, xml_folder)``: base is the folder treated as the drive root."""
    cands = [p for p in folder.rglob("*") if p.is_dir() and p.name.lower() == "riivolution"
             and not any(x.lower() in JUNK for x in p.relative_to(folder).parts)
             and any(q.suffix.lower() == ".xml" for q in p.iterdir())]
    if cands:
        cands.sort(key=lambda p: len(p.parts))
        return cands[0].parent, cands[0]
    xmls = [p for p in folder.rglob("*.xml")
            if not any(x.lower() in JUNK for x in p.relative_to(folder).parts)]
    if not xmls:
        raise ModError("no Riivolution XML found in the mod")
    xmls.sort(key=lambda p: len(p.parts))
    return xmls[0].parent, xmls[0].parent


def _resolve_src(base: Path, xml_folder: Path, drive_path: str) -> Optional[Path]:
    """Map a drive path like ``/mymod/files`` to the mod source."""
    rel = PurePosixPath(drive_path).relative_to("/")
    p = base.joinpath(*rel.parts)
    if p.exists():
        return p
    # mods packed without the riivolution/ wrapper: relative to the XML's folder
    if rel.parts and rel.parts[0].lower() == "riivolution":
        p = xml_folder.joinpath(*rel.parts[1:])
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- install
@dataclass
class InstallResult:
    """What :func:`install_mod` did."""
    name: str
    xmls: list[RiivoXml]
    placed: list[str]                  # drive paths (POSIX, relative to the root)
    warnings: list[str]
    library_copy: Optional[Path]


def _record_path(drive_root: Path) -> Path:
    return DriveLayout(drive_root).rewii / RECORD


def _load_record(drive_root: Path) -> dict:
    try:
        return json.loads(_record_path(drive_root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_record(drive_root: Path, data: dict) -> None:
    p = _record_path(drive_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), "utf-8")


def _copy_tree(src: Path, dst: Path, overwrite: bool, placed: list[str], root: Path) -> None:
    items = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for f in items:
        if f.name.lower() in JUNK:
            continue
        target = dst if src.is_file() else dst / f.relative_to(src)
        if target.exists() and not overwrite:
            if target.read_bytes() != f.read_bytes():
                raise ModError(f"{target.relative_to(root).as_posix()} already exists and is "
                               "different (another mod?); use overwrite to replace it")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        placed.append(target.relative_to(root).as_posix())


def install_mod(source: Path, drive_root: Path, settings: Settings | None = None, *,
                name: str | None = None, game_id: str | None = None,
                overwrite: bool = False) -> InstallResult:
    """Install a Riivolution mod (zip or folder) onto a drive.

    Every XML must parse. With ``game_id`` (e.g. ``RMCE01``), a warning is added
    when no XML targets that game/region. Missing referenced files are warned
    about, not fatal. A copy of the mod is kept in ``settings.mods_dir``.
    """
    source, drive_root = Path(source), Path(drive_root)
    name = safe_name(name or source.stem)
    with tempfile.TemporaryDirectory(prefix="rewii-mod-") as td:
        if source.is_file():
            if not zipfile.is_zipfile(source):
                raise ModError(f"{source.name} isn't a zip file or folder")
            folder = Path(td)
            _safe_extract(source, folder)
        elif source.is_dir():
            folder = source
        else:
            raise ModError(f"{source} doesn't exist")
        base, xml_folder = _find_base(folder)
        xml_files = sorted(p for p in xml_folder.iterdir()
                           if p.is_file() and p.suffix.lower() == ".xml")
        xmls = [parse_xml(p) for p in xml_files]
        warnings: list[str] = []
        if game_id and not any(x.targets_game(game_id) for x in xmls):
            want = ", ".join(sorted({g for x in xmls for g in x.game_ids})) or "?"
            warnings.append(f"This mod is for {want}, not {game_id.upper()} (wrong game or "
                            "region); Riivolution won't offer it for that disc.")
        # what to copy: each external resolved to a source; group by drive path
        copies: dict[str, Path] = {}
        missing: list[str] = []
        for x in xmls:
            for ext in x.externals:
                src = _resolve_src(base, xml_folder, ext)
                if src is None:
                    missing.append(ext)
                else:
                    copies[ext] = src
        if missing:
            warnings.append(f"{len(missing)} file(s) the XML refers to aren't in the mod, e.g. "
                            + ", ".join(sorted(missing)[:3]))
        lay = DriveLayout(drive_root)
        placed: list[str] = []
        lay.riivolution.mkdir(parents=True, exist_ok=True)
        for p in xml_files:
            _copy_tree(p, lay.riivolution / p.name, overwrite, placed, drive_root)
        # copy whole top-level referenced folders when the mod ships them (keeps
        # files referenced through {$params} we can't resolve)
        done: set[str] = set()
        for ext, src in sorted(copies.items()):
            parts = PurePosixPath(ext).parts
            top = "/" + parts[1] if len(parts) > 1 else ext
            if top.lower() == "/riivolution":
                top = "/" + "/".join(parts[1:3]) if len(parts) > 2 else ext
            top_src = _resolve_src(base, xml_folder, top)
            unit, unit_src = (top, top_src) if top_src is not None else (ext, src)
            if unit in done or any(unit.startswith(d + "/") for d in done):
                continue
            done.add(unit)
            dst = drive_root.joinpath(*PurePosixPath(unit).parts[1:])
            if dst.resolve() == drive_root.resolve():
                continue
            _copy_tree(unit_src, dst, overwrite, placed, drive_root)
        lib: Optional[Path] = None
        if settings is not None:
            lib = settings.mods_dir / name
            if lib.exists():
                shutil.rmtree(lib)
            lib.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                lib.mkdir()
                shutil.copy2(source, lib / source.name)
            else:
                shutil.copytree(source, lib)
    rec = _load_record(drive_root)
    rec[name] = {"xmls": [p.name for p in xml_files], "paths": sorted(set(placed)),
                 "games": sorted({g for x in xmls for g in x.game_ids}),
                 "source": str(source)}
    _save_record(drive_root, rec)
    return InstallResult(name, xmls, sorted(set(placed)), warnings, lib)


# --------------------------------------------------------------------------- list / remove
@dataclass
class InstalledMod:
    """A mod on the drive. ``managed`` = installed by ReWii (removable)."""
    name: str
    xmls: list[str]
    games: list[str]
    paths: list[str]
    managed: bool


def list_installed_mods(drive_root: Path) -> list[InstalledMod]:
    """Mods installed by ReWii plus any other XMLs found in ``riivolution/``."""
    drive_root = Path(drive_root)
    rec = _load_record(drive_root)
    out = [InstalledMod(n, d.get("xmls", []), d.get("games", []), d.get("paths", []), True)
           for n, d in sorted(rec.items())]
    known = {x.lower() for m in out for x in m.xmls}
    rdir = DriveLayout(drive_root).riivolution
    if rdir.is_dir():
        for p in sorted(rdir.iterdir()):
            if p.is_file() and p.suffix.lower() == ".xml" and p.name.lower() not in known:
                try:
                    games = parse_xml(p).game_ids
                except ModError:
                    games = []
                out.append(InstalledMod(p.stem, [p.name], games, [f"riivolution/{p.name}"],
                                        False))
    return out


def remove_mod(drive_root: Path, name: str) -> list[str]:
    """Remove a ReWii-installed mod's files (shared files used by other mods stay).

    Returns the removed paths. Empty folders left behind are pruned.
    """
    drive_root = Path(drive_root)
    rec = _load_record(drive_root)
    if name not in rec:
        raise ModError(f"{name} wasn't installed by ReWii")
    others = {p for n, d in rec.items() if n != name for p in d.get("paths", [])}
    removed = []
    dirs: set[Path] = set()
    for rel in rec[name].get("paths", []):
        if rel in others:
            continue
        p = drive_root.joinpath(*PurePosixPath(rel).parts)
        if p.is_file():
            p.unlink()
            removed.append(rel)
            dirs.update(p.parents)
    root = drive_root.resolve()
    riiv = DriveLayout(drive_root).riivolution.resolve()
    for d in sorted(dirs, key=lambda x: -len(x.parts)):
        rd = d.resolve()
        if rd == root or rd == riiv or root not in rd.parents:
            continue
        try:
            d.rmdir()
        except OSError:
            pass
    del rec[name]
    _save_record(drive_root, rec)
    return removed
